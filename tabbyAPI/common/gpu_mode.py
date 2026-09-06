"""Exclusive GPU ownership between TabbyAPI (LLM) and ComfyUI (Flux)."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def comfy_paths(
    comfy_dir: Optional[Path] = None,
    windows: Optional[bool] = None,
) -> tuple[Path, Path]:
    """ComfyUI root and venv python for this OS (or a forced Windows/Linux layout)."""
    is_win = os.name == "nt" if windows is None else windows
    if comfy_dir is not None:
        root = Path(comfy_dir)
    elif os.environ.get("COMFYUI_DIR"):
        root = Path(os.environ["COMFYUI_DIR"])
    elif is_win:
        root = Path(r"D:\tabbyapi-stack\ComfyUI")
    else:
        root = ROOT.parent / "ComfyUI"
    if is_win:
        python = root / "venv" / "Scripts" / "python.exe"
    else:
        python = root / "venv" / "bin" / "python"
    return root, python


COMFY_DIR, COMFY_PYTHON = comfy_paths()
COMFY_START = COMFY_DIR / ("start.bat" if os.name == "nt" else "start.sh")
COMFY_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
_COMFY_PARTS = urlparse(COMFY_URL)
COMFY_HOST = _COMFY_PARTS.hostname or "127.0.0.1"
COMFY_PORT = str(_COMFY_PARTS.port or 8188)
COMFY_LOG = COMFY_DIR / "comfy.log"
STATUS_PATH = ROOT / "model_profiles" / "gpu_mode.json"
WORKFLOW_PATH = ROOT / "comfy_workflows" / "flux_schnell_api.json"
IMG2IMG_WORKFLOW_PATH = ROOT / "comfy_workflows" / "flux_schnell_img2img.json"
QWEN_IMAGE_WORKFLOW_PATH = ROOT / "comfy_workflows" / "qwen_image_api.json"
GENERATED_DIR = ROOT / "pasted-images"
JOBS_PERSIST_NAME = "mcp_jobs.json"
TURN_PATH = GENERATED_DIR / "turn.json"


def persisted_jobs_block_llm_load(path: Optional[Path] = None) -> bool:
    """True when mcp_jobs.json still has a queued/running Comfy batch.

    Used by the switch_model subprocess, which must not import images.jobs
    (that would treat the live worker as dead and abandon the job on disk).
    """
    target = path or (GENERATED_DIR / JOBS_PERSIST_NAME)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, list):
        return False
    return any(
        isinstance(entry, dict) and str(entry.get("status") or "") in ("queued", "running")
        for entry in raw
    )
GALLERY_THUMB_MAX = 480
GALLERY_THUMB_QUALITY = 72
GALLERY_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
GALLERY_UPLOAD_MAX_PIXELS = 40_000_000
GALLERY_UPLOAD_MAX_EDGE = 2048
CHECKPOINT_NAME = "flux1-schnell-fp8.safetensors"
QWEN_IMAGE_UNET = "qwen-image-Q4_K_M.gguf"
QWEN_IMAGE_CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
QWEN_IMAGE_VAE = "qwen_image_vae.safetensors"
QWEN_IMAGE_LORA = "Qwen-Image-Lightning-8steps-V1.0.safetensors"
QWEN_IMAGE_STEPS = 8
QWEN_IMAGE_TIMEOUT = 1200
QWEN_IMAGE_PREFIX = re.compile(r"(?is)^\s*qwen-image\s*:\s*(.*)$")
QWEN_IMAGE_HINTS = re.compile(
    r"(?is)\b("
    r"text|poster|button|ui|mockup|logo|sign|label|"
    r"typography|caption|lettering|"
    r"qwen-image"
    r")\b"
)
DEFAULT_PUBLIC_BASE = "http://127.0.0.1:5000/v1"


def public_api_base(request=None) -> str:
    """URL prefix remote clients can fetch images from.

    Prefer TABBY_PUBLIC_BASE (tunnel / reverse proxy). Otherwise use the
    Host the client already called. Localhost is only the last resort —
    coding machines are not the GPU server.
    """
    env = (os.environ.get("TABBY_PUBLIC_BASE") or "").strip().rstrip("/")
    if env:
        return env
    headers = getattr(request, "headers", None)
    url = getattr(request, "url", None)
    proto = None
    host = None
    if headers is not None:
        proto = headers.get("x-forwarded-proto")
        host = headers.get("x-forwarded-host") or headers.get("host")
    if not proto and url is not None:
        proto = getattr(url, "scheme", None)
    if not host and url is not None:
        host = getattr(url, "netloc", None)
    if proto and host:
        return f"{proto}://{host}/v1"
    return DEFAULT_PUBLIC_BASE


# Kept for older callers / tests that read the module constant.
PUBLIC_BASE = (os.environ.get("TABBY_PUBLIC_BASE") or DEFAULT_PUBLIC_BASE).rstrip("/")

GPU_ALIASES = {
    "comfy": "comfy",
    "flux": "comfy",
    "image": "comfy",
    "comfyui": "comfy",
}


def _is_http_timeout(exc: BaseException) -> bool:
    """True for urlopen socket timeouts, including those wrapped in URLError."""
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    text = str(reason if reason is not None else exc).lower()
    return "timed out" in text


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    timeout: float = 30,
) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode())


def read_mode() -> dict:
    if not STATUS_PATH.exists():
        return {"mode": "llm"}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"mode": "llm"}
    if not isinstance(data, dict):
        return {"mode": "llm"}
    return data


def should_skip_startup_load() -> bool:
    """True when Flux owns the GPU. Restarting Tabby must not load an LLM."""

    return (read_mode().get("mode") or "").lower() == "comfy"


def write_mode(mode: str, **extra) -> dict:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"mode": mode, **extra}
    STATUS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def user_systemd_env(base: Optional[dict] = None) -> dict[str, str]:
    """Env for `systemctl --user` when sudo/sg/env_reset dropped the session bus.

    systemd 256+ refuses `--user` unless both vars are set. Always write the
    usual unix path; do not wait for the socket to exist.
    """
    env = {str(key): str(value) for key, value in dict(os.environ if base is None else base).items()}
    uid = os.getuid()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = runtime
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={Path(runtime) / 'bus'}"
    return env


def systemctl_user(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """`systemctl --user` with a reconstructed session bus when needed."""
    kwargs.setdefault("env", user_systemd_env())
    return subprocess.run(["systemctl", "--user", *args], **kwargs)


def comfy_up() -> bool:
    try:
        request_json("GET", f"{COMFY_URL}/system_stats", timeout=3)
        return True
    except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def interrupt_comfy() -> bool:
    """Ask Comfy to drop the current prompt so /free and stop can proceed."""
    if not comfy_up():
        return False
    try:
        request_json("POST", f"{COMFY_URL}/interrupt", timeout=10)
        print("  ComfyUI interrupt sent")
        return True
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        print(f"  ComfyUI /interrupt failed: {exc}")
        return False


def free_comfy() -> bool:
    """Unload Flux weights. Returns False if ComfyUI is not running."""
    if not comfy_up():
        return False
    try:
        request_json(
            "POST",
            f"{COMFY_URL}/free",
            {"unload_models": True, "free_memory": True},
            timeout=60,
        )
        print("  ComfyUI models unloaded")
        return True
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        print(f"  ComfyUI /free failed: {exc}")
        return False


def _parse_pids(text: str) -> list[int]:
    pids: set[int] = set()
    for token in (text or "").replace(",", " ").replace(":", " ").split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 1:
            pids.add(pid)
    pids.discard(os.getpid())
    return sorted(pids)


def comfy_pids() -> list[int]:
    """PIDs for ComfyUI/main.py or whoever is bound to the Comfy port."""
    if os.name == "nt":
        return []
    found: set[int] = set()
    main_py = str(COMFY_DIR / "main.py")
    try:
        result = subprocess.run(
            ["pgrep", "-f", main_py],
            capture_output=True,
            text=True,
        )
        found.update(_parse_pids(result.stdout or ""))
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["fuser", f"{COMFY_PORT}/tcp"],
            capture_output=True,
            text=True,
        )
        found.update(_parse_pids((result.stdout or "") + " " + (result.stderr or "")))
    except OSError:
        pass
    found.discard(os.getpid())
    return sorted(pid for pid in found if pid > 1)


def kill_comfy_process(timeout: float = 10) -> None:
    """SIGTERM then SIGKILL leftover Comfy processes if systemd stop did not."""
    pids = comfy_pids()
    if not pids:
        return
    print(f"  killing ComfyUI process {', '.join(str(pid) for pid in pids)}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            print(f"  SIGTERM {pid} failed: {exc}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not comfy_up() and not comfy_pids():
            print("  ComfyUI process gone")
            return
        time.sleep(0.3)
    for pid in comfy_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  SIGKILL ComfyUI pid {pid}")
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(0.4)


def comfy_unit_active() -> bool:
    """True if the user unit is active (HTTP may still be coming up)."""
    if os.name == "nt" or not comfy_user_unit_path().is_file():
        return False
    result = systemctl_user(
        "is-active",
        "--quiet",
        "comfyui",
        capture_output=True,
    )
    return result.returncode == 0


def stop_comfy_via_systemd() -> bool:
    """Stop the user unit so Flux cannot keep the GPU."""
    if os.name == "nt":
        return False
    if not comfy_user_unit_path().is_file():
        return False
    result = systemctl_user(
        "stop",
        "comfyui",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"  systemctl --user stop comfyui failed: {err}")
        return False
    print("  Stopped ComfyUI via systemd")
    return True


VRAM_DRAIN_MAX_MIB = 2048
VRAM_DRAIN_TIMEOUT_S = 24.0


def gpu_used_mib() -> Optional[int]:
    """nvidia-smi used VRAM on GPU 0, or None if the query fails."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
        return int(out.strip().splitlines()[0])
    except (
        OSError,
        ValueError,
        IndexError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None


def wait_gpu_vram_drain(
    timeout: float = VRAM_DRAIN_TIMEOUT_S,
    max_used_mib: int = VRAM_DRAIN_MAX_MIB,
) -> None:
    """Wait until driver-reported used VRAM drops after Comfy exits."""
    deadline = time.time() + timeout
    used = gpu_used_mib()
    if used is None:
        time.sleep(1)
        return
    while True:
        if used <= max_used_mib:
            print(f"  GPU VRAM {used} MiB")
            return
        if time.time() >= deadline:
            print(f"  GPU VRAM still {used} MiB after stop; LLM load may hit VRAM")
            return
        time.sleep(0.4)
        nxt = gpu_used_mib()
        if nxt is None:
            return
        used = nxt


def stop_comfy(timeout: float = 30) -> None:
    """Give the GPU back before an LLM load.

    `/free` is not enough: Comfy stays up and can still hold VRAM
    (RAM-pressure cache). Stop the unit, then wait until it is gone.
    If systemd cannot talk to the user bus, kill the process.
    """
    http_up = comfy_up()
    if not http_up and not comfy_unit_active() and not comfy_pids():
        return
    if http_up:
        interrupt_comfy()
        free_comfy()
    stopped = stop_comfy_via_systemd()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not comfy_up() and not comfy_pids():
            print("  ComfyUI is down")
            wait_gpu_vram_drain()
            return
        time.sleep(0.4)
    if comfy_up() or not stopped or comfy_pids():
        print("  ComfyUI still answering after stop; killing process")
        kill_comfy_process()
    if not comfy_up() and not comfy_pids():
        print("  ComfyUI is down")
        wait_gpu_vram_drain()
        return
    print("  ComfyUI still answering after stop; LLM load may hit VRAM")


def nvidia_lib_dirs(comfy_dir: Optional[Path] = None) -> list[str]:
    """venv nvidia/*/lib folders so torchaudio can find libcudart."""

    root = Path(comfy_dir) if comfy_dir is not None else COMFY_DIR
    found = []
    for nvidia in root.glob("venv/lib/python*/site-packages/nvidia"):
        for lib in sorted(nvidia.glob("*/lib")):
            if lib.is_dir():
                found.append(str(lib))
    return found


def comfy_user_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / "comfyui.service"


def format_comfy_journal_line(line: str) -> str:
    text = line.rstrip("\n")
    if text.startswith("[comfy] "):
        return text
    return f"[comfy] {text}"


_comfy_journal_lock = threading.Lock()
_comfy_journal_started = False


def start_comfy_journal_forwarder() -> None:
    """Copy ComfyUI journal lines into this process stdout (tabbyapi unit)."""
    global _comfy_journal_started
    if os.name == "nt":
        return
    with _comfy_journal_lock:
        if _comfy_journal_started:
            return
        _comfy_journal_started = True
    thread = threading.Thread(
        target=_pump_comfy_journal,
        name="comfy-journal",
        daemon=True,
    )
    thread.start()


def _pump_comfy_journal() -> None:
    cmd = [
        "journalctl",
        "--user",
        "-u",
        "comfyui",
        "-f",
        "-n",
        "20",
        "-o",
        "cat",
        "--no-pager",
    ]
    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=user_systemd_env(),
            )
            assert proc.stdout is not None
            for raw in proc.stdout:
                text = raw.rstrip("\n")
                if text:
                    print(format_comfy_journal_line(text), flush=True)
            proc.wait()
        except Exception as exc:
            print(f"[comfy] journal forwarder: {exc}", flush=True)
        time.sleep(2)


