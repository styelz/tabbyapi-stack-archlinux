"""Per-user backup zip is only that account's chats, workspaces, prefs, and gallery."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from common import gallery_owners
from ui import backup, chats, prefs, workspace
from ui.backup import BackupError


class UserBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.folder = folder
        chats.set_chats_dir(folder / "ui_chats")
        prefs.set_prefs_dir(folder / "ui_prefs")
        workspace.set_workspaces_dir(folder / "ui_workspaces")
        gallery_owners.set_owners_path(folder / "gallery_owners.json")
        self._dir_patch = mock.patch("common.gpu_mode.GENERATED_DIR", folder)
        self._dir_patch.start()
        self._drop_patch = mock.patch("ui.codebox.drop_user_containers")
        self._drop_patch.start()
        self._seed()

    def tearDown(self):
        self._drop_patch.stop()
        self._dir_patch.stop()
        gallery_owners.set_owners_path(None)
        workspace.set_workspaces_dir(None)
        prefs.set_prefs_dir(None)
        chats.set_chats_dir(None)
        self.tmp.cleanup()

    def _seed(self):
        chats.save_store(
            "alice",
            {
                "version": 1,
                "activeId": "a1",
                "chats": [
                    {
                        "id": "a1",
                        "mode": "code",
                        "title": "Alice app",
                        "updatedAt": 1,
                        "messages": [{"role": "user", "content": "hello alice"}],
                    }
                ],
            },
        )
        chats.save_store(
            "bob",
            {
                "version": 1,
                "activeId": "b1",
                "chats": [
                    {
                        "id": "b1",
                        "mode": "chat",
                        "title": "Bob chat",
                        "updatedAt": 1,
                        "messages": [{"role": "user", "content": "hello bob"}],
                    }
                ],
            },
        )
        prefs.save_prefs("alice", {"theme": "ember", "zoom": 110})
        prefs.save_prefs("bob", {"theme": "moss", "zoom": 90})
        alice_ws = workspace.user_dir("alice") / "a1"
        alice_ws.mkdir(parents=True)
        (alice_ws / "index.html").write_text("<h1>Alice</h1>\n", encoding="utf-8")
        bob_ws = workspace.user_dir("bob") / "b1"
        bob_ws.mkdir(parents=True)
        (bob_ws / "secret.txt").write_text("bob only\n", encoding="utf-8")
        (self.folder / "generated-alice.png").write_bytes(b"\x89PNG\r\nalice")
        (self.folder / "generated-bob.png").write_bytes(b"\x89PNG\r\nbob")
        (self.folder / "generated-admin.png").write_bytes(b"\x89PNG\r\nadmin")
        gallery_owners.record_owner("generated-alice.png", "alice")
        gallery_owners.record_owner("generated-bob.png", "bob")

    def _names(self, zip_path: Path) -> set[str]:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return set(zf.namelist())

    def test_alice_zip_excludes_bob(self):
        dest = self.folder / "alice.zip"
        backup.build_archive("alice", dest)
        names = self._names(dest)
        self.assertIn("manifest.json", names)
        self.assertIn("chats.json", names)
        self.assertIn("prefs.json", names)
        self.assertIn("workspace/a1/index.html", names)
        self.assertIn("gallery/generated-alice.png", names)
        self.assertNotIn("workspace/b1/secret.txt", names)
        self.assertNotIn("gallery/generated-bob.png", names)
        self.assertNotIn("gallery/generated-admin.png", names)
        with zipfile.ZipFile(dest, "r") as zf:
            store = json.loads(zf.read("chats.json"))
            prefs_data = json.loads(zf.read("prefs.json"))
            manifest = json.loads(zf.read("manifest.json"))
            owners = json.loads(zf.read("gallery_owners.json"))
        titles = [chat["title"] for chat in store["chats"]]
        self.assertEqual(titles, ["Alice app"])
        self.assertNotIn("Bob chat", titles)
        self.assertEqual(prefs_data["theme"], "ember")
        self.assertEqual(manifest["format"], backup.FORMAT)
        self.assertEqual(manifest["username"], "alice")
        self.assertEqual(owners, {"generated-alice.png": "alice"})

    def test_admin_untagged_images_only_when_requested(self):
        dest = self.folder / "admin.zip"
        backup.build_archive("tabby", dest, include_untagged=True)
        names = self._names(dest)
        self.assertIn("gallery/generated-admin.png", names)
        self.assertNotIn("gallery/generated-alice.png", names)
        self.assertNotIn("gallery/generated-bob.png", names)
        extra = self.folder / "alice2.zip"
        backup.build_archive("alice", extra, include_untagged=False)
        extra_names = self._names(extra)
        self.assertIn("gallery/generated-alice.png", extra_names)
        self.assertNotIn("gallery/generated-admin.png", extra_names)

    def test_restore_alice_does_not_change_bob(self):
        dest = self.folder / "alice.zip"
        backup.build_archive("alice", dest)
        chats.save_store(
            "alice",
            {
                "version": 1,
                "activeId": "gone",
                "chats": [
                    {
                        "id": "gone",
                        "mode": "chat",
                        "title": "Wiped",
                        "updatedAt": 2,
                        "messages": [{"role": "user", "content": "new"}],
                    }
                ],
            },
        )
        (workspace.user_dir("alice") / "a1" / "index.html").write_text(
            "changed\n", encoding="utf-8"
        )
        result = backup.restore_archive("alice", dest)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_username"], "alice")
        store = chats.load_store("alice")
        self.assertEqual(store["chats"][0]["title"], "Alice app")
        self.assertEqual(
            (workspace.user_dir("alice") / "a1" / "index.html").read_text(encoding="utf-8"),
            "<h1>Alice</h1>\n",
        )
        bob = chats.load_store("bob")
        self.assertEqual(bob["chats"][0]["title"], "Bob chat")
        self.assertEqual(
            (workspace.user_dir("bob") / "b1" / "secret.txt").read_text(encoding="utf-8"),
            "bob only\n",
        )
        self.assertEqual(prefs.load_prefs("bob")["theme"], "moss")
        self.assertTrue((self.folder / "generated-bob.png").is_file())
        self.assertEqual(gallery_owners.owner_of("generated-bob.png"), "bob")

    def test_restore_accepts_legacy_format_name(self):
        dest = self.folder / "legacy.zip"
        backup.build_archive("alice", dest)
        with zipfile.ZipFile(dest, "r") as zf:
            payload = {name: zf.read(name) for name in zf.namelist()}
        manifest = json.loads(payload["manifest.json"])
        manifest["format"] = backup.FORMAT_LEGACY
        payload["manifest.json"] = (json.dumps(manifest) + "\n").encode()
        with zipfile.ZipFile(dest, "w") as zf:
            for name, data in payload.items():
                zf.writestr(name, data)
        chats.save_store("alice", {"version": 1, "activeId": "gone", "chats": []})
        result = backup.restore_archive("alice", dest)
        self.assertTrue(result["ok"])
        store = chats.load_store("alice")
        self.assertEqual(store["chats"][0]["title"], "Alice app")

    def test_restore_imports_into_current_user(self):
        dest = self.folder / "alice.zip"
        backup.build_archive("alice", dest)
        backup.restore_archive("carol", dest)
        store = chats.load_store("carol")
        self.assertEqual(store["chats"][0]["title"], "Alice app")
        self.assertTrue((workspace.user_dir("carol") / "a1" / "index.html").is_file())
        self.assertEqual(gallery_owners.owner_of("generated-alice.png"), "alice")
        restored = self.folder / "generated-alice-restored1.png"
        self.assertTrue(restored.is_file())
        self.assertEqual(gallery_owners.owner_of(restored.name), "carol")
        self.assertEqual(chats.load_store("alice")["chats"][0]["title"], "Alice app")

    def test_gallery_collision_keeps_other_user_file(self):
        dest = self.folder / "alice.zip"
        backup.build_archive("alice", dest)
        gallery_owners.forget_owners(["generated-alice.png"])
        (self.folder / "generated-alice.png").write_bytes(b"\x89PNG\r\nbob-took-name")
        gallery_owners.record_owner("generated-alice.png", "bob")
        backup.restore_archive("alice", dest)
        self.assertEqual(
            (self.folder / "generated-alice.png").read_bytes(),
            b"\x89PNG\r\nbob-took-name",
        )
        self.assertEqual(gallery_owners.owner_of("generated-alice.png"), "bob")
        restored = self.folder / "generated-alice-restored1.png"
        self.assertTrue(restored.is_file())
        self.assertEqual(restored.read_bytes(), b"\x89PNG\r\nalice")
        self.assertEqual(gallery_owners.owner_of(restored.name), "alice")

    def test_zip_slip_rejected(self):
        dest = self.folder / "evil.zip"
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr(
                "manifest.json",
                json.dumps({"format": backup.FORMAT, "version": 1, "username": "alice"})
                + "\n",
            )
            zf.writestr("../etc/passwd", "hacked\n")
        with self.assertRaises(BackupError):
            backup.restore_archive("alice", dest)
        self.assertFalse((self.folder / "etc" / "passwd").exists())

    def test_bad_manifest_rejected(self):
        dest = self.folder / "bad.zip"
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"format": "nope", "version": 1}) + "\n")
        with self.assertRaises(BackupError):
            backup.restore_archive("alice", dest)
        dest2 = self.folder / "empty.zip"
        with zipfile.ZipFile(dest2, "w") as zf:
            zf.writestr("chats.json", "{}\n")
        with self.assertRaises(BackupError):
            backup.restore_archive("alice", dest2)

    def test_menu_and_routes_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "ui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-user-act="backup"', html)
        self.assertIn('data-user-act="restore"', html)
        self.assertIn("id=\"user-backup-file\"", html)
        self.assertIn("downloadBackup", js)
        self.assertIn("backup.zip", js)
        self.assertIn("application/zip", js)
        self.assertIn("backup/restore", js)


if __name__ == "__main__":
    unittest.main()
