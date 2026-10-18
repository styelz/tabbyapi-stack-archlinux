/* code-files.js — File tree rendering, file filter, git pane, upload/download.
 * Attach to window.TabbyCodeFiles.
 *
 * The mount function receives function references from mountChat's closure
 * and re-exports them so cross-module calls use window.TabbyCodeFiles.fn().
 */
(function () {
  "use strict";
  window.TabbyCodeFiles = {};

  window.TabbyCodeFiles.mount = function mount(fns) {
    var T = window.TabbyCodeFiles;
    var keys = Object.keys(fns);
    for (var i = 0; i < keys.length; i++) {
      T[keys[i]] = fns[keys[i]];
    }
  };
})();
