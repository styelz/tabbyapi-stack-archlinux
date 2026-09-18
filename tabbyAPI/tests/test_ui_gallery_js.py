"""Gallery checkbox shift-range. Keep in sync with ui/static/gallery.js."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

GALLERY_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "gallery.js"


class GalleryShiftRangeTests(unittest.TestCase):
    def setUp(self):
        self.src = GALLERY_JS.read_text(encoding="utf-8")

    def test_shift_range_follows_clicked_checkbox_state(self):
        self.assertIn("function applyRange(from, to, checked)", self.src)
        self.assertIn("boxes[j].checked = checked", self.src)
        self.assertNotRegex(self.src, r"boxes\[j\]\.checked\s*=\s*true")
        self.assertRegex(
            self.src,
            r"const checked = boxes\[i\]\.checked",
        )

    def test_shift_click_captures_state_before_prevent_default(self):
        click = re.search(
            r"const from = lastIndex;\n(?P<body>.*?)\n    lastIndex = i;",
            self.src,
            re.S,
        )
        self.assertIsNotNone(click)
        body = click.group("body")
        checked_at = body.find("const checked = boxes[i].checked")
        prevent_at = body.find("event.preventDefault()")
        self.assertGreater(checked_at, -1)
        self.assertGreater(prevent_at, -1)
        self.assertLess(checked_at, prevent_at)

    def test_figure_html_escapes_urls(self):
        self.assertIn(
            'const url = TabbyUI.escapeHtml(TabbyUI.resolveUiUrl(item.url));',
            self.src,
        )
        self.assertIn(
            'const thumb = TabbyUI.escapeHtml(TabbyUI.resolveUiUrl(item.thumb));',
            self.src,
        )
        self.assertIn('href="${url}" data-full="${url}"', self.src)
        self.assertNotRegex(self.src, r'href="\$\{TabbyUI\.resolveUiUrl')

    def test_modal_click_does_not_close_on_player(self):
        self.assertIn('if (event.target.closest("video, audio")) return;', self.src)
        self.assertIn('window.open(url, "_blank", "noopener")', self.src)
        self.assertNotIn('window.open(url, "_blank", "noreferrer")', self.src)


if __name__ == "__main__":
    unittest.main()
