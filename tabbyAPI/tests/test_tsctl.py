import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_tsctl():
    path = Path(__file__).resolve().parents[1] / "tools" / "tsctl.py"
    spec = importlib.util.spec_from_file_location("tsctl_cli", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TsctlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tsctl = _load_tsctl()

    def test_parse_pairs(self):
        self.assertEqual(self.tsctl.parse_pairs(["timeout=120"]), [("timeout", "120")])
        self.assertEqual(self.tsctl.parse_pairs(["timeout", "90"]), [("timeout", "90")])

    def test_dispatch_sets_screensaver_timeout(self):
        payload = {
            "ok": True,
            "tabby": [],
            "screensaver": {
                "name": "screensaver",
                "label": "Screensaver",
                "fields": [
                    {"name": "enabled", "kind": "bool", "value": False},
                    {"name": "timeout", "kind": "int", "value": 120},
                    {"name": "logout_timeout", "kind": "int", "value": 10},
                    {"name": "hud_timeout", "kind": "int", "value": 300},
                ],
            },
            "system": {"name": "system", "fields": []},
        }
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            with mock.patch.object(self.tsctl, "save_settings", return_value={"ok": True}) as save:
                code = self.tsctl.dispatch(["screensaver", "timeout=90"])
        self.assertEqual(code, 0)
        save.assert_called_once()
        self.assertEqual(save.call_args[0][0]["screensaver"]["timeout"], 90)

    def test_dispatch_enable(self):
        payload = {
            "ok": True,
            "tabby": [],
            "screensaver": {
                "name": "screensaver",
                "fields": [{"name": "enabled", "kind": "bool", "value": False}],
            },
            "updates": {
                "name": "updates",
                "fields": [{"name": "enabled", "kind": "bool", "value": True}],
            },
            "system": {"name": "system", "fields": []},
        }
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            with mock.patch.object(self.tsctl, "save_settings", return_value={"ok": True}) as save:
                self.tsctl.dispatch(["screensaver", "enable"])
        self.assertEqual(save.call_args[0][0]["screensaver"]["enabled"], True)

    def test_dispatch_updates_interval(self):
        payload = {
            "ok": True,
            "tabby": [],
            "updates": {
                "name": "updates",
                "label": "Updates",
                "fields": [
                    {"name": "enabled", "kind": "bool", "value": True},
                    {"name": "interval_days", "kind": "int", "value": 7},
                    {"name": "full", "kind": "bool", "value": True},
                ],
            },
            "system": {"name": "system", "fields": []},
        }
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            with mock.patch.object(self.tsctl, "save_settings", return_value={"ok": True}) as save:
                code = self.tsctl.dispatch(["updates", "interval_days=14"])
        self.assertEqual(code, 0)
        save.assert_called_once()
        self.assertEqual(save.call_args[0][0]["updates"]["interval_days"], 14)

    def test_complete_lists_sections(self):
        payload = {
            "tabby": [{"name": "network", "fields": [{"name": "host"}]}],
            "screensaver": {"name": "screensaver", "fields": [{"name": "timeout"}]},
            "updates": {"name": "updates", "fields": [{"name": "interval_days"}]},
            "system": {"name": "system", "fields": []},
        }
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            words = self.tsctl.complete_words(1, ["tsctl"])
        self.assertIn("screensaver", words)
        self.assertIn("updates", words)
        self.assertIn("network", words)
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            keys = self.tsctl.complete_words(2, ["tsctl", "updates"])
        self.assertIn("interval_days", keys)
        self.assertIn("enable", keys)
        self.assertIn("git", keys)
        self.assertIn("all", keys)
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            flags = self.tsctl.complete_words(3, ["tsctl", "updates", "git"])
        self.assertIn("--comfy", flags)
        self.assertIn("--no-restart", flags)

    def test_backup_dry_run_prints_plan_without_copying(self):
        from ui import stack_backup

        plan = {
            "destination": "/mnt/backup",
            "groups": ["models"],
            "totals": {"models": 1024},
            "files": 2,
            "needed_bytes": 1024,
            "free_bytes": 2048,
            "enough_space": True,
        }
        with mock.patch.object(stack_backup, "plan_backup", return_value=plan) as inspect:
            with mock.patch.object(stack_backup, "run_backup") as run:
                code = self.tsctl.dispatch(["backup", "/mnt/backup", "--dry-run"])
        self.assertEqual(code, 0)
        inspect.assert_called_once()
        run.assert_not_called()

    def test_restore_group_flags_are_exact(self):
        path, options = self.tsctl._stack_backup_args(
            "restore", ["/mnt/backup", "--config"]
        )
        self.assertEqual(path, "/mnt/backup")
        self.assertFalse(options["include_models"])
        self.assertTrue(options["include_config"])

    def test_complete_lists_backup_flags(self):
        with mock.patch.object(
            self.tsctl,
            "load_settings",
            return_value={"tabby": [], "screensaver": {}, "gpu": {}, "system": {}},
        ):
            flags = self.tsctl.complete_words(3, ["tsctl", "backup", "/mnt/backup"])
        self.assertIn("--config", flags)
        self.assertIn("--dry-run", flags)

    def test_menu_groups_use_plain_tags(self):
        groups = [tag for tag, *_ in self.tsctl.MENU_GROUPS]
        self.assertEqual(
            groups, ["service", "updates", "inference", "server", "host", "data", "help"]
        )
        service = [tag for tag, *_ in self.tsctl.SERVICE_ACTIONS]
        self.assertEqual(service, ["start", "stop", "restart", "status"])
        backup = [tag for tag, *_ in self.tsctl.BACKUP_ACTIONS]
        self.assertEqual(backup, ["backup", "restore"])
        update_run = [tag for tag, *_ in self.tsctl.UPDATE_ACTIONS]
        self.assertEqual(update_run, ["git", "all", "git-comfy", "all-comfy"])
        for tag in groups + service + backup + update_run:
            self.assertFalse(tag.startswith("_"))
            self.assertFalse(tag.endswith("_"))

    def test_menu_groups_cover_every_section(self):
        payload = {
            "tabby": [
                {"name": name, "fields": []}
                for name in (
                    "network",
                    "logging",
                    "model",
                    "draft_model",
                    "lora",
                    "embeddings",
                    "sampling",
                    "memory",
                    "developer",
                    "future_section",
                )
            ],
            "screensaver": {"name": "screensaver", "fields": []},
            "updates": {"name": "updates", "fields": []},
            "gpu": {"name": "gpu", "fields": []},
            "system": {"name": "system", "fields": []},
        }
        grouped = self.tsctl._grouped_sections(payload)
        names = lambda tag: [section["name"] for section in grouped[tag]]
        self.assertEqual(
            names("inference"),
            ["model", "draft_model", "lora", "embeddings", "sampling", "memory"],
        )
        self.assertEqual(names("server"), ["network", "logging", "developer", "future_section"])
        self.assertEqual(names("host"), ["gpu", "screensaver", "system"])
        self.assertEqual(names("updates"), ["updates"])
        self.assertEqual(names("service"), [])
        self.assertEqual(names("data"), [])
        shown = [section["name"] for tag in grouped for section in grouped[tag]]
        expected = [section["name"] for section in self.tsctl._sections(payload)]
        self.assertEqual(sorted(shown), sorted(expected))

    def test_complete_lists_api_unit_commands(self):
        payload = {"tabby": [], "screensaver": {}, "gpu": {}, "system": {}}
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            words = self.tsctl.complete_words(1, ["tsctl"])
        self.assertIn("start", words)
        self.assertIn("stop", words)
        self.assertIn("restart", words)
        self.assertIn("status", words)

    def test_dispatch_api_unit(self):
        with mock.patch.object(self.tsctl, "api_unit", return_value=0) as run:
            self.assertEqual(self.tsctl.dispatch(["start"]), 0)
            self.assertEqual(self.tsctl.dispatch(["stop"]), 0)
            self.assertEqual(self.tsctl.dispatch(["restart"]), 0)
            self.assertEqual(self.tsctl.dispatch(["status"]), 0)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            ["start", "stop", "restart", "status"],
        )

    def test_parse_update_run(self):
        self.assertEqual(self.tsctl.parse_update_run(["git"]), ("git", False, None))
        self.assertEqual(self.tsctl.parse_update_run(["all", "--comfy"]), ("all", True, None))
        self.assertEqual(
            self.tsctl.parse_update_run(["git-comfy", "--no-restart"]),
            ("git", True, False),
        )
        self.assertEqual(
            self.tsctl.parse_update_run(["all-comfy", "--restart"]),
            ("all", True, True),
        )

    def test_run_stack_update_builds_update_sh_args(self):
        script = Path("/tmp/tabbyapi-stack/update.sh")
        with mock.patch.object(self.tsctl, "update_script", return_value=script):
            with mock.patch("subprocess.call", return_value=0) as call:
                code = self.tsctl.run_stack_update("all", comfy=True, restart=None)
        self.assertEqual(code, 0)
        self.assertEqual(
            call.call_args[0][0],
            ["bash", str(script), "--all", "--comfy"],
        )

    def test_dispatch_updates_runs_update_sh(self):
        payload = {
            "ok": True,
            "tabby": [],
            "updates": {
                "name": "updates",
                "fields": [{"name": "enabled", "kind": "bool", "value": True}],
            },
            "system": {"name": "system", "fields": []},
        }
        with mock.patch.object(self.tsctl, "load_settings", return_value=payload):
            with mock.patch.object(self.tsctl, "run_stack_update", return_value=0) as run:
                code = self.tsctl.dispatch(["updates", "git", "--no-restart"])
        self.assertEqual(code, 0)
        run.assert_called_once_with("git", comfy=False, restart=False)

    def test_api_restart_refreshes_screensaver(self):
        with (
            mock.patch("common.gpu_mode.systemctl_user") as user,
            mock.patch("restart_stack.maybe_restart_screensaver") as saver,
        ):
            user.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertEqual(self.tsctl.api_unit("restart"), 0)
        saver.assert_called_once_with()
        user.assert_called()

    def test_run_dialog_labels_submenu_cancel_as_back(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stderr="")
            self.tsctl.run_dialog(["--menu", "x", "10", "40", "4"], cancel_label="Back")
            argv = run.call_args[0][0]
            self.assertEqual(argv[argv.index("--cancel-label") + 1], "Back")
            self.tsctl.run_dialog(["--menu", "x", "10", "40", "4"])
            argv = run.call_args[0][0]
            self.assertNotIn("--cancel-label", argv)

    def test_submenu_menus_use_back_button(self):
        calls = []

        def fake_dialog(args, cancel_label=None):
            calls.append(cancel_label)
            return 1, ""

        with mock.patch.object(self.tsctl, "run_dialog", side_effect=fake_dialog):
            self.assertEqual(self.tsctl.tui_dialog(), 0)
            self.assertEqual(self.tsctl.dialog_service(), 0)
            self.assertEqual(self.tsctl.dialog_updates(), 0)
            self.assertEqual(self.tsctl.dialog_backup_menu(), 0)
        self.assertEqual(calls[0], None)
        self.assertEqual(calls[1:], ["Back", "Back", "Back"])
