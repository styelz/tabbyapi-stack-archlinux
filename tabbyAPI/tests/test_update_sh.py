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
        self.assertIn("use_scrollbar = OFF", src)
        self.assertIn("position_indicator_color = (WHITE,WHITE,ON)", src)
        self.assertIn("write_dialogrc", src)

    def test_git_update_always_offers_restart_button(self):
        src = UPDATE_SH.read_text()
        self.assertIn('--yes-label "Restart"', src)
        self.assertIn('--no-label "Skip"', src)
        self.assertIn("Already up to date. Restart tabbyapi anyway", src)
        self.assertIn("if ask_restart_api; then", src)
        self.assertIn("git_should_auto_restart", src)
        self.assertIn("if [[ ! -t 1 ]]; then", src)
        self.assertNotIn('if [[ ! -t 1 && ! -c /dev/tty ]]; then', src)
        self.assertIn("[[ ! -t 0 && ! -t 1 ]]", src)
        self.assertNotIn("Building Code sandbox image", src)
        self.assertNotIn("ensure_codebox_image", src)
        self.assertIn("write_restart_prompt_json", src)
        self.assertIn("tabby-update-prompt.json", src)
        self.assertIn("needs_restart", src)
        self.assertIn("TABBY_PROMPT_NEEDS", src)
        self.assertIn("git_should_auto_restart && default_yes=1", src)
        self.assertIn("path_needs_saver_restart", src)
        self.assertIn("tabbyAPI/deploy/arch/tabby-saver.py) return 1 ;;", src)
        self.assertIn("install_tabby_saver", src)
        self.assertIn("export_saver_changed", src)
        self.assertIn("TABBY_SAVER_CHANGED", src)
        self.assertIn("restart_stack.py", src)
        self.assertIn("--saver-if-updated", src)
        self.assertIn("systemctl restart tabby-saver", src)
        self.assertIn("saver_files=", src)
        self.assertIn("fetch --progress origin", src)
        self.assertIn("log_run()", src)
        self.assertIn("tr '\\r' '\\n'", src)
        self.assertIn("stdbuf -oL -eL", src)
        self.assertIn('kill "$GAUGE_PID"', src)
        self.assertIn("Applying deps and restart", src)
        self.assertIn("update_branch()", src)
        self.assertIn("Prefer the branch already checked out", src)
        self.assertIn("sidecar/*.py|sidecar/*.sh) return 0 ;;", src)
        self.assertIn("tabbyAPI/watch_api.py|tabbyAPI/deploy/arch/run-api.sh) return 0 ;;", src)
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
            self.assertIn("needs_restart", data)
            self.assertIn("restart_files", data)
            self.assertIn("tabbyAPI/phrase.py", data.get("restart_files") or [])
            self.assertTrue(data["needs_restart"])
            self.assertTrue(data["default_yes"])

    def test_git_update_restarts_screensaver_without_api_python(self):
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
            (seed / "tabbyAPI" / "deploy" / "arch").mkdir(parents=True)
            (seed / "tabbyAPI" / "main.py").write_text("print('ok')\n")
            (seed / "tabbyAPI" / "deploy" / "arch" / "tabby-saver.py").write_text("print('saver-v1')\n")
            (seed / "tabbyAPI" / "deploy" / "arch" / "tabby-saver.service").write_text("[Service]\nExecStart=/usr/bin/python\n")
            (seed / "update.sh").write_text(script)
            git(
                seed,
                "add",
                "install.sh",
                "update.sh",
                "tabbyAPI/main.py",
                "tabbyAPI/deploy/arch/tabby-saver.py",
                "tabbyAPI/deploy/arch/tabby-saver.service",
            )
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(origin))
            git(seed, "push", "-u", "origin", "HEAD:main")

            git(tmp, "clone", str(origin), str(live))
            git(live, "config", "user.email", "test@test")
            git(live, "config", "user.name", "test")
            os.chmod(live / "update.sh", 0o755)

            (seed / "tabbyAPI" / "deploy" / "arch" / "tabby-saver.py").write_text("print('saver-v2')\n")
            git(seed, "add", "tabbyAPI/deploy/arch/tabby-saver.py")
            git(seed, "commit", "-m", "screensaver newer")
            git(seed, "push", "origin", "HEAD:main")

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
            self.assertEqual(
                (live / "tabbyAPI" / "deploy" / "arch" / "tabby-saver.py").read_text(),
                "print('saver-v2')\n",
            )
            self.assertIn("Screensaver files changed", log)
            self.assertIn("tabbyAPI/deploy/arch/tabby-saver.py", log)
            self.assertIn("saver_files=1", log)
            prompt = json.loads((live / "tabby-update-prompt.json").read_text())
            self.assertNotIn("tabbyAPI/deploy/arch/tabby-saver.py", prompt.get("restart_files") or [])

    def test_pull_stays_on_checked_out_origin_branch(self):
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
            (seed / "tabbyAPI" / "phrase.py").write_text("main-v1\n")
            (seed / "update.sh").write_text(script)
            git(seed, "add", "install.sh", "update.sh", "tabbyAPI/main.py", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(origin))
            git(seed, "push", "-u", "origin", "HEAD:main")
            git(seed, "checkout", "-b", "vanilla-tapi")
            (seed / "tabbyAPI" / "phrase.py").write_text("feature-v1\n")
            git(seed, "add", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "feature seed")
            git(seed, "push", "-u", "origin", "HEAD:vanilla-tapi")

            git(tmp, "clone", "-b", "vanilla-tapi", str(origin), str(live))
            git(live, "config", "user.email", "test@test")
            git(live, "config", "user.name", "test")
            os.chmod(live / "update.sh", 0o755)

            git(seed, "checkout", "main")
            (seed / "tabbyAPI" / "phrase.py").write_text("main-should-not-win\n")
            git(seed, "add", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "main newer")
            git(seed, "push", "origin", "HEAD:main")

            git(seed, "checkout", "vanilla-tapi")
            (seed / "tabbyAPI" / "phrase.py").write_text("feature-v2\n")
            git(seed, "add", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "feature newer")
            git(seed, "push", "origin", "HEAD:vanilla-tapi")

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
            branch = subprocess.check_output(
                ["git", "-C", str(live), "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
            ).strip()
            self.assertEqual(branch, "vanilla-tapi", log)
            self.assertEqual((live / "tabbyAPI" / "phrase.py").read_text(), "feature-v2\n")
            self.assertNotIn("Switching tabbyapi-stack from vanilla-tapi to main", log)

    def test_pull_falls_back_to_main_for_local_only_branch(self):
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
            (seed / "tabbyAPI" / "phrase.py").write_text("main-v1\n")
            (seed / "update.sh").write_text(script)
            git(seed, "add", "install.sh", "update.sh", "tabbyAPI/main.py", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "seed")
            git(seed, "remote", "add", "origin", str(origin))
            git(seed, "push", "-u", "origin", "HEAD:main")

            git(tmp, "clone", str(origin), str(live))
            git(live, "config", "user.email", "test@test")
            git(live, "config", "user.name", "test")
            git(live, "checkout", "-b", "rewrite")
            os.chmod(live / "update.sh", 0o755)

            (seed / "tabbyAPI" / "phrase.py").write_text("main-v2\n")
            git(seed, "add", "tabbyAPI/phrase.py")
            git(seed, "commit", "-m", "main newer")
            git(seed, "push", "origin", "HEAD:main")

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
            branch = subprocess.check_output(
                ["git", "-C", str(live), "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
            ).strip()
            self.assertEqual(branch, "main", log)
            self.assertEqual((live / "tabbyAPI" / "phrase.py").read_text(), "main-v2\n")
            self.assertIn("Switching tabbyapi-stack from rewrite to main", log)


INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"


class InstallShHeadlessUpdateTests(unittest.TestCase):
    def test_update_restarts_active_screensaver_when_files_changed(self):
        src = INSTALL_SH.read_text()
        self.assertIn("TABBY_SAVER_CHANGED", src)
        self.assertIn("Restarting tabby-saver (screensaver files changed)", src)
        self.assertIn("systemctl restart tabby-saver", src)
        self.assertIn("systemctl start tabby-saver", src)

    def test_dialogrc_hides_unused_percent_marker(self):
        src = INSTALL_SH.read_text()
        self.assertIn("use_scrollbar = OFF", src)
        self.assertIn("position_indicator_color = (WHITE,WHITE,ON)", src)
        self.assertNotIn("use_scrollbar = ON", src)

    def test_text_gauge_requires_writable_tty(self):
        src = INSTALL_SH.read_text()
        self.assertIn("tty_writable()", src)
        self.assertIn('if tty_writable; then\n    GAUGE_MODE="text"', src)
        self.assertNotIn('if [[ -c /dev/tty ]]; then\n    GAUGE_MODE="text"', src)
        self.assertIn(">/dev/tty 2>/dev/null || true", src)
        self.assertIn('if [[ -n "${TABBY_UPDATE_LOG:-}" ]]; then', src)
        self.assertIn('tee -a "$INSTALL_LOG" >> "$TABBY_UPDATE_LOG"', src)
        self.assertIn('[[ "$(type -t "$1" 2>/dev/null || true)" == function ]]', src)
        self.assertIn(
            'if [[ "$UPDATE_MODE" -eq 1 && "$USE_TUI" -eq 0 ]] && tty_writable; then',
            src,
        )
