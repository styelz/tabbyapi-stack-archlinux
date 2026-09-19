"""Chat markdown fences. Keep in sync with ui/static/utils.js extractFences."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

UTILS_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"

OPEN_RE = re.compile(r"^(\s*)(`{3,}|~{3,})[ \t]*([\w+-]*)(.*)$")

SAMPLE = """\
Here is a simple, robust shell script to find all broken symbolic links on a specific drive.

### The Script: `find_broken_links.sh`

```bash
#!/bin/bash
TARGET_PATH="/"
find "$TARGET_PATH" -xtype l 2>/dev/null
```

### How to use it

1.  **Create the file:**
    ```bash
    nano find_broken_links.sh
    ```
    Paste the code above into the file and save it (Ctrl+O, Enter, Ctrl+X).

2.  **Make it executable:**
    ```bash
    chmod +x find_broken_links.sh
    ```

3.  **Run it:**
    ```bash
    ./find_broken_links.sh
    ```
"""


def _strip_indent(line: str, indent: str) -> str:
    if not indent:
        return line
    if line.startswith(indent):
        return line[len(indent) :]
    n = 0
    while n < len(indent) and n < len(line) and line[n] in " \t":
        n += 1
    return line[n:]


def extract_fences(raw: str) -> tuple[str, list[tuple[str, str]]]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    fences: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        open_m = OPEN_RE.match(lines[i])
        if not open_m:
            out.append(lines[i])
            i += 1
            continue
        indent, marker, lang, rest = (
            open_m.group(1),
            open_m.group(2),
            open_m.group(3) or "",
            open_m.group(4) or "",
        )
        fence_char = marker[0]
        close_same = re.search(
            r"`{3,}[ \t]*$" if fence_char == "`" else r"~{3,}[ \t]*$",
            rest,
        )
        if close_same and rest[: close_same.start()].strip():
            fences.append((lang, rest[: close_same.start()].strip()))
            out.append(f"{indent}@@CODE{len(fences) - 1}@@")
            i += 1
            continue
        if fence_char in rest:
            out.append(lines[i])
            i += 1
            continue
        body: list[str] = []
        i += 1
        close_re = re.compile(rf"^\s*{re.escape(fence_char)}{{{len(marker)},}}[ \t]*$")
        while i < len(lines) and not close_re.match(lines[i]):
            body.append(_strip_indent(lines[i], indent))
            i += 1
        fences.append((lang, "\n".join(body)))
        out.append(f"{indent}@@CODE{len(fences) - 1}@@")
        if i < len(lines):
            i += 1
    return "\n".join(out), fences


class UiMarkdownFenceTests(unittest.TestCase):
    def test_utils_js_allows_indented_fences(self):
        src = UTILS_JS.read_text()
        self.assertIn(r"^(\s*)(`{3,}|~{3,})[ \t]*([\w+-]*)(.*)$", src)
        self.assertIn("stripFenceIndent", src)
        self.assertIn("isFenceToken(raw)", src)
        self.assertIn("extractImplicitCode", src)
        self.assertIn("lineCodeKind", src)
        self.assertIn("CHAT_HIGHLIGHT_LIMIT", src)
        self.assertNotIn(r"^```([\w+-]*)[ \t]*$", src)

    def test_column_zero_fence_still_extracts(self):
        text, fences = extract_fences("```bash\necho hi\n```\n")
        self.assertEqual(len(fences), 1)
        self.assertEqual(fences[0], ("bash", "echo hi"))
        self.assertIn("@@CODE0@@", text)
        self.assertNotIn("```", text)

    def test_numbered_how_to_fences_are_code_blocks(self):
        text, fences = extract_fences(SAMPLE)
        self.assertEqual(len(fences), 4)
        self.assertEqual([lang for lang, _ in fences], ["bash"] * 4)
        self.assertEqual(fences[1][1], "nano find_broken_links.sh")
        self.assertEqual(fences[2][1], "chmod +x find_broken_links.sh")
        self.assertEqual(fences[3][1], "./find_broken_links.sh")
        self.assertNotIn("```", text)
        self.assertIn("    @@CODE1@@", text)
        self.assertIn("Paste the code above", text)

    def test_one_line_fence(self):
        _, fences = extract_fences("```bash chmod +x find_broken_links.sh ```")
        self.assertEqual(fences, [("bash", "chmod +x find_broken_links.sh")])

    def test_crlf_openers(self):
        _, fences = extract_fences("```python\r\nprint(1)\r\n```\r\n")
        self.assertEqual(fences, [("python", "print(1)")])


HIGHLIGHT_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "highlight.js"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ImplicitCodeFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        utils = UTILS_JS.read_text(encoding="utf-8")
        highlight = HIGHLIGHT_JS.read_text(encoding="utf-8")
        start = utils.index("const CHAT_HIGHLIGHT_LIMIT")
        end = utils.index("\n  function autolink(")
        cls.prelude = "\n".join(
            [
                "globalThis.window = { TabbyHighlight: null };",
                highlight,
                "function escapeHtml(value) {",
                "  return String(value ?? '')",
                "    .replaceAll('&', '&amp;')",
                "    .replaceAll('<', '&lt;')",
                "    .replaceAll('>', '&gt;')",
                "    .replaceAll('\"', '&quot;');",
                "}",
                utils[start:end],
            ]
        )

    def _extract(self, text: str) -> dict:
        script = (
            self.prelude
            + "\nconst extracted = extractFences("
            + json.dumps(text)
            + ");\n"
            + "console.log(JSON.stringify({ text: extracted.text, n: extracted.fences.length, html: extracted.fences.join('\\n') }));\n"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(proc.stderr or proc.stdout or "node failed")
        return json.loads(proc.stdout)

    def test_unfenced_html_becomes_highlighted_block(self):
        sample = (
            "Okay — here is the page.\n"
            "\n"
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<title>Chrome & Thunder</title>\n"
            "</head>\n"
            "<body>\n"
            "<nav id=\"navbar\"></nav>\n"
            "</body>\n"
            "</html>\n"
            "\n"
            "Wrote index.html\n"
        )
        out = self._extract(sample)
        self.assertIn("@@CODE0@@", out["text"])
        self.assertNotIn("<!DOCTYPE html>", out["text"])
        self.assertIn("Wrote index.html", out["text"])
        self.assertIn("class=\"md-code\"", out["html"])
        self.assertIn("language-html", out["html"])
        self.assertIn("tok-tag", out["html"])

    def test_unfenced_js_becomes_highlighted_block(self):
        sample = (
            "Render the grid at runtime:\n"
            "function renderCars(filter = 'All') {\n"
            "  const list = filter === 'All' ? cars : cars.filter(c => c.genre === filter);\n"
            "  grid.innerHTML = list.map(c => c.title).join('');\n"
            "}\n"
        )
        out = self._extract(sample)
        self.assertIn("@@CODE0@@", out["text"])
        self.assertIn("Render the grid at runtime:", out["text"])
        self.assertIn("tok-keyword", out["html"])

    def test_prose_is_not_swallowed(self):
        sample = (
            "Hero — full-screen background, sparkle stars, flowing clouds.\n"
            "Cars — grid of car cards with year and genre badge.\n"
            "Crews — grid of crew cards with emoji faces.\n"
            "Timeline — four items alternating left and right.\n"
        )
        out = self._extract(sample)
        self.assertEqual(out["n"], 0)
        self.assertIn("Hero — full-screen background", out["text"])

    def test_keyword_prose_is_not_code(self):
        sample = (
            "if you want a darker theme, switch it in Settings.\n"
            "for the navbar, add a glow on hover.\n"
            "return to the homepage after login.\n"
            "try a second page for the gallery.\n"
        )
        out = self._extract(sample)
        self.assertEqual(out["n"], 0)
        self.assertIn("if you want a darker theme", out["text"])
        self.assertIn("for the navbar, add a glow", out["text"])
        self.assertIn("return to the homepage", out["text"])

    def test_colon_labels_are_not_css(self):
        sample = (
            "hero: full-screen background with stars\n"
            "nav: sticky bar with links\n"
            "footer: copyright and socials\n"
        )
        out = self._extract(sample)
        self.assertEqual(out["n"], 0)
        self.assertIn("hero: full-screen background", out["text"])
        self.assertIn("nav: sticky bar with links", out["text"])

    def test_arrow_prose_is_not_js(self):
        sample = (
            "Home => Cars => Crews\n"
            "Use ${name} in the title later.\n"
            "Then switch to the gallery view.\n"
        )
        out = self._extract(sample)
        self.assertEqual(out["n"], 0)
        self.assertIn("Home => Cars => Crews", out["text"])
        self.assertIn("Use ${name} in the title later.", out["text"])

    def test_unfenced_css_still_highlights(self):
        sample = (
            "Theme tokens:\n"
            ".hero {\n"
            "  background: #0b1020;\n"
            "  color: #fff;\n"
            "}\n"
        )
        out = self._extract(sample)
        self.assertIn("@@CODE0@@", out["text"])
        self.assertIn("Theme tokens:", out["text"])
        self.assertIn("language-css", out["html"])

    def test_explicit_fences_still_win(self):
        out = self._extract("```bash\necho hi\n```\n")
        self.assertEqual(out["n"], 1)
        self.assertIn("language-shell", out["html"])
        self.assertIn("tok-fn", out["html"])
        self.assertIn(">echo</span> hi", out["html"])


if __name__ == "__main__":
    unittest.main()
