"""Exclusive llama-server (GGUF) GPU occupant, sibling to ComfyUI."""

from __future__ import annotations

import json
import os
import re
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
# 13 GB GGUF + 32k ctx on a 12 GB / 32 GB host: llama-server's cgroup was
# already ~15 GiB idle (mmap + CPU layers). A 22k-token chat then wedged
# the box mid-token with no OOM log. Cap heavy weights at 16k, keep
# --cache-ram off, and let systemd MemoryMax / earlyoom kill llama first.
DEFAULT_PARALLEL = 1
DEFAULT_FIT = "on"
DEFAULT_FIT_TARGET_MIB = 2048
DEFAULT_CACHE_TYPE = "q8_0"
DEFAULT_FLASH_ATTN = "on"
DEFAULT_CACHE_RAM_MIB = 0
DEFAULT_BATCH = 512
DEFAULT_UBATCH = 256
GGUF_HEAVY_BYTES = 8 * 1024 * 1024 * 1024
GGUF_MID_BYTES = 5 * 1024 * 1024 * 1024
GGUF_HEAVY_CTX = 16384
GGUF_MID_CTX = 16384
DUMMY_MODEL = "gpt-4o"
LLAMA_ALIASES = {
    "llama": "llama",
    "llamacpp": "llama",
    "llama.cpp": "llama",
    "gguf": "llama",
}
_BASE_TOKEN = re.compile(r"(?:^|[-_./\s])base(?:[-_.]|$)")
_CHAT_TUNED = re.compile(r"instruct|\bchat\b|(?:^|[-_./\s])it(?:[-_.]|$)")
_DEEPSEEK_CODER = re.compile(r"deepseek[-_]?coder|kexer")


def is_gguf_base_name(*parts: str) -> bool:
    """True for completion-only GGUFs (DeepSeek Coder -base, not instruct)."""
    blob = " ".join(str(part or "") for part in parts).lower()
    if not blob or _CHAT_TUNED.search(blob):
        return False
    return bool(_BASE_TOKEN.search(blob))


def guess_llama_chat_template(model_path: Path | str, override: str | None = None) -> str:
    """Built-in llama.cpp template when the GGUF has no chat template of its own.

    llama-server --jinja falls back to ChatML. DeepSeek Coder / Kexer / -base
    GGUFs do not own those tokens, so replies turn into glued nonce words.
    """
    if str(override or "").strip():
        return str(override).strip()
    path = Path(str(model_path or ""))
    blob = f"{path.name} {path.parent.name} {path}".lower()
    if "mmproj" in path.name.lower():
        return ""
    if _DEEPSEEK_CODER.search(blob):
        return "deepseek"
    if is_gguf_base_name(blob):
        return "vicuna"
    return ""


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


def gguf_weight_bytes(model_path: Path | str | None) -> int:
    """Size of the GGUF weights file, or the largest non-mmproj GGUF in a folder."""
    if not model_path:
        return 0
    path = Path(model_path)
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if not path.is_dir():
            return 0
        sizes = [
            int(item.stat().st_size)
            for item in path.glob("*.gguf")
            if item.is_file() and "mmproj" not in item.name.lower()
        ]
        return max(sizes) if sizes else 0
    except OSError:
        return 0


def clamp_gguf_ctx(
    ctx: Any,
    model_path: Path | str | None = None,
    *,
    size_bytes: int | None = None,
) -> int:
    """Cap context for GGUFs whose weights already spill into host RAM."""
    try:
        ctx_n = int(ctx or 0)
    except (TypeError, ValueError):
        ctx_n = 0
    if ctx_n < 256:
        ctx_n = DEFAULT_CTX
    size = int(size_bytes) if size_bytes is not None else gguf_weight_bytes(model_path)
    cap = DEFAULT_CTX
    if size >= GGUF_HEAVY_BYTES:
        cap = GGUF_HEAVY_CTX
    elif size >= GGUF_MID_BYTES:
        cap = GGUF_MID_CTX
    return min(ctx_n, cap)


