import unittest
from pathlib import Path
from unittest import mock

from common import downloader


class DownloadFolderTests(unittest.TestCase):
    def setUp(self):
        self.model_dir = Path("/models")
        self.lora_dir = Path("/loras")
        patcher = mock.patch.object(downloader, "config")
        cfg = patcher.start()
        self.addCleanup(patcher.stop)
        cfg.model.model_dir = str(self.model_dir)
        cfg.lora.lora_dir = str(self.lora_dir)

    def test_defaults_to_repo_name(self):
        path = downloader._get_download_folder("org/My-Model", "model", None)
        self.assertEqual(path, self.model_dir / "My-Model")
        path = downloader._get_download_folder("org/My-Model", "model", "")
        self.assertEqual(path, self.model_dir / "My-Model")

    def test_lora_uses_lora_dir(self):
        path = downloader._get_download_folder("org/adapter", "lora", "custom")
        self.assertEqual(path, self.lora_dir / "custom")

    def test_rejects_traversal_and_separators(self):
        for bad in ("..", ".", "  ", "../x", "a/b", "a\\b", "x\0y", "/abs"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    downloader._get_download_folder("org/repo", "model", bad)

    def test_rejects_bad_repo_derived_name(self):
        with self.assertRaises(ValueError):
            downloader._get_download_folder("org/..", "model", None)


if __name__ == "__main__":
    unittest.main()
