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
        <button type="button" class="btn" id="models-job-cancel" data-action="cancel">Cancel</button>
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
          <p class="muted" id="models-lib-empty" hidden>No models in the catalog yet.</p>
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
          <form class="models-search" id="models-search-form">
            <input id="models-q" type="search" placeholder="Search Hugging Face or paste org/repo" autocomplete="off" />
            <div class="range-bar models-format" role="group" aria-label="Format">
              <button type="button" class="range-seg is-active" data-format="exl3">EXL3</button>
              <button type="button" class="range-seg" data-format="exl2">EXL2</button>
            </div>
            <button class="btn primary" type="submit">Search</button>
          </form>
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
  const resultsEl = root.querySelector("#models-results");
  const searchForm = root.querySelector("#models-search-form");
  const queryInput = root.querySelector("#models-q");
  let format = "exl3";
  let pollTimer = 0;
  let hideTimer = 0;
  let paused = false;
  let lastJobId = "";
  let paintedJobId = "";
  let loading = false;
  let hfAlias = "";
  let repoSeq = 0;
  const JOB_DONE_TTL_MS = 90 * 1000;
  const JOB_DONE_HIDE_MS = 8 * 1000;
  const JOB_DISMISS_KEY = "tabby-models-dismissed-job";

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

  function timeAgo(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const days = Math.floor((Date.now() - t) / 86400000);
    if (days <= 0) return "today";
    if (days < 30) return `${days}d ago`;
    if (days < 365) return `${Math.floor(days / 30)}mo ago`;
    return `${Math.floor(days / 365)}y ago`;
  }

  function jobBusy(job) {
    const status = job && job.status;
    return status === "queued" || status === "running" || status === "cancelling";
  }

  function jobTerminal(job) {
    const status = job && job.status;
    return status === "done" || status === "error" || status === "cancelled";
  }

  function jobUpdatedAt(job) {
    const t = Date.parse((job && (job.updated_at || job.started_at)) || "");
    return Number.isFinite(t) ? t : 0;
  }

  function dismissedJobId() {
    try {
      return sessionStorage.getItem(JOB_DISMISS_KEY) || "";
    } catch (_exc) {
      return "";
    }
  }

  function rememberDismissed(id) {
    try {
      if (id) sessionStorage.setItem(JOB_DISMISS_KEY, id);
    } catch (_exc) {
      /* ignore quota / private mode */
    }
  }

  function jobIsFresh(job) {
    const t = jobUpdatedAt(job);
    return t > 0 && Date.now() - t < JOB_DONE_TTL_MS;
  }

  function shouldPaintJob(job) {
    if (!job) return false;
    if (jobBusy(job)) return true;
    if (!jobTerminal(job)) return false;
    if (job.id && job.id === dismissedJobId()) return false;
    return jobIsFresh(job);
  }

  function clearHideTimer() {
    if (hideTimer) window.clearTimeout(hideTimer);
    hideTimer = 0;
  }

  function dismissPaintedJob() {
    clearHideTimer();
    if (paintedJobId) rememberDismissed(paintedJobId);
    jobEl.hidden = true;
    paintedJobId = "";
  }

  function paintJob(job) {
    if (!shouldPaintJob(job)) {
      if (!jobBusy(job)) {
        clearHideTimer();
        jobEl.hidden = true;
      }
      return;
    }
    jobEl.hidden = false;
    paintedJobId = job.id || paintedJobId;
    jobEl.classList.toggle("is-error", job.status === "error");
    jobEl.classList.toggle("is-done", job.status === "done");
    const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
    jobFill.style.width = `${percent}%`;
    jobPct.textContent = `${percent}%`;
    jobTitle.textContent = job.label || "Download";
    const bits = [job.message || job.status || ""];
    if (jobBusy(job)) {
      if (job.file) bits.push(job.file);
      if (job.bytes_total) {
        bits.push(`${TabbyUI.formatBytes(job.bytes_done || 0)} / ${TabbyUI.formatBytes(job.bytes_total)}`);
      }
    }
    jobMsg.textContent = bits.filter(Boolean).join(" · ");
    const busy = jobBusy(job);
    jobCancel.hidden = false;
    jobCancel.disabled = job.status === "cancelling";
    jobCancel.textContent = busy ? "Cancel" : "Dismiss";
    jobCancel.dataset.action = busy ? "cancel" : "dismiss";
    if (jobTerminal(job) && job.status !== "error") {
      if (!hideTimer) hideTimer = window.setTimeout(dismissPaintedJob, JOB_DONE_HIDE_MS);
    } else {
      clearHideTimer();
    }
  }

  function libraryRows(data) {
    const llms = data.llms || [];
    const catalog = data.catalog || [];
    const seen = new Set();
    const rows = [];

    catalog.forEach((pick) => {
      const llm = llms.find((row) => row.catalog_id === pick.id);
      if (llm) seen.add(llm.folder || llm.id);
      const installed = Boolean(pick.installed || llm);
      const kind = pick.kind || (pick.id === "embed" ? "embed" : "llm");
      rows.push({
        id: kind === "image" ? pick.id : llm ? llm.id : pick.id,
        catalog_id: pick.id,
        kind,
        pretty: pick.label || (llm && llm.pretty) || pick.id,
        profile: llm && llm.profile,
        folder: llm && (llm.folder || llm.id),
        local_profile: Boolean(llm && llm.local_profile),
        loaded: Boolean(llm && llm.loaded),
        size_bytes: Number((llm && llm.size_bytes) || pick.size_bytes || 0),
        disk_gib: pick.disk_gib,
        min_vram_mib: pick.min_vram_mib,
        installed,
        partial: Boolean(pick.partial && !pick.installed),
      });
    });

    llms.forEach((llm) => {
      const key = llm.folder || llm.id;
      if (llm.catalog_id || seen.has(key)) return;
      rows.push(Object.assign({}, llm, { installed: true, catalog_id: null }));
    });

    rows.sort((a, b) => {
      if (Boolean(a.loaded) !== Boolean(b.loaded)) return a.loaded ? -1 : 1;
      if (Boolean(a.installed) !== Boolean(b.installed)) return a.installed ? -1 : 1;
      return String(a.pretty || a.id).localeCompare(String(b.pretty || b.id));
    });
    return rows;
  }

  function actionSlot(html) {
    return html || '<span class="models-action-gap" aria-hidden="true"></span>';
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
    const rowHtml = (row) => {
        const name = TabbyUI.escapeHtml(row.pretty || row.label || row.id);
        const id = TabbyUI.escapeHtml(row.id);
        const catalogId = TabbyUI.escapeHtml(row.catalog_id || "");
        const kind = TabbyUI.escapeHtml(kindLabel(row.kind));
        const size = row.installed || row.partial
          ? TabbyUI.formatBytes(row.size_bytes || 0)
          : row.disk_gib
            ? `~${row.disk_gib} GiB`
            : "—";
        const badges = [];
        if (row.loaded) badges.push('<span class="models-badge is-on">Loaded</span>');
        else if (row.installed) badges.push('<span class="models-badge is-on">Installed</span>');
        if (row.partial) badges.push('<span class="models-badge">Incomplete</span>');
        if (row.profile && !row.local_profile) {
          badges.push(`<span class="muted">switch to ${TabbyUI.escapeHtml(row.profile)}</span>`);
        }
        if (row.profile && row.local_profile) {
          badges.push(
            `<button type="button" class="models-alias-btn" data-alias="${TabbyUI.escapeHtml(row.profile)}" data-folder="${TabbyUI.escapeHtml(row.folder || row.id)}">switch to ${TabbyUI.escapeHtml(row.profile)}</button>`
          );
        }
        if (!row.installed && row.min_vram_mib) {
          badges.push(`${Math.round(row.min_vram_mib / 1024)} GB VRAM`);
        }
        const sub = badges.join(" ") || "Not installed";
        const canLoad = row.kind === "llm" && row.profile && row.installed && !row.partial;
        const load = canLoad
          ? `<button type="button" class="btn" data-load="${TabbyUI.escapeHtml(row.profile)}" ${row.loaded ? "disabled" : ""}>Load</button>`
          : "";
        const download = (!row.installed || row.partial) && row.catalog_id
          ? `<button type="button" class="btn primary" data-catalog="${catalogId}">Download</button>`
          : "";
        const primary = download || load;
        const del = row.installed || row.partial
          ? `<button type="button" class="btn danger" data-del="${id}">Delete</button>`
          : "";
        return `<tr data-id="${id}" data-kind="${TabbyUI.escapeHtml(row.kind)}" data-catalog="${catalogId}">
          <td><strong>${name}</strong><div class="muted models-sub">${sub}</div></td>
          <td class="muted">${kind}</td>
          <td class="num">${TabbyUI.escapeHtml(size)}</td>
          <td class="models-actions">${actionSlot(primary)}${actionSlot(del)}</td>
        </tr>`;
    };
    const groupHeader = (label) =>
      `<tr class="models-group"><td colspan="4"><span class="models-group-label">${label}</span></td></tr>`;
    const installed = rows.filter((row) => row.installed || row.partial);
    const available = rows.filter((row) => !row.installed && !row.partial);
    const chunks = [];
    if (installed.length) {
      chunks.push(groupHeader("Installed"), ...installed.map(rowHtml));
    }
    if (available.length) {
      chunks.push(groupHeader("Available to download"), ...available.map(rowHtml));
    }
    libBody.innerHTML = chunks.join("");
  }

  function paintResults(payload) {
    const rows = (payload && payload.results) || [];
    if (!rows.length) {
      resultsEl.innerHTML = payload && payload.query
        ? `<p class="muted">No EXL${payload.format === "exl2" ? "2" : "3"} models matched.</p>`
        : "";
      return;
    }
    const fmtLabel = payload.format === "exl2" ? "EXL2" : "EXL3";
    resultsEl.innerHTML = rows
      .map((row) => {
        const id = TabbyUI.escapeHtml(row.id);
        const badges = [];
        badges.push(
          row.compatible
            ? `<span class="models-badge is-on">${fmtLabel}</span>`
            : '<span class="models-badge is-warn">not EXL2/EXL3</span>'
        );
        if (row.gated) badges.push('<span class="models-badge is-warn">gated</span>');
        const meta = [];
        if (row.downloads) meta.push(`${Number(row.downloads).toLocaleString()} downloads`);
        if (row.likes) meta.push(`${Number(row.likes).toLocaleString()} likes`);
        const updated = timeAgo(row.last_modified);
        if (updated) meta.push(`updated ${updated}`);
        return `<article class="models-hit" data-repo="${id}">
          <button type="button" class="models-hit-toggle" data-repo="${id}" aria-expanded="false">
            <span class="models-hit-top">
              <strong>${id}</strong>
              <span class="models-hit-badges">${badges.join("")}</span>
            </span>
            <span class="muted models-hit-meta">${TabbyUI.escapeHtml(meta.join(" · "))}</span>
            <span class="models-hit-cta">View files &amp; sizes →</span>
          </button>
          <div class="models-repo" hidden></div>
        </article>`;
      })
      .join("");
  }

  function hitRepoSlot(hit) {
    return hit && hit.querySelector ? hit.querySelector(".models-repo") : null;
  }

  function rememberAliasFrom(scope) {
    if (!scope || !scope.querySelector) return;
    const prevAlias = scope.querySelector("#models-hf-alias");
    if (prevAlias) hfAlias = String(prevAlias.value || "").trim();
  }

  function setHitOpen(hit, open) {
    if (!hit) return;
    hit.classList.toggle("is-open", open);
    const toggle = hit.querySelector(".models-hit-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    const cta = hit.querySelector(".models-hit-cta");
    if (cta) cta.innerHTML = open ? "Hide files &amp; sizes" : "View files &amp; sizes →";
  }

  function closeRepo(hit) {
    repoSeq += 1;
    const hits = hit
      ? [hit]
      : Array.from(resultsEl.querySelectorAll(".models-hit.is-open"));
    hits.forEach((el) => {
      rememberAliasFrom(el);
      setHitOpen(el, false);
      const slot = hitRepoSlot(el);
      if (!slot) return;
      slot.hidden = true;
      slot.innerHTML = "";
    });
  }

  function paintRepo(data, hit) {
    rememberAliasFrom(resultsEl);
    if (!data || !hit || !hit.isConnected) {
      closeRepo(hit || null);
      return;
    }
    resultsEl.querySelectorAll(".models-hit.is-open").forEach((el) => {
      if (el !== hit) closeRepo(el);
    });
    const slot = hitRepoSlot(hit);
    if (!slot) return;
    setHitOpen(hit, true);
    slot.hidden = false;
    const id = TabbyUI.escapeHtml(data.id);
    const note = data.gguf_only
      ? '<p class="error">This repo looks like GGUF. Tabby needs EXL2/EXL3.</p>'
      : data.compatible
        ? ""
        : '<p class="muted">This may not be an EXL2/EXL3 snapshot. Download only if you know it will load.</p>';
    const revs = data.revisions || [];
    const sized = revs.filter((rev) => rev.fits === true || rev.fits === false);
    const tight = revs.filter((rev) => rev.fits === false);
    const vramGb = Number(data.vram_gb) || 0;
    const headBadges = [];
    if (data.compatible) headBadges.push('<span class="models-badge is-on">Compatible</span>');
    if (sized.length && tight.length === sized.length) {
      const badge = tight[0].vram_badge || (vramGb ? `won't fit ${vramGb} GB` : "");
      if (badge) headBadges.push(`<span class="models-badge is-warn">${TabbyUI.escapeHtml(badge)}</span>`);
    }
    if (data.gated) headBadges.push('<span class="models-badge is-warn">gated</span>');
    const vramNote = tight.length && vramGb
      ? `<p class="muted">This GPU has ${vramGb} GB. Revisions larger than that will not load.</p>`
      : "";
    const revRows = revs
      .map((rev) => {
        const name = TabbyUI.escapeHtml(rev.name);
        const size = rev.size_bytes != null ? TabbyUI.formatBytes(rev.size_bytes) : "size unknown";
        const files = rev.files ? `${rev.files} files` : "";
        const disabled = data.gguf_only ? "disabled" : "";
        const vramBadge = rev.vram_badge
          ? `<div class="models-sub"><span class="models-badge is-warn">${TabbyUI.escapeHtml(rev.vram_badge)}</span></div>`
          : "";
        return `<tr>
          <td><code>${name}</code>${files ? `<div class="muted models-sub">${files}</div>` : ""}</td>
          <td class="num">${TabbyUI.escapeHtml(size)}${vramBadge}</td>
          <td class="models-actions">
            <button type="button" class="btn primary" data-hf="${id}" data-rev="${name}" data-size="${rev.size_bytes || ""}" ${disabled}>Download</button>
          </td>
        </tr>`;
      })
      .join("");
    slot.innerHTML = `
      <div class="models-repo-head">
        <div class="models-repo-title">
          <strong>${id}</strong>
          <span class="models-hit-badges">${headBadges.join("")}</span>
        </div>
        <button type="button" class="btn" data-repo-close>Close</button>
      </div>
      ${note}
      ${vramNote}
      <label class="models-alias-field">
        <span>Short name</span>
        <input id="models-hf-alias" type="text" maxlength="32" placeholder="qwen38" autocomplete="off" spellcheck="false" value="${TabbyUI.escapeHtml(hfAlias)}" />
      </label>
      <p class="muted models-alias-hint">Used for <code>switch to qwen38</code> and the model dropdown. Letters, digits, and hyphens.</p>
      <table class="models-table models-repo-table">
        <thead><tr><th>Revision</th><th class="num">Size</th><th></th></tr></thead>
        <tbody>${revRows || '<tr><td colspan="3" class="muted">No branches listed.</td></tr>'}</tbody>
      </table>
    `;
  }

  async function loadLibrary() {
    const data = await TabbyUI.api("models");
    paintLibrary(data);
    paintJob(data.job);
    if (data.job && data.job.status === "error" && shouldPaintJob(data.job)) {
      showError(data.job.error || data.job.message);
    }
    lastJobId = (data.job && data.job.id) || lastJobId;
    refreshStatus();
    return data;
  }

  async function refreshStatus() {
    try {
      const data = await TabbyUI.api("status");
      if (typeof TabbyUI.paintGpuChip === "function") TabbyUI.paintGpuChip(data);
    } catch (_exc) {
      /* header poll will catch up */
    }
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
        if (job.status === "error") showError(job.error || job.message || "Download failed");
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

  function findHit(repoId) {
    const id = String(repoId || "");
    return Array.from(resultsEl.querySelectorAll(".models-hit[data-repo]")).find(
      (el) => el.getAttribute("data-repo") === id
    ) || null;
  }

  async function openRepo(repoId, hit) {
    showError("");
    const target = hit && hit.isConnected ? hit : findHit(repoId);
    if (!target) return;
    if (target.classList.contains("is-open") && hitRepoSlot(target) && hitRepoSlot(target).querySelector(".models-repo-head")) {
      closeRepo(target);
      return;
    }
    resultsEl.querySelectorAll(".models-hit.is-open").forEach((el) => {
      if (el !== target) closeRepo(el);
    });
    const slot = hitRepoSlot(target);
    if (!slot) return;
    const seq = ++repoSeq;
    setHitOpen(target, true);
    slot.hidden = false;
    slot.innerHTML = `<p class="muted">Loading files…</p>`;
    const data = await TabbyUI.api(`models/repo?id=${encodeURIComponent(repoId)}`);
    if (seq !== repoSeq || !target.isConnected) return;
    paintRepo(data, target);
  }

  libBody.addEventListener("click", async (event) => {
    const del = event.target.closest("[data-del]");
    const load = event.target.closest("[data-load]");
    const catalog = event.target.closest("button[data-catalog]");
    const aliasBtn = event.target.closest("[data-alias]");
    try {
      if (aliasBtn) {
        const current = aliasBtn.getAttribute("data-alias") || "";
        const folder = aliasBtn.getAttribute("data-folder") || "";
        const next = await TabbyUI.promptModal({
          title: "Short name",
          text: "Used for switch to …, the model dropdown, and list models.",
          label: "Short name",
          value: current,
          placeholder: "qwen38",
          yes: "Save",
        });
        if (next == null) return;
        const alias = String(next || "").trim();
        if (!alias || alias === current) return;
        await TabbyUI.api("models/alias", {
          method: "POST",
          body: { folder, profile: current, alias },
        });
        await loadLibrary();
        showOk(`Named ${alias}. Send switch to ${alias} to load it.`);
        return;
      }
      if (load) {
        load.disabled = true;
        showOk("Loading…");
        await TabbyUI.api("gpu", { method: "POST", body: { mode: load.getAttribute("data-load") } });
        await loadLibrary();
        showOk("Loaded.");
        return;
      }
      if (catalog) {
        await beginDownload({ kind: "catalog", pick_id: catalog.getAttribute("data-catalog") });
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

  resultsEl.addEventListener("click", async (event) => {
    if (event.target.closest("[data-repo-close]")) {
      closeRepo(event.target.closest(".models-hit"));
      return;
    }
    const download = event.target.closest("[data-hf]");
    if (download) {
      const size = download.getAttribute("data-size");
      rememberAliasFrom(resultsEl);
      try {
        await beginDownload({
          kind: "hf",
          repo_id: download.getAttribute("data-hf"),
          revision: download.getAttribute("data-rev"),
          size_bytes: size ? Number(size) : null,
          alias: hfAlias || null,
        });
      } catch (exc) {
        showError(exc.message || String(exc));
      }
      return;
    }
    const toggle = event.target.closest(".models-hit-toggle");
    if (!toggle) return;
    const hit = toggle.closest(".models-hit");
    try {
      await openRepo(toggle.getAttribute("data-repo"), hit);
    } catch (exc) {
      closeRepo(hit);
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
      if (exact && data.results[0].id) {
        await openRepo(data.results[0].id, findHit(data.results[0].id));
      }
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
      if (jobCancel.dataset.action === "dismiss") {
        dismissPaintedJob();
        showOk("");
        return;
      }
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
      clearHideTimer();
    },
  };
}

window.mountModels = mountModels;