def start_comfy_via_systemd() -> bool:
    """Start the user unit. Logs are also copied into journalctl --user -u tabbyapi."""
    if os.name == "nt":
        return False
    if not comfy_user_unit_path().is_file():
        return False
    result = systemctl_user(
        "start",
        "comfyui",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"  systemctl --user start comfyui failed: {err}")
        return False
    print("  Started ComfyUI via systemd")
    return True


def start_comfy_if_needed(timeout: float = 90) -> None:
    if comfy_up():
        print("  ComfyUI already running")
        return
    if not COMFY_PYTHON.exists():
        raise SystemExit(f"ComfyUI python missing: {COMFY_PYTHON}")
    if not (COMFY_DIR / "main.py").exists():
        raise SystemExit(f"ComfyUI main.py missing in {COMFY_DIR}")

    print(f"  Starting ComfyUI ({COMFY_DIR})...")
    if start_comfy_via_systemd():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if comfy_up():
                print("  ComfyUI is up")
                return
            time.sleep(1)
        raise SystemExit(
            f"ComfyUI did not start within {timeout:.0f}s. See journalctl --user -u tabbyapi"
        )

    COMFY_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = COMFY_LOG.open("a", encoding="utf-8")
    env = os.environ.copy()
    extra = nvidia_lib_dirs()
    if extra:
        current = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(extra + ([current] if current else []))
    cmd = [
        str(COMFY_PYTHON),
        "-u",
        "main.py",
        "--listen",
        COMFY_HOST,
        "--port",
        COMFY_PORT,
    ]
    kwargs: dict = {
        "cwd": str(COMFY_DIR),
        "env": env,
    }
    if os.name == "nt":
        kwargs["stdout"] = log
        kwargs["stderr"] = log
        kwargs["creationflags"] = (
            0x00000008 | 0x00000200
        )  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        # Fallback when the user unit is missing: still land in the journal.
        if _have_systemd_cat():
            cmd = ["systemd-cat", "-t", "comfyui", "--", *cmd]
            kwargs["stdout"] = None
            kwargs["stderr"] = None
            print("  ComfyUI logs via systemd-cat (also in journalctl --user -u tabbyapi)")
        else:
            kwargs["stdout"] = log
            kwargs["stderr"] = log
    subprocess.Popen(cmd, **kwargs)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if comfy_up():
            print("  ComfyUI is up")
            return
        time.sleep(1)
    raise SystemExit(f"ComfyUI did not start within {timeout:.0f}s. See {COMFY_LOG}")


