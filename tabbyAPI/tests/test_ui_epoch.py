"""A new ISO at the same LAN URL must not reuse the previous install's id."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui import epoch
from ui.epoch import EPOCH_BOOT_MARK, inject_index_epoch, load_epoch


class EpochStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        epoch.set_epoch_path(Path(self.tmp.name) / "ui_epoch")

    def tearDown(self):
        epoch.set_epoch_path(None)
        self.tmp.cleanup()

    def test_persists_until_file_is_removed(self):
        first = load_epoch()
        self.assertEqual(len(first), 32)
        self.assertEqual(load_epoch(), first)
        epoch.epoch_path().unlink()
        second = load_epoch()
        self.assertEqual(len(second), 32)
        self.assertNotEqual(second, first)

    def test_inject_replaces_mark(self):
        html = f"<script>{EPOCH_BOOT_MARK}</script>"
        out = inject_index_epoch(html, "abc123")
        self.assertNotIn(EPOCH_BOOT_MARK, out)
        self.assertIn('window.TABBY_UI_EPOCH = "abc123";', out)


if __name__ == "__main__":
    unittest.main()
