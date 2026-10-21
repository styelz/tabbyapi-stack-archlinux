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

  // Monaco's loader sets define.amd. xterm's UMD build then calls define()
  // and never assigns window.Terminal, so the pane stays on "xterm.js is missing."
  function loadClassicScript(src) {
    return fetch(src, { credentials: "same-origin" }).then(function (res) {
      if (!res.ok) throw new Error("Failed to load " + src);
      return res.text();
    }).then(function (code) {
      var define = window.define;
      var hide = define && define.amd;
      var saved = hide ? define.amd : undefined;
      if (hide) define.amd = undefined;
      try {
        var node = document.createElement("script");
        node.text = code;
        document.head.appendChild(node);
        node.remove();
      } finally {
        if (hide) define.amd = saved;
      }
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
    if (typeof window.Terminal === "function" && window.FitAddon && window.FitAddon.FitAddon) {
      return Promise.resolve(window.Terminal);
    }
    if (_xtermPromise) return _xtermPromise;
    _xtermPromise = Promise.resolve()
      .then(function () {
        loadCss(assetUrl("xterm.css"));
        return loadClassicScript(assetUrl("xterm.js"));
      })
      .then(function () {
        return loadClassicScript(assetUrl("xterm-addon-fit.js"));
      })
      .then(function () {
        if (typeof window.Terminal !== "function") {
          throw new Error("xterm.js did not load.");
        }
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
