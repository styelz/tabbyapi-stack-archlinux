/* code-preview.js — Browser preview iframe, tabs, address bar, navigation.
 * Attach to window.TabbyCodePreview.
 *
 * The mount function receives function references from mountChat's closure
 * and re-exports them so cross-module calls use window.TabbyCodePreview.fn().
 */
(function () {
  "use strict";
  window.TabbyCodePreview = {};

  window.TabbyCodePreview.mount = function mount(fns) {
    var T = window.TabbyCodePreview;
    var keys = Object.keys(fns);
    for (var i = 0; i < keys.length; i++) {
      T[keys[i]] = fns[keys[i]];
    }
  };
})();