def llama_launch_flags(
    model_path: Path | str,
    *,
    n_gpu_layers: Any = -1,
    max_seq_len: Any = None,
) -> dict[str, Any]:
    """Flags that keep llama-server from freezing a 12 GB host."""
    ctx = clamp_gguf_ctx(max_seq_len, model_path)
    return {
        "max_seq_len": ctx,
        "n_gpu_layers": n_gpu_layers,
        "ngl": ngl_arg(n_gpu_layers),
        "parallel": DEFAULT_PARALLEL,
        "fit": DEFAULT_FIT,
        "fit_target": DEFAULT_FIT_TARGET_MIB,
        "flash_attn": DEFAULT_FLASH_ATTN,
        "cache_k": DEFAULT_CACHE_TYPE,
        "cache_v": DEFAULT_CACHE_TYPE,
        "cache_ram": DEFAULT_CACHE_RAM_MIB,
        "batch": DEFAULT_BATCH,
        "ubatch": DEFAULT_UBATCH,
        "kv_unified": "on",
    }


def _launch_signature(data: dict[str, Any]) -> tuple[Any, ...]:
    flags = llama_launch_flags(
        str(data.get("model") or ""),
        n_gpu_layers=data.get("n_gpu_layers", -1),
        max_seq_len=data.get("max_seq_len"),
    )
    try:
        model = str(Path(str(data.get("model") or "")).resolve())
    except OSError:
        model = str(data.get("model") or "")
    return (
        model,
        str(data.get("chat_template") or ""),
        str(data.get("mmproj") or ""),
        flags["ngl"],
        int(data.get("max_seq_len") or flags["max_seq_len"]),
        int(data.get("parallel") if data.get("parallel") is not None else flags["parallel"]),
        int(data.get("fit_target") if data.get("fit_target") is not None else flags["fit_target"]),
        str(data.get("cache_k") or flags["cache_k"]),
        str(data.get("cache_v") or flags["cache_v"]),
        int(data.get("cache_ram") if data.get("cache_ram") is not None else flags["cache_ram"]),
        str(data.get("flash_attn") or flags["flash_attn"]),
        str(data.get("fit") or flags["fit"]),
        int(data.get("batch") if data.get("batch") is not None else flags["batch"]),
        int(data.get("ubatch") if data.get("ubatch") is not None else flags["ubatch"]),
        str(data.get("kv_unified") or flags["kv_unified"]),
    )


def llama_launch_matches(previous: dict[str, Any], runtime: dict[str, Any]) -> bool:
    if not previous or not runtime:
        return False
    return _launch_signature(previous) == _launch_signature(runtime)