def _have_systemd_cat() -> bool:
    from shutil import which

    return which("systemd-cat") is not None


def load_workflow(path: Optional[Path] = None) -> dict:
    target = path or WORKFLOW_PATH
    if not target.exists():
        raise FileNotFoundError(f"Missing workflow: {target}")
    return json.loads(target.read_text(encoding="utf-8"))


def wants_qwen_image(prompt: str) -> bool:
    """True when the chat line needs readable words, not a Flux draft."""
    from common.image_prompts import (
        FLUX_PREFIX,
        FORCE_FLUX_RE,
        SCENE_TAIL,
        rewrite_comfy_prompt,
    )

    if FORCE_FLUX_RE.search(prompt or "") or FLUX_PREFIX.match(prompt or ""):
        return False
    text = rewrite_comfy_prompt(prompt or "")
    if SCENE_TAIL in text or FLUX_PREFIX.match(text):
        return False
    if QWEN_IMAGE_PREFIX.match(text):
        return True
    return bool(QWEN_IMAGE_HINTS.search(text))


def qwen_image_prompt_text(prompt: str) -> str:
    match = QWEN_IMAGE_PREFIX.match(prompt or "")
    if match:
        cleaned = (match.group(1) or "").strip()
        return cleaned or (prompt or "").strip()
    return (prompt or "").strip()


