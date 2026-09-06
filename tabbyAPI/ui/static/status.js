function mountStatus(root) {
  root.innerHTML = `
    <div class="toolbar status-toolbar">
      <button class="btn" id="status-refresh">Refresh</button>
      <span class="spacer"></span>
      <span class="muted" id="status-stamp"></span>
    </div>
    <div class="status-layout">
      <aside class="status-side">
        <div class="card status-facts" id="status-cards"></div>
        <div class="card status-actions">
          <h2>Actions</h2>
          <div class="actions-grid">
            <select id="profile-select" aria-label="Profile"></select>
            <button class="btn" id="switch-llm">Load LLM</button>
            <button class="btn" id="switch-comfy">To Comfy</button>
            <button class="btn danger" id="restart-btn" hidden>Restart</button>
            <button class="btn" id="update-git" hidden>Update git</button>
            <button class="btn" id="update-all" hidden>Update all</button>
          </div>
          <p class="muted" id="action-msg"></p>
        </div>
      </aside>
      <section class="card metrics-panel">
        <div class="metrics-toolbar">
          <strong class="metrics-title">Graphs</strong>
          <div class="range-bar" role="group" aria-label="Time range">
            <button type="button" class="range-seg" data-hours="1">1h</button>
            <button type="button" class="range-seg" data-hours="6">6h</button>
            <button type="button" class="range-seg is-active" data-hours="24">24h</button>
            <button type="button" class="range-seg" data-days="7">7d</button>
            <button type="button" class="range-seg" data-days="30">30d</button>
          </div>
          <form class="range-custom" id="metrics-custom" title="Custom window">
            <input type="number" id="metrics-amount" min="0.25" max="720" step="0.25" value="24" aria-label="Custom amount" />
            <select id="metrics-unit" aria-label="Unit">
              <option value="hours" selected>h</option>
              <option value="days">d</option>
            </select>
            <button type="submit" class="btn range-go">Go</button>
          </form>
          <span class="muted metrics-meta" id="metrics-meta"></span>
        </div>
        <div class="charts">
          <figure class="chart-card">
            <figcaption>
              <strong>GPU</strong>
              <span class="legend">
                <span class="swatch" style="--c:var(--accent)"></span>util %
                <span class="swatch" style="--c:var(--accent-2)"></span>VRAM %
                <span class="swatch" style="--c:var(--warn)"></span>°C
              </span>
            </figcaption>
            <canvas id="chart-gpu" width="900" height="240" aria-label="GPU chart"></canvas>
          </figure>
          <figure class="chart-card">
            <figcaption>
              <strong>Host</strong>
              <span class="legend">
                <span class="swatch" style="--c:var(--ok)"></span>CPU %
                <span class="swatch" style="--c:var(--bad)"></span>RAM %
                <span class="swatch" style="--c:var(--muted)"></span>load×10
              </span>
            </figcaption>
            <canvas id="chart-host" width="900" height="240" aria-label="Host chart"></canvas>
          </figure>
        </div>
      </section>
    </div>
  `;
  const cards = root.querySelector("#status-cards");
  const select = root.querySelector("#profile-select");
  const msg = root.querySelector("#action-msg");
  const meta = root.querySelector("#metrics-meta");
  const amountInput = root.querySelector("#metrics-amount");
  const unitSelect = root.querySelector("#metrics-unit");
  let range = { hours: 24, days: null };
  let lastSeries = [];
  let lastPayload = null;
  let actionBusy = false;

  function themeColor(name, fallback) {
    return (TabbyUI.cssVar && TabbyUI.cssVar(name)) || fallback;
  }

  function occupancyLabel(data) {
    const queue = (data && data.stack_queue) || {};
    if (queue.queued) return queue.hint || "You are in a queue";
    if (queue.mine) return queue.hint || "Your session is running";
    if (queue.busy) return "In use";
    return "Free";
  }

  function occupancyExtra(data) {
    const queue = (data && data.stack_queue) || {};
    const parts = [];
    if (queue.occupant) parts.push(queue.occupant);
    if (queue.kind) parts.push(queue.kind);
    if (queue.waiters) parts.push(`${queue.waiters} waiting`);
    return parts.join(" · ");
  }

  function fact(title, value, extra = "") {
    const sub = extra ? `<span class="fact-x">${TabbyUI.escapeHtml(extra)}</span>` : "";
    const plain = `${title}: ${String(value).replace(/<[^>]+>/g, "")}${extra ? ` (${extra})` : ""}`;
    return `<div class="status-fact" data-copy="${TabbyUI.escapeHtml(plain)}"><span class="fact-k">${TabbyUI.escapeHtml(title)}</span><span class="fact-v">${value}</span>${sub}</div>`;
  }

  function syncCustomFields() {
    if (range.days != null) {
      amountInput.value = String(range.days);
      unitSelect.value = "days";
    } else {
      amountInput.value = String(range.hours);
      unitSelect.value = "hours";
    }
  }

  function setActivePreset() {
    root.querySelectorAll(".range-seg").forEach((btn) => {
      const h = btn.dataset.hours ? Number(btn.dataset.hours) : null;
      const d = btn.dataset.days ? Number(btn.dataset.days) : null;
      const on =
        range.days != null
          ? d === range.days
          : h != null && range.hours === h && range.days == null;
      btn.classList.toggle("is-active", on);
    });
    syncCustomFields();
  }

  function metricsQuery() {
    if (range.days != null) return `days=${encodeURIComponent(range.days)}`;
    return `hours=${encodeURIComponent(range.hours)}`;
  }

  function formatAxisTime(ts, windowS) {
    const d = new Date(ts * 1000);
    if (windowS > 48 * 3600) {
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit" });
    }
    if (windowS > 3 * 3600) {
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  function drawChart(canvas, series, lines, yMaxHint, windowS) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 900;
    const cssH = canvas.clientHeight || 240;
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const pad = { l: 40, r: 10, t: 12, b: 26 };
    const w = cssW - pad.l - pad.r;
    const h = cssH - pad.t - pad.b;
    ctx.fillStyle = themeColor("--hover-fill", "rgba(255,255,255,0.02)");
    ctx.fillRect(pad.l, pad.t, w, h);

    let yMax = yMaxHint || 100;
    for (const line of lines) {
      for (const row of series) {
        const v = row[line.key];
        if (typeof v === "number" && Number.isFinite(v)) {
          yMax = Math.max(yMax, line.scale ? v * line.scale : v);
        }
      }
    }
    yMax = Math.max(1, Math.ceil(yMax / 10) * 10);

    ctx.strokeStyle = themeColor("--line", "rgba(255,255,255,0.08)");
    ctx.fillStyle = themeColor("--muted", "#9aa3b5");
    const z = window.TabbyUI && TabbyUI.getZoom ? TabbyUI.getZoom() / 100 : 1;
    ctx.font = `${Math.max(8, Math.round(11 * z))}px ui-monospace, Menlo, Consolas, monospace`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (h * i) / 4;
      const val = Math.round(yMax * (1 - i / 4));
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + w, y);
      ctx.stroke();
      ctx.fillText(String(val), pad.l - 6, y);
    }

    if (!series.length) {
      ctx.textAlign = "center";
      ctx.fillStyle = themeColor("--muted", "#9aa3b5");
      ctx.fillText("Collecting samples… wait ~30s", pad.l + w / 2, pad.t + h / 2);
      return;
    }

    const t0 = series[0].t;
    const t1 = series[series.length - 1].t;
    const span = Math.max(1, t1 - t0);

    function xAt(t) {
      return pad.l + ((t - t0) / span) * w;
    }
    function yAt(v) {
      return pad.t + h * (1 - Math.min(yMax, Math.max(0, v)) / yMax);
    }

    for (const line of lines) {
      const pts = [];
      for (const row of series) {
        const v = row[line.key];
        if (typeof v !== "number" || !Number.isFinite(v)) continue;
        pts.push([xAt(row.t), yAt(line.scale ? v * line.scale : v)]);
      }
      if (pts.length < 2) continue;
      ctx.beginPath();
      ctx.strokeStyle = line.color;
      ctx.lineWidth = 1.8;
      ctx.lineJoin = "round";
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
    }

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = themeColor("--muted", "#9aa3b5");
    const ticks = Math.min(5, series.length);
    for (let i = 0; i < ticks; i++) {
      const row = series[Math.round((i * (series.length - 1)) / Math.max(1, ticks - 1))];
      ctx.fillText(formatAxisTime(row.t, windowS), xAt(row.t), pad.t + h + 6);
    }
  }

  function paintCharts(payload) {
    const series = payload.series || [];
    lastSeries = series;
    lastPayload = payload;
    const windowS = payload.window_s || (range.days != null ? range.days * 86400 : range.hours * 3600);
    drawChart(
      root.querySelector("#chart-gpu"),
      series,
      [
        { key: "gpu", color: themeColor("--accent", "#7aa2ff") },
        { key: "vram", color: themeColor("--accent-2", "#8b5cf6") },
        { key: "temp", color: themeColor("--warn", "#f5c542") },
      ],
      100,
      windowS
    );
    drawChart(
      root.querySelector("#chart-host"),
      series,
      [
        { key: "cpu", color: themeColor("--ok", "#3dd68c") },
        { key: "ram", color: themeColor("--bad", "#ff6b7a") },
        { key: "load1", color: themeColor("--muted", "#9aa3b5"), scale: 10 },
      ],
      100,
      windowS
    );
    const hoursLabel =
      payload.days >= 1 && payload.hours >= 24
        ? `${Number(payload.days) === Math.floor(payload.days) ? Math.floor(payload.days) : payload.days}d`
        : `${payload.hours}h`;
    meta.textContent = series.length
      ? `${series.length} pts · ${hoursLabel} · ~${payload.interval_s || 30}s`
      : "No samples yet";
  }

  let metricsGen = 0;
  async function refreshMetrics() {
    const gen = ++metricsGen;
    try {
      const data = await TabbyUI.api(`metrics?${metricsQuery()}&max_points=720`);
      if (gen !== metricsGen) return data;
      paintCharts(data);
      return data;
    } catch (err) {
      if (gen !== metricsGen) return;
      throw err;
    }
  }

  async function refresh() {
    const data = await TabbyUI.api("status");
    const gpu = data.gpu || {};
    const host = data.host || {};
    const model = data.model || {};
    const health = data.health || {};
    const healthLabel = health.healthy ? "healthy" : "unhealthy";
    const healthClass = health.healthy ? "ok" : "bad";
    const gpuName = String(gpu.name || "n/a")
      .replace(/^NVIDIA\s+GeForce\s+/i, "")
      .replace(/^NVIDIA\s+/i, "");
    cards.innerHTML = [
      fact("GPU", TabbyUI.escapeHtml(data.gpu_mode || "unknown"), data.comfy_up ? "Comfy up" : "Comfy idle"),
      fact("Stack", TabbyUI.escapeHtml(occupancyLabel(data)), occupancyExtra(data)),
      fact("Profile", TabbyUI.escapeHtml(data.profile || "—"), data.tabby_model || "LLM unloaded"),
      fact("Context", TabbyUI.escapeHtml(String(model.max_seq_len || "—")), model.cache_mode ? `cache ${model.cache_mode}` : ""),
      fact(
        "Health",
        `<span class="fact-pill ${healthClass}">${healthLabel}</span>`,
        (health.issues || []).join("; ") || "no issues"
      ),
      fact("Uptime", TabbyUI.escapeHtml(TabbyUI.formatDuration(data.uptime_s)), data.api_base || ""),
      fact("CPU", host.cpu_pct != null ? `${host.cpu_pct}%` : "—", host.load1 != null ? `load ${host.load1}` : ""),
      fact("RAM", host.ram_pct != null ? `${host.ram_pct}%` : "—", ""),
      fact(
        "NVIDIA",
        TabbyUI.escapeHtml(gpuName),
        gpu.memory_total_mib
          ? `${gpu.memory_used_mib}/${gpu.memory_total_mib} MiB · ${gpu.utilization_pct}% · ${gpu.temperature_c}°C`
          : ""
      ),
    ].join("");
    const profiles = data.profiles || [];
    const labels = data.profile_labels || {};
    select.innerHTML = profiles
      .map((name) => {
        const pretty = labels[name];
        const text = pretty && pretty !== name ? `${name} — ${pretty}` : name;
        return `<option value="${TabbyUI.escapeHtml(name)}">${TabbyUI.escapeHtml(text)}</option>`;
      })
      .join("");
    if (data.profile) select.value = data.profile;
    root.querySelector("#status-stamp").textContent = data.now || "";
    TabbyUI.paintGpuChip(data);
    await refreshMetrics().catch((err) => {
      meta.textContent = err.message;
    });
    return data;
  }

  async function act(fn) {
    msg.textContent = "Working…";
    try {
      const result = await fn();
      msg.textContent = result.message || result.detail || "Done.";
      await refresh();
    } catch (err) {
      msg.textContent = err.message;
      TabbyUI.paintApiDown(err);
    }
  }

  function finishProgress(modal, { title, note, reloadPrimary = false } = {}) {
    if (title) modal.setTitle(title);
    if (note) modal.setNote(note);
    modal.setBusy(false);
    modal.stopJournal();
    modal.stopUpdateLog();
    const closeBtn = { label: "Close", run: () => modal.close() };
    const reloadBtn = {
      label: "Reload UI",
      primary: reloadPrimary,
      run: () => location.reload(),
    };
    modal.setActions(reloadPrimary ? [closeBtn, reloadBtn] : [reloadBtn, { ...closeBtn, primary: true }]);
  }

  async function runRestart(modal, { fromUpdate = false } = {}) {
    msg.textContent = "Restarting…";
    await TabbyUI.followRestart(modal);
    msg.textContent = "API is back.";
    await refresh().catch((err) => TabbyUI.paintApiDown(err));
    finishProgress(modal, {
      reloadPrimary: fromUpdate,
      note: fromUpdate
        ? "TabbyAPI is healthy again. Reload the UI to pick up updated pages."
        : "TabbyAPI is healthy again.",
    });
  }

  function applyRange() {
    setActivePreset();
    refreshMetrics().catch((err) => {
      meta.textContent = err.message;
    });
  }

  root.querySelector("#status-refresh").addEventListener("click", () => refresh().catch((err) => {
    msg.textContent = err.message;
    TabbyUI.paintApiDown(err);
  }));
  root.querySelector("#switch-llm").addEventListener("click", () =>
    act(() => TabbyUI.api("gpu", { method: "POST", body: { mode: select.value || "llm" } }))
  );
  root.querySelector("#switch-comfy").addEventListener("click", () =>
    act(() => TabbyUI.api("gpu", { method: "POST", body: { mode: "comfy" } }))
  );
  const adminActionIds = ["restart-btn", "update-git", "update-all"];
  TabbyUI.api("auth/check")
    .then((data) => {
      if (!data.is_admin) return;
      adminActionIds.forEach((id) => {
        const el = root.querySelector(`#${id}`);
        if (el) el.hidden = false;
      });
    })
    .catch(() => {});
  root.querySelector("#restart-btn").addEventListener("click", async () => {
    if (actionBusy) return;
    const yes = await TabbyUI.confirmModal({
      title: "Restart API?",
      text: "Restart TabbyAPI now? The UI will drop for about a minute.",
      yes: "Restart",
      no: "Cancel",
    });
    if (!yes) return;
    actionBusy = true;
    const modal = TabbyUI.progressModal({
      title: "Restarting",
      note: "Restarting TabbyAPI. The UI will drop for about a minute.",
    });
    try {
      await runRestart(modal);
    } catch (err) {
      msg.textContent = err.message;
      finishProgress(modal, {
        title: "Restart",
        note: err.message || "Restart failed.",
      });
    } finally {
      actionBusy = false;
    }
  });
  root.querySelector("#update-git").addEventListener("click", async () => {
    if (actionBusy) return;
    actionBusy = true;
    msg.textContent = "Updating git…";
    const modal = TabbyUI.progressModal({
      title: "Updating git",
      note: "Pulling origin. If API code changed, TabbyAPI restarts by itself.",
    });
    modal.startUpdateLog();
    modal.startJournal();
    try {
      const result = await TabbyUI.api("update", { method: "POST", body: { full: false } });
      modal.ingestText(result.log || "");
      if (!result.ok && !result.already_running && !/already running/i.test(result.message || "")) {
        msg.textContent = result.message || "Git update failed.";
        finishProgress(modal, {
          title: "Git update failed",
          note: result.message || "Git update failed to start.",
        });
        return;
      }
      if (result.message) modal.setNote(result.message);
      modal.setTitle("Updating git");
      await modal.waitUntilReady({ requireDown: false, watchUpdate: true });
      msg.textContent = "API is back.";
      await refresh().catch((err) => TabbyUI.paintApiDown(err));
      finishProgress(modal, {
        title: "Git update finished",
        note: "Git update finished. Reload the UI to pick up updated pages.",
        reloadPrimary: true,
      });
    } catch (err) {
      msg.textContent = err.message;
      TabbyUI.paintApiDown(err);
      finishProgress(modal, {
        title: "Git update failed",
        note: err.message || "Git update failed.",
      });
    } finally {
      actionBusy = false;
    }
  });
  root.querySelector("#update-all").addEventListener("click", async () => {
    if (actionBusy) return;
    const yes = await TabbyUI.confirmModal({
      title: "Full update?",
      text: "Run a full update (git + deps) and restart?",
      yes: "Update",
      no: "Cancel",
    });
    if (!yes) return;
    actionBusy = true;
    msg.textContent = "Updating…";
    const modal = TabbyUI.progressModal({
      title: "Full update",
      note: "Running git + deps. TabbyAPI will restart when that finishes.",
    });
    modal.startUpdateLog();
    modal.startJournal();
    try {
      const result = await TabbyUI.api("update", { method: "POST", body: { full: true } });
      modal.ingestText(result.log || "");
      if (!result.ok && !result.already_running && !/already running/i.test(result.message || "")) {
        msg.textContent = result.message || "Full update failed.";
        finishProgress(modal, {
          title: "Update failed",
          note: result.message || "Full update failed to start.",
        });
        return;
      }
      if (result.message) modal.setNote(result.message);
      modal.setTitle("Updating, then restarting");
      await modal.waitUntilReady({ requireDown: true, watchUpdate: true });
      msg.textContent = "API is back.";
      await refresh().catch((err) => TabbyUI.paintApiDown(err));
      finishProgress(modal, {
        title: "API is back",
        note: "Full update finished and TabbyAPI is healthy again. Reload the UI to pick up updated pages.",
        reloadPrimary: true,
      });
    } catch (err) {
      msg.textContent = err.message;
      TabbyUI.paintApiDown(err);
      finishProgress(modal, {
        title: "Update failed",
        note: err.message || "Full update failed.",
      });
    } finally {
      actionBusy = false;
    }
  });

  root.querySelectorAll(".range-seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.days) {
        range = { hours: null, days: Number(btn.dataset.days) };
      } else {
        range = { hours: Number(btn.dataset.hours), days: null };
      }
      applyRange();
    });
  });

  root.querySelector("#metrics-custom").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const amount = Number(amountInput.value);
    const unit = unitSelect.value;
    if (!Number.isFinite(amount) || amount <= 0) {
      meta.textContent = "Enter a positive number.";
      return;
    }
    if (unit === "days") {
      range = { hours: null, days: Math.min(30, amount) };
    } else {
      range = { hours: Math.min(720, amount), days: null };
    }
    applyRange();
  });

  const onResize = () => {
    if (!lastSeries.length) return;
    paintCharts({
      series: lastSeries,
      window_s: range.days != null ? range.days * 86400 : range.hours * 3600,
      hours: range.days != null ? range.days * 24 : range.hours,
      days: range.days != null ? range.days : (range.hours || 0) / 24,
      interval_s: 30,
    });
  };
  window.addEventListener("resize", onResize);
  const onTheme = () => {
    if (lastPayload) paintCharts(lastPayload);
  };
  document.addEventListener("tabby-theme-change", onTheme);

  cards.addEventListener("contextmenu", (event) => {
    const factEl = event.target.closest(".status-fact");
    if (!factEl || !cards.contains(factEl)) return;
    const one = factEl.dataset.copy || factEl.innerText || "";
    const all = Array.from(cards.querySelectorAll(".status-fact"))
      .map((node) => node.dataset.copy || node.innerText || "")
      .filter(Boolean)
      .join("\n");
    TabbyUI.showContextMenu(event, [
      { label: "Copy", run: () => TabbyUI.copyText(one) },
      { label: "Copy all facts", run: () => TabbyUI.copyText(all) },
    ]);
  });

  setActivePreset();
  refresh().catch((err) => {
    msg.textContent = err.message;
    TabbyUI.paintApiDown(err);
  });
  let timer = setInterval(() => refresh().catch((err) => TabbyUI.paintApiDown(err)), 15000);
  return {
    pause() {
      if (timer) {
        clearInterval(timer);
        timer = 0;
      }
    },
    resume() {
      if (!timer) {
        refresh().catch((err) => TabbyUI.paintApiDown(err));
        timer = setInterval(() => refresh().catch((err) => TabbyUI.paintApiDown(err)), 15000);
      }
    },
    destroy() {
      if (timer) clearInterval(timer);
      timer = 0;
      window.removeEventListener("resize", onResize);
      document.removeEventListener("tabby-theme-change", onTheme);
    },
  };
}

window.mountStatus = mountStatus;
