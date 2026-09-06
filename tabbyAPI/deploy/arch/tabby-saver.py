#!/usr/bin/python
"""CPU-rendered KMSDRM kiosk: stack activity as a thermal field.

Does not import TabbyAPI or CUDA. Polls GET /v1/ui/saver/state on localhost.
Software SDL only — do not point this at a GL renderer on the LLM GPU.
"""

from __future__ import annotations

import argparse
import colorsys
import fcntl
import glob
import json
import math
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
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
_GETTY_COMMS = frozenset({"agetty", "getty", "mingetty", "login", "(sd-pam)", "systemd"})
_DISMISS_EVENT_NAMES = (
    "KEYDOWN",
    "KEYUP",
    "MOUSEMOTION",
    "MOUSEBUTTONDOWN",
    "MOUSEBUTTONUP",
    "JOYBUTTONDOWN",
    "JOYAXISMOTION",
    "JOYHATMOTION",
)
HUD_IDLE_HOLD_S = 300.0
HUD_IDLE_FADE_S = 12.0
HUD_IDLE_HIDE_ALPHA = 0.02
HUD_IDLE_PEEK_GRACE_S = 1.0

SIN_BITS = 12
SIN_SIZE = 1 << SIN_BITS
SIN_MASK = SIN_SIZE - 1
SIN_LUT = [math.sin(i * (2.0 * math.pi / SIN_SIZE)) for i in range(SIN_SIZE)]
TWO_PI = 2.0 * math.pi


def lsin(x: float) -> float:
    return SIN_LUT[int(x * (SIN_SIZE / TWO_PI)) & SIN_MASK]


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
    "idle": _palette([(0.0, BG), (0.5, (14, 18, 28)), (1.0, (26, 34, 56))]),
    "chat": _palette(
        [
            (0.0, BG),
            (0.22, (24, 30, 64)),
            (0.48, (58, 78, 138)),
            (0.74, (72, 48, 128)),
            (1.0, (118, 48, 78)),
        ]
    ),
    "image": _palette([(0.0, BG), (0.42, (32, 22, 8)), (1.0, (128, 96, 32))]),
    "switch": _palette([(0.0, BG), (0.45, (8, 28, 22)), (1.0, (28, 96, 64))]),
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


def _shift_ramp(
    ramp: list[tuple[int, int, int]], hue_delta: float
) -> list[tuple[int, int, int]]:
    if abs(hue_delta) < 1e-6:
        return ramp
    return [_shift_color(c, hue_delta) for c in ramp]


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
_LAST_PROFILE_PATH = Path(__file__).resolve().parents[2] / "model_profiles" / "last.json"
_IDLE_TIMES: dict[str, Any] | None = None
_UNIT_STATE: tuple[float, str] = (0.0, "")
_SKIP_PROFILES = frozenset({"", "—", "-", "comfy", "flux", "llm", "restart"})