def write_llama_runtime(
    model_path: Path,
    *,
    profile: str = "",
    n_gpu_layers: Any = -1,
    max_seq_len: Any = None,
    mmproj: Optional[str] = None,
    chat_template: Optional[str] = None,
) -> dict[str, Any]:
    flags = llama_launch_flags(
        model_path, n_gpu_layers=n_gpu_layers, max_seq_len=max_seq_len
    )
    ctx = int(flags["max_seq_len"])
    template = guess_llama_chat_template(model_path, chat_template)
    data = {
        "model": str(model_path.resolve()),
        "profile": profile,
        "n_gpu_layers": n_gpu_layers,
        "max_seq_len": ctx,
        "mmproj": str(mmproj) if mmproj else "",
        "chat_template": template,
        "parallel": flags["parallel"],
        "fit": flags["fit"],
        "fit_target": flags["fit_target"],
        "flash_attn": flags["flash_attn"],
        "cache_k": flags["cache_k"],
        "cache_v": flags["cache_v"],
        "cache_ram": flags["cache_ram"],
        "batch": flags["batch"],
        "ubatch": flags["ubatch"],
        "kv_unified": flags["kv_unified"],
    }
    dest = llama_runtime_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    env_lines = [
        f"LLAMA_MODEL={shlex.quote(data['model'])}",
        f"LLAMA_CTX={ctx}",
        f"LLAMA_NGL={flags['ngl']}",
        f"LLAMA_PORT={llama_port()}",
        f"LLAMA_HOST={llama_host()}",
        f"LLAMA_ALIAS={DUMMY_MODEL}",
        f"LLAMA_MMPROJ={shlex.quote(data['mmproj']) if data['mmproj'] else ''}",
        f"LLAMA_CHAT_TEMPLATE={shlex.quote(template) if template else ''}",
        f"LLAMA_PARALLEL={flags['parallel']}",
        f"LLAMA_FIT={flags['fit']}",
        f"LLAMA_FIT_TARGET={flags['fit_target']}",
        f"LLAMA_FLASH_ATTN={flags['flash_attn']}",
        f"LLAMA_CACHE_K={flags['cache_k']}",
        f"LLAMA_CACHE_V={flags['cache_v']}",
        f"LLAMA_CACHE_RAM={flags['cache_ram']}",
        f"LLAMA_BATCH={flags['batch']}",
        f"LLAMA_UBATCH={flags['ubatch']}",
        f"LLAMA_KV_UNIFIED={flags['kv_unified']}",
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
    chat_template: Optional[str] = None,
) -> list[str]:
    server = llama_server_bin()
    if not server:
        raise SystemExit(
            "llama-server is not installed. The installer builds it next to ComfyUI, "
            "or set LLAMA_SERVER to the binary."
        )
    flags = llama_launch_flags(
        model_path, n_gpu_layers=n_gpu_layers, max_seq_len=max_seq_len
    )
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
        str(flags["max_seq_len"]),
        "-ngl",
        flags["ngl"],
        "--parallel",
        str(flags["parallel"]),
        "--fit",
        flags["fit"],
        "--fit-target",
        str(flags["fit_target"]),
        "--flash-attn",
        flags["flash_attn"],
        "--cache-type-k",
        flags["cache_k"],
        "--cache-type-v",
        flags["cache_v"],
        "--cache-ram",
        str(flags["cache_ram"]),
        "--batch-size",
        str(flags["batch"]),
        "--ubatch-size",
        str(flags["ubatch"]),
    ]
    if str(flags["kv_unified"]).lower() in {"1", "on", "true", "yes"}:
        args.append("--kv-unified")
    if mmproj:
        args.extend(["--mmproj", str(mmproj)])
    template = guess_llama_chat_template(model_path, chat_template)
    if template:
        args.extend(["--chat-template", template])
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


def _wait_llama_healthy(timeout: float) -> bool:
    """True once /health answers. False if the process dies or time runs out."""
    deadline = time.time() + max(1.0, float(timeout))
    saw_pid = False
    missing = 0
    while time.time() < deadline:
        if llama_up():
            return True
        pids = llama_pids()
        if pids:
            saw_pid = True
            missing = 0
        elif saw_pid:
            missing += 1
            if missing >= 3:
                print("  llama-server exited before becoming healthy")
                return False
        time.sleep(1)
    return llama_up()


def start_llama_if_needed(
    model_path: Path,
    *,
    profile: str = "",
    n_gpu_layers: Any = -1,
    max_seq_len: Any = None,
    mmproj: Optional[str] = None,
    chat_template: Optional[str] = None,
    timeout: float = 180,
) -> None:
    path = Path(model_path)
    if not path.exists():
        raise SystemExit(f"GGUF missing: {path}")
    previous = read_llama_runtime()
    runtime = write_llama_runtime(
        path,
        profile=profile,
        n_gpu_layers=n_gpu_layers,
        max_seq_len=max_seq_len,
        mmproj=mmproj,
        chat_template=chat_template,
    )
    template = str(runtime.get("chat_template") or "")
    if llama_up() and llama_launch_matches(previous, runtime):
        print("  llama-server already running this GGUF")
        return
    if llama_up() or llama_pids():
        if llama_up() and _same_model(path):
            print("  llama-server flags changed; restarting llama-server")
        stop_llama()
    print(f"  Starting llama-server ({path.name})...")
    log_path = ROOT / "llama-server.log"
    if start_llama_via_systemd():
        if _wait_llama_healthy(timeout):
            print("  llama-server is up")
            return
        print("  systemd llama-server did not become healthy; trying a direct start")
        if llama_pids():
            kill_llama_process()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    cmd = llama_argv(
        path,
        n_gpu_layers=n_gpu_layers,
        max_seq_len=max_seq_len,
        mmproj=mmproj,
        chat_template=template,
    )
    kwargs: dict = {"stdout": log, "stderr": log}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    if _wait_llama_healthy(timeout):
        print("  llama-server is up")
        return
    raise SystemExit(f"llama-server did not start within {timeout:.0f}s. See {log_path}")
