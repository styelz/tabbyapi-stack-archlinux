import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


UPDATE_SH = Path(__file__).resolve().parents[2] / "update.sh"


class UpdateShRestartOptionTests(unittest.TestCase):
    def test_restart_flags_are_wired(self):
        src = UPDATE_SH.read_text()
        self.assertIn("[--restart|--no-restart]", src)
        self.assertIn("--restart) RESTART_API=1; shift ;;", src)
        self.assertIn("--no-restart) RESTART_API=0; shift ;;", src)
        self.assertIn("args+=(--restart)", src)
        self.assertIn("args+=(--no-restart)", src)
        self.assertIn('if [[ "$RESTART_API" == 1 ]]; then', src)
        self.assertIn("TABBY_UPDATE_RESTART", src)

    def test_git_update_always_offers_restart_button(self):
        src = UPDATE_SH.read_text()
        self.assertIn('--yes-label "Restart"', src)
        self.assertIn('--no-label "Skip"', src)
        self.assertIn("Already up to date. Restart tabbyapi anyway", src)
        self.assertIn("if ask_restart_api; then", src)
        self.assertIn("git_should_auto_restart", src)
        self.assertIn("Code sandbox image already present; skipping rebuild", src)
        self.assertIn("write_restart_prompt_json", src)
        self.assertIn("tabby-update-prompt.json", src)
        self.assertIn("restart_prompt_text", src)
        self.assertNotIn("tabbyapi is not running, so it was not restarted.", src)
        self.assertNotIn(
            'if [[ "$pulled" -eq 0 ]]; then\n    ui_msg "Update git" "Already up to date. The API was not restarted.',
            src,
        )

    def test_origin_wrappers_win_when_pull_changes_them(self):
        src = UPDATE_SH.read_text()
        self.assertIn("Keeping origin/", src)
        self.assertIn("Restored local $wrap (unchanged on origin/", src)
        self.assertNotIn("Restored local install/update scripts", src)

    def test_divergent_tracked_files_do_not_abort(self):
        src = UPDATE_SH.read_text()
        self.assertIn("backup_divergent_tracked", src)
        self.assertIn(".tabby-update-backup/", src)
        self.assertNotIn("has local edits that are not on origin", src)
        self.assertNotIn("has local edits in tracked files (not just line endings)", src)
        self.assertNotIn("Commit, stash, or restore them, then re-run", src)


class UpdateShFfPullTests(unittest.TestCase):
    def test_pull_backs_up_divergent_tracked_source_and_fast_forwards(self):
        script = UPDATE_SH.read_text()
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test",
            "GIT_TERMINAL_PROMPT": "0",
            "TABBY_INSTALL_VERBOSE": "1",
        }

        def git(cwd, *args):
            subprocess.check_call(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=git_env)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            origin = tmp / "origin.git"
            live = tmp / "live"
            origin.mkdir()
            git(origin, "init", "--bare")

            seed = tmp / "seed"
            seed.mkdir()
            git(seed, "init")
            git(seed, "config", "user.email", "test@test")
            git(seed, "config", "user.name", "test")
            (seed / "install.sh").write_text("#!/bin/bash\necho install\n")
            (seed / "tabbyAPI").mkdir()
            (seed / "tabbyAPI" / "main.py").write_text("print('ok')\n")
            (seed / "tabbyAPI" / "phrase.py").write_text("v1\n")
            (seed / "update.sh").write_text(script)
            git(seed, "add", "install.sh", "update.sh", "tabbyAPI/main.py", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(origin))
            git(seed, "push", "-u", "origin", "HEAD:main")

            git(tmp, "clone", str(origin), str(live))
            git(live, "config", "user.email", "test@test")
            git(live, "config", "user.name", "test")
            os.chmod(live / "update.sh", 0o755)

            (seed / "tabbyAPI" / "phrase.py").write_text("origin-v2\n")
            git(seed, "add", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "origin newer")
            git(seed, "push", "origin", "HEAD:main")

            (live / "tabbyAPI" / "phrase.py").write_text("frankenstein-not-on-origin\n")
            proc = subprocess.run(
                ["bash", str(live / "update.sh"), "--git", "--no-restart"],
                cwd=live,
                env=git_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
            log = (live / "tabby-update.log").read_text() if (live / "tabby-update.log").exists() else proc.stdout
            self.assertEqual(proc.returncode, 0, log)
            self.assertEqual((live / "tabbyAPI" / "phrase.py").read_text(), "origin-v2\n")
            backups = list((live / ".tabby-update-backup").glob("*/tabbyAPI/phrase.py"))
            self.assertTrue(backups, log)
            self.assertEqual(backups[0].read_text(), "frankenstein-not-on-origin\n")
            self.assertIn("Moving tracked copies that are not on origin/main aside", log)
            prompt = live / "tabby-update-prompt.json"
            self.assertTrue(prompt.is_file(), log)
            data = json.loads(prompt.read_text())
            self.assertEqual(data["title"], "Restart API?")
            self.assertEqual(data["yes_label"], "Restart")
            self.assertEqual(data["no_label"], "Skip")
            self.assertTrue(data["pulled"])
            self.assertTrue(data.get("text"))


INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"


class InstallShHeadlessUpdateTests(unittest.TestCase):
    def test_text_gauge_requires_writable_tty(self):
        src = INSTALL_SH.read_text()
        self.assertIn("tty_writable()", src)
        self.assertIn('if tty_writable; then\n    GAUGE_MODE="text"', src)
        self.assertNotIn('if [[ -c /dev/tty ]]; then\n    GAUGE_MODE="text"', src)
        self.assertIn(">/dev/tty 2>/dev/null || true", src)
