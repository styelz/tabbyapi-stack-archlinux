import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import gallery_owners
from ui.manager import gallery_listing


class GalleryOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        gallery_owners.set_owners_path(folder / "gallery_owners.json")
        self._dir_patch = mock.patch("common.gpu_mode.GENERATED_DIR", folder)
        self._dir_patch.start()
        (folder / "generated-admin.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (folder / "generated-alice.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        gallery_owners.record_owner("generated-alice.png", "alice")

    def tearDown(self):
        self._dir_patch.stop()
        gallery_owners.set_owners_path(None)
        self.tmp.cleanup()

    def test_untagged_is_admin_only(self):
        self.assertIsNone(gallery_owners.owner_of("generated-admin.png"))
        self.assertTrue(gallery_owners.can_access("generated-admin.png", "tabby", True))
        self.assertFalse(gallery_owners.can_access("generated-admin.png", "alice", False))
        self.assertTrue(gallery_owners.can_access("generated-alice.png", "alice", False))
        self.assertFalse(gallery_owners.can_access("generated-alice.png", "bob", False))

    def test_listing_filters_for_extra_user(self):
        admin = gallery_listing(page=1, per_page=24, username="tabby", is_admin=True)
        names = {item["name"] for item in admin["items"]}
        self.assertIn("generated-admin.png", names)
        self.assertIn("generated-alice.png", names)
        extra = gallery_listing(page=1, per_page=24, username="alice", is_admin=False)
        extra_names = {item["name"] for item in extra["items"]}
        self.assertEqual(extra_names, {"generated-alice.png"})
        self.assertEqual(extra["items"][0]["owner"], "alice")
        self.assertEqual(extra["items"][0]["url"], "/v1/images/generated-alice.png")
