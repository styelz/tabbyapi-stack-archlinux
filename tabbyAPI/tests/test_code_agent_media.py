"""Code-mode generate/inspect tools and jail-facing Shell args."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import code_agent
from ui import codebox
from ui import workspace


class CodeToolSpecTests(unittest.TestCase):
    def test_agent_includes_media_and_shell(self):
        names = [spec.function.name for spec in code_agent.code_tool_specs("agent")]
        for name in (
            "Shell",
            "InspectMedia",
            "GenerateImage",
            "GenerateAudio",
            "GenerateVideo",
        ):
            self.assertIn(name, names)

    def test_ask_is_readonly_plus_inspect(self):
        names = [spec.function.name for spec in code_agent.code_tool_specs("ask")]
        self.assertEqual(set(names), {"Read", "List", "Grep", "Glob", "InspectMedia"})

    def test_prompts_mention_audio_video_and_installs(self):
        self.assertIn("GenerateAudio", code_agent.CODE_SYSTEM)
        self.assertIn("videos/clip.mp4", code_agent.PLAN_SYSTEM)
        self.assertIn("audio/track.wav", code_agent.PLAN_SYSTEM)
        self.assertIn("sudo apt-get install", code_agent.CODE_SYSTEM)
        self.assertIn("/work/.venv", code_agent.CODE_SYSTEM)


class GenerateToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        workspace.set_workspaces_dir(Path(self._tmp.name))

    def tearDown(self):
        workspace.set_workspaces_dir(None)
        self._tmp.cleanup()

    def test_ask_refuses_generate(self):
        label, result, change = code_agent.execute_tool(
            "alice",
            "c1",
            "GenerateImage",
            {"prompt": "a harbor at dusk", "output_path": "images/hero.png"},
            agent="ask",
        )
        self.assertEqual(label, "Tool error")
        self.assertIn("read-only", result)
        self.assertEqual(change, {})

    def test_generate_image_binds_this_chat(self):
        job = mock.Mock()
        job.items = [mock.Mock(output_path="images/hero.png")]
        with mock.patch(
            "images.jobs.queue_code_media_job",
            return_value=(job, "coding"),
        ) as queued:
            label, result, change = code_agent.execute_tool(
                "alice",
                "c1",
                "GenerateImage",
                {"prompt": "qwen-image: Cafe logo", "output_path": "images/logo.png"},
                agent="agent",
            )
        queued.assert_called_once()
        kwargs = queued.call_args.kwargs
        self.assertEqual(kwargs["owner"], "alice")
        self.assertEqual(kwargs["chat_id"], "c1")
        self.assertEqual(kwargs["items"][0]["output_path"], "images/logo.png")
        self.assertTrue(kwargs["items"][0]["prompt"].lower().startswith("qwen-image"))
        self.assertEqual(label, "Queuing media")
        self.assertIn("images/hero.png", result)
        self.assertEqual(change["kind"], "generate")
        self.assertIn("images/hero.png", change["images"])

    def test_generate_does_not_attach_other_chat(self):
        from images import jobs

        other = jobs.McpImageJob(
            id="other",
            items=[jobs.McpImageItem(prompt="x", output_path="images/a.png")],
            restore=True,
            api_base="",
            wait_text="",
            wait_s=0,
            status="coding",
            owner="alice",
            chat_id="other-chat",
        )
        with mock.patch.object(jobs, "active_mcp_image_job", return_value=other):
            with mock.patch.object(jobs, "abandon_foreign_coding_job", return_value=False):
                job, status = jobs.queue_code_media_job(
                    owner="alice",
                    chat_id="this-chat",
                    items=[{"prompt": "a tree", "output_path": "images/tree.png"}],
                )
        self.assertEqual(status, "busy")
        self.assertEqual(job.id, "other")

    def test_generate_appends_to_same_chat_coding_job(self):
        from images import jobs

        existing = jobs.McpImageJob(
            id="same",
            items=[jobs.McpImageItem(prompt="hero", output_path="images/hero.png")],
            restore=True,
            api_base="",
            wait_text="",
            wait_s=0,
            status="coding",
            phase="writing_code",
            owner="alice",
            chat_id="this-chat",
        )
        with mock.patch.object(jobs, "active_mcp_image_job", return_value=existing):
            with mock.patch.object(jobs, "abandon_foreign_coding_job", return_value=False):
                with mock.patch.object(jobs, "refresh_job_wait"):
                    with mock.patch.object(jobs, "_signal"):
                        job, status = jobs.queue_code_media_job(
                            owner="alice",
                            chat_id="this-chat",
                            items=[{"prompt": "a logo", "output_path": "images/logo.png"}],
                        )
        self.assertEqual(status, "appended")
        self.assertEqual(job.id, "same")
        self.assertEqual(len(job.items), 2)
        self.assertEqual(job.items[1].output_path, "images/logo.png")


class InspectAndShellTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        workspace.set_workspaces_dir(Path(self._tmp.name))
        workspace.workspace_root("alice", "c1", create=True, box=False)

    def tearDown(self):
        workspace.set_workspaces_dir(None)
        self._tmp.cleanup()

    def test_inspect_media_parses_ffprobe(self):
        dest = workspace.write_text("alice", "c1", "videos/clip.mp4", "not-really-mp4")
        self.assertEqual(dest, "videos/clip.mp4")
        payload = {
            "format": {"duration": "3.2", "format_name": "mov,mp4,m4a"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 640}
            ],
        }
        with mock.patch("ui.codebox.run_shell", return_value=(0, json.dumps(payload))):
            label, result, change = code_agent.execute_tool(
                "alice", "c1", "InspectMedia", {"path": "videos/clip.mp4"}
            )
        self.assertIn("Inspecting", label)
        self.assertIn("640x640", result)
        self.assertIn("3.2", result)
        self.assertEqual(change, {})

    def test_shell_passes_timeout_and_records_change(self):
        with mock.patch("ui.codebox.run_shell", return_value=(0, "ok")) as ran:
            label, result, change = code_agent.execute_tool(
                "alice",
                "c1",
                "Shell",
                {"command": "python3 -c 'print(1)'", "timeout": 90},
                agent="agent",
            )
        ran.assert_called_once()
        self.assertEqual(ran.call_args.kwargs.get("timeout"), 90.0)
        self.assertEqual(label, "Running command")
        self.assertEqual(change["kind"], "shell")
        self.assertIn("PATH=", " ".join(codebox._sandbox_env_pairs("alice")))
        self.assertIn("/work/.venv/bin", codebox.WORK_PATH)


if __name__ == "__main__":
    unittest.main()
