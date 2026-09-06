import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "iso" / "build.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "iso.yml"


class IsoBuildSmallTests(unittest.TestCase):
    def test_build_does_not_freeze_long_lived_downloads(self):
        src = BUILD.read_text(encoding="utf-8")
        self.assertIn("tsos-live-install", src)
        self.assertIn("TSOS_INSTALLER_STARTED", src)
        self.assertIn("city96/ComfyUI-GGUF", src)
        self.assertIn("tabbyapi-stack", src)
        self.assertNotIn("pacman -Sw", src)
        self.assertNotIn("pip download", src)
        self.assertNotIn("download.pytorch.org", src)
        self.assertNotIn("python.org/ftp/python", src)
        self.assertNotIn("docker save", src)
        self.assertNotIn("bundle_repo https://github.com/pyenv/pyenv.git", src)
        self.assertNotIn("comfyanonymous/ComfyUI.git", src)
        self.assertIn("frozen pacman repo should not be on the small ISO", src)

    def test_workflow_is_small_network_iso(self):
        src = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: Build TSOS ISO", src)
        self.assertNotIn("codebox-images.tar", src)
        self.assertNotIn("split -b 1900M", src)
        self.assertIn("first console starts the", src)
