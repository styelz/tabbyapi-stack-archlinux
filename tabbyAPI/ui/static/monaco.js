(() => {
  const VERSION = "0.52.2";

  let loadPromise = null;
  let onChange = null;
  let onSave = null;
  let ignoreChange = false;
  let currentPath = "";
  let mountGen = 0;
  const hosts = {
    main: { editor: null, diffEditor: null, originalModel: null, modifiedModel: null, path: "" },
    split: { editor: null, diffEditor: null, originalModel: null, modifiedModel: null, path: "" },
  };

  function hostOf(name) {
    return hosts[name] || hosts.main;
  }

  function localVs() {
    const rel = window.TabbyUI ? window.TabbyUI.path("assets/vs") : "/v1/ui/assets/vs";
    try {
      return new URL(rel, window.location.href).href.replace(/\/+$/, "");
    } catch {
      return rel;
    }
  }

  function installWorkers(vs) {
    const base = String(vs || "").replace(/\/+$/, "");
    // Worker AMD ids are "vs/language/typescript/tsWorker". baseUrl must be
    // the parent of /vs, or the worker 404s on .../vs/vs/language/...
    const root = /\/vs$/i.test(base) ? base.slice(0, -3) : base;
    const workerMain = `${base}/base/worker/workerMain.js`;
    const body =
      `self.MonacoEnvironment={baseUrl:${JSON.stringify(`${root}/`)}};` +
      `importScripts(${JSON.stringify(workerMain)});`;
    const workerUrl = URL.createObjectURL(new Blob([body], { type: "text/javascript" }));
    window.MonacoEnvironment = {
      getWorkerUrl() {
        return workerUrl;
      },
    };
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const node = document.createElement("script");
      node.src = src;
      node.onload = resolve;
      node.onerror = reject;
      document.head.appendChild(node);
    });
  }

  function loadCss(href) {
    if (document.querySelector(`link[data-monaco="${href}"]`)) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.dataset.monaco = href;
    document.head.appendChild(link);
  }

  function hexByte(n) {
    return Math.max(0, Math.min(255, Math.round(Number(n) || 0)))
      .toString(16)
      .padStart(2, "0");
  }

  function channelByte(value) {
    const raw = String(value || "").trim();
    if (raw.endsWith("%")) return (parseFloat(raw) / 100) * 255;
    return Number(raw);
  }

  function alphaByte(value) {
    const raw = String(value || "").trim();
    if (!raw) return 255;
    if (raw.endsWith("%")) return (parseFloat(raw) / 100) * 255;
    const n = Number(raw);
    return n <= 1 ? n * 255 : n;
  }

  // Monaco token colors must be #hex. getComputedStyle returns rgb()/rgba().
  function toMonacoHex(value, fallback) {
    const raw = String(value || "").trim();
    if (!raw) return fallback;
    if (/^#[0-9a-f]{3,8}$/i.test(raw)) {
      if (raw.length === 4) {
        return `#${raw[1]}${raw[1]}${raw[2]}${raw[2]}${raw[3]}${raw[3]}`.toLowerCase();
      }
      if (raw.length === 5) {
        return `#${raw[1]}${raw[1]}${raw[2]}${raw[2]}${raw[3]}${raw[3]}${raw[4]}${raw[4]}`.toLowerCase();
      }
      return raw.toLowerCase();
    }
    const rgb = raw.match(
      /^rgba?\(\s*([\d.]+%?)\s*[, ]\s*([\d.]+%?)\s*[, ]\s*([\d.]+%?)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/i
    );
    if (rgb) {
      const r = hexByte(channelByte(rgb[1]));
      const g = hexByte(channelByte(rgb[2]));
      const b = hexByte(channelByte(rgb[3]));
      if (rgb[4] === undefined) return `#${r}${g}${b}`;
      const a = hexByte(alphaByte(rgb[4]));
      return a === "ff" ? `#${r}${g}${b}` : `#${r}${g}${b}${a}`;
    }
    return fallback;
  }

  function cssColor(name, fallback) {
    const probe = document.createElement("span");
    probe.style.backgroundColor = `var(${name})`;
    document.documentElement.appendChild(probe);
    const resolved = getComputedStyle(probe).backgroundColor;
    probe.remove();
    if (resolved && resolved !== "transparent" && resolved !== "rgba(0, 0, 0, 0)") {
      return toMonacoHex(resolved, fallback);
    }
    const value =
      window.TabbyUI && TabbyUI.cssVar
        ? TabbyUI.cssVar(name)
        : getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return toMonacoHex(value, fallback);
  }

  function applyMonacoTheme() {
    if (!window.monaco) return;
    const id = document.documentElement.getAttribute("data-theme") || "midnight-dark";
    const light = /-light$/.test(id);
    window.monaco.editor.defineTheme("tabby", {
      base: light ? "vs" : "vs-dark",
      inherit: true,
      rules: [],
      colors: {
        "editor.background": cssColor("--bg-elev", "#12151c"),
        "editor.foreground": cssColor("--text", "#e8ecf4"),
        "editorLineNumber.foreground": cssColor("--editor-muted", "#5a6379"),
        "editorGutter.background": cssColor("--editor-gutter", "#0e1218"),
        "editor.lineHighlightBackground": cssColor("--editor-line", "#1a2030"),
        "diffEditor.insertedTextBackground": cssColor("--diff-add-text", "#3dd68c73"),
        "diffEditor.removedTextBackground": cssColor("--diff-del-text", "#ff6b7a73"),
        "diffEditor.insertedLineBackground": cssColor("--diff-add-line", "#3dd68c4d"),
        "diffEditor.removedLineBackground": cssColor("--diff-del-line", "#ff6b7a4d"),
        "diffEditorGutter.insertedLineBackground": cssColor("--diff-add-line", "#3dd68c4d"),
        "diffEditorGutter.removedLineBackground": cssColor("--diff-del-line", "#ff6b7a4d"),
        "diffEditorOverview.insertedForeground": cssColor("--diff-add-gutter", "#3dd68cbf"),
        "diffEditorOverview.removedForeground": cssColor("--diff-del-gutter", "#ff6b7abf"),
        "editorGutter.addedBackground": cssColor("--diff-add-gutter", "#3dd68cbf"),
        "editorGutter.deletedBackground": cssColor("--diff-del-gutter", "#ff6b7abf"),
        "minimapGutter.addedBackground": cssColor("--diff-add-gutter", "#3dd68cbf"),
        "minimapGutter.deletedBackground": cssColor("--diff-del-gutter", "#ff6b7abf"),
        "scrollbar.shadow": "#00000000",
        "scrollbarSlider.background": cssColor("--scroll-thumb", "#7aa2ff66"),
        "scrollbarSlider.hoverBackground": cssColor("--scroll-thumb-hover", "#7aa2ffad"),
        "scrollbarSlider.activeBackground": cssColor("--scroll-thumb-active", "#7aa2ffe0"),
        "minimapSlider.background": cssColor("--scroll-thumb", "#7aa2ff66"),
        "minimapSlider.hoverBackground": cssColor("--scroll-thumb-hover", "#7aa2ffad"),
        "minimapSlider.activeBackground": cssColor("--scroll-thumb-active", "#7aa2ffe0"),
      },
    });
    window.monaco.editor.setTheme("tabby");
  }

  async function ensure() {
    if (window.monaco) return window.monaco;
    if (loadPromise) return loadPromise;
    loadPromise = (async () => {
      try {
        let vs = localVs();
        try {
          await loadScript(`${vs}/loader.js`);
        } catch (err) {
          throw new Error("Code editor failed to load from this host.");
        }
        loadCss(`${vs}/editor/editor.main.css`);
        installWorkers(vs);
        window.require.config({ paths: { vs } });
        await new Promise((resolve, reject) => {
          try {
            window.require(["vs/editor/editor.main"], resolve);
          } catch (err) {
            reject(err);
          }
        });
        applyMonacoTheme();
        return window.monaco;
      } catch (err) {
        loadPromise = null;
        throw err;
      }
    })();
    return loadPromise;
  }

  function languageFor(path) {
    const ext = String(path || "").split(".").pop().toLowerCase();
    const map = {
      html: "html",
      htm: "html",
      css: "css",
      js: "javascript",
      mjs: "javascript",
      jsx: "javascript",
      ts: "typescript",
      tsx: "typescript",
      json: "json",
      md: "markdown",
      py: "python",
      sh: "shell",
      xml: "xml",
      svg: "xml",
      yml: "yaml",
      yaml: "yaml",
      php: "php",
      txt: "plaintext",
      csv: "plaintext",
      toml: "ini",
      ini: "ini",
      conf: "ini",
    };
    return map[ext] || "plaintext";
  }

  function modelUri(path) {
    const clean = String(path || "untitled").replace(/^\/+/, "");
    return window.monaco.Uri.parse(`inmemory://tabby/${clean}`);
  }

  function disposeHostModels(host) {
    if (host.originalModel) {
      host.originalModel.dispose();
      host.originalModel = null;
    }
    if (host.modifiedModel && host.diffEditor) {
      host.modifiedModel.dispose();
      host.modifiedModel = null;
    }
  }

  function disposeHost(name) {
    const host = hostOf(name);
    if (host.editor) {
      host.editor.dispose();
      host.editor = null;
    }
    if (host.diffEditor) {
      host.diffEditor.dispose();
      host.diffEditor = null;
    }
    disposeHostModels(host);
    host.path = "";
  }

  function disposeEditors() {
    disposeHost("main");
    disposeHost("split");
  }

  function getOrCreateModel(path, text) {
    const monaco = window.monaco;
    const uri = modelUri(path);
    const existing = monaco.editor.getModel(uri);
    if (existing) {
      if (text != null && existing.getValue() !== String(text || "")) {
        ignoreChange = true;
        existing.setValue(String(text || ""));
        ignoreChange = false;
      }
      return existing;
    }
    return monaco.editor.createModel(String(text || ""), languageFor(path), uri);
  }

  function pathForModel(model) {
    if (!model || !model.uri) return "";
    return String(model.uri.path || "").replace(/^\/+/, "");
  }

  function zoomFactor() {
    if (window.TabbyUI && typeof TabbyUI.getZoom === "function") {
      return TabbyUI.getZoom() / 100;
    }
    const raw = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--ui-zoom")
    );
    return Number.isFinite(raw) && raw > 0 ? raw : 1;
  }

  function editorFontSize() {
    return Math.max(8, Math.round(13 * zoomFactor()));
  }

  function editorPadding() {
    return { top: Math.max(4, Math.round(8 * zoomFactor())) };
  }

  function applyEditorZoom() {
    const opts = { fontSize: editorFontSize(), padding: editorPadding() };
    Object.keys(hosts).forEach((name) => {
      const host = hosts[name];
      if (host.editor) host.editor.updateOptions(opts);
      if (host.diffEditor) {
        host.diffEditor.updateOptions(opts);
        const modified = host.diffEditor.getModifiedEditor && host.diffEditor.getModifiedEditor();
        const original = host.diffEditor.getOriginalEditor && host.diffEditor.getOriginalEditor();
        if (modified) modified.updateOptions(opts);
        if (original) original.updateOptions(opts);
      }
    });
  }

  const common = {
    theme: "tabby",
    automaticLayout: true,
    fontSize: 13,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    minimap: { enabled: true, maxColumn: 80, scale: 1, showSlider: "mouseover" },
    scrollBeyondLastLine: false,
    tabSize: 2,
    insertSpaces: true,
    wordWrap: "off",
    renderWhitespace: "selection",
    smoothScrolling: true,
    cursorSmoothCaretAnimation: "on",
    stickyScroll: { enabled: true },
    scrollbar: {
      useShadows: false,
      verticalScrollbarSize: 10,
      horizontalScrollbarSize: 10,
    },
    padding: { top: 8 },
    mouseWheelZoom: true,
    contextmenu: true,
    folding: true,
    glyphMargin: true,
    lineNumbers: "on",
    renderLineHighlight: "all",
    bracketPairColorization: { enabled: true },
    guides: { bracketPairs: true, indentation: true },
    autoClosingBrackets: "languageDefined",
    formatOnPaste: true,
    links: true,
    find: { addExtraSpaceOnTop: false, seedSearchStringFromSelection: "always" },
  };

  window.addEventListener("tabby-zoom-change", applyEditorZoom);

  function bindSave(ed, hostName) {
    if (!ed || !window.monaco) return;
    ed.addCommand(window.monaco.KeyMod.CtrlCmd | window.monaco.KeyCode.KeyS, () => {
      if (typeof onSave !== "function") return;
      const name = hostName || focusedHost();
      onSave(name, hostOf(name).path);
    });
  }

  function getEditEditor(name) {
    const host = hostOf(name || focusedHost());
    if (host.editor) return host.editor;
    if (host.diffEditor) return host.diffEditor.getModifiedEditor();
    if (name) return null;
    const main = hosts.main;
    if (main.editor) return main.editor;
    if (main.diffEditor) return main.diffEditor.getModifiedEditor();
    return null;
  }

  function focusedHost() {
    const active = document.activeElement;
    if (hosts.split.editor && hosts.split.el && hosts.split.el.contains(active)) return "split";
    if (hosts.split.diffEditor && hosts.split.el && hosts.split.el.contains(active)) return "split";
    return "main";
  }

  async function showFile(el, opts) {
    if (!el) return;
    const name = (opts && opts.host) === "split" ? "split" : "main";
    const host = hostOf(name);
    host.gen = (host.gen || 0) + 1;
    const gen = host.gen;
    const monaco = await ensure();
    if (gen !== host.gen) return;
    const path = (opts && opts.path) || "";
    currentPath = path;
    disposeHost(name);
    el.innerHTML = "";
    host.el = el;
    host.path = path;
    const model = getOrCreateModel(path, opts && opts.text);
    host.editor = monaco.editor.create(el, {
      ...common,
      fontSize: editorFontSize(),
      padding: editorPadding(),
      model,
      readOnly: Boolean(opts && opts.readOnly),
    });
    host.editor.onDidChangeModelContent(() => {
      if (ignoreChange || typeof onChange !== "function") return;
      onChange(host.editor.getValue(), name, host.path);
    });
    host.editor.onDidFocusEditorText(() => {
      currentPath = host.path;
    });
    bindSave(host.editor, name);
    if (Array.isArray(opts && opts.caret) && model) {
      const pos = model.getPositionAt(Math.max(0, opts.caret[0] || 0));
      host.editor.setPosition(pos);
      host.editor.revealPositionInCenter(pos);
    }
    if (opts && opts.line) {
      const line = Math.max(1, Number(opts.line) || 1);
      const col = Math.max(1, Number(opts.column) || 1);
      host.editor.setPosition({ lineNumber: line, column: col });
      host.editor.revealLineInCenter(line);
    }
    host.editor.focus();
    if (window.TabbyLsp) window.TabbyLsp.attachMonaco(path, host.editor);
  }

  async function showDiff(el, opts) {
    if (!el) return;
    const name = "main";
    const host = hostOf(name);
    host.gen = (host.gen || 0) + 1;
    const gen = host.gen;
    const monaco = await ensure();
    if (gen !== host.gen) return;
    const path = (opts && opts.path) || "";
    currentPath = path;
    disposeHost(name);
    el.innerHTML = "";
    host.el = el;
    host.path = path;
    host.originalModel = monaco.editor.createModel(
      (opts && opts.original) || "",
      languageFor(path)
    );
    host.modifiedModel = getOrCreateModel(path, (opts && opts.modified) || "");
    host.diffEditor = monaco.editor.createDiffEditor(el, {
      ...common,
      fontSize: editorFontSize(),
      padding: editorPadding(),
      originalEditable: false,
      renderSideBySide: !window.matchMedia("(max-width: 1100px)").matches,
      useInlineViewWhenSpaceIsLimited: true,
      readOnly: false,
    });
    host.diffEditor.setModel({ original: host.originalModel, modified: host.modifiedModel });
    const modified = host.diffEditor.getModifiedEditor();
    modified.onDidChangeModelContent(() => {
      if (ignoreChange || typeof onChange !== "function") return;
      onChange(modified.getValue(), name, host.path);
    });
    bindSave(modified, name);
    modified.focus();
    if (window.TabbyLsp) window.TabbyLsp.attachMonaco(path, modified);
  }

  document.addEventListener("tabby-theme-change", applyMonacoTheme);

  window.TabbyMonaco = {
    ready: ensure,
    languageFor,
    showFile,
    showDiff,
    getValue(name) {
      const ed = getEditEditor(name);
      return ed ? ed.getValue() : "";
    },
    setValue(text, name) {
      const ed = getEditEditor(name);
      if (!ed) return;
      ignoreChange = true;
      ed.setValue(String(text || ""));
      ignoreChange = false;
    },
    getCaret(name) {
      const ed = getEditEditor(name);
      const model = ed && ed.getModel();
      const sel = ed && ed.getSelection();
      if (!model || !sel) return null;
      return [model.getOffsetAt(sel.getStartPosition()), model.getOffsetAt(sel.getEndPosition())];
    },
    layout() {
      Object.keys(hosts).forEach((name) => {
        const host = hosts[name];
        if (host.editor) host.editor.layout();
        if (host.diffEditor) host.diffEditor.layout();
      });
    },
    find() {
      const ed = getEditEditor();
      if (!ed) return;
      const action = ed.getAction("actions.find");
      if (action) action.run();
    },
    insertAtCursor(text) {
      const ed = getEditEditor();
      if (!ed) return false;
      const sel = ed.getSelection();
      if (!sel) return false;
      ed.executeEdits("tabby", [{ range: sel, text: String(text || ""), forceMoveMarkers: true }]);
      return true;
    },
    format() {
      const ed = getEditEditor();
      if (!ed) return;
      const action = ed.getAction("editor.action.formatDocument");
      if (action) action.run();
    },
    pathForModel,
    focusedHost,
    disposeHost,
    setMarkers(path, items) {
      if (!window.monaco) return;
      const model = window.monaco.editor.getModel(modelUri(path));
      if (!model) return;
      const markers = (items || []).map((item) => {
        const range = item.range || {};
        const start = range.start || {};
        const end = range.end || start;
        return {
          severity:
            Number(item.severity) === 1
              ? window.monaco.MarkerSeverity.Error
              : Number(item.severity) === 2
                ? window.monaco.MarkerSeverity.Warning
                : window.monaco.MarkerSeverity.Info,
          message: String(item.message || ""),
          startLineNumber: (start.line || 0) + 1,
          startColumn: (start.character || 0) + 1,
          endLineNumber: (end.line || start.line || 0) + 1,
          endColumn: (end.character || start.character || 0) + 1,
        };
      });
      window.monaco.editor.setModelMarkers(model, "tabby-lsp", markers);
    },
    onChange(fn) {
      onChange = fn;
    },
    onSave(fn) {
      onSave = fn;
    },
    dispose: disposeEditors,
    getEditor: getEditEditor,
    path() {
      return currentPath;
    },
  };
})();
