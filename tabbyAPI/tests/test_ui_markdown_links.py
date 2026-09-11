"""Markdown link/attr sanitizing. Keep in sync with ui/static/utils.js."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

UTILS_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"


def _js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unterminated function {name}")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UtilsJsSanitizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = UTILS_JS.read_text(encoding="utf-8")
        cls.prelude = (
            'const window = { location: { href: "http://tabby.local/v1/ui/" } };\n'
            + _js_function(src, "escapeHtml")
            + "\n"
            + _js_function(src, "markdownHrefAllowed")
            + "\n"
        )

    def _eval(self, expr: str):
        return _run_node(self.prelude + f"console.log(JSON.stringify({expr}));")

    def _allowed(self, values: list[str]) -> list:
        calls = ",".join(f"markdownHrefAllowed({json.dumps(v)})" for v in values)
        return self._eval(f"[{calls}]")

    def test_escape_html_covers_attribute_breakouts(self):
        out = self._eval("escapeHtml(`<a href=\"x\" title='y'>&</a>`)")
        self.assertEqual(out, "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;")
        self.assertEqual(self._eval("escapeHtml(null)"), "")

    def test_href_blocks_non_http_schemes(self):
        blocked = [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            " javascript:alert(1)",
            "java\tscript:alert(1)",
            "java\nscript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox",
            "file:///etc/passwd",
            "ftp://host/x",
            "//evil.example/x",
        ]
        results = self._allowed(blocked)
        # protocol-relative http is allowed by design; everything else must be blocked
        self.assertEqual(results[:-1], [False] * (len(blocked) - 1))
        self.assertTrue(results[-1])

    def test_href_allows_http_and_safe_relative(self):
        allowed = [
            "https://example.com/x",
            "http://example.com/x?y=1",
            "/v1/ui/gallery/file/a.png",
            "/v1/images/generated-1.png",
            "images/logo.png",
            "docs/readme.md#top",
        ]
        self.assertEqual(self._allowed(allowed), [True] * len(allowed))

    def test_href_blocks_root_and_parent_paths(self):
        blocked = ["/etc/passwd", "/v1/ui", "../secret", "a/../b", "", "   "]
        self.assertEqual(self._allowed(blocked), [False] * len(blocked))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UtilsJsProxyPrefixTests(unittest.TestCase):
    def test_resolve_ui_url_rewrites_bare_v1_image_links(self):
        src = UTILS_JS.read_text(encoding="utf-8")
        start = src.index("function uiBase(")
        end = src.index("function apiUrl(")
        script = (
            "const window = { location: { "
            'href: "https://git.example.com/openai/v1/ui/", '
            'pathname: "/openai/v1/ui/", '
            'origin: "https://git.example.com" } };\n'
            "const MARKER = '/v1/ui';\n"
            + src[start:end]
            + "console.log(JSON.stringify({"
            "rel: resolveUiUrl('/v1/images/generated-1.png'),"
            "abs: resolveUiUrl('https://git.example.com/v1/images/generated-1.png'),"
            "keep: resolveUiUrl('https://git.example.com/openai/v1/images/generated-1.png'),"
            "ui: resolveUiUrl('/v1/ui/gallery/file/a.png')"
            "}));"
        )
        out = _run_node(script)
        self.assertEqual(out["rel"], "/openai/v1/images/generated-1.png")
        self.assertEqual(
            out["abs"], "https://git.example.com/openai/v1/images/generated-1.png"
        )
        self.assertEqual(
            out["keep"], "https://git.example.com/openai/v1/images/generated-1.png"
        )
        self.assertEqual(out["ui"], "/openai/v1/ui/gallery/file/a.png")


if __name__ == "__main__":
    unittest.main()
