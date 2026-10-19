(function () {
  const pages = {
    logs: { el: document.getElementById("page-logs"), mount: window.mountLogs, title: "Logs" },
    chat: { el: document.getElementById("page-chat"), mount: window.mountChat, title: "Chat" },
    status: { el: document.getElementById("page-status"), mount: window.mountStatus, title: "Status" },
    models: { el: document.getElementById("page-models"), mount: window.mountModels, title: "Models" },
    gallery: { el: document.getElementById("page-gallery"), mount: window.mountGallery, title: "Gallery" },
    users: { el: document.getElementById("page-users"), mount: window.mountUsers, title: "Users" },
    settings: { el: document.getElementById("page-settings"), mount: window.mountSettings, title: "Settings" },
  };
  let isAdmin = false;
  const handles = {};

  function currentName() {
    const hash = (location.hash || "#chat").replace("#", "");
    if ((hash === "users" || hash === "settings" || hash === "logs" || hash === "models") && !isAdmin) return "chat";
    return pages[hash] ? hash : "chat";
  }

  let activePage = "";

  function show(name) {
    if (window.TabbyUI && TabbyUI.hideContextMenu) TabbyUI.hideContextMenu();
    closeUserMenu();
    closeGpuMenu();
    closeContextMenu();
    closeTasksPanel();
    const key = pages[name] ? name : "chat";
    const previous = activePage;
    activePage = key;
    if (window.TabbyStore) TabbyStore.set("page", { current: key, previous });
    Object.entries(pages).forEach(([id, page]) => {
      const on = id === key;
      page.el.hidden = !on;
      page.el.classList.toggle("is-active", on);
      if (on) {
        if (!handles[id]) handles[id] = page.mount(page.el);
        else if (typeof handles[id].resume === "function") handles[id].resume();
      } else if (handles[id] && typeof handles[id].pause === "function") {
        handles[id].pause();
      }
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("is-active", tab.dataset.page === key);
    });
    const title = document.getElementById("header-title");
    if (title) title.textContent = pages[key].title;
  }

  const gpuChip = document.getElementById("gpu-chip");
  const gpuMenu = document.getElementById("gpu-menu");
  const gpuPanel = document.getElementById("gpu-menu-panel");
  const contextChip = document.getElementById("context-chip");
  const contextMenu = document.getElementById("context-menu");
  const contextPanel = document.getElementById("context-menu-panel");
  let gpuMenuOpen = false;
  let contextMenuOpen = false;
  let gpuSwitchBusy = false;
  let gpuSwitchTarget = "";

  function gpuChipText() {
    const labelEl = document.getElementById("gpu-chip-label");
    return (labelEl && labelEl.textContent) || (gpuChip && gpuChip.textContent) || "";
  }

  function currentGpuMode(data) {
    if (!data || data.down) return "";
    if (!data.tabby_model && !data.llama_up && !data.comfy_up) return "";
    const mode = String(data.gpu_mode || "").toLowerCase();
    if (mode === "comfy") return "comfy";
    if (mode === "llama") return data.profile || "llama";
    if (data.comfy_up && !data.tabby_model && !data.llama_up) return "comfy";
    return data.profile || "";
  }

  function gpuMenuBusy(data) {
    if (gpuSwitchBusy) return true;
    if (!data || data.down) return true;
    return Boolean(data.switching || data.restarting);
  }

  function statusWithLocalSwitch(data) {
    if (!gpuSwitchBusy) return data;
    const queue = (data && data.stack_queue) || {};
    if (queue.queued || (queue.busy && !(data && data.switching))) {
      return Object.assign({}, data || {}, {
        gpu_waiting: true,
        switch_target: (data && data.switch_target) || gpuSwitchTarget,
      });
    }
    return Object.assign({}, data || {}, {
      switching: true,
      busy: true,
      switch_target: (data && data.switch_target) || gpuSwitchTarget,
    });
  }

  function makeGpuItem(label, mode, on, busy, hint, note) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "user-menu-item" + (on ? " is-on" : "");
    btn.setAttribute("role", "menuitem");
    btn.dataset.gpuMode = mode;
    btn.disabled = Boolean(busy);
    const copy = document.createElement("span");
    copy.className = "gpu-item-copy";
    const name = document.createElement("span");
    name.textContent = label;
    copy.append(name);
    if (note && note !== label) {
      const sub = document.createElement("span");
      sub.className = "gpu-item-note";
      sub.textContent = note;
      copy.append(sub);
    }
    const mark = document.createElement("kbd");
    mark.textContent = on ? "✓" : hint || "";
    if (!on && hint === "Download") mark.classList.add("is-download");
    btn.append(copy, mark);
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      if (on) {
        closeGpuMenu();
        return;
      }
      switchGpu(mode).catch(() => {});
    });
    return btn;
  }

  function fillGpuMenu(data) {
    if (!gpuPanel) return;
    gpuPanel.replaceChildren();
    const switchLocked = gpuMenuBusy(data);
    const occupied = Boolean(data && data.stack_queue && data.stack_queue.busy);
    const current = currentGpuMode(data);
    const profiles = (data && data.profiles) || [];
    const labels = (data && data.profile_labels) || {};
    if (profiles.length) {
      profiles.forEach((name) => {
        const ready = data && data.profile_ready && Object.prototype.hasOwnProperty.call(data.profile_ready, name)
          ? data.profile_ready[name]
          : true;
        const pretty = String(labels[name] || "").trim() || name;
        gpuPanel.appendChild(
          makeGpuItem(
            pretty,
            name,
            current === name,
            switchLocked,
            occupied && !switchLocked && current !== name ? "Wait" : (!ready && current !== name ? "Download" : ""),
            pretty !== name && !String(name).toLowerCase().startsWith("hf-") ? name : ""
          )
        );
      });
    } else {
      const empty = document.createElement("p");
      empty.className = "gpu-menu-empty";
      empty.textContent = data && data.down ? "API unavailable" : "No profiles yet";
      gpuPanel.appendChild(empty);
    }
    const sep = document.createElement("div");
    sep.className = "user-menu-sep";
    gpuPanel.appendChild(sep);
    gpuPanel.appendChild(
      makeGpuItem(
        "Comfy",
        "comfy",
        current === "comfy",
        switchLocked,
        occupied && !switchLocked && current !== "comfy" ? "Wait" : "Images"
      )
    );
    const copySep = document.createElement("div");
    copySep.className = "user-menu-sep";
    gpuPanel.appendChild(copySep);
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "user-menu-item";
    copyBtn.setAttribute("role", "menuitem");
    copyBtn.textContent = "Copy status";
    copyBtn.addEventListener("click", (event) => {
      event.preventDefault();
      TabbyUI.copyText(gpuChipText());
      closeGpuMenu();
    });
    gpuPanel.appendChild(copyBtn);
  }

  function closeGpuMenu() {
    gpuMenuOpen = false;
    if (gpuPanel) gpuPanel.hidden = true;
    if (gpuChip) gpuChip.setAttribute("aria-expanded", "false");
  }

  function closeContextMenu() {
    contextMenuOpen = false;
    if (contextPanel) contextPanel.hidden = true;
    if (contextChip) contextChip.setAttribute("aria-expanded", "false");
  }

  function openContextMenu() {
    if (!contextChip || contextChip.hidden) return;
    closeUserMenu();
    closeGpuMenu();
    closeTasksPanel();
    contextMenuOpen = true;
    if (contextPanel) contextPanel.hidden = false;
    contextChip.setAttribute("aria-expanded", "true");
  }

  async function openGpuMenu() {
    closeUserMenu();
    closeContextMenu();
    closeTasksPanel();
    gpuMenuOpen = true;
    if (gpuPanel) gpuPanel.hidden = false;
    if (gpuChip) gpuChip.setAttribute("aria-expanded", "true");
    fillGpuMenu(TabbyUI.lastGpuStatus);
    if (TabbyUI.lastGpuStatus && (TabbyUI.lastGpuStatus.profiles || []).length) return;
    try {
      TabbyUI.paintGpuChip(statusWithLocalSwitch(await TabbyUI.api("status")));
    } catch (err) {
      TabbyUI.paintApiDown(err);
    }
  }

  async function switchGpu(mode, opts) {
    const token = String(mode || "").trim().toLowerCase();
    const force = Boolean(opts && opts.force);
    if (!token || gpuSwitchBusy) return;
    const data = TabbyUI.lastGpuStatus || {};
    if (currentGpuMode(data) === token && !force) {
      closeGpuMenu();
      return;
    }
    if (gpuMenuBusy(data) && !gpuSwitchBusy) {
      closeGpuMenu();
      return;
    }
    const readyMap = data.profile_ready || {};
    if (token !== "comfy" && token !== "llm" && readyMap[token] === false) {
      closeGpuMenu();
      try {
        await TabbyUI.offerMissingModelDownload(token, data);
      } catch (err) {
        TabbyUI.paintApiDown(err);
      }
      return;
    }
    closeGpuMenu();
    gpuSwitchBusy = true;
    gpuSwitchTarget = token;
    const switchTaskId = "switch:" + token;
    if (window.TabbyTasks) TabbyTasks.add({ id: switchTaskId, type: "switch", label: "Switching to " + token, progress: null, status: "running", startedAt: Date.now() });
    const queue = data.stack_queue || {};
    const waiting = Boolean(queue.busy) && !data.switching && !data.restarting;
    TabbyUI.paintGpuChip(
      Object.assign({}, data, waiting
        ? { gpu_waiting: true, switch_target: token }
        : {
            switching: true,
            busy: true,
            switch_target: token,
            profile: token === "comfy" ? data.profile : token,
          })
    );
    kickHeaderStatus(400);
    try {
      await TabbyUI.api("gpu", { method: "POST", body: { mode: token } });
      await refreshHeaderStatus();
      if (window.TabbyTasks) TabbyTasks.update(switchTaskId, { status: "done", label: "Switched to " + token });
    } catch (err) {
      if (window.TabbyTasks) TabbyTasks.update(switchTaskId, { status: "failed", label: "Switch to " + token + " failed" });
      TabbyUI.paintApiDown(err);
      throw err;
    } finally {
      gpuSwitchBusy = false;
      gpuSwitchTarget = "";
    }
  }
  TabbyUI.switchGpu = switchGpu;

  if (gpuChip) {
    gpuChip.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (gpuMenuOpen) closeGpuMenu();
      else openGpuMenu();
    });
    gpuChip.addEventListener("contextmenu", (event) => {
      TabbyUI.showContextMenu(event, [
        { label: "Copy status", run: () => TabbyUI.copyText(gpuChipText()) },
      ]);
    });
  }
  window.addEventListener("tabby-gpu-status", () => {
    if (gpuMenuOpen) fillGpuMenu(TabbyUI.lastGpuStatus);
  });

  const userChip = document.getElementById("user-chip");
  const userMenu = document.getElementById("user-menu");
  const userPanel = document.getElementById("user-menu-panel");
  const themeBtn = document.getElementById("user-menu-theme");
  const modeBtn = document.getElementById("user-menu-mode");
  const themeFly = document.getElementById("user-menu-theme-flyout");
  const modeFly = document.getElementById("user-menu-mode-flyout");
  const themeHint = document.getElementById("user-menu-theme-hint");
  const modeHint = document.getElementById("user-menu-mode-hint");
  const zoomInput = document.getElementById("user-menu-zoom");
  const zoomHint = document.getElementById("user-menu-zoom-hint");
  const restartItem = document.getElementById("user-menu-restart");
  const settingsItem = document.getElementById("user-menu-settings");
  const settingsBtn = document.getElementById("settings-btn");
  let userMenuOpen = false;

  function closeFlyouts() {
    if (themeFly) themeFly.hidden = true;
    if (modeFly) modeFly.hidden = true;
    if (themeBtn) themeBtn.setAttribute("aria-expanded", "false");
    if (modeBtn) modeBtn.setAttribute("aria-expanded", "false");
  }

  function closeUserMenu() {
    userMenuOpen = false;
    if (userPanel) userPanel.hidden = true;
    if (userChip) userChip.setAttribute("aria-expanded", "false");
    closeFlyouts();
  }

  function paintUserMenuHints() {
    if (themeHint) themeHint.textContent = TabbyUI.THEME_LABELS[TabbyUI.getTheme()] || "Midnight";
    if (modeHint) modeHint.textContent = TabbyUI.MODE_LABELS[TabbyUI.getMode()] || "Dark";
    if (themeFly) {
      themeFly.querySelectorAll("[data-theme-family]").forEach((btn) => {
        btn.classList.toggle("is-on", btn.dataset.themeFamily === TabbyUI.getTheme());
      });
    }
    if (modeFly) {
      modeFly.querySelectorAll("[data-theme-mode]").forEach((btn) => {
        btn.classList.toggle("is-on", btn.dataset.themeMode === TabbyUI.getMode());
      });
    }
    const fsBtn = userPanel && userPanel.querySelector('[data-user-act="fullscreen"]');
    if (fsBtn) fsBtn.textContent = document.fullscreenElement ? "Exit full screen" : "Full screen";
    const pct = TabbyUI.getZoom();
    if (zoomInput) zoomInput.value = String(pct);
    if (zoomHint) zoomHint.textContent = `${pct}%`;
  }

  function fillThemeFlyout() {
    if (!themeFly) return;
    themeFly.replaceChildren();
    TabbyUI.THEME_FAMILIES.forEach((id) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "user-menu-item user-menu-theme";
      btn.dataset.themeFamily = id;
      const swatch = document.createElement("span");
      swatch.className = "theme-swatch";
      swatch.dataset.family = id;
      swatch.innerHTML = "<i></i><i></i>";
      const label = document.createElement("span");
      label.textContent = TabbyUI.THEME_LABELS[id] || id;
      const mark = document.createElement("kbd");
      mark.textContent = "✓";
      btn.append(swatch, label, mark);
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        TabbyUI.setTheme(id);
        paintUserMenuHints();
      });
      themeFly.appendChild(btn);
    });
  }

  function fillModeFlyout() {
    if (!modeFly) return;
    modeFly.replaceChildren();
    TabbyUI.THEME_MODES.forEach((id) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "user-menu-item";
      btn.dataset.themeMode = id;
      const label = document.createElement("span");
      label.textContent = TabbyUI.MODE_LABELS[id] || id;
      const mark = document.createElement("kbd");
      mark.textContent = "✓";
      btn.append(label, mark);
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        TabbyUI.setMode(id);
        paintUserMenuHints();
      });
      modeFly.appendChild(btn);
    });
  }

  function openUserMenu() {
    closeGpuMenu();
    closeContextMenu();
    closeTasksPanel();
    userMenuOpen = true;
    if (userPanel) userPanel.hidden = false;
    if (userChip) userChip.setAttribute("aria-expanded", "true");
    paintUserMenuHints();
  }

  fillThemeFlyout();
  fillModeFlyout();
  paintUserMenuHints();
  document.addEventListener("tabby-theme-change", paintUserMenuHints);
  document.addEventListener("fullscreenchange", paintUserMenuHints);

  if (settingsBtn) {
    settingsBtn.addEventListener("click", (event) => {
      event.preventDefault();
      closeUserMenu();
      closeGpuMenu();
      closeContextMenu();
      location.hash = "#settings";
    });
  }

  if (userChip) {
    userChip.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (userMenuOpen) closeUserMenu();
      else openUserMenu();
    });
    userChip.addEventListener("contextmenu", (event) => {
      const nameEl = document.getElementById("user-chip-name");
      const name = (nameEl && nameEl.textContent) || "";
      if (!name) return;
      TabbyUI.showContextMenu(event, [
        { label: "Copy username", run: () => TabbyUI.copyText(name) },
      ]);
    });
  }

  if (themeBtn && themeFly) {
    themeBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const open = themeFly.hidden;
      closeFlyouts();
      themeFly.hidden = !open;
      themeBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  if (modeBtn && modeFly) {
    modeBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const open = modeFly.hidden;
      closeFlyouts();
      modeFly.hidden = !open;
      modeBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  if (zoomInput) {
    zoomInput.addEventListener("input", () => {
      TabbyUI.setZoom(zoomInput.value);
      paintUserMenuHints();
    });
  }

  async function logout() {
    if (TabbyUI.flushPrefs) await TabbyUI.flushPrefs();
    try {
      const response = await fetch(TabbyUI.path("auth/logout"), {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Logout failed (${response.status})`);
      TabbyUI.redirectToLogin();
    } catch (err) {
      closeUserMenu();
      TabbyUI.confirmModal({
        title: "Log out failed",
        text: (err && err.message) || "Could not log out. Try again.",
        yes: "OK",
        no: "Close",
      });
    }
  }

  function backupFilename(response) {
    const header = (response && response.headers && response.headers.get("content-disposition")) || "";
    const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
    if (star) {
      try {
        const name = decodeURIComponent(star[1].trim().replace(/^["']|["']$/g, ""));
        if (name) return name.toLowerCase().endsWith(".zip") ? name : `${name}.zip`;
      } catch {
        /* ignore */
      }
    }
    const quoted = /filename="([^"]+)"/i.exec(header);
    if (quoted && quoted[1]) {
      const name = quoted[1];
      return name.toLowerCase().endsWith(".zip") ? name : `${name}.zip`;
    }
    return "tabby-backup.zip";
  }

  function saveBackupZip(blob, filename) {
    const type = (blob && blob.type) || "";
    const zip = type.includes("zip") ? blob : new Blob([blob], { type: "application/zip" });
    const url = URL.createObjectURL(zip);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename || "tabby-backup.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }

  async function readZipWithProgress(response, onBytes) {
    const reader = response.body && response.body.getReader && response.body.getReader();
    if (!reader) return response.arrayBuffer();
    const chunks = [];
    let received = 0;
    const total = Number(response.headers.get("content-length")) || 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.byteLength;
      if (typeof onBytes === "function") onBytes(received, total);
    }
    const out = new Uint8Array(received);
    let offset = 0;
    for (const chunk of chunks) {
      out.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return out.buffer;
  }

  function applyProgressEvent(modal, event) {
    if (!event) return;
    if (event.line) modal.appendLine(event.line);
    if (event.error) throw new Error(event.error);
  }

  async function followBackupProgress(response, modal) {
    const done = await TabbyUI.readNdjson(response, (event) => applyProgressEvent(modal, event));
    if (done && done.error) throw new Error(done.error);
    if (!done || !done.ok || !done.token) throw new Error("Backup did not finish.");
    return done;
  }

  async function downloadBackup() {
    closeUserMenu();
    const yes = await TabbyUI.confirmModal({
      title: "Download backup?",
      text: "Downloads a zip of this account's chats, Code files, prefs, and gallery images. Other accounts and model weights are not included. Keep this tab open until the download finishes.",
      yes: "Download",
      no: "Cancel",
    });
    if (!yes) return;
    const modal = TabbyUI.progressModal({
      title: "Preparing backup",
      note: "Building a zip of this account's chats, Code files, prefs, and gallery. Log output appears below.",
    });
    modal.appendLine("Starting backup…");
    try {
      const response = await fetch(TabbyUI.path("backup.zip"), {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/x-ndjson, application/zip, application/octet-stream" },
      });
      if (response.status === 401) {
        TabbyUI.redirectToLogin();
        throw new Error("Not authenticated");
      }
      const type = (response.headers.get("content-type") || "").toLowerCase();
      if (type.includes("ndjson")) {
        if (!response.ok) {
          const failed = await TabbyUI.readNdjson(response, (event) => applyProgressEvent(modal, event)).catch(() => null);
          throw new Error((failed && failed.error) || TabbyUI.httpErrorMessage(response, failed) || "Backup failed");
        }
        const done = await followBackupProgress(response, modal);
        const filename = done.filename || "tabby-backup.zip";
        modal.appendLine(`Downloading ${filename} (${TabbyUI.formatBytes(done.bytes || 0)})…`);
        const zipRes = await fetch(
          TabbyUI.path("backup.zip?token=" + encodeURIComponent(done.token || "")),
          {
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/zip, application/octet-stream" },
          }
        );
        if (zipRes.status === 401) {
          TabbyUI.redirectToLogin();
          throw new Error("Not authenticated");
        }
        if (!zipRes.ok) {
          throw new Error(TabbyUI.httpErrorMessage(zipRes) || "Could not download the backup zip.");
        }
        let lastPaint = 0;
        const buffer = await readZipWithProgress(zipRes, (received, total) => {
          const now = Date.now();
          if (now - lastPaint < 200 && total && received < total) return;
          lastPaint = now;
          modal.setNote(
            total
              ? `Downloading zip… ${TabbyUI.formatBytes(received)} / ${TabbyUI.formatBytes(total)}`
              : `Downloading zip… ${TabbyUI.formatBytes(received)}`
          );
        });
        const bytes = new Uint8Array(buffer);
        if (!(bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b)) {
          throw new Error("Backup was not a zip file.");
        }
        saveBackupZip(new Blob([buffer], { type: "application/zip" }), backupFilename(zipRes) || filename);
        modal.appendLine(`Saved ${filename} (${TabbyUI.formatBytes(bytes.length)})`);
        modal.close();
        return;
      }
      const buffer = await response.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      const isZip = bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b;
      if (isZip) {
        saveBackupZip(new Blob([buffer], { type: "application/zip" }), backupFilename(response));
        modal.close();
        return;
      }
      let data = "";
      try {
        data = JSON.parse(new TextDecoder().decode(buffer));
      } catch {
        data = new TextDecoder().decode(buffer).slice(0, 240);
      }
      if (!response.ok || !bytes.length) {
        throw new Error(TabbyUI.httpErrorMessage(response, data) || "Backup file is empty");
      }
      throw new Error("Backup was not a zip file.");
    } catch (err) {
      modal.setBusy(false);
      modal.setTitle("Backup failed");
      modal.setNote((err && err.message) || "Could not download the backup zip.");
      modal.appendLine((err && err.message) || "Could not download the backup zip.");
      modal.setActions([{ label: "Close", primary: true, run: () => modal.close() }]);
    }
  }

  async function restoreBackupFile(file) {
    if (!file) return;
    const yes = await TabbyUI.confirmModal({
      title: "Restore this backup?",
      text: "This replaces this account's chats, Code files, prefs, and gallery images. Other accounts are not changed. If the zip was made by another user, it is imported into this account.",
      yes: "Restore",
      no: "Cancel",
    });
    if (!yes) return;
    if (TabbyUI.suspendPersistence) TabbyUI.suspendPersistence();
    const modal = TabbyUI.progressModal({
      title: "Restoring backup",
      note: "Uploading and restoring this account's data. Log output appears below.",
    });
    modal.appendLine(`Uploading ${file.name || "backup.zip"} (${TabbyUI.formatBytes(file.size || 0)})…`);
    try {
      const response = await fetch(TabbyUI.path("backup/restore"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/zip",
          Accept: "application/x-ndjson, application/json",
        },
        body: file,
      });
      if (response.status === 401) {
        TabbyUI.redirectToLogin();
        throw new Error("Not authenticated");
      }
      const type = (response.headers.get("content-type") || "").toLowerCase();
      let data = null;
      if (type.includes("ndjson")) {
        if (!response.ok) {
          const failed = await TabbyUI.readNdjson(response, (event) => applyProgressEvent(modal, event)).catch(() => null);
          throw new Error((failed && failed.error) || TabbyUI.httpErrorMessage(response, failed) || "Restore failed");
        }
        data = await TabbyUI.readNdjson(response, (event) => applyProgressEvent(modal, event));
        if (data && data.error) throw new Error(data.error);
      } else {
        data = type.includes("application/json") ? await response.json() : await response.text();
        if (typeof data === "string" && data) modal.appendLine(data.slice(0, 400));
      }
      if (!response.ok) {
        throw new Error(TabbyUI.httpErrorMessage(response, data));
      }
      if (!data || data.ok === false) {
        throw new Error((data && data.error) || "Could not restore the backup.");
      }
      const chats = Number(data.chats || 0);
      const gallery = Number(data.gallery || 0);
      modal.appendLine(
        `Restored ${chats} chat${chats === 1 ? "" : "s"} and ${gallery} gallery image${gallery === 1 ? "" : "s"}.`
      );
      modal.setBusy(false);
      modal.setTitle("Backup restored");
      modal.setNote("Reloading this account's chats, Code files, and gallery.");
      modal.setActions([]);
      location.reload();
    } catch (err) {
      modal.setBusy(false);
      modal.setTitle("Restore failed");
      modal.setNote((err && err.message) || "Could not restore the backup.");
      modal.appendLine((err && err.message) || "Could not restore the backup.");
      modal.setActions([{ label: "Close", primary: true, run: () => modal.close() }]);
    }
  }

  function pickBackupFile() {
    closeUserMenu();
    const input = document.getElementById("user-backup-file");
    if (!input) return;
    input.value = "";
    input.click();
  }

  async function restartApi() {
    closeUserMenu();
    const yes = await TabbyUI.confirmModal({
      title: "Restart API?",
      text: "Restart TabbyAPI now? The UI will drop for about a minute.",
      yes: "Restart",
      no: "Cancel",
    });
    if (!yes) return;
    const restartTaskId = "restart:" + Date.now();
    if (window.TabbyTasks) TabbyTasks.add({ id: restartTaskId, type: "restart", label: "Restarting API", progress: null, status: "running", startedAt: Date.now() });
    const modal = TabbyUI.progressModal({
      title: "Restarting",
      note: "Restarting TabbyAPI. The UI will drop for about a minute.",
    });
    try {
      await TabbyUI.followRestart(modal);
      if (window.TabbyTasks) TabbyTasks.update(restartTaskId, { status: "done", label: "API restarted" });
      modal.setActions([
        { label: "Close", run: () => modal.close() },
        { label: "Reload UI", primary: true, run: () => location.reload() },
      ]);
    } catch (err) {
      if (window.TabbyTasks) TabbyTasks.update(restartTaskId, { status: "failed", label: "API restart failed" });
      modal.setBusy(false);
      modal.setTitle("Restart");
      modal.setNote((err && err.message) || "Restart failed.");
      modal.setActions([{ label: "Close", primary: true, run: () => modal.close() }]);
    }
  }

  if (userPanel) {
    userPanel.addEventListener("click", (event) => {
      const act = event.target.closest("[data-user-act]");
      if (!act) return;
      event.preventDefault();
      const name = act.dataset.userAct;
      if (name === "keys") {
        closeUserMenu();
        TabbyUI.showShortcuts();
      } else if (name === "copy") {
        const nameEl = document.getElementById("user-chip-name");
        TabbyUI.copyText((nameEl && nameEl.textContent) || "");
        closeUserMenu();
      } else if (name === "fullscreen") {
        if (document.fullscreenElement) document.exitFullscreen();
        else document.documentElement.requestFullscreen().catch(() => {});
      } else if (name === "settings") {
        closeUserMenu();
        location.hash = "#settings";
      } else if (name === "backup") {
        downloadBackup();
      } else if (name === "restore") {
        pickBackupFile();
      } else if (name === "restart") {
        restartApi();
      } else if (name === "logout") {
        closeUserMenu();
        logout();
      }
    });
  }

  const backupFile = document.getElementById("user-backup-file");
  if (backupFile) {
    backupFile.addEventListener("change", () => {
      const file = backupFile.files && backupFile.files[0];
      backupFile.value = "";
      restoreBackupFile(file);
    });
  }

  if (contextChip) {
    contextChip.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (contextMenuOpen) closeContextMenu();
      else openContextMenu();
    });
    contextChip.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      if (contextMenuOpen) closeContextMenu();
      else openContextMenu();
    });
  }
  const contextHandoff = document.getElementById("context-handoff");
  if (contextHandoff) {
    contextHandoff.addEventListener("click", (event) => {
      event.preventDefault();
      closeContextMenu();
      if (typeof window.tabbyContinueInNewChat === "function") window.tabbyContinueInNewChat();
    });
  }

  document.addEventListener("pointerdown", (event) => {
    if (userMenuOpen && userMenu && !userMenu.contains(event.target)) closeUserMenu();
    if (gpuMenuOpen && gpuMenu && !gpuMenu.contains(event.target)) closeGpuMenu();
    if (contextMenuOpen && contextMenu && !contextMenu.contains(event.target)) closeContextMenu();
    if (tasksOpen && tasksMenu && !tasksMenu.contains(event.target)) closeTasksPanel();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (gpuMenuOpen) closeGpuMenu();
    if (userMenuOpen) closeUserMenu();
    if (contextMenuOpen) closeContextMenu();
    if (tasksOpen) closeTasksPanel();
  });

  TabbyUI.api("auth/check")
    .then((data) => {
      const nameEl = document.getElementById("user-chip-name");
      const name = data.username || data.stack_user || "";
      if (nameEl) nameEl.textContent = name;
      const chip = document.getElementById("user-chip");
      if (chip && name) chip.setAttribute("aria-label", name);
      isAdmin = Boolean(data.is_admin);
      TabbyUI.isAdmin = isAdmin;
      if (window.TabbyStore) TabbyStore.set("user", { name, isAdmin });
      const logsTab = document.getElementById("tab-logs");
      const usersTab = document.getElementById("tab-users");
      const settingsTab = document.getElementById("tab-settings");
      const modelsTab = document.getElementById("tab-models");
      if (logsTab) logsTab.hidden = !isAdmin;
      if (usersTab) usersTab.hidden = !isAdmin;
      if (settingsTab) settingsTab.hidden = !isAdmin;
      if (modelsTab) modelsTab.hidden = !isAdmin;
      if (settingsBtn) settingsBtn.hidden = !isAdmin;
      if (settingsItem) settingsItem.hidden = !isAdmin;
      if (restartItem) restartItem.hidden = !isAdmin;
      const hash = (location.hash || "").replace("#", "");
      if (!isAdmin && (hash === "users" || hash === "settings" || hash === "logs" || hash === "models")) {
        location.hash = "#chat";
      }
      show(currentName());
    })
    .catch(() => {});

  let headerTimer = 0;
  let headerFailing = false;

  function kickHeaderStatus(delay) {
    if (headerTimer) clearTimeout(headerTimer);
    headerTimer = setTimeout(refreshHeaderStatus, delay);
  }

  async function refreshHeaderStatus() {
    try {
      const data = await TabbyUI.api("status");
      headerFailing = false;
      TabbyUI.paintGpuChip(statusWithLocalSwitch(data));
    } catch (err) {
      headerFailing = true;
      TabbyUI.paintApiDown(err);
    } finally {
      const data = TabbyUI.lastGpuStatus;
      const switching = gpuSwitchBusy || (data && (data.switching || data.restarting || data.busy));
      const occupied = Boolean(data && data.stack_queue && (data.stack_queue.busy || data.stack_queue.queued));
      if (window.TabbyStore && data) {
        TabbyStore.patch("gpu", { switching: Boolean(switching) });
      }
      kickHeaderStatus(headerFailing ? 5000 : switching || occupied ? 5000 : 15000);
    }
  }
  refreshHeaderStatus();

  window.addEventListener("hashchange", () => {
    const hash = (location.hash || "").replace("#", "");
    if (!isAdmin && (hash === "users" || hash === "settings" || hash === "logs")) {
      location.hash = "#chat";
      return;
    }
    show(currentName());
  });
  show(currentName());
  if (typeof TABBY_REVEAL_UI === "function") TABBY_REVEAL_UI();
  performance.mark("tabby-page-ready");

  /* ==== Background tasks panel ==== */
  const tasksMenu = document.getElementById("tasks-menu");
  const tasksBtn = document.getElementById("tasks-btn");
  const tasksBadge = document.getElementById("tasks-badge");
  const tasksPanel = document.getElementById("tasks-panel");
  const tasksList = document.getElementById("tasks-list");
  let tasksOpen = false;
  let taskSeq = 0;
  const taskDismissTimers = {};

  function taskId(type, extra) {
    return type + ":" + (extra || "default");
  }

  function getTasks() {
    return (window.TabbyStore && TabbyStore.get("tasks")) || [];
  }

  function setTasks(tasks) {
    if (window.TabbyStore) TabbyStore.set("tasks", tasks);
    paintTasks();
  }

  function addTask(task) {
    const tasks = getTasks().filter((t) => t.id !== task.id);
    task.startedAt = task.startedAt || Date.now();
    tasks.push(task);
    setTasks(tasks);
    return task;
  }

  function updateTask(id, patch) {
    const tasks = getTasks();
    const task = tasks.find((t) => t.id === id);
    if (!task) return;
    Object.assign(task, patch);
    setTasks(tasks);
    if (patch.status === "done") scheduleAutoDismiss(id);
    else if (patch.status === "failed") clearAutoDismiss(id);
  }

  function removeTask(id) {
    clearAutoDismiss(id);
    setTasks(getTasks().filter((t) => t.id !== id));
  }

  function scheduleAutoDismiss(id) {
    clearAutoDismiss(id);
    taskDismissTimers[id] = setTimeout(() => {
      const el = tasksList && tasksList.querySelector(`[data-task-id="${CSS.escape(id)}"]`);
      if (el) {
        el.classList.add("is-fading");
        setTimeout(() => removeTask(id), 400);
      } else {
        removeTask(id);
      }
    }, 5000);
  }

  function clearAutoDismiss(id) {
    if (taskDismissTimers[id]) {
      clearTimeout(taskDismissTimers[id]);
      delete taskDismissTimers[id];
    }
  }

  function paintTasks() {
    const tasks = getTasks();
    const active = tasks.filter((t) => t.status === "running");
    const count = active.length;
    if (tasksBtn) tasksBtn.hidden = tasks.length === 0;
    if (tasksBadge) {
      tasksBadge.hidden = count === 0;
      tasksBadge.textContent = String(count);
    }
    if (!tasksList) return;
    if (!tasks.length) {
      tasksList.innerHTML = '<div class="tasks-empty">No background tasks</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    tasks.forEach((task) => {
      const row = document.createElement("div");
      row.className = "tasks-item" + (task.status === "done" ? " is-done" : "") + (task.status === "failed" ? " is-failed" : "");
      row.setAttribute("data-task-id", task.id);

      if (task.status === "running") {
        const spinner = document.createElement("span");
        spinner.className = "tasks-spinner";
        row.appendChild(spinner);
      } else {
        const icon = document.createElement("span");
        icon.className = "tasks-icon" + (task.status === "done" ? " is-done" : " is-failed");
        icon.textContent = task.status === "done" ? "✓" : "✗";
        row.appendChild(icon);
      }

      const label = document.createElement("span");
      label.className = "tasks-item-label";
      label.textContent = task.label || task.type;
      row.appendChild(label);

      if (task.progress != null && task.status === "running") {
        const pct = document.createElement("span");
        pct.className = "tasks-item-pct";
        pct.textContent = `${Math.round(task.progress)}%`;
        row.appendChild(pct);
        const bar = document.createElement("div");
        bar.className = "tasks-item-progress";
        const fill = document.createElement("div");
        fill.className = "tasks-item-fill";
        fill.style.width = `${Math.max(0, Math.min(100, task.progress))}%`;
        bar.appendChild(fill);
        row.appendChild(bar);
      }

      if (task.status === "failed" || task.status === "done") {
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "tasks-item-dismiss";
        dismiss.textContent = "×";
        dismiss.title = "Dismiss";
        dismiss.addEventListener("click", (ev) => {
          ev.stopPropagation();
          removeTask(task.id);
        });
        row.appendChild(dismiss);
      }
      frag.appendChild(row);
    });
    tasksList.replaceChildren(frag);
  }

  function closeTasksPanel() {
    tasksOpen = false;
    if (tasksPanel) tasksPanel.hidden = true;
    if (tasksBtn) tasksBtn.setAttribute("aria-expanded", "false");
  }

  function openTasksPanel() {
    closeUserMenu();
    closeGpuMenu();
    closeContextMenu();
    tasksOpen = true;
    if (tasksPanel) tasksPanel.hidden = false;
    if (tasksBtn) tasksBtn.setAttribute("aria-expanded", "true");
    paintTasks();
  }

  if (tasksBtn) {
    tasksBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (tasksOpen) closeTasksPanel();
      else openTasksPanel();
    });
  }

  document.addEventListener("pointerdown", (event) => {
    if (tasksOpen && tasksMenu && !tasksMenu.contains(event.target)) closeTasksPanel();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && tasksOpen) closeTasksPanel();
  });

  window.TabbyTasks = Object.freeze({
    add: addTask,
    update: updateTask,
    remove: removeTask,
    id: taskId,
    get: getTasks,
  });

  paintTasks();

  /* ==== Command palette registrations ==== */
  if (window.TabbyPalette && typeof TabbyPalette.register === "function") {
    const reg = TabbyPalette.register;
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
    const mod = isMac ? "⌘" : "Ctrl+";

    function clickEl(id) {
      const el = document.getElementById(id);
      if (el) el.click();
    }

    // -- Navigation --
    reg("nav.chat",     { label: "Go to Chat",     group: "Navigation", run: () => { location.hash = "#chat"; } });
    reg("nav.status",   { label: "Go to Status",   group: "Navigation", run: () => { location.hash = "#status"; } });
    reg("nav.models",   { label: "Go to Models",   group: "Navigation", run: () => { location.hash = "#models"; } });
    reg("nav.gallery",  { label: "Go to Gallery",  group: "Navigation", run: () => { location.hash = "#gallery"; } });
    reg("nav.logs",     { label: "Go to Logs",     group: "Navigation", run: () => { location.hash = "#logs"; } });
    reg("nav.settings", { label: "Go to Settings", group: "Navigation", run: () => { location.hash = "#settings"; } });

    // -- Chat --
    reg("chat.new", {
      label: "New chat", group: "Chat", kbd: mod + "N",
      run: () => { location.hash = "#chat"; clickEl("chat-new"); },
    });
    reg("chat.search", {
      label: "Search chats", group: "Chat", kbd: mod + "K",
      run: () => {
        location.hash = "#chat";
        const el = document.getElementById("chat-search");
        if (el) { el.focus(); el.select(); }
      },
    });
    reg("chat.find", {
      label: "Find in chat", group: "Chat", kbd: mod + "F",
      run: () => {
        location.hash = "#chat";
        const el = document.getElementById("chat-find-input");
        const bar = document.getElementById("chat-find");
        if (bar) bar.hidden = false;
        if (el) { el.focus(); el.select(); }
      },
    });
    reg("chat.export", {
      label: "Export markdown", group: "Chat",
      run: () => {
        location.hash = "#chat";
        const btn = document.querySelector('[data-more="export"]');
        if (btn) btn.click();
      },
    });
    reg("chat.copy", {
      label: "Copy entire chat", group: "Chat",
      run: () => {
        location.hash = "#chat";
        const btn = document.querySelector('[data-more="copy"]');
        if (btn) btn.click();
      },
    });
    reg("chat.regen", {
      label: "Regenerate last reply", group: "Chat",
      run: () => {
        location.hash = "#chat";
        const btn = document.querySelector('[data-more="regen"]');
        if (btn) btn.click();
      },
    });
    reg("chat.handoff", {
      label: "Continue in new chat", group: "Chat",
      run: () => {
        if (typeof window.tabbyContinueInNewChat === "function") window.tabbyContinueInNewChat();
      },
    });

    // -- Code --
    reg("code.jump", {
      label: "Jump to file", group: "Code", kbd: mod + "P",
      run: () => {
        location.hash = "#chat";
        setTimeout(() => {
          const el = document.querySelector('[data-more="jump"]');
          if (el) { el.click(); return; }
          if (window.TabbyPalette) TabbyPalette.open({ title: "Jump to file", placeholder: "File name…", items: [] });
        }, 50);
      },
    });
    reg("code.findInFiles", {
      label: "Find in files", group: "Code", kbd: mod + "Shift+F",
      run: () => {
        location.hash = "#chat";
        setTimeout(() => {
          const bar = document.getElementById("project-find");
          const inp = document.getElementById("project-find-input");
          if (bar) bar.hidden = false;
          if (inp) { inp.focus(); inp.select(); }
        }, 50);
      },
    });
    reg("code.save", {
      label: "Save file", group: "Code", kbd: mod + "S",
      run: () => {
        document.dispatchEvent(new KeyboardEvent("keydown", {
          key: "s", code: "KeyS", ctrlKey: !isMac, metaKey: isMac, bubbles: true,
        }));
      },
    });
    reg("code.terminal", {
      label: "Toggle terminal", group: "Code", kbd: mod + "`",
      run: () => {
        location.hash = "#chat";
        setTimeout(() => clickEl("chat-term-toggle") || clickEl("files-term-btn"), 50);
      },
    });
    reg("code.newTerminal", {
      label: "New terminal", group: "Code",
      run: () => {
        location.hash = "#chat";
        setTimeout(() => {
          const btn = document.querySelector('[data-term-action="add"]');
          if (btn) btn.click();
        }, 50);
      },
    });
    reg("code.preview", {
      label: "Toggle preview", group: "Code",
      run: () => {
        location.hash = "#chat";
        setTimeout(() => clickEl("files-preview-btn"), 50);
      },
    });
    reg("code.split", {
      label: "Split editor", group: "Code",
      run: () => {
        location.hash = "#chat";
        setTimeout(() => {
          const btn = document.querySelector('[data-editor-action="split"]');
          if (btn) btn.click();
        }, 50);
      },
    });

    // -- GPU --
    const gpuProfiles = (TabbyUI.lastGpuStatus && TabbyUI.lastGpuStatus.profiles) || [];
    const gpuLabels = (TabbyUI.lastGpuStatus && TabbyUI.lastGpuStatus.profile_labels) || {};
    function regGpuSwitch(name, label) {
      reg("gpu." + name, {
        label: "Switch to " + label, group: "GPU",
        run: () => { if (TabbyUI.switchGpu) TabbyUI.switchGpu(name); },
      });
    }
    regGpuSwitch("qwen", "qwen");
    regGpuSwitch("gemma", "gemma");
    regGpuSwitch("comfy", "comfy");
    reg("gpu.restart", {
      label: "Restart API", group: "GPU",
      run: () => {
        const btn = document.querySelector('[data-user-act="restart"]');
        if (btn) btn.click();
        else restartApi && restartApi();
      },
    });

    // -- Theme --
    (TabbyUI.THEME_FAMILIES || []).forEach((family) => {
      (TabbyUI.THEME_MODES || []).forEach((mode) => {
        const familyLabel = (TabbyUI.THEME_LABELS || {})[family] || family;
        const modeLabel = (TabbyUI.MODE_LABELS || {})[mode] || mode;
        reg("theme." + family + "." + mode, {
          label: familyLabel + " — " + modeLabel, group: "Theme",
          run: () => {
            TabbyUI.setTheme(family);
            TabbyUI.setMode(mode);
          },
        });
      });
    });

    // -- Account --
    reg("account.keys", {
      label: "Keyboard shortcuts", group: "Account", kbd: "?",
      run: () => { if (TabbyUI.showShortcuts) TabbyUI.showShortcuts(); },
    });
    reg("account.backup", {
      label: "Download backup", group: "Account",
      run: () => {
        const btn = document.querySelector('[data-user-act="backup"]');
        if (btn) btn.click();
      },
    });
    reg("account.restore", {
      label: "Restore backup", group: "Account",
      run: () => {
        const btn = document.querySelector('[data-user-act="restore"]');
        if (btn) btn.click();
      },
    });
    reg("account.fullscreen", {
      label: "Full screen", group: "Account", kbd: "F11",
      run: () => {
        if (document.fullscreenElement) document.exitFullscreen();
        else document.documentElement.requestFullscreen().catch(() => {});
      },
    });
    reg("account.logout", {
      label: "Log out", group: "Account",
      run: () => {
        const btn = document.querySelector('[data-user-act="logout"]');
        if (btn) btn.click();
      },
    });
  }
})();
