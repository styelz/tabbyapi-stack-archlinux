(() => {
  /* ---- Fuzzy scoring shared by file palette and command palette ---- */
  function score(item, query) {
    const q = String(query || "").trim().toLowerCase();
    const label = String(item.label || item.path || "").toLowerCase();
    const hint = String(item.hint || item.path || "").toLowerCase();
    if (!q) return 1;
    if (label === q || hint === q) return 100;
    if (label.startsWith(q) || hint.startsWith(q)) return 80;
    const name = label.split("/").pop() || "";
    if (name.startsWith(q)) return 70;
    if (label.includes(q) || hint.includes(q)) return 40;
    const parts = q.split(/\s+/).filter(Boolean);
    if (parts.length && parts.every((part) => label.includes(part) || hint.includes(part))) {
      return 20;
    }
    return 0;
  }

  /* ---- Original file palette (open/close/callbacks) ---- */
  function closePalette() {
    const wrap = document.querySelector(".tabby-palette");
    if (wrap) wrap.remove();
  }

  function openPalette(opts) {
    closePalette();
    closeCommandPalette();
    const items = Array.isArray(opts && opts.items) ? opts.items.slice() : [];
    const wrap = document.createElement("div");
    wrap.className = "tabby-palette";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.innerHTML =
      '<div class="tabby-palette-card">' +
      `<p class="tabby-palette-title">${window.TabbyUI ? TabbyUI.escapeHtml((opts && opts.title) || "Jump to file") : "Jump to file"}</p>` +
      '<input type="search" class="tabby-palette-input" autocomplete="off" spellcheck="false" />' +
      '<ul class="tabby-palette-list" role="listbox"></ul>' +
      "</div>";
    const input = wrap.querySelector(".tabby-palette-input");
    const list = wrap.querySelector(".tabby-palette-list");
    input.placeholder = (opts && opts.placeholder) || "Filter…";
    let index = 0;
    let shown = [];

    function paint() {
      const query = input.value;
      shown = items
        .map((item) => ({ item, score: score(item, query) }))
        .filter((row) => row.score > 0)
        .sort((a, b) => b.score - a.score || String(a.item.label).localeCompare(String(b.item.label)))
        .slice(0, 60)
        .map((row) => row.item);
      if (index >= shown.length) index = Math.max(0, shown.length - 1);
      list.replaceChildren();
      if (!shown.length) {
        const empty = document.createElement("li");
        empty.className = "is-empty";
        empty.textContent = "No matches";
        list.appendChild(empty);
        return;
      }
      shown.forEach((item, i) => {
        const li = document.createElement("li");
        li.className = i === index ? "is-active" : "";
        li.innerHTML =
          `<span>${window.TabbyUI ? TabbyUI.escapeHtml(item.label || item.path || "") : item.label}</span>` +
          (item.hint
            ? `<kbd>${window.TabbyUI ? TabbyUI.escapeHtml(item.hint) : item.hint}</kbd>`
            : "");
        li.addEventListener("mousedown", (event) => {
          event.preventDefault();
          pick(item);
        });
        list.appendChild(li);
      });
    }

    function pick(item) {
      finish();
      if (item && typeof (opts && opts.onPick) === "function") opts.onPick(item);
    }

    function finish() {
      document.removeEventListener("keydown", onKey, true);
      wrap.remove();
    }

    function onKey(event) {
      if (!document.body.contains(wrap)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        finish();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        index = shown.length ? (index + 1) % shown.length : 0;
        paint();
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        index = shown.length ? (index - 1 + shown.length) % shown.length : 0;
        paint();
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        if (shown[index]) pick(shown[index]);
      }
    }

    wrap.addEventListener("click", (event) => {
      if (event.target === wrap) finish();
    });
    input.addEventListener("input", () => {
      index = 0;
      paint();
    });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(wrap);
    paint();
    input.focus();
    if (opts && opts.query) {
      input.value = String(opts.query);
      paint();
    }
    return { close: finish, input };
  }

  /* ==================================================================
     Command Palette — Ctrl+Shift+P / Cmd+Shift+P
     ================================================================== */

  const registry = new Map();

  function register(id, def) {
    if (!id || !def || typeof def.run !== "function") return;
    registry.set(id, {
      id: id,
      label: String(def.label || id),
      group: String(def.group || ""),
      icon: def.icon || "",
      kbd: def.kbd || "",
      run: def.run,
    });
  }

  function unregister(id) {
    registry.delete(id);
  }

  function allCommands() {
    return Array.from(registry.values());
  }

  /* ---- Fuzzy match for commands (label + group) ---- */
  function cmdFuzzyMatch(text, query) {
    const t = text.toLowerCase();
    const q = query.toLowerCase();
    const indices = [];
    let ti = 0;
    for (let qi = 0; qi < q.length; qi++) {
      const ch = q[qi];
      while (ti < t.length && t[ti] !== ch) ti++;
      if (ti >= t.length) return null;
      indices.push(ti);
      ti++;
    }
    return indices;
  }

  function cmdScore(cmd, query) {
    if (!query) return { score: 1, indices: [] };
    const combined = cmd.group ? cmd.group + " " + cmd.label : cmd.label;
    const indices = cmdFuzzyMatch(combined, query);
    if (!indices) return { score: 0, indices: [] };
    let s = 10;
    const lower = combined.toLowerCase();
    const q = query.toLowerCase();
    if (lower === q) s = 100;
    else if (lower.startsWith(q)) s = 80;
    else if (cmd.label.toLowerCase().startsWith(q)) s = 70;
    else if (lower.includes(q)) s = 50;
    else s = 20 - indices.length * 0.1;
    return { score: s, indices: indices };
  }

  function highlightChars(text, indices, offset) {
    const esc = window.TabbyUI ? TabbyUI.escapeHtml : (s) => s;
    if (!indices || !indices.length) return esc(text);
    const set = new Set(indices.map((i) => i - (offset || 0)));
    let out = "";
    for (let i = 0; i < text.length; i++) {
      const ch = esc(text[i]);
      if (set.has(i)) out += '<span class="cmd-palette-match">' + ch + "</span>";
      else out += ch;
    }
    return out;
  }

  /* ---- Command palette DOM ---- */
  let cmdWrap = null;

  function closeCommandPalette() {
    if (cmdWrap) {
      cmdWrap.remove();
      cmdWrap = null;
    }
  }

  function openCommandPalette() {
    closeCommandPalette();
    closePalette();
    const cmds = allCommands();
    if (!cmds.length) return;

    cmdWrap = document.createElement("div");
    cmdWrap.className = "cmd-palette cmd-palette-backdrop";
    cmdWrap.setAttribute("role", "dialog");
    cmdWrap.setAttribute("aria-modal", "true");

    const card = document.createElement("div");
    card.className = "cmd-palette-card";

    const input = document.createElement("input");
    input.type = "search";
    input.className = "cmd-palette-input";
    input.placeholder = "Type a command…";
    input.autocomplete = "off";
    input.spellcheck = false;

    const list = document.createElement("ul");
    list.className = "cmd-palette-list";
    list.setAttribute("role", "listbox");

    card.append(input, list);
    cmdWrap.appendChild(card);

    let index = 0;
    let shown = [];

    function paint() {
      const query = input.value.trim();
      const scored = cmds
        .map((cmd) => {
          const result = cmdScore(cmd, query);
          return { cmd, score: result.score, indices: result.indices };
        })
        .filter((row) => row.score > 0)
        .sort((a, b) => {
          if (b.score !== a.score) return b.score - a.score;
          if (a.cmd.group !== b.cmd.group) return a.cmd.group.localeCompare(b.cmd.group);
          return a.cmd.label.localeCompare(b.cmd.label);
        })
        .slice(0, 80);

      shown = scored;
      if (index >= shown.length) index = Math.max(0, shown.length - 1);
      list.replaceChildren();

      if (!shown.length) {
        const empty = document.createElement("li");
        empty.className = "cmd-palette-item";
        empty.style.color = "var(--muted)";
        empty.style.cursor = "default";
        empty.textContent = "No matching commands";
        list.appendChild(empty);
        return;
      }

      let lastGroup = null;
      shown.forEach((row, i) => {
        if (row.cmd.group && row.cmd.group !== lastGroup) {
          lastGroup = row.cmd.group;
          const groupEl = document.createElement("li");
          groupEl.className = "cmd-palette-group";
          groupEl.textContent = row.cmd.group;
          list.appendChild(groupEl);
        }

        const li = document.createElement("li");
        li.className = "cmd-palette-item" + (i === index ? " is-active" : "");
        li.setAttribute("role", "option");
        li.dataset.index = String(i);

        const labelSpan = document.createElement("span");
        labelSpan.className = "cmd-palette-label";
        const groupOffset = row.cmd.group ? row.cmd.group.length + 1 : 0;
        if (row.cmd.icon) {
          labelSpan.innerHTML = row.cmd.icon + " " + highlightChars(row.cmd.label, row.indices, groupOffset);
        } else {
          labelSpan.innerHTML = highlightChars(row.cmd.label, row.indices, groupOffset);
        }
        li.appendChild(labelSpan);

        if (row.cmd.kbd) {
          const kbd = document.createElement("kbd");
          kbd.className = "cmd-palette-kbd";
          kbd.textContent = row.cmd.kbd;
          li.appendChild(kbd);
        }

        li.addEventListener("mousedown", (event) => {
          event.preventDefault();
          pick(row);
        });
        li.addEventListener("mouseenter", () => {
          index = i;
          paintActive();
        });
        list.appendChild(li);
      });
      scrollActive();
    }

    function paintActive() {
      list.querySelectorAll(".cmd-palette-item").forEach((el) => {
        const idx = Number(el.dataset.index);
        el.classList.toggle("is-active", idx === index);
      });
      scrollActive();
    }

    function scrollActive() {
      const active = list.querySelector(".cmd-palette-item.is-active");
      if (active) active.scrollIntoView({ block: "nearest" });
    }

    function pick(row) {
      finish();
      if (row && row.cmd && typeof row.cmd.run === "function") {
        try { row.cmd.run(); } catch (e) { /* command error */ }
      }
    }

    function finish() {
      document.removeEventListener("keydown", onKey, true);
      closeCommandPalette();
    }

    function onKey(event) {
      if (!cmdWrap || !document.body.contains(cmdWrap)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        finish();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        index = shown.length ? (index + 1) % shown.length : 0;
        paintActive();
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        index = shown.length ? (index - 1 + shown.length) % shown.length : 0;
        paintActive();
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        if (shown[index]) pick(shown[index]);
      }
    }

    cmdWrap.addEventListener("click", (event) => {
      if (event.target === cmdWrap) finish();
    });
    input.addEventListener("input", () => {
      index = 0;
      paint();
    });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(cmdWrap);
    paint();
    input.focus();
  }

  /* ---- Global keyboard shortcut: Ctrl+Shift+P / Cmd+Shift+P ---- */
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "p") {
      event.preventDefault();
      event.stopPropagation();
      if (cmdWrap) closeCommandPalette();
      else openCommandPalette();
    }
  }, true);

  /* ---- Public API ---- */
  window.TabbyPalette = {
    open: openPalette,
    close: closePalette,
    register: register,
    unregister: unregister,
    allCommands: allCommands,
    openCommands: openCommandPalette,
    closeCommands: closeCommandPalette,
  };
})();
