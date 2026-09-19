"""Code-mode file history: prompt-run checkpoints and restore-to-point."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui import workspace


class RestoreRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        workspace.set_workspaces_dir(Path(self.tmp.name))
        workspace.workspace_root("u", "w", create=True, box=False)

    def tearDown(self):
        workspace.set_workspaces_dir(None)
        self.tmp.cleanup()

    def _run(self, run_id: str):
        return workspace.push_history_run(run_id)

    def test_restore_one_run_puts_file_back(self):
        workspace.write_text("u", "w", "app.js", "v1")
        token = self._run("run-a")
        try:
            workspace.write_text("u", "w", "app.js", "v2")
        finally:
            workspace.pop_history_run(token)
        self.assertEqual(workspace.read_text("u", "w", "app.js"), "v2")
        result = workspace.restore_run("u", "w", "run-a")
        self.assertEqual(result["restored"], ["app.js"])
        self.assertEqual(workspace.read_text("u", "w", "app.js"), "v1")

    def test_restore_deletes_file_created_in_run(self):
        token = self._run("run-b")
        try:
            workspace.write_text("u", "w", "fresh.js", "hello")
        finally:
            workspace.pop_history_run(token)
        result = workspace.restore_run("u", "w", "run-b")
        self.assertEqual(result["deleted"], ["fresh.js"])
        with self.assertRaises(FileNotFoundError):
            workspace.read_text("u", "w", "fresh.js")

    def test_restore_multiple_runs_uses_oldest_snapshot(self):
        workspace.write_text("u", "w", "app.js", "v1")
        token = self._run("run-a")
        try:
            workspace.write_text("u", "w", "app.js", "v2")
        finally:
            workspace.pop_history_run(token)
        token = self._run("run-b")
        try:
            workspace.write_text("u", "w", "app.js", "v3")
            workspace.write_text("u", "w", "extra.js", "new")
        finally:
            workspace.pop_history_run(token)
        result = workspace.restore_run(
            "u", "w", run_ids=["run-a", "run-b"]
        )
        self.assertEqual(set(result["restored"]), {"app.js"})
        self.assertEqual(set(result["deleted"]), {"extra.js"})
        self.assertEqual(workspace.read_text("u", "w", "app.js"), "v1")
        with self.assertRaises(FileNotFoundError):
            workspace.read_text("u", "w", "extra.js")

    def test_restore_since_timestamp(self):
        workspace.write_text("u", "w", "app.js", "v1")
        versions = workspace.list_history("u", "w", "app.js")
        self.assertEqual(versions, [])
        token = self._run("run-c")
        try:
            workspace.write_text("u", "w", "app.js", "v2")
        finally:
            workspace.pop_history_run(token)
        after = workspace.list_history("u", "w", "app.js")
        self.assertTrue(after)
        since = int(after[0]["ts"])
        workspace.write_text("u", "w", "app.js", "v3")
        result = workspace.restore_run("u", "w", since_ts=since)
        self.assertIn("app.js", result["restored"])
        self.assertEqual(workspace.read_text("u", "w", "app.js"), "v1")

    def test_sticky_created_flag_does_not_delete_older_file(self):
        token = self._run("run-a")
        try:
            workspace.write_text("u", "w", "app.js", "v1")
        finally:
            workspace.pop_history_run(token)
        token = self._run("run-b")
        try:
            workspace.write_text("u", "w", "app.js", "v2")
        finally:
            workspace.pop_history_run(token)
        result = workspace.restore_run(
            "u", "w", "run-b", created=["app.js"]
        )
        self.assertEqual(result["restored"], ["app.js"])
        self.assertEqual(result["deleted"], [])
        self.assertEqual(workspace.read_text("u", "w", "app.js"), "v1")

    def test_list_history_hides_create_markers(self):
        workspace.write_text("u", "w", "only.js", "one")
        self.assertEqual(workspace.list_history("u", "w", "only.js"), [])

    def test_restore_requires_run_or_since(self):
        with self.assertRaises(ValueError):
            workspace.restore_run("u", "w", "")
