/* TabbyStore — lightweight reactive state for the TabbyAPI UI */
(() => {
  const state = {
    gpu:     { status: null, switching: false, switchTarget: "", serving: false, mode: "" },
    flight:  { inFlight: false, chatId: "", away: false },
    context: { used: 0, max: 0, estimated: false, hide: true },
    user:    { name: "", isAdmin: false },
    page:    { current: "chat", previous: "" },
    tasks:   []
  };

  const subs = {};

  function notify(key, cur, prev) {
    const list = subs[key];
    if (!list) return;
    for (let i = list.length - 1; i >= 0; i--) list[i](cur, prev);
  }

  function clone(v) {
    if (v == null || typeof v !== "object") return v;
    return Array.isArray(v) ? v.slice() : Object.assign({}, v);
  }

  window.TabbyStore = Object.freeze({
    get(key) {
      return clone(state[key]);
    },

    set(key, value) {
      if (!(key in state)) return;
      const prev = state[key];
      state[key] = value;
      notify(key, value, prev);
    },

    patch(key, partial) {
      if (!(key in state) || typeof state[key] !== "object" || Array.isArray(state[key])) return;
      const prev = Object.assign({}, state[key]);
      Object.assign(state[key], partial);
      notify(key, state[key], prev);
    },

    subscribe(key, cb) {
      if (!subs[key]) subs[key] = [];
      subs[key].push(cb);
      return () => {
        const list = subs[key];
        if (!list) return;
        const idx = list.indexOf(cb);
        if (idx !== -1) list.splice(idx, 1);
      };
    },

    getAll() {
      const snap = {};
      for (const k in state) snap[k] = clone(state[k]);
      return Object.freeze(snap);
    }
  });
})();
