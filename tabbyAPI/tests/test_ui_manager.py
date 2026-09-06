import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ui import manager


class UiManagerTests(unittest.TestCase):
    def test_journalctl_cmd_follows_user_units(self):
        cmd = manager.journalctl_cmd(follow=True, lines=0)
        self.assertEqual(cmd[0], "journalctl")
        self.assertIn("--user", cmd)
        self.assertIn("-f", cmd)
        self.assertIn("--since", cmd)
        self.assertIn("now", cmd)
        self.assertNotIn("-n", cmd)
        self.assertIn("tabbyapi", cmd)
        self.assertIn("comfyui", cmd)

    def test_journalctl_cmd_history_still_uses_line_count(self):
        cmd = manager.journalctl_cmd(follow=False, lines=300)
        self.assertIn("-n", cmd)
        self.assertEqual(cmd[cmd.index("-n") + 1], "300")
        self.assertNotIn("-f", cmd)

    def test_ui_access_lines_are_detected(self):
        self.assertTrue(
            manager.is_ui_access_line(
                "2026-08-24T04:50:18+10:00 archy.local python[106135]: "
                "2026-08-24 04:50:18.835 INFO:     36.255.114.172:0 - "
                '"GET /v1/ui/assets/status.js HTTP/1.1" 200'
            )
        )
        self.assertTrue(
            manager.is_ui_access_line(
                "Aug 24 06:08:12 archy.local python[122943]: "
                "2026-08-24 06:08:12.392 INFO:     36.255.114.172:0 - "
                '"GET /v1/ui/status HTTP/1.1" 200'
            )
        )
        self.assertTrue(manager.is_ui_access_line('"POST /v1/ui/restart HTTP/1.1" 200'))
        self.assertTrue(manager.is_ui_access_line('"GET /v1/ui/logs/history?lines=300 HTTP/1.1" 200'))
        self.assertTrue(manager.is_ui_access_line('"GET /ui/status HTTP/1.1" 200'))
        self.assertTrue(manager.is_ui_access_line('"GET /openai/v1/ui/status HTTP/1.1" 200'))
        self.assertFalse(manager.is_ui_access_line('"GET /v1/chat/completions HTTP/1.1" 200'))
        self.assertFalse(manager.is_ui_access_line('"GET /health HTTP/1.1" 200'))
        self.assertFalse(manager.is_ui_access_line("Model loaded: qwen"))
        self.assertFalse(
            manager.is_ui_access_line("Management UI: http://127.0.0.1:5000/v1/ui")
        )

    def test_journalctl_history_drops_ui_access(self):
        previous = list(manager.PROCESS_LOGS)
        mixed = [
            "keep me",
            '"GET /v1/ui/status HTTP/1.1" 200',
            "also keep",
        ]
        try:
            with mock.patch.object(manager.shutil, "which", return_value=None):
                manager.PROCESS_LOGS.clear()
                manager.PROCESS_LOGS.extend(mixed)
                lines = manager.journalctl_history(10)
            self.assertEqual(lines, ["keep me", "also keep"])
        finally:
            manager.PROCESS_LOGS.clear()
            manager.PROCESS_LOGS.extend(previous)

    def test_journalctl_history_overfetches_then_filters(self):
        ui = '"GET /v1/ui/status HTTP/1.1" 200'
        stdout = "\n".join([ui, "real log", ui, "another"])
        completed = mock.Mock(returncode=0, stdout=stdout)
        with mock.patch.object(manager.shutil, "which", return_value="/usr/bin/journalctl"):
            with mock.patch.object(manager.subprocess, "run", return_value=completed) as run:
                lines = manager.journalctl_history(2)
        self.assertEqual(lines, ["real log", "another"])
        cmd = run.call_args[0][0]
        self.assertGreater(int(cmd[cmd.index("-n") + 1]), 2)

    def test_sanitize_chat_strips_tools_and_injects_system(self):
        payload = manager.sanitize_chat_payload(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "hello",
                        "tool_calls": [{"function": {"name": "Write"}}],
                    }
                ],
                "tools": [{"function": {"name": "Write"}}],
                "stream": True,
            }
        )
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("do not write", payload["messages"][0]["content"].lower())
        self.assertEqual(payload["messages"][-1]["content"], "hello")
        self.assertNotIn("tool_calls", payload["messages"][-1])

    def test_sanitize_rejects_empty_messages(self):
        with self.assertRaises(ValueError):
            manager.sanitize_chat_payload({"messages": []})

    def test_sanitize_code_requires_chat_id_and_names_workspace(self):
        with self.assertRaises(ValueError):
            manager.sanitize_code_payload({"messages": [{"role": "user", "content": "hi"}]})
        payload = manager.sanitize_code_payload(
            {"messages": [{"role": "user", "content": "hi"}], "chat_id": "w1"}
        )
        self.assertEqual(payload["chat_id"], "w1")
        self.assertEqual(payload["mode"], "code")
        self.assertIn("workspace", payload["messages"][0]["content"].lower())
        self.assertIn("emit every Write and StrReplace in one", payload["messages"][0]["content"])
        self.assertNotIn("per-chat project", payload["messages"][0]["content"])

    def test_sanitize_code_appends_workspace_file_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            from ui import workspace

            workspace.set_workspaces_dir(folder)
            try:
                workspace.write_text("u", "w1", "index.html", "<p>hi</p>")
                payload = manager.sanitize_code_payload(
                    {"messages": [{"role": "user", "content": "hi"}], "chat_id": "w1"},
                    username="u",
                )
            finally:
                workspace.set_workspaces_dir(None)
        self.assertIn("index.html", payload["messages"][0]["content"])
        self.assertIn("Workspace files", payload["messages"][0]["content"])

    def test_sanitize_code_plan_injects_contract(self):
        from ui import code_agent

        payload = manager.sanitize_code_payload(
            {
                "messages": [{"role": "user", "content": "design a site"}],
                "chat_id": "w1",
                "agent": "plan",
            }
        )
        self.assertEqual(payload["agent"], "plan")
        self.assertIn("Plan mode", payload["messages"][0]["content"])
        self.assertIn(code_agent.PLAN_CONTRACT_MARK, payload["messages"][-1]["content"])
        self.assertTrue(payload["messages"][-1]["content"].startswith("design a site"))

    def test_sanitize_code_build_injects_contract(self):
        from ui import code_agent

        quoted = (
            f"{code_agent.BUILD_PROMPT}\n\n<approved_plan>\n"
            "## Assets\n- images/liner.png\n</approved_plan>"
        )
        payload = manager.sanitize_code_payload(
            {
                "messages": [{"role": "user", "content": quoted}],
                "chat_id": "w1",
                "agent": "agent",
            }
        )
        self.assertEqual(payload["agent"], "agent")
        self.assertIn(code_agent.BUILD_CONTRACT_MARK, payload["messages"][-1]["content"])
        self.assertNotIn(code_agent.PLAN_CONTRACT_MARK, payload["messages"][-1]["content"])
        self.assertTrue(payload["messages"][-1]["content"].startswith(code_agent.BUILD_PROMPT))

    def test_sanitize_code_ask_skips_plan_contract(self):
        from ui import code_agent

        payload = manager.sanitize_code_payload(
            {
                "messages": [{"role": "user", "content": "what files are here?"}],
                "chat_id": "w1",
                "agent": "ask",
            }
        )
        self.assertEqual(payload["agent"], "ask")
        self.assertIn("answering questions", payload["messages"][0]["content"])
        self.assertNotIn(code_agent.PLAN_CONTRACT_MARK, payload["messages"][-1]["content"])
        self.assertNotIn(code_agent.BUILD_CONTRACT_MARK, payload["messages"][-1]["content"])
        self.assertEqual(payload["messages"][-1]["content"], "what files are here?")

    def test_sanitize_code_ask_images_injects_mode_hint(self):
        from ui import code_agent

        payload = manager.sanitize_code_payload(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "create 2 images of varying width and height",
                    }
                ],
                "chat_id": "w1",
                "agent": "ask",
            }
        )
        blob = payload["messages"][-1]["content"]
        self.assertIn(code_agent.MODE_HINT_MARK, blob)
        self.assertIn("needs Agent or Chat", blob)
        self.assertTrue(blob.startswith("create 2 images"))

    def test_readonly_mode_targets(self):
        from ui import code_agent

        self.assertEqual(
            code_agent.readonly_mode_targets(
                "ask", "create 2 images of varying width and height"
            ),
            ("agent", "chat"),
        )
        self.assertEqual(
            code_agent.readonly_mode_targets("ask", "what files are here?"),
            (),
        )
        self.assertEqual(
            code_agent.readonly_mode_targets("plan", "design a site"),
            (),
        )
        self.assertEqual(
            code_agent.readonly_mode_targets("plan", "generate an image of a harbor"),
            ("chat",),
        )

    def test_code_tool_round_cap_drops_inspect_before_writes(self):
        def write_round(idx):
            return [
                {
                    "role": "assistant",
                    "content": f"plan {idx}",
                    "tool_calls": [
                        {
                            "id": f"w{idx}",
                            "function": {
                                "name": "Write",
                                "arguments": '{"path":"f.html"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": f"w{idx}", "content": "wrote"},
            ]

        def read_round(idx):
            return [
                {
                    "role": "assistant",
                    "content": f"check {idx}",
                    "tool_calls": [
                        {
                            "id": f"r{idx}",
                            "function": {
                                "name": "Read",
                                "arguments": '{"path":"f.html"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": f"r{idx}", "content": "<html>"},
            ]

        messages = [{"role": "user", "content": "edit the page"}]
        for i in range(6):
            messages.extend(write_round(i))
            messages.extend(read_round(i))
        capped = manager._cap_tool_rounds(messages, limit=8)
        writes = [
            item
            for item in capped
            if item.get("role") == "assistant"
            and (item.get("tool_calls") or [{}])[0].get("function", {}).get("name") == "Write"
        ]
        reads = [
            item
            for item in capped
            if item.get("role") == "assistant"
            and (item.get("tool_calls") or [{}])[0].get("function", {}).get("name") == "Read"
        ]
        self.assertEqual(len(writes), 6)
        self.assertEqual(len(reads), 2)

    def test_update_missing_script(self):
        missing = Path("/tmp/does-not-exist-tabby-update.sh")
        with mock.patch.object(manager, "STACK_ROOT", missing.parent):
            with mock.patch.object(Path, "is_file", return_value=False):
                result = manager.start_stack_update()
        self.assertFalse(result["ok"])
        self.assertIn("update.sh", result["message"])

    def test_git_update_starts_outside_tabbyapi_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "update.sh").write_text("#!/bin/bash\nexit 0\n")
            with mock.patch.object(manager, "STACK_ROOT", root):
                with mock.patch.object(manager, "update_job_running", return_value=False):
                    with mock.patch.object(manager.shutil, "which", return_value="/usr/bin/systemd-run"):
                        with mock.patch.object(manager.subprocess, "run") as run:
                            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                            with mock.patch.object(manager.subprocess, "Popen") as popen:
                                result = manager.start_stack_update(full=False)
            popen.assert_not_called()
            cmds = [c[0][0] for c in run.call_args_list]
            spawned = next(cmd for cmd in cmds if cmd and cmd[0] == "/usr/bin/systemd-run")
            self.assertIn("--git", spawned)
            self.assertNotIn("--no-restart", spawned)
            self.assertNotIn("--all", spawned)
            self.assertTrue(result["ok"])
            self.assertTrue(result["restarting"])
            self.assertNotIn("ask_restart", result)
            self.assertIn("git update", result["message"].lower())

    def test_full_update_starts_outside_tabbyapi_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "update.sh").write_text("#!/bin/bash\nexit 0\n")
            with mock.patch.object(manager, "STACK_ROOT", root):
                with mock.patch.object(manager, "update_job_running", return_value=False):
                    with mock.patch.object(manager.shutil, "which", return_value="/usr/bin/systemd-run"):
                        with mock.patch.object(manager.subprocess, "run") as run:
                            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                            with mock.patch.object(manager.subprocess, "Popen") as popen:
                                result = manager.start_stack_update(full=True)
            popen.assert_not_called()
            cmds = [c[0][0] for c in run.call_args_list]
            self.assertTrue(any(cmd and cmd[0] == "/usr/bin/systemd-run" for cmd in cmds))
            spawned = next(cmd for cmd in cmds if cmd and cmd[0] == "/usr/bin/systemd-run")
            self.assertIn("--user", spawned)
            self.assertIn("--collect", spawned)
            self.assertIn("--all", spawned)
            self.assertIn("--restart", spawned)
            self.assertTrue(result["ok"])
            self.assertNotIn("ask_restart", result)
