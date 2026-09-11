#!/usr/bin/python
"""CPU-rendered TTY kiosk: stack activity as a thermal field.

Does not import TabbyAPI or CUDA. Polls GET /v1/ui/saver/state on localhost.
Pygame draws into RAM (SDL dummy). Pixels go to /dev/fb0 with the CPU.
Never open kmsdrm/EGL/GL — NVIDIA's kmsdrm driver ignores LIBGL_ALWAYS_SOFTWARE
and steals SM time from the LLM.
"""

from __future__ import annotations

import argparse
import colorsys
import fcntl
import glob
import json
import math
import mmap
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BG = (11, 13, 18)
TEXT = (232, 236, 244)
MUTED = (154, 163, 181)
ACCENT = (122, 162, 255)
ACCENT2 = (139, 92, 246)
WARN = (245, 197, 66)
OK = (61, 214, 140)
DOWN = (48, 10, 14)
DOWN_TEXT = (232, 96, 90)

VT_ACTIVATE = 0x5606
VT_WAITACTIVE = 0x5607
FBIOGET_VSCREENINFO = 0x4600
# In-memory B,G,R,X — what nvidia-drm fbdev and most 32bpp consoles expose.
XRGB_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
KDSETMODE = 0x4B3A
KD_TEXT = 0
KD_GRAPHICS = 1
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
_GETTY_COMMS = frozenset({"agetty", "getty", "mingetty", "login", "(sd-pam)", "systemd"})
# Click or key drops the field. Motion only peeks the HUD — a wireless
# mouse or HOTAS axis twitch must not tear down the 4K present path.
_DISMISS_EVENT_NAMES = (
    "KEYDOWN",
    "KEYUP",
    "MOUSEBUTTONDOWN",
    "MOUSEBUTTONUP",
    "JOYBUTTONDOWN",
)
_IGNORE_INPUT_NAMES = (
    "stick",
    "throttle",
    "joystick",
    "gamepad",
    "hotas",
    "flight",
    "yubi",
    "speaker",
    "headphone",
    "hdmi",
    "microphone",
    "front mic",
    "rear mic",
    "line out",
    "pcm=",
)
HUD_IDLE_HOLD_S = 300.0
HUD_IDLE_FADE_S = 12.0
HUD_IDLE_HIDE_ALPHA = 0.02
HUD_IDLE_PEEK_GRACE_S = 1.0
# 1px dark ring around HUD glyphs so they stay readable on bloom.
HUD_HALO_RADIUS = 1
IDLE_FIELD_HUE_HOLD_S = 300.0
IDLE_FIELD_HUE_BLEND_S = 40.0
# Idle only breathes (~0.25 Hz) and drifts a few px/s. Each 4K present is a
# 33 MB framebuffer write that costs ~20 ms once the core has clocked down
# between frames, so idle paints at 8 fps; live states use --fps.
IDLE_FPS = 8

SIN_BITS = 12
SIN_SIZE = 1 << SIN_BITS
SIN_MASK = SIN_SIZE - 1
SIN_LUT = [math.sin(i * (2.0 * math.pi / SIN_SIZE)) for i in range(SIN_SIZE)]
TWO_PI = 2.0 * math.pi


def lsin(x: float) -> float:
    return SIN_LUT[int(x * (SIN_SIZE / TWO_PI)) & SIN_MASK]


def prepare_kiosk_sdl_env() -> None:
    """Force CPU pygame. NVIDIA kmsdrm/EGL ignores LIBGL_ALWAYS_SOFTWARE."""
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_RENDER_DRIVER"] = "software"
    os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ.pop("DISPLAY", None)
    os.environ.pop("WAYLAND_DISPLAY", None)


def read_fb0_geometry(
    sys_dir: str = "/sys/class/graphics/fb0",
) -> tuple[int, int, int, int]:
    """width, height, stride bytes, bits per pixel."""
    base = Path(sys_dir)
    width_s, height_s = base.joinpath("virtual_size").read_text(encoding="ascii").strip().split(",", 1)
    width, height = int(width_s), int(height_s)
    stride = int(base.joinpath("stride").read_text(encoding="ascii").strip())
    bpp_path = base / "bits_per_pixel"
    bpp = int(bpp_path.read_text(encoding="ascii").strip()) if bpp_path.exists() else 32
    if width < 64 or height < 36:
        raise ValueError(f"fb0 too small: {width}x{height}")
    if bpp != 32:
        raise ValueError(f"fb0 bits_per_pixel {bpp} is not 32")
    if stride < width * 4:
        raise ValueError(f"fb0 stride {stride} too small for width {width}")
    return width, height, stride, bpp


def fb_channel_masks(fd: int) -> tuple[int, int, int, int] | None:
    """(r, g, b, 0) bitmasks from FBIOGET_VSCREENINFO; None when unreadable.

    The console ignores the fourth byte, so it is never treated as alpha.
    """
    try:
        raw = fcntl.ioctl(fd, FBIOGET_VSCREENINFO, bytes(160))
    except OSError:
        return None
    try:
        fields = struct.unpack_from("20I", raw, 0)
    except struct.error:
        return None
    if fields[6] != 32:
        return None
    masks: list[int] = []
    for idx in range(3):
        offset, length = fields[8 + idx * 3], fields[9 + idx * 3]
        if length <= 0 or length > 8 or offset + length > 32:
            return None
        masks.append(((1 << length) - 1) << offset)
    return masks[0], masks[1], masks[2], 0


def compose_size(fb_w: int, fb_h: int, max_edge: int = 1280) -> tuple[int, int]:
    """Pygame draw size. Keep 4K fb presents as a cheap nearest upsample."""
    width = max(64, int(fb_w))
    height = max(36, int(fb_h))
    long = max(width, height)
    cap = max(64, int(max_edge))
    if long <= cap:
        return width, height
    scale = cap / long
    return max(64, int(round(width * scale))), max(36, int(round(height * scale)))


class CpuFramebuffer:
    """CPU present path: mmap /dev/fb0. Does not open DRM, EGL, GL, or NVIDIA."""

    def __init__(
        self,
        dev: str = "/dev/fb0",
        sys_dir: str = "/sys/class/graphics/fb0",
    ) -> None:
        self.width, self.height, self.stride, self.bpp = read_fb0_geometry(sys_dir)
        self.fd = os.open(dev, os.O_RDWR)
        self.mem = mmap.mmap(self.fd, self.stride * self.height)
        self.masks = fb_channel_masks(self.fd) or XRGB_MASKS
        self._staging: Any = None
        self._rows: Any = None
        self._pad_cleared = False

    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def _fb_rows(self, np_mod: Any) -> Any:
        if self._rows is None:
            self._rows = np_mod.ndarray((self.height, self.stride), dtype=np_mod.uint8, buffer=self.mem)
        if not self._pad_cleared:
            # Bytes past each row's pixels are never shown; zero them once.
            if self.stride > self.width * 4:
                self._rows[:, self.width * 4 :] = 0
            self._pad_cleared = True
        return self._rows

    def _staging_surface(self, pygame_mod: Any, height: int) -> Any:
        if self._staging is None or self._staging.get_height() != height:
            # Same masks as the compose surface (see _init_display), so
            # transform.scale can write into it with no conversion.
            self._staging = pygame_mod.Surface((self.width, height), 0, 32, self.masks)
        return self._staging

    def present_surface(self, pygame_mod: Any, surface: Any) -> None:
        """Nearest upsample into fb0 with one C scale pass and one row copy.

        Nearest scaling repeats rows, so when fb height is a multiple of the
        compose height (4K from 1280x720) only a width-wise scale is needed;
        numpy then broadcasts each scaled row ky times straight into the
        mapping at memcpy speed. Other ratios scale to full size first.

        The old path pulled the frame through surfarray, gathered a 4K nearest
        upsample in numpy, and wrote four strided byte planes into the mmap.
        On a 3840x2160 console that alone was ~120 ms a frame.
        """
        import numpy as np

        row_bytes = self.width * 4
        rows = self._fb_rows(np)
        ch = max(1, int(surface.get_height()))
        ky = self.height // ch if self.height % ch == 0 else 1
        stage_h = self.height // ky
        stage = self._staging_surface(pygame_mod, stage_h)
        pygame_mod.transform.scale(surface, (self.width, stage_h), stage)
        view = stage.get_buffer()
        src = np.frombuffer(view, dtype=np.uint8).reshape(stage_h, 1, stage.get_pitch())
        rows.reshape(stage_h, ky, self.stride)[:, :, :row_bytes] = src[:, :, :row_bytes]
        # Drop the buffer proxy now so the staging surface is unlocked for the next frame.
        del src, view

    def close(self) -> None:
        self._staging = None
        self._rows = None
        try:
            self.mem.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def set_tty_graphics(enabled: bool) -> None:
    mode = KD_GRAPHICS if enabled else KD_TEXT
    for stream in (sys.stdin, sys.stdout):
        try:
            fd = stream.fileno()
        except Exception:
            continue
        try:
            fcntl.ioctl(fd, KDSETMODE, mode)
            return
        except OSError:
            continue


def _mix(c0: tuple[int, int, int], c1: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return (
        int(c0[0] + (c1[0] - c0[0]) * t),
        int(c0[1] + (c1[1] - c0[1]) * t),
        int(c0[2] + (c1[2] - c0[2]) * t),
    )


def _palette(stops: list[tuple[float, tuple[int, int, int]]]) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for i in range(256):
        t = i / 255.0
        color = stops[-1][1]
        for j in range(len(stops) - 1):
            p0, c0 = stops[j]
            p1, c1 = stops[j + 1]
            if t <= p1 or j == len(stops) - 2:
                span = p1 - p0
                u = 0.0 if span <= 0 else (t - p0) / span
                color = _mix(c0, c1, u)
                break
        out.append(color)
    return out


PALETTES = {
    "idle": _palette(
        [
            (0.0, BG),
            (0.28, (30, 40, 68)),
            (0.62, (52, 68, 118)),
            (1.0, (74, 96, 156)),
        ]
    ),
    "chat": _palette(
        [
            (0.0, BG),
            (0.22, (32, 40, 78)),
            (0.48, (70, 92, 156)),
            (0.74, (86, 60, 144)),
            (1.0, (136, 60, 92)),
        ]
    ),
    "image": _palette([(0.0, BG), (0.42, (42, 30, 12)), (1.0, (148, 112, 42))]),
    "switch": _palette([(0.0, BG), (0.45, (12, 38, 30)), (1.0, (36, 112, 78))]),
    "down": _palette([(0.0, (6, 3, 4)), (0.42, (32, 8, 10)), (1.0, (96, 22, 28))]),
}


def _shift_color(color: tuple[int, int, int], hue_delta: float) -> tuple[int, int, int]:
    """Rotate hue; leave near-black / gray stops so the field still sits on BG."""
    if abs(hue_delta) < 1e-6:
        return color
    r, g, b = color
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if s < 0.08 or v < 0.08:
        return color
    nr, ng, nb = colorsys.hsv_to_rgb((h + hue_delta) % 1.0, s, v)
    return (int(nr * 255.0 + 0.5), int(ng * 255.0 + 0.5), int(nb * 255.0 + 0.5))


# Hue-shifted ramps keyed by (ramp identity, hue in 1/2048 turn). colorsys on
# 256 stops every frame was ~1.5 ms; a 0.18 degree step is invisible.
_SHIFT_Q = 2048.0
_SHIFT_CACHE: dict[tuple[int, int], tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]] = {}
_SHIFT_CACHE_MAX = 512


def _shift_ramp(
    ramp: list[tuple[int, int, int]], hue_delta: float
) -> list[tuple[int, int, int]]:
    if abs(hue_delta) < 1e-6:
        return ramp
    q = int(round((hue_delta % 1.0) * _SHIFT_Q)) % int(_SHIFT_Q)
    if q == 0:
        return ramp
    key = (id(ramp), q)
    hit = _SHIFT_CACHE.get(key)
    if hit is None or hit[0] is not ramp:
        if len(_SHIFT_CACHE) >= _SHIFT_CACHE_MAX:
            _SHIFT_CACHE.clear()
        shifted = [_shift_color(c, q / _SHIFT_Q) for c in ramp]
        _SHIFT_CACHE[key] = (ramp, shifted)
        return shifted
    return hit[1]


def _chat_hue_rate(speed: float, token_rate: float, chatty: bool) -> float:
    """Turns of the colour wheel per second. Faster decode / tokens → faster travel."""
    if not chatty:
        return 0.004
    return 0.016 + 0.030 * max(0.0, speed) + min(0.038, max(0.0, token_rate) * 0.0015)


