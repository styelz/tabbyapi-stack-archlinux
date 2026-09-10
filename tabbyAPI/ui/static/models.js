function mountModels(root) {
  root.innerHTML = `
    <div class="models-page">
      <div class="models-job" id="models-job" hidden>
        <div class="models-job-copy">
          <strong id="models-job-title">Download</strong>
          <span class="muted" id="models-job-msg"></span>
        </div>
        <div class="models-job-bar" aria-hidden="true">
          <span id="models-job-fill"></span>
        </div>
        <span class="models-job-pct" id="models-job-pct">0%</span>
        <button type="button" class="btn" id="models-job-cancel">Cancel</button>
      </div>
      <p class="error" id="models-error" hidden></p>
      <p class="muted" id="models-ok" hidden></p>
      <div class="models-layout">
        <section class="card models-pane">
          <div class="models-pane-head">
            <h2>Library</h2>
            <span class="muted" id="models-disk"></span>
            <span class="spacer"></span>
            <button type="button" class="btn" id="models-refresh">Refresh</button>
          </div>
          <p class="muted" id="models-lib-empty" hidden>No models on disk yet.</p>
          <table class="models-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Kind</th>
                <th class="num">Size</th>
                <th></th>
              </tr>
            </thead>
            <tbody id="models-lib-body"></tbody>
          </table>
        </section>
        <section class="card models-pane">
          <div class="models-pane-head">
            <h2>Discover</h2>
            <span class="muted" id="models-token-hint"></span>
          </div>
          <div class="models-catalog" id="models-catalog"></div>
          <form class="models-search" id="models-search-form">
            <input id="models-q" type="search" placeholder="Search Hugging Face or paste org/repo" autocomplete="off" />
            <div class="range-bar models-format" role="group" aria-label="Format">
              <button type="button" class="range-seg is-active" data-format="exl3">EXL3</button>
              <button type="button" class="range-seg" data-format="exl2">EXL2</button>
            </div>
            <button class="btn primary" type="submit">Search</button>
          </form>
          <div id="models-repo" class="models-repo" hidden></div>
          <div id="models-results" class="models-results"></div>
        </section>
      </div>
    </div>
  `;

  const err = root.querySelector("#models-error");
  const ok = root.querySelector("#models-ok");
  const jobEl = root.querySelector("#models-job");
  const jobTitle = root.querySelector("#models-job-title");
  const jobMsg = root.querySelector("#models-job-msg");
  const jobFill = root.querySelector("#models-job-fill");
  const jobPct = root.querySelector("#models-job-pct");
  const jobCancel = root.querySelector("#models-job-cancel");
  const diskEl = root.querySelector("#models-disk");
  const tokenHint = root.querySelector("#models-token-hint");
  const libBody = root.querySelector("#models-lib-body");
  const libEmpty = root.querySelector("#models-lib-empty");
  const catalogEl = root.querySelector("#models-catalog");
  const resultsEl = root.querySelector("#models-results");
  const repoEl = root.querySelector("#models-repo");
  const searchForm = root.querySelector("#models-search-form");
  const queryInput = root.querySelector("#models-q");
  let format = "exl3";
  let pollTimer = 0;
  let paused = false;
  let lastJobId = "";
  let loading = false;

  function showError(message) {
    err.hidden = !message;
    err.textContent = message || "";
    if (message) ok.hidden = true;
  }

  function showOk(message) {
    ok.hidden = !message;
    ok.textContent = message || "";
    if (message) err.hidden = true;
  }

  function kindLabel(kind) {
    if (kind === "image") return "Image";
    if (kind === "embed") return "Embed";
    return "LLM";
  }

  function jobBusy(job) {
    const status = job && job.status;
    return status === "queued" || status === "running" || status === "cancelling";
  }

  function paintJob(job) {
    if (!job || (!jobBusy(job) && job.status !== "done" && job.status !== "error" && job.status !== "cancelled")) {
      if (!jobBusy(job)) jobEl.hidden = true;
      return;
    }
    jobEl.hidden = false;
    jobEl.classList.toggle("is-error", job.status === "error");
    jobEl.classList.toggle("is-done", job.status === "done");
    const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
    jobFill.style.width = `${percent}%`;
    jobPct.textContent = `${percent}%`;
    jobTitle.textContent = job.label || "Download";
    const bits = [job.message || job.status || ""];
    if (job.file) bits.push(job.file);
    if (job.bytes_total) {
      bits.push(`${TabbyUI.formatBytes(job.bytes_done || 0)} / ${TabbyUI.formatBytes(job.bytes_total)}`);
    }
    jobMsg.textContent = bits.filter(Boolean).join(" · ");
    jobCancel.hidden = !jobBusy(job);
    jobCancel.disabled = job.status === "cancelling";
  }

  function libraryRows(data) {
    const rows = [];
    (data.llms || []).forEach((row) => rows.push(row));
    (data.images || []).forEach((row) => rows.push(row));
    return rows;
  }

  function paintLibrary(data) {
    const disk = data.disk || {};
    if (disk.free_bytes != null) {
      diskEl.textContent = `${TabbyUI.formatBytes(disk.free_bytes)} free`;
    } else {
      diskEl.textContent = "";
    }
    tokenHint.textContent = data.has_token
      ? ""
      : "Set a Hugging Face token in Settings for gated repos.";
    const rows = libraryRows(data);
    libEmpty.hidden = rows.length > 0;
    libBody.innerHTML = rows
      .map((row) => {
        const name = TabbyUI.escapeHtml(row.pretty || row.label || row.id);
        const id = TabbyUI.escapeHtml(row.id);
        const kind = TabbyUI.escapeHtml(kindLabel(row.kind));
        const size = TabbyUI.formatBytes(row.size_bytes || 0);
        const badges = [];
        if (row.loaded) badges.push('<span class="models-badge is-on">Loaded</span>');
        if (row.profile) badges.push(`<span class="muted">${TabbyUI.escapeHtml(row.profile)}</span>`);
        if (row.partial) badges.push('<span class="models-badge">Incomplete</span>');
        const load =
          row.kind === "llm" && row.profile
            ? `<button type="button" class="btn" data-load="${TabbyUI.escapeHtml(row.profile)}" ${row.loaded ? "disabled" : ""}>Load</button>`
            : "";
        return `<tr data-id="${id}" data-kind="${TabbyUI.escapeHtml(row.kind)}">
          <td><strong>${name}</strong><div class="muted models-sub">${id}${badges.length ? " · " : ""}${badges.join(" ")}</div></td>
          <td class="muted">${kind}</td>
          <td class="num">${TabbyUI.escapeHtml(size)}</td>
          <td class="models-actions">${load}<button type="button" class="btn danger" data-del="${id}">Delete</button></td>
        </tr>`;
      })
      .join("");
  }

  function paintCatalog(data) {
    const picks = data.catalog || [];
    catalogEl.innerHTML = picks
      .map((pick) => {
        const id = TabbyUI.escapeHtml(pick.id);
        const label = TabbyUI.escapeHtml(pick.label);
        const meta = [];
        if (pick.disk_gib) meta.push(`~${pick.disk_gib} GiB`);
        if (pick.min_vram_mib) meta.push(`${Math.round(pick.min_vram_mib / 1024)} GB VRAM`);
        const status = pick.installed
          ? '<span class="models-badge is-on">Installed</span>'
          : pick.partial
            ? '<span class="models-badge">Incomplete</span>'
            : "";
        const action = pick.installed
          ? ""
          : `<button type="button" class="btn primary" data-catalog="${id}">Download</button>`;
        return `<div class="models-pick" data-id="${id}">
          <div>
            <strong>${label}</strong>
            <div class="muted models-sub">${TabbyUI.escapeHtml(meta.join(" · "))}</div>
          </div>
          <div class="models-actions">${status}${action}</div>
        </div>`;
      })
      .join("");
  }

  function paintResults(payload) {
    const rows = (payload && payload.results) || [];
    if (!rows.length) {
      resultsEl.innerHTML = payload && payload.query
        ? `<p class="muted">No EXL${payload.format === "exl2" ? "2" : "3"} models matched.</p>`
        : "";
      return;
    }
    resultsEl.innerHTML = rows
      .map((row) => {
        const id = TabbyUI.escapeHtml(row.id);
        const extra = [];
        if (row.downloads) extra.push(`${Number(row.downloads).toLocaleString()} downloads`);
        if (row.gated) extra.push("gated");
        if (!row.compatible) extra.push("not EXL2/EXL3");
        return `<button type="button" class="models-hit" data-repo="${id}">
          <strong>${id}</strong>
          <span class="muted">${TabbyUI.escapeHtml(extra.join(" · "))}</span>
        </button>`;
      })
      .join("");
  }

  function paintRepo(data) {
    if (!data) {
      repoEl.hidden = true;
      repoEl.innerHTML = "";
      return;
    }
    repoEl.hidden = false;
    const id = TabbyUI.escapeHtml(data.id);
    const note = data.gguf_only
      ? '<p class="error">This repo looks like GGUF. Tabby needs EXL2/EXL3.</p>'
      : data.compatible
        ? ""
        : '<p class="muted">This may not be an EXL2/EXL3 snapshot. Download only if you know it will load.</p>';
    const revs = (data.revisions || [])
      .map((rev) => {
        const name = TabbyUI.escapeHtml(rev.name);
        const size = rev.size_bytes != null ? TabbyUI.formatBytes(rev.size_bytes) : "size unknown";
        const disabled = data.gguf_only ? "disabled" : "";
        return `<tr>
          <td><code>${name}</code></td>
          <td class="num">${TabbyUI.escapeHtml(size)}</td>
          <td class="models-actions">
            <button type="button" class="btn primary" data-hf="${id}" data-rev="${name}" data-size="${rev.size_bytes || ""}" ${disabled}>Download</button>
          </td>
        </tr>`;
      })
      .join("");
    repoEl.innerHTML = `
      <div class="models-repo-head">
        <strong>${id}</strong>
        <button type="button" class="btn" id="models-repo-close">Close</button>
      </div>
      ${note}
      <table class="models-table">
        <thead><tr><th>Revision</th><th class="num">Size</th><th></th></tr></thead>
        <tbody>${revs || '<tr><td colspan="3" class="muted">No branches listed.</td></tr>'}</tbody>
      </table>
    `;
  }

  async function loadLibrary() {
    const data = await TabbyUI.api("models");
    paintLibrary(data);
    paintCatalog(data);
    paintJob(data.job);
    if (data.job && data.job.status === "error") showError(data.job.error || data.job.message);
    if (data.job && data.job.status === "done" && data.job.id && data.job.id !== lastJobId) {
      showOk(data.job.message || "Download finished");
    }
    lastJobId = (data.job && data.job.id) || lastJobId;
    return data;
  }

  async function pollJob() {
    if (paused) return;
    try {
      const data = await TabbyUI.api("models/job");
      const job = data.job;
      paintJob(job);
      if (jobBusy(job)) return;
      stopPoll();
      if (job && (job.status === "done" || job.status === "error" || job.status === "cancelled")) {
        await loadLibrary();
        if (job.status === "done") showOk(job.message || "Download finished");
        if (job.status === "error") showError(job.error || job.message || "Download failed");
        if (job.status === "cancelled") showOk(job.message || "Cancelled");
      }
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = window.setInterval(pollJob, 1000);
  }

  function stopPoll() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = 0;
  }

  async function beginDownload(body) {
    showError("");
    showOk("");
    const data = await TabbyUI.api("models/download", { method: "POST", body });
    paintJob(data.job);
    startPoll();
  }

  async function openRepo(repoId) {
    showError("");
    const data = await TabbyUI.api(`models/repo?id=${encodeURIComponent(repoId)}`);
    paintRepo(data);
  }

  libBody.addEventListener("click", async (event) => {
    const del = event.target.closest("[data-del]");
    const load = event.target.closest("[data-load]");
    try {
      if (load) {
        load.disabled = true;
        showOk("Loading…");
        await TabbyUI.api("gpu", { method: "POST", body: { mode: load.getAttribute("data-load") } });
        await loadLibrary();
        showOk("Loaded.");
        return;
      }
      if (del) {
        const id = del.getAttribute("data-del");
        const row = del.closest("tr");
        const kind = (row && row.getAttribute("data-kind")) || "llm";
        const yes = await TabbyUI.confirmModal({
          title: "Delete model",
          text: `Remove ${id} from this host? This cannot be undone.`,
          yes: "Delete",
          no: "Keep",
        });
        if (!yes) return;
        await TabbyUI.api("models/delete", { method: "POST", body: { kind, id } });
        await loadLibrary();
        showOk(`Deleted ${id}`);
      }
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  });

  catalogEl.addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-catalog]");
    if (!btn) return;
    try {
      await beginDownload({ kind: "catalog", pick_id: btn.getAttribute("data-catalog") });
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  });

  resultsEl.addEventListener("click", async (event) => {
    const hit = event.target.closest("[data-repo]");
    if (!hit) return;
    try {
      await openRepo(hit.getAttribute("data-repo"));
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  });

  repoEl.addEventListener("click", async (event) => {
    if (event.target.closest("#models-repo-close")) {
      paintRepo(null);
      return;
    }
    const btn = event.target.closest("[data-hf]");
    if (!btn) return;
    const size = btn.getAttribute("data-size");
    try {
      await beginDownload({
        kind: "hf",
        repo_id: btn.getAttribute("data-hf"),
        revision: btn.getAttribute("data-rev"),
        size_bytes: size ? Number(size) : null,
      });
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  });

  searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const q = String(queryInput.value || "").trim();
    if (!q) return;
    showError("");
    resultsEl.innerHTML = `<p class="muted">Searching…</p>`;
    try {
      const data = await TabbyUI.api(
        `models/search?q=${encodeURIComponent(q)}&format=${encodeURIComponent(format)}`
      );
      paintResults(data);
      const exact = data.results && data.results.length === 1 && /[\/]/.test(q);
      if (exact && data.results[0].id) await openRepo(data.results[0].id);
    } catch (exc) {
      resultsEl.innerHTML = "";
      showError(exc.message || String(exc));
    }
  });

  root.querySelector(".models-format").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-format]");
    if (!btn) return;
    format = btn.getAttribute("data-format") || "exl3";
    root.querySelectorAll(".models-format .range-seg").forEach((el) => {
      el.classList.toggle("is-active", el === btn);
    });
  });

  root.querySelector("#models-refresh").addEventListener("click", async () => {
    showError("");
    try {
      await loadLibrary();
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  });

  jobCancel.addEventListener("click", async () => {
    try {
      await TabbyUI.api("models/job/cancel", { method: "POST", body: {} });
      startPoll();
    } catch (exc) {
      showError(exc.message || String(exc));
    }
  });

  async function boot() {
    if (loading) return;
    loading = true;
    try {
      const data = await loadLibrary();
      if (jobBusy(data.job)) startPoll();
    } catch (exc) {
      showError(exc.message || String(exc));
    } finally {
      loading = false;
    }
  }

  boot();

  return {
    resume() {
      paused = false;
      boot();
    },
    pause() {
      paused = true;
      stopPoll();
    },
  };
}

window.mountModels = mountModels;
