import unittest
from pathlib import Path
from unittest import mock

from ui import settings

SETTINGS_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "settings.js"


class UiSettingsSaveTests(unittest.TestCase):
    def test_reload_failure_returns_warning(self):
        with mock.patch.object(settings, "_apply_tabby"):
            with mock.patch.object(settings, "_reload_live", side_effect=RuntimeError("bad yaml")):
                with mock.patch.object(
                    settings,
                    "load_settings",
                    return_value={"ok": True, "tabby": [], "restart_hint": "hint"},
                ):
                    data = settings.save_settings({"tabby": {"logging": {}}})
        self.assertTrue(data["ok"])
        self.assertIn("bad yaml", data["reload_warning"])
        self.assertTrue(data["reload_warning"].startswith("Saved, but live reload failed:"))

    def test_reload_success_has_no_warning(self):
        with mock.patch.object(settings, "_apply_tabby"):
            with mock.patch.object(settings, "_reload_live"):
                with mock.patch.object(
                    settings,
                    "load_settings",
                    return_value={"ok": True, "tabby": [], "restart_hint": "hint"},
                ):
                    data = settings.save_settings({"tabby": {"logging": {}}})
        self.assertTrue(data["ok"])
        self.assertNotIn("reload_warning", data)


class ScreensaverSettingsTests(unittest.TestCase):
    def test_load_includes_screensaver_section(self):
        data = settings.load_settings()
        self.assertIn("screensaver", data)
        names = [field["name"] for field in data["screensaver"]["fields"]]
        self.assertEqual(names, ["enabled", "timeout", "logout_timeout", "hud_timeout"])

    def test_screensaver_save_writes_env(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "tabby.env"
            env.write_text("COMFYUI_URL=http://127.0.0.1:8188\n", encoding="utf-8")
            with mock.patch.object(settings, "ENV_PATH", env):
                with mock.patch.object(settings, "apply_saver_unit", return_value="") as apply_unit:
                    with mock.patch.object(settings, "_reload_live"):
                        settings.save_settings(
                            {
                                "screensaver": {
                                    "timeout": 90,
                                    "logout_timeout": 8,
                                    "hud_timeout": 0,
                                }
                            }
                        )
            text = env.read_text(encoding="utf-8")
            self.assertIn("TABBY_SAVER_IDLE_S=90", text)
            self.assertIn("TABBY_SAVER_LOGOUT_IDLE_S=8", text)
            self.assertIn("TABBY_SAVER_HUD_S=0", text)
            apply_unit.assert_called()

    def test_normalize_saver_aliases(self):
        self.assertEqual(settings.normalize_saver_key("timeout"), "timeout")
        self.assertEqual(settings.normalize_saver_key("logout-timeout"), "logout_timeout")
        self.assertEqual(settings.normalize_saver_key("TABBY_SAVER_IDLE_S"), "timeout")
        self.assertEqual(settings.normalize_saver_key("hud-timeout"), "hud_timeout")
        self.assertEqual(settings.normalize_saver_key("TABBY_SAVER_HUD_S"), "hud_timeout")


class SettingsJsTests(unittest.TestCase):
    def test_save_shows_reload_warning(self):
        src = SETTINGS_JS.read_text(encoding="utf-8")
        self.assertIn("data.reload_warning", src)
        self.assertIn("showError(data.reload_warning)", src)
        self.assertIn("data.screensaver", src)
        self.assertIn("section === \"screensaver\"", src)
        self.assertIn("data.updates", src)
        self.assertIn("section === \"updates\"", src)


class AutoUpdateSettingsTests(unittest.TestCase):
    def test_load_includes_updates_section(self):
        data = settings.load_settings()
        self.assertIn("updates", data)
        names = [field["name"] for field in data["updates"]["fields"]]
        self.assertEqual(names, ["enabled", "interval_days", "full"])

    def test_updates_save_writes_env(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "tabby.env"
            env.write_text("COMFYUI_URL=http://127.0.0.1:8188\n", encoding="utf-8")
            with mock.patch.object(settings, "ENV_PATH", env):
                with mock.patch.object(settings, "apply_auto_update_unit", return_value="") as apply_unit:
                    with mock.patch.object(settings, "_reload_live"):
                        settings.save_settings(
                            {
                                "updates": {
                                    "enabled": True,
                                    "interval_days": 14,
                                    "full": False,
                                }
                            }
                        )
            text = env.read_text(encoding="utf-8")
            self.assertIn("TABBY_AUTO_UPDATE=1", text)
            self.assertIn("TABBY_AUTO_UPDATE_DAYS=14", text)
            self.assertIn("TABBY_AUTO_UPDATE_FULL=0", text)
            apply_unit.assert_called()

    def test_normalize_update_aliases(self):
        self.assertEqual(settings.normalize_update_key("interval"), "interval_days")
        self.assertEqual(settings.normalize_update_key("TABBY_AUTO_UPDATE_DAYS"), "interval_days")
        self.assertEqual(settings.normalize_update_key("update-all"), "full")
        self.assertEqual(settings.normalize_update_key("enable"), "enabled")
