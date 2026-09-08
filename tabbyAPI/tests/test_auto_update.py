import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTO_UPDATE = ROOT / "tabbyAPI" / "deploy" / "arch" / "auto-update.sh"
UPDATE_SH = ROOT / "update.sh"
STATUS_JS = ROOT / "tabbyAPI" / "ui" / "static" / "status.js"


class AutoUpdateScriptTests(unittest.TestCase):
    def _run(self, env_text: str, stamp: str | None, extra_env: dict[str, str], *args: str):
        with tempfile.TemporaryDirectory() as tmp:
            stack = Path(tmp)
            tabby = stack / "tabbyAPI"
            deploy = tabby / "deploy" / "arch"
            deploy.mkdir(parents=True)
            (deploy / "tabby.env").write_text(env_text, encoding="utf-8")
            if stamp is not None:
                (stack / "tabby-auto-update.stamp").write_text(stamp, encoding="utf-8")
            log = stack / "auto.log"
            env = os.environ.copy()
            env.update(
                {
                    "TABBY_INSTALL_ROOT": str(stack),
                    "TABBY_AUTO_UPDATE_LOG": str(log),
                    **extra_env,
                }
            )
            result = subprocess.run(
                ["bash", str(AUTO_UPDATE), *args],
                cwd=str(stack),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result, log.read_text(encoding="utf-8") if log.is_file() else ""

    def test_disabled_skips(self):
        result, log = self._run(
            "TABBY_AUTO_UPDATE=0\nTABBY_AUTO_UPDATE_DAYS=7\nTABBY_NETWORK_PORT=1\n",
            "1",
            {},
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip (disabled)", log)

    def test_fresh_stamp_is_not_due(self):
        now = str(int(__import__("time").time()))
        result, log = self._run(
            "TABBY_AUTO_UPDATE=1\nTABBY_AUTO_UPDATE_DAYS=7\nTABBY_NETWORK_PORT=1\n",
            now,
            {},
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip (not-due)", log)

    def test_old_stamp_would_run(self):
        result, log = self._run(
            "TABBY_AUTO_UPDATE=1\nTABBY_AUTO_UPDATE_DAYS=7\nTABBY_AUTO_UPDATE_FULL=1\nTABBY_NETWORK_PORT=1\n",
            "1",
            {},
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would update", log)

    def test_status_prints_enabled(self):
        result, _log = self._run(
            "TABBY_AUTO_UPDATE=1\nTABBY_AUTO_UPDATE_DAYS=7\nTABBY_NETWORK_PORT=1\n",
            "1",
            {},
            "--status",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("enabled=on", result.stdout)
        self.assertIn("days=7", result.stdout)

    def test_update_sh_installs_timer(self):
        src = UPDATE_SH.read_text(encoding="utf-8")
        self.assertIn("auto-update.sh", src)
        self.assertIn("--install-units", src)

    def test_status_js_shows_auto_update(self):
        src = STATUS_JS.read_text(encoding="utf-8")
        self.assertIn("autoUpdateLabel", src)
        self.assertIn("data.auto_update", src)