def _fmt_runtime(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


_CHAT_RUN_PHASES = frozenset({"thinking", "using tools", "in use"})
_RUN_GAP_S = 12.0


def fmt_run_clock(run_s: float, step_s: float = 0.0) -> str:
    """Whole-run clock, plus this generate when it is clearly a later step."""
    run = _fmt_runtime(run_s)
    try:
        step = float(step_s or 0.0)
        whole = float(run_s or 0.0)
    except (TypeError, ValueError):
        return run
    if step >= 1.0 and whole >= step + 1.5:
        return f"{run}  step {_fmt_runtime(step)}"
    return run


def tok_hud_line(run_tokens: int, step_tokens: int, rate: float) -> str:
    """Run total + tok/s, and this step when it is not the whole run."""
    run = max(0, int(run_tokens or 0))
    step = max(0, int(step_tokens or 0))
    if run <= 0 and step <= 0 and rate < 0.5:
        return ""
    shown = max(run, step)
    if rate >= 0.5:
        line = f"{shown} tok   {int(round(rate))}/s"
    elif shown > 0:
        line = f"{shown} tok"
    else:
        line = ""
    if line and step > 0 and shown > step + 2:
        line = f"{line}   step {step}"
    return line


def idle_tod_hue(hour: float) -> float:
    """Idle field only: cooler after midnight, warmer around dusk."""
    h = hour % 24.0
    if 16.0 <= h < 21.0:
        peak = 18.5
        dist = abs(h - peak) / 2.5
        return -0.08 * max(0.0, 1.0 - dist)
    if h >= 21.0 or h < 5.0:
        return 0.08
    if 5.0 <= h < 8.0:
        return 0.04 * (1.0 - (h - 5.0) / 3.0)
    return 0.0


def lerp_hue(a: float, b: float, t: float) -> float:
    """Shortest-arc mix on the hue wheel (turns, 0–1)."""
    d = ((b - a) + 0.5) % 1.0 - 0.5
    return (a + d * _clamp01(t)) % 1.0


def idle_field_hue_for(cycle: int, seed: int = 0) -> float:
    return _u01(int(cycle) + 3, int(seed) + 9049)


def idle_field_hue(
    idle_s: float,
    seed: int = 0,
    hold_s: float = IDLE_FIELD_HUE_HOLD_S,
    blend_s: float = IDLE_FIELD_HUE_BLEND_S,
) -> float:
    """Hold a random idle field hue, then ease to the next one every hold_s."""
    hold = max(1.0, float(hold_s))
    blend = max(0.5, min(hold * 0.45, float(blend_s)))
    s = max(0.0, float(idle_s))
    cycle = int(s // hold)
    local = s - cycle * hold
    src = idle_field_hue_for(cycle, seed)
    dst = idle_field_hue_for(cycle + 1, seed)
    start = hold - blend
    if local < start:
        return src
    return lerp_hue(src, dst, _smoothstep((local - start) / blend))


def wall_clock_parts(stamp: float | None = None) -> tuple[str, str]:
    lt = time.localtime(stamp)
    date = f"{time.strftime('%a', lt)} {lt.tm_mday:2d} {time.strftime('%b', lt)}"
    return time.strftime("%H:%M:%S", lt), date


def idle_hud_alpha(
    idle_s: float,
    hold_s: float = HUD_IDLE_HOLD_S,
    fade_s: float = HUD_IDLE_FADE_S,
) -> float:
    """1 while the idle clock is up, then linear fade to 0 after hold_s.

    hold_s of 0 hides the idle text (no clock, no peek).
    """
    s = max(0.0, float(idle_s or 0.0))
    hold = max(0.0, float(hold_s))
    fade = max(0.0, float(fade_s))
    if hold <= 0.0:
        return 0.0
    if s <= hold:
        return 1.0
    if fade <= 0.0:
        return 0.0
    return _clamp01(1.0 - (s - hold) / fade)


def idle_hud_quiet(scene: dict[str, Any]) -> bool:
    cycle = str(scene.get("cycle") or "")
    down = str(scene.get("palette") or "") == "down" or not scene.get("connected")
    active = bool(scene.get("live")) or cycle in ("boot", "halt")
    return bool(scene.get("connected")) and not down and not active


_TIMES_PATH = Path(__file__).resolve().parents[2] / "model_profiles" / "switch_times.json"
# Real loads EMA into this untracked overlay (common/switch_times.record_ready).
_LOCAL_TIMES_PATH = _TIMES_PATH.with_name("switch_times.local.json")
_LAST_PROFILE_PATH = Path(__file__).resolve().parents[2] / "model_profiles" / "last.json"
_IDLE_TIMES: dict[str, Any] | None = None
# (mtime_ns, size) of both times files when _IDLE_TIMES was read. Live loads
# update the overlay, so the idle facts must follow it, not the boot copy.
_IDLE_TIMES_STAMP: tuple | None = None
_IDLE_TIMES_CHECKED: float = 0.0
_IDLE_TIMES_RECHECK_S = 5.0
_UNIT_STATE: tuple[float, str] = (0.0, "")
_SKIP_PROFILES = frozenset({"", "—", "-", "comfy", "flux", "llm", "restart"})


def _times_stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (int(st.st_mtime_ns), int(st.st_size))


def _read_times_file(target: Path) -> dict[str, Any]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_times(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Same rule as common.switch_times.merge_times: overlay fields win."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        prev = out.get(key)
        out[key] = {**prev, **value} if isinstance(value, dict) and isinstance(prev, dict) else value
    return out


def load_idle_times(
    path: Path | None = None,
    now: float | None = None,
    local_path: Path | None = None,
) -> dict[str, Any]:
    """Bench baseline + live overlay, re-read when either file changes."""
    global _IDLE_TIMES, _IDLE_TIMES_STAMP, _IDLE_TIMES_CHECKED
    target = path or _TIMES_PATH
    overlay_path = local_path or (
        target.with_name("switch_times.local.json") if path is not None else _LOCAL_TIMES_PATH
    )
    t = time.monotonic() if now is None else now
    if _IDLE_TIMES is not None and (t - _IDLE_TIMES_CHECKED) < _IDLE_TIMES_RECHECK_S:
        return _IDLE_TIMES
    _IDLE_TIMES_CHECKED = t
    stamp = (_times_stamp(target), _times_stamp(overlay_path))
    if _IDLE_TIMES is not None and stamp == _IDLE_TIMES_STAMP:
        return _IDLE_TIMES
    _IDLE_TIMES = merge_times(_read_times_file(target), _read_times_file(overlay_path))
    _IDLE_TIMES_STAMP = stamp
    return _IDLE_TIMES


def _ready_s(entry: Any) -> int | None:
    if isinstance(entry, dict) and entry.get("ready_s") is not None:
        try:
            return max(1, int(round(float(entry["ready_s"]))))
        except (TypeError, ValueError):
            return None
    return None


def read_last_profile(path: Path | None = None) -> str:
    target = path or _LAST_PROFILE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return ""
    name = str((data or {}).get("profile") or "").strip().lower()
    return "" if name in _SKIP_PROFILES else name


def profile_from_state(data: dict[str, Any] | None) -> str:
    raw = str((data or {}).get("profile") or "").strip()
    if raw.lower() not in _SKIP_PROFILES:
        return raw
    return read_last_profile()


def wait_s_for(name: str) -> int | None:
    key = (name or "").strip().lower()
    times = load_idle_times()
    if key in {"comfy", "flux", "image"}:
        return _ready_s(times.get("comfy"))
    if key:
        got = _ready_s(times.get(key))
        if got:
            return got
    return _ready_s(times.get("llm")) or _ready_s(times.get("qwen"))


def format_wait_s(seconds: int | None) -> str:
    if seconds is None:
        return ""
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    if secs < 90:
        rounded = int(5 * round(secs / 5.0)) if secs >= 5 else max(1, int(round(secs)))
        unit = "second" if rounded == 1 else "seconds"
        return f"{rounded} {unit}"
    minutes = max(1, int(round(secs / 60.0)))
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def tabbyapi_unit_state(now: float | None = None) -> str:
    """active / activating / failed / inactive / unknown. Cached ~2s."""
    global _UNIT_STATE
    t = time.monotonic() if now is None else now
    cached_t, cached = _UNIT_STATE
    if cached and (t - cached_t) < 2.0:
        return cached
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "tabbyapi"],
            capture_output=True,
            text=True,
            timeout=0.4,
            check=False,
        )
        state = (proc.stdout or proc.stderr or "").strip().splitlines()
        text = (state[0] if state else "").strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        text = "unknown"
    if text not in {"active", "activating", "failed", "inactive"}:
        text = "unknown"
    _UNIT_STATE = (t, text)
    return text


def scene_help_note(
    *,
    phase: str,
    connected: bool,
    data: dict[str, Any],
    unit_state: str = "",
) -> str:
    """Plain-language HUD line: what the box is doing and how long to wait."""
    profile = profile_from_state(data)
    want = str(data.get("switch_target") or "").strip().lower()
    waiters = 0
    try:
        waiters = int(round(float(data.get("waiters") or 0)))
    except (TypeError, ValueError):
        waiters = 0
    if phase in {"waiting for api", "restarting api"}:
        load_name = "comfy" if want == "comfy" else profile
        if unit_state == "failed":
            return "tabbyapi failed to start. open Logs, or send restart from Status."
        if unit_state == "inactive":
            return "tabbyapi is stopped. send restart from Status or in chat."
        loading = unit_state in {"active", "activating"}
        extra = ""
        if loading and phase == "waiting for api":
            extra = " the unit started; /health is still down while copying weights into VRAM."
        elif loading:
            extra = " the process is running; it has not opened the port yet."
        if phase == "waiting for api":
            if profile:
                wait = format_wait_s(wait_s_for(profile))
                how = f" wait about {wait} for /health." if wait else " wait for /health."
                return (
                    f"the API is coming up after a reboot or restart. last model is {profile}."
                    f"{how}{extra} first boot can compile longer."
                )
            qwen = format_wait_s(wait_s_for("qwen"))
            q35 = format_wait_s(wait_s_for("qwen35"))
            return (
                "the API is coming up after a reboot or restart. "
                f"/health stays down until the last model is in VRAM. "
                f"daily models take about {qwen}; qwen35 about {q35}."
                f"{extra} first boot can compile longer."
            )
        wait = format_wait_s(wait_s_for(load_name or "qwen"))
        who = profile or "the last model"
        how = f" wait about {wait} for /health." if wait else " wait for /health."
        if data.get("restarting") and connected:
            return f"restarting TabbyAPI. reloading {who}.{how}{extra}"
        return (
            f"the API dropped (reboot, restart, or a model switch). "
            f"reloading {who}.{how}{extra}"
        )
    if phase == "loading comfy":
        wait = format_wait_s(wait_s_for("comfy"))
        how = f" wait about {wait}." if wait else ""
        return (
            f"unloading the LLM and starting ComfyUI.{how} "
            "then describe an image. flux is the draft; prefix qwen-image: for readable text."
        )
    if phase == "loading llm":
        who = profile or "the last LLM"
        wait = format_wait_s(wait_s_for(profile or "llm"))
        how = f" wait about {wait}." if wait else ""
        return (
            f"loading {who} onto the GPU.{how} "
            "editors should keep the model name gpt-4o."
        )
    if phase == "resetting generator":
        return "VRAM recovery. Comfy is clearing, then the last LLM will reload."
    if phase == "using tools":
        extra = f" {waiters} more waiting." if waiters > 0 else ""
        return f"the model called a tool and is waiting for the workspace.{extra}"
    if phase == "thinking":
        extra = f" {waiters} more waiting." if waiters > 0 else ""
        return f"answering a chat or code request.{extra}"
    if phase == "in use":
        extra = f" {waiters} more waiting." if waiters > 0 else ""
        return f"the GPU is busy.{extra}"
    if phase == "rendering":
        if str(data.get("image_what") or "").strip():
            return ""
        return "drawing a picture. first flux is about 3 minutes; qwen-image about 4."
    if phase == "comfy":
        return "ComfyUI is loaded. describe an image in chat, or send switch to llm."
    return ""


def idle_fact_lines(
    times: dict[str, Any], idle_s: float, profile: str = "", mode: str = ""
) -> list[str]:
    facts: list[str] = []
    prof = str(profile or "").strip()
    if prof.lower() not in _SKIP_PROFILES:
        facts.append(f"ready  {prof}")
    mode_l = str(mode or "").strip().lower()
    if mode_l == "comfy":
        facts.append("comfy is loaded  describe an image in chat")
    elif mode_l == "llm" or prof.lower() not in _SKIP_PROFILES:
        facts.append("chat and code are ready  keep the editor model as gpt-4o")
        facts.append("say switch to comfy for pictures")
    gpu = str(times.get("gpu") or "").strip()
    if gpu:
        facts.append(f"this box is a {gpu}")
    qwen = _ready_s(times.get("qwen"))
    if qwen:
        facts.append(f"qwen warm switch ~{qwen}s")
    comfy = times.get("comfy") if isinstance(times.get("comfy"), dict) else {}
    flux = comfy.get("flux_s") if isinstance(comfy, dict) else None
    if flux is not None:
        try:
            mins = max(1, int(round(float(flux) / 60.0)))
            facts.append(f"flux first picture ~{mins} min")
        except (TypeError, ValueError):
            pass
    qimg = comfy.get("qwen_image_s") if isinstance(comfy, dict) else None
    if qimg is not None:
        try:
            mins = max(1, int(round(float(qimg) / 60.0)))
            facts.append(f"qwen-image first picture ~{mins} min")
        except (TypeError, ValueError):
            pass
    asleep = max(0, int(idle_s))
    if asleep >= 60:
        facts.append(f"asleep  {asleep // 60}m")
    elif asleep >= 12:
        facts.append(f"asleep  {asleep}s")
    return facts


def pick_idle_fact(facts: list[str], wall: float) -> str:
    if not facts:
        return ""
    return facts[int(wall // 45.0) % len(facts)]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def saver_url(base: str) -> str:
    root = (base or "http://127.0.0.1:5000").rstrip("/")
    return f"{root}/v1/ui/saver/state"


def origin_peer(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, int(port)


def tcp_up(host: str, port: int, timeout: float = 0.2) -> bool:
    """True when something is listening. Does not wait on the event loop."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True


def fetch_state(url: str, timeout: float = 0.8) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def state_is_busy(payload: dict[str, Any] | None, live: dict[str, Any] | None = None) -> bool:
    """True when the kiosk must follow a live job, not the idle wash."""
    for src in (live, payload):
        if not isinstance(src, dict):
            continue
        if src.get("busy") or src.get("live") or src.get("switching") or src.get("restarting"):
            return True
        stage = str(src.get("stage") or "").strip().lower()
        if stage in {"prefill", "decode", "tool", "image", "switch", "recover"}:
            return True
    return False


def http_poll_gap(*, busy: bool, idle_s: float = 1.0, busy_s: float = 0.1) -> float:
    """Idle HTTP stays slow so the API can start a generate. Busy stays snappy."""
    idle_s = max(0.2, float(idle_s))
    busy_s = max(0.08, min(float(busy_s), idle_s))
    return busy_s if busy else idle_s


_LIVE_PATH = Path(__file__).resolve().parents[2] / "saver-live.json"


def _pid_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_saver_live() -> dict[str, Any] | None:
    try:
        data = json.loads(_LIVE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Leftover from a killed API: no live pid (or an old file with none).
    if not _pid_alive(data.get("pid")):
        try:
            _LIVE_PATH.unlink()
        except OSError:
            pass
        return None
    return data


def overlay_saver_live(
    payload: dict[str, Any] | None,
    live: dict[str, Any] | None,
    *,
    trust_idle_http: bool = False,
) -> dict[str, Any] | None:
    """Apply the API sidecar when HTTP is idle or hung mid-prompt."""
    if not live or not live.get("busy"):
        return payload
    if live.get("pid") is not None and not _pid_alive(live.get("pid")):
        return payload
    if trust_idle_http and _http_status_idle(payload):
        return payload
    if payload and (
        payload.get("switching")
        or payload.get("restarting")
        or payload.get("recovering")
        or str(payload.get("stage") or "").strip().lower() == "switch"
    ):
        return payload
    out = dict(payload or {})
    out["busy"] = True
    stage = str(live.get("stage") or "prefill").strip().lower()
    if stage in {"prefill", "decode", "tool"}:
        out["stage"] = stage
    if not out.get("kind"):
        out["kind"] = "chat"
    try:
        tokens = int(live.get("tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens > 0:
        out["tokens"] = tokens
    try:
        run_tokens = int(live.get("run_tokens") or 0)
    except (TypeError, ValueError):
        run_tokens = 0
    if run_tokens > 0:
        out["run_tokens"] = run_tokens
    return out


def _http_status_idle(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if payload.get("busy") or payload.get("switching") or payload.get("restarting"):
        return False
    if payload.get("recovering"):
        return False
    stage = str(payload.get("stage") or "idle").strip().lower()
    return stage in ("", "idle")


def _exp_approach(current: float, target: float, dt: float, tau: float) -> float:
    if tau <= 0.0:
        return target
    return current + (target - current) * (1.0 - math.exp(-dt / tau))


def _clamp01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def _smoothstep(t: float) -> float:
    t = _clamp01(t)
    return t * t * (3.0 - 2.0 * t)


class SceneFollow:
    """Hold one continuous field: never snap phase, palette, or HUD on a poll."""

    _HOLD_LIVE_S = 0.7
    _BOOT_S = 1.25
    _HALT_S = 5.0
    _TAU_S = 2.4

    def __init__(self, hud_hold_s: float = HUD_IDLE_HOLD_S) -> None:
        self.hud_hold_s = max(0.0, float(hud_hold_s))
        self.intensity = 0.52
        self.speed = 0.34
        self.heat = 0.18
        self.util = 0.0
        self.vram = 0.0
        self.temp = 40.0
        self.cpu = 0.0
        self.st = 0.0
        # Wall-clock seed so a restart is not always the same blue/pink family.
        self.hue = (time.time() * 0.007) % 1.0
        self.live = False
        self._live_until = 0.0
        self.weights = {name: (1.0 if name == "idle" else 0.0) for name in PALETTES}
        self.phase = "idle"
        self.palette = "idle"
        self.mode = "—"
        self.profile = "—"
        self.connected = False
        self.overlay = 0.0
        self.cycle = "idle"
        self.cycle_t = 0.0
        self._cycle_started = 0.0
        self.tokens = 0.0
        self.token_rate = 0.0
        self.run_tokens = 0.0
        self._run_bank = 0.0
        self._run_t0 = 0.0
        self._step_t0 = 0.0
        self._last_chat = 0.0
        self.stage = "idle"
        self.has_gpu = False
        self.has_cpu = False
        self.image_n = 0.0
        self.image_of = 0.0
        self.image_file = ""
        self.image_what = ""
        self.note = ""
        self.task_name = ""
        self._task_t0 = 0.0
        self.runtime_s = 0.0
        self.waiters = 0.0
        self.elapsed_s = 0.0
        self.typical_s = 0.0
        self.idle_s = 0.0
        self._idle_t0 = 0.0
        self._idle_hue_seed = 0
        self.idle_hue = 0.0
        self.clock = ""
        self.date = ""
        self.idle_fact = ""

    def _hold_live(self, want: bool, now: float) -> bool:
        if want:
            self._live_until = now + self._HOLD_LIVE_S
            return True
        return now < self._live_until

    def _enter_cycle(self, name: str, now: float, t0: float = 0.0) -> None:
        self.cycle = name
        self.cycle_t = _clamp01(t0)
        if name == "boot":
            self._cycle_started = now - self.cycle_t * self._BOOT_S
        elif name == "halt":
            self._cycle_started = now - self.cycle_t * self._HALT_S
        else:
            self._cycle_started = now

    def wake_idle_hud(self, now: float) -> None:
        """Restart the idle HUD hold (mouse peek while the clock is hidden)."""
        self._idle_t0 = now
        self.idle_s = 0.0

    def _tick_cycle(self, held: bool, now: float) -> None:
        if held:
            if self.cycle == "idle":
                self._enter_cycle("boot", now)
            elif self.cycle == "halt":
                self._enter_cycle("boot", now, t0=max(0.0, 1.0 - self.cycle_t))
            if self.cycle == "boot":
                self.cycle_t = min(1.0, (now - self._cycle_started) / self._BOOT_S)
                if self.cycle_t >= 1.0:
                    self._enter_cycle("run", now)
                    self.cycle_t = 1.0
            elif self.cycle == "run":
                self.cycle_t = 1.0
            return
        if self.cycle in ("boot", "run"):
            t0 = 0.0 if self.cycle == "run" else max(0.0, 1.0 - self.cycle_t)
            self._enter_cycle("halt", now, t0=t0)
        if self.cycle == "halt":
            self.cycle_t = min(1.0, (now - self._cycle_started) / self._HALT_S)
            if self.cycle_t >= 1.0:
                self._enter_cycle("idle", now)
                self.cycle_t = 0.0

    def tick(self, target: dict[str, Any], dt: float, now: float) -> dict[str, Any]:
        dt = 0.0 if dt < 0.0 else 0.08 if dt > 0.08 else dt
        want_live = bool(target.get("live"))
        held = self._hold_live(want_live, now)
        self.live = held
        self._tick_cycle(held, now)
        if self.cycle == "boot":
            # Neurons must read as live on the first busy poll. The bloom still
            # uses cycle_t; do not fade the overlay in over a second.
            self.overlay = 1.0
        elif self.cycle == "run":
            self.overlay = 1.0
        elif self.cycle == "halt":
            # Linear over _HALT_S so they dim the whole way instead of snapping off.
            self.overlay = 1.0 - self.cycle_t
        else:
            self.overlay = 0.0
        if self.cycle != "halt" and self.overlay < 0.002:
            self.overlay = 0.0
        tau = 0.45 if (want_live or held) else 2.2
        self.intensity = _exp_approach(self.intensity, float(target["intensity"]), dt, tau)
        self.speed = _exp_approach(self.speed, float(target["speed"]), dt, tau)
        self.heat = _exp_approach(self.heat, float(target["heat"]), dt, tau)
        self.util = _exp_approach(self.util, float(target["util"]), dt, 1.6)
        self.vram = _exp_approach(self.vram, float(target["vram"]), dt, 1.6)
        self.temp = _exp_approach(
            self.temp, float(target["temp"]), dt, 0.55 if (want_live or held) else 1.6
        )
        self.cpu = _exp_approach(self.cpu, float(target.get("cpu") or 0.0), dt, 1.6)
        self.st += self.speed * dt
        dest = str(target.get("palette") or "idle")
        if dest not in self.weights:
            dest = "idle"
        down = dest == "down" or not bool(target.get("connected"))
        if dest == "down":
            for name in self.weights:
                self.weights[name] = 1.0 if name == dest else 0.0
        else:
            blend_tau = 0.12 if (want_live or held) else 1.8
            for name in self.weights:
                goal = 1.0 if name == dest else 0.0
                self.weights[name] = _exp_approach(self.weights[name], goal, dt, blend_tau)
        self.palette = max(self.weights, key=lambda name: self.weights[name])
        self.mode = str(target.get("mode") or self.mode)
        self.profile = str(target.get("profile") or self.profile)
        self.connected = bool(target.get("connected"))
        dest_tokens = max(0.0, float(target.get("tokens") or 0.0))
        dest_run = max(0.0, float(target.get("run_tokens") or dest_tokens))
        stage_now = str(target.get("stage") or self.stage or "idle")
        if dest_tokens + 1.0 < self.tokens:
            self._run_bank += self.tokens
            self.tokens = dest_tokens
            self._step_t0 = now
        else:
            self.tokens = dest_tokens
        if dest_run + 1.0 < self.run_tokens and dest_run <= dest_tokens + 1.0:
            dest_run = max(dest_run, self._run_bank + dest_tokens)
        self.run_tokens = max(dest_run, self._run_bank + dest_tokens)
        if stage_now == "decode" and dest_tokens > 0.0:
            step_s = max(0.08, now - self._step_t0) if self._step_t0 else dt
            inst = min(120.0, dest_tokens / step_s)
            self.token_rate = _exp_approach(self.token_rate, inst, dt, 0.28)
        # Keep last decode rate through tool / prefill so /s does not drop to 0
        # between steps of the same run.
        chatty = dest == "chat" or self.weights.get("chat", 0.0) > 0.18
        self.hue = (self.hue + _chat_hue_rate(self.speed, self.token_rate, chatty) * dt) % 1.0
        self.stage = stage_now
        self.has_gpu = bool(target.get("has_gpu"))
        self.has_cpu = bool(target.get("has_cpu"))
        self.image_n = _exp_approach(
            self.image_n, float(target.get("image_n") or 0.0), dt, 0.4
        )
        self.image_of = float(target.get("image_of") or 0.0)
        dest_file = str(target.get("image_file") or "").strip()
        dest_what = str(target.get("image_what") or "").strip()
        if dest_file:
            self.image_file = dest_file
        if dest_what:
            self.image_what = dest_what
        if self.cycle == "idle" and not held:
            self.image_file = dest_file
            self.image_what = dest_what
        self.note = str(target.get("note") or "")
        self.waiters = float(target.get("waiters") or 0.0)
        # Last HTTP elapsed is frozen while the port is down (restart lock age
        # is often ~1s). Count wall time from when this down/load phase began.
        self.elapsed_s = (
            0.0 if down else max(0.0, float(target.get("elapsed_s") or 0.0))
        )
        dest_typical = target.get("typical_s")
        self.typical_s = max(0.0, float(dest_typical)) if dest_typical is not None else 0.0
        wall = time.time()
        self.clock, self.date = wall_clock_parts(wall)
        if self.cycle == "idle" and not held:
            if self._idle_t0 <= 0.0:
                self._idle_t0 = now
                self._idle_hue_seed = int(now * 1_000_003) & 0x7FFFFFFF
            self.idle_s = max(0.0, now - self._idle_t0)
            self.idle_hue = idle_field_hue(self.idle_s, self._idle_hue_seed)
        else:
            self._idle_t0 = 0.0
            self.idle_s = 0.0
        self.idle_fact = pick_idle_fact(
            idle_fact_lines(load_idle_times(), self.idle_s, self.profile, self.mode),
            wall,
        )
        hud_alpha = (
            idle_hud_alpha(self.idle_s, hold_s=self.hud_hold_s)
            if self.cycle == "idle" and not held
            else 1.0
        )
        if not self.connected or dest == "down":
            self.phase = str(target.get("phase") or "restarting api")
        elif self.cycle == "boot":
            self.phase = "stirring"
        elif self.cycle == "halt":
            self.phase = "settling"
        elif held:
            self.phase = str(target.get("phase") or self.phase)
        elif self.weights.get("idle", 0.0) > 0.65:
            self.phase = "idle"
        chat_run = self.phase in _CHAT_RUN_PHASES
        if chat_run:
            self._last_chat = now
            if self._run_t0 <= 0.0:
                self._run_t0 = now
            if self._step_t0 <= 0.0:
                self._step_t0 = now
        elif self._last_chat > 0.0 and (now - self._last_chat) > _RUN_GAP_S:
            self._run_t0 = 0.0
            self._step_t0 = 0.0
            self._run_bank = 0.0
            self.run_tokens = 0.0
            self.tokens = 0.0
            self.token_rate = 0.0
            self._last_chat = 0.0
        if self.phase != self.task_name:
            same_run = self.task_name in _CHAT_RUN_PHASES and chat_run
            same_down = {self.task_name, self.phase} <= {
                "restarting api",
                "waiting for api",
            }
            self.task_name = self.phase
            if not same_run and not same_down:
                self._task_t0 = now
            if same_run:
                self._step_t0 = now
        self.runtime_s = max(0.0, now - self._run_t0) if chat_run and self._run_t0 else (
            max(0.0, now - self._task_t0) if self._task_t0 else 0.0
        )
        step_s = max(0.0, now - self._step_t0) if chat_run and self._step_t0 else 0.0
        show_clock = dest == "down" or self.phase not in {"idle", "stirring", "settling"}
        if show_clock:
            clock_s = self.runtime_s if down else (
                self.elapsed_s if self.elapsed_s > 0.5 else self.runtime_s
            )
            if chat_run and self._run_t0:
                clock_s = max(clock_s, now - self._run_t0)
            runtime = (
                fmt_run_clock(clock_s, step_s)
                if chat_run
                else _fmt_runtime(clock_s)
            )
        else:
            runtime = ""
            step_s = 0.0
        return {
            "phase": self.phase,
            "palette": self.palette,
            "weights": dict(self.weights),
            "live": self.live,
            "intensity": self.intensity,
            "speed": self.speed,
            "heat": self.heat,
            "st": self.st,
            "hue": self.hue,
            "mode": self.mode,
            "profile": self.profile,
            "util": self.util,
            "vram": self.vram,
            "temp": self.temp,
            "cpu": self.cpu,
            "connected": self.connected,
            "overlay": self.overlay,
            "cycle": self.cycle,
            "cycle_t": self.cycle_t,
            "tokens": self.tokens,
            "token_rate": self.token_rate,
            "run_tokens": self.run_tokens,
            "step_s": step_s,
            "stage": self.stage,
            "has_gpu": self.has_gpu,
            "has_cpu": self.has_cpu,
            "image_n": self.image_n,
            "image_of": self.image_of,
            "image_file": self.image_file,
            "image_what": self.image_what,
            "note": self.note,
            "runtime": runtime,
            "runtime_s": self.runtime_s,
            "waiters": self.waiters,
            "elapsed_s": self.elapsed_s,
            "typical_s": self.typical_s,
            "idle_s": self.idle_s,
            "idle_hue": self.idle_hue,
            "clock": self.clock,
            "date": self.date,
            "idle_fact": self.idle_fact,
            "hud_alpha": hud_alpha,
            "hud_hold_s": self.hud_hold_s,
        }


def scene_from_state(
    data: dict[str, Any] | None,
    connected: bool,
    *,
    unit_state: str | None = None,
) -> dict[str, Any]:
    data = data or {}
    gpu = data.get("gpu") if isinstance(data.get("gpu"), dict) else {}
    host = data.get("host") if isinstance(data.get("host"), dict) else {}
    util_raw = gpu.get("utilization_pct")
    vram_raw = gpu.get("vram_pct")
    temp_raw = gpu.get("temperature_c")
    cpu_raw = host.get("cpu_pct")
    has_gpu = any(value is not None and value != "" for value in (util_raw, vram_raw, temp_raw))
    has_cpu = cpu_raw is not None and cpu_raw != ""
    util = _num(util_raw) if util_raw is not None else 0.0
    vram = _num(vram_raw) if vram_raw is not None else 0.0
    temp = _num(temp_raw, 40.0) if temp_raw is not None else 40.0
    cpu = _num(cpu_raw) if has_cpu else 0.0
    kind = str(data.get("kind") or "")
    mode = str(data.get("gpu_mode") or "").strip() or "—"
    profile = profile_from_state(data) or str(data.get("profile") or "").strip() or "—"
    restarting = bool(data.get("restarting"))
    recovering = bool(data.get("recovering")) or str(data.get("stage") or "").strip().lower() == "recover"
    switching = bool(data.get("switching") or restarting)
    busy = bool(data.get("busy"))
    stage = str(data.get("stage") or "").strip().lower()
    working = busy or switching or restarting or recovering or stage in {"prefill", "decode", "tool", "recover"}
    tokens = max(0.0, _num(data.get("tokens")))
    image_n = _num(data.get("image_n")) if data.get("image_n") is not None else 0.0
    image_of = _num(data.get("image_of")) if data.get("image_of") is not None else 0.0
    # GPU % only tints the field. nvidia-smi also moves when this kiosk
    # scanouts on the same card, so it must not rename the HUD to generating.
    live = working or stage in {"prefill", "decode", "tool", "recover"}

    image_job = kind == "image" or mode == "comfy" or stage == "image"
    down = (not connected) or restarting
    if down:
        if restarting and connected:
            phase, palette = "restarting api", "down"
        elif data:
            phase, palette = "restarting api", "down"
        else:
            phase, palette = "waiting for api", "down"
        live = True
        intensity = 0.30
        speed = 0.20
        heat = 0.42
    elif recovering:
        phase, palette = "resetting generator", "switch"
        live = True
        intensity = 0.34
        speed = 0.28
        heat = 0.40
    elif stage == "switch" or switching or (working and kind == "gpu"):
        want = str(data.get("switch_target") or "").strip().lower()
        if want == "comfy":
            phase = "loading comfy"
        elif want == "llm":
            phase = "loading llm"
        elif image_job and mode == "comfy":
            phase = "loading comfy"
        else:
            phase = "loading llm"
        palette = "switch"
    elif image_job:
        phase, palette = ("rendering" if working else "comfy"), "image"
    elif working and stage == "tool":
        phase, palette = "using tools", "chat"
    elif working and (kind == "code" or kind == "chat" or stage in {"prefill", "decode"}):
        # Code occupancy is the UI workspace, not the LLM phase. Prefill/decode
        # there is still Thinking, same as Chat.
        phase, palette = "thinking", "chat"
    elif working:
        phase, palette = "in use", "chat"
    else:
        phase, palette = "idle", "idle"

    if down:
        pass
    elif not live:
        # Idle still has to drift: a nearly-static navy field reads as frozen.
        intensity = min(0.48 + 0.16 * (vram / 100.0), 0.64)
        speed = 0.36 + 0.10 * (vram / 100.0)
        heat = max(0.08, min(0.28, 0.08 + (temp - 38.0) / 110.0))
    elif stage == "prefill":
        intensity = 0.48 + 0.10 * (util / 100.0)
        speed = 0.42 + 0.16 * (util / 100.0)
        heat = max(0.18, min(0.55, 0.22 + (temp - 38.0) / 70.0))
    elif stage == "tool":
        intensity = 0.42
        speed = 0.38
        heat = max(0.14, min(0.46, 0.18 + (temp - 38.0) / 75.0))
    elif stage == "image":
        frac = (image_n / image_of) if image_of > 0 else 0.45
        intensity = 0.50 + 0.12 * frac
        speed = 0.48 + 0.22 * frac
        heat = max(0.22, min(0.62, 0.28 + (temp - 38.0) / 55.0))
    else:
        # Job running: ignore nvidia-smi (often near 0 during decode).
        intensity = 0.52 + 0.14 * (util / 100.0)
        speed = 0.55 + 0.35 * (util / 100.0)
        heat = max(0.22, min(0.62, 0.28 + (temp - 38.0) / 55.0))
    typical = data.get("typical_s")
    if typical is None and (
        down or phase in {"loading llm", "loading comfy", "restarting api", "waiting for api"}
    ):
        want = str(data.get("switch_target") or "").strip().lower()
        load_name = "comfy" if (want == "comfy" or phase == "loading comfy") else (
            profile if str(profile).lower() not in _SKIP_PROFILES else "qwen"
        )
        typical = wait_s_for(load_name)
    unit = unit_state if unit_state is not None else (
        tabbyapi_unit_state() if down else "unknown"
    )
    note = scene_help_note(
        phase=phase,
        connected=connected,
        data=data,
        unit_state=unit,
    )
    return {
        "phase": phase,
        "palette": palette,
        "live": live,
        "intensity": max(0.14, min(0.72, intensity)),
        "speed": speed,
        "heat": heat,
        "mode": mode,
        "profile": profile,
        "util": util,
        "vram": vram,
        "temp": temp,
        "cpu": cpu,
        "connected": connected,
        "has_gpu": has_gpu,
        "has_cpu": has_cpu,
        "tokens": tokens,
        "run_tokens": max(
            _num(data.get("run_tokens")) if data.get("run_tokens") is not None else tokens,
            tokens,
        ),
        "stage": stage or ("idle" if not live else "decode"),
        "image_n": image_n,
        "image_of": image_of,
        "image_file": str(data.get("image_file") or "").strip(),
        "image_what": str(data.get("image_what") or "").strip(),
        "note": note,
        "waiters": _num(data.get("waiters")),
        "elapsed_s": 0.0 if down else _num(data.get("elapsed_s")),
        "typical_s": _num(typical) if typical is not None else None,
    }


class StateBus:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, Any] | None = None
        self.ok = False
        self.stop = threading.Event()

    def snapshot(self) -> tuple[dict[str, Any] | None, bool]:
        with self.lock:
            return self.data, self.ok

    def ingest(self, payload: dict[str, Any] | None, reachable: bool) -> None:
        """HTTP JSON vs TCP. A hung generate times out but keeps the port open."""
        with self.lock:
            if payload is not None:
                self.data = payload
                self.ok = True
                return
            if not reachable:
                self.ok = False
                return
            if self.data is None:
                self.ok = False

    def poll_once(
        self,
        url: str,
        interval: float,
        *,
        last_http: float,
        now: float | None = None,
    ) -> float:
        """One poll. A closed port stays down; leftover busy JSON is not 'up'."""
        host, port = origin_peer(url)
        live = read_saver_live()
        with self.lock:
            had_http = self.data is not None
            cached = dict(self.data) if self.data else None
        now = time.monotonic() if now is None else now
        due = (now - last_http) >= http_poll_gap(
            busy=state_is_busy(cached, live), idle_s=interval
        )
        if not due:
            with self.lock:
                still_up = self.ok
            # Sidecar can mark thinking the instant a generate POST lands, but
            # only while this process still sees the API as up. A restart must
            # not treat leftover JSON as a live prompt.
            if still_up and live and live.get("busy") and had_http:
                self.ingest(overlay_saver_live(cached, live), True)
            return last_http
        reachable = tcp_up(host, port)
        payload = fetch_state(url, timeout=0.8) if reachable else None
        if payload is None:
            reachable = tcp_up(host, port)
        live = read_saver_live()
        if not reachable:
            self.ingest(None, False)
            return now
        if payload is None and live and live.get("busy") and had_http:
            self.ingest(overlay_saver_live(cached, live), True)
            return now
        if payload is None:
            self.ingest(None, True)
            return now
        self.ingest(overlay_saver_live(payload, live, trust_idle_http=True), True)
        return now

    def run(self, url: str, interval: float) -> None:
        last_http = 0.0
        tick = 0.1
        while not self.stop.is_set():
            last_http = self.poll_once(url, interval, last_http=last_http)
            self.stop.wait(tick)


def _warm_palette(
    name: str, heat: float, hue: float = 0.0, idle_hue: float = 0.0
) -> list[tuple[int, int, int]]:
    base = PALETTES.get(name) or PALETTES["idle"]
    if name == "chat":
        base = _shift_ramp(base, hue)
    elif name == "idle":
        base = _shift_ramp(base, idle_hue)
    if name == "down" or heat <= 0.02:
        return base
    return [_mix(color, WARN, heat * 0.14 * (i / 255.0)) for i, color in enumerate(base)]


def _u01(i: int, salt: int = 0) -> float:
    x = (i * 374761393 + salt * 668265263) & 0xFFFFFFFF
    x = (x ^ (x >> 13)) * 1274126177 & 0xFFFFFFFF
    return (x & 0xFFFFFF) / float(0xFFFFFF)


def _ramp_color(ramp: list[tuple[int, int, int]], t: float) -> tuple[int, int, int]:
    if not ramp:
        return ACCENT
    t = _clamp01(t)
    if len(ramp) == 1:
        return ramp[0]
    span = (len(ramp) - 1) * t
    idx = int(span)
    if idx >= len(ramp) - 1:
        return ramp[-1]
    return _mix(ramp[idx], ramp[idx + 1], span - idx)


def _neuron_rest(n: int = 58) -> list[tuple[float, float]]:
    """First-added scatter: golden-ratio tissue stretched to the bezel."""
    golden = 0.5 * (1.0 + math.sqrt(5.0))
    raw_x = [(i * golden) % 1.0 for i in range(n)]
    raw_y = [(i + 0.5) / n for i in range(n)]
    minx, maxx = min(raw_x), max(raw_x)
    miny, maxy = min(raw_y), max(raw_y)
    pts: list[tuple[float, float]] = []
    for x, y in zip(raw_x, raw_y):
        nx = 0.0 if maxx == minx else (x - minx) / (maxx - minx)
        ny = 0.0 if maxy == miny else (y - miny) / (maxy - miny)
        pts.append((nx, ny))
    return pts


def _neuron_edges(pts: list[tuple[float, float]]) -> list[tuple[int, int]]:
    n = len(pts)
    seen: set[tuple[int, int]] = set()
    edges: list[tuple[int, int]] = []

    def add(i: int, j: int) -> None:
        if i == j:
            return
        a, b = (i, j) if i < j else (j, i)
        if (a, b) in seen:
            return
        seen.add((a, b))
        edges.append((a, b))

    for i in range(n):
        p = pts[i]
        near = sorted(
            (math.hypot(p[0] - pts[j][0], p[1] - pts[j][1]), j) for j in range(n) if j != i
        )
        for _dist, j in near[:3]:
            add(i, j)
    for i in range(0, n, 7):
        add(i, (i + n // 3) % n)
    return edges


NEURON_REST = _neuron_rest()
NEURON_EDGES = _neuron_edges(NEURON_REST)
NEURON_SPARK = {
    "idle": ((90, 130, 210), (210, 230, 255)),
    "chat": ((90, 160, 255), (255, 210, 240)),
    "image": ((180, 110, 40), (255, 220, 120)),
    "switch": ((40, 160, 110), (180, 255, 210)),
    "down": ((140, 28, 36), (255, 92, 78)),
}
NEURON_RAMPS: dict[str, list[tuple[int, int, int]]] = {
    "idle": [(90, 130, 210), (140, 175, 235), (190, 215, 250), (220, 235, 255)],
    "chat": [(40, 220, 255), (80, 150, 255), (139, 92, 246), (220, 70, 210), (255, 90, 170)],
    "image": [(255, 196, 64), (255, 140, 48), (255, 88, 72), (255, 110, 160), (255, 190, 140)],
    "switch": [(20, 200, 190), (48, 230, 130), (140, 255, 170), (200, 255, 210)],
    "down": [(80, 16, 20), (140, 28, 36), (200, 48, 48), (255, 96, 80)],
}
# GPU °C outline: gold → ember → red-orange → white-hot.
HOT_GLOW = [(255, 210, 96), (255, 158, 48), (255, 86, 32), (255, 236, 210)]
# Real envelope from Status metrics on this 4070 Ti: idle 37–44°C,
# busy 50–68°C (never the 80–90°C the first scale assumed).
_TEMP_GLOW_LO = 44.0
_TEMP_GLOW_HI = 68.0


def _temp_hotness(temp: float) -> float:
    """Linear 0 at idle-cluster top, 1 at the hottest samples this card hits."""
    span = _TEMP_GLOW_HI - _TEMP_GLOW_LO
    if span <= 0.0:
        return 0.0
    return _clamp01((float(temp) - _TEMP_GLOW_LO) / span)


def overlay_amount(scene: dict[str, Any]) -> float:
    raw = scene.get("overlay")
    if raw is None:
        return 1.0 if scene.get("live") else 0.0
    try:
        return _clamp01(float(raw))
    except (TypeError, ValueError):
        return 0.0


def neuron_overlay_state(scene: dict[str, Any]) -> dict[str, Any] | None:
    """Unit-space graph + fires. None when the overlay has fully faded."""
    overlay = overlay_amount(scene)
    cycle = str(scene.get("cycle") or "")
    if overlay <= 0.02:
        return None
    st = float(scene.get("st", 0.0))
    intensity = float(scene.get("intensity") or 0.0)
    stage = str(scene.get("stage") or "")
    token_rate = float(scene.get("token_rate") or 0.0)
    tokens = int(float(scene.get("tokens") or 0.0))
    image_n = float(scene.get("image_n") or 0.0)
    image_of = float(scene.get("image_of") or 0.0)
    if cycle == "boot" and overlay < 0.08:
        overlay = 0.08
    nodes: list[tuple[float, float]] = []
    fires: list[float] = []
    for i, (nx, ny) in enumerate(NEURON_REST):
        nodes.append(
            (
                _clamp01(nx + 0.018 * lsin(st * 0.33 + i * 0.37)),
                _clamp01(ny + 0.016 * lsin(st * 0.27 + i * 0.51)),
            )
        )
        rest = 0.10 + 0.10 * (0.5 + 0.5 * lsin(st * 2.3 + i * 0.91))
        pop = 0.5 + 0.5 * lsin(st * (4.1 + 0.13 * (i % 8)) + i * 1.27)
        thresh = 0.84 - 0.14 * intensity
        if stage == "prefill":
            thresh = 0.70 - 0.10 * intensity
        elif stage == "tool":
            thresh = 0.92
        spike = 0.0
        if pop > thresh:
            spike = min(1.0, (pop - thresh) / max(0.04, 1.0 - thresh))
        fire = max(rest, spike) * overlay
        if stage == "decode" and token_rate > 0.5:
            boost = min(1.0, token_rate / 18.0)
            pick = _u01(i, tokens * 17 + 3)
            if pick < 0.22 + 0.35 * boost:
                fire = max(fire, (0.55 + 0.45 * boost) * overlay)
        fires.append(fire)
    pulses: list[tuple[int, float, float]] = []
    if stage == "prefill":
        rate = 0.32 + 0.45 * intensity
        extra = 1
    elif stage == "tool":
        rate = 0.22 + 0.20 * intensity
        extra = 1
    elif stage == "image":
        frac = (image_n / image_of) if image_of > 0 else 0.4
        rate = 0.40 + 0.70 * frac
        extra = 1 + (1 if frac > 0.5 else 0)
    else:
        boost = min(1.6, 0.15 * token_rate)
        rate = 0.70 + 1.25 * intensity + boost
        extra = 1 + (1 if intensity > 0.72 or token_rate > 8 else 0)
        extra += 1 if intensity > 0.90 or token_rate > 20 else 0
    reverse = cycle == "halt"
    for ei, (a, b) in enumerate(NEURON_EDGES):
        for p in range(extra):
            phase = (st * rate * (0.50 + 0.85 * _u01(ei, p + 3)) + _u01(ei, p + 17)) % 1.0
            if reverse:
                phase = 1.0 - phase
            u = phase * 2.0
            if u > 1.0:
                u = 2.0 - u
            bright = (0.45 + 0.55 * intensity) * overlay
            pulses.append((ei, u, bright))
            width = 0.16
            if u < width:
                fires[a] = max(fires[a], (1.0 - u / width) * overlay)
            if u > 1.0 - width:
                fires[b] = max(fires[b], ((u - (1.0 - width)) / width) * overlay)
    return {
        "nodes": nodes,
        "edges": NEURON_EDGES,
        "fires": fires,
        "pulses": pulses,
        "overlay": overlay,
        "reverse": reverse,
    }


def _draw_glow(
    pygame_mod: Any,
    screen: Any,
    pos: tuple[int, int],
    color: tuple[int, int, int],
    strength: float,
    scale: float = 1.0,
) -> None:
    if strength <= 0.02:
        return
    for rad, amt in ((9.0, 0.12), (5.0, 0.28), (2.0, 0.55)):
        r = max(1, int(round(rad * scale * (0.55 + 0.45 * strength))))
        pygame_mod.draw.circle(screen, _mix(BG, color, amt * strength), pos, r)


def neuron_draw_sizes(
    fire: float, bright: float, height: float = 1080.0, fade: float = 1.0
) -> tuple[int, int]:
    """Soma and pulse radii. About half the first-added thickness."""
    h = max(1.0, float(height))
    fade = _clamp01(fade)
    ring = int(round((h / 380.0) * (1.5 + 3.5 * fire) * fade))
    head = int(round((h / 420.0) * (1.1 + 1.4 * bright) * fade))
    return ring, head


def draw_neurons(pygame_mod: Any, screen: Any, scene: dict[str, Any]) -> None:
    state = neuron_overlay_state(scene)
    if state is None:
        return
    w, h = screen.get_size()
    overlay = float(state["overlay"])
    name = str(scene.get("palette") or "chat")
    axon, spark = NEURON_SPARK.get(name) or NEURON_SPARK["chat"]
    if name == "chat":
        hue = float(scene.get("hue") or 0.0)
        axon = _shift_color(axon, hue)
        spark = _shift_color(spark, hue)
    hotness = _temp_hotness(float(scene.get("temp") or 0.0)) if scene.get("has_gpu") else 0.0
    hot = _ramp_color(HOT_GLOW, hotness)
    dim_axon = _mix(BG, axon, 0.42 * overlay)
    last_x = max(1, w - 1)
    last_y = max(1, h - 1)
    nodes = [(int(round(x * last_x)), int(round(y * last_y))) for x, y in state["nodes"]]
    edges: list[tuple[int, int]] = state["edges"]
    if overlay > 0.04:
        for a, b in edges:
            if nodes[a] != nodes[b]:
                pygame_mod.draw.line(screen, dim_axon, nodes[a], nodes[b], 1)
    for ei, u, bright in state["pulses"]:
        a, b = edges[ei]
        x0, y0 = nodes[a]
        x1, y1 = nodes[b]
        px = int(x0 + (x1 - x0) * u)
        py = int(y0 + (y1 - y0) * u)
        _ring, head = neuron_draw_sizes(0.0, bright, h, overlay)
        if head < 1:
            continue
        color = _mix(BG, _mix(axon, spark, bright), overlay)
        pygame_mod.draw.circle(screen, color, (px, py), head)
        if overlay > 0.45 and head > 1:
            pygame_mod.draw.circle(screen, _mix(BG, (255, 255, 255), overlay), (px, py), max(1, head // 2))
    fires: list[float] = state["fires"]
    for i, (x, y) in enumerate(nodes):
        fire = fires[i]
        ring, _head = neuron_draw_sizes(fire, 0.0, h, overlay)
        if hotness > 0.05 and overlay > 0.08:
            halo = overlay * hotness * (0.30 + 0.70 * max(fire, 0.22))
            soma_r = max(ring, int(round((h / 380.0) * 1.5 * overlay)))
            if halo > 0.02 and soma_r >= 1:
                _draw_glow(
                    pygame_mod,
                    screen,
                    (x, y),
                    hot,
                    halo,
                    scale=1.05 + 1.7 * hotness + 0.45 * fire,
                )
                outline_r = max(soma_r + 2, int(round(soma_r * (1.35 + 0.55 * hotness))))
                width = max(1, int(round(1.0 + 2.4 * hotness)))
                pygame_mod.draw.circle(
                    screen,
                    _mix(BG, hot, overlay * (0.38 + 0.52 * hotness)),
                    (x, y),
                    outline_r,
                    width,
                )
        if ring < 1:
            continue
        body = _mix(axon, spark, fire)
        amt = overlay * (0.40 + 0.60 * fire)
        if amt <= 0.03:
            continue
        pygame_mod.draw.circle(screen, _mix(BG, body, amt * 0.55), (x, y), ring + 1)
        pygame_mod.draw.circle(screen, _mix(BG, body, amt), (x, y), ring)
        if fire > 0.35 and overlay > 0.45 and ring > 1:
            pygame_mod.draw.circle(
                screen, _mix(BG, (255, 255, 255), overlay), (x, y), max(1, ring // 3)
            )


def draw_cycle_fx(pygame_mod: Any, screen: Any, scene: dict[str, Any]) -> None:
    """Center bloom + ring: stirring expands, settling contracts. Field stays on."""
    cycle = str(scene.get("cycle") or "")
    if cycle not in ("boot", "halt"):
        return
    try:
        t = _clamp01(float(scene.get("cycle_t") or 0.0))
    except (TypeError, ValueError):
        t = 0.0
    w, h = screen.get_size()
    cx, cy = w // 2, h // 2
    name = str(scene.get("palette") or "chat")
    ramp = NEURON_RAMPS.get(name) or NEURON_RAMPS["chat"]
    if name == "chat":
        ramp = _shift_ramp(ramp, float(scene.get("hue") or 0.0))
    color = _ramp_color(ramp, 0.45 if cycle == "boot" else 0.2)
    span = min(w, h)
    if cycle == "boot":
        core = max(0.0, 1.0 - t * 1.15)
        radius = max(10, int(span * 0.06 + span * 0.42 * t))
        _draw_glow(pygame_mod, screen, (cx, cy), color, 0.50 + 0.50 * core, scale=2.2 + 3.6 * core)
        pygame_mod.draw.circle(screen, _mix(BG, color, 0.22 + 0.38 * core), (cx, cy), max(4, int(18 * core)))
    else:
        core = max(0.0, 1.0 - t)
        radius = max(8, int(span * 0.48 * core))
        _draw_glow(pygame_mod, screen, (cx, cy), color, 0.28 + 0.40 * core, scale=1.6 + 2.2 * core)
    if radius > 6:
        ring = _mix(BG, color, 0.40 if cycle == "boot" else 0.28)
        pygame_mod.draw.circle(screen, ring, (cx, cy), radius, 2)
        inner = max(1, radius - 6)
        if inner < radius:
            pygame_mod.draw.circle(screen, _mix(BG, color, 0.18), (cx, cy), inner, 1)


# Idle-only: faint ray-marched solids on the full framebuffer.
# Three homes; at most two visible. Fade in/out with no rest gap.
_SLEEP_KINDS = ("sphere", "box", "torus", "octa", "capsule")
_SLEEP_HOMES = (
    (0.18, 0.28),
    (0.82, 0.32),
    (0.50, 0.78),
)
_SLEEP_SLOTS = (
    (36.0, 0.00),
    (36.0, 0.38),
    (36.0, 0.72),
)
_SLEEP_MIN_SEP = 0.34
_SLEEP_LIFE = 0.56
_SLEEP_FADE = 0.18
_SLEEP_SPAN_FRAC = 0.48
_SLEEP_SPAN_MAX = 520
# March at most this many px on a side, then smoothscale up. The solids are
# soft and dim, so 192 reads the same as 256 at 40% less work.
_SLEEP_RT_MAX = 192
_SLEEP_TINT = (56, 84, 132)


def _sleep_unit(slot: int, cycle: int, salt: int) -> float:
    return _u01(slot + 3, int(cycle) * 10007 + salt)


def idle_sleeper_envelope(
    u: float, life: float = _SLEEP_LIFE, fade: float = _SLEEP_FADE
) -> float:
    """Fade in, hold, fade out. Zero outside the life window (no pop, no rest)."""
    if u < 0.0 or u > life:
        return 0.0
    fade = max(1e-6, min(float(life) * 0.49, float(fade)))
    return _smoothstep(u / fade) * _smoothstep((life - u) / fade)


def _sleep_xy(slot: int, st: float) -> tuple[float, float]:
    """Home in a screen third, plus a slow orbit. math.sin so it does not stair-step."""
    hx, hy = _SLEEP_HOMES[slot % len(_SLEEP_HOMES)]
    x = hx + 0.06 * math.sin(st * 0.037 + slot * 2.15)
    y = hy + 0.07 * math.sin(st * 0.029 + slot * 1.37)
    return x, y


def _sleep_tint_for(slot: int, cycle: int, idle_hue: float = 0.0) -> tuple[int, int, int]:
    shift = idle_hue + (_sleep_unit(slot, cycle, 61) - 0.5) * 0.10
    return _shift_color(_SLEEP_TINT, shift)


def idle_sleeper_items(scene: dict[str, Any], width: int, height: int) -> list[dict[str, Any]]:
    """Soft solids while the field is fully idle. Empty otherwise."""
    if overlay_amount(scene) > 0.04:
        return []
    if str(scene.get("cycle") or "idle") != "idle":
        return []
    if scene.get("live"):
        return []
    if str(scene.get("palette") or "") == "down" or not scene.get("connected", True):
        return []
    st = float(scene.get("st") or 0.0)
    idle_hue = float(scene.get("idle_hue") or 0.0)
    w = max(1, int(width))
    h = max(1, int(height))
    n_kinds = len(_SLEEP_KINDS)
    short = min(w, h)
    candidates: list[dict[str, Any]] = []
    for slot, (period, phase) in enumerate(_SLEEP_SLOTS):
        period = max(1.0, float(period))
        clock = st / period + phase
        u = clock % 1.0
        amt = idle_sleeper_envelope(u)
        if amt <= 0.02:
            continue
        cycle = int(math.floor(clock - u + 1e-9))
        kind = _SLEEP_KINDS[(slot + cycle) % n_kinds]
        x, y = _sleep_xy(slot, st)
        scale = 0.92 + 0.12 * _sleep_unit(slot, cycle, 29)
        yaw0 = TWO_PI * _sleep_unit(slot, cycle, 31)
        pitch0 = (_sleep_unit(slot, cycle, 37) - 0.5) * 0.40
        span = max(160, min(_SLEEP_SPAN_MAX, int(round(short * _SLEEP_SPAN_FRAC * scale))))
        candidates.append(
            {
                "kind": kind,
                "fx": x,
                "fy": y,
                "x": x * (w - 1),
                "y": y * (h - 1),
                "size": span,
                "amt": amt,
                "yaw": yaw0 + st * 0.055,
                "pitch": pitch0 + 0.10 * math.sin(st * 0.048 + slot),
                "tint": _sleep_tint_for(slot, cycle, idle_hue),
                "seed": slot * 10007 + cycle,
            }
        )
    candidates.sort(key=lambda item: float(item["amt"]), reverse=True)
    return candidates[:2]


def _sleep_add_color(lift: float, tint: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    t = _clamp01(lift)
    ink = tint or _SLEEP_TINT
    return (
        max(0, min(56, int(ink[0] * t))),
        max(0, min(78, int(ink[1] * t))),
        max(0, min(124, int(ink[2] * t))),
    )


def _blit_sleep_add(
    pygame_mod: Any, screen: Any, surf: Any, cx: float, cy: float
) -> None:
    flags = getattr(pygame_mod, "BLEND_RGB_ADD", 0)
    screen.blit(
        surf,
        (int(round(cx - surf.get_width() * 0.5)), int(round(cy - surf.get_height() * 0.5))),
        special_flags=flags,
    )


def _sleep_sdf(kind: str, px: float, py: float, pz: float) -> float:
    if kind == "sphere":
        return math.sqrt(px * px + py * py + pz * pz) - 0.80
    if kind == "box":
        ax, ay, az = abs(px) - 0.50, abs(py) - 0.50, abs(pz) - 0.50
        qx, qy, qz = max(ax, 0.0), max(ay, 0.0), max(az, 0.0)
        return math.sqrt(qx * qx + qy * qy + qz * qz) + min(max(ax, max(ay, az)), 0.0) - 0.10
    if kind == "torus":
        q = math.hypot(px, pz) - 0.58
        return math.hypot(q, py) - 0.20
    if kind == "octa":
        return (abs(px) + abs(py) + abs(pz) - 0.92) * 0.57735027
    t = 0.0 if py < -0.44 else 1.0 if py > 0.46 else (py + 0.44) / 0.90
    dx, dy, dz = px, py - (-0.44 + 0.90 * t), pz
    return math.sqrt(dx * dx + dy * dy + dz * dz) - 0.30


def _sleep_rotate(
    px: float, py: float, pz: float, yaw: float, pitch: float
) -> tuple[float, float, float]:
    c, s = math.cos(pitch), math.sin(pitch)
    py, pz = py * c - pz * s, py * s + pz * c
    c, s = math.cos(yaw), math.sin(yaw)
    px, pz = px * c - pz * s, px * s + pz * c
    return px, py, pz


def _sleep_rt_rgb(
    kind: str,
    width: int,
    height: int,
    yaw: float,
    pitch: float,
    amt: float,
    tint: tuple[int, int, int],
) -> bytes:
    """Software ray march of one solid. Black misses; dim additive hits."""
    w = max(8, int(width))
    h = max(8, int(height))
    amt = _clamp01(amt) * 0.52
    tr, tg, tb = tint
    out = bytearray(w * h * 3)
    if amt <= 0.01:
        return bytes(out)
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        return _sleep_rt_rgb_numpy(kind, w, h, yaw, pitch, amt, (tr, tg, tb), np)
    steps = 12
    light = (0.48, 0.78, 0.44)
    ln = math.sqrt(light[0] ** 2 + light[1] ** 2 + light[2] ** 2)
    lx, ly, lz = light[0] / ln, light[1] / ln, light[2] / ln
    eps = 0.014
    i = 0
    for iy in range(h):
        v = 1.0 - (iy + 0.5) / h * 2.0
        for ix in range(w):
            u = (ix + 0.5) / w * 2.0 - 1.0
            rdx, rdy, rdz = u * 0.82, v * 0.82, -1.32
            inv = 1.0 / math.sqrt(rdx * rdx + rdy * rdy + rdz * rdz)
            rdx, rdy, rdz = rdx * inv, rdy * inv, rdz * inv
            t = 0.0
            hit = False
            px = py = pz = 0.0
            closest = 8.0
            for _step in range(steps):
                px, py, pz = rdx * t, rdy * t, 2.05 + rdz * t
                qx, qy, qz = _sleep_rotate(px, py, pz, yaw, pitch)
                d = _sleep_sdf(kind, qx, qy, qz)
                closest = min(closest, d if d > 0.0 else 0.0)
                if d < 0.012:
                    hit = True
                    break
                t += max(d, 0.01)
                if t > 6.0:
                    break
            if not hit:
                glow = math.exp(-closest * closest * 70.0) * amt * 0.20
                if glow > 0.004:
                    out[i] = min(255, int(tr * glow))
                    out[i + 1] = min(255, int(tg * glow))
                    out[i + 2] = min(255, int(tb * glow))
                i += 3
                continue
            def n_at(dx: float, dy: float, dz: float) -> float:
                sx, sy, sz = _sleep_rotate(px + dx, py + dy, pz + dz, yaw, pitch)
                return _sleep_sdf(kind, sx, sy, sz)
            nx = n_at(eps, 0.0, 0.0) - n_at(-eps, 0.0, 0.0)
            ny = n_at(0.0, eps, 0.0) - n_at(0.0, -eps, 0.0)
            nz = n_at(0.0, 0.0, eps) - n_at(0.0, 0.0, -eps)
            nn = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / nn, ny / nn, nz / nn
            diff = max(0.0, nx * lx + ny * ly + nz * lz)
            ndv = max(0.0, -(nx * rdx + ny * rdy + nz * rdz))
            rim = (1.0 - ndv) * (1.0 - ndv)
            hx, hy, hz = lx - rdx, ly - rdy, lz - rdz
            hn = math.sqrt(hx * hx + hy * hy + hz * hz) or 1.0
            spec = max(0.0, nx * hx / hn + ny * hy / hn + nz * hz / hn) ** 14
            fog = math.exp(-t * 0.18)
            lift = (0.10 + 0.40 * diff + 0.16 * rim) * fog * amt
            out[i] = min(255, int(tr * lift + 210 * spec * amt * fog))
            out[i + 1] = min(255, int(tg * lift + 220 * spec * amt * fog))
            out[i + 2] = min(255, int(tb * lift + 255 * spec * amt * fog))
            i += 3
    return bytes(out)


def _sleep_rt_rgb_numpy(
    kind: str,
    w: int,
    h: int,
    yaw: float,
    pitch: float,
    amt: float,
    tint: tuple[int, int, int],
    np: Any,
) -> bytes:
    xs = ((np.arange(w, dtype=np.float32) + 0.5) / w) * 2.0 - 1.0
    ys = 1.0 - ((np.arange(h, dtype=np.float32) + 0.5) / h) * 2.0
    u, v = np.meshgrid(xs, ys)
    rdx = u * np.float32(0.82)
    rdy = v * np.float32(0.82)
    rdz = np.full((h, w), np.float32(-1.32))
    inv = 1.0 / np.sqrt(rdx * rdx + rdy * rdy + rdz * rdz)
    rdx, rdy, rdz = rdx * inv, rdy * inv, rdz * inv
    cy, sy = np.float32(math.cos(pitch)), np.float32(math.sin(pitch))
    cz, sz = np.float32(math.cos(yaw)), np.float32(math.sin(yaw))

    def rotate(px, py, pz):
        py2 = py * cy - pz * sy
        pz2 = py * sy + pz * cy
        px2 = px * cz - pz2 * sz
        pz3 = px * sz + pz2 * cz
        return px2, py2, pz3

    def sdf(px, py, pz):
        if kind == "sphere":
            return np.sqrt(px * px + py * py + pz * pz) - np.float32(0.80)
        if kind == "box":
            ax = np.abs(px) - np.float32(0.50)
            ay = np.abs(py) - np.float32(0.50)
            az = np.abs(pz) - np.float32(0.50)
            qx = np.maximum(ax, 0.0)
            qy = np.maximum(ay, 0.0)
            qz = np.maximum(az, 0.0)
            return np.sqrt(qx * qx + qy * qy + qz * qz) + np.minimum(
                np.maximum(ax, np.maximum(ay, az)), 0.0
            ) - np.float32(0.10)
        if kind == "torus":
            q = np.sqrt(px * px + pz * pz) - np.float32(0.58)
            return np.sqrt(q * q + py * py) - np.float32(0.20)
        if kind == "octa":
            return (np.abs(px) + np.abs(py) + np.abs(pz) - np.float32(0.92)) * np.float32(
                0.57735027
            )
        tcap = np.clip((py + np.float32(0.44)) / np.float32(0.90), 0.0, 1.0)
        dy = py - (np.float32(-0.44) + np.float32(0.90) * tcap)
        return np.sqrt(px * px + dy * dy + pz * pz) - np.float32(0.30)

    t = np.zeros((h, w), dtype=np.float32)
    hit = np.zeros((h, w), dtype=bool)
    dmin = np.full((h, w), np.float32(8.0))
    for _step in range(14):
        px = rdx * t
        py = rdy * t
        pz = np.float32(2.05) + rdz * t
        qx, qy, qz = rotate(px, py, pz)
        d = sdf(qx, qy, qz)
        dmin = np.minimum(dmin, np.maximum(d, 0.0))
        arrived = d < np.float32(0.012)
        hit = hit | arrived
        t = np.where(hit | (t > 6.0), t, t + np.maximum(d, np.float32(0.01)))
    live = hit & (t <= 6.0)
    px = rdx * t
    py = rdy * t
    pz = np.float32(2.05) + rdz * t
    eps = np.float32(0.014)

    def sdf_at(dx, dy, dz):
        qx, qy, qz = rotate(px + dx, py + dy, pz + dz)
        return sdf(qx, qy, qz)

    nx = sdf_at(eps, 0.0, 0.0) - sdf_at(-eps, 0.0, 0.0)
    ny = sdf_at(0.0, eps, 0.0) - sdf_at(0.0, -eps, 0.0)
    nz = sdf_at(0.0, 0.0, eps) - sdf_at(0.0, 0.0, -eps)
    nn = np.maximum(np.sqrt(nx * nx + ny * ny + nz * nz), 1e-5)
    nx, ny, nz = nx / nn, ny / nn, nz / nn
    lx, ly, lz = np.float32(0.48), np.float32(0.78), np.float32(0.44)
    ln = math.sqrt(0.48 ** 2 + 0.78 ** 2 + 0.44 ** 2)
    lx, ly, lz = lx / ln, ly / ln, lz / ln
    diff = np.maximum(nx * lx + ny * ly + nz * lz, 0.0)
    ndv = np.maximum(-(nx * rdx + ny * rdy + nz * rdz), 0.0)
    rim = (1.0 - ndv) ** 2
    hx = lx - rdx
    hy = ly - rdy
    hz = lz - rdz
    hn = np.maximum(np.sqrt(hx * hx + hy * hy + hz * hz), 1e-5)
    spec = np.maximum(nx * hx / hn + ny * hy / hn + nz * hz / hn, 0.0) ** 14
    fog = np.exp(-t * np.float32(0.18))
    lift = (0.10 + 0.40 * diff + 0.16 * rim) * fog * np.float32(amt)
    tr, tg, tb = tint
    rgb = np.zeros((h, w, 3), dtype=np.float32)
    rgb[..., 0] = tr * lift + 210.0 * spec * amt * fog
    rgb[..., 1] = tg * lift + 220.0 * spec * amt * fog
    rgb[..., 2] = tb * lift + 255.0 * spec * amt * fog
    glow = np.exp(-dmin * dmin * np.float32(70.0)) * np.float32(amt) * np.float32(0.20)
    rgb[..., 0] = np.where(live, rgb[..., 0], tr * glow)
    rgb[..., 1] = np.where(live, rgb[..., 1], tg * glow)
    rgb[..., 2] = np.where(live, rgb[..., 2], tb * glow)
    rgb = np.clip(rgb, 0.0, 255.0)
    return np.ascontiguousarray(rgb.astype(np.uint8)).tobytes()


def _draw_sleeping_solid(pygame_mod: Any, screen: Any, item: dict[str, Any]) -> None:
    amt = float(item.get("amt") or 0.0)
    if amt <= 0.02:
        return
    key = sleep_cache_key(item)
    q_amt = max(1, int(round(_clamp01(amt) * _SLEEP_FADE_STEPS)))
    surf = _SLEEP_SURF_CACHE.get((key, q_amt))
    if surf is None:
        kind, _qyaw, _qpitch, _tint, span = key
        rt = max(48, min(_SLEEP_RT_MAX, span))
        rgb = _sleep_fade_rgb(_sleep_solid_rgb(key, rt), q_amt / _SLEEP_FADE_STEPS)
        surf = pygame_mod.image.frombuffer(rgb, (rt, rt), "RGB").convert()
        if span != rt:
            surf = pygame_mod.transform.smoothscale(surf, (span, span))
        if len(_SLEEP_SURF_CACHE) >= _SLEEP_SURF_CACHE_MAX:
            _SLEEP_SURF_CACHE.clear()
        _SLEEP_SURF_CACHE[(key, q_amt)] = surf
    _blit_sleep_add(pygame_mod, screen, surf, float(item["x"]), float(item["y"]))


# Yaw moves ~0.055 rad/s, so a fresh march every frame is wasted work. Render
# at quantised angles and reuse until the solid has turned _SLEEP_ANGLE_Q
# (about 1 degree, roughly every 0.3 s). The march is cached as RGB (survives
# a display restart); the scaled Surface per fade step is cached too, and
# those die with pygame.quit(), so _close_display drops them.
_SLEEP_ANGLE_Q = 0.016
_SLEEP_FADE_STEPS = 32
_SLEEP_CACHE: dict[tuple[Any, ...], Any] = {}
_SLEEP_CACHE_MAX = 8
_SLEEP_SURF_CACHE: dict[tuple[Any, ...], Any] = {}
_SLEEP_SURF_CACHE_MAX = 24


def sleep_cache_key(item: dict[str, Any]) -> tuple[Any, ...]:
    span = max(80, min(_SLEEP_SPAN_MAX, int(item.get("size") or 160)))
    return (
        str(item.get("kind") or "sphere"),
        int(round(float(item.get("yaw") or 0.0) / _SLEEP_ANGLE_Q)),
        int(round(float(item.get("pitch") or 0.0) / _SLEEP_ANGLE_Q)),
        tuple(item.get("tint") or _SLEEP_TINT),
        span,
    )


def _sleep_solid_rgb(key: tuple[Any, ...], rt: int) -> Any:
    """Full-strength march for one quantised pose (bytes, or ndarray with numpy)."""
    hit = _SLEEP_CACHE.get(key)
    if hit is not None:
        return hit
    kind, qyaw, qpitch, tint, _span = key
    raw = _sleep_rt_rgb(kind, rt, rt, qyaw * _SLEEP_ANGLE_Q, qpitch * _SLEEP_ANGLE_Q, 1.0, tint)
    try:
        import numpy as np
    except ImportError:
        base: Any = raw
    else:
        base = np.frombuffer(raw, dtype=np.uint8).reshape(rt, rt, 3)
    if len(_SLEEP_CACHE) >= _SLEEP_CACHE_MAX:
        _SLEEP_CACHE.clear()
    _SLEEP_CACHE[key] = base
    return base


def _sleep_fade_rgb(base: Any, amt: float) -> Any:
    """Lighting is linear in amt, so dim the cached render instead of re-marching."""
    amt = _clamp01(amt)
    if amt >= 0.995:
        return base
    level = int(round(256.0 * amt))
    if hasattr(base, "astype"):
        import numpy as np

        return ((base.astype(np.uint16) * np.uint16(level)) >> 8).astype(np.uint8)
    return bytes((b * level) >> 8 for b in base)


def draw_sleepers(pygame_mod: Any, screen: Any, scene: dict[str, Any]) -> None:
    w, h = screen.get_size()
    for item in idle_sleeper_items(scene, w, h):
        _draw_sleeping_solid(pygame_mod, screen, item)




def _blended_palette(
    weights: dict[str, float], heat: float, hue: float = 0.0, idle_hue: float = 0.0
) -> list[tuple[int, int, int]]:
    names = [name for name, w in weights.items() if w > 0.01 and name in PALETTES]
    if not names:
        names = ["idle"]
    total = sum(weights[name] for name in names) or 1.0
    ramps = {}
    for name in names:
        ramp = PALETTES[name]
        if name == "chat":
            ramp = _shift_ramp(ramp, hue)
        elif name == "idle":
            ramp = _shift_ramp(ramp, idle_hue)
        ramps[name] = ramp
    out: list[tuple[int, int, int]] = []
    for i in range(256):
        r = g = b = 0.0
        for name in names:
            w = weights[name] / total
            cr, cg, cb = ramps[name][i]
            r += cr * w
            g += cg * w
            b += cb * w
        color = (int(r), int(g), int(b))
        down_w = weights.get("down", 0.0) / total
        if down_w > 0.45:
            pass
        elif heat > 0.02:
            color = _mix(color, WARN, heat * 0.14 * (i / 255.0))
        out.append(color)
    return out


def _field_common(width: int, height: int, scene: dict[str, Any]):
    hue = float(scene.get("hue") or 0.0)
    idle_hue = float(scene.get("idle_hue") or 0.0)
    weights = scene.get("weights")
    if isinstance(weights, dict) and weights:
        mixed = {str(k): float(v) for k, v in weights.items()}
        palette = _blended_palette(mixed, float(scene["heat"]), hue, idle_hue)
    else:
        palette = _warm_palette(str(scene["palette"]), float(scene["heat"]), hue, idle_hue)
    intensity = float(scene["intensity"])
    mix = overlay_amount(scene)
    st = float(scene.get("st", 0.0))
    breath_idle = 0.5 + 0.5 * lsin(st * 1.55)
    breath_live = 0.5 + 0.5 * lsin(st * 1.15)
    breath = breath_idle + (breath_live - breath_idle) * mix
    gain = intensity * (0.78 + 0.14 * breath)
    cx = (width - 1) * 0.5
    cy = (height - 1) * 0.5
    inv_diag = 1.0 / (math.hypot(cx, cy) + 1.0)
    pulse = (0.14 + 0.04 * mix) * (0.40 + 0.50 * breath)
    ax = cx + lsin(st * 0.62) * cx * 0.38
    ay = cy + lsin(st * 0.47 + 1.2) * cy * 0.32
    bx = cx + lsin(st * 0.31 + 2.1) * cx * 0.45
    by = cy + lsin(st * 0.53 + 0.4) * cy * 0.40
    return palette, mix, st, gain, cx, cy, inv_diag, pulse, ax, ay, bx, by


class _FieldGrid:
    """Per-size constants for the numpy field.

    Four of the six idle sine terms (and all four live ones) only depend on
    x, y, x+y, or distance from centre, so their LUT indices are computed
    once; per frame that is an integer add and a gather instead of float
    multiply, convert, and mask over the whole grid. x and y terms are 1-D.
    """

    def __init__(self, width: int, height: int, np_mod: Any) -> None:
        np = np_mod
        self.np = np
        self.x1 = np.arange(width, dtype=np.float32)
        self.y1 = np.arange(height, dtype=np.float32)
        self.x, self.y = np.meshgrid(self.x1, self.y1)
        cx = (width - 1) * 0.5
        cy = (height - 1) * 0.5
        self.dist = np.sqrt((self.x - cx) ** 2 + (self.y - cy) ** 2).astype(np.float32)
        inv_diag = 1.0 / (math.hypot(cx, cy) + 1.0)
        self.glow_shape = np.exp(-self.dist * np.float32(inv_diag * 3.2)).astype(np.float32)
        self.lut = np.asarray(SIN_LUT, dtype=np.float32)
        self._bases = {"x": self.x1, "y": self.y1, "xy": self.x + self.y, "dist": self.dist}
        self._idx: dict[tuple[str, float], Any] = {}

    def sin_static(self, name: str, coef: float, phase: float) -> Any:
        """sin(base * coef + phase) via the LUT; base is one of the fixed grids."""
        key = (name, float(coef))
        idx = self._idx.get(key)
        if idx is None:
            idx = (self._bases[name] * self.np.float32(coef * SIN_SIZE / TWO_PI)).astype(self.np.int32)
            self._idx[key] = idx
        off = int(round((phase % TWO_PI) * (SIN_SIZE / TWO_PI)))
        return self.lut[(idx + off) & SIN_MASK]


_FIELD_GRID: dict[tuple[int, int], _FieldGrid] = {}


def _field_grid(width: int, height: int, np_mod: Any) -> _FieldGrid:
    key = (int(width), int(height))
    hit = _FIELD_GRID.get(key)
    if hit is None:
        _FIELD_GRID.clear()
        hit = _FIELD_GRID[key] = _FieldGrid(width, height, np_mod)
    return hit


def _draw_field_numpy(
    width: int, height: int, scene: dict[str, Any], np_mod: Any, like: Any | None = None
) -> Any:
    import pygame

    palette, mix, st, gain, cx, cy, inv_diag, pulse, ax, ay, bx, by = _field_common(
        width, height, scene
    )
    pal = np_mod.asarray(palette, dtype=np_mod.uint8)
    grid = _field_grid(width, height, np_mod)
    lut = grid.lut
    scale = np_mod.float32(SIN_SIZE / TWO_PI)
    f32 = np_mod.float32

    def vsin(arr: Any) -> Any:
        idx = (arr * scale).astype(np_mod.int32) & SIN_MASK
        return lut[idx]

    glow42 = grid.glow_shape * f32(0.42 * pulse)
    use_idle = mix <= 0.02
    use_live = mix >= 0.98
    v_idle = None
    v_live = None
    if not use_live:
        da = np_mod.sqrt((grid.x - f32(ax)) ** 2 + (grid.y - f32(ay)) ** 2)
        db = np_mod.sqrt((grid.x - f32(bx)) ** 2 + (grid.y - f32(by)) ** 2)
        # Same terms as before: sum of six sines / 6 + 0.5, folded into the gain.
        wave = grid.sin_static("x", 0.048, st * 1.35)[None, :] + grid.sin_static("y", 0.042, -st * 1.18)[:, None]
        wave += grid.sin_static("xy", 0.028, st * 0.92)
        wave += grid.sin_static("dist", 0.055, -st * 0.74)
        wave += vsin(da * f32(0.062) - f32((st * 1.05) % TWO_PI))
        wave += vsin(db * f32(0.051) + f32((st * 0.88) % TWO_PI))
        blob = np_mod.exp(np_mod.minimum(da, db) * f32(-inv_diag * 2.4))
        v_idle = wave * f32(0.62 * gain / 6.0)
        v_idle += f32(0.27 + 0.31 * gain)
        v_idle += glow42
        v_idle += blob * f32(0.44 * pulse)
    if not use_idle:
        wave = grid.sin_static("x", 0.041, st)[None, :] + grid.sin_static("y", 0.036, -st * 0.81)[:, None]
        wave += grid.sin_static("xy", 0.021, st * 1.13)
        wave += grid.sin_static("dist", 0.048, -st * 0.47)
        v_live = wave * f32(0.40 * gain / 4.0)
        v_live += f32(0.24 + 0.20 * gain)
        v_live += glow42
    if use_idle:
        v = v_idle
    elif use_live:
        v = v_live
    else:
        v = v_idle + (v_live - v_idle) * f32(mix)
    v *= f32(255.0)
    idx = np_mod.clip(v.astype(np_mod.int32), 0, 254)
    rgb = np_mod.ascontiguousarray(pal[idx])
    surf = pygame.image.frombuffer(rgb, (width, height), "RGB")
    return surf.convert(like) if like is not None else surf.convert()


def _draw_field_python(
    width: int, height: int, scene: dict[str, Any], like: Any | None = None
) -> Any:
    import pygame

    palette, mix, st, gain, cx, cy, inv_diag, pulse, ax, ay, bx, by = _field_common(
        width, height, scene
    )
    buf = bytearray(width * height * 3)
    i = 0
    use_idle = mix <= 0.02
    use_live = mix >= 0.98
    for y in range(height):
        for x in range(width):
            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)
            glow = pulse * math.exp(-dist * inv_diag * 3.2)
            v_idle = v_live = 0.0
            if not use_live:
                da = math.sqrt((x - ax) ** 2 + (y - ay) ** 2)
                db = math.sqrt((x - bx) ** 2 + (y - by) ** 2)
                wave = (
                    lsin(x * 0.048 + st * 1.35)
                    + lsin(y * 0.042 - st * 1.18)
                    + lsin((x + y) * 0.028 + st * 0.92)
                    + lsin(dist * 0.055 - st * 0.74)
                    + lsin(da * 0.062 - st * 1.05)
                    + lsin(db * 0.051 + st * 0.88)
                ) * (1.0 / 6.0) + 0.5
                blob = pulse * math.exp(-min(da, db) * inv_diag * 2.4)
                v_idle = 0.27 + wave * 0.62 * gain + glow * 0.42 + blob * 0.44
            if not use_idle:
                wave = (
                    lsin(x * 0.041 + st)
                    + lsin(y * 0.036 - st * 0.81)
                    + lsin((x + y) * 0.021 + st * 1.13)
                    + lsin(dist * 0.048 - st * 0.47)
                ) * 0.25 + 0.5
                v_live = 0.24 + wave * 0.40 * gain + glow * 0.42
            if use_idle:
                v = v_idle
            elif use_live:
                v = v_live
            else:
                v = v_idle + (v_live - v_idle) * mix
            if v < 0.0:
                v = 0.0
            elif v > 0.999:
                v = 0.999
            r, g, b = palette[int(v * 255.0)]
            buf[i] = r
            buf[i + 1] = g
            buf[i + 2] = b
            i += 3
    surf = pygame.image.frombuffer(buf, (width, height), "RGB")
    return surf.convert(like) if like is not None else surf.convert()


def draw_field(
    width: int,
    height: int,
    scene: dict[str, Any],
    like: Any | None = None,
) -> Any:
    """Low-res field surface. `like` is the surface it will be scaled into."""
    try:
        import numpy as np
    except ImportError:
        return _draw_field_python(width, height, scene, like)
    return _draw_field_numpy(width, height, scene, np, like)


HUD_CLOCK_SLOT = "00:00:00"
HUD_CPU_SLOT = "CPU 100%"
HUD_WHISPER_GPU_SLOT = "VRAM 100%   100°C"
HUD_WHISPER_SLOT = "CPU 100%   VRAM 100%   100°C"
HUD_STATS_GPU_SLOT = "GPU 100%   VRAM 100%   100°C"
HUD_STATS_SLOT = "CPU 100%   GPU 100%   VRAM 100%   100°C"


def _hud_pct(value: Any) -> str:
    try:
        number = int(round(float(value or 0.0)))
    except (TypeError, ValueError):
        number = 0
    return f"{number:3d}%"


def hud_whisper_text(scene: dict[str, Any]) -> str:
    parts: list[str] = []
    if scene.get("has_cpu"):
        parts.append(f"CPU {_hud_pct(scene.get('cpu'))}")
    if scene.get("has_gpu"):
        parts.append(f"VRAM {_hud_pct(scene.get('vram'))}")
        try:
            temp = int(round(float(scene.get("temp") or 0.0)))
        except (TypeError, ValueError):
            temp = 0
        parts.append(f"{temp:3d}°C")
    return "   ".join(parts)


def hud_stats_text(scene: dict[str, Any]) -> str:
    parts: list[str] = []
    if scene.get("has_cpu"):
        parts.append(f"CPU {_hud_pct(scene.get('cpu'))}")
    if scene.get("has_gpu"):
        parts.append(f"GPU {_hud_pct(scene.get('util'))}")
        parts.append(f"VRAM {_hud_pct(scene.get('vram'))}")
        try:
            temp = int(round(float(scene.get("temp") or 0.0)))
        except (TypeError, ValueError):
            temp = 0
        parts.append(f"{temp:3d}°C")
    return "   ".join(parts)


def hud_metric_slot(scene: dict[str, Any], *, idle: bool) -> str:
    has_cpu = bool(scene.get("has_cpu"))
    has_gpu = bool(scene.get("has_gpu"))
    if has_cpu and has_gpu:
        return HUD_WHISPER_SLOT if idle else HUD_STATS_SLOT
    if has_gpu:
        return HUD_WHISPER_GPU_SLOT if idle else HUD_STATS_GPU_SLOT
    if has_cpu:
        return HUD_CPU_SLOT
    return ""


def hud_font_sizes(height: int) -> tuple[int, int, int]:
    """pygame.Font sizes at 1080p: phase 60, chrome 32, info 22."""
    h = max(1, int(height or 0))
    return (
        max(48, round(h * 60 / 1080)),
        max(26, round(h * 32 / 1080)),
        max(20, round(h * 22 / 1080)),
    )


def hud_anchor_center(face: Any, template: str, width: int, pad: int) -> int:
    return max(pad, (int(width) - int(face.size(template)[0])) // 2)


def hud_anchor_right(face: Any, template: str, right: int) -> int:
    return int(right) - int(face.size(template)[0])


def _hud_ends_sentence(word: str) -> bool:
    return word.rstrip("\"')").endswith((".", "?", "!"))


def hud_caption(text: str) -> str:
    """Sentence-case HUD text. Keep API/LLM and names; do not title every word."""
    special = {
        "api": "API",
        "llm": "LLM",
        "cpu": "CPU",
        "gpu": "GPU",
        "vram": "VRAM",
        "comfy": "Comfy",
        "comfyui": "ComfyUI",
        "gb": "GB",
        "rtx": "RTX",
        "gpt-4o": "gpt-4o",
        "qwen": "qwen",
        "qwen35": "qwen35",
        "qwen36": "qwen36",
        "gemma": "gemma",
        "gemma26": "gemma26",
        "glm": "glm",
        "flux": "Flux",
        "qwen-image:": "qwen-image:",
        "tabbyapi": "TabbyAPI",
        "/health": "/health",
    }
    bits: list[str] = []
    start = True
    for word in str(text or "").split(" "):
        if not word:
            bits.append(word)
            continue
        key = word.lower()
        if key in special:
            bits.append(special[key])
            start = _hud_ends_sentence(word)
            continue
        stem = word.rstrip(".,;:)")
        suffix = word[len(stem) :]
        if stem.lower() in special:
            bits.append(special[stem.lower()] + suffix)
            start = _hud_ends_sentence(word)
            continue
        if word[:1].isdigit() or word.startswith("~") or word.startswith("%"):
            bits.append(word)
            start = _hud_ends_sentence(word)
            continue
        if start:
            chars = list(word)
            for i, ch in enumerate(chars):
                if ch.isalpha():
                    chars[i] = ch.upper()
                    break
            bits.append("".join(chars))
        else:
            bits.append(word)
        start = _hud_ends_sentence(word)
    return " ".join(bits)


def _hud_apply_alpha(surf: Any, amt: float) -> None:
    setter = getattr(surf, "set_alpha", None)
    if setter is None:
        return
    setter(max(1, min(255, int(round(255 * amt)))))


def _hud_fade_layer(fg: Any, shadow_img: Any, halo: list[tuple[int, int]], radius: int) -> Any | None:
    """One surface so the halo and fill share the same fade, over the field."""
    get_size = getattr(fg, "get_size", None)
    if get_size is None:
        return None
    try:
        import pygame as pg
    except ImportError:
        return None
    tw, th = get_size()
    r = max(1, int(radius))
    layer = pg.Surface((tw + 2 * r, th + 2 * r), pg.SRCALPHA)
    for dx, dy in halo:
        layer.blit(shadow_img, (r + dx, r + dy))
    layer.blit(fg, (r, r))
    return layer


def hud_halo_offsets(radius: int = HUD_HALO_RADIUS) -> list[tuple[int, int]]:
    """Dark ring around glyphs so they stay readable on amber/white bloom."""
    r = max(1, int(radius))
    out: list[tuple[int, int]] = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx == 0 and dy == 0:
                continue
            if abs(dx) + abs(dy) > r + 1:
                continue
            out.append((dx, dy))
    return out


# Composed halo+fill per (font, text, colour). HUD text changes about once a
# second (the clock), so this turns the halo blits and two renders per label
# per frame into one blit. Cleared with the display: Surfaces die on pygame.quit().
_HUD_LAYER_CACHE: dict[tuple[Any, ...], Any] = {}
_HUD_LAYER_CACHE_MAX = 96


def _hud_layer(face: Any, text: str, color: Any, shadow: Any, halo: list[tuple[int, int]], radius: int) -> Any | None:
    key = (id(face), text, tuple(color))
    layer = _HUD_LAYER_CACHE.get(key)
    if layer is not None:
        return layer
    layer = _hud_fade_layer(face.render(text, True, color), face.render(text, True, shadow), halo, radius)
    if layer is None:
        return None
    if len(_HUD_LAYER_CACHE) >= _HUD_LAYER_CACHE_MAX:
        _HUD_LAYER_CACHE.clear()
    _HUD_LAYER_CACHE[key] = layer
    return layer


def _hud_fit(face: Any, text: str, max_w: int) -> str:
    raw = str(text or "")
    if not raw or max_w <= 0:
        return raw
    if face.size(raw)[0] <= max_w:
        return raw
    ell = "..."
    keep = raw
    while keep and face.size(keep + ell)[0] > max_w:
        keep = keep[:-1]
    return (keep + ell) if keep else ell


def _hud_wrap(face: Any, text: str, max_w: int, max_lines: int = 6) -> list[str]:
    raw = " ".join(str(text or "").split())
    if not raw or max_w <= 0 or max_lines <= 0:
        return []
    if face.size(raw)[0] <= max_w:
        return [raw]
    words = raw.split(" ")
    lines: list[str] = []
    cur = ""
    idx = 0
    while idx < len(words) and len(lines) < max_lines:
        word = words[idx]
        trial = word if not cur else f"{cur} {word}"
        if face.size(trial)[0] <= max_w:
            cur = trial
            idx += 1
            continue
        if cur:
            last = len(lines) == max_lines - 1
            rest = " ".join([cur] + words[idx:])
            lines.append(_hud_fit(face, rest, max_w) if last else cur)
            cur = ""
            if last:
                break
            continue
        lines.append(_hud_fit(face, word, max_w))
        idx += 1
    if cur and len(lines) < max_lines:
        last = len(lines) == max_lines - 1
        rest = " ".join([cur] + words[idx:])
        lines.append(_hud_fit(face, rest, max_w) if last else cur)
    return [line for line in lines if line]


def draw_hud(
    screen: Any, font, small, scene: dict[str, Any], info: Any | None = None
) -> None:
    cycle = str(scene.get("cycle") or "")
    down = str(scene.get("palette") or "") == "down" or not scene.get("connected")
    active = bool(scene.get("live")) or cycle in ("boot", "halt")
    idle_quiet = idle_hud_quiet(scene)
    w, h = screen.get_size()
    profile = str(scene["profile"])
    mode = str(scene["mode"]).upper()
    clock, date = str(scene.get("clock") or "").strip(), str(scene.get("date") or "").strip()
    if not clock:
        clock, date = wall_clock_parts()
    shadow = (0, 0, 0)
    halo_r = HUD_HALO_RADIUS
    halo = hud_halo_offsets(halo_r)
    pad = max(32, int(round(h * 32 / 1080)))
    main_h = font.size("Ag")[1]
    small_h = small.size("Ag")[1]
    info_font = info or small
    info_h = info_font.size("Ag")[1]
    gap = max(6, int(round(h * 8 / 1080)))
    fade_amt = 1.0

    def blit(
        text: str,
        pos: tuple[int, int],
        color,
        use_small: bool = False,
        face: Any | None = None,
    ) -> None:
        used = face or (small if use_small else font)
        x, y = pos
        layer = _hud_layer(used, text, color, shadow, halo, halo_r)
        if layer is not None:
            _hud_apply_alpha(layer, fade_amt)
            screen.blit(layer, (x - halo_r, y - halo_r))
            return
        # No pygame Surface (tests): halo by repeated shadow blits.
        img = used.render(text, True, shadow)
        fg = used.render(text, True, color)
        if fade_amt < 0.999:
            _hud_apply_alpha(img, fade_amt)
            _hud_apply_alpha(fg, fade_amt)
        for dx, dy in halo:
            screen.blit(img, (x + dx, y + dy))
        screen.blit(fg, pos)

    if idle_quiet:
        try:
            hud_alpha = float(scene.get("hud_alpha"))
        except (TypeError, ValueError):
            hold_raw = scene.get("hud_hold_s")
            try:
                hold_s = HUD_IDLE_HOLD_S if hold_raw is None else float(hold_raw)
            except (TypeError, ValueError):
                hold_s = HUD_IDLE_HOLD_S
            hud_alpha = idle_hud_alpha(float(scene.get("idle_s") or 0.0), hold_s=hold_s)
        if hud_alpha <= HUD_IDLE_HIDE_ALPHA:
            return
        fade_amt = hud_alpha
        whisper = hud_whisper_text(scene)
        fact = str(scene.get("idle_fact") or "").strip()
        if not fact:
            fact = pick_idle_fact(idle_fact_lines(load_idle_times(), float(scene.get("idle_s") or 0.0)), time.time())
        blit(profile, (pad, pad), MUTED)
        blit(mode, (hud_anchor_right(small, mode, w - pad), pad + 4), MUTED, use_small=True)
        cx = hud_anchor_center(font, HUD_CLOCK_SLOT, w, pad)
        cy = (h - main_h - small_h - gap) // 2
        blit(clock, (cx, cy), TEXT)
        if date:
            blit(date, (cx, cy + main_h + gap), MUTED, use_small=True)
        whisper_slot = hud_metric_slot(scene, idle=True)
        whisper_w = small.size(whisper_slot)[0] if whisper else 0
        fact_max = max(80, w - pad * 2 - (whisper_w + pad if whisper else 0))
        if fact:
            blit(_hud_fit(small, hud_caption(fact), fact_max), (pad, h - pad - small_h), MUTED, use_small=True)
        if whisper:
            blit(whisper, (hud_anchor_right(small, whisper_slot, w - pad), h - pad - small_h), MUTED, use_small=True)
        return

    phase = str(scene["phase"])
    runtime = str(scene.get("runtime") or "").strip()
    if runtime:
        phase = f"{phase}   {runtime}"
    typical = scene.get("typical_s")
    try:
        typical_n = float(typical) if typical is not None else 0.0
    except (TypeError, ValueError):
        typical_n = 0.0
    if typical_n >= 1.0 and str(scene.get("phase") or "") not in _CHAT_RUN_PHASES:
        phase = f"{phase}   ~{_fmt_runtime(typical_n)} typical"
    note = str(scene.get("note") or "").strip()
    if scene["connected"] and not down:
        stats = hud_stats_text(scene)
        stats_slot = hud_metric_slot(scene, idle=False)
    elif scene["connected"]:
        stats = ""
        stats_slot = ""
    else:
        stats = "API down"
        stats_slot = "API down"
    max_left = max(80, w - pad - small.size(stats_slot)[0] - pad) if stats else w - pad * 2

    dest = str(scene.get("image_file") or "").strip()
    what = str(scene.get("image_what") or "").strip()
    n = scene.get("image_n")
    of = scene.get("image_of")
    try:
        n_i = int(round(float(n))) if n else 0
        of_i = int(round(float(of))) if of else 0
    except (TypeError, ValueError):
        n_i, of_i = 0, 0
    if dest and of_i > 1 and n_i > 0:
        dest = f"{dest}  {n_i}/{of_i}"
    try:
        waiters = int(round(float(scene.get("waiters") or 0)))
    except (TypeError, ValueError):
        waiters = 0
    wait_line = f"{waiters} waiting" if waiters > 0 else ""
    tok_line = ""
    try:
        step_tok = int(round(float(scene.get("tokens") or 0)))
    except (TypeError, ValueError):
        step_tok = 0
    try:
        run_tok = int(round(float(scene.get("run_tokens") or step_tok)))
    except (TypeError, ValueError):
        run_tok = step_tok
    try:
        rate = float(scene.get("token_rate") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    stage = str(scene.get("stage") or "")
    if active and (
        stage in {"prefill", "decode", "tool"}
        or str(scene.get("phase") or "") in _CHAT_RUN_PHASES
    ):
        tok_line = tok_hud_line(run_tok, step_tok, rate)

    if down:
        phase_color = DOWN_TEXT
    else:
        phase_color = WARN if not scene["connected"] else (OK if active else MUTED)
    blit(profile, (pad, pad), TEXT)
    blit(mode, (hud_anchor_right(small, mode, w - pad), pad + 4), ACCENT, use_small=True)
    if clock:
        blit(clock, (hud_anchor_right(small, HUD_CLOCK_SLOT, w - pad), pad + 4 + small_h + gap), MUTED, use_small=True)
    extras: list[tuple[str, Any, Any]] = []
    if dest:
        extras.append((dest, TEXT, info_font))
    for line in _hud_wrap(info_font, what, max_left, 6):
        extras.append((line, MUTED, info_font))
    if note:
        extras.extend(
            (line, MUTED, info_font)
            for line in _hud_wrap(info_font, hud_caption(note), max_left, 4)
        )
    if tok_line:
        extras.append((tok_line, MUTED, info_font))
    if wait_line:
        extras.append((wait_line, MUTED, info_font))
    y = h - pad - main_h
    if extras:
        y -= sum((info_h if item[2] is info_font else small_h) + gap for item in extras)
    for line, color, face in extras:
        blit(_hud_fit(face, line, max_left), (pad, y), color, face=face)
        y += (info_h if face is info_font else small_h) + gap
    blit(hud_caption(phase), (pad, h - pad - main_h), phase_color)
    if stats:
        blit(stats, (hud_anchor_right(small, stats_slot, w - pad), h - pad - small_h), MUTED, use_small=True)


def tty_nr(name: str) -> int:
    text = (name or "").strip().lower().removeprefix("/dev/")
    if text.startswith("tty"):
        return int(text[3:])
    raise ValueError(f"not a virtual console: {name}")


def evdev_is_activity(ev_type: int) -> bool:
    """Keyboard/mouse only. EV_ABS is joysticks and rest-jitter on analog axes."""
    return ev_type in (EV_KEY, EV_REL)


def evdev_keep_device(name: str) -> bool:
    text = (name or "").lower()
    return not any(token in text for token in _IGNORE_INPUT_NAMES)


def idle_wait_s(
    *,
    idle_s: float,
    logout_idle_s: float,
    logged_in: bool,
    boot: bool = False,
) -> float:
    """Seconds to wait before taking the console. Zero on process start (reboot)."""
    if boot:
        return 0.0
    return idle_s if logged_in else logout_idle_s


def should_resume_saver(
    *,
    now: float,
    last_input: float,
    idle_s: float,
    logout_idle_s: float,
    logged_in: bool,
    was_logged_in: bool | None = None,
    boot: bool = False,
) -> bool:
    """Show the field after idle while logged in, or after logout-idle when not.

    boot=True (service just started after install or reboot) skips those waits
    so the kiosk takes the console immediately. After the first dismiss, idle
    and logout-idle apply as configured.
    """
    del was_logged_in
    wait = idle_wait_s(
        idle_s=idle_s,
        logout_idle_s=logout_idle_s,
        logged_in=logged_in,
        boot=boot,
    )
    return (now - last_input) >= wait


def login_from_ps(text: str) -> bool:
    for raw in text.splitlines():
        comm = raw.strip().split("/")[-1]
        if comm and comm not in _GETTY_COMMS:
            return True
    return False


def console_logged_in(tty: str) -> bool:
    name = (tty or "tty1").strip().removeprefix("/dev/")
    try:
        out = subprocess.check_output(
            ["loginctl", "list-sessions", "--no-legend"],
            text=True,
            timeout=1.0,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        out = ""
    if out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[4].removeprefix("/dev/") == name:
                return True
    try:
        ps_out = subprocess.check_output(
            ["ps", "-t", name, "-o", "comm="],
            text=True,
            timeout=1.0,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return login_from_ps(ps_out)


def vt_control_paths(own_tty: str = "") -> list[str]:
    """VT_ACTIVATE works on any console fd. Try the one this service owns
    first: /dev/tty0 and /dev/console are root-only (0600) on most boxes."""
    paths: list[str] = []
    own = (own_tty or "").strip()
    if own:
        paths.append(own if own.startswith("/dev/") else f"/dev/{own}")
    for extra in ("/dev/tty0", "/dev/console"):
        if extra not in paths:
            paths.append(extra)
    return paths


def _stdin_console_fd() -> int | None:
    """stdin when systemd handed us a TTY (TTYPath + StandardInput=tty)."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return fd if os.isatty(fd) else None


def activate_vt(nr: int, own_tty: str = "") -> None:
    last_exc: OSError | None = None
    fd = _stdin_console_fd()
    if fd is not None:
        try:
            fcntl.ioctl(fd, VT_ACTIVATE, nr)
            fcntl.ioctl(fd, VT_WAITACTIVE, nr)
            return
        except OSError as exc:
            last_exc = exc
    for path in vt_control_paths(own_tty):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            last_exc = exc
            continue
        try:
            fcntl.ioctl(fd, VT_ACTIVATE, nr)
            fcntl.ioctl(fd, VT_WAITACTIVE, nr)
            return
        except OSError as exc:
            last_exc = exc
        finally:
            os.close(fd)
    if last_exc is not None:
        print(f"tabby-saver: chvt {nr} failed: {last_exc}", file=sys.stderr)


def is_dismiss_event(event: Any, pygame_mod: Any, windowed: bool) -> str | None:
    if event.type == pygame_mod.QUIT:
        return "quit"
    if windowed and event.type == pygame_mod.KEYDOWN:
        if event.key in (pygame_mod.K_ESCAPE, pygame_mod.K_q):
            return "quit"
        return None
    if windowed:
        return None
    types = {getattr(pygame_mod, name, None) for name in _DISMISS_EVENT_NAMES}
    types.discard(None)
    if event.type in types:
        return "dismiss"
    return None


def field_input_action(
    event: Any,
    pygame_mod: Any,
    windowed: bool,
    *,
    idle_quiet: bool = False,
    hud_alpha: float = 1.0,
    hud_hold_s: float = HUD_IDLE_HOLD_S,
) -> str | None:
    """Mouse move peeks the clock. Key or click dismisses."""
    del idle_quiet, hud_alpha
    if (
        not windowed
        and event.type == getattr(pygame_mod, "MOUSEMOTION", None)
    ):
        return "peek" if float(hud_hold_s) > 0.0 else "dismiss"
    return is_dismiss_event(event, pygame_mod, windowed)


def watch_field_action(
    kind: str,
    *,
    idle_quiet: bool = False,
    hud_alpha: float = 1.0,
    hud_hold_s: float = HUD_IDLE_HOLD_S,
) -> str | None:
    """Same peek/dismiss rules as pygame events, for evdev while SDL is dummy."""
    del idle_quiet, hud_alpha
    if kind == "motion":
        return "peek" if float(hud_hold_s) > 0.0 else "dismiss"
    if kind == "key":
        return "dismiss"
    return None


def apply_peek_grace(grace_until: float, now: float) -> float:
    """Ignore dismiss for 1s after a peek so leftover mouse motion does not drop the field."""
    return max(float(grace_until), now + HUD_IDLE_PEEK_GRACE_S)


class InputWatch:
    """Global keyboard/mouse timestamps while the field is not on screen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._kind = "key"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def last(self) -> float:
        with self._lock:
            return self._last

    def last_kind(self) -> str:
        with self._lock:
            return self._kind

    def bump(self, kind: str = "key") -> None:
        with self._lock:
            self._last = time.monotonic()
            if kind in {"key", "motion"}:
                self._kind = kind

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="saver-input", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open_devices(self) -> list[int]:
        fds: list[int] = []
        for path in sorted(glob.glob("/dev/input/event*")):
            name_path = Path("/sys/class/input") / Path(path).name / "device" / "name"
            try:
                name = name_path.read_text(encoding="ascii", errors="replace")
            except OSError:
                name = ""
            if not evdev_keep_device(name):
                continue
            try:
                fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                continue
        return fds

    def _run(self) -> None:
        fmt = "llHHi"
        size = struct.calcsize(fmt)
        fds: list[int] = []
        refresh = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= refresh:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                fds = self._open_devices()
                refresh = now + 5.0
            if not fds:
                self._stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(fds, [], [], 0.4)
            except (OSError, ValueError):
                refresh = 0.0
                continue
            activity = False
            kind = "key"
            for fd in ready:
                try:
                    data = os.read(fd, size * 32)
                except BlockingIOError:
                    continue
                except OSError:
                    refresh = 0.0
                    activity = False
                    break
                for off in range(0, len(data) - size + 1, size):
                    _sec, _usec, ev_type, _code, _value = struct.unpack_from(fmt, data, off)
                    if evdev_is_activity(ev_type):
                        activity = True
                        kind = "key" if ev_type == EV_KEY else "motion"
                        break
                if activity:
                    break
            if activity:
                self.bump(kind)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tabby-stack activity screensaver (CPU framebuffer)")
    parser.add_argument(
        "--window",
        action="store_true",
        help="Windowed SDL for a machine that already has a GUI (dev only)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("TABBY_SAVER_URL", "http://127.0.0.1:5000"),
        help="TabbyAPI origin (default TABBY_SAVER_URL or http://127.0.0.1:5000)",
    )
    parser.add_argument("--fps", type=int, default=int(os.environ.get("TABBY_SAVER_FPS", "24")))
    parser.add_argument("--width", type=int, default=480, help="Internal field width")
    parser.add_argument("--height", type=int, default=270, help="Internal field height")
    parser.add_argument("--poll", type=float, default=float(os.environ.get("TABBY_SAVER_POLL_S", "1.0")), help="Seconds between idle API polls (busy stays 0.1)")
    parser.add_argument(
        "--idle",
        type=float,
        default=float(os.environ.get("TABBY_SAVER_IDLE_S", "120")),
        help="Seconds without input while logged in before the field comes back (default 120)",
    )
    parser.add_argument(
        "--logout-idle",
        type=float,
        default=float(os.environ.get("TABBY_SAVER_LOGOUT_IDLE_S", "5")),
        help="Seconds without input after logout / at the login prompt (default 5)",
    )
    parser.add_argument(
        "--hud-idle",
        type=float,
        default=float(os.environ.get("TABBY_SAVER_HUD_S", str(int(HUD_IDLE_HOLD_S)))),
        help="Seconds the idle clock stays up; 0 hides it (default 300)",
    )
    parser.add_argument(
        "--user-tty",
        default=os.environ.get("TABBY_SAVER_USER_TTY", "tty1"),
        help="Login/getty TTY to show when the saver dismisses",
    )
    parser.add_argument(
        "--saver-tty",
        default=os.environ.get("TABBY_SAVER_TTY", "tty8"),
        help="Virtual console the KMS field runs on",
    )
    return parser.parse_args(argv)


def _init_display(windowed: bool):
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    fb: CpuFramebuffer | None = None
    if not windowed:
        prepare_kiosk_sdl_env()
    try:
        import pygame
    except ImportError as exc:
        raise SystemExit(
            "tabby-saver: pygame is missing. On Arch: sudo pacman -S --needed python-pygame python-numpy"
        ) from exc
    pygame.init()
    try:
        pygame.joystick.quit()
    except Exception:
        pass
    _SLEEP_CACHE.clear()
    _SLEEP_SURF_CACHE.clear()
    _HUD_LAYER_CACHE.clear()
    pygame.mouse.set_visible(False)
    allowed = [pygame.QUIT, pygame.KEYDOWN, pygame.KEYUP]
    for name in _DISMISS_EVENT_NAMES:
        val = getattr(pygame, name, None)
        if val is not None and val not in allowed:
            allowed.append(val)
    pygame.event.set_allowed(allowed)
    if windowed:
        screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
        pygame.display.set_caption("tabbyapi-stack")
        return pygame, screen, None
    fb = CpuFramebuffer()
    pygame.display.set_mode((1, 1))
    # Same pixel layout as fb0 so present_surface can scale straight into it.
    screen = pygame.Surface(compose_size(*fb.size()), 0, 32, fb.masks)
    set_tty_graphics(True)
    return pygame, screen, fb


def _close_display(pygame_mod: Any, fb: CpuFramebuffer | None = None) -> None:
    # Free the marched solids while the field is hidden, and drop HUD layers:
    # those Surfaces die with pygame.quit(), and the next run makes new fonts.
    _SLEEP_CACHE.clear()
    _SLEEP_SURF_CACHE.clear()
    _HUD_LAYER_CACHE.clear()
    if fb is not None:
        try:
            fb.close()
        except Exception:
            pass
    set_tty_graphics(False)
    if pygame_mod is None:
        return
    try:
        pygame_mod.event.set_grab(False)
    except Exception:
        pass
    try:
        pygame_mod.display.quit()
    except Exception:
        pass
    try:
        pygame_mod.quit()
    except Exception:
        pass


def run_visible_field(
    args: argparse.Namespace,
    bus: StateBus,
    follow: SceneFollow,
    watch: InputWatch | None = None,
) -> str:
    """Paint until input (kiosk) or ESC/Q (window). Returns dismiss or quit."""
    pygame, screen, fb = _init_display(args.window)
    try:
        if args.window:
            pygame.event.set_grab(True)
    except Exception:
        pass
    pygame.event.clear()
    font_h = -1
    font = small = info = None
    clock = pygame.time.Clock()
    prev = time.monotonic()
    grace_until = prev if args.window else prev + 0.6
    scene: dict[str, Any] | None = None
    try:
        while True:
            now = time.monotonic()
            for event in pygame.event.get():
                quiet = idle_hud_quiet(scene) if scene else False
                raw_alpha = None if scene is None else scene.get("hud_alpha")
                try:
                    hud_alpha = 1.0 if raw_alpha is None else float(raw_alpha)
                except (TypeError, ValueError):
                    hud_alpha = 1.0
                action = field_input_action(
                    event,
                    pygame,
                    args.window,
                    idle_quiet=quiet,
                    hud_alpha=hud_alpha,
                    hud_hold_s=follow.hud_hold_s,
                )
                if action == "quit":
                    return "quit"
                if action == "peek":
                    follow.wake_idle_hud(now)
                    grace_until = apply_peek_grace(grace_until, now)
                    continue
                if action == "dismiss" and now >= grace_until:
                    print("tabby-saver: field dismissed (key or click)", file=sys.stderr)
                    return "dismiss"
            if watch is not None and now >= grace_until and watch.last() >= grace_until:
                quiet = idle_hud_quiet(scene) if scene else False
                raw_alpha = None if scene is None else scene.get("hud_alpha")
                try:
                    hud_alpha = 1.0 if raw_alpha is None else float(raw_alpha)
                except (TypeError, ValueError):
                    hud_alpha = 1.0
                action = watch_field_action(
                    watch.last_kind(),
                    idle_quiet=quiet,
                    hud_alpha=hud_alpha,
                    hud_hold_s=follow.hud_hold_s,
                )
                if action == "peek":
                    follow.wake_idle_hud(now)
                    grace_until = apply_peek_grace(grace_until, now)
                elif action == "dismiss":
                    print("tabby-saver: field dismissed (evdev key)", file=sys.stderr)
                    return "dismiss"
            dt = now - prev
            prev = now
            data, ok = bus.snapshot()
            if ok and data is not None:
                data = overlay_saver_live(
                    data, read_saver_live(), trust_idle_http=True
                )
            scene = follow.tick(scene_from_state(data, ok), dt, now)
            field = draw_field(max(64, args.width), max(36, args.height), scene, screen)
            pygame.transform.scale(field, screen.get_size(), screen)
            draw_sleepers(pygame, screen, scene)
            draw_neurons(pygame, screen, scene)
            draw_cycle_fx(pygame, screen, scene)
            height = screen.get_size()[1]
            if height != font_h or font is None or small is None or info is None:
                large_n, small_n, info_n = hud_font_sizes(height)
                font = pygame.font.Font(None, large_n)
                small = pygame.font.Font(None, small_n)
                info = pygame.font.Font(None, info_n)
                font_h = height
            draw_hud(screen, font, small, scene, info)
            if fb is not None:
                fb.present_surface(pygame, screen)
            else:
                pygame.display.flip()
            live = bool(scene and (scene.get("live") or overlay_amount(scene) > 0.04))
            fps = max(8, min(30, args.fps))
            if not live:
                # Idle only breathes and drifts; every 4K present is ~33 MB of
                # framebuffer writes, so do not spend them on motion nobody sees.
                fps = min(fps, IDLE_FPS)
            clock.tick(fps)
    finally:
        _close_display(pygame, fb)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = saver_url(args.url)
    bus = StateBus()
    thread = threading.Thread(target=bus.run, args=(url, max(0.2, args.poll)), daemon=True)
    thread.start()
    follow = SceneFollow(hud_hold_s=args.hud_idle)
    watch: InputWatch | None = None
    try:
        if args.window:
            try:
                action = run_visible_field(args, bus, follow)
            except Exception as exc:
                return _drm_fail(exc)
            return 0 if action in {"quit", "dismiss"} else 0

        watch = InputWatch()
        watch.start()
        user_tty = str(args.user_tty or "tty1")
        try:
            saver_nr = tty_nr(args.saver_tty)
            user_nr = tty_nr(user_tty)
        except ValueError as exc:
            print(f"tabby-saver: {exc}", file=sys.stderr)
            return 1
        logged_in = console_logged_in(user_tty)
        # Reboot / first start: take the console now. Idle and logout-idle
        # start after the first key/mouse dismiss, not at process start.
        boot = True
        show = True
        while True:
            if show:
                activate_vt(saver_nr, args.saver_tty)
                print("tabby-saver: showing field on", args.saver_tty, file=sys.stderr)
                try:
                    action = run_visible_field(args, bus, follow, watch=watch)
                except Exception as exc:
                    print(f"tabby-saver: display failed: {exc}", file=sys.stderr)
                    time.sleep(2.0)
                    continue
                if action == "quit":
                    return 0
                watch.bump()
                boot = False
                logged_in = console_logged_in(user_tty)
                activate_vt(user_nr, args.saver_tty)
                show = False
                continue
            login_checked = time.monotonic()
            while not show:
                now = time.monotonic()
                # loginctl + ps are two forks; every 0.25 s is wasted while the
                # user is at the console. 2 s is well inside the logout wait.
                if (now - login_checked) >= 2.0:
                    logged_in = console_logged_in(user_tty)
                    login_checked = now
                if should_resume_saver(
                    now=now,
                    last_input=watch.last(),
                    idle_s=max(1.0, float(args.idle)),
                    logout_idle_s=max(0.0, float(args.logout_idle)),
                    logged_in=logged_in,
                    boot=boot,
                ):
                    show = True
                    break
                time.sleep(0.25)
    finally:
        bus.stop.set()
        if watch is not None:
            watch.stop()
    return 0


def _drm_fail(exc: BaseException) -> int:
    print(
        "tabby-saver: could not open /dev/fb0 for a CPU framebuffer.\n"
        "  Need the video group, a free TTY, and nvidia-drm.modeset=1 (fbdev).\n"
        "  Do not enable this unit if Omarchy or a desktop already owns the GPU.\n"
        f"  {exc}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
