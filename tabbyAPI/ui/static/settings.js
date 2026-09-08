function mountSettings(root) {
  root.innerHTML = `
    <div class="settings-page">
      <div class="toolbar">
        <button class="btn primary" type="button" id="settings-save">Save</button>
        <button class="btn" type="button" id="settings-reload">Reload</button>
        <button class="btn danger" type="button" id="settings-restart">Restart API</button>
        <span class="spacer"></span>
        <div class="settings-stamp">
          <span class="settings-path-line" id="settings-path"></span>
          <span class="settings-hint" id="settings-hint"></span>
        </div>
      </div>
      <p class="error" id="settings-error" hidden></p>
      <p class="muted" id="settings-ok" hidden></p>
      <div class="settings-layout">
        <nav class="settings-nav" id="settings-nav" aria-label="Settings sections"></nav>
        <div class="settings-body" id="settings-body"></div>
      </div>
    </div>
  `;
  const nav = root.querySelector("#settings-nav");
  const body = root.querySelector("#settings-body");
  const err = root.querySelector("#settings-error");
  const ok = root.querySelector("#settings-ok");
  const pathEl = root.querySelector("#settings-path");
  const hintEl = root.querySelector("#settings-hint");
  let payload = null;
  let selectedSec = "";

  function showError(message) {
    err.hidden = !message;
    err.textContent = message || "";
    if (message) ok.hidden = true;
  }

  function showOk(message) {
    ok.hidden = !message;
    ok.textContent = message || "";
    if (message) err.hidden = true;
  }

  function fieldId(section, name) {
    return `set-${section}-${name}`;
  }

  function prettyLabel(text) {
    const value = String(text || "").trim();
    if (!value || value !== value.toLowerCase()) return value;
    const acronyms = { sse: "SSE", api: "API", url: "URL", gpu: "GPU", llm: "LLM", ip: "IP" };
    return value.replace(/(^|\s)(\S+)/g, (match, space, word) => {
      if (acronyms[word]) return `${space}${acronyms[word]}`;
      return `${space}${word.charAt(0).toUpperCase()}${word.slice(1)}`;
    });
  }

  function helpText(text) {
    return String(text || "").replace(/\n+/g, " ").trim();
  }

  function layoutClass(kind) {
    if (kind === "bool") return " is-check";
    if (kind === "int" || kind === "float") return " is-number";
    if (kind === "json") return " is-stack";
    return "";
  }

  function formatValue(value) {
    if (value == null) return "";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function sameValue(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  function inputFor(section, field) {
    const id = fieldId(section, field.name);
    const kind = field.kind;
    const value = field.value;
    if (kind === "bool") {
      const on = value === true || value === "true";
      return `<input id="${id}" type="checkbox"${on ? " checked" : ""} />`;
    }
    if (kind === "select") {
      const choices = field.choices || [];
      const current = value == null || value === "" ? "" : String(value);
      const blank = field.blank != null ? `<option value="">${TabbyUI.escapeHtml(field.blank)}</option>` : "";
      const opts = choices
        .map((choice) => {
          const picked = current === String(choice) ? " selected" : "";
          return `<option value="${TabbyUI.escapeHtml(String(choice))}"${picked}>${TabbyUI.escapeHtml(String(choice))}</option>`;
        })
        .join("");
      return `<select id="${id}">${blank}${opts}</select>`;
    }
    if (kind === "json") {
      const text = value == null || value === "" ? "" : JSON.stringify(value, null, 2);
      return `<textarea id="${id}" rows="5" spellcheck="false">${TabbyUI.escapeHtml(text)}</textarea>`;
    }
    if (kind === "list_text" || kind === "list_number") {
      const text = Array.isArray(value) ? value.join(", ") : formatValue(value);
      return `<input id="${id}" type="text" value="${TabbyUI.escapeHtml(text)}" />`;
    }
    if (kind === "int" || kind === "float") {
      const text = value == null ? "" : String(value);
      const step = kind === "int" ? "1" : "any";
      return `<input id="${id}" type="number" step="${step}" value="${TabbyUI.escapeHtml(text)}" />`;
    }
    const type = field.secret ? "password" : "text";
    const placeholder = field.secret && field.set ? "leave blank to keep" : "";
    return `<input id="${id}" type="${type}" value="${field.secret ? "" : TabbyUI.escapeHtml(formatValue(value))}" placeholder="${TabbyUI.escapeHtml(placeholder)}" autocomplete="off" />`;
  }

  function fieldHtml(section, field) {
    const id = fieldId(section, field.name);
    const help = helpText(field.description);
    const live = field.secret ? "" : field.live;
    const fileVal = field.secret ? null : field.value;
    const showLive = !field.secret && live != null && !sameValue(live, fileVal);
    const env = field.env;
    const notes = [];
    if (field.secret && field.set) notes.push("Saved");
    if (env) notes.push("Env override");
    if (showLive) notes.push(`Active: ${formatValue(live)}`);
    const note = notes.length
      ? `<div class="settings-notes">${notes.map((item) => `<span class="settings-note">${TabbyUI.escapeHtml(item)}</span>`).join("")}</div>`
      : "";
    const clear = field.secret && field.set
      ? `<label class="settings-clear"><input type="checkbox" data-clear="${TabbyUI.escapeHtml(field.name)}" /> Clear</label>`
      : "";
    return `
      <div class="settings-field${layoutClass(field.kind)}" data-section="${TabbyUI.escapeHtml(section)}" data-name="${TabbyUI.escapeHtml(field.name)}" data-kind="${TabbyUI.escapeHtml(field.kind)}">
        <div class="settings-field-meta">
          <label for="${id}">${TabbyUI.escapeHtml(prettyLabel(field.label || field.name))}</label>
          ${help ? `<p class="settings-help" title="${TabbyUI.escapeHtml(field.description || "")}">${TabbyUI.escapeHtml(help)}</p>` : ""}
        </div>
        <div class="settings-field-control">
          ${inputFor(section, field)}
          ${note}
          ${clear}
        </div>
      </div>
    `;
  }

  function sectionCard(section) {
    const path = section.path ? `<p class="muted settings-path">${TabbyUI.escapeHtml(section.path)}</p>` : "";
    const fields = (section.fields || []).map((field) => fieldHtml(section.name, field)).join("");
    return `
      <section class="card settings-card" id="settings-sec-${TabbyUI.escapeHtml(section.name)}" hidden>
        <header class="settings-card-head">
          <h2>${TabbyUI.escapeHtml(prettyLabel(section.label || section.name))}</h2>
          ${section.description ? `<p class="muted">${TabbyUI.escapeHtml(section.description)}</p>` : ""}
          ${path}
        </header>
        <div class="settings-fields">${fields}</div>
      </section>
    `;
  }

  function showSection(name) {
    const sections = [...(payload && payload.tabby || []), payload && payload.screensaver, payload && payload.updates, payload && payload.gpu, payload && payload.system].filter(Boolean);
    const names = sections.map((section) => section.name);
    const next = names.includes(name) ? name : names[0] || "";
    selectedSec = next;
    body.querySelectorAll(".settings-card").forEach((card) => {
      card.hidden = card.id !== `settings-sec-${next}`;
    });
    nav.querySelectorAll(".settings-nav-item").forEach((item) => {
      const on = item.dataset.sec === next;
      item.classList.toggle("is-on", on);
      if (on) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    });
    body.scrollTop = 0;
  }

  function paint(data) {
    payload = data;
    const sections = [...(data.tabby || []), data.screensaver, data.updates, data.gpu, data.system].filter(Boolean);
    nav.innerHTML = sections
      .map((section) => (
        `<button type="button" class="settings-nav-item" data-sec="${TabbyUI.escapeHtml(section.name)}">${TabbyUI.escapeHtml(prettyLabel(section.label || section.name))}</button>`
      ))
      .join("");
    body.innerHTML = sections.map(sectionCard).join("");
    pathEl.textContent = (data.paths && data.paths.config) || "";
    hintEl.textContent = data.restart_hint || "";
    showSection(selectedSec);
  }

  function parseField(wrap, spec) {
    const input = wrap.querySelector("input:not([data-clear]), select, textarea");
    if (!input) return undefined;
    const kind = spec.kind;
    if (spec.secret) {
      const clear = wrap.querySelector("[data-clear]");
      if (clear && clear.checked) return null;
      if (!String(input.value || "").trim()) return undefined;
      return input.value;
    }
    if (kind === "bool") return Boolean(input.checked);
    if (kind === "select") {
      const value = input.value;
      if (spec.blank != null && value === "") return null;
      if (spec.choices && spec.choices.length === 2 && spec.choices[0] === "true") {
        return value === "true";
      }
      return value;
    }
    if (kind === "json") {
      const text = String(input.value || "").trim();
      if (!text) return spec.optional ? null : {};
      return JSON.parse(text);
    }
    if (kind === "list_text") {
      return String(input.value || "")
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
    }
    if (kind === "list_number") {
      return String(input.value || "")
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => {
          const n = part.includes(".") ? Number(part) : parseInt(part, 10);
          if (!Number.isFinite(n)) throw new Error("not a number");
          return n;
        });
    }
    if (kind === "int") {
      const text = String(input.value || "").trim();
      if (!text) return spec.optional ? null : 0;
      const n = parseInt(text, 10);
      if (!Number.isFinite(n)) throw new Error("not a number");
      return n;
    }
    if (kind === "float") {
      const text = String(input.value || "").trim();
      if (!text) return spec.optional ? null : 0;
      const n = Number(text);
      if (!Number.isFinite(n)) throw new Error("not a number");
      return n;
    }
    const text = String(input.value || "");
    if (!text.trim() && spec.optional) return null;
    return text;
  }

  function collect() {
    const specs = {};
    (payload.tabby || []).forEach((section) => {
      (section.fields || []).forEach((field) => {
        specs[`${section.name}.${field.name}`] = field;
      });
    });
    (payload.system && payload.system.fields || []).forEach((field) => {
      specs[`system.${field.name}`] = field;
    });
    (payload.screensaver && payload.screensaver.fields || []).forEach((field) => {
      specs[`screensaver.${field.name}`] = field;
    });
    (payload.updates && payload.updates.fields || []).forEach((field) => {
      specs[`updates.${field.name}`] = field;
    });
    (payload.gpu && payload.gpu.fields || []).forEach((field) => {
      specs[`gpu.${field.name}`] = field;
    });
    const tabby = {};
    const system = {};
    const screensaver = {};
    const updates = {};
    const gpu = {};
    body.querySelectorAll(".settings-field").forEach((wrap) => {
      const section = wrap.dataset.section;
      const name = wrap.dataset.name;
      const spec = specs[`${section}.${name}`];
      if (!spec) return;
      let value;
      try {
        value = parseField(wrap, spec);
      } catch (exc) {
        throw new Error(`${section}.${name}: ${exc.message || "invalid value"}`);
      }
      if (value === undefined) return;
      if (section === "system") system[name] = value;
      else if (section === "screensaver") screensaver[name] = value;
      else if (section === "updates") updates[name] = value;
      else if (section === "gpu") gpu[name] = value;
      else {
        if (!tabby[section]) tabby[section] = {};
        tabby[section][name] = value;
      }
    });
    return { tabby, system, screensaver, updates, gpu };
  }

  async function load() {
    showError("");
    const data = await TabbyUI.api("settings");
    paint(data);
  }

  async function save() {
    showError("");
    showOk("");
    const bodyPayload = collect();
    const data = await TabbyUI.api("settings", { method: "PUT", body: bodyPayload });
    paint(data);
    if (data.reload_warning) {
      showError(data.reload_warning);
      return;
    }
    showOk("Saved. Restart the API if network, model, or system values changed.");
  }

  async function restartApi() {
    const yes = await TabbyUI.confirmModal({
      title: "Restart API?",
      text: "Restart TabbyAPI now? The UI will drop for about a minute.",
      yes: "Restart",
      no: "Cancel",
    });
    if (!yes) return;
    const modal = TabbyUI.progressModal({
      title: "Restarting",
      note: "Restarting TabbyAPI. The UI will drop for about a minute.",
    });
    try {
      await TabbyUI.followRestart(modal);
      modal.setActions([
        { label: "Close", run: () => modal.close() },
        { label: "Reload UI", primary: true, run: () => location.reload() },
      ]);
    } catch (exc) {
      modal.setBusy(false);
      modal.setTitle("Restart");
      modal.setNote((exc && exc.message) || "Restart failed.");
      modal.setActions([{ label: "Close", primary: true, run: () => modal.close() }]);
    }
  }

  nav.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-sec]");
    if (!btn) return;
    showSection(btn.dataset.sec);
  });

  root.querySelector("#settings-save").addEventListener("click", () => {
    save().catch((exc) => showError(exc.message || "Could not save."));
  });
  root.querySelector("#settings-reload").addEventListener("click", () => {
    load().catch((exc) => showError(exc.message || "Could not load."));
  });
  root.querySelector("#settings-restart").addEventListener("click", () => {
    restartApi().catch((exc) => showError(exc.message || "Restart failed."));
  });

  load().catch((exc) => showError(exc.message || "Could not load settings."));
  return {
    resume() {
      load().catch(() => {});
    },
    destroy() {},
  };
}

window.mountSettings = mountSettings;
