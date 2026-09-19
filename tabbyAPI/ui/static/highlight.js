(() => {
  const JS_KW = new Set([
    "async", "await", "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "export", "extends", "finally", "for", "from",
    "function", "if", "import", "in", "instanceof", "let", "new", "of", "return", "static",
    "super", "switch", "this", "throw", "try", "typeof", "var", "void", "while", "with",
    "yield", "true", "false", "null", "undefined", "NaN", "Infinity", "get", "set",
  ]);
  const PHP_KW = new Set([
    "abstract", "and", "array", "as", "break", "callable", "case", "catch", "class", "clone",
    "const", "continue", "declare", "default", "die", "do", "echo", "else", "elseif", "empty",
    "enddeclare", "endfor", "endforeach", "endif", "endswitch", "endwhile", "eval", "exit",
    "extends", "final", "finally", "fn", "for", "foreach", "function", "global", "goto", "if",
    "implements", "include", "include_once", "instanceof", "insteadof", "interface", "isset",
    "list", "match", "namespace", "new", "or", "print", "private", "protected", "public",
    "readonly", "require", "require_once", "return", "static", "switch", "throw", "trait",
    "try", "unset", "use", "var", "while", "xor", "yield", "true", "false", "null", "self",
    "parent",
  ]);
  const CSS_KW = new Set([
    "important", "and", "or", "not", "only", "from", "to", "through", "in",
  ]);
  const SH_KW = new Set([
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case",
    "esac", "in", "function", "select", "time", "coproc", "return", "exit", "break",
    "continue", "shift", "local", "export", "unset", "readonly", "declare", "typeset",
    "alias", "unalias", "source", "true", "false",
  ]);
  const SH_CMD = new Set([
    "echo", "printf", "cd", "pwd", "ls", "cat", "grep", "sed", "awk", "find", "xargs",
    "mkdir", "rm", "cp", "mv", "chmod", "chown", "ln", "touch", "head", "tail", "sort",
    "uniq", "wc", "tr", "cut", "tee", "curl", "wget", "ssh", "scp", "tar", "gzip", "jq",
    "git", "npm", "npx", "node", "python", "pip", "sudo", "apt", "pacman", "systemctl",
    "docker", "test", "read", "eval", "exec", "trap", "wait", "kill", "sleep", "date",
    "basename", "dirname", "realpath", "which", "command", "type", "hash", "set", "unset",
  ]);
  const PY_KW = new Set([
    "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del",
    "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in",
    "is", "lambda", "match", "case", "nonlocal", "not", "or", "pass", "raise", "return",
    "try", "while", "with", "yield", "True", "False", "None", "self", "cls",
  ]);
  const PY_BUILTIN = new Set([
    "abs", "all", "any", "bool", "bytes", "callable", "dict", "dir", "enumerate", "eval",
    "filter", "float", "format", "frozenset", "getattr", "hasattr", "hash", "id", "input",
    "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "object", "open", "ord", "print", "range", "repr", "reversed", "round", "set",
    "setattr", "sorted", "str", "sum", "super", "tuple", "type", "vars", "zip",
  ]);

  const LANGS = {
    js: "javascript",
    javascript: "javascript",
    jsx: "javascript",
    mjs: "javascript",
    cjs: "javascript",
    node: "javascript",
    ts: "javascript",
    tsx: "javascript",
    mts: "javascript",
    cts: "javascript",
    typescript: "javascript",
    html: "html",
    htm: "html",
    xml: "html",
    svg: "html",
    css: "css",
    php: "php",
    sh: "shell",
    bash: "shell",
    zsh: "shell",
    shell: "shell",
    console: "shell",
    terminal: "shell",
    json: "json",
    jsonc: "json",
    py: "python",
    python: "python",
    yml: "yaml",
    yaml: "yaml",
    toml: "ini",
    ini: "ini",
    cfg: "ini",
    conf: "ini",
    md: "markdown",
    markdown: "markdown",
    tabby: "tabby",
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function span(type, text) {
    if (!text) return "";
    const escaped = escapeHtml(text);
    return type ? `<span class="tok-${type}">${escaped}</span>` : escaped;
  }

  function skipWs(src, i, end) {
    while (i < end && /\s/.test(src[i])) i += 1;
    return i;
  }

  function isIdentChar(ch, extra, first) {
    if (!ch) return false;
    if (first) {
      if (/[A-Za-z_$]/.test(ch)) return true;
    } else if (/[\w$]/.test(ch)) {
      return true;
    }
    return extra.includes(ch);
  }

  function readIdent(src, i, end, extra = "") {
    if (!isIdentChar(src[i], extra, true)) return "";
    let j = i + 1;
    while (j < end && isIdentChar(src[j], extra, false)) j += 1;
    return src.slice(i, j);
  }

  function quotedEnd(src, i, end, q, { newlines = false } = {}) {
    let j = i + 1;
    while (j < end) {
      if (src[j] === "\\") {
        j += 2;
        continue;
      }
      if (src[j] === q) return j + 1;
      if (!newlines && src[j] === "\n") return j;
      j += 1;
    }
    return end;
  }

  function canStartRegex(prev) {
    if (!prev) return true;
    if (/^(?:return|throw|case|else|new|typeof|void|delete|in|of|await|yield|instanceof)$/.test(prev)) {
      return true;
    }
    return /[({\[=,;:!&|?+\-*%<>~^]$/.test(prev);
  }

  function highlightJs(src, start = 0, end = src.length, stopAt = "") {
    const out = [];
    let i = start;
    let prev = "";
    let brace = 0;
    while (i < end) {
      const ch = src[i];
      if (/\s/.test(ch)) {
        const j = skipWs(src, i, end);
        out.push(escapeHtml(src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("//", i)) {
        const nl = src.indexOf("\n", i);
        const j = nl < 0 || nl > end ? end : nl;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("/*", i)) {
        const close = src.indexOf("*/", i + 2);
        const j = close < 0 || close + 2 > end ? end : close + 2;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === "'" || ch === '"') {
        const j = quotedEnd(src, i, end, ch);
        out.push(span("string", src.slice(i, j)));
        prev = "string";
        i = j;
        continue;
      }
      if (ch === "`") {
        out.push(span("string", "`"));
        i += 1;
        while (i < end) {
          if (src[i] === "\\") {
            out.push(span("string", src.slice(i, Math.min(i + 2, end))));
            i += 2;
            continue;
          }
          if (src[i] === "`") {
            out.push(span("string", "`"));
            i += 1;
            break;
          }
          if (src.startsWith("${", i)) {
            out.push(span("punct", "${"));
            i += 2;
            const inner = highlightJs(src, i, end, "}");
            out.push(inner.html);
            i = inner.index;
            if (i < end && src[i] === "}") {
              out.push(span("punct", "}"));
              i += 1;
            }
            continue;
          }
          let j = i + 1;
          while (j < end && src[j] !== "`" && src[j] !== "\\" && !src.startsWith("${", j)) j += 1;
          out.push(span("string", src.slice(i, j)));
          i = j;
        }
        prev = "string";
        continue;
      }
      if (ch === "/" && canStartRegex(prev)) {
        let j = i + 1;
        while (j < end) {
          if (src[j] === "\\") {
            j += 2;
            continue;
          }
          if (src[j] === "[") {
            j += 1;
            while (j < end && src[j] !== "]") {
              if (src[j] === "\\") j += 1;
              j += 1;
            }
            j += 1;
            continue;
          }
          if (src[j] === "/") {
            j += 1;
            break;
          }
          if (src[j] === "\n") break;
          j += 1;
        }
        while (j < end && /[gimsuy]/.test(src[j])) j += 1;
        out.push(span("string", src.slice(i, j)));
        prev = "regex";
        i = j;
        continue;
      }
      if (/\d/.test(ch) || (ch === "." && /\d/.test(src[i + 1] || ""))) {
        const m = /^(0x[\da-fA-F]+|0b[01]+|0o[0-7]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)/i.exec(src.slice(i, end));
        const tok = m ? m[0] : ch;
        out.push(span("number", tok));
        prev = "number";
        i += tok.length;
        continue;
      }
      if (/[A-Za-z_$]/.test(ch)) {
        const word = readIdent(src, i, end);
        let type = "";
        if (JS_KW.has(word)) type = "keyword";
        else {
          let k = skipWs(src, i + word.length, end);
          if (src[k] === "(") type = "fn";
        }
        out.push(span(type, word));
        prev = word;
        i += word.length;
        continue;
      }
      if (stopAt === "}" && ch === "}" && brace === 0) break;
      if (ch === "{") brace += 1;
      if (ch === "}" && brace) brace -= 1;
      out.push(span("punct", ch));
      prev = ch;
      i += 1;
    }
    return { html: out.join(""), index: i };
  }

  function highlightCss(src, start = 0, end = src.length) {
    const out = [];
    let i = start;
    let depth = 0;
    let expectValue = false;
    while (i < end) {
      const ch = src[i];
      if (/\s/.test(ch)) {
        const j = skipWs(src, i, end);
        out.push(escapeHtml(src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("/*", i)) {
        const close = src.indexOf("*/", i + 2);
        const j = close < 0 || close + 2 > end ? end : close + 2;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === "'" || ch === '"') {
        const j = quotedEnd(src, i, end, ch);
        out.push(span("string", src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === "#" && /[\da-fA-F]{3,8}(?![\w-])/.test(src.slice(i + 1, end))) {
        const m = src.slice(i + 1, end).match(/^[\da-fA-F]{3,8}/);
        const j = i + 1 + (m ? m[0].length : 0);
        out.push(span("number", src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === "@") {
        const word = readIdent(src, i + 1, end, "-");
        out.push(span("keyword", `@${word}`));
        i += 1 + word.length;
        continue;
      }
      if (ch === "!") {
        const word = readIdent(src, i + 1, end);
        if (word.toLowerCase() === "important") {
          out.push(span("keyword", src.slice(i, i + 1 + word.length)));
          i += 1 + word.length;
          continue;
        }
      }
      if (/\d/.test(ch) || (ch === "." && /\d/.test(src[i + 1] || ""))) {
        const m = /^-?\d*\.?\d+(?:e[+-]?\d+)?(?:%|[a-z]+)?/i.exec(src.slice(i, end));
        const tok = m ? m[0] : ch;
        out.push(span("number", tok));
        i += tok.length;
        continue;
      }
      if (/[A-Za-z_-]/.test(ch)) {
        const word = readIdent(src, i, end, "-");
        let k = skipWs(src, i + word.length, end);
        let type = "";
        if (CSS_KW.has(word.toLowerCase())) type = "keyword";
        else if (expectValue && src[k] === "(") type = "fn";
        else if (expectValue) type = "";
        else if (depth > 0 && src[k] === ":" && src[k + 1] !== ":") type = "attr";
        else if (src[k] === "(") type = "fn";
        else type = "tag";
        out.push(span(type, word));
        i += word.length;
        continue;
      }
      if (ch === "{") {
        depth += 1;
        expectValue = false;
      } else if (ch === "}") {
        if (depth) depth -= 1;
        expectValue = false;
      } else if (ch === ";" || ch === ",") {
        expectValue = false;
      } else if (ch === ":" && src[i + 1] !== ":") {
        expectValue = depth > 0;
      }
      out.push(span("punct", ch));
      i += 1;
    }
    return out.join("");
  }

  function highlightAttrValue(src, i, end) {
    if (src[i] === "'" || src[i] === '"') {
      const q = src[i];
      const j = quotedEnd(src, i, end, q, { newlines: true });
      return { html: span("string", src.slice(i, j)), index: j };
    }
    let j = i;
    while (j < end && !/[\s>=]/.test(src[j])) j += 1;
    return { html: span("string", src.slice(i, j)), index: j };
  }

  function highlightTag(src, i, end) {
    const out = [span("punct", "<")];
    i += 1;
    let closing = false;
    let selfClose = false;
    if (src[i] === "/" || src[i] === "!") {
      closing = src[i] === "/";
      out[0] = span("punct", `<${src[i]}`);
      i += 1;
    }
    const name = readIdent(src, i, end, "-:");
    out.push(span("tag", name));
    i += name.length;
    while (i < end) {
      if (/\s/.test(src[i])) {
        const j = skipWs(src, i, end);
        out.push(escapeHtml(src.slice(i, j)));
        i = j;
        continue;
      }
      if (src[i] === ">") {
        out.push(span("punct", ">"));
        i += 1;
        break;
      }
      if (src.startsWith("/>", i)) {
        out.push(span("punct", "/>"));
        selfClose = true;
        i += 2;
        break;
      }
      const attr = readIdent(src, i, end, "-:");
      if (!attr) {
        out.push(span("punct", src[i]));
        i += 1;
        continue;
      }
      out.push(span("attr", attr));
      i += attr.length;
      let k = i;
      if (/\s/.test(src[k] || "")) {
        const j = skipWs(src, k, end);
        out.push(escapeHtml(src.slice(k, j)));
        k = j;
      }
      if (src[k] === "=") {
        out.push(span("punct", "="));
        k += 1;
        if (/\s/.test(src[k] || "")) {
          const j = skipWs(src, k, end);
          out.push(escapeHtml(src.slice(k, j)));
          k = j;
        }
        const val = highlightAttrValue(src, k, end);
        out.push(val.html);
        i = val.index;
      } else {
        i = k;
      }
    }
    return { html: out.join(""), index: i, name: name.toLowerCase(), closing, selfClose };
  }

  function highlightHtml(src, start = 0, end = src.length) {
    const out = [];
    let i = start;
    while (i < end) {
      if (src.startsWith("<!--", i)) {
        const close = src.indexOf("-->", i + 4);
        const j = close < 0 || close + 3 > end ? end : close + 3;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("<?", i)) {
        const close = src.indexOf("?>", i + 2);
        const j = close < 0 || close + 2 > end ? end : close + 2;
        const openEnd = /^<\?php/i.test(src.slice(i))
          ? i + 5
          : src.startsWith("<?=", i)
            ? i + 3
            : i + 2;
        out.push(span("keyword", src.slice(i, openEnd)));
        const closeAt = close < 0 || close + 2 > end ? end : close;
        out.push(highlightPhpInner(src.slice(openEnd, closeAt)));
        if (closeAt < end) out.push(span("keyword", "?>"));
        i = j;
        continue;
      }
      if (src[i] === "<") {
        const tag = highlightTag(src, i, end);
        out.push(tag.html);
        i = tag.index;
        if (!tag.closing && !tag.selfClose && (tag.name === "script" || tag.name === "style")) {
          const closeRe = tag.name === "script" ? /<\/script\s*>/i : /<\/style\s*>/i;
          const found = src.slice(i, end).search(closeRe);
          const bodyEnd = found < 0 ? end : i + found;
          out.push(tag.name === "script" ? highlightJs(src, i, bodyEnd).html : highlightCss(src, i, bodyEnd));
          i = bodyEnd;
        }
        continue;
      }
      const next = src.indexOf("<", i);
      const j = next < 0 || next > end ? end : next;
      out.push(escapeHtml(src.slice(i, j)));
      i = j;
    }
    return out.join("");
  }

  function highlightPhpInner(src) {
    const out = [];
    const end = src.length;
    let i = 0;
    while (i < end) {
      const ch = src[i];
      if (/\s/.test(ch)) {
        const j = skipWs(src, i, end);
        out.push(escapeHtml(src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("//", i) || ch === "#") {
        const nl = src.indexOf("\n", i);
        const j = nl < 0 ? end : nl;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("/*", i)) {
        const close = src.indexOf("*/", i + 2);
        const j = close < 0 ? end : close + 2;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("<<<", i)) {
        const rest = src.slice(i);
        const mark = /^(<<<[-]?\s*['"]?(\w+)['"]?\s*\n)/.exec(rest);
        if (mark) {
          const closer = `\n${mark[2]}`;
          const found = src.indexOf(closer, i + mark[1].length);
          const j = found < 0 ? end : found + closer.length;
          out.push(span("string", src.slice(i, j)));
          i = j;
          continue;
        }
      }
      if (ch === "'" || ch === '"') {
        const j = quotedEnd(src, i, end, ch, { newlines: true });
        if (ch === '"') {
          out.push(highlightInterpolated(src.slice(i, j), /\$[A-Za-z_][\w]*|\$\{[^}]+\}/g));
        } else {
          out.push(span("string", src.slice(i, j)));
        }
        i = j;
        continue;
      }
      if (ch === "$") {
        let j = i + 1;
        if (src[j] === "$") j += 1;
        const name = readIdent(src, j, end);
        j += name.length;
        out.push(span("var", src.slice(i, j)));
        i = j;
        continue;
      }
      if (/\d/.test(ch)) {
        const m = /^(0x[\da-fA-F]+|\d+(?:\.\d+)?)/.exec(src.slice(i));
        const tok = m ? m[0] : ch;
        out.push(span("number", tok));
        i += tok.length;
        continue;
      }
      if (/[A-Za-z_]/.test(ch)) {
        const word = readIdent(src, i, end);
        let type = "";
        if (PHP_KW.has(word.toLowerCase())) type = "keyword";
        else {
          const k = skipWs(src, i + word.length, end);
          if (src[k] === "(") type = "fn";
        }
        out.push(span(type, word));
        i += word.length;
        continue;
      }
      out.push(span("punct", ch));
      i += 1;
    }
    return out.join("");
  }

  function highlightInterpolated(text, varRe) {
    const out = [];
    let last = 0;
    varRe.lastIndex = 0;
    let m = varRe.exec(text);
    while (m) {
      if (m.index > last) out.push(span("string", text.slice(last, m.index)));
      out.push(span("var", m[0]));
      last = m.index + m[0].length;
      m = varRe.exec(text);
    }
    if (last < text.length) out.push(span("string", text.slice(last)));
    return out.join("");
  }

  function highlightPhp(src) {
    if (/<\?(?:php|=)?/i.test(src) || /<\s*(?:html|div|span|p|a|form|input|script|style|!doctype)\b/i.test(src)) {
      return highlightHtml(src);
    }
    return highlightPhpInner(src);
  }

  function highlightShell(src) {
    const out = [];
    const end = src.length;
    let i = 0;
    let fresh = true;
    while (i < end) {
      const ch = src[i];
      if (ch === "\n") {
        out.push("\n");
        i += 1;
        fresh = true;
        continue;
      }
      if (ch === " " || ch === "\t") {
        let j = i + 1;
        while (j < end && (src[j] === " " || src[j] === "\t")) j += 1;
        out.push(src.slice(i, j));
        i = j;
        continue;
      }
      if (ch === "#" && (fresh || /[\s;|&]/.test(src[i - 1] || ""))) {
        const nl = src.indexOf("\n", i);
        const j = nl < 0 ? end : nl;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === "'") {
        const j = (() => {
          let k = i + 1;
          while (k < end && src[k] !== "'") k += 1;
          return k < end ? k + 1 : end;
        })();
        out.push(span("string", src.slice(i, j)));
        fresh = false;
        i = j;
        continue;
      }
      if (ch === '"') {
        const j = quotedEnd(src, i, end, '"', { newlines: true });
        out.push(highlightInterpolated(src.slice(i, j), /\$[A-Za-z_][\w]*|\$\{[^}]+\}|\$\d+/g));
        fresh = false;
        i = j;
        continue;
      }
      if (ch === "$") {
        let j = i + 1;
        if (src[j] === "{") {
          const close = src.indexOf("}", j + 1);
          j = close < 0 ? end : close + 1;
        } else if (src[j] === "(") {
          const close = src.indexOf(")", j + 1);
          j = close < 0 ? end : close + 1;
        } else if (/\d/.test(src[j] || "") || /[#@*!?]/.test(src[j] || "")) {
          j += 1;
        } else {
          j += readIdent(src, j, end).length;
        }
        out.push(span("var", src.slice(i, j)));
        fresh = false;
        i = j;
        continue;
      }
      if (ch === "-" && (src[i + 1] === "-" || /[A-Za-z?]/.test(src[i + 1] || ""))) {
        let j = i + 1;
        while (j < end && /[A-Za-z0-9_-]/.test(src[j])) j += 1;
        out.push(span("attr", src.slice(i, j)));
        fresh = false;
        i = j;
        continue;
      }
      if (/[A-Za-z_./~]/.test(ch)) {
        let j = i + 1;
        while (j < end && !/[\s;|&<>(){}[\]`'"]/.test(src[j])) j += 1;
        const word = src.slice(i, j);
        let type = "";
        if (SH_KW.has(word)) type = "keyword";
        else if (fresh && SH_CMD.has(word.split("/").pop())) type = "fn";
        else if (fresh) type = "fn";
        else if (SH_CMD.has(word)) type = "fn";
        out.push(span(type, word));
        fresh = false;
        i = j;
        continue;
      }
      if (/[;|&]/.test(ch)) {
        let j = i;
        while (j < end && /[;|&]/.test(src[j])) j += 1;
        out.push(span("punct", src.slice(i, j)));
        fresh = true;
        i = j;
        continue;
      }
      out.push(span("punct", ch));
      fresh = false;
      i += 1;
    }
    return out.join("");
  }

  function highlightJson(src) {
    const out = [];
    const end = src.length;
    let i = 0;
    while (i < end) {
      const ch = src[i];
      if (/\s/.test(ch)) {
        const j = skipWs(src, i, end);
        out.push(escapeHtml(src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("//", i)) {
        const nl = src.indexOf("\n", i);
        const j = nl < 0 ? end : nl;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (src.startsWith("/*", i)) {
        const close = src.indexOf("*/", i + 2);
        const j = close < 0 ? end : close + 2;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === '"') {
        const j = quotedEnd(src, i, end, '"', { newlines: true });
        // A string that a colon follows is an object key, not a value.
        const after = skipWs(src, j, end);
        out.push(span(src[after] === ":" ? "attr" : "string", src.slice(i, j)));
        i = j;
        continue;
      }
      if (/\d/.test(ch) || (ch === "-" && /\d/.test(src[i + 1] || ""))) {
        const m = /^-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/i.exec(src.slice(i, end));
        const tok = m ? m[0] : ch;
        out.push(span("number", tok));
        i += tok.length;
        continue;
      }
      if (/[A-Za-z_]/.test(ch)) {
        const word = readIdent(src, i, end);
        out.push(span(/^(?:true|false|null)$/.test(word) ? "keyword" : "", word));
        i += word.length;
        continue;
      }
      out.push(span("punct", ch));
      i += 1;
    }
    return out.join("");
  }

  /** End index of a Python string literal at `i`, or 0 when there is none. */
  function pyStringEnd(src, i, end) {
    let j = i;
    let prefix = 0;
    while (j < end && prefix < 3 && /[rRbBuUfF]/.test(src[j])) {
      j += 1;
      prefix += 1;
    }
    const q = src[j];
    if (q !== "'" && q !== '"') return 0;
    const fence = q.repeat(3);
    if (src.startsWith(fence, j)) {
      const close = src.indexOf(fence, j + 3);
      return close < 0 ? end : Math.min(close + 3, end);
    }
    return quotedEnd(src, j, end, q);
  }

  function highlightPython(src) {
    const out = [];
    const end = src.length;
    let i = 0;
    let prev = "";
    while (i < end) {
      const ch = src[i];
      if (/\s/.test(ch)) {
        const j = skipWs(src, i, end);
        out.push(escapeHtml(src.slice(i, j)));
        i = j;
        continue;
      }
      if (ch === "#") {
        const nl = src.indexOf("\n", i);
        const j = nl < 0 ? end : nl;
        out.push(span("comment", src.slice(i, j)));
        i = j;
        continue;
      }
      const strEnd = pyStringEnd(src, i, end);
      if (strEnd) {
        out.push(span("string", src.slice(i, strEnd)));
        prev = "string";
        i = strEnd;
        continue;
      }
      if (ch === "@" && /[A-Za-z_]/.test(src[i + 1] || "")) {
        const word = readIdent(src, i + 1, end, ".");
        out.push(span("attr", `@${word}`));
        prev = "decorator";
        i += 1 + word.length;
        continue;
      }
      if (/\d/.test(ch) || (ch === "." && /\d/.test(src[i + 1] || ""))) {
        const m = /^(0x[\da-fA-F_]+|0b[01_]+|0o[0-7_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:e[+-]?\d+)?j?)/i.exec(
          src.slice(i, end)
        );
        const tok = m ? m[0] : ch;
        out.push(span("number", tok));
        prev = "number";
        i += tok.length;
        continue;
      }
      if (/[A-Za-z_]/.test(ch)) {
        const word = readIdent(src, i, end);
        let type = "";
        if (PY_KW.has(word)) type = "keyword";
        else if (prev === "def" || prev === "class") type = "fn";
        else if (PY_BUILTIN.has(word)) type = "fn";
        else if (src[skipWs(src, i + word.length, end)] === "(") type = "fn";
        out.push(span(type, word));
        prev = word;
        i += word.length;
        continue;
      }
      out.push(span("punct", ch));
      prev = ch;
      i += 1;
    }
    return out.join("");
  }

  function scalarType(text) {
    if (/^(['"]).*\1$/s.test(text)) return "string";
    if (/^-?\d+(?:\.\d+)?$/.test(text)) return "number";
    if (/^(?:true|false|null|yes|no|on|off|~)$/i.test(text)) return "keyword";
    return "";
  }

  function highlightScalar(raw) {
    const text = String(raw);
    const trimmed = text.trim();
    if (!trimmed) return escapeHtml(text);
    const lead = text.slice(0, text.indexOf(trimmed));
    const tail = text.slice(lead.length + trimmed.length);
    return escapeHtml(lead) + span(scalarType(trimmed), trimmed) + escapeHtml(tail);
  }

  function highlightYamlLine(line) {
    const indent = /^\s*/.exec(line)[0];
    let rest = line.slice(indent.length);
    if (!rest) return escapeHtml(line);
    if (rest.startsWith("#")) return escapeHtml(indent) + span("comment", rest);
    let out = escapeHtml(indent);
    const dash = /^-(?:\s+|$)/.exec(rest);
    if (dash) {
      out += span("punct", "-") + escapeHtml(dash[0].slice(1));
      rest = rest.slice(dash[0].length);
    }
    const key = /^([^:#]+):(\s|$)/.exec(rest);
    if (key) {
      out += span("attr", key[1]) + span("punct", ":") + escapeHtml(key[2]);
      rest = rest.slice(key[0].length);
    }
    // Only " #" starts a comment; a bare # can live inside a URL fragment.
    const hash = rest.indexOf(" #");
    if (hash >= 0) return out + highlightScalar(rest.slice(0, hash)) + span("comment", rest.slice(hash));
    return out + highlightScalar(rest);
  }

  function highlightYaml(src) {
    return src.split("\n").map(highlightYamlLine).join("\n");
  }

  function highlightIni(src) {
    return src
      .split("\n")
      .map((line) => {
        const trimmed = line.trim();
        if (!trimmed) return escapeHtml(line);
        if (/^[#;]/.test(trimmed)) return span("comment", line);
        if (/^\[.*\]$/.test(trimmed)) return span("tag", line);
        const eq = line.indexOf("=");
        if (eq < 0) return escapeHtml(line);
        return (
          span("attr", line.slice(0, eq)) + span("punct", "=") + highlightScalar(line.slice(eq + 1))
        );
      })
      .join("\n");
  }

  const MD_INLINE = /(`+)([^`]*?)\1|(\*\*|__)([^]+?)\3|(\[[^\]]*\])(\([^)]*\))/g;

  function highlightMarkdownInline(line) {
    const bullet = /^(\s*)([-*+]|\d+[.)])(\s+)/.exec(line);
    let out = "";
    let rest = line;
    if (bullet) {
      out += escapeHtml(bullet[1]) + span("punct", bullet[2]) + escapeHtml(bullet[3]);
      rest = line.slice(bullet[0].length);
    }
    let last = 0;
    MD_INLINE.lastIndex = 0;
    let m = MD_INLINE.exec(rest);
    while (m) {
      if (m.index > last) out += escapeHtml(rest.slice(last, m.index));
      if (m[1]) out += span("string", m[0]);
      else if (m[3]) out += span("keyword", m[0]);
      else out += span("fn", m[5]) + span("attr", m[6]);
      last = m.index + m[0].length;
      m = MD_INLINE.exec(rest);
    }
    return out + escapeHtml(rest.slice(last));
  }

  function highlightMarkdown(src) {
    const out = [];
    let fence = "";
    src.split("\n").forEach((line) => {
      const open = /^\s*(```+|~~~+)(.*)$/.exec(line);
      if (fence) {
        if (open && line.trim().startsWith(fence)) {
          fence = "";
          out.push(span("keyword", line));
        } else {
          out.push(span("string", line));
        }
        return;
      }
      if (open) {
        fence = open[1];
        out.push(span("keyword", line.slice(0, line.length - open[2].length)) + span("attr", open[2]));
        return;
      }
      if (/^\s{0,3}#{1,6}\s/.test(line) || /^[=-]{3,}\s*$/.test(line)) {
        out.push(span("tag", line));
        return;
      }
      if (/^\s{0,3}>/.test(line)) {
        out.push(span("comment", line));
        return;
      }
      out.push(highlightMarkdownInline(line));
    });
    return out.join("\n");
  }

  function highlightTabby(src) {
    return src
      .split("\n")
      .map((line) => {
        const commentAt = line.indexOf("#");
        const command = commentAt >= 0 ? line.slice(0, commentAt).trimEnd() : line;
        const comment = commentAt >= 0 ? line.slice(commentAt) : "";
        let html = "";
        const switchMatch = /^(switch)(\s+to)(\s+)([\w.-]+)$/.exec(command);
        if (switchMatch) {
          html =
            span("keyword", switchMatch[1]) +
            span("punct", switchMatch[2]) +
            escapeHtml(switchMatch[3]) +
            span("string", switchMatch[4]);
        } else if (/^list models$/.test(command)) {
          html = span("keyword", "list") + escapeHtml(" ") + span("string", "models");
        } else if (/^(?:help|restart)$/.test(command)) {
          html = span("keyword", command);
        } else if (/^qwen-image:/.test(command)) {
          html = span("tag", "qwen-image:") + escapeHtml(command.slice("qwen-image:".length));
        } else {
          const first = /^(\S+)(.*)$/.exec(command);
          html = first ? span("fn", first[1]) + escapeHtml(first[2]) : "";
        }
        if (comment) html += (command ? escapeHtml("  ") : "") + span("comment", comment);
        return html;
      })
      .join("\n");
  }

  function normalizeLang(lang) {
    const key = String(lang || "").trim().toLowerCase().replace(/^language-/, "");
    return LANGS[key] || "";
  }

  function pathLanguage(path) {
    const name = String(path || "").split("/").pop() || "";
    const dot = name.lastIndexOf(".");
    return dot > 0 ? normalizeLang(name.slice(dot + 1)) : "";
  }

  function guessLanguage(code) {
    const sample = String(code || "").slice(0, 6000);
    if (/^\s*<!DOCTYPE/i.test(sample) || /^\s*<html\b/i.test(sample)) return "html";
    const html = (sample.match(/<\/?[a-zA-Z][\w:-]*\b/g) || []).length;
    const js = (sample.match(/\b(?:const|let|var|function|document\.|addEventListener|=>)\b/g) || []).length;
    const css = (sample.match(/(?:^|\n)\s*(?:--|[\w-]+)\s*:[^;\n]+;|(?:^|\n)\s*[.#@][\w-].*\{/g) || []).length;
    const py = (sample.match(/\b(?:def |elif |import |from .+ import|None|self\.)\b/g) || []).length;
    const sh = (sample.match(/^(?:#!\/bin\/|echo |npm |git |sudo )/m) || []).length;
    const scores = [
      ["html", html],
      ["javascript", js],
      ["css", css],
      ["python", py],
      ["shell", sh],
    ];
    let best = "";
    let n = 0;
    scores.forEach(([name, count]) => {
      if (count > n) {
        n = count;
        best = name;
      }
    });
    if (n >= 2) return best;
    if (html >= 1 && sample.includes("</")) return "html";
    return "";
  }

  function highlight(lang, code) {
    const src = String(code ?? "");
    const kind = normalizeLang(lang);
    if (!kind) return escapeHtml(src);
    try {
      if (kind === "javascript") return highlightJs(src).html;
      if (kind === "css") return highlightCss(src);
      if (kind === "html") return highlightHtml(src);
      if (kind === "php") return highlightPhp(src);
      if (kind === "shell") return highlightShell(src);
      if (kind === "json") return highlightJson(src);
      if (kind === "python") return highlightPython(src);
      if (kind === "yaml") return highlightYaml(src);
      if (kind === "ini") return highlightIni(src);
      if (kind === "markdown") return highlightMarkdown(src);
      if (kind === "tabby") return highlightTabby(src);
    } catch (_err) {
      return escapeHtml(src);
    }
    return escapeHtml(src);
  }

  window.TabbyHighlight = { highlight, language: normalizeLang, pathLanguage, guessLanguage };
})();
