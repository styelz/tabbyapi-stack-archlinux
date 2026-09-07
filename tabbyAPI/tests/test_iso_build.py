import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "iso" / "build.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "iso.yml"
MK_SPLASH = ROOT / "iso" / "mk-splash.py"
PLYMOUTH_SCRIPT = ROOT / "iso" / "plymouth" / "tsos.script"
INITCPIO_HOOK = ROOT / "iso" / "initcpio" / "hooks" / "tsos_wait"
INSTALLER = ROOT / "tsos-installer.sh"


class IsoBuildSmallTests(unittest.TestCase):
    def test_build_does_not_freeze_long_lived_downloads(self):
        src = BUILD.read_text(encoding="utf-8")
        self.assertIn("tsos-live-install", src)
        self.assertIn("TSOS_INSTALLER_STARTED", src)
        self.assertIn("ExecStart=-/usr/bin/agetty --noissue --autologin root - linux", src)
        self.assertNotIn("agetty --noreset --clear", src)
        self.assertIn("plymouth", src)
        self.assertIn("quiet splash", src)
        self.assertIn("plymouth.use-simpledrm=1", src)
        self.assertIn("UseSimpledrm=true", src)
        self.assertIn("udev tsos_wait plymouth", src)
        self.assertIn("tsos-boot-splash", src)
        self.assertIn("themes/tsos", src)
        self.assertIn("loading.png", src)
        self.assertIn("please-wait.png", src)
        self.assertIn("console-mode keep", src)
        self.assertIn("Loading, please wait", src)
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

    def test_mk_splash_writes_logo_and_spinner(self):
        spec = importlib.util.spec_from_file_location("mk_splash", MK_SPLASH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            self.assertEqual(mod.main([str(dest)]), 0)
            for name in (
                "splash.png",
                "logo.png",
                "spinner.png",
                "loading.png",
                "please-wait.png",
            ):
                data = (dest / name).read_bytes()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"), name)
                self.assertGreater(len(data), 200, name)
            for ch in "ARCH LINUX + TABBYAPI-STACK":
                self.assertIn(ch, mod.FONT, ch)
            logo = mod.build_logo()
            self.assertEqual(len(logo), 100)
            self.assertEqual(len(logo[0]), 320)
            visible_x = [
                x
                for row in logo
                for x, pixel in enumerate(row)
                if pixel != mod.BG
            ]
            visible_center = (min(visible_x) + max(visible_x)) / 2
            self.assertEqual(visible_center, (len(logo[0]) - 1) / 2)

    def test_splash_layout_and_early_wait_hook(self):
        theme = PLYMOUTH_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("column_height", theme)
        self.assertIn("spin_slot_y", theme)
        self.assertIn('"Sans Bold 28"', theme)
        hook = INITCPIO_HOOK.read_text(encoding="utf-8")
        self.assertIn("LOADING", hook)
        self.assertIn("PLEASE WAIT", hook)

    def test_workflow_is_small_network_iso(self):
        src = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: Build TSOS ISO", src)
        self.assertNotIn("codebox-images.tar", src)
        self.assertNotIn("split -b 1900M", src)
        self.assertIn("first console starts the", src)

    def test_installer_discovers_mounted_weights_and_backups(self):
        src = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("list_mounted_storage()", src)
        self.assertIn("list_weight_sources()", src)
        self.assertIn("After choosing one, you can edit the path", src)
        self.assertIn("-name manifest.json", src)
        self.assertIn("-maxdepth 5", src)
