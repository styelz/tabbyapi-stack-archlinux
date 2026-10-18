"""A page whose HTML, CSS, and JS disagree is reported on the write result."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui import workspace


def code_agent_src() -> Path:
    return Path(__file__).resolve().parents[1] / "ui" / "code_agent.py"

_HTML = """<!DOCTYPE html>
<html><head><link rel="stylesheet" href="styles.css"></head>
<body>
<nav class="nav-content logo-wrap" id="navbar">
  <button class="theme-btn burger" id="burgerBtn"></button>
  <ul class="nav-links" id="navMenu"></ul>
</nav>
<section class="hero-section" id="hero">
  <img src="images/header.webp" alt="">
  <div class="hero-stats"><span class="stat stat-num"></span></div>
</section>
<div class="car-grid" id="carGrid"></div>
<div class="crew-grid" id="crewGrid"></div>
<button class="back-top" id="backTop"></button>
<script src="main.js"></script>
</body></html>
"""

_CSS = """
.nav-container { display: flex; }
.nav-brand { display: flex; }
.theme-toggle { border: 0; }
.burger-btn { border: 0; }
.hero { min-height: 100vh; }
.stat-item { display: flex; }
.stat-number { font-size: 2rem; }
.cars-grid { display: grid; }
.crews-grid { display: grid; }
.back-to-top { position: fixed; }
.nav-actions { display: flex; }
.navbar { position: fixed; }
"""

_JS = """
document.getElementById('carsGrid').innerHTML = '';
document.getElementById('crewsGrid').innerHTML = '';
document.getElementById('backToTop').addEventListener('click', () => {});
document.getElementById('navLinks').classList.toggle('open');
const cars = [{ image: "images/cars/1.png" }, { image: "images/cars/2.png" }];
"""

_MATCH_HTML = """<!DOCTYPE html>
<html><head><link rel="stylesheet" href="styles.css"></head>
<body><section class="hero card" id="hero"></section>
<script src="main.js"></script></body></html>
"""
_MATCH_CSS = ".hero { min-height: 100vh; } .card { padding: 1rem; }\n"
_MATCH_JS = "document.getElementById('hero');\n"


class PageWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        workspace.set_workspaces_dir(Path(self._tmp.name))

    def tearDown(self):
        workspace.set_workspaces_dir(None)
        self._tmp.cleanup()

    def _write(self, name, contents):
        workspace.write_text("alice", "c1", name, contents)

    def test_prompt_tells_the_model_to_fix_wiring(self):
        src = Path(code_agent_src()).read_text(encoding="utf-8")
        self.assertIn("Page wiring", src)
        self.assertIn("_with_page_wiring(username, chat_id, rel, f\"Wrote {written}\")", src)
        self.assertIn("_with_page_wiring(username, chat_id, rel, f\"Updated {rel}\")", src)

    def test_html_alone_has_no_wiring_note(self):
        self._write("index.html", _HTML)
        self.assertEqual(workspace.page_wiring_note("alice", "c1"), "")

    def test_mismatched_css_and_js_are_reported(self):
        self._write("index.html", _HTML)
        self._write("styles.css", _CSS)
        css_note = workspace.page_wiring_note("alice", "c1")
        self.assertIn("Page wiring", css_note)
        self.assertIn("car-grid", css_note)
        self.assertIn("cars-grid", css_note)
        self._write("main.js", _JS)
        js_note = workspace.page_wiring_note("alice", "c1")
        self.assertIn("carsGrid", js_note)
        self.assertIn("carGrid", js_note)
        self.assertIn("images/cars/1.png", js_note)
        root = workspace.workspace_root("alice", "c1")
        (root / "audio").mkdir()
        (root / "audio" / "track.wav").write_bytes(b"RIFF")
        (root / "images").mkdir()
        (root / "images" / "header.webp").write_bytes(b"RIFF")
        note = workspace.page_wiring_note("alice", "c1")
        self.assertIn("audio/track.wav", note)
        self.assertIn("images/header.webp", note)

    def test_matching_page_is_quiet(self):
        self._write("index.html", _MATCH_HTML)
        self._write("styles.css", _MATCH_CSS)
        self._write("main.js", _MATCH_JS)
        self.assertEqual(workspace.page_wiring_note("alice", "c1"), "")
