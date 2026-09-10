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
INITCPIO_INSTALL = ROOT / "iso" / "initcpio" / "install" / "tsos_wait"
INSTALLER = ROOT / "tsos-installer.sh"


class IsoBuildSmallTests(unittest.TestCase):
    def test_build_does_not_freeze_long_lived_downloads(self):
        src = BUILD.read_text(encoding="utf-8")
        self.assertIn("tsos-live-install", src)
        self.assertIn("TSOS_INSTALLER_STARTED", src)
        self.assertIn(
            "ExecStart=-/usr/bin/agetty --noclear --noissue --autologin root - linux",
            src,
        )
        self.assertNotIn("agetty --noreset --clear", src)
        self.assertIn("plymouth", src)
        self.assertIn("quiet splash", src)
        self.assertIn("plymouth.use-simpledrm=1", src)
        self.assertIn("UseSimpledrm=true", src)
        self.assertIn("udev tsos_wait", src)
        self.assertIn('COMPRESSION="zstd"', src)
        self.assertIn("COMPRESSION_OPTIONS=(-19 -T0)", src)
        self.assertIn("tsos-boot-splash", src)
        self.assertIn("themes/tsos", src)
        self.assertIn("loading.png", src)
        self.assertIn("please-wait.png", src)
        self.assertIn("console-mode keep", src)
        self.assertIn("Loading, please wait", src)
        self.assertIn("city96/ComfyUI-GGUF", src)
        self.assertIn("tabbyapi-stack", src)
        self.assertIn('rm -f "$OUT/tsos-archlinux.iso" "$OUT/SHA256SUMS"', src)
        self.assertIn("-name 'tsos-archlinux-*-x86_64.iso'", src)
        self.assertIn("sha256sum tsos-archlinux.iso >SHA256SUMS", src)
        self.assertNotIn("pacman -Sw", src)
        self.assertNotIn("pip download", src)
        self.assertNotIn("download.pytorch.org", src)
        self.assertNotIn("python.org/ftp/python", src)
        self.assertNotIn("docker save", src)
        self.assertNotIn("bundle_repo https://github.com/pyenv/pyenv.git", src)
        self.assertNotIn("comfyanonymous/ComfyUI.git", src)
        self.assertIn("frozen pacman repo should not be on the small ISO", src)
        self.assertIn("pasted-images (chats/workspaces) must not ship on the ISO", src)
        self.assertIn("--exclude '**/pasted-images/'", src)
        self.assertIn("--exclude '**/ui_chats/'", src)
        self.assertIn("--exclude '**/ui_workspaces/'", src)
        self.assertIn("--exclude '**/ui_prefs/'", src)

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
        self.assertIn("run_earlyhook()", hook)
        self.assertIn("graphics_ready()", hook)
        self.assertIn("udevadm settle --timeout=5", hook)
        self.assertIn("/usr/bin/plymouthd", hook)
        self.assertIn("/usr/bin/plymouth --show-splash", hook)
        self.assertIn("run_latehook()", hook)
        self.assertIn("update-root-fs --new-root-dir=/new_root", hook)
        self.assertIn("run_emergencyhook()", hook)
        self.assertIn("LOADING", hook)
        self.assertIn("PLEASE WAIT", hook)
        install_hook = INITCPIO_INSTALL.read_text(encoding="utf-8")
        self.assertIn("source /usr/lib/initcpio/install/plymouth", install_hook)
        self.assertIn("add_runscript() { :; }", install_hook)

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

    def test_installer_installs_plymouth_on_full_arch(self):
        src = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("stage_target_boot_splash()", src)
        self.assertIn("tsos_source_tree()", src)
        self.assertRegex(
            src, r"(?m)^\s+plymouth$", msg="plymouth must be pacstrapped"
        )
        self.assertIn("splash_hook=\" tsos_wait\"", src)
        self.assertIn(
            "quiet splash plymouth.use-simpledrm=1 loglevel=3 systemd.show_status=false",
            src,
        )
        self.assertIn("UseSimpledrm=true", src)
        chroot = src.split('cat >"$TARGET/root/configure-arch.sh"')[1].split(
            "CHROOT\n"
        )[0]
        self.assertIn("udev${splash_hook}", chroot)
        self.assertIn("nvidia-drm.modeset=1${SPLASH_CMDLINE}", chroot)
        self.assertNotIn("plymouth-quit.service", chroot)

    def test_installer_overlay_excludes_user_state(self):
        overlay = INSTALLER.read_text(encoding="utf-8").split(
            "overlay_local_tabby_sources()"
        )[1].split("chown_target_user_tree()")[0]
        self.assertIn("--exclude '**/pasted-images/'", overlay)
        self.assertIn("--exclude '**/ui_chats/'", overlay)
        self.assertIn("--exclude '**/ui_workspaces/'", overlay)
        self.assertIn("--exclude '**/ui_users.json'", overlay)
        self.assertIn('rm -rf "$dest/tabbyAPI/venv" "$dest/tabbyAPI/models" "$dest/tabbyAPI/pasted-images"', overlay)
        install = ROOT / "install.sh"
        src = install.read_text(encoding="utf-8")
        self.assertIn("clear_fresh_install_ui_state()", src)
        self.assertIn('rm -rf "$DEST_TABBY/pasted-images"', src)
        epoch = ROOT / "tabbyAPI" / "ui" / "epoch.py"
        self.assertTrue(epoch.is_file())
        self.assertIn("pasted-images", epoch.read_text(encoding="utf-8"))
        self.assertIn('[[ "${UPDATE_MODE:-0}" -eq 0 ]] || return 0', src)
        self.assertIn("--exclude 'pasted-images/'", src)

    def test_installer_hides_unused_dialog_percent_marker(self):
        src = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("use_scrollbar = OFF", src)
        self.assertIn("position_indicator_color = (WHITE,WHITE,ON)", src)
        self.assertNotIn("use_scrollbar = ON", src)

    def test_installer_uses_github_main_when_online(self):
        src = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("pull_tabbyapi_stack_from_github()", src)
        self.assertIn("should_overlay_local_tabby()", src)
        self.assertIn("TABBY_STACK_FROM_GITHUB=1", src)
        self.assertIn("hub_edit_updates()", src)
        self.assertIn("TABBY_AUTO_UPDATE", src)
        self.assertIn("--no-auto-update", src)
        self.assertIn("origin/main", src)
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("inst_edit_updates()", install)
        self.assertIn("auto-update.sh", install)
        self.assertIn("tabbyapi-auto-update.timer", install)
