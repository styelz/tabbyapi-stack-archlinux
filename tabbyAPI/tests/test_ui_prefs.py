"""Per-user UI prefs stay on the server, not in the browser."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui import prefs
from ui.prefs import inject_index_prefs, normalize_prefs, prefs_js_literal

STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "static"


class PrefsNormalizeTests(unittest.TestCase):
    def test_defaults(self):
        out = normalize_prefs(None)
        self.assertEqual(out["theme"], "midnight")
        self.assertEqual(out["mode"], "dark")
        self.assertEqual(out["zoom"], 100)
        self.assertEqual(out["codeAgent"], "agent")
        self.assertEqual(out["samplers"]["temperature"], None)
        self.assertEqual(out["layout"]["filesOpen"], True)
        self.assertEqual(out["layout"]["gitOpen"], False)
        self.assertEqual(out["layout"]["filesFr"], [2.0, 1.0, 1.0, 1.0])
        self.assertEqual(out["wsOpen"], {})
        self.assertEqual(out["extraFolders"], [])

    def test_clamps_and_filters(self):
        out = normalize_prefs(
            {
                "theme": "ember",
                "mode": "system",
                "zoom": 137,
                "codeAgent": "plan",
                "samplers": {"temperature": 9, "max_tokens": 8},
                "layout": {
                    "sidebarHidden": True,
                    "sidebarW": 900,
                    "filesFr": "2,0.5,1",
                    "composeH": 40,
                    "gitOpen": 1,
                },
                "wsOpen": {"w1": True, "": False, "x" * 120: True},
                "extraFolders": [" Work ", "Work", "", "Nope"],
            }
        )
        self.assertEqual(out["theme"], "ember")
        self.assertEqual(out["mode"], "system")
        self.assertEqual(out["zoom"], 135)
        self.assertEqual(out["codeAgent"], "plan")
        self.assertEqual(out["samplers"]["temperature"], 2.0)
        self.assertEqual(out["samplers"]["max_tokens"], 16.0)
        self.assertTrue(out["layout"]["sidebarHidden"])
        self.assertEqual(out["layout"]["sidebarW"], 520)
        self.assertEqual(out["layout"]["filesFr"][1], 1.0)
        self.assertEqual(out["layout"]["composeH"], 56)
        self.assertTrue(out["layout"]["gitOpen"])
        self.assertEqual(out["wsOpen"]["w1"], True)
        self.assertNotIn("", out["wsOpen"])
        self.assertEqual(out["extraFolders"], ["Work", "Nope"])

    def test_unknown_theme_falls_back(self):
        out = normalize_prefs({"theme": "neon", "mode": "sepia", "codeAgent": "wizard"})
        self.assertEqual(out["theme"], "midnight")
        self.assertEqual(out["mode"], "dark")
        self.assertEqual(out["codeAgent"], "agent")


class PrefsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        prefs.set_prefs_dir(Path(self.tmp.name))

    def tearDown(self):
        prefs.set_prefs_dir(None)
        self.tmp.cleanup()

    def test_isolation(self):
        prefs.save_prefs("alice", {"theme": "ember", "zoom": 120})
        alice = prefs.load_prefs("alice")
        bob = prefs.load_prefs("bob")
        self.assertEqual(alice["theme"], "ember")
        self.assertEqual(alice["zoom"], 120)
        self.assertEqual(bob["theme"], "midnight")
        self.assertEqual(bob["zoom"], 100)
        prefs.delete_prefs("alice")
        self.assertEqual(prefs.load_prefs("alice")["theme"], "midnight")

    def test_round_trip(self):
        saved = prefs.save_prefs(
            "u",
            {
                "theme": "glacier",
                "mode": "light",
                "layout": {"filesOpen": False, "gitOpen": True},
                "extraFolders": ["Client"],
            },
        )
        loaded = prefs.load_prefs("u")
        self.assertEqual(loaded["theme"], saved["theme"])
        self.assertEqual(loaded["mode"], "light")
        self.assertFalse(loaded["layout"]["filesOpen"])
        self.assertTrue(loaded["layout"]["gitOpen"])
        self.assertEqual(loaded["extraFolders"], ["Client"])


class PrefsIndexInjectTests(unittest.TestCase):
    def test_router_wires_prefs_and_index(self):
        router = (Path(__file__).resolve().parents[1] / "ui" / "router.py").read_text(encoding="utf-8")
        self.assertIn('@router.get("/prefs"', router)
        self.assertIn('@router.put("/prefs"', router)
        self.assertIn("index_page_html(user)", router)
        self.assertIn("delete_prefs(name)", router)
        self.assertNotIn('file_response("index.html")', router)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn(prefs.PREFS_BOOT_MARK, html)
        self.assertIn("window.TABBY_UI_EPOCH = null;", html)
        self.assertIn("dropPrefixed(localStorage", html)
        self.assertIn("tabby-ui-epoch", html)
        self.assertNotIn("localStorage.getItem(\"tabby-ui-chat", html)
        router_src = router
        self.assertIn("_private_json(load_prefs(_user))", router_src)
        self.assertIn('payload["epoch"] = load_epoch()', router_src)

    def test_inject_replaces_mark(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        out = inject_index_prefs(html, {"theme": "ember", "zoom": 110})
        self.assertNotIn(prefs.PREFS_BOOT_MARK, out)
        self.assertIn('"theme":"ember"', out)
        self.assertIn('"zoom":110', out)
        self.assertNotIn("</script>", prefs_js_literal({"theme": "ember"}))

    def test_index_page_html_uses_saved_prefs(self):
        from ui import epoch

        tmp = tempfile.TemporaryDirectory()
        prefs.set_prefs_dir(Path(tmp.name))
        epoch.set_epoch_path(Path(tmp.name) / "ui_epoch")
        try:
            prefs.save_prefs("alice", {"theme": "moss", "mode": "light"})
            html = prefs.index_page_html("alice")
            self.assertIn('"theme":"moss"', html)
            self.assertIn('"mode":"light"', html)
            self.assertIn("window.TABBY_UI_EPOCH = ", html)
            self.assertNotIn("window.TABBY_UI_EPOCH = null;", html)
        finally:
            epoch.set_epoch_path(None)
            prefs.set_prefs_dir(None)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
