function mountLogs(root) {
  root.innerHTML = `
    <div class="toolbar">
      <button class="btn" id="log-pause">Pause</button>
      <button class="btn" id="log-clear">Clear</button>
      <button class="btn" id="log-latest">Jump to latest</button>
      <input class="search" id="log-filter" placeholder="Filter logs" />
      <span class="spacer"></span>
      <span class="muted" id="log-state">connecting…</span>
    </div>
    <pre class="log-view" id="log-view"></pre>
  `;
  const view = root.querySelector("#log-view");
  const state = root.querySelector("#log-state");
  const filter = root.querySelector("#log-filter");
  const MAX_LINES = 1200;
  const TRIM_TO = 900;

  let paused = false;
  let active = true;
  let stick = true;
  let buffer = [];
  let source = null;
  let pending = [];
  let raf = 0;
  let filterQ = "";
  let downTimer = 0;

  function levelClass(line) {
    if (/\b(ERROR|CRITICAL)\b/i.test(line)) return "lvl-error";
    if (/\bWARN(ING)?\b/i.test(line)) return "lvl-warning";
    if (/\bDEBUG\b/i.test(line)) return "lvl-debug";
    return "lvl-info";
  }

  function makeLineNode(line) {
    const span = document.createElement("span");
    span.className = levelClass(line);
    span.textContent = line;
    return span;
  }

  function trimBuffer() {
    if (buffer.length <= MAX_LINES) return 0;
    const drop = buffer.length - TRIM_TO;
    buffer.splice(0, drop);
    return drop;
  }

  function trimView(drop) {
    if (!drop || filterQ) return;
    // Each line is a <span> plus a newline text node.
    let remove = drop * 2;
    while (remove > 0 && view.firstChild) {
      view.removeChild(view.firstChild);
      remove -= 1;
    }
  }

  function renderFull() {
    const frag = document.createDocumentFragment();
    const lines = filterQ
      ? buffer.filter((line) => line.toLowerCase().includes(filterQ))
      : buffer;
    for (const line of lines) {
      frag.appendChild(makeLineNode(line));
      frag.appendChild(document.createTextNode("\n"));
    }
    view.replaceChildren(frag);
    if (stick) view.scrollTop = view.scrollHeight;
  }

  function appendVisible(lines) {
    if (!lines.length) return;
    if (filterQ) {
      const matched = lines.filter((line) => line.toLowerCase().includes(filterQ));
      if (!matched.length) return;
      const frag = document.createDocumentFragment();
      for (const line of matched) {
        frag.appendChild(makeLineNode(line));
        frag.appendChild(document.createTextNode("\n"));
      }
      view.appendChild(frag);
    } else {
      const frag = document.createDocumentFragment();
      for (const line of lines) {
        frag.appendChild(makeLineNode(line));
        frag.appendChild(document.createTextNode("\n"));
      }
      view.appendChild(frag);
    }
    if (stick) view.scrollTop = view.scrollHeight;
  }

  function flush() {
    raf = 0;
    if (!pending.length) return;
    const batch = pending;
    pending = [];
    buffer.push(...batch);
    const drop = trimBuffer();
    if (!active || paused) {
      return;
    }
    if (filterQ) {
      if (drop) renderFull();
      else appendVisible(batch);
      return;
    }
    trimView(drop);
    appendVisible(batch);
  }

  let catchUpBusy = false;
  let retryTimer = 0;
  let watchTimer = 0;
  let lastLineAt = Date.now();
  const seenCatchUp = new Set();

  function isUiAccess(line) {
    return (
      /"[A-Z]+ (?:\/\w+)?(?:\/v1)?\/ui(?:[/?\s]|$)/.test(line)
      || /"GET (?:\/\w+)?(?:\/v1)?\/model(?:[\s?"]|$)/.test(line)
    );
  }

  function rememberLine() {
    lastLineAt = Date.now();
  }

  function lastQueued() {
    if (pending.length) return pending[pending.length - 1];
    if (buffer.length) return buffer[buffer.length - 1];
    return null;
  }

  function extrasFromHistory(lines) {
    const last = lastQueued();
    if (last == null) return lines.slice();
    const idx = lines.lastIndexOf(last);
    if (idx >= 0) return lines.slice(idx + 1);
    const have = new Set([...buffer.slice(-400), ...pending]);
    return lines.filter((line) => !have.has(line));
  }

  function queue(line) {
    if (!line && line !== "") return;
    if (isUiAccess(line)) return;
    if (seenCatchUp.delete(line)) return;
    pending.push(line);
    rememberLine();
    if (!raf) raf = requestAnimationFrame(flush);
  }

  function stopWatch() {
    if (watchTimer) {
      clearInterval(watchTimer);
      watchTimer = 0;
    }
  }

  function startWatch() {
    if (watchTimer) return;
    watchTimer = setInterval(() => {
      if (!active || paused || document.hidden) return;
      if (Date.now() - lastLineAt < 5000) return;
      catchUp();
    }, 5000);
  }

  function scheduleReconnect() {
    if (retryTimer || !active || paused || document.hidden) return;
    retryTimer = setTimeout(() => {
      retryTimer = 0;
      connect();
    }, 800);
  }

  function disconnect() {
    stopWatch();
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = 0;
    }
    if (downTimer) {
      clearTimeout(downTimer);
      downTimer = 0;
    }
    if (source) {
      source.close();
      source = null;
    }
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    if (pending.length) {
      buffer.push(...pending);
      pending = [];
      trimBuffer();
    }
  }

  async function catchUp() {
    if (catchUpBusy) return;
    catchUpBusy = true;
    try {
      const data = await TabbyUI.api("logs/history?lines=300");
      const lines = (Array.isArray(data.lines) ? data.lines : [])
        .filter((line) => !isUiAccess(line))
        .slice(-MAX_LINES);
      if (!buffer.length && !pending.length) {
        buffer = lines;
        if (active && !paused) renderFull();
        return;
      }
      const extra = extrasFromHistory(lines);
      if (!extra.length) return;
      extra.forEach((line) => seenCatchUp.add(line));
      if (seenCatchUp.size > 800) {
        const keep = extra.slice(-400);
        seenCatchUp.clear();
        keep.forEach((line) => seenCatchUp.add(line));
      }
      pending.push(...extra);
      rememberLine();
      if (!raf) raf = requestAnimationFrame(flush);
    } catch (err) {
      if (!buffer.length) state.textContent = "waiting for API…";
      TabbyUI.paintApiDown(err);
    } finally {
      catchUpBusy = false;
    }
  }

  function connect() {
    if (source || !active) return;
    startWatch();
    state.textContent = "connecting…";
    source = new EventSource(TabbyUI.path("logs/stream"));
    source.addEventListener("log", (event) => {
      try {
        queue(JSON.parse(event.data).line || event.data);
      } catch {
        queue(event.data);
      }
    });
    source.onopen = () => {
      if (downTimer) {
        clearTimeout(downTimer);
        downTimer = 0;
      }
      state.textContent = paused ? "paused" : "live";
      catchUp();
      TabbyUI.api("status").then((data) => TabbyUI.paintGpuChip(data)).catch((err) => TabbyUI.paintApiDown(err));
    };
    source.onerror = () => {
      const reconnecting = active && !paused && !document.hidden;
      state.textContent = reconnecting ? "reconnecting…" : "idle";
      if (source && source.readyState === EventSource.CLOSED) {
        source.close();
        source = null;
        if (reconnecting) scheduleReconnect();
      }
      if (!reconnecting || downTimer) return;
      downTimer = setTimeout(() => {
        downTimer = 0;
        TabbyUI.paintApiDown();
      }, 400);
    };
  }

  function visibleLogText() {
    return filterQ
      ? buffer.filter((line) => line.toLowerCase().includes(filterQ)).join("\n")
      : buffer.join("\n");
  }

  function lineFromEvent(event) {
    const node = event.target.closest("span");
    if (node && view.contains(node)) return node.textContent || "";
    return TabbyUI.selectedText();
  }

  function setPaused(next) {
    paused = Boolean(next);
    const btn = root.querySelector("#log-pause");
    if (btn) btn.textContent = paused ? "Resume" : "Pause";
    if (paused) {
      disconnect();
      state.textContent = "paused";
    } else {
      connect();
      renderFull();
    }
  }

  function clearLogs() {
    buffer = [];
    pending = [];
    seenCatchUp.clear();
    view.replaceChildren();
  }

  view.addEventListener("contextmenu", (event) => {
    const line = lineFromEvent(event);
    const picked = TabbyUI.selectionIn(view) || TabbyUI.selectedText();
    TabbyUI.showContextMenu(event, [
      picked ? { label: "Copy selection", run: () => TabbyUI.copyText(picked) } : null,
      line ? { label: "Copy line", run: () => TabbyUI.copyText(line) } : null,
      { label: "Copy all", disabled: !buffer.length, run: () => TabbyUI.copyText(visibleLogText()) },
      { sep: true },
      { label: paused ? "Resume" : "Pause", run: () => setPaused(!paused) },
      { label: "Jump to latest", run: () => {
        stick = true;
        view.scrollTop = view.scrollHeight;
      } },
      { label: "Clear", danger: true, run: () => clearLogs() },
    ]);
  });

  view.addEventListener("scroll", () => {
    stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
  }, { passive: true });

  let filterTimer = 0;
  filter.addEventListener("contextmenu", (event) => {
    TabbyUI.showContextMenu(event, TabbyUI.inputMenuItems(filter, [
      { label: "Clear filter", disabled: !filter.value, run: () => {
        filter.value = "";
        filter.dispatchEvent(new Event("input", { bubbles: true }));
        filter.focus();
      } },
    ]));
  });
  filter.addEventListener("input", () => {
    filterQ = (filter.value || "").toLowerCase();
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      filterTimer = 0;
      renderFull();
    }, 120);
  });

  root.querySelector("#log-pause").addEventListener("click", () => {
    setPaused(!paused);
  });
  root.querySelector("#log-clear").addEventListener("click", () => {
    clearLogs();
  });
  root.querySelector("#log-latest").addEventListener("click", () => {
    stick = true;
    view.scrollTop = view.scrollHeight;
  });

  catchUp().then(() => {
    if (active && !paused) connect();
  });

  function onVisibility() {
    if (document.hidden) {
      if (source) {
        disconnect();
        state.textContent = "idle";
      }
    } else if (active && !paused) {
      renderFull();
      catchUp();
      connect();
    }
  }
  document.addEventListener("visibilitychange", onVisibility);

  return {
    pause() {
      active = false;
      disconnect();
      state.textContent = "idle";
    },
    resume() {
      active = true;
      if (!paused && !document.hidden) {
        renderFull();
        catchUp();
        connect();
      }
    },
    destroy() {
      active = false;
      disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      if (filterTimer) clearTimeout(filterTimer);
    },
  };
}

window.mountLogs = mountLogs;
