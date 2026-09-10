import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import restart_stack
from common.phrase_switch import restart_reply_text, start_restart
from endpoints.OAI.types.chat_completion import ChatCompletionMessage, ChatCompletionRequest


class RestartStackTests(unittest.TestCase):
    def test_restart_units_llm_stops_comfy(self):
        with (
            mock.patch("restart_stack.subprocess.run") as run,
            mock.patch("restart_stack.maybe_restart_screensaver", return_value=False),
        ):
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(restart_stack.restart_units("llm"), 0)
        cmds = [call.args[0] for call in run.call_args_list]
        self.assertEqual(cmds[0], ["systemctl", "--user", "stop", "comfyui"])
        self.assertEqual(cmds[-1], ["systemctl", "--user", "restart", "tabbyapi"])

    def test_restart_units_comfy_restarts_both(self):
        with (
            mock.patch("restart_stack.subprocess.run") as run,
            mock.patch("restart_stack.maybe_restart_screensaver", return_value=False),
        ):
            run.return_value = mock.Mock(returncode=0)
            restart_stack.restart_units("comfy")
        cmds = [call.args[0] for call in run.call_args_list]
        self.assertEqual(cmds[0], ["systemctl", "--user", "reset-failed", "comfyui"])
        self.assertEqual(cmds[1], ["systemctl", "--user", "restart", "comfyui"])
        self.assertEqual(cmds[-1], ["systemctl", "--user", "restart", "tabbyapi"])

    def test_restart_units_refreshes_stale_screensaver(self):
        with (
            mock.patch("restart_stack.subprocess.run") as run,
            mock.patch("restart_stack.maybe_restart_screensaver") as saver,
        ):
            run.return_value = mock.Mock(returncode=0)
            restart_stack.restart_units("llm")
        saver.assert_called_once_with()

    def test_screensaver_needs_restart_when_files_are_newer(self):
        self.assertTrue(
            restart_stack.screensaver_needs_restart(force=True, pid=0)
        )
        self.assertFalse(
            restart_stack.screensaver_needs_restart(force=False, pid=0)
        )
        self.assertTrue(
            restart_stack.screensaver_needs_restart(
                force=False, pid=9, started=100.0, newest=200.0
            )
        )
        self.assertFalse(
            restart_stack.screensaver_needs_restart(
                force=False, pid=9, started=200.0, newest=100.0
            )
        )

    def test_maybe_restart_screensaver_restarts_stale_unit(self):
        with (
            mock.patch("restart_stack._unit_main_pid", return_value=42),
            mock.patch("restart_stack.screensaver_needs_restart", return_value=True),
            mock.patch("restart_stack.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertTrue(restart_stack.maybe_restart_screensaver(force=False))
        self.assertEqual(
            run.call_args.args[0],
            ["sudo", "-n", "systemctl", "restart", "tabby-saver"],
        )

    def test_maybe_restart_screensaver_skips_current_process(self):
        with (
            mock.patch("restart_stack._unit_main_pid", return_value=42),
            mock.patch("restart_stack.screensaver_needs_restart", return_value=False),
            mock.patch("restart_stack.subprocess.run") as run,
        ):
            self.assertFalse(restart_stack.maybe_restart_screensaver(force=False))
        run.assert_not_called()

    def test_saver_if_updated_flag_skips_api_restart(self):
        with mock.patch("restart_stack.maybe_restart_screensaver") as saver:
            self.assertEqual(restart_stack.main(["--saver-if-updated"]), 0)
        saver.assert_called_once_with()

    def test_main_unlinks_lock_after_delay(self):
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "switch-model.lock"
            lock.write_text("restart", encoding="utf-8")
            with (
                mock.patch("restart_stack.time.sleep") as sleep,
                mock.patch("restart_stack.restart_units", return_value=0) as units,
                mock.patch("restart_stack.abandon_persisted_jobs") as abandon,
            ):
                self.assertEqual(
                    restart_stack.main(["--delay", "1.5", "--mode", "llm", "--lock", str(lock)]),
                    0,
                )
            sleep.assert_called_once_with(1.5)
            abandon.assert_called_once()
            units.assert_called_once_with("llm")
            self.assertFalse(lock.exists())

    def test_start_restart_detaches_helper(self):
        with (
            mock.patch("common.phrase_switch.shutil.which", return_value="/usr/bin/systemctl"),
            mock.patch("common.phrase_switch.gpu_is_comfy", return_value=False),
            mock.patch("common.phrase_switch._abandon_jobs_for_restart") as abandon,
            mock.patch("common.phrase_switch.subprocess.Popen") as popen,
            mock.patch("common.phrase_switch.LOCK") as lock,
            mock.patch("common.phrase_switch.LOG") as log,
        ):
            lock.write_text = mock.Mock()
            log.touch = mock.Mock()
            log.open = mock.mock_open()
            self.assertTrue(start_restart())
            abandon.assert_called_once()
            popen.assert_called_once()
            args = popen.call_args.args[0]
            self.assertIn("restart_stack.py", args[1])
            self.assertIn("--mode", args)

    def test_abandon_persisted_jobs_rewrites_running(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "mcp_jobs.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": "running-job",
                            "status": "running",
                            "phase": "generating",
                            "items": [
                                {"status": "done", "prompt": "hero"},
                                {"status": "queued", "prompt": "logo"},
                            ],
                        },
                        {
                            "id": "done-job",
                            "status": "done",
                            "phase": "done",
                            "items": [{"status": "done", "prompt": "old"}],
                        },
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(restart_stack.abandon_persisted_jobs(path), 1)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["status"], "error")
            self.assertEqual(data[0]["phase"], "error")
            self.assertIn("restarted", data[0]["error"])
            self.assertEqual(data[0]["items"][0]["status"], "done")
            self.assertEqual(data[0]["items"][1]["status"], "error")
            self.assertEqual(data[1]["status"], "done")

    def test_start_restart_missing_systemctl(self):
        with mock.patch("common.phrase_switch.shutil.which", return_value=None):
            self.assertFalse(start_restart())

    def test_restart_reply_mentions_wait(self):
        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="restart")]
        )
        with mock.patch("common.phrase_switch.gpu_is_comfy", return_value=False):
            with mock.patch("common.phrase_switch.wait_hint", return_value="Wait about 65 seconds"):
                text = restart_reply_text()
        self.assertIn("Restarting the stack", text)
        self.assertIn("gpt-4o", text)
        self.assertIsNotNone(data)


if __name__ == "__main__":
    unittest.main()