def load_idle_times() -> dict[str, Any]:
    global _IDLE_TIMES
    if _IDLE_TIMES is not None:
        return _IDLE_TIMES
    try:
        data = json.loads(_TIMES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    _IDLE_TIMES = data if isinstance(data, dict) else {}
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
            extra = " the service is up and still copying weights into VRAM."
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


_LIVE_PATH = Path(__file__).resolve().parents[2] / "saver-live.json"


def read_saver_live() -> dict[str, Any] | None:
    try:
        data = json.loads(_LIVE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def overlay_saver_live(
    payload: dict[str, Any] | None, live: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Apply the API sidecar when HTTP is idle or hung mid-prompt."""
    if not live or not live.get("busy"):
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
        self.elapsed_s = max(0.0, float(target.get("elapsed_s") or 0.0))
        dest_typical = target.get("typical_s")
        self.typical_s = max(0.0, float(dest_typical)) if dest_typical is not None else 0.0
        wall = time.time()
        self.clock, self.date = wall_clock_parts(wall)
        lt = time.localtime(wall)
        self.idle_hue = idle_tod_hue(lt.tm_hour + lt.tm_min / 60.0)
        if self.cycle == "idle" and not held:
            if self._idle_t0 <= 0.0:
                self._idle_t0 = now
            self.idle_s = max(0.0, now - self._idle_t0)
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
            self.task_name = self.phase
            if not same_run:
                self._task_t0 = now
            if same_run:
                self._step_t0 = now
        self.runtime_s = max(0.0, now - self._run_t0) if chat_run and self._run_t0 else (
            max(0.0, now - self._task_t0) if self._task_t0 else 0.0
        )
        step_s = max(0.0, now - self._step_t0) if chat_run and self._step_t0 else 0.0
        show_clock = dest == "down" or self.phase not in {"idle", "stirring", "settling"}
        if show_clock:
            clock_s = self.elapsed_s if self.elapsed_s > 0.5 else self.runtime_s
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
    util_raw = gpu.get("utilization_pct")
    vram_raw = gpu.get("vram_pct")
    temp_raw = gpu.get("temperature_c")
    has_gpu = any(value is not None and value != "" for value in (util_raw, vram_raw, temp_raw))
    util = _num(util_raw) if util_raw is not None else 0.0
    vram = _num(vram_raw) if vram_raw is not None else 0.0
    temp = _num(temp_raw, 40.0) if temp_raw is not None else 40.0
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
        intensity = min(0.40 + 0.14 * (vram / 100.0), 0.54)
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
        "connected": connected,
        "has_gpu": has_gpu,
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
        "elapsed_s": _num(data.get("elapsed_s")),
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

    def run(self, url: str, interval: float) -> None:
        host, port = origin_peer(url)
        while not self.stop.is_set():
            live = read_saver_live()
            if live and live.get("busy"):
                with self.lock:
                    cached = dict(self.data) if self.data else {}
                self.ingest(overlay_saver_live(cached, live), True)
            reachable = tcp_up(host, port)
            payload = fetch_state(url, timeout=0.15) if reachable else None
            if payload is None:
                reachable = tcp_up(host, port)
            live = read_saver_live()
            if payload is None and live and live.get("busy"):
                with self.lock:
                    payload = dict(self.data) if self.data else {}
            payload = overlay_saver_live(payload, live)
            self.ingest(payload, reachable or bool(payload and payload.get("busy")))
            self.stop.wait(interval)


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


# Idle-only: faint light in the field, not clip-art on top of it.
# Slots are only a cadence. Kind, place, and tint are hashed per appearance.
# At idle speed ~0.36 a slot is ~60–130s; some beats rest so it does not loop.
_SLEEP_KINDS = (
    "bloom",
    "lens",
    "halo",
    "diamond",
    "crescent",
    "vesica",
    "petal",
    "twin",
    "spark",
    "wedge",
)
_SLEEP_SLOTS = (
    (22.5, 0.06),
    (31.0, 0.38),
    (39.5, 0.61),
    (48.0, 0.87),
)
_SLEEP_LIFE = 0.20
_SLEEP_FADE = 0.08
_SLEEP_TINT = (36, 52, 82)
_SLEEP_ACCENTS = (
    (28, 64, 88),
    (48, 40, 92),
    (40, 58, 70),
    (52, 46, 72),
    (32, 56, 80),
)


def _sleep_unit(slot: int, cycle: int, salt: int) -> float:
    return _u01(slot + 3, int(cycle) * 10007 + salt)


def _sleep_pick_index(slot: int, cycle: int, count: int, salt: int) -> int:
    n = max(1, int(count))
    cyc = int(cycle)
    raw = int(_sleep_unit(slot, cyc, salt) * n) % n
    prev = int(_sleep_unit(slot, cyc - 1, salt) * n) % n
    prev_prev = int(_sleep_unit(slot, cyc - 2, salt) * n) % n
    if prev == prev_prev:
        prev = (prev + 1 + int(_sleep_unit(slot, cyc - 1, salt + 1) * (n - 1))) % n
    if raw == prev:
        raw = (raw + 1 + int(_sleep_unit(slot, cyc, salt + 1) * (n - 1))) % n
    return raw


def _sleep_xy(slot: int, cycle: int) -> tuple[float, float]:
    def at(cyc: int) -> tuple[float, float]:
        return (
            0.12 + 0.76 * _sleep_unit(slot, cyc, 41),
            0.14 + 0.72 * _sleep_unit(slot, cyc, 43),
        )

    x, y = at(cycle)
    px, py = at(cycle - 1)
    if (x - px) * (x - px) + (y - py) * (y - py) < 0.045:
        x = 0.12 + 0.76 * ((x + 0.37) % 1.0)
        y = 0.14 + 0.72 * ((y + 0.29) % 1.0)
    return x, y


def _sleep_tint_for(slot: int, cycle: int) -> tuple[int, int, int]:
    shift = (_sleep_unit(slot, cycle, 61) - 0.5) * 0.22
    base = _shift_color(_SLEEP_TINT, shift)
    accent = _SLEEP_ACCENTS[_sleep_pick_index(slot, cycle, len(_SLEEP_ACCENTS), 67)]
    mixed = _mix(base, accent, 0.16 + 0.28 * _sleep_unit(slot, cycle, 71))
    return (
        max(18, min(70, mixed[0] + int(10 * (_sleep_unit(slot, cycle, 73) - 0.5)))),
        max(24, min(86, mixed[1] + int(12 * (_sleep_unit(slot, cycle, 74) - 0.5)))),
        max(40, min(118, mixed[2] + int(14 * (_sleep_unit(slot, cycle, 75) - 0.5)))),
    )


def idle_sleeper_items(scene: dict[str, Any], width: int, height: int) -> list[dict[str, Any]]:
    """Soft luminous breaths while the field is fully idle. Empty otherwise."""
    if overlay_amount(scene) > 0.04:
        return []
    if str(scene.get("cycle") or "idle") != "idle":
        return []
    if scene.get("live"):
        return []
    if str(scene.get("palette") or "") == "down" or not scene.get("connected", True):
        return []
    st = float(scene.get("st") or 0.0)
    hue = float(scene.get("hue") or 0.0)
    w = max(1, int(width))
    h = max(1, int(height))
    out: list[dict[str, Any]] = []
    placed: list[tuple[float, float]] = []
    used_kinds: set[str] = set()
    n_kinds = len(_SLEEP_KINDS)
    for slot, (period, phase) in enumerate(_SLEEP_SLOTS):
        period = max(1.0, float(period))
        clock = st / period + phase + hue
        u = clock % 1.0
        if u > _SLEEP_LIFE:
            continue
        cycle = int(math.floor(clock - u + 1e-9))
        if _sleep_unit(slot, cycle, 13) < 0.24:
            continue
        fade = _smoothstep(u / _SLEEP_FADE) * _smoothstep((_SLEEP_LIFE - u) / _SLEEP_FADE)
        if fade <= 0.02:
            continue
        kind_i = _sleep_pick_index(slot, cycle, n_kinds, 17)
        kind = _SLEEP_KINDS[kind_i]
        if kind in used_kinds:
            kind = _SLEEP_KINDS[(kind_i + 1 + int(_sleep_unit(slot, cycle, 19) * (n_kinds - 1))) % n_kinds]
        used_kinds.add(kind)
        x, y = _sleep_xy(slot, cycle)
        for ox, oy in placed:
            if (x - ox) * (x - ox) + (y - oy) * (y - oy) < 0.05:
                x = 0.12 + 0.76 * ((x + 0.41) % 1.0)
                y = 0.14 + 0.72 * ((y + 0.33) % 1.0)
        drift = u / _SLEEP_LIFE
        dx = 0.028 * (_sleep_unit(slot, cycle, 23) - 0.5)
        dy = -0.010 - 0.022 * _sleep_unit(slot, cycle, 24)
        x += dx * drift + 0.006 * lsin(st * 0.11 + slot * 1.7)
        y += dy * drift + 0.004 * lsin(st * 0.09 + slot * 2.1)
        x = 0.08 if x < 0.08 else 0.92 if x > 0.92 else x
        y = 0.10 if y < 0.10 else 0.88 if y > 0.88 else y
        placed.append((x, y))
        scale = 0.70 + 0.75 * _sleep_unit(slot, cycle, 29)
        breath = 0.88 + 0.12 * lsin(st * 0.20 + slot * 1.3)
        out.append(
            {
                "kind": kind,
                "x": int(round(x * (w - 1))),
                "y": int(round(y * (h - 1))),
                "size": max(12, int(round(h * 0.15 * scale))),
                "amt": fade * breath,
                "angle": TWO_PI * _sleep_unit(slot, cycle, 31),
                "tint": _sleep_tint_for(slot, cycle),
                "seed": slot * 10007 + cycle,
            }
        )
    return out


def _sleep_add_color(lift: float, tint: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    t = _clamp01(lift)
    ink = tint or _SLEEP_TINT
    return (
        max(0, min(36, int(ink[0] * t))),
        max(0, min(44, int(ink[1] * t))),
        max(0, min(52, int(ink[2] * t))),
    )


def _blit_sleep_add(
    pygame_mod: Any, screen: Any, surf: Any, cx: int, cy: int, angle: float = 0.0
) -> None:
    if abs(angle) > 0.03:
        surf = pygame_mod.transform.rotate(surf, -angle * 180.0 / math.pi)
    flags = getattr(pygame_mod, "BLEND_RGB_ADD", 0)
    screen.blit(surf, (cx - surf.get_width() // 2, cy - surf.get_height() // 2), special_flags=flags)


def _sleep_blank(pygame_mod: Any, width: int, height: int) -> Any:
    surf = pygame_mod.Surface((max(3, int(width)), max(3, int(height))))
    surf.fill((0, 0, 0))
    return surf


def _sleep_ellipse_sheet(
    pygame_mod: Any,
    rx: int,
    ry: int,
    amt: float,
    tint: tuple[int, int, int],
    hole: float = 0.0,
) -> Any | None:
    amt = _clamp01(amt)
    rx = max(4, int(rx))
    ry = max(4, int(ry))
    if amt <= 0.02:
        return None
    w = rx * 2 + 3
    h = ry * 2 + 3
    surf = _sleep_blank(pygame_mod, w, h)
    ox, oy = w // 2, h // 2
    steps = max(5, min(10, min(rx, ry) // 2))
    for i in range(steps, 0, -1):
        t = i / float(steps)
        fall = (1.0 - t) ** 1.7
        color = _sleep_add_color(amt * (0.16 + 0.84 * fall) * 0.38, tint)
        if color == (0, 0, 0):
            continue
        ww = max(1, int(round(rx * t)))
        hh = max(1, int(round(ry * t)))
        pygame_mod.draw.ellipse(surf, color, (ox - ww, oy - hh, ww * 2, hh * 2))
    if hole > 0.08:
        inner_x = max(1, int(round(rx * hole)))
        inner_y = max(1, int(round(ry * hole)))
        pygame_mod.draw.ellipse(
            surf, (0, 0, 0), (ox - inner_x, oy - inner_y, inner_x * 2, inner_y * 2)
        )
    return surf


def _sleep_nested_poly(
    pygame_mod: Any,
    pts: list[tuple[float, float]],
    amt: float,
    tint: tuple[int, int, int],
) -> Any | None:
    if len(pts) < 3 or _clamp01(amt) <= 0.02:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    pad = 3
    w = max(6, int(math.ceil(maxx - minx)) + pad * 2)
    h = max(6, int(math.ceil(maxy - miny)) + pad * 2)
    surf = _sleep_blank(pygame_mod, w, h)
    ox = -minx + pad
    oy = -miny + pad
    cx = sum(xs) / len(pts) + ox
    cy = sum(ys) / len(pts) + oy
    shifted = [(p[0] + ox, p[1] + oy) for p in pts]
    steps = 7
    for i in range(steps, 0, -1):
        t = i / float(steps)
        fall = (1.0 - t) ** 1.55
        color = _sleep_add_color(amt * (0.18 + 0.82 * fall) * 0.36, tint)
        if color == (0, 0, 0):
            continue
        scaled = [(cx + (px - cx) * t, cy + (py - cy) * t) for px, py in shifted]
        pygame_mod.draw.polygon(surf, color, scaled)
    return surf


def _sleep_disc_sheet(
    pygame_mod: Any,
    discs: list[tuple[float, float, float]],
    amt: float,
    tint: tuple[int, int, int],
) -> Any | None:
    if not discs or _clamp01(amt) <= 0.02:
        return None
    xs = [x + r for x, _y, r in discs] + [x - r for x, _y, r in discs]
    ys = [y + r for _x, y, r in discs] + [y - r for _x, y, r in discs]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    pad = 3
    w = max(6, int(math.ceil(maxx - minx)) + pad * 2)
    h = max(6, int(math.ceil(maxy - miny)) + pad * 2)
    surf = _sleep_blank(pygame_mod, w, h)
    ox = -minx + pad
    oy = -miny + pad
    steps = 6
    for i in range(steps, 0, -1):
        t = i / float(steps)
        fall = (1.0 - t) ** 1.6
        color = _sleep_add_color(amt * (0.16 + 0.84 * fall) * 0.38, tint)
        if color == (0, 0, 0):
            continue
        for x, y, r in discs:
            rad = max(1, int(round(r * t)))
            pygame_mod.draw.circle(surf, color, (int(round(x + ox)), int(round(y + oy))), rad)
    return surf


def _draw_sleeping_bloom(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    rx = max(6, int(size * (0.88 + 0.18 * abs(math.cos(angle)))))
    ry = max(5, int(size * (0.62 + 0.16 * abs(math.sin(angle * 1.2)))))
    surf = _sleep_ellipse_sheet(pygame_mod, rx, ry, amt, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle * 0.25)


def _draw_sleeping_lens(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    surf = _sleep_ellipse_sheet(
        pygame_mod, max(10, int(size * 1.48)), max(4, int(size * 0.34)), amt * 0.92, tint
    )
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle)


def _draw_sleeping_halo(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    rx = max(10, int(size * 1.18))
    ry = max(8, int(rx * (0.78 + 0.22 * abs(math.sin(angle)))))
    surf = _sleep_ellipse_sheet(pygame_mod, rx, ry, amt * 0.80, tint, hole=0.58 + 0.10 * abs(math.cos(angle)))
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle * 0.2)


def _draw_sleeping_diamond(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    rx = max(8, size * 0.82)
    ry = max(7, size * 0.62)
    pts = [(0.0, -ry), (rx, 0.0), (0.0, ry), (-rx, 0.0)]
    surf = _sleep_nested_poly(pygame_mod, pts, amt, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle)


def _draw_sleeping_crescent(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    r = max(8, int(size * 0.92))
    surf = _sleep_ellipse_sheet(pygame_mod, r, r, amt, tint)
    if surf is None:
        return
    punch = max(5, int(r * 0.78))
    ox = int(round(math.cos(angle) * r * 0.42))
    oy = int(round(math.sin(angle) * r * 0.42))
    pygame_mod.draw.circle(surf, (0, 0, 0), (surf.get_width() // 2 + ox, surf.get_height() // 2 + oy), punch)
    _blit_sleep_add(pygame_mod, screen, surf, cx, cy)


def _draw_sleeping_vesica(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    r = max(6, size * 0.48)
    span = r * 0.62
    discs = [(-span, 0.0, r), (span, 0.0, r)]
    surf = _sleep_disc_sheet(pygame_mod, discs, amt * 0.9, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle)


def _draw_sleeping_petal(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    discs: list[tuple[float, float, float]] = []
    n = 5
    for i in range(n):
        t = i / float(n - 1)
        discs.append((0.0, (t - 0.55) * size * 1.15, max(2.5, size * (0.42 - 0.28 * t))))
    surf = _sleep_disc_sheet(pygame_mod, discs, amt, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle)


def _draw_sleeping_twin(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    r = max(5, size * 0.40)
    span = r * 1.15
    discs = [(-span, 0.0, r), (span, 0.0, r * 0.82)]
    surf = _sleep_disc_sheet(pygame_mod, discs, amt * 0.88, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle)


def _draw_sleeping_spark(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
    seed: int,
) -> None:
    n = 3 + int(_u01(seed, 5) * 3)
    discs: list[tuple[float, float, float]] = []
    for i in range(n):
        a = angle + i * (TWO_PI / n) + _u01(seed, 7 + i) * 0.7
        rad = size * (0.28 + 0.55 * _u01(seed, 11 + i))
        discs.append((math.cos(a) * rad, math.sin(a) * rad, max(2.0, size * (0.10 + 0.12 * _u01(seed, 13 + i)))))
    surf = _sleep_disc_sheet(pygame_mod, discs, amt * 0.95, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy)


def _draw_sleeping_wedge(
    pygame_mod: Any,
    screen: Any,
    cx: int,
    cy: int,
    size: int,
    amt: float,
    angle: float,
    tint: tuple[int, int, int],
) -> None:
    h = max(8, size * 0.92)
    b = max(7, size * 0.70)
    pts = [(0.0, -h), (b, h * 0.55), (-b, h * 0.55)]
    surf = _sleep_nested_poly(pygame_mod, pts, amt * 0.9, tint)
    if surf is not None:
        _blit_sleep_add(pygame_mod, screen, surf, cx, cy, angle)


def draw_sleepers(pygame_mod: Any, screen: Any, scene: dict[str, Any]) -> None:
    w, h = screen.get_size()
    for item in idle_sleeper_items(scene, w, h):
        kind = item["kind"]
        x, y, size, amt = item["x"], item["y"], item["size"], item["amt"]
        angle = float(item.get("angle") or 0.0)
        tint = item.get("tint") or _SLEEP_TINT
        seed = int(item.get("seed") or 0)
        if kind == "lens":
            _draw_sleeping_lens(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "halo":
            _draw_sleeping_halo(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "diamond":
            _draw_sleeping_diamond(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "crescent":
            _draw_sleeping_crescent(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "vesica":
            _draw_sleeping_vesica(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "petal":
            _draw_sleeping_petal(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "twin":
            _draw_sleeping_twin(pygame_mod, screen, x, y, size, amt, angle, tint)
        elif kind == "spark":
            _draw_sleeping_spark(pygame_mod, screen, x, y, size, amt, angle, tint, seed)
        elif kind == "wedge":
            _draw_sleeping_wedge(pygame_mod, screen, x, y, size, amt, angle, tint)
        else:
            _draw_sleeping_bloom(pygame_mod, screen, x, y, size, amt, angle, tint)


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
    gain = intensity * (0.70 + 0.14 * breath)
    cx = (width - 1) * 0.5
    cy = (height - 1) * 0.5
    inv_diag = 1.0 / (math.hypot(cx, cy) + 1.0)
    pulse = (0.14 + 0.04 * mix) * (0.40 + 0.50 * breath)
    ax = cx + lsin(st * 0.62) * cx * 0.38
    ay = cy + lsin(st * 0.47 + 1.2) * cy * 0.32
    bx = cx + lsin(st * 0.31 + 2.1) * cx * 0.45
    by = cy + lsin(st * 0.53 + 0.4) * cy * 0.40
    return palette, mix, st, gain, cx, cy, inv_diag, pulse, ax, ay, bx, by


def _draw_field_numpy(width: int, height: int, scene: dict[str, Any], np_mod: Any) -> Any:
    import pygame

    palette, mix, st, gain, cx, cy, inv_diag, pulse, ax, ay, bx, by = _field_common(
        width, height, scene
    )
    pal = np_mod.asarray(palette, dtype=np_mod.uint8)
    lut = np_mod.asarray(SIN_LUT, dtype=np_mod.float32)
    scale = np_mod.float32(SIN_SIZE / TWO_PI)

    def vsin(arr: Any) -> Any:
        idx = (arr * scale).astype(np_mod.int32) & SIN_MASK
        return lut[idx]

    xs = np_mod.arange(width, dtype=np_mod.float32)
    ys = np_mod.arange(height, dtype=np_mod.float32)
    x, y = np_mod.meshgrid(xs, ys)
    dist = np_mod.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    glow = pulse * np_mod.exp(-dist * inv_diag * 3.2)
    use_idle = mix <= 0.02
    use_live = mix >= 0.98
    v_idle = None
    v_live = None
    if not use_live:
        da = np_mod.sqrt((x - ax) ** 2 + (y - ay) ** 2)
        db = np_mod.sqrt((x - bx) ** 2 + (y - by) ** 2)
        wave = (
            vsin(x * 0.048 + st * 1.35)
            + vsin(y * 0.042 - st * 1.18)
            + vsin((x + y) * 0.028 + st * 0.92)
            + vsin(dist * 0.055 - st * 0.74)
            + vsin(da * 0.062 - st * 1.05)
            + vsin(db * 0.051 + st * 0.88)
        ) * (1.0 / 6.0) + 0.5
        blob = pulse * np_mod.exp(-np_mod.minimum(da, db) * inv_diag * 2.4)
        v_idle = 0.16 + wave * 0.52 * gain + glow * 0.32 + blob * 0.36
    if not use_idle:
        wave = (
            vsin(x * 0.041 + st)
            + vsin(y * 0.036 - st * 0.81)
            + vsin((x + y) * 0.021 + st * 1.13)
            + vsin(dist * 0.048 - st * 0.47)
        ) * 0.25 + 0.5
        v_live = 0.18 + wave * 0.34 * gain + glow * 0.38
    if use_idle:
        v = v_idle
    elif use_live:
        v = v_live
    else:
        v = v_idle + (v_live - v_idle) * mix
    v = np_mod.clip(v, 0.0, 0.999)
    rgb = np_mod.ascontiguousarray(pal[(v * 255.0).astype(np_mod.int32)], dtype=np_mod.uint8)
    return pygame.image.frombuffer(rgb.tobytes(), (width, height), "RGB").convert()


def _draw_field_python(width: int, height: int, scene: dict[str, Any]) -> Any:
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
                v_idle = 0.16 + wave * 0.52 * gain + glow * 0.32 + blob * 0.36
            if not use_idle:
                wave = (
                    lsin(x * 0.041 + st)
                    + lsin(y * 0.036 - st * 0.81)
                    + lsin((x + y) * 0.021 + st * 1.13)
                    + lsin(dist * 0.048 - st * 0.47)
                ) * 0.25 + 0.5
                v_live = 0.18 + wave * 0.34 * gain + glow * 0.38
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
    return pygame.image.frombuffer(buf, (width, height), "RGB").convert()


def draw_field(
    width: int,
    height: int,
    scene: dict[str, Any],
) -> Any:
    try:
        import numpy as np
    except ImportError:
        return _draw_field_python(width, height, scene)
    return _draw_field_numpy(width, height, scene, np)


HUD_CLOCK_SLOT = "00:00:00"
HUD_WHISPER_SLOT = "VRAM 100%   100°C"
HUD_STATS_SLOT = "GPU 100%   VRAM 100%   100°C"


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


def hud_caption(text: str) -> str:
    """Title-case HUD words. Keep times and API/LLM/GPU as acronyms."""
    special = {
        "api": "API",
        "llm": "LLM",
        "gpu": "GPU",
        "vram": "VRAM",
        "comfy": "Comfy",
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
    for word in str(text or "").split(" "):
        if not word:
            bits.append(word)
            continue
        key = word.lower()
        if key in special:
            bits.append(special[key])
            continue
        stem = word.rstrip(".,;:")
        suffix = word[len(stem) :]
        if stem.lower() in special:
            bits.append(special[stem.lower()] + suffix)
            continue
        if word[:1].isdigit() or word.startswith("~") or word.startswith("%"):
            bits.append(word)
            continue
        bits.append(word[:1].upper() + word[1:] if word else word)
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


def hud_halo_offsets(radius: int = 3) -> list[tuple[int, int]]:
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
    halo = hud_halo_offsets(3)
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
        img = used.render(text, True, shadow)
        fg = used.render(text, True, color)
        if fade_amt < 0.999:
            layer = _hud_fade_layer(fg, img, halo, 3)
            if layer is not None:
                _hud_apply_alpha(layer, fade_amt)
                screen.blit(layer, (x - 3, y - 3))
                return
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
        whisper = ""
        if scene.get("has_gpu"):
            vram = int(round(scene["vram"]))
            temp = int(round(scene["temp"]))
            whisper = f"VRAM {vram:3d}%   {temp:3d}°C"
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
        whisper_w = small.size(HUD_WHISPER_SLOT)[0] if whisper else 0
        fact_max = max(80, w - pad * 2 - (whisper_w + pad if whisper else 0))
        if fact:
            blit(_hud_fit(small, hud_caption(fact), fact_max), (pad, h - pad - small_h), MUTED, use_small=True)
        if whisper:
            blit(whisper, (hud_anchor_right(small, HUD_WHISPER_SLOT, w - pad), h - pad - small_h), MUTED, use_small=True)
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
    if scene["connected"] and scene.get("has_gpu") and not down:
        util = int(round(scene["util"]))
        vram = int(round(scene["vram"]))
        temp = int(round(scene["temp"]))
        stats = f"GPU {util:3d}%   VRAM {vram:3d}%   {temp:3d}°C"
    elif scene["connected"]:
        stats = ""
    else:
        stats = "API Down"
    max_left = max(80, w - pad - small.size(HUD_STATS_SLOT)[0] - pad) if stats else w - pad * 2

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
    wait_line = f"{waiters} Waiting" if waiters > 0 else ""
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
        blit(stats, (hud_anchor_right(small, HUD_STATS_SLOT, w - pad), h - pad - small_h), MUTED, use_small=True)


def tty_nr(name: str) -> int:
    text = (name or "").strip().lower().removeprefix("/dev/")
    if text.startswith("tty"):
        return int(text[3:])
    raise ValueError(f"not a virtual console: {name}")


def evdev_is_activity(ev_type: int) -> bool:
    return ev_type in (EV_KEY, EV_REL, EV_ABS)


def should_resume_saver(
    *,
    now: float,
    last_input: float,
    idle_s: float,
    logout_idle_s: float,
    logged_in: bool,
    was_logged_in: bool | None = None,
) -> bool:
    """Show the field after idle while logged in, or after logout-idle when not."""
    del was_logged_in
    wait = idle_s if logged_in else logout_idle_s
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


def activate_vt(nr: int) -> None:
    last_exc: OSError | None = None
    for path in ("/dev/tty0", "/dev/console"):
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
    """Peek the idle clock on mouse move when it has faded; otherwise dismiss."""
    if (
        not windowed
        and idle_quiet
        and float(hud_hold_s) > 0.0
        and hud_alpha <= HUD_IDLE_HIDE_ALPHA
        and event.type == getattr(pygame_mod, "MOUSEMOTION", None)
    ):
        return "peek"
    return is_dismiss_event(event, pygame_mod, windowed)


def apply_peek_grace(grace_until: float, now: float) -> float:
    """Ignore dismiss for 1s after a peek so leftover mouse motion does not drop the field."""
    return max(float(grace_until), now + HUD_IDLE_PEEK_GRACE_S)


class InputWatch:
    """Global keyboard/mouse timestamps while the field is not on screen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def last(self) -> float:
        with self._lock:
            return self._last

    def bump(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="saver-input", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open_devices(self) -> list[int]:
        fds: list[int] = []
        for path in sorted(glob.glob("/dev/input/event*")):
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
                        break
                if activity:
                    break
            if activity:
                self.bump()
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tabby-stack activity screensaver (KMSDRM)")
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
    parser.add_argument("--poll", type=float, default=0.1, help="Seconds between API polls")
    parser.add_argument(
        "--idle",
        type=float,
        default=float(os.environ.get("TABBY_SAVER_IDLE_S", "120")),
        help="Seconds without input while logged in before the field comes back (default 120)",
    )
    parser.add_argument(
        "--logout-idle",
        type=float,
        default=float(os.environ.get("TABBY_SAVER_LOGOUT_IDLE_S", "10")),
        help="Seconds without input after logout / at the login prompt (default 10)",
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
    if not windowed:
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        os.environ.setdefault("SDL_RENDER_DRIVER", "software")
        os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        os.environ.pop("DISPLAY", None)
        os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        import pygame
    except ImportError as exc:
        raise SystemExit(
            "tabby-saver: pygame is missing. On Arch: sudo pacman -S --needed python-pygame python-numpy"
        ) from exc
    pygame.init()
    pygame.mouse.set_visible(False)
    allowed = [pygame.QUIT, pygame.KEYDOWN, pygame.KEYUP]
    for name in _DISMISS_EVENT_NAMES:
        val = getattr(pygame, name, None)
        if val is not None and val not in allowed:
            allowed.append(val)
    pygame.event.set_allowed(allowed)
    flags = pygame.RESIZABLE if windowed else pygame.FULLSCREEN | pygame.NOFRAME
    size = (1280, 720) if windowed else (0, 0)
    try:
        screen = pygame.display.set_mode(size, flags)
    except pygame.error:
        if windowed:
            raise
        screen = pygame.display.set_mode((1920, 1080), flags)
    pygame.display.set_caption("tabbyapi-stack")
    return pygame, screen


def _close_display(pygame_mod: Any) -> None:
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


def run_visible_field(args: argparse.Namespace, bus: StateBus, follow: SceneFollow) -> str:
    """Paint until input (kiosk) or ESC/Q (window). Returns dismiss or quit."""
    pygame, screen = _init_display(args.window)
    try:
        if not args.window:
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
                    return "dismiss"
            dt = now - prev
            prev = now
            data, ok = bus.snapshot()
            data = overlay_saver_live(data, read_saver_live())
            ok = bool(ok or (data and data.get("busy")))
            scene = follow.tick(scene_from_state(data, ok), dt, now)
            field = draw_field(max(64, args.width), max(36, args.height), scene)
            draw_sleepers(pygame, field, scene)
            screen.blit(pygame.transform.smoothscale(field, screen.get_size()), (0, 0))
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
            pygame.display.flip()
            clock.tick(max(8, min(30, args.fps)))
    finally:
        _close_display(pygame)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = saver_url(args.url)
    bus = StateBus()
    thread = threading.Thread(target=bus.run, args=(url, max(0.08, args.poll)), daemon=True)
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
        show = True
        while True:
            if show:
                activate_vt(saver_nr)
                try:
                    action = run_visible_field(args, bus, follow)
                except Exception as exc:
                    print(f"tabby-saver: display failed: {exc}", file=sys.stderr)
                    time.sleep(2.0)
                    continue
                if action == "quit":
                    return 0
                watch.bump()
                logged_in = console_logged_in(user_tty)
                activate_vt(user_nr)
                show = False
                continue
            while not show:
                now = time.monotonic()
                now_login = console_logged_in(user_tty)
                if should_resume_saver(
                    now=now,
                    last_input=watch.last(),
                    idle_s=max(1.0, float(args.idle)),
                    logout_idle_s=max(0.0, float(args.logout_idle)),
                    logged_in=now_login,
                ):
                    show = True
                logged_in = now_login
                if show:
                    break
                time.sleep(0.25)
    finally:
        bus.stop.set()
        if watch is not None:
            watch.stop()
    return 0


def _drm_fail(exc: BaseException) -> int:
    print(
        "tabby-saver: could not open a KMSDRM display.\n"
        "  Need a free TTY, nvidia-drm.modeset=1, and the video group.\n"
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
