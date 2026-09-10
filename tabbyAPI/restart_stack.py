"""Bounce TabbyAPI (and Comfy if needed) after a chat ``restart``.

Spawned detached so the chat reply can flush before this process is killed.
Also refreshes the TTY screensaver when its files are newer than the process.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

JOBS_PERSIST = Path(__file__).resolve().parent / "pasted-images" / "mcp_jobs.json"
RESTART_ABANDON_REASON = "TabbyAPI restarted before this job finished."
_TABBY = Path(__file__).resolve().parent
SAVER_SOURCES = (
    _TABBY / "deploy/arch/tabby-saver.py",
    _TABBY / "deploy/arch/tabby-saver.service",
    Path("/etc/systemd/system/tabby-saver.service"),
)


def abandon_persisted_jobs(
    path: Path | None = None,
    reason: str = RESTART_ABANDON_REASON,
) -> int:
    """Rewrite mcp_jobs.json so queued/running jobs cannot block the next process."""
    persist = path or JOBS_PERSIST
    try:
        raw = json.loads(persist.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(raw, list):
        return 0
    changed = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") not in ("queued", "running"):
            continue
        entry["status"] = "error"
        entry["phase"] = "error"
        entry["client_saved"] = False
        if not entry.get("error"):
            entry["error"] = reason
        for item in entry.get("items") or []:
            if isinstance(item, dict) and item.get("status") in ("queued", "running"):
                item["status"] = "error"
                if not item.get("error"):
                    item["error"] = reason
        changed += 1
    if not changed:
        return 0
    try:
        tmp = persist.with_name(persist.name + ".tmp")
        tmp.write_text(json.dumps(raw), encoding="utf-8")
        tmp.replace(persist)
    except OSError:
        return 0
    return changed


def _unit_main_pid(unit: str = "tabby-saver") -> int:
    try:
        proc = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    try:
        return int((proc.stdout or "").strip() or "0")
    except ValueError:
        return 0


def _unit_enabled(unit: str = "tabby-saver") -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", unit],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _newest_saver_mtime(paths: tuple[Path, ...] | None = None) -> float:
    newest = 0.0
    for path in paths or SAVER_SOURCES:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    return newest


def screensaver_needs_restart(
    *,
    force: bool = False,
    pid: int | None = None,
    started: float | None = None,
    newest: float | None = None,
) -> bool:
    """True when the kiosk is running files older than the tree, or force."""
    if force:
        return True
    if pid is None:
        pid = _unit_main_pid()
    if pid <= 0:
        return False
    if started is None:
        try:
            started = os.stat(f"/proc/{pid}").st_ctime
        except OSError:
            return False
    if newest is None:
        newest = _newest_saver_mtime()
    return newest > started + 0.25


def maybe_restart_screensaver(*, force: bool | None = None) -> bool:
    """Restart system tabby-saver when this pull changed it or the process is stale.

    force defaults to TABBY_SAVER_CHANGED=1 from update.sh. A Status / chat
    API restart leaves force off and still bounces a kiosk older than the files.
    """
    if force is None:
        force = str(os.environ.get("TABBY_SAVER_CHANGED") or "").strip() == "1"
    pid = _unit_main_pid()
    if not screensaver_needs_restart(force=force, pid=pid):
        return False
    if pid <= 0 and not _unit_enabled():
        return False
    action = "restart" if pid > 0 else "start"
    try:
        proc = subprocess.run(
            ["sudo", "-n", "systemctl", action, "tabby-saver"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARNING: could not {action} tabby-saver: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        print(f"WARNING: could not {action} tabby-saver: {err}", file=sys.stderr)
        return False
    print(f"{action.capitalize()}ed tabby-saver (screensaver files updated).", flush=True)
    return True


def restart_units(mode: str = "llm") -> int:
    """Stop or restart user units. mode is llm or comfy."""
    from common.gpu_mode import user_systemd_env

    env = user_systemd_env()
    if mode == "comfy":
        subprocess.run(
            ["systemctl", "--user", "reset-failed", "comfyui"],
            check=False,
            env=env,
        )
        subprocess.run(
            ["systemctl", "--user", "restart", "comfyui"],
            check=False,
            env=env,
        )
    else:
        subprocess.run(["systemctl", "--user", "stop", "comfyui"], check=False, env=env)
    subprocess.run(["systemctl", "--user", "reset-failed", "tabbyapi"], check=False, env=env)
    rc = subprocess.run(["systemctl", "--user", "restart", "tabbyapi"], env=env).returncode
    maybe_restart_screensaver()
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart the TabbyAPI stack")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--mode", default="llm", choices=("llm", "comfy"))
    parser.add_argument("--lock", type=Path, default=None)
    parser.add_argument(
        "--saver-if-updated",
        action="store_true",
        help="Only bounce tabby-saver when its files are newer (or TABBY_SAVER_CHANGED=1)",
    )
    args = parser.parse_args(argv)
    if args.saver_if_updated:
        maybe_restart_screensaver()
        return 0
    if args.delay > 0:
        time.sleep(args.delay)
    if args.lock is not None:
        args.lock.unlink(missing_ok=True)
    abandon_persisted_jobs()
    return restart_units(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
