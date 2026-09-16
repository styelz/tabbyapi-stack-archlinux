"""Code-mode preview tokens and storage shim."""

from __future__ import annotations

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

    def test_guess_media_type_webp(self):
        self.assertEqual(workspace.guess_media_type(Path("images/logo.webp")), "image/webp")
        self.assertEqual(workspace.guess_media_type(Path("images/hero.png")), "image/png")


if __name__ == "__main__":
    unittest.main()
