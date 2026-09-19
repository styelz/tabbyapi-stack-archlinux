"""Jailed Code-mode shell: docker required, workspace bound at /work."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import codebox
from ui import shell
from ui import workspace


class ShellJailTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        workspace.set_workspaces_dir(Path(self._tmp.name))

    def tearDown(self):
        workspace.set_workspaces_dir(None)
        self._tmp.cleanup()

    def test_missing_docker_is_a_hard_error(self):
        with mock.patch("ui.codebox.shutil.which", return_value=None):
            with self.assertRaises(codebox.CodeboxError) as ctx:
                codebox.docker_bin()
            self.assertIn("docker", str(ctx.exception).lower())
            with self.assertRaises(shell.ShellError) as sh_ctx:
                shell.docker_bin()
            self.assertIn("docker", str(sh_ctx.exception).lower())

    def test_jail_binds_workspace_at_work(self):
        root = workspace.workspace_root("alice", "c", create=True, box=False)
        with mock.patch("ui.codebox.shutil.which", return_value="/usr/bin/docker"):
            with mock.patch("ui.codebox.os.getuid", return_value=1000):
                with mock.patch("ui.codebox.os.getgid", return_value=1000):
                    cmd = codebox.run_args("alice", "c", root)
        self.assertEqual(cmd[0], "/usr/bin/docker")
        self.assertIn("run", cmd)
        vols = [cmd[i + 1] for i, item in enumerate(cmd) if item == "-v"]
        self.assertIn(f"{root.resolve()}:/work", vols)
        self.assertTrue(any(item.endswith(":/etc/passwd:ro") for item in vols))
        self.assertTrue(any(item.endswith(":/etc/tabby-git-credentials:ro") for item in vols))
        self.assertNotIn("none", cmd)
        self.assertNotIn("no-new-privileges", cmd)
        self.assertNotIn("/var/run/docker.sock", " ".join(cmd))
        self.assertNotIn("-p", cmd)
        self.assertIn("GIT_CONFIG_VALUE_0=store --file=/etc/tabby-git-credentials", cmd)
        self.assertIn("--cap-drop", cmd)
        self.assertIn("ALL", cmd)
        self.assertIn("--cap-add", cmd)
        self.assertIn("SETUID", cmd)
        self.assertNotIn("SYS_ADMIN", cmd)
        self.assertIn(f"PATH={codebox.WORK_PATH}", cmd)
        self.assertIn("2g", cmd)
        self.assertIn("tabbyapi-stack-code:local", cmd)
        self.assertIn(
            "alice:x:1000:1000:alice:/work:/bin/bash",
            (root.parent / "c.codebox" / "passwd").read_text(),
        )
        self.assertIn(
            "sudo:x:27:alice",
            (root.parent / "c.codebox" / "group").read_text(),
        )

    def test_shell_timeout_is_clamped(self):
        self.assertEqual(codebox.SHELL_TIMEOUT_S, 600)
        self.assertEqual(codebox.SHELL_TIMEOUT_MAX_S, 1200)
        self.assertGreaterEqual(codebox.SHELL_MAX_BYTES, 256 * 1024)

    def test_sandbox_outdated_when_network_is_none(self):
        def fake_inspect(_name, fmt):
            if "NetworkMode" in fmt:
                return "none"
            return None
        with mock.patch.object(codebox, "_inspect_field", side_effect=fake_inspect):
            with mock.patch.object(codebox, "_stale_container", return_value=False):
                with mock.patch.object(codebox, "_missing_git_creds_mount", return_value=False):
                    self.assertTrue(codebox._sandbox_outdated("tabby-code-alice-c"))


if __name__ == "__main__":
    unittest.main()