def flux_prompt_text(prompt: str) -> str:
    from common.image_prompts import FLUX_PREFIX

    match = FLUX_PREFIX.match(prompt or "")
    if match:
        cleaned = (match.group(1) or "").strip()
        return cleaned or (prompt or "").strip()
    return (prompt or "").strip()


def parse_size(size: Optional[str]) -> tuple[int, int]:
    if not size:
        return 1024, 1024
    text = size.lower().replace(" ", "")
    if "x" not in text:
        raise ValueError("size must look like 1024x1024")
    width_s, height_s = text.split("x", 1)
    width, height = int(width_s), int(height_s)
    if width < 256 or height < 256 or width > 2048 or height > 2048:
        raise ValueError("size must be between 256 and 2048")
    width = (width // 16) * 16
    height = (height // 16) * 16
    return width, height


def build_prompt(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 4,
) -> dict:
    graph = load_workflow()
    graph["1"]["inputs"]["ckpt_name"] = CHECKPOINT_NAME
    graph["2"]["inputs"]["text"] = flux_prompt_text(prompt)
    graph["5"]["inputs"]["width"] = width
    graph["5"]["inputs"]["height"] = height
    graph["6"]["inputs"]["seed"] = int(seed)
    graph["6"]["inputs"]["steps"] = int(steps)
    return graph


def build_qwen_image_prompt(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = QWEN_IMAGE_STEPS,
) -> dict:
    graph = load_workflow(QWEN_IMAGE_WORKFLOW_PATH)
    graph["1"]["inputs"]["unet_name"] = QWEN_IMAGE_UNET
    graph["2"]["inputs"]["clip_name"] = QWEN_IMAGE_CLIP
    graph["3"]["inputs"]["vae_name"] = QWEN_IMAGE_VAE
    graph["4"]["inputs"]["text"] = qwen_image_prompt_text(prompt)
    graph["6"]["inputs"]["width"] = width
    graph["6"]["inputs"]["height"] = height
    graph["7"]["inputs"]["lora_name"] = QWEN_IMAGE_LORA
    graph["9"]["inputs"]["seed"] = int(seed)
    graph["9"]["inputs"]["steps"] = int(steps)
    return graph


def build_img2img_prompt(
    prompt: str,
    image_name: str,
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 4,
    denoise: float = 0.75,
) -> dict:
    graph = load_workflow(IMG2IMG_WORKFLOW_PATH)
    graph["1"]["inputs"]["ckpt_name"] = CHECKPOINT_NAME
    graph["2"]["inputs"]["text"] = prompt
    graph["9"]["inputs"]["image"] = image_name
    graph["10"]["inputs"]["width"] = width
    graph["10"]["inputs"]["height"] = height
    graph["6"]["inputs"]["seed"] = int(seed)
    graph["6"]["inputs"]["steps"] = int(steps)
    graph["6"]["inputs"]["denoise"] = float(denoise)
    return graph


def upload_input_image(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Source image missing: {path}")
    filename = path.name
    boundary = "----TabbyImg2Img"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header + path.read_bytes() + footer
    req = Request(
        f"{COMFY_URL}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    if not isinstance(result, dict):
        raise RuntimeError(f"ComfyUI upload failed: {result}")
    name = result.get("name") or filename
    sub = result.get("subfolder") or ""
    return f"{sub}/{name}" if sub else str(name)


def _first_image_ref(history_item: dict) -> dict:
    outputs = history_item.get("outputs") or {}
    for node_out in outputs.values():
        images = node_out.get("images") or []
        if images:
            return images[0]
    raise RuntimeError("ComfyUI finished but produced no images")


PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_TEXT_CHUNKS = {b"tEXt", b"zTXt", b"iTXt"}


def strip_png_text(raw: bytes) -> bytes:
    """Drop PNG text chunks. Comfy embeds the workflow JSON there; Cursor WebFetch
    then saves that JSON as a .png instead of the pixels."""

    if not raw.startswith(PNG_SIG):
        return raw
    out = bytearray(PNG_SIG)
    index = 8
    while index + 8 <= len(raw):
        length = int.from_bytes(raw[index : index + 4], "big")
        chunk_end = index + 12 + length
        if chunk_end > len(raw):
            break
        kind = raw[index + 4 : index + 8]
        if kind not in _PNG_TEXT_CHUNKS:
            out.extend(raw[index:chunk_end])
        index = chunk_end
    return bytes(out)


def fetch_comfy_image(image_ref: dict) -> bytes:
    query = urlencode(
        {
            "filename": image_ref.get("filename", ""),
            "subfolder": image_ref.get("subfolder", ""),
            "type": image_ref.get("type", "output"),
        }
    )
    req = Request(f"{COMFY_URL}/view?{query}")
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def generate_image(
    prompt: str,
    size: Optional[str] = "1024x1024",
    seed: int = 0,
    timeout: float = 300,
    source_image: Optional[Path] = None,
    denoise: Optional[float] = None,
) -> bytes:
    from common.image_prompts import rewrite_comfy_prompt

    prompt = rewrite_comfy_prompt(prompt)
    if not comfy_up():
        raise RuntimeError(f"ComfyUI is not running at {COMFY_HOST}:{COMFY_PORT}")
    width, height = parse_size(size)
    if source_image:
        uploaded = upload_input_image(Path(source_image))
        strength = 0.75 if denoise is None else float(denoise)
        graph = build_img2img_prompt(
            prompt,
            uploaded,
            width=width,
            height=height,
            seed=seed,
            denoise=strength,
        )
    elif wants_qwen_image(prompt):
        timeout = max(timeout, QWEN_IMAGE_TIMEOUT)
        graph = build_qwen_image_prompt(prompt, width=width, height=height, seed=seed)
    else:
        graph = build_prompt(prompt, width=width, height=height, seed=seed)
    queued = request_json("POST", f"{COMFY_URL}/prompt", {"prompt": graph}, timeout=30)
    if not isinstance(queued, dict) or not queued.get("prompt_id"):
        raise RuntimeError(f"ComfyUI /prompt failed: {queued}")
    prompt_id = queued["prompt_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # Qwen-Image can block Comfy's HTTP thread for tens of seconds
            # after "prompt executed" while it writes the PNG. A 15s socket
            # timeout used to abort the whole job with a bare "timed out".
            history = request_json(
                "GET", f"{COMFY_URL}/history/{prompt_id}", timeout=60
            )
        except TimeoutError:
            time.sleep(0.5)
            continue
        except HTTPError:
            raise
        except URLError as exc:
            if not _is_http_timeout(exc):
                raise
            time.sleep(0.5)
            continue
        if isinstance(history, dict) and prompt_id in history:
            item = history[prompt_id]
            status = item.get("status") or {}
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI job failed: {status}")
            if item.get("outputs") or status.get("completed"):
                return strip_png_text(fetch_comfy_image(_first_image_ref(item)))
        time.sleep(0.5)
    interrupt_comfy()
    raise TimeoutError(f"ComfyUI job {prompt_id} timed out after {timeout:.0f}s")


def png_bytes_from_upload(raw: bytes) -> bytes:
    """Turn a PNG/JPEG/WebP/GIF upload into gallery PNG bytes."""
    import io

    from PIL import Image, ImageOps

    data = raw if isinstance(raw, (bytes, bytearray)) else bytes(raw or b"")
    if len(data) > GALLERY_UPLOAD_MAX_BYTES:
        raise ValueError("Image must be under 8 MB.")
    if len(data) < 32:
        raise ValueError("That file is not a valid image.")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            image.load()
            if image.width * image.height > GALLERY_UPLOAD_MAX_PIXELS:
                raise ValueError("Image dimensions are too large.")
            if max(image.width, image.height) > GALLERY_UPLOAD_MAX_EDGE:
                image.thumbnail(
                    (GALLERY_UPLOAD_MAX_EDGE, GALLERY_UPLOAD_MAX_EDGE),
                    Image.Resampling.LANCZOS,
                )
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                image = image.convert("RGBA")
            elif image.mode != "RGB":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Upload a PNG, JPEG, WebP, or GIF.") from exc


def save_generated_image(
    raw: bytes, owner: str | None = None, *, as_latest: bool = True
) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = GENERATED_DIR / f"generated-{stamp}-{os.getpid()}.png"
    if dest.exists():
        dest = (
            GENERATED_DIR / f"generated-{stamp}-{os.getpid()}-{int(time.time() * 1000) % 1000}.png"
        )
    raw = strip_png_text(raw)
    dest.write_bytes(raw)
    if as_latest:
        latest = GENERATED_DIR / "generated-latest.png"
        latest.write_bytes(raw)
    ensure_gallery_thumb(dest)
    if owner:
        from common.gallery_owners import record_owner

        record_owner(dest.name, owner)
    return dest


def _read_turn() -> dict:
    if not TURN_PATH.exists():
        return {}
    try:
        data = json.loads(TURN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def begin_image_turn(prompt: str, force_new: bool = False) -> float:
    """Start a new image batch when the user prompt changes.

    The same prompt within 180s reuses the turn so Cursor retries do not
    start another batch. force_new only applies when the prompt text changes
    or the previous turn is older than 180s.
    """
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    prev = _read_turn()
    started = float(prev.get("started") or 0)
    same_prompt = (prev.get("prompt") or "") == prompt
    age = now - started if started else 9999
    if same_prompt and started and age < 180:
        return started
    started = now - 0.25
    TURN_PATH.write_text(
        json.dumps({"started": started, "prompt": prompt}),
        encoding="utf-8",
    )
    return started


def turn_images_ready(prompt: str, count: int) -> list[Path]:
    """PNGs already saved for this prompt's current turn (newest last)."""

    prev = _read_turn()
    if (prev.get("prompt") or "") != prompt:
        return []
    started = float(prev.get("started") or 0)
    if not started or time.time() - started > 180:
        return []
    files = recent_generated_files(since=started)
    return files[: max(0, int(count))]


def current_turn_started() -> float:
    started = float(_read_turn().get("started") or 0)
    return started if started else time.time()


def recent_generated_files(
    window_sec: Optional[float] = None,
    since: Optional[float] = None,
) -> list[Path]:
    """Timestamped Flux PNGs for the current request only (not the latest alias)."""
    if not GENERATED_DIR.exists():
        return []
    if since is None:
        since = (time.time() - window_sec) if window_sec is not None else current_turn_started()
    files = []
    for path in GENERATED_DIR.glob("generated-*.png"):
        if path.name == "generated-latest.png":
            continue
        try:
            if path.stat().st_mtime >= since:
                files.append(path)
        except OSError:
            continue
    files.sort(key=lambda item: item.stat().st_mtime)
    return files


def gallery_page(
    files: list[Path],
    page: int = 1,
    per_page: int = 24,
) -> tuple[list[Path], int, int, int]:
    """Return (slice, page, pages, per_page) for the image gallery."""
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 24
    per_page = max(6, min(per_page, 60))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (len(files) + per_page - 1) // per_page) if files else 1
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    return files[start : start + per_page], page, pages, per_page


def delete_generated_images(
    names: Optional[list[str]] = None, *, delete_all: bool = False
) -> list[str]:
    """Unlink generated-*.png files (and their thumbs). Never deletes other files."""
    if delete_all:
        targets = list(list_generated_files())
        latest = GENERATED_DIR / "generated-latest.png"
        if latest.is_file():
            targets.append(latest)
    else:
        targets = []
        seen: set[str] = set()
        for raw in names or []:
            path = generated_image_path(str(raw))
            if not path or path.name in seen:
                continue
            seen.add(path.name)
            targets.append(path)
    removed: list[str] = []
    for path in targets:
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path.name)
        thumb = GENERATED_DIR / "thumbs" / f"{path.stem}.jpg"
        try:
            if thumb.is_file():
                thumb.unlink()
        except OSError:
            pass
    if removed:
        from common.gallery_owners import forget_owners

        forget_owners(removed)
    return removed


def list_generated_files() -> list[Path]:
    """All timestamped Flux PNGs, newest first. Skips the latest alias."""
    if not GENERATED_DIR.exists():
        return []
    files = []
    for path in GENERATED_DIR.glob("generated-*.png"):
        if path.name == "generated-latest.png":
            continue
        try:
            if path.is_file():
                files.append(path)
        except OSError:
            continue
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files


def public_image_url(
    filename: str = "generated-latest.png",
    bust: bool = True,
    api_base: Optional[str] = None,
    request=None,
) -> str:
    base = (api_base or public_api_base(request)).rstrip("/")
    url = f"{base}/images/{filename}"
    if bust:
        url = f"{url}?t={int(time.time())}"
    return url


# Live saver names: generated-YYYYMMDD-HHMMSS-PID.png (optional extra suffix).
TIMESTAMPED_GENERATED_PNG_RE = re.compile(
    r"^generated-\d{8}-\d{6}-\d+(?:-\d+)?\.png$"
)


def is_public_generated_png(name: str) -> bool:
    """Timestamped gallery PNGs can be fetched without a bearer (coding-PC curl)."""
    return bool(TIMESTAMPED_GENERATED_PNG_RE.match(name or ""))


def generated_image_path(name: str) -> Optional[Path]:
    if name in ("latest", "latest.png"):
        name = "generated-latest.png"
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    if not name.endswith(".png") or not name.startswith("generated-"):
        return None
    path = (GENERATED_DIR / name).resolve()
    if path.parent != GENERATED_DIR.resolve() or not path.is_file():
        return None
    return path


def _png_name_for_thumb(name: str) -> str:
    if name.endswith(".jpg"):
        return name[: -len(".jpg")] + ".png"
    return name


def gallery_thumb_href(name: str) -> str:
    """Relative URL for the JPEG preview of a generated-*.png file."""
    stem = Path(name).stem
    return f"thumbs/{stem}.jpg"


def ensure_gallery_thumb(src: Path) -> Optional[Path]:
    """Write a small JPEG next to the gallery so the grid does not load full PNGs."""
    dest = GENERATED_DIR / "thumbs" / f"{src.stem}.jpg"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
            return dest
        from PIL import Image

        tmp = dest.with_name(dest.name + ".tmp")
        with Image.open(src) as im:
            if im.mode == "RGBA":
                bg = Image.new("RGB", im.size, (221, 221, 221))
                bg.paste(im, mask=im.split()[-1])
                rgb = bg
            else:
                rgb = im.convert("RGB")
            rgb.thumbnail((GALLERY_THUMB_MAX, GALLERY_THUMB_MAX))
            rgb.save(tmp, "JPEG", quality=GALLERY_THUMB_QUALITY, optimize=True)
        tmp.replace(dest)
        return dest
    except (OSError, ValueError):
        return None


def generated_thumb_path(name: str) -> Optional[Path]:
    """Safe JPEG preview for a generated PNG. Creates the file on first use."""
    src = generated_image_path(_png_name_for_thumb(name))
    if not src:
        return None
    return ensure_gallery_thumb(src)
