"""Code-mode preview tokens and storage shim."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from ui import preview, workspace


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        workspace.set_workspaces_dir(Path(self._tmp.name))
        preview._tokens.clear()

    def tearDown(self):
        preview._tokens.clear()
        workspace.set_workspaces_dir(None)
        self._tmp.cleanup()

    def test_mint_reuses_live_token(self):
        first = preview.mint("u", "c")
        second = preview.mint("u", "c")
        self.assertEqual(first, second)
        self.assertEqual(preview.resolve(first), ("u", "c"))

    def test_resolve_expires_old_token(self):
        token = preview.mint("u", "c")
        preview._tokens[token]["created_at"] = time.time() - preview.TOKEN_TTL_S - 1
        self.assertIsNone(preview.resolve(token))

    def test_storage_round_trip_and_drop(self):
        preview.save_storage("u", "c", {"theme": "dark"})
        self.assertEqual(preview.load_storage("u", "c"), {"theme": "dark"})
        preview.drop_storage("u", "c")
        self.assertEqual(preview.load_storage("u", "c"), {})

    def test_injects_storage_shim_once(self):
        html = "<html><head></head><body></body></html>"
        out = preview.inject_storage_shim(html, {"k": "v"}, "__tabby_storage")
        self.assertIn("data-tabby-preview-storage", out)
        self.assertIn("localStorage", out)
        self.assertIn("get:function(){return mem;}", out)
        self.assertIn("Window.prototype", out)
        again = preview.inject_storage_shim(out, {"k": "v"}, "__tabby_storage")
        self.assertEqual(out.count("data-tabby-preview-storage"), 1)
        self.assertEqual(again, out)

    def test_spa_fallback_rels_for_hash_routes(self):
        self.assertEqual(preview.spa_fallback_rels("movies"), ["movies/index.html", "index.html"])
        self.assertEqual(preview.spa_fallback_rels("index.html"), [])
        self.assertEqual(preview.spa_fallback_rels("js/app.js"), [])
        self.assertEqual(preview.spa_fallback_rels("about/"), [])

    def test_preview_csp_blocks_extension_scripts(self):
        csp = preview.SANDBOX_CSP
        self.assertIn("script-src", csp)
        self.assertIn("https:", csp)
        self.assertNotIn("chrome-extension", csp)
        self.assertNotIn("moz-extension", csp)
        self.assertNotIn("allow-same-origin", csp)
        embed = preview.sandbox_csp(allow_same_origin=True)
        self.assertIn("allow-same-origin", embed)
        self.assertTrue(
            preview.preview_embed_allows_storage(
                {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin"}
            )
        )
        self.assertFalse(
            preview.preview_embed_allows_storage(
                {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none"}
            )
        )

    def test_injects_screenshot_capture_once(self):
        html = "<html><head></head><body><h1>Hi</h1></body></html>"
        out = preview.inject_browser_shim(html)
        self.assertIsInstance(out, str)
        self.assertIn("data-tabby-preview-screenshot", out)
        self.assertIn("captureScreenshot", out)
        self.assertIn("kind==='screenshot'", out)
        again = preview.inject_browser_shim(out)
        self.assertEqual(out.count("data-tabby-preview-screenshot"), 1)
        self.assertEqual(again, out)

    def test_html_preview_bytes_include_screenshot_shim(self):
        page = workspace.workspace_root("u", "c") / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html><body>Hi</body></html>", encoding="utf-8")
        raw = preview.html_preview_bytes(
            page, username="u", chat_id="c", persist_url="__tabby_storage"
        ).decode("utf-8")
        self.assertIn("data-tabby-preview-screenshot", raw)
        self.assertIn("captureScreenshot", raw)

    def test_guess_media_type_webp(self):
        self.assertEqual(workspace.guess_media_type(Path("images/logo.webp")), "image/webp")
        self.assertEqual(workspace.guess_media_type(Path("images/hero.png")), "image/png")


def _chrome_bin() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return ""


class PreviewScreenshotCaptureTests(unittest.TestCase):
    def test_shim_captures_a_jpeg_in_headless_chrome(self):
        chrome = _chrome_bin()
        if not chrome:
            self.skipTest("chrome is not installed")
        html = preview.inject_browser_shim(
            "<!doctype html><html><head><meta charset='utf-8'></head>"
            "<body style='margin:0;background:#c44536;color:#fff'>"
            "<h1 style='padding:24px;font:32px sans-serif'>Preview shot</h1>"
            "<script>"
            "window.addEventListener('message',function(ev){"
            "var d=ev.data;"
            "if(!d||d.source!=='tabby-preview'||d.kind!=='screenshot')return;"
            "document.body.setAttribute('data-shot',d.ok?'ok':'fail');"
            "document.body.setAttribute('data-mime',String(d.dataUrl||'').slice(0,22));"
            "document.title=d.ok?'shot-ok':('shot-fail:'+d.error);"
            "});"
            "window.addEventListener('load',function(){"
            "window.postMessage({source:'tabby-preview-host',kind:'screenshot',id:'t1'},'*');"
            "});"
            "</script></body></html>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "shot.html"
            page.write_text(html, encoding="utf-8")
            completed = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--window-size=800,600",
                    "--virtual-time-budget=8000",
                    "--dump-dom",
                    page.as_uri(),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        blob = completed.stdout or ""
        self.assertIn(
            'data-shot="ok"',
            blob,
            completed.stderr[-500:] if completed.stderr else blob[-500:],
        )
        self.assertIn("data:image/jpeg", blob)


if __name__ == "__main__":
    unittest.main()
