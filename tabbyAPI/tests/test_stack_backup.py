import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import stack_backup


class StackBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.stack = base / "live"
        self.tabby = self.stack / "tabbyAPI"
        self.comfy = self.stack / "ComfyUI"
        self.destination = base / "backup"
        self.catalog = self.tabby / "deploy" / "arch" / "models.json"
        self.catalog.parent.mkdir(parents=True)
        self.catalog.write_text(
            json.dumps(
                {
                    "items": {
                        "image": {
                            "kind": "file",
                            "dest": "comfy/models/checkpoints/image.safetensors",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        model = self.tabby / "models" / "test-model"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"model-data")
        image = self.comfy / "models" / "checkpoints" / "image.safetensors"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"image-data")
        (self.tabby / "config.yml").write_text("model: test\n", encoding="utf-8")
        (self.tabby / "ui_users.json").write_text('{"users":[]}\n', encoding="utf-8")
        chats = self.tabby / "pasted-images" / "ui_chats"
        chats.mkdir(parents=True)
        (chats / "alice.json").write_text('{"chats":[]}\n', encoding="utf-8")
        self.patchers = [
            mock.patch.object(stack_backup, "STACK_ROOT", self.stack),
            mock.patch.object(stack_backup, "TABBY_ROOT", self.tabby),
            mock.patch.object(stack_backup, "COMFY_ROOT", self.comfy),
            mock.patch.object(stack_backup, "CATALOG_PATH", self.catalog),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_plan_and_resumable_backup(self):
        plan = stack_backup.plan_backup(
            self.destination,
            include_config=True,
            include_users=True,
            include_chats=True,
        )
        self.assertGreater(plan["totals"]["models"], 0)
        self.assertGreater(plan["totals"]["config"], 0)
        self.assertGreater(plan["totals"]["users"], 0)
        self.assertGreater(plan["totals"]["chats"], 0)
        self.assertEqual(plan["needed_bytes"], plan["bytes"])
        self.assertTrue(plan["enough_space"])

        result = stack_backup.run_backup(
            self.destination,
            include_config=True,
            include_users=True,
            include_chats=True,
        )
        self.assertTrue((self.destination / "manifest.json").is_file())
        self.assertTrue(
            (self.destination / "tabbyAPI/models/test-model/model.safetensors").is_file()
        )
        self.assertTrue((self.destination / "extras/tabbyAPI/config.yml").is_file())
        self.assertFalse((self.destination / "tabbyAPI/models/extras").exists())
        self.assertGreater(result["copied_bytes"], 0)

        resumed = stack_backup.plan_backup(
            self.destination,
            include_config=True,
            include_users=True,
            include_chats=True,
        )
        self.assertEqual(resumed["needed_bytes"], 0)

    def test_restore_round_trip(self):
        stack_backup.run_backup(
            self.destination,
            include_config=True,
            include_users=True,
            include_chats=True,
        )
        weight = self.tabby / "models" / "test-model" / "model.safetensors"
        weight.unlink()
        (self.tabby / "config.yml").write_text("changed: true\n", encoding="utf-8")

        result = stack_backup.run_restore(
            self.destination,
            include_models=True,
            include_config=True,
            include_users=True,
            include_chats=True,
        )
        self.assertEqual(weight.read_bytes(), b"model-data")
        self.assertEqual((self.tabby / "config.yml").read_text(), "model: test\n")
        self.assertTrue(result["restart_recommended"])

    def test_rejects_destination_inside_install(self):
        with self.assertRaisesRegex(stack_backup.StackBackupError, "outside"):
            stack_backup.plan_backup(self.stack / "nested-backup")

    def test_restore_rejects_unsafe_manifest_path(self):
        self.destination.mkdir()
        (self.destination / "manifest.json").write_text(
            json.dumps(
                {
                    "format": stack_backup.FORMAT,
                    "version": stack_backup.VERSION,
                    "groups": ["models"],
                    "items": [{"path": "../escape", "group": "models", "bytes": 1}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(stack_backup.StackBackupError, "unsafe"):
            stack_backup.plan_restore(self.destination)


if __name__ == "__main__":
    unittest.main()
