/* code-editor.js — Monaco editor tab management, file open/save/close,
 * split editor, editor find bar, Monaco mount and theme sync.
 * Attach to window.TabbyCodeEditor.
 *
 * The mount function receives function references from mountChat's closure
 * and re-exports them so cross-module calls use window.TabbyCodeEditor.fn().
 */
(function () {
  "use strict";
  window.TabbyCodeEditor = {};

  window.TabbyCodeEditor.mount = function mount(fns) {
    var T = window.TabbyCodeEditor;
    var keys = Object.keys(fns);
    for (var i = 0; i < keys.length; i++) {
      T[keys[i]] = fns[keys[i]];
    }
  };
})();
