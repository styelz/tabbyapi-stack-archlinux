"""Start the sidecar and the localhost Tabby backend together."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from sidecar.backend_key import ensure_backend_key, write_backend_tokens
from sidecar.paths import STACK_ROOT, TABBY_DIR
from sidecar.settings import (
    backend_dir,
    backend_host,
    backend_port,
    backend_url,
    sidecar_enabled,
    sidecar_host,
    sidecar_port,
    uses_vanilla_backend,
)

DELAY_SEC = 5
FAST_CRASH_SEC = 90
FAST_CRASH_LIMIT = 3


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    key = ensure_backend_key()
    env["TABBY_BACKEND_KEY"] = key
    env["TABBY_BACKEND_URL"] = backend_url()
    env["SIDECAR_HOST"] = sidecar_host()
    env["SIDECAR_PORT"] = str(sidecar_port())
    env["TABBY_BACKEND_HOST"] = backend_host()
    env["TABBY_BACKEND_PORT"] = str(backend_port())
    env["TABBY_BACKEND_DIR"] = str(backend_dir())
    parts = [str(STACK_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    if uses_vanilla_backend():
        write_backend_tokens(backend_dir(), key)
    return env


def tabby_command(python: str) -> list[str]:
    main = backend_dir() / "main.py"
    return [
        python,
        str(main),
        "--host",
        backend_host(),
        "--port",
        str(backend_port()),
    ]


def sidecar_command(python: str) -> list[str]:
    return [python, "-m", "sidecar"]


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def run(python: Optional[str] = None) -> int:
    """Supervise Tabby (and the sidecar when enabled)."""
    py = python or sys.executable
    env = child_env()

    try:
        from common.gpu_mode import start_comfy_journal_forwarder

        start_comfy_journal_forwarder()
    except Exception as exc:
        print(f"Comfy journal forwarder not started: {exc}", flush=True)

    try:
        from common.ssh_forwarder import ensure_ssh_forwarder

        ensure_ssh_forwarder()
    except Exception as exc:
        print(f"SSH reverse tunnel not started: {exc}", flush=True)

    if not sidecar_enabled():
        print("TabbyAPI watchdog: sidecar off (TABBY_SIDECAR=0).", flush=True)
        cwd = TABBY_DIR
        cmd = [py, str(cwd / "main.py")]
        while True:
            result = subprocess.run(cmd, cwd=str(cwd), env=env)
            if result.returncode == 0:
                return 0
            print(f"TabbyAPI exited with code {result.returncode}.", flush=True)
            time.sleep(DELAY_SEC)

    print(
        f"Stack watchdog: sidecar {sidecar_host()}:{sidecar_port()} "
        f"-> Tabby {backend_url()} ({backend_dir()})",
        flush=True,
    )
    tabby: Optional[subprocess.Popen] = None
    side: Optional[subprocess.Popen] = None
    fast_exits: list[float] = []
    tabby_stopped = False

    def stop_all(*_args) -> None:
        _terminate(side)
        _terminate(tabby)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    try:
        while True:
            now = time.time()
            if not tabby_stopped and (tabby is None or tabby.poll() is not None):
                if tabby is not None:
                    code = tabby.returncode
                    lived = now - getattr(tabby, "_started", now)
                    print(f"TabbyAPI exited with code {code} after {lived:.0f}s.", flush=True)
                    if code == 0:
                        print("TabbyAPI exited cleanly.", flush=True)
                        _terminate(side)
                        return 0
                    if lived < FAST_CRASH_SEC:
                        fast_exits = [t for t in fast_exits if now - t < FAST_CRASH_SEC]
                        fast_exits.append(now)
                    if len(fast_exits) >= FAST_CRASH_LIMIT:
                        print(
                            f"Crashed {FAST_CRASH_LIMIT} times in {FAST_CRASH_SEC}s "
                            "(likely OOM on load). Not restarting Tabby.",
                            flush=True,
                        )
                        tabby_stopped = True
                        tabby = None
                        continue
                    time.sleep(DELAY_SEC)
                print(f"--- starting TabbyAPI ({backend_dir()}) ---\n", flush=True)
                tabby = subprocess.Popen(
                    tabby_command(py),
                    cwd=str(backend_dir()),
                    env=env,
                )
                tabby._started = time.time()  # type: ignore[attr-defined]
            if side is None or side.poll() is not None:
                if side is not None:
                    print(f"Sidecar exited with code {side.returncode}. Restarting.", flush=True)
                    time.sleep(1)
                print("--- starting sidecar ---\n", flush=True)
                side = subprocess.Popen(
                    sidecar_command(py),
                    cwd=str(STACK_ROOT),
                    env=env,
                )
            time.sleep(0.4)
    finally:
        _terminate(side)
        _terminate(tabby)
