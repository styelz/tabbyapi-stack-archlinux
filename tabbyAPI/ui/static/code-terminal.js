/* code-terminal.js — xterm terminal sessions, tabs, resize, WebSocket.
 * Attach to window.TabbyCodeTerminal.
 *
 * The mount function receives function references from mountChat's closure
 * and re-exports them so cross-module calls use window.TabbyCodeTerminal.fn().
 *
 * ensureXterm() lazy-loads xterm.js, xterm-addon-fit.js and xterm.css
 * on first terminal open.
 */
(function () {
  "use strict";
  window.TabbyCodeTerminal = {};

  var _xtermPromise = null;

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

  function loadCss(href) {
    if (document.querySelector('link[data-xterm-css="' + href + '"]')) return;
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.dataset.xtermCss = href;
    document.head.appendChild(link);
  }

  window.TabbyCodeTerminal.ensureXterm = function ensureXterm() {
    if (typeof window.Terminal === "function") return Promise.resolve(window.Terminal);
    if (_xtermPromise) return _xtermPromise;
    _xtermPromise = Promise.resolve()
      .then(function () {
        loadCss(assetUrl("xterm.css"));
        return loadScript(assetUrl("xterm.js"));
      })
      .then(function () {
        return loadScript(assetUrl("xterm-addon-fit.js"));
      })
      .then(function () {
        performance.mark("tabby-xterm-ready");
        return window.Terminal;
      })
      .catch(function (err) {
        _xtermPromise = null;
        throw err;
      });
    return _xtermPromise;
  };

  window.TabbyCodeTerminal.mount = function mount(fns) {
    var T = window.TabbyCodeTerminal;
    var keys = Object.keys(fns);
    for (var i = 0; i < keys.length; i++) {
      T[keys[i]] = fns[keys[i]];
    }
  };
})();
