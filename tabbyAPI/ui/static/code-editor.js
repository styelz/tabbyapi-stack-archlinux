/* code-editor.js — Monaco editor tab management, file open/save/close,
 * split editor, editor find bar, Monaco mount and theme sync.
 * Attach to window.TabbyCodeEditor.
 *
 * The mount function receives function references from mountChat's closure
 * and re-exports them so cross-module calls use window.TabbyCodeEditor.fn().
 *
 * ensureMonaco() lazy-loads monaco.js + lsp.js on first Code-mode use.
 */
(function () {
  "use strict";
  window.TabbyCodeEditor = {};

  var _monacoPromise = null;

  function assetUrl(name) {
    var base = window.TABBY_UI_BASE || "/v1/ui";
    var v = window.TABBY_UI_ASSET_V || "";
    return base + "/assets/" + name + (v ? "?v=" + v : "");
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var node = document.createElement("script");
      node.src = src;
      node.onload = resolve;
      node.onerror = function () { reject(new Error("Failed to load " + src)); };
      document.head.appendChild(node);
    });
  }

  window.TabbyCodeEditor.ensureMonaco = function ensureMonaco() {
    if (window.TabbyMonaco) return Promise.resolve(window.TabbyMonaco);
    if (_monacoPromise) return _monacoPromise;
    _monacoPromise = loadScript(assetUrl("monaco.js"))
      .then(function () {
        return loadScript(assetUrl("lsp.js"));
      })
      .then(function () {
        performance.mark("tabby-monaco-ready");
        return window.TabbyMonaco;
      })
      .catch(function (err) {
        _monacoPromise = null;
        throw err;
      });
    return _monacoPromise;
  };

  window.TabbyCodeEditor.mount = function mount(fns) {
    var T = window.TabbyCodeEditor;
    var keys = Object.keys(fns);
    for (var i = 0; i < keys.length; i++) {
      T[keys[i]] = fns[keys[i]];
    }
  };
})();
