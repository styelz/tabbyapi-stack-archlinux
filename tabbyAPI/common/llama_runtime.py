"""Exclusive llama-server (GGUF) GPU occupant, sibling to ComfyUI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
LLAMA_ENV_NAME = "llama.env"
LLAMA_RUNTIME_NAME = "llama_runtime.json"
DEFAULT_LLAMA_PORT = 5002
DEFAULT_CTX = 32768
DUMMY_MODEL = "gpt-4o"
LLAMA_ALIASES = {
    "llama": "llama",
    "llamacpp": "llama",
    "llama.cpp": "llama",
    "gguf": "llama",
}


def llama_dir(windows: Optional[bool] = None) -> Path:
    override = os.environ.get("LLAMA_DIR")
    if override:
        return Path(override).expanduser()
    is_win = os.name == "nt" if windows is None else windows
    if is_win:
        return Path(r"D:\tabbyapi-stack\llama.cpp")
    return ROOT.parent / "llama.cpp"


def llama_port() -> int:
    return int(os.environ.get("LLAMA_PORT") or DEFAULT_LLAMA_PORT)


def llama_host() -> str:
    return os.environ.get("LLAMA_HOST") or "127.0.0.1"


def llama_url() -> str:
    explicit = os.environ.get("LLAMA_BACKEND_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"http://{llama_host()}:{llama_port()}"


def llama_env_path() -> Path:
    return ROOT / "model_profiles" / LLAMA_ENV_NAME


def llama_runtime_path() -> Path:
    return ROOT / "model_profiles" / LLAMA_RUNTIME_NAME


def llama_user_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / "llamacpp.service"


def llama_server_bin() -> Optional[Path]:
    explicit = os.environ.get("LLAMA_SERVER")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
    root = llama_dir()
    for candidate in (
        root / "llama-server",
        root / "build" / "bin" / "llama-server",
        root / "build" / "llama-server",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    which = shutil.which("llama-server")
    return Path(which) if which else None


def ngl_arg(n_gpu_layers: Any) -> str:
    try:
        value = int(n_gpu_layers)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        # llama.cpp --fit only adjusts ngl when it is unset/auto. An explicit
        # 999 ("all layers") pins VRAM and OOMs a 32k KV cache on 12 GB cards.
        return "auto"
    return str(value)


def write_llama_runtime(
    model_path: Path,
    *,
    profile: str = "",
    n_gpu_layers: Any = -1,
    max_seq_len: Any = None,
    mmproj: Optional[str] = None,
) -> dict[str, Any]:
    ctx = int(max_seq_len or DEFAULT_CTX)
    if ctx < 256:
        ctx = DEFAULT_CTX
    data = {
        "model": str(model_path.resolve()),
        "profile": profile,
        "n_gpu_layers": n_gpu_layers,
        "max_seq_len": ctx,
        "mmproj": str(mmproj) if mmproj else "",
    }
    dest = llama_runtime_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    env_lines = [
        f"LLAMA_MODEL={shlex.quote(data['model'])}",
        f"LLAMA_CTX={ctx}",
        f"LLAMA_NGL={ngl_arg(n_gpu_layers)}",
        f"LLAMA_PORT={llama_port()}",
        f"LLAMA_HOST={llama_host()}",
        f"LLAMA_ALIAS={DUMMY_MODEL}",
        f"LLAMA_MMPROJ={shlex.quote(data['mmproj']) if data['mmproj'] else ''}",
    ]
    bin_path = llama_server_bin()
    if bin_path:
        env_lines.append(f"LLAMA_SERVER={shlex.quote(str(bin_path))}")
    llama_env_path().write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return data


def read_llama_runtime() -> dict[str, Any]:
    path = llama_runtime_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def llama_argv(
    model_path: Path,
    *,
    n_gpu_layers: Any = -1,
    max_seq_len: Any = None,
    mmproj: Optional[str] = None,
) -> list[str]:
    server = llama_server_bin()
    if not server:
        raise SystemExit(
            "llama-server is not installed. The installer builds it next to ComfyUI, "
            "or set LLAMA_SERVER to the binary."
        )
    ctx = int(max_seq_len or DEFAULT_CTX)
    args = [
        str(server),
        "--host",
        llama_host(),
        "--port",
        str(llama_port()),
        "--alias",
        DUMMY_MODEL,
        "--jinja",
        "-m",
        str(model_path),
        "-c",
        str(ctx),
        "-ngl",
        ngl_arg(n_gpu_layers),
    ]
    if mmproj:
        args.extend(["--mmproj", str(mmproj)])
    return args


def llama_up() -> bool:
    try:
        req = Request(llama_url().rstrip("/") + "/health", method="GET")
        with urlopen(req, timeout=3) as resp:
            return 200 <= int(getattr(resp, "status", 200) or 200) < 500
    except (URLError, HTTPError, TimeoutError, OSError):
        return False


def llama_loaded_id() -> Optional[str]:
    if not llama_up():
        return None
    try:
        req = Request(llama_url().rstrip("/") + "/v1/models", method="GET")
        with urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        runtime = read_llama_runtime()
        return str(runtime.get("profile") or Path(str(runtime.get("model") or "")).name or "") or None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(rows, list) and rows:
        name = str((rows[0] or {}).get("id") or "").strip()
        if name:
            return name
    runtime = read_llama_runtime()
    return str(runtime.get("profile") or Path(str(runtime.get("model") or "")).name or "") or None


def llama_pids() -> list[int]:
    if os.name == "nt":
        return []
    found: set[int] = set()
    try:
        result = subprocess.run(
            ["fuser", f"{llama_port()}/tcp"],
            capture_output=True,
            text=True,
        )
        blob = f"{result.stdout or ''} {result.stderr or ''}"
        for token in blob.replace(",", " ").split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid > 1:
                found.add(pid)
    except OSError:
        pass
    found.discard(os.getpid())
    return sorted(found)


def llama_unit_active() -> bool:
    if os.name == "nt" or not llama_user_unit_path().is_file():
        return False
    from common.gpu_mode import systemctl_user

    result = systemctl_user("is-active", "--quiet", "llamacpp", capture_output=True)
    return result.returncode == 0


def stop_llama_via_systemd() -> bool:
    if os.name == "nt" or not llama_user_unit_path().is_file():
        return False
    from common.gpu_mode import systemctl_user

    result = systemctl_user("stop", "llamacpp", capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"  systemctl --user stop llamacpp failed: {err}")
        return False
    print("  Stopped llama-server via systemd")
    return True


def start_llama_via_systemd() -> bool:
    if os.name == "nt" or not llama_user_unit_path().is_file():
        return False
    from common.gpu_mode import systemctl_user

    result = systemctl_user("restart", "llamacpp", capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"  systemctl --user start llamacpp failed: {err}")
        return False
    print("  Started llama-server via systemd")
    return True


def kill_llama_process(timeout: float = 10) -> None:
    pids = llama_pids()
    if not pids:
        return
    print(f"  killing llama-server {', '.join(str(pid) for pid in pids)}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not llama_up() and not llama_pids():
            print("  llama-server process gone")
            return
        time.sleep(0.3)
    for pid in llama_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(0.4)


def stop_llama(timeout: float = 30) -> None:
    """Give the GPU back before an EXL load or Comfy start."""
    from common.gpu_mode import wait_gpu_vram_drain

    if not llama_up() and not llama_unit_active() and not llama_pids():
        return
    stopped = stop_llama_via_systemd()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not llama_up() and not llama_pids():
            print("  llama-server is down")
            wait_gpu_vram_drain()
            return
        time.sleep(0.4)
    if llama_up() or not stopped or llama_pids():
        print("  llama-server still answering after stop; killing process")
        kill_llama_process()
    if not llama_up() and not llama_pids():
        print("  llama-server is down")
        wait_gpu_vram_drain()
        return
    print("  llama-server still answering after stop; LLM load may hit VRAM")


def _same_model(model_path: Path) -> bool:
    current = str(read_llama_runtime().get("model") or "")
    try:
        return bool(current) and Path(current).resolve() == model_path.resolve()
    except OSError:
        return False


def start_llama_if_needed(
    model_path: Path,
    *,
    profile: str = "",
    n_gpu_layers: Any = -1,
    max_seq_len: Any = None,
    mmproj: Optional[str] = None,
    timeout: float = 180,
) -> None:
    path = Path(model_path)
    if not path.exists():
        raise SystemExit(f"GGUF missing: {path}")
    write_llama_runtime(
        path,
        profile=profile,
        n_gpu_layers=n_gpu_layers,
        max_seq_len=max_seq_len,
        mmproj=mmproj,
    )
    if llama_up() and _same_model(path):
        print("  llama-server already running this GGUF")
        return
    if llama_up() or llama_pids():
        stop_llama()
    print(f"  Starting llama-server ({path.name})...")
    if start_llama_via_systemd():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if llama_up():
                print("  llama-server is up")
                return
            time.sleep(1)
        print("  systemd llama-server did not become healthy; trying a direct start")
    log_path = ROOT / "llama-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    cmd = llama_argv(
        path,
        n_gpu_layers=n_gpu_layers,
        max_seq_len=max_seq_len,
        mmproj=mmproj,
    )
    kwargs: dict = {"stdout": log, "stderr": log}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if llama_up():
            print("  llama-server is up")
            return
        time.sleep(1)
    raise SystemExit(f"llama-server did not start within {timeout:.0f}s. See {log_path}")
