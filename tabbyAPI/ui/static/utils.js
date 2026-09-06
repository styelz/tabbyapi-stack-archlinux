(() => {
  const MARKER = "/v1/ui";
  let authRedirecting = false;

  function uiBase() {
    const path = window.location.pathname || "";
    const idx = path.indexOf(MARKER);
    if (idx >= 0) return path.slice(0, idx + MARKER.length);
    return MARKER;
  }

  function uiPath(suffix) {
    const base = uiBase();
    const part = String(suffix || "").replace(/^\/+/, "");
    return part ? `${base}/${part}` : `${base}/`;
  }

  function redirectToLogin() {
    if (authRedirecting) return;
    authRedirecting = true;
    window.location.replace(uiPath("login"));
  }

  /** Map server paths (/v1/ui/...) onto the browser's proxy prefix. */
  function resolveUiUrl(url) {
    const value = String(url || "");
    if (value.startsWith(MARKER)) return uiBase() + value.slice(MARKER.length);
    if (value.startsWith("/ui/")) return uiBase() + value.slice(3);
    if (value === "/ui") return `${uiBase()}/`;
    return value;
  }

  function apiUrl(path) {
    const value = String(path || "");
    if (value.startsWith("http")) return value;
    if (value.startsWith(uiBase() + "/") || value === uiBase()) return value;
    if (value.startsWith(MARKER + "/")) return uiBase() + value.slice(MARKER.length);
    if (value.startsWith("/ui/")) return uiBase() + value.slice(3);
    if (value.startsWith("/")) return uiPath(value.slice(1));
    return uiPath(value);
  }

  function compactText(value, max = 180) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, max);
  }

  function looksLikeHtml(value) {
    // Proxy 502 pages start with a document. Do not match <h1> / <body> inside
    // a Code reply that quotes the HTML it just wrote.
    return /^(?:<\s*!doctype[\s>]|<\s*html\b)/i.test(String(value || "").trim());
  }

  /** Proxy 502/503/504 pages are HTML; never dump that markup into the UI. */
  function httpErrorMessage(response, data) {
    const status = response && response.status;
    const unavailable = status === 502 || status === 503 || status === 504;
    if (unavailable) {
      return `API unavailable (${status}) — service may be restarting`;
    }
    if (data && typeof data === "object") {
      const detail = data.detail;
      if (Array.isArray(detail) && detail.length) {
        const first = detail[0];
        const msg = (first && (first.msg || first.message)) || first;
        if (msg) return compactText(msg);
      } else if (typeof detail === "string" && detail.trim()) {
        return compactText(detail);
      }
      if (typeof data.message === "string" && data.message.trim()) {
        return compactText(data.message);
      }
    }
    if (typeof data === "string" && data.trim()) {
      if (looksLikeHtml(data)) {
        const heading = data.match(/<\s*h[1-6][^>]*>([\s\S]*?)<\s*\/\s*h[1-6]>/i);
        const stripped = (heading ? heading[1] : data).replace(/<[^>]+>/g, " ");
        return compactText(stripped) || `Request failed (${status})`;
      }
      return compactText(data);
    }
    return status ? `Request failed (${status})` : "Request failed";
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    if (options.body && typeof options.body !== "string" && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    const url = apiUrl(path);
    let response;
    try {
      response = await fetch(url, Object.assign({ credentials: "same-origin" }, options, { headers }));
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new Error("API unreachable — service may be restarting");
    }
    if (response.status === 401 && !String(url).includes("/auth/login")) {
      redirectToLogin();
      throw new Error("Not authenticated");
    }
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      throw new Error(httpErrorMessage(response, data));
    }
    return data;
  }

  function drainNdjson(buf, onEvent, flush) {
    let rest = buf;
    let nl = rest.indexOf("\n");
    while (nl >= 0) {
      const line = rest.slice(0, nl).trim();
      rest = rest.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
      nl = rest.indexOf("\n");
    }
    if (flush && rest.trim()) {
      onEvent(JSON.parse(rest.trim()));
      return "";
    }
    return rest;
  }

  async function readNdjson(response, onEvent) {
    const emit = (event) => {
      if (typeof onEvent === "function") onEvent(event);
      return event;
    };
    if (!response || !response.body || typeof response.body.getReader !== "function") {
      const text = await response.text();
      let last = null;
      drainNdjson(String(text || ""), (event) => {
        last = emit(event);
      }, true);
      return last;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let last = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      buf = drainNdjson(buf, (event) => {
        last = emit(event);
      }, false);
    }
    buf += decoder.decode();
    drainNdjson(buf, (event) => {
      last = emit(event);
    }, true);
    return last;
  }

  function $(sel, root = document) {
    return root.querySelector(sel);
  }

  function $all(sel, root = document) {
    return Array.from(root.querySelectorAll(sel));
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function formatBytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const sec = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    if (h) return `${h}h ${pad(m)}m ${pad(sec)}s`;
    if (m) return `${m}m ${pad(sec)}s`;
    return `${sec}s`;
  }

  function formatAssistantContent(text) {
    let out = String(text || "");
    out = out.replace(/[ \t]*tabby-image-job:\s*[0-9a-fA-F-]{8,}[ \t]*/gi, "");
    out = out.replace(/^[ \t]*tabby-switch-llm[ \t]*\n?/gim, "");
    out = out.replace(/<mode_hint\b[^>]*>[\s\S]*?<\/mode_hint>/gi, "");
    out = out.replace(/^\d+\s+image\(s\) from this turn:\s*$/gim, "");
    out = out.replace(/^\d+\.\s+generated-\d{8}-\d{6}-\d+\.png\s*$/gim, "");
    out = out.replace(/^These URLs are on this API host\.[^\n]*$/gim, "");
    out = out.replace(/^Another picture:\s*[^\n]+$/gim, "");
    out = out.replace(/^This picture:\s*/gim, "");
    // Model-only image dest reminders. The GPU already has the plan.
    out = out.replace(/Write the page now\.[^\n]*/gi, "");
    out = out.replace(/Point img src or CSS url\(\) at these exact (?:local )?paths:[^\n]*/gi, "");
    out = out.replace(/Write HTML, CSS, and JS only[^\n]*/gi, "");
    out = out.replace(/Do not Write PNG[^\n]*/gi, "");
    out = out.replace(/Write every HTML\/CSS\/JS file for this page now\.[^\n]*/gi, "");
    out = out.replace(/Do not generate images\.[^\n]*/gi, "");
    out = out.replace(/Do not dump the page in chat[^\n]*/gi, "");
    out = out.replace(/The GPU will save those PNG files[^\n]*/gi, "");
    out = out.replace(/\n{3,}/g, "\n\n");
    return out.trim();
  }

  function isImageHref(href) {
    return (
      /\.(png|jpg|jpeg|webp)(\?|$)/i.test(href) ||
      href.includes("/gallery/file/") ||
      href.includes("/v1/images/")
    );
  }

  function workspaceImageHint(alt) {
    const label = String(alt || "").trim().replace(/\\/g, "/").replace(/^\/+/, "");
    if (!label || label.includes("..") || /^(https?:)?\/\//i.test(label)) return "";
    return /\.(png|jpe?g|webp|gif)$/i.test(label) ? label : "";
  }

  function markdownHrefAllowed(href) {
    const value = String(href || "").trim();
    if (!value) return false;
    const lower = value.toLowerCase();
    if (
      lower.startsWith("javascript:") ||
      lower.startsWith("data:") ||
      lower.startsWith("vbscript:") ||
      lower.startsWith("file:")
    ) {
      return false;
    }
    if (/^(https?:)?\/\//i.test(value) || lower.startsWith("http:") || lower.startsWith("https:")) {
      try {
        const parsed = new URL(value, window.location.href);
        return parsed.protocol === "http:" || parsed.protocol === "https:";
      } catch {
        return false;
      }
    }
    if (value.startsWith("/v1/ui/") || value.startsWith("/v1/images/")) return true;
    if (value.startsWith("/")) return false;
    if (value.includes("..")) return false;
    return true;
  }

  function markdownImage(href, alt, inlineImages) {
    const resolved = resolveUiUrl(href);
    const allowed = markdownHrefAllowed(resolved);
    const safeHref = escapeHtml(allowed ? resolved : "#");
    const safeAlt = escapeHtml(alt || "");
    const cleanHref = String(href || "").split(/[?#]/, 1)[0];
    const fallback = cleanHref.slice(cleanHref.lastIndexOf("/") + 1) || "Generated image";
    let label = alt || fallback;
    try {
      label = decodeURIComponent(label);
    } catch (_) {
      // Keep the original label when a URL contains malformed escapes.
    }
    if (inlineImages === false) {
      const fileHint = workspaceImageHint(label);
      const fileAttr = fileHint ? ` data-file="${escapeHtml(fileHint)}"` : "";
      return (
        `<a class="md-image-link" href="${safeHref}"${fileAttr}>` +
        `<span class="md-image-link-kind">Image</span>` +
        `<span class="md-image-link-name">${escapeHtml(label)}</span>` +
        `<span class="md-image-link-open">Open</span></a>`
      );
    }
    if (!allowed) {
      return `<span class="md-image-link-name">${escapeHtml(label)}</span>`;
    }
    return (
      `<figure class="md-image">` +
      `<img src="${safeHref}" alt="${safeAlt}">` +
      `<button type="button" class="btn ghost md-image-dl" data-href="${safeHref}" data-name="${escapeHtml(fallback)}">Download</button>` +
      `</figure>`
    );
  }

  function codeBlockHtml(lang, code) {
    const label = String(lang || "").trim() || "code";
    const kind = window.TabbyHighlight ? window.TabbyHighlight.language(lang) : "";
    const body = window.TabbyHighlight
      ? window.TabbyHighlight.highlight(lang, code)
      : escapeHtml(code);
    const langClass = kind ? ` class="language-${kind}"` : "";
    return (
      `<div class="md-code">` +
      `<div class="md-code-head"><span class="md-code-lang">${escapeHtml(label)}</span>` +
      `<span class="md-code-tools">` +
      `<button type="button" class="md-code-copy">Copy</button>` +
      `<button type="button" class="md-code-apply">Apply</button>` +
      `<button type="button" class="md-code-insert">Insert</button>` +
      `</span></div>` +
      `<pre><code${langClass}>${body}</code></pre></div>`
    );
  }

  function stripFenceIndent(line, indent) {
    if (!indent) return line;
    if (line.startsWith(indent)) return line.slice(indent.length);
    let n = 0;
    while (n < indent.length && n < line.length && (line[n] === " " || line[n] === "\t")) n += 1;
    return line.slice(n);
  }

  function extractFences(raw) {
    const fences = [];
    const lines = String(raw || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    const out = [];
    const openRe = /^(\s*)(`{3,}|~{3,})[ \t]*([\w+-]*)(.*)$/;
    for (let i = 0; i < lines.length; i += 1) {
      const open = openRe.exec(lines[i]);
      if (!open) {
        out.push(lines[i]);
        continue;
      }
      const indent = open[1];
      const marker = open[2];
      const lang = open[3] || "";
      const rest = open[4] || "";
      const fenceChar = marker[0];
      const closeSame = rest.match(fenceChar === "`" ? /`{3,}[ \t]*$/ : /~{3,}[ \t]*$/);
      if (closeSame && rest.slice(0, closeSame.index).trim()) {
        const code = rest.slice(0, closeSame.index).trim();
        const token = `@@CODE${fences.length}@@`;
        fences.push(codeBlockHtml(lang, code));
        out.push(`${indent}${token}`);
        continue;
      }
      if (rest.includes(fenceChar)) {
        out.push(lines[i]);
        continue;
      }
      const body = [];
      i += 1;
      const closeRe =
        fenceChar === "`"
          ? new RegExp(`^\\s*\`{${marker.length},}[ \\t]*$`)
          : new RegExp(`^\\s*~{${marker.length},}[ \\t]*$`);
      while (i < lines.length && !closeRe.test(lines[i])) {
        body.push(stripFenceIndent(lines[i], indent));
        i += 1;
      }
      const token = `@@CODE${fences.length}@@`;
      fences.push(codeBlockHtml(lang, body.join("\n")));
      out.push(`${indent}${token}`);
    }
    return { text: out.join("\n"), fences };
  }

  function autolink(html, used, inlineImages) {
    return html.replace(
      /(https?:\/\/[^\s<]+|\/v1\/ui\/gallery\/file\/[^\s<]+|\/ui\/gallery\/file\/[^\s<]+|\/v1\/images\/generated-[^\s<]+)/g,
      (url) => {
        const href = resolveUiUrl(url);
        if (used.has(url) || used.has(href)) return "";
        if (isImageHref(href)) {
          used.add(href);
          used.add(url);
          return markdownImage(href, "", inlineImages);
        }
        return `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>`;
      }
    );
  }

  function formatInline(text, used, inlineImages) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return autolink(html, used, inlineImages);
  }

  function isFenceToken(line) {
    return /^@@CODE\d+@@$/.test(line.trim()) || /^@@IMG\d+@@$/.test(line.trim());
  }

  function renderBlocks(src, used, inlineImages) {
    const lines = String(src || "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i += 1;
        continue;
      }
      if (isFenceToken(line)) {
        out.push(line.trim());
        i += 1;
        continue;
      }
      const heading = /^(#{1,3})\s+(.+)$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${formatInline(heading[2], used, inlineImages)}</h${level}>`);
        i += 1;
        continue;
      }
      const ordered = /^\s*\d+\.\s+/.test(line);
      const bullet = /^\s*[-*]\s+/.test(line);
      if (ordered || bullet) {
        const itemRe = ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*]\s+(.+)$/;
        const otherListRe = ordered ? /^\s*[-*]\s+/ : /^\s*\d+\.\s+/;
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (i < lines.length) {
          const item = itemRe.exec(lines[i]);
          if (!item) break;
          const parts = [formatInline(item[1], used, inlineImages)];
          i += 1;
          while (i < lines.length) {
            const raw = lines[i];
            if (!raw.trim()) {
              let j = i + 1;
              while (j < lines.length && !lines[j].trim()) j += 1;
              if (j >= lines.length) break;
              if (itemRe.test(lines[j]) || otherListRe.test(lines[j])) break;
              if (isFenceToken(lines[j]) || /^\s+/.test(lines[j])) {
                i += 1;
                continue;
              }
              break;
            }
            if (itemRe.test(raw) || otherListRe.test(raw)) break;
            if (/^(#{1,3})\s+/.test(raw)) break;
            if (isFenceToken(raw)) {
              parts.push(raw.trim());
              i += 1;
              continue;
            }
            if (/^\s+/.test(raw)) {
              parts.push(`<br>${formatInline(raw.trim(), used, inlineImages)}`);
              i += 1;
              continue;
            }
            break;
          }
          items.push(`<li>${parts.join("")}</li>`);
        }
        out.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }
      const para = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !isFenceToken(lines[i]) &&
        !/^(#{1,3})\s+/.test(lines[i]) &&
        !/^\s*[-*]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i])
      ) {
        para.push(lines[i]);
        i += 1;
      }
      out.push(`<p>${para.map((part) => formatInline(part, used, inlineImages)).join("<br>")}</p>`);
    }
    return out.join("");
  }

  function renderMarkdown(text, options) {
    const inlineImages = !options || options.inlineImages !== false;
    const used = new Set();
    const images = [];
    const extracted = extractFences(String(text || ""));
    let src = extracted.text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, url) => {
      const href = String(url || "").trim();
      used.add(href);
      used.add(resolveUiUrl(href));
      const token = `@@IMG${images.length}@@`;
      images.push(markdownImage(href, alt, inlineImages));
      return token;
    });
    let html = renderBlocks(src, used, inlineImages);
    html = html.replace(/@@IMG(\d+)@@/g, (_, i) => images[Number(i)] || "");
    html = html.replace(/@@CODE(\d+)@@/g, (_, i) => extracted.fences[Number(i)] || "");
    return html;
  }

  function copyText(text) {
    const value = String(text || "");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value);
    }
    return new Promise((resolve, reject) => {
      try {
        const ta = document.createElement("textarea");
        ta.value = value;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        resolve();
      } catch (err) {
        reject(err);
      }
    });
  }

  function selectedText() {
    const sel = window.getSelection();
    return sel ? String(sel).trim() : "";
  }

  function selectionIn(node) {
    if (!node) return "";
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || !String(sel).trim()) return "";
    const range = sel.getRangeAt(0);
    if (!node.contains(range.commonAncestorContainer)) return "";
    return String(sel).trim();
  }

  function editCut(el) {
    if (!el || el.readOnly || el.disabled) return Promise.resolve();
    const start = el.selectionStart;
    const end = el.selectionEnd;
    if (start === end) return Promise.resolve();
    return copyText(el.value.slice(start, end)).then(() => {
      el.setRangeText("", start, end, "end");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  function editPaste(el) {
    if (!el || el.readOnly || el.disabled) return Promise.resolve();
    const apply = (text) => {
      const start = el.selectionStart;
      const end = el.selectionEnd;
      el.setRangeText(String(text || ""), start, end, "end");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    if (navigator.clipboard && navigator.clipboard.readText) {
      return navigator.clipboard.readText().then(apply);
    }
    return Promise.reject(new Error("Clipboard paste is not available"));
  }

  function inputMenuItems(el, extras) {
    if (!el) return extras || [];
    const start = el.selectionStart == null ? 0 : el.selectionStart;
    const end = el.selectionEnd == null ? 0 : el.selectionEnd;
    const hasSel = start !== end;
    const hasVal = Boolean(el.value);
    const locked = Boolean(el.readOnly || el.disabled);
    const items = [
      {
        label: "Cut",
        disabled: locked || !hasSel,
        kbd: "Ctrl+X",
        run: () => editCut(el),
      },
      {
        label: "Copy",
        disabled: !hasSel,
        kbd: "Ctrl+C",
        run: () => copyText(el.value.slice(start, end)),
      },
      {
        label: "Paste",
        disabled: locked,
        kbd: "Ctrl+V",
        run: () => editPaste(el).catch(() => {}),
      },
      {
        label: "Select all",
        disabled: !hasVal,
        kbd: "Ctrl+A",
        run: () => {
          el.focus();
          el.select();
        },
      },
    ];
    if (extras && extras.length) items.push({ sep: true }, ...extras);
    return items;
  }

  let ctxMenu = null;
  let ctxCleanup = null;

  function hideContextMenu() {
    if (ctxCleanup) {
      ctxCleanup();
      ctxCleanup = null;
    }
    if (ctxMenu) {
      ctxMenu.remove();
      ctxMenu = null;
    }
  }

  function normalizeMenuItems(items) {
    const out = [];
    (items || []).forEach((item) => {
      if (!item) return;
      if (item === "—" || item === "-" || item.sep) {
        if (out.length && !out[out.length - 1].sep) out.push({ sep: true });
        return;
      }
      out.push(item);
    });
    while (out.length && out[out.length - 1].sep) out.pop();
    return out;
  }

  function showContextMenu(event, items) {
    const list = normalizeMenuItems(items);
    if (!list.length) return false;
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    hideContextMenu();
    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    menu.setAttribute("role", "menu");
    const buttons = [];
    list.forEach((item) => {
      if (item.sep) {
        const hr = document.createElement("div");
        hr.className = "ctx-sep";
        hr.setAttribute("role", "separator");
        menu.appendChild(hr);
        return;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.setAttribute("role", "menuitem");
      btn.className = "ctx-item" + (item.danger ? " is-danger" : "");
      btn.disabled = Boolean(item.disabled);
      const label = document.createElement("span");
      label.textContent = item.label || "";
      btn.appendChild(label);
      if (item.kbd) {
        const kbd = document.createElement("kbd");
        kbd.textContent = item.kbd;
        btn.appendChild(kbd);
      }
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        if (btn.disabled) return;
        hideContextMenu();
        if (typeof item.run === "function") item.run();
      });
      menu.appendChild(btn);
      buttons.push(btn);
    });
    if (!buttons.length) return false;
    document.body.appendChild(menu);
    const x = event && event.clientX != null ? event.clientX : 8;
    const y = event && event.clientY != null ? event.clientY : 8;
    const rect = menu.getBoundingClientRect();
    const pad = 8;
    let left = x;
    let top = y;
    if (left + rect.width > window.innerWidth - pad) left = window.innerWidth - rect.width - pad;
    if (top + rect.height > window.innerHeight - pad) top = Math.max(pad, y - rect.height);
    if (left < pad) left = pad;
    if (top < pad) top = pad;
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;

    let index = buttons.findIndex((btn) => !btn.disabled);
    const focusAt = (next) => {
      if (!buttons.length) return;
      index = (next + buttons.length) % buttons.length;
      let guard = 0;
      while (buttons[index].disabled && guard < buttons.length) {
        index = (index + 1) % buttons.length;
        guard += 1;
      }
      buttons[index].focus();
    };
    if (index >= 0) buttons[index].focus();

    const onKey = (ev) => {
      if (!ctxMenu) return;
      if (ev.key === "Escape") {
        ev.preventDefault();
        ev.stopPropagation();
        hideContextMenu();
        return;
      }
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        focusAt(index + 1);
      }
      if (ev.key === "ArrowUp") {
        ev.preventDefault();
        focusAt(index - 1);
      }
      if (ev.key === "Home") {
        ev.preventDefault();
        focusAt(0);
      }
      if (ev.key === "End") {
        ev.preventDefault();
        focusAt(buttons.length - 1);
      }
    };
    const onPointer = (ev) => {
      if (ctxMenu && ctxMenu.contains(ev.target)) return;
      hideContextMenu();
    };
    const onReposition = () => hideContextMenu();
    const timer = setTimeout(() => {
      document.addEventListener("pointerdown", onPointer, true);
      document.addEventListener("keydown", onKey, true);
      window.addEventListener("resize", onReposition);
      window.addEventListener("scroll", onReposition, true);
    }, 0);
    ctxCleanup = () => {
      clearTimeout(timer);
      document.removeEventListener("pointerdown", onPointer, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    };
    ctxMenu = menu;
    return true;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function logLevelClass(line) {
    if (/\b(ERROR|CRITICAL)\b/i.test(line)) return "lvl-error";
    if (/\bWARN(ING)?\b/i.test(line)) return "lvl-warning";
    if (/\bDEBUG\b/i.test(line)) return "lvl-debug";
    return "lvl-info";
  }

  function progressModal({ title = "Working", note = "" } = {}) {
    const existing = document.querySelector(".dialog-modal[data-progress]");
    if (existing && existing._tabbyProgress && !existing._tabbyProgress.closed) {
      const handle = existing._tabbyProgress;
      if (handle.busy) return handle;
      handle.close();
    }

    const wrap = document.createElement("div");
    wrap.className = "dialog-modal";
    wrap.dataset.progress = "1";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.innerHTML =
      '<div class="dialog-card dialog-progress">' +
      '<div class="progress-head"><h2></h2><span class="muted progress-elapsed">0s</span></div>' +
      '<p class="progress-note"></p>' +
      '<pre class="log-view progress-log" aria-live="polite"></pre>' +
      '<div class="dialog-actions"></div></div>';
    wrap.querySelector("h2").textContent = title;
    wrap.querySelector(".progress-note").textContent = note || "Working…";
    document.body.appendChild(wrap);

    const view = wrap.querySelector(".progress-log");
    const actionsEl = wrap.querySelector(".dialog-actions");
    const noteEl = wrap.querySelector(".progress-note");
    const elapsedEl = wrap.querySelector(".progress-elapsed");
    const MAX_LINES = 800;
    const handle = {};
    wrap._tabbyProgress = handle;

    let busy = true;
    let closed = false;
    let stick = true;
    let buffer = [];
    let journal = null;
    let journalRetry = 0;
    let updateTimer = 0;
    let updateSeen = 0;
    let updateRunning = false;
    let clockTimer = 0;
    const startedAt = Date.now();

    function tickElapsed() {
      elapsedEl.textContent = formatDuration((Date.now() - startedAt) / 1000);
    }
    clockTimer = setInterval(tickElapsed, 500);
    tickElapsed();

    function trimView() {
      if (buffer.length <= MAX_LINES) return;
      buffer = buffer.slice(-MAX_LINES);
      renderAll();
    }

    function renderAll() {
      const frag = document.createDocumentFragment();
      for (const line of buffer) {
        const span = document.createElement("span");
        span.className = logLevelClass(line);
        span.textContent = line;
        frag.appendChild(span);
        frag.appendChild(document.createTextNode("\n"));
      }
      view.replaceChildren(frag);
      if (stick) view.scrollTop = view.scrollHeight;
    }

    function appendLine(line) {
      const text = String(line || "").replace(/\r$/, "");
      if (!text) return;
      buffer.push(text);
      if (buffer.length > MAX_LINES) {
        trimView();
        return;
      }
      const span = document.createElement("span");
      span.className = logLevelClass(text);
      span.textContent = text;
      view.appendChild(span);
      view.appendChild(document.createTextNode("\n"));
      if (stick) view.scrollTop = view.scrollHeight;
    }

    function ingestText(text) {
      const incoming = String(text || "").split(/\r?\n/).filter((line) => line !== "");
      if (!incoming.length) return;
      const last = buffer[buffer.length - 1];
      const idx = last ? incoming.lastIndexOf(last) : -1;
      const extra = idx >= 0 ? incoming.slice(idx + 1) : incoming;
      extra.forEach(appendLine);
    }

    view.addEventListener("scroll", () => {
      stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
    }, { passive: true });

    function stopJournal() {
      if (journalRetry) {
        clearTimeout(journalRetry);
        journalRetry = 0;
      }
      if (journal) {
        journal.close();
        journal = null;
      }
    }

    function extrasFromHistory(lines) {
      const last = buffer[buffer.length - 1];
      if (last == null) return lines.slice();
      const idx = lines.lastIndexOf(last);
      if (idx >= 0) return lines.slice(idx + 1);
      const have = new Set(buffer.slice(-200));
      return lines.filter((line) => !have.has(line));
    }

    function startJournal() {
      if (closed || journal) return;
      journal = new EventSource(uiPath("logs/stream"));
      journal.addEventListener("log", (event) => {
        try {
          appendLine(JSON.parse(event.data).line || event.data);
        } catch {
          appendLine(event.data);
        }
      });
      journal.onopen = () => {
        api("logs/history?lines=120")
          .then((data) => {
            const lines = Array.isArray(data.lines) ? data.lines : [];
            extrasFromHistory(lines).forEach(appendLine);
          })
          .catch(() => {});
      };
      journal.onerror = () => {
        if (!journal || journal.readyState !== EventSource.CLOSED) return;
        journal.close();
        journal = null;
        if (closed || !busy) return;
        journalRetry = setTimeout(() => {
          journalRetry = 0;
          startJournal();
        }, 800);
      };
    }

    function stopUpdateLog() {
      if (updateTimer) {
        clearInterval(updateTimer);
        updateTimer = 0;
      }
    }

    async function pullUpdateLog() {
      try {
        const data = await api("update/log?lines=500");
        updateRunning = Boolean(data.running);
        const lines = Array.isArray(data.lines) ? data.lines : [];
        if (lines.length < updateSeen) updateSeen = 0;
        const extra = lines.slice(updateSeen);
        updateSeen = lines.length;
        extra.forEach(appendLine);
      } catch {
        /* API is down during restart; journal catch-up covers boot logs. */
      }
    }

    function startUpdateLog() {
      if (closed || updateTimer) return;
      pullUpdateLog();
      updateTimer = setInterval(pullUpdateLog, 800);
    }

    function looksReady(data) {
      if (!data || data.ok === false) return false;
      if (data.switching || data.restarting || data.busy) return false;
      const health = data.health || {};
      return Boolean(data.tabby_model || data.comfy_up || health.healthy);
    }

    function updateLooksFailed(text) {
      return /Git command failed|Failed to restart tabbyapi|did not become healthy|Update failed|Install failed/i.test(text);
    }

    function updateLooksDone(text) {
      return /Update finished\.|==> \[100%\] (Git update finished|API healthy|Finished)\b/i.test(
        text
      );
    }

    async function waitUntilReady(opts = {}) {
      const requireDown = Boolean(opts.requireDown);
      const watchUpdate = Boolean(opts.watchUpdate);
      let sawBusy = false;
      let sawUpdateRunning = false;
      let updateGoneAt = 0;
      let lastUptime = null;
      const started = Date.now();
      const deadline = started + (Number(opts.timeoutMs) || (watchUpdate ? 45 * 60 * 1000 : 20 * 60 * 1000));
      while (!closed) {
        if (Date.now() > deadline) {
          throw new Error("Timed out waiting for TabbyAPI. See the log above.");
        }
        try {
          const data = await api("status");
          paintGpuFromStatus(data);
          const name = (data && data.switch_target) || "";
          const up = Number(data && data.uptime_s);
          if (Number.isFinite(up)) {
            if (lastUptime != null && up + 5 < lastUptime) sawBusy = true;
            lastUptime = up;
          }
          if (data && (data.restarting || data.switching || data.busy)) {
            sawBusy = true;
            if (data.restarting) {
              noteEl.textContent = "Restarting the API…";
            } else {
              noteEl.textContent = name ? `Loading ${name}…` : "Loading…";
            }
          } else if (looksReady(data) && sawBusy) {
            paintGpuFromStatus(data);
            return data;
          } else if (
            looksReady(data) &&
            !requireDown &&
            !watchUpdate &&
            Date.now() - started > 2500
          ) {
            paintGpuFromStatus(data);
            return data;
          } else if (
            requireDown &&
            looksReady(data) &&
            updateLooksFailed(buffer.join("\n"))
          ) {
            throw new Error("The update failed before a restart. See the log above.");
          }
          if (watchUpdate) {
            const text = buffer.join("\n");
            if (updateRunning) {
              sawUpdateRunning = true;
              updateGoneAt = 0;
            } else if (sawUpdateRunning && !updateGoneAt) {
              updateGoneAt = Date.now();
            }
            if (updateLooksFailed(text) && !updateRunning) {
              throw new Error("The update failed. See the log above.");
            }
            if (looksReady(data) && updateLooksDone(text) && !updateRunning) {
              paintGpuFromStatus(data);
              return data;
            }
            if (
              looksReady(data) &&
              sawUpdateRunning &&
              !updateRunning &&
              updateGoneAt &&
              Date.now() - updateGoneAt > 2500
            ) {
              paintGpuFromStatus(data);
              return data;
            }
          }
        } catch (err) {
          if (err && /failed before a restart|The update failed|Timed out waiting/i.test(err.message || "")) throw err;
          sawBusy = true;
          if (window.TabbyUI && window.TabbyUI.paintApiDown) window.TabbyUI.paintApiDown(err);
          noteEl.textContent = "API is down. Waiting for it to come back…";
        }
        await sleep(1500);
      }
      return null;
    }

    function setActions(items) {
      actionsEl.replaceChildren();
      (items || []).forEach((item) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn";
        if (item.primary) btn.classList.add("primary");
        if (item.danger) btn.classList.add("danger");
        btn.textContent = item.label || "OK";
        btn.addEventListener("click", () => {
          if (typeof item.run === "function") item.run();
        });
        actionsEl.appendChild(btn);
      });
      const focusBtn =
        actionsEl.querySelector(".btn.primary, .btn.danger") || actionsEl.querySelector(".btn");
      if (focusBtn) focusBtn.focus();
    }

    function close() {
      if (closed) return;
      closed = true;
      busy = false;
      stopJournal();
      stopUpdateLog();
      if (clockTimer) clearInterval(clockTimer);
      document.removeEventListener("keydown", onKey);
      wrap.remove();
    }

    const onKey = (ev) => {
      if (ev.key !== "Escape") return;
      if (busy) {
        ev.preventDefault();
        return;
      }
      close();
    };
    wrap.addEventListener("click", (ev) => {
      if (ev.target !== wrap || busy) return;
      close();
    });
    document.addEventListener("keydown", onKey);

    Object.assign(handle, {
      setTitle(value) {
        wrap.querySelector("h2").textContent = value || "Working";
      },
      setNote(value) {
        noteEl.textContent = value || "";
      },
      setBusy(value) {
        busy = Boolean(value);
      },
      appendLine,
      ingestText,
      startJournal,
      stopJournal,
      startUpdateLog,
      stopUpdateLog,
      waitUntilReady,
      setActions,
      close,
      get busy() {
        return busy;
      },
      get closed() {
        return closed;
      },
    });
    return handle;
  }

  function paintGpuFromStatus(data) {
    window.TabbyUI && window.TabbyUI.paintGpuChip(data);
  }

  async function followRestart(modal) {
    const progress = modal || progressModal({
      title: "Restarting",
      note: "Restarting TabbyAPI. The UI will drop for about a minute.",
    });
    progress.setBusy(true);
    progress.setTitle("Restarting");
    progress.setNote("Restarting TabbyAPI. The UI will drop for about a minute.");
    progress.setActions([]);
    progress.startJournal();
    try {
      const result = await api("restart", { method: "POST", body: {} });
      if (result && result.message) progress.setNote(result.message);
      if (result && result.ok === false) {
        progress.setBusy(false);
        progress.stopJournal();
        throw new Error(result.message || "Could not start a restart.");
      }
    } catch (err) {
      if (err && /Could not start a restart/i.test(err.message || "")) throw err;
      progress.appendLine(String((err && err.message) || err));
    }
    await progress.waitUntilReady({ requireDown: true });
    progress.stopJournal();
    progress.stopUpdateLog();
    progress.setTitle("API is back");
    progress.setNote("TabbyAPI is healthy again. Reload the UI if pages look stale.");
    progress.setBusy(false);
    return progress;
  }

  function confirmModal({ title, text, yes = "Restart", no = "Skip", other = "" } = {}) {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "dialog-modal";
      wrap.setAttribute("role", "dialog");
      wrap.setAttribute("aria-modal", "true");
      wrap.innerHTML =
        '<div class="dialog-card">' +
        "<h2></h2>" +
        '<pre class="dialog-text"></pre>' +
        '<div class="dialog-actions">' +
        '<button type="button" class="btn dialog-no"></button>' +
        (other ? '<button type="button" class="btn dialog-other"></button>' : "") +
        '<button type="button" class="btn danger dialog-yes"></button>' +
        "</div></div>";
      wrap.querySelector("h2").textContent = title || "Confirm";
      wrap.querySelector(".dialog-text").textContent = text || "";
      wrap.querySelector(".dialog-no").textContent = no;
      wrap.querySelector(".dialog-yes").textContent = yes;
      const otherBtn = wrap.querySelector(".dialog-other");
      if (otherBtn) otherBtn.textContent = other;
      const finish = (value) => {
        document.removeEventListener("keydown", onKey);
        wrap.remove();
        resolve(value);
      };
      const onKey = (ev) => {
        if (ev.key === "Escape") finish(false);
      };
      wrap.querySelector(".dialog-no").addEventListener("click", () => finish(false));
      wrap.querySelector(".dialog-yes").addEventListener("click", () => finish(true));
      if (otherBtn) otherBtn.addEventListener("click", () => finish("other"));
      wrap.addEventListener("click", (ev) => {
        if (ev.target === wrap) finish(false);
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(wrap);
      wrap.querySelector(".dialog-yes").focus();
    });
  }

  function promptModal({
    title,
    text,
    label,
    yes = "Save",
    no = "Cancel",
    type = "text",
    minlength = 0,
    autocomplete = "off",
    value = "",
    placeholder = "",
  } = {}) {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "dialog-modal";
      wrap.setAttribute("role", "dialog");
      wrap.setAttribute("aria-modal", "true");
      wrap.innerHTML =
        '<div class="dialog-card">' +
        "<h2></h2>" +
        '<p class="dialog-text" hidden></p>' +
        '<form class="dialog-form">' +
        '<label><span class="dialog-label"></span><input class="dialog-input" /></label>' +
        '<div class="dialog-actions">' +
        '<button type="button" class="btn dialog-no"></button>' +
        '<button type="submit" class="btn primary dialog-yes"></button>' +
        "</div></form></div>";
      wrap.querySelector("h2").textContent = title || "Enter a value";
      const textEl = wrap.querySelector(".dialog-text");
      if (text) {
        textEl.hidden = false;
        textEl.textContent = text;
      }
      wrap.querySelector(".dialog-label").textContent = label || "";
      const input = wrap.querySelector(".dialog-input");
      input.type = type === "password" ? "password" : "text";
      input.autocomplete = autocomplete || "off";
      if (placeholder) input.placeholder = placeholder;
      if (value) input.value = String(value);
      const min = Number(minlength) || 0;
      if (min) {
        input.minLength = min;
        input.required = true;
      }
      wrap.querySelector(".dialog-no").textContent = no;
      wrap.querySelector(".dialog-yes").textContent = yes;
      const finish = (value) => {
        document.removeEventListener("keydown", onKey);
        wrap.remove();
        resolve(value);
      };
      const onKey = (ev) => {
        if (ev.key === "Escape") {
          ev.preventDefault();
          finish(null);
        }
      };
      wrap.querySelector(".dialog-no").addEventListener("click", () => finish(null));
      wrap.querySelector("form").addEventListener("submit", (ev) => {
        ev.preventDefault();
        const value = String(input.value || "");
        if (min && value.length < min) return;
        if (!value) return;
        finish(value);
      });
      wrap.addEventListener("click", (ev) => {
        if (ev.target === wrap) finish(null);
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(wrap);
      input.focus();
      if (input.value) input.select();
    });
  }

  function shortcutRow(label, keysHtml) {
    return "<li><span>" + label + '</span><span class="shortcut-keys">' + keysHtml + "</span></li>";
  }

  function showShortcuts() {
    const existing = document.querySelector(".dialog-modal[data-shortcuts]");
    if (existing) {
      existing.querySelector(".dialog-yes") && existing.querySelector(".dialog-yes").focus();
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "dialog-modal";
      wrap.dataset.shortcuts = "1";
      wrap.setAttribute("role", "dialog");
      wrap.setAttribute("aria-modal", "true");
      wrap.innerHTML =
        '<div class="dialog-card">' +
        "<h2>Keyboard shortcuts</h2>" +
        '<div class="dialog-body"><div class="shortcuts">' +
        '<section><h3>Composer</h3><ul class="shortcuts-list">' +
        shortcutRow("Send", "<kbd>Enter</kbd>") +
        shortcutRow("New line", "<kbd>Shift</kbd>+<kbd>Enter</kbd>") +
        shortcutRow("Slash commands", "<kbd>/</kbd>") +
        shortcutRow("Attach a project file", "<kbd>@</kbd>") +
        shortcutRow("Recall sent text", "<kbd>↑</kbd><kbd>↓</kbd>") +
        "</ul></section>" +
        '<section><h3>Chats</h3><ul class="shortcuts-list">' +
        shortcutRow("Cycle chats", "<kbd>Tab</kbd>") +
        shortcutRow("Search chats", "<kbd>Ctrl</kbd>+<kbd>K</kbd>") +
        shortcutRow("Find in chat", "<kbd>Ctrl</kbd>+<kbd>F</kbd>") +
        shortcutRow("New chat", "<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>O</kbd>") +
        shortcutRow("Stop or close", "<kbd>Esc</kbd>") +
        "</ul></section>" +
        '<section><h3>Workspace</h3><ul class="shortcuts-list">' +
        shortcutRow("Jump to file", "<kbd>Ctrl</kbd>+<kbd>P</kbd>") +
        shortcutRow("Find in files", "<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>F</kbd>") +
        shortcutRow("Save file", "<kbd>Ctrl</kbd>+<kbd>S</kbd>") +
        shortcutRow("Split editor", "<kbd>Ctrl</kbd>+<kbd>\\</kbd>") +
        shortcutRow("Go to definition", "<kbd>F12</kbd>") +
        shortcutRow("Find references", "<kbd>Shift</kbd>+<kbd>F12</kbd>") +
        shortcutRow("Format document", "<kbd>Shift</kbd>+<kbd>Alt</kbd>+<kbd>F</kbd>") +
        shortcutRow("Cycle Agent / Ask / Plan", "<kbd>Shift</kbd>+<kbd>Tab</kbd>") +
        shortcutRow("Toggle terminal", "<kbd>Ctrl</kbd>+<kbd>`</kbd>") +
        shortcutRow("New preview tab", "<kbd>Ctrl</kbd>+<kbd>T</kbd>") +
        shortcutRow("Close preview tab", "<kbd>Ctrl</kbd>+<kbd>W</kbd>") +
        shortcutRow("Focus preview address", "<kbd>Ctrl</kbd>+<kbd>L</kbd>") +
        shortcutRow("More actions", "<kbd>Right-click</kbd>") +
        "</ul></section>" +
        "</div></div>" +
        '<div class="dialog-actions">' +
        '<button type="button" class="btn primary dialog-yes">Close</button>' +
        "</div></div>";
      const finish = () => {
        document.removeEventListener("keydown", onKey);
        wrap.remove();
        resolve();
      };
      const onKey = (ev) => {
        if (ev.key === "Escape") {
          ev.preventDefault();
          finish();
        }
      };
      wrap.querySelector(".dialog-yes").addEventListener("click", finish);
      wrap.addEventListener("click", (ev) => {
        if (ev.target === wrap) finish();
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(wrap);
      wrap.querySelector(".dialog-yes").focus();
    });
  }

  const THEME_BOOT = window.TABBY_THEME_BOOT || {};
  const THEME_FAMILIES = THEME_BOOT.FAMILIES || ["midnight", "ember", "glacier", "moss", "contrast"];
  const THEME_LABELS = THEME_BOOT.LABELS || {
    midnight: "Midnight",
    ember: "Ember",
    glacier: "Glacier",
    moss: "Moss",
    contrast: "Contrast",
  };
  const THEME_MODES = THEME_BOOT.MODES || ["dark", "light", "system"];
  const MODE_LABELS = THEME_BOOT.MODE_LABELS || { dark: "Dark", light: "Light", system: "System" };
  const PREFS_SAVE_MS = 400;

  function clonePrefs(raw) {
    try {
      return JSON.parse(JSON.stringify(raw && typeof raw === "object" ? raw : {}));
    } catch {
      return {};
    }
  }

  let prefsState = clonePrefs(window.TABBY_UI_PREFS);
  window.TABBY_UI_PREFS = prefsState;
  let prefsTimer = 0;
  let prefsDirty = false;
  let prefsFlushing = Promise.resolve();

  function mergePrefsPatch(base, patch) {
    const next = Object.assign({}, base, patch);
    if (patch && patch.layout) {
      next.layout = Object.assign({}, base.layout || {}, patch.layout);
    }
    if (patch && patch.samplers) {
      next.samplers = Object.assign({}, base.samplers || {}, patch.samplers);
    }
    if (patch && patch.wsOpen) {
      next.wsOpen = Object.assign({}, base.wsOpen || {}, patch.wsOpen);
    }
    return next;
  }

  function suspendPersistence() {
    window.TABBY_SUSPEND_PERSIST = true;
    prefsDirty = false;
    if (prefsTimer) {
      clearTimeout(prefsTimer);
      prefsTimer = 0;
    }
  }

  function flushPrefs() {
    if (window.TABBY_SUSPEND_PERSIST) return prefsFlushing;
    if (prefsTimer) {
      clearTimeout(prefsTimer);
      prefsTimer = 0;
    }
    if (!prefsDirty) return prefsFlushing;
    prefsDirty = false;
    const snapshot = clonePrefs(prefsState);
    prefsFlushing = prefsFlushing
      .then(() => api("prefs", { method: "PUT", body: snapshot }))
      .catch(() => {});
    return prefsFlushing;
  }

  function schedulePrefsSave() {
    if (prefsTimer) clearTimeout(prefsTimer);
    prefsTimer = setTimeout(() => {
      prefsTimer = 0;
      flushPrefs();
    }, PREFS_SAVE_MS);
  }

  function patchPrefs(patch) {
    prefsState = mergePrefsPatch(prefsState, patch);
    window.TABBY_UI_PREFS = prefsState;
    if (window.TabbyUI) window.TabbyUI.prefs = prefsState;
    prefsDirty = true;
    schedulePrefsSave();
    return prefsState;
  }

  window.addEventListener("pagehide", () => {
    flushPrefs();
  });

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function getTheme() {
    const v = prefsState.theme;
    if (THEME_FAMILIES.includes(v)) return v;
    return THEME_BOOT.family ? THEME_BOOT.family() : "midnight";
  }

  function getMode() {
    const v = prefsState.mode;
    if (THEME_MODES.includes(v)) return v;
    return THEME_BOOT.mode ? THEME_BOOT.mode() : "dark";
  }

  function resolvedTheme() {
    return THEME_BOOT.resolved ? THEME_BOOT.resolved() : `${getTheme()}-dark`;
  }

  function applyTheme() {
    const id = resolvedTheme();
    if (THEME_BOOT.apply) THEME_BOOT.apply(id);
    else document.documentElement.setAttribute("data-theme", id);
    document.dispatchEvent(
      new CustomEvent("tabby-theme-change", {
        detail: { theme: getTheme(), mode: getMode(), resolved: id },
      })
    );
  }

  function setTheme(family) {
    if (!THEME_FAMILIES.includes(family)) return;
    patchPrefs({ theme: family });
    applyTheme();
  }

  function setMode(mode) {
    if (!THEME_MODES.includes(mode)) return;
    patchPrefs({ mode });
    applyTheme();
  }

  const ZOOM_MIN = 75;
  const ZOOM_MAX = 150;
  const ZOOM_STEP = 5;
  const ZOOM_DEFAULT = 100;

  function clampZoom(n) {
    const stepped = Math.round(Number(n) / ZOOM_STEP) * ZOOM_STEP;
    if (!Number.isFinite(stepped)) return ZOOM_DEFAULT;
    return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, stepped));
  }

  function getZoom() {
    return clampZoom(prefsState.zoom);
  }

  function applyZoom(pct) {
    const value = clampZoom(pct == null ? getZoom() : pct);
    document.documentElement.style.setProperty("--ui-zoom", String(value / 100));
    return value;
  }

  function zoomFactor() {
    return getZoom() / 100;
  }

  function setZoom(pct) {
    const value = applyZoom(pct);
    patchPrefs({ zoom: value });
    window.dispatchEvent(new Event("tabby-zoom-change"));
    window.dispatchEvent(new Event("resize"));
    return value;
  }

  applyZoom();

  function followUpSuggestions(text) {
    const clean = String(text || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/[#*_`]/g, "")
      .replace(/\s+/g, " ")
      .trim();
    const out = [];
    const seen = new Set();
    const add = (value) => {
      const item = String(value || "").replace(/\s+/g, " ").trim();
      if (item.length < 8 || item.length > 90) return;
      const key = item.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(item);
    };
    const questions = clean.match(/[^.!?]{12,80}\?/g) || [];
    questions.slice(0, 2).forEach((item) => add(item));
    add("Explain that in more detail");
    add("Give a concrete example");
    if (/\b(how|steps|install|setup|config)/i.test(clean)) add("Walk through the steps");
    return out.slice(0, 3);
  }

  if (window.matchMedia) {
    const schemeQuery = matchMedia("(prefers-color-scheme: dark)");
    const onScheme = () => {
      if (getMode() === "system") applyTheme();
    };
    if (schemeQuery.addEventListener) schemeQuery.addEventListener("change", onScheme);
    else if (schemeQuery.addListener) schemeQuery.addListener(onScheme);
  }

  window.TabbyUI = {
    base: uiBase,
    path: uiPath,
    redirectToLogin,
    resolveUiUrl,
    api,
    $,
    $all,
    copyText,
    selectedText,
    selectionIn,
    editCut,
    editPaste,
    inputMenuItems,
    hideContextMenu,
    showContextMenu,
    confirmModal,
    promptModal,
    progressModal,
    followRestart,
    showShortcuts,
    escapeHtml,
    looksLikeHtml,
    httpErrorMessage,
    readNdjson,
    formatBytes,
    formatDuration,
    formatAssistantContent,
    followUpSuggestions,
    renderMarkdown,
    cssVar,
    getTheme,
    setTheme,
    getMode,
    setMode,
    getZoom,
    zoomFactor,
    setZoom,
    applyZoom,
    resolvedTheme,
    applyTheme,
    prefs: prefsState,
    patchPrefs,
    flushPrefs,
    suspendPersistence,
    THEME_FAMILIES,
    THEME_LABELS,
    THEME_MODES,
    MODE_LABELS,
    lastGpuStatus: null,
    formatTokenCount(count) {
      const n = Math.max(0, Number(count) || 0);
      if (n >= 1000000 - 500) return `${(n / 1000000).toFixed(1)}M`;
      if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
      return String(Math.round(n));
    },
    paintContextUsage(info) {
      const chip = document.getElementById("context-chip");
      if (!chip) return;
      const max = Number(info && info.max);
      if (!info || info.hide || !(max > 0)) {
        chip.hidden = true;
        chip.classList.remove("warning", "error");
        chip.setAttribute("aria-expanded", "false");
        const panel = document.getElementById("context-menu-panel");
        if (panel) panel.hidden = true;
        return;
      }
      const used = Math.max(0, Number(info.used) || 0);
      const estimated = Boolean(info.estimated);
      const pct = (used / max) * 100;
      const clamped = Math.max(0, Math.min(100, pct));
      const circ = 2 * Math.PI * 14;
      const arc = chip.querySelector(".progress-arc");
      if (arc) arc.setAttribute("stroke-dashoffset", String(circ - (clamped / 100) * circ));
      chip.classList.toggle("warning", pct >= 75 && pct < 90);
      chip.classList.toggle("error", pct >= 90);
      const rounded = Math.min(100, Math.round(pct));
      const pctLabel = document.getElementById("context-chip-pct");
      if (pctLabel) pctLabel.textContent = `${rounded}%`;
      const tokens = `${this.formatTokenCount(used)} / ${this.formatTokenCount(max)} tokens`;
      chip.title = estimated ? `${tokens} (estimated)` : tokens;
      chip.setAttribute("aria-label", `Context window usage: ${rounded}%`);
      chip.hidden = false;
      const tokenEl = document.getElementById("context-usage-tokens");
      if (tokenEl) tokenEl.textContent = tokens;
      const pctEl = document.getElementById("context-usage-pct");
      if (pctEl) pctEl.textContent = `${rounded}%`;
      const note = document.getElementById("context-usage-note");
      if (note) note.hidden = pct < 75;
      const est = document.getElementById("context-usage-est");
      if (est) est.hidden = !estimated;
    },
    paintGpuChip(data) {
      const chip = document.getElementById("gpu-chip");
      if (!chip) return;
      const labelEl = document.getElementById("gpu-chip-label") || chip;
      if (!data) {
        this.paintApiDown();
        return;
      }
      this.lastGpuStatus = data;
      const queue = data.stack_queue || {};
      const holderTitle = String(queue.hint || "The stack is being used")
        .replace(/\s*You are in a queue\.?/gi, "")
        .replace(/\s*You are number \d+\.?/gi, "")
        .replace(/\s*Your previous request is still running\.?/gi, "")
        .replace(/\s*Your request will wait\.?/gi, "")
        .trim();
      if (data.gpu_waiting) {
        const name = data.switch_target || data.profile || "model";
        const text = `WAITING · ${name}`;
        labelEl.textContent = text;
        chip.className = "chip warn is-busy";
        chip.title = holderTitle || `Waiting to switch to ${name}`;
        chip.setAttribute("aria-label", text);
        window.dispatchEvent(new CustomEvent("tabby-gpu-status", { detail: data }));
        return;
      }
      if (data.switching || data.restarting || data.busy) {
        const name = data.switch_target || data.profile || "model";
        const key = String(name).toLowerCase();
        const comfy = key === "comfy" || key === "flux";
        const text = data.restarting ? "RESTARTING" : `LOADING · ${name}`;
        labelEl.textContent = text;
        chip.className = "chip warn is-busy";
        chip.title = data.restarting
          ? "API is restarting"
          : comfy
            ? "Loading Comfy. Chat is paused until it is ready."
            : `Loading ${name}. Chat is paused until the model is ready.`;
        chip.setAttribute("aria-label", text);
        window.dispatchEvent(new CustomEvent("tabby-gpu-status", { detail: data }));
        return;
      }
      if (queue.busy && !queue.mine) {
        const kind = String(queue.kind || "chat");
        const kindLabel =
          kind === "image" ? "images" : kind === "code" ? "code" : kind === "gpu" ? "gpu" : "chat";
        const text = `IN USE · ${kindLabel}`;
        labelEl.textContent = text;
        chip.className = "chip warn";
        chip.title = holderTitle || "The stack is being used";
        chip.setAttribute("aria-label", text);
        window.dispatchEvent(new CustomEvent("tabby-gpu-status", { detail: data }));
        return;
      }
      const mode = data.gpu_mode || "gpu";
      const label = data.profile || data.tabby_model || "idle";
      const pretty = ((data.profile_labels || {})[data.profile] || "").trim();
      const text = `${String(mode).toUpperCase()} · ${label}`;
      labelEl.textContent = text;
      chip.className = "chip" + (mode === "llm" ? " ok" : " warn");
      const parts = ["Click to switch model"];
      if (pretty && pretty !== label) parts.unshift(pretty);
      if (data.tabby_model && data.tabby_model !== pretty) parts.push(data.tabby_model);
      chip.title = parts.join(" · ");
      chip.setAttribute("aria-label", `GPU and model: ${text}`);
      window.dispatchEvent(new CustomEvent("tabby-gpu-status", { detail: data }));
    },
    paintApiDown(err) {
      const chip = document.getElementById("gpu-chip");
      if (!chip) return;
      const labelEl = document.getElementById("gpu-chip-label") || chip;
      const raw = (err && err.message) || "";
      const prev = this.lastGpuStatus || {};
      const code = ((raw.match(/\((\d{3})\)/) || [])[1] || "").trim();
      const bounce = Boolean(
        prev.switching
          || prev.restarting
          || prev.busy
          || prev.gpu_waiting
          || (prev.profile && (code === "502" || code === "503" || code === "504"))
      );
      if (bounce) {
        const text = "RESTARTING";
        labelEl.textContent = text;
        chip.className = "chip warn is-busy";
        chip.title = raw || "API is restarting";
        chip.setAttribute("aria-label", text);
        this.lastGpuStatus = Object.assign({}, prev, {
          down: true,
          restarting: true,
          busy: true,
          switch_target: prev.switch_target || prev.profile || "llm",
        });
        window.dispatchEvent(new CustomEvent("tabby-gpu-status", { detail: this.lastGpuStatus }));
        return;
      }
      let detail = "reconnecting";
      if (code) detail = code;
      else if (/unreachable/i.test(raw)) detail = "unreachable";
      else if (/restart/i.test(raw)) detail = "restarting";
      const text = `DOWN · ${detail}`;
      labelEl.textContent = text;
      chip.className = "chip bad";
      chip.title = raw || "API unavailable";
      chip.setAttribute("aria-label", text);
      this.lastGpuStatus = Object.assign({}, prev, { down: true });
      window.dispatchEvent(new CustomEvent("tabby-gpu-status", { detail: this.lastGpuStatus }));
    },
  };
})();
