/* code-terminal.js — xterm terminal sessions, tabs, resize, WebSocket.
 * Attach to window.TabbyCodeTerminal.
 *
 * The mount function receives function references from mountChat's closure
 * and re-exports them so cross-module calls use window.TabbyCodeTerminal.fn().
 */
(function () {
  "use strict";
  window.TabbyCodeTerminal = {};

  window.TabbyCodeTerminal.mount = function mount(fns) {
    var T = window.TabbyCodeTerminal;
    var keys = Object.keys(fns);
    for (var i = 0; i < keys.length; i++) {
      T[keys[i]] = fns[keys[i]];
    }
  };
})();
