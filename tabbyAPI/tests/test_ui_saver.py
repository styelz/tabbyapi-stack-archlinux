import importlib.util
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from common import live_decode
from ui import saver


def _load_kiosk():
    path = Path(__file__).resolve().parents[1] / "deploy/arch/tabby-saver.py"
    spec = importlib.util.spec_from_file_location("tabby_saver_kiosk", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _request(host: str | None, forwarded: str | None = None):
    client = None if host is None else SimpleNamespace(host=host)
    headers = {}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(client=client, headers=headers)


class _FakeScreen:
    def __init__(self) -> None:
        self.blits: list[tuple] = []

    def get_size(self) -> tuple[int, int]:
        return (1280, 720)

    def blit(self, *args: object) -> None:
        self.blits.append(args)


class _FakeFont:
    def size(self, text: str) -> tuple[int, int]:
        return (len(text) * 8, 16)

    def render(self, text: str, _aa: bool, _color: object) -> str:
        return text


class _PropFont:
    """Uneven glyph widths so a recentered clock would visibly jump."""

    def size(self, text: str) -> tuple[int, int]:
        return (sum(4 if ch in "1: " else 9 for ch in text), 16)

    def render(self, text: str, _aa: bool, _color: object) -> str:
        return text


def _last_xy(blits: list, text: str) -> tuple[int, int] | None:
    pos = None
    for args in blits:
        if args and args[0] == text:
            pos = args[1]
    return pos


class SaverLoopbackTests(unittest.TestCase):
    def test_loopback_peers_allowed(self):
        for host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
            self.assertTrue(saver.peer_is_loopback(_request(host)), host)

    def test_lan_peer_rejected(self):
        self.assertFalse(saver.peer_is_loopback(_request("192.168.1.20")))
        self.assertFalse(saver.peer_is_loopback(_request("10.0.0.8")))

    def test_local_proxy_with_public_forwarded_rejected(self):
        self.assertFalse(saver.peer_is_loopback(_request("127.0.0.1", "203.0.113.9")))

    def test_missing_client_rejected(self):
        self.assertFalse(saver.peer_is_loopback(_request(None)))

    def test_require_loopback_raises(self):
        with self.assertRaises(HTTPException) as caught:
            saver.require_loopback(_request("8.8.8.8"))
        self.assertEqual(caught.exception.status_code, 403)


class SaverSanitizeTests(unittest.IsolatedAsyncioTestCase):
    def test_strips_prompts_users_and_job_text(self):
        raw = {
            "ok": True,
            "gpu_mode": "llm",
            "profile": "qwen",
            "busy": True,
            "switching": False,
            "restarting": False,
            "tokens": 12,
            "stage": "decode",
            "waiters": 2,
            "elapsed_s": 9,
            "user": "alice",
            "api_base": "https://example.invalid/v1",
            "job": {"prompt": "secret image prompt", "phase": "rendering"},
            "gpu": {
                "name": "RTX 4070 Ti",
                "memory_used_mib": 7100,
                "memory_total_mib": 12282,
                "utilization_pct": 82,
                "temperature_c": 64,
            },
            "host": {"cpu_pct": 12.3, "ram_pct": 40.0, "load1": 1.1},
            "stack_queue": {
                "busy": True,
                "kind": "chat",
                "occupant": "alice",
                "prompt": "how do I hack",
                "chat_id": "abc123",
                "hint": "alice is chatting.",
            },
        }
        payload = saver.sanitize_status(raw)
        blob = repr(payload)
        self.assertNotIn("alice", blob)
        self.assertNotIn("secret", blob)
        self.assertNotIn("abc123", blob)
        self.assertNotIn("example.invalid", blob)
        self.assertEqual(payload["gpu_mode"], "llm")
        self.assertEqual(payload["profile"], "qwen")
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["kind"], "chat")
        self.assertEqual(payload["gpu"]["utilization_pct"], 82)
        self.assertEqual(payload["gpu"]["vram_pct"], 58)
        self.assertEqual(payload["gpu"]["temperature_c"], 64)
        self.assertEqual(payload["host"]["cpu_pct"], 12.3)
        self.assertEqual(payload["tokens"], 12)
        self.assertEqual(payload["stage"], "decode")
        self.assertEqual(payload["waiters"], 2)
        self.assertEqual(payload["elapsed_s"], 9)
        self.assertIsNone(payload["typical_s"])
        self.assertEqual(payload["image_n"], None)
        self.assertEqual(payload["image_of"], None)
        self.assertEqual(payload["image_file"], "")
        self.assertEqual(payload["image_what"], "how do I hack")
        for key in ("occupant", "prompt", "chat_id", "user", "hint", "job", "stack_queue"):
            self.assertNotIn(key, payload)

    def test_unknown_kind_dropped(self):
        payload = saver.sanitize_status(
            {
                "gpu_mode": "llm",
                "stack_queue": {"kind": "admin-shell", "busy": True, "prompt": "nope"},
            }
        )
        self.assertIsNone(payload["kind"])
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["stage"], "idle")
        self.assertEqual(payload["tokens"], 0)

    def test_idle_defaults(self):
        payload = saver.sanitize_status({})
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["busy"])
        self.assertFalse(payload["switching"])
        self.assertFalse(payload["recovering"])
        self.assertIsNone(payload["kind"])
        self.assertIsNone(payload["gpu"]["vram_pct"])
        self.assertEqual(payload["stage"], "idle")
        self.assertEqual(payload["tokens"], 0)
        self.assertEqual(payload["waiters"], 0)
        self.assertIsNone(payload["image_n"])
        self.assertEqual(payload["image_file"], "")
        self.assertEqual(payload["image_what"], "")
        self.assertIsNone(payload["typical_s"])

    def test_typical_s_only_while_switching(self):
        idle = saver.sanitize_status({"gpu_mode": "llm", "stage": "decode"})
        self.assertIsNone(idle["typical_s"])
        payload = saver.sanitize_status(
            {
                "gpu_mode": "llm",
                "switching": True,
                "stage": "switch",
                "typical_s": 66,
            }
        )
        self.assertEqual(payload["typical_s"], 66)

    def test_unknown_stage_becomes_idle(self):
        payload = saver.sanitize_status({"stage": "secret-thoughts", "tokens": -3})
        self.assertEqual(payload["stage"], "idle")
        self.assertEqual(payload["tokens"], 0)

    def test_recover_stage_is_kept(self):
        payload = saver.sanitize_status(
            {
                "recovering": True,
                "stage": "recover",
                "image_what": "GPU ran out of memory. Resetting the generator.",
            }
        )
        self.assertTrue(payload["recovering"])
        self.assertEqual(payload["stage"], "recover")
        self.assertIn("GPU ran out of memory", payload["image_what"])

    def test_queue_live_without_busy_flag_is_still_busy(self):
        payload = saver.sanitize_status(
            {"gpu_mode": "llm", "stack_queue": {"busy": False, "live": True, "kind": "chat"}}
        )
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["kind"], "chat")

    async def test_saver_state_uses_empty_username(self):
        snap = mock.Mock(
            return_value={"busy": False, "kind": "gpu", "occupant": "bob", "live": False}
        )
        with (
            mock.patch("ui.occupancy.snapshot", snap),
            mock.patch("ui.manager.stack_status", new=mock.AsyncMock()) as status,
            mock.patch("ui.manager.cached_nvidia_stats", return_value={}),
            mock.patch("ui.manager.ensure_gpu_cache"),
            mock.patch("common.live_decode.snapshot", return_value={"tokens": 0, "stage": "idle"}),
            mock.patch("images.jobs.active_mcp_image_job", return_value=None),
            mock.patch("ui.flight.iter_live_flights", return_value=[]),
            mock.patch("common.phrase_switch.switch_lock_held", return_value=True),
            mock.patch("common.phrase_switch.switch_lock_name", return_value="comfy"),
            mock.patch("common.gpu_mode.read_mode", return_value={"mode": "comfy"}),
            mock.patch("images.jobs.loaded_tabby_name", return_value=None),
            mock.patch("common.phrase_switch.profile_alias_for_model", return_value=None),
            mock.patch("common.phrase_switch.last_llm_profile_name", return_value=""),
            mock.patch("select_model.last_profile", return_value="flux"),
        ):
            payload = await saver.saver_state()
        snap.assert_called_once_with("")
        status.assert_not_called()
        self.assertEqual(payload["kind"], "gpu")
        self.assertTrue(payload["switching"])
        self.assertEqual(payload["stage"], "switch")
        self.assertEqual(payload["switch_target"], "comfy")
        self.assertEqual(payload["profile"], "flux")
        self.assertEqual(payload["typical_s"], 37)
        self.assertIsNone(payload["gpu"]["utilization_pct"])
        self.assertNotIn("bob", repr(payload))

    async def test_saver_state_busy_from_generate_post_before_occupancy(self):
        snap = mock.Mock(return_value={"busy": False, "kind": None, "live": False})
        with (
            mock.patch("ui.occupancy.snapshot", snap),
            mock.patch("ui.manager.cached_nvidia_stats", return_value={}),
            mock.patch("ui.manager.ensure_gpu_cache"),
            mock.patch("common.live_decode.snapshot", return_value={"tokens": 0, "stage": "prefill"}),
            mock.patch("images.jobs.active_mcp_image_job", return_value=None),
            mock.patch("ui.flight.iter_live_flights", return_value=[]),
            mock.patch("common.phrase_switch.switch_lock_held", return_value=False),
            mock.patch("common.phrase_switch.switch_lock_name", return_value=""),
            mock.patch("common.gpu_mode.read_mode", return_value={"mode": "llm"}),
            mock.patch("images.jobs.loaded_tabby_name", return_value="Qwen"),
            mock.patch("common.phrase_switch.profile_alias_for_model", return_value="qwen"),
            mock.patch("common.phrase_switch.last_llm_profile_name", return_value="qwen"),
            mock.patch("select_model.last_profile", return_value="qwen"),
        ):
            payload = await saver.saver_state()
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["stage"], "prefill")
        self.assertEqual(payload["kind"], "chat")


class SaverKioskSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kiosk = _load_kiosk()

    def test_high_gpu_util_alone_is_still_idle(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "profile": "qwen",
                "busy": False,
                "kind": None,
                "gpu": {"utilization_pct": 41, "vram_pct": 70, "temperature_c": 55},
            },
            True,
        )
        self.assertEqual(scene["phase"], "idle")
        self.assertEqual(scene["palette"], "idle")
        self.assertFalse(scene["live"])

    def test_wall_clock_includes_seconds(self):
        stamp = time.mktime((2026, 9, 5, 14, 20, 7, 0, 0, -1))
        clock, date = self.kiosk.wall_clock_parts(stamp)
        self.assertEqual(clock, "14:20:07")
        self.assertIn("Sep", date)

    def test_hud_caption_title_cases_status_words(self):
        cap = self.kiosk.hud_caption
        self.assertEqual(cap("thinking"), "Thinking")
        self.assertEqual(cap("settling"), "Settling")
        self.assertEqual(cap("loading llm"), "Loading LLM")
        self.assertEqual(cap("restarting api"), "Restarting API")
        self.assertEqual(cap("resetting generator"), "Resetting Generator")

    def test_overlay_live_file_makes_idle_http_live(self):
        idle = {"gpu_mode": "llm", "profile": "qwen", "busy": False, "stage": "idle"}
        live = {"busy": True, "stage": "prefill", "tokens": 0}
        merged = self.kiosk.overlay_saver_live(idle, live)
        scene = self.kiosk.scene_from_state(merged, True)
        self.assertTrue(scene["live"])
        self.assertEqual(scene["phase"], "thinking")
        self.assertEqual(scene["palette"], "chat")

    def test_overlay_live_file_without_http_payload(self):
        merged = self.kiosk.overlay_saver_live(None, {"busy": True, "stage": "prefill"})
        scene = self.kiosk.scene_from_state(merged, True)
        self.assertTrue(scene["live"])
        self.assertEqual(scene["phase"], "thinking")

    def test_kind_without_a_job_is_idle(self):
        scene = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": False},
            True,
        )
        self.assertEqual(scene["phase"], "idle")
        self.assertFalse(scene["live"])

    def test_idle_uses_idle_palette_but_keeps_moving(self):
        scene = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False, "gpu": {"utilization_pct": 3}},
            True,
        )
        self.assertEqual(scene["phase"], "idle")
        self.assertEqual(scene["palette"], "idle")
        self.assertFalse(scene["live"])
        self.assertGreaterEqual(scene["speed"], 0.30)
        self.assertLess(scene["speed"], 0.55)
        self.assertGreaterEqual(scene["intensity"], 0.38)

    def test_hud_idle_shows_clock_and_profile(self):
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        idle["clock"] = "14:20:07"
        idle["date"] = "Sat 5 Sep"
        idle["idle_fact"] = "this box is a RTX 4070 Ti 12 GB"
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            True,
        )
        idle_screen = _FakeScreen()
        hot_screen = _FakeScreen()
        font = _FakeFont()
        self.kiosk.draw_hud(idle_screen, font, font, idle)
        self.kiosk.draw_hud(hot_screen, font, font, hot)
        idle_text = " ".join(str(item) for item in idle_screen.blits)
        hot_text = " ".join(str(item) for item in hot_screen.blits)
        self.assertGreater(len(idle_screen.blits), 0)
        self.assertIn("qwen", idle_text)
        self.assertIn("14:20:07", idle_text)
        self.assertIn("Sat 5 Sep", idle_text)
        self.assertIn("This Box Is A RTX 4070 Ti 12 GB", idle_text)
        self.assertNotIn("thinking", idle_text)
        self.assertGreater(len(hot_screen.blits), 0)
        self.assertIn("Thinking", hot_text)

    def test_hud_clock_and_stats_keep_anchor(self):
        font = _PropFont()
        idle_narrow = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "profile": "qwen",
                "busy": False,
                "gpu": {"utilization_pct": 3, "vram_pct": 5, "temperature_c": 41},
            },
            True,
        )
        idle_wide = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "profile": "qwen",
                "busy": False,
                "gpu": {"utilization_pct": 99, "vram_pct": 100, "temperature_c": 99},
            },
            True,
        )
        idle_narrow["clock"] = "11:11:11"
        idle_narrow["date"] = "Sat  5 Sep"
        idle_wide["clock"] = "08:08:08"
        idle_wide["date"] = "Sat 15 Sep"
        a = _FakeScreen()
        b = _FakeScreen()
        self.kiosk.draw_hud(a, font, font, idle_narrow)
        self.kiosk.draw_hud(b, font, font, idle_wide)
        self.assertEqual(_last_xy(a.blits, "11:11:11"), _last_xy(b.blits, "08:08:08"))
        self.assertEqual(_last_xy(a.blits, "Sat  5 Sep"), _last_xy(b.blits, "Sat 15 Sep"))
        self.assertEqual(_last_xy(a.blits, "VRAM   5%    41°C"), _last_xy(b.blits, "VRAM 100%    99°C"))
        hot_n = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "profile": "qwen",
                "gpu": {"utilization_pct": 3, "vram_pct": 5, "temperature_c": 41},
            },
            True,
        )
        hot_w = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "profile": "qwen",
                "gpu": {"utilization_pct": 99, "vram_pct": 100, "temperature_c": 99},
            },
            True,
        )
        hot_n["clock"] = "11:11:11"
        hot_w["clock"] = "08:08:08"
        ha = _FakeScreen()
        hb = _FakeScreen()
        self.kiosk.draw_hud(ha, font, font, hot_n)
        self.kiosk.draw_hud(hb, font, font, hot_w)
        self.assertEqual(_last_xy(ha.blits, "11:11:11"), _last_xy(hb.blits, "08:08:08"))
        self.assertEqual(
            _last_xy(ha.blits, "GPU   3%   VRAM   5%    41°C"),
            _last_xy(hb.blits, "GPU  99%   VRAM 100%    99°C"),
        )

    def test_idle_hud_alpha_holds_then_fades(self):
        alpha = self.kiosk.idle_hud_alpha
        self.assertEqual(alpha(0.0), 1.0)
        self.assertEqual(alpha(299.0), 1.0)
        self.assertAlmostEqual(alpha(306.0), 0.5)
        self.assertEqual(alpha(312.0), 0.0)
        self.assertEqual(alpha(400.0), 0.0)
        self.assertEqual(alpha(0.0, hold_s=0), 0.0)
        self.assertEqual(alpha(12.0, hold_s=0), 0.0)
        self.assertEqual(alpha(5.0, hold_s=60), 1.0)
        self.assertEqual(alpha(72.0, hold_s=60), 0.0)

    def test_hud_idle_hidden_after_fade_draws_nothing(self):
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        idle["clock"] = "14:20:07"
        idle["date"] = "Sat 5 Sep"
        idle["idle_fact"] = "this box is a RTX 4070 Ti 12 GB"
        idle["hud_alpha"] = 0.0
        screen = _FakeScreen()
        font = _FakeFont()
        self.kiosk.draw_hud(screen, font, font, idle)
        self.assertEqual(len(screen.blits), 0)

    def test_hud_mid_fade_still_draws_clock(self):
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        idle["clock"] = "14:20:07"
        idle["date"] = "Sat 5 Sep"
        idle["hud_alpha"] = 0.5
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), idle)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("14:20:07", text)

    def test_wake_idle_hud_restores_alpha(self):
        follow = self.kiosk.SceneFollow()
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        shown = follow.tick(idle, 0.04, 10.0)
        self.assertEqual(shown["hud_alpha"], 1.0)
        hidden = follow.tick(idle, 0.04, 10.0 + 320.0)
        self.assertEqual(hidden["hud_alpha"], 0.0)
        follow.wake_idle_hud(10.0 + 320.0)
        woken = follow.tick(idle, 0.04, 10.0 + 320.04)
        self.assertEqual(woken["hud_alpha"], 1.0)

    def test_hud_timeout_zero_stays_hidden(self):
        follow = self.kiosk.SceneFollow(hud_hold_s=0)
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        shown = follow.tick(idle, 0.04, 10.0)
        self.assertEqual(shown["hud_alpha"], 0.0)
        follow.wake_idle_hud(10.0)
        woken = follow.tick(idle, 0.04, 10.04)
        self.assertEqual(woken["hud_alpha"], 0.0)

    def test_hud_only_when_live(self):
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            True,
        )
        idle_screen = _FakeScreen()
        hot_screen = _FakeScreen()
        font = _FakeFont()
        self.kiosk.draw_hud(idle_screen, font, font, idle)
        self.kiosk.draw_hud(hot_screen, font, font, hot)
        self.assertGreater(len(idle_screen.blits), 0)
        self.assertGreater(len(hot_screen.blits), 0)

    def test_neurons_only_fire_when_live(self):
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": False},
            True,
        )
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            True,
        )
        self.assertIsNone(self.kiosk.neuron_overlay_state(idle))
        overlay = self.kiosk.neuron_overlay_state(hot)
        assert overlay is not None
        self.assertGreater(len(overlay["nodes"]), 20)
        self.assertGreater(len(overlay["edges"]), 20)
        self.assertGreater(len(overlay["pulses"]), 10)
        self.assertGreater(sum(overlay["fires"]), 0.5)
        xs = [p[0] for p in overlay["nodes"]]
        ys = [p[1] for p in overlay["nodes"]]
        self.assertLess(min(xs), 0.05)
        self.assertGreater(max(xs), 0.95)
        self.assertLess(min(ys), 0.05)
        self.assertGreater(max(ys), 0.95)
        ring, head = self.kiosk.neuron_draw_sizes(1.0, 1.0, 1080)
        self.assertLessEqual(ring, 16)
        self.assertLessEqual(head, 8)
        self.assertGreaterEqual(ring, 3)
        fading = dict(hot)
        fading["live"] = False
        fading["overlay"] = 0.4
        self.assertIsNotNone(self.kiosk.neuron_overlay_state(fading))
        gone = dict(hot)
        gone["live"] = False
        gone["overlay"] = 0.0
        self.assertIsNone(self.kiosk.neuron_overlay_state(gone))
        boot = dict(hot)
        boot["cycle"] = "boot"
        boot["cycle_t"] = 0.0
        boot["overlay"] = 0.08
        boot_state = self.kiosk.neuron_overlay_state(boot)
        self.assertIsNotNone(boot_state)
        halt = dict(hot)
        halt["live"] = False
        halt["cycle"] = "halt"
        halt["cycle_t"] = 0.2
        halt["overlay"] = 0.7
        self.assertIsNotNone(self.kiosk.neuron_overlay_state(halt))

    def test_neuron_pulses_travel_one_way(self):
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            True,
        )
        hot["st"] = 1.0
        first = self.kiosk.neuron_overlay_state(hot)
        hot["st"] = 1.05
        second = self.kiosk.neuron_overlay_state(hot)
        assert first is not None and second is not None
        u0 = first["pulses"][0][1]
        u1 = second["pulses"][0][1]
        self.assertNotEqual(u0, u1)

    def test_chat_busy_is_thinking_and_hot(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "gpu": {"utilization_pct": 0, "vram_pct": 70, "temperature_c": 48},
            },
            True,
        )
        self.assertEqual(scene["phase"], "thinking")
        self.assertEqual(scene["palette"], "chat")
        self.assertTrue(scene["live"])
        self.assertGreaterEqual(scene["intensity"], 0.50)
        self.assertGreater(scene["speed"], 0.5)

    def test_code_occupancy_while_decoding_is_thinking(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "code",
                "busy": True,
                "stage": "decode",
                "gpu": {"utilization_pct": 0, "vram_pct": 70, "temperature_c": 48},
            },
            True,
        )
        self.assertEqual(scene["phase"], "thinking")
        self.assertEqual(scene["palette"], "chat")
        tools = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "code", "busy": True, "stage": "tool"},
            True,
        )
        self.assertEqual(tools["phase"], "using tools")

    def test_prefill_stage_is_live_without_busy_flag(self):
        scene = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "stage": "prefill", "busy": False, "kind": "chat"},
            True,
        )
        self.assertTrue(scene["live"])
        self.assertEqual(scene["phase"], "thinking")
        self.assertEqual(scene["palette"], "chat")

    def test_comfy_and_switch_palettes(self):
        image = self.kiosk.scene_from_state(
            {"gpu_mode": "comfy", "kind": "image", "busy": True}, True
        )
        switch = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "switching": True, "kind": "gpu"}, True
        )
        waiting = self.kiosk.scene_from_state(None, False)
        self.assertEqual(image["palette"], "image")
        self.assertEqual(switch["palette"], "switch")
        self.assertEqual(waiting["phase"], "waiting for api")
        self.assertEqual(waiting["palette"], "down")
        self.assertTrue(waiting["live"])
        self.assertIn("1-2 min", waiting["note"])
        self.assertIn("reboot", waiting["note"])
        self.assertIn("fresh install", waiting["note"])

    def test_recovering_from_oom_is_live_hud(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "profile": "qwen35",
                "recovering": True,
                "stage": "recover",
                "image_what": "GPU ran out of memory. Resetting the generator.",
            },
            True,
        )
        self.assertEqual(scene["phase"], "resetting generator")
        self.assertEqual(scene["palette"], "switch")
        self.assertTrue(scene["live"])
        self.assertEqual(
            scene["image_what"],
            "GPU ran out of memory. Resetting the generator.",
        )
        self.assertFalse(self.kiosk.idle_hud_quiet(scene))
        screen = _FakeScreen()
        scene = dict(scene)
        scene["clock"] = "02:07:02"
        scene["connected"] = True
        scene["has_gpu"] = True
        scene["vram"] = 96
        scene["temp"] = 35
        scene["util"] = 40
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), scene)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("Resetting Generator", text)
        self.assertIn("GPU ran out of memory", text)

    def test_waiting_hud_mentions_reboot_ready_time(self):
        waiting = self.kiosk.scene_from_state(None, False)
        waiting = dict(waiting)
        waiting["clock"] = "14:20:07"
        waiting["date"] = "Sat 5 Sep"
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), waiting)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("Waiting For API", text)
        self.assertIn("1-2 Min", text)
        self.assertIn("Reboot", text)
        self.assertIn("Fresh Install", text)

    def test_disconnect_after_live_is_restarting_api(self):
        scene = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            False,
        )
        self.assertEqual(scene["phase"], "restarting api")
        self.assertEqual(scene["palette"], "down")
        self.assertTrue(scene["live"])
        self.assertIn("health", scene["note"])

    def test_restarting_flag_uses_down_palette(self):
        scene = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "restarting": True, "busy": True},
            True,
        )
        self.assertEqual(scene["phase"], "restarting api")
        self.assertEqual(scene["palette"], "down")
        self.assertEqual(scene["note"], "reloading python / weights")

    def test_saver_url_joins_origin(self):
        self.assertEqual(
            self.kiosk.saver_url("http://127.0.0.1:5000/"),
            "http://127.0.0.1:5000/v1/ui/saver/state",
        )
        self.assertEqual(
            self.kiosk.origin_peer("http://127.0.0.1:5000/v1/ui/saver/state"),
            ("127.0.0.1", 5000),
        )

    def test_ingest_timeout_keeps_last_but_closed_port_is_down(self):
        bus = self.kiosk.StateBus()
        live = {"gpu_mode": "llm", "busy": True, "kind": "chat", "stage": "decode"}
        bus.ingest(live, True)
        self.assertTrue(bus.snapshot()[1])
        bus.ingest(None, True)
        data, ok = bus.snapshot()
        self.assertTrue(ok)
        self.assertEqual(data["stage"], "decode")
        bus.ingest(None, False)
        data, ok = bus.snapshot()
        self.assertFalse(ok)
        self.assertEqual(data["stage"], "decode")

    def test_follow_keeps_plasma_phase_when_speed_jumps(self):
        follow = self.kiosk.SceneFollow()
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "busy": False, "gpu": {"utilization_pct": 2}},
            True,
        )
        hot = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "gpu": {"utilization_pct": 90},
            },
            True,
        )
        a = follow.tick(idle, 0.04, 10.0)
        b = follow.tick(hot, 0.04, 10.04)
        self.assertGreaterEqual(b["st"], a["st"])
        self.assertLess(b["st"] - a["st"], 0.05)
        self.assertLess(abs(b["intensity"] - a["intensity"]), 0.08)
        self.assertEqual(b["cycle"], "boot")
        self.assertEqual(b["phase"], "stirring")
        self.assertGreaterEqual(b["overlay"], 0.85)
        self.assertIsNotNone(self.kiosk.neuron_overlay_state(b))

    def test_follow_holds_live_through_a_brief_idle_poll(self):
        follow = self.kiosk.SceneFollow()
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "gpu": {"utilization_pct": 80}},
            True,
        )
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "busy": False, "gpu": {"utilization_pct": 0}},
            True,
        )
        follow.tick(hot, 0.04, 1.0)
        held = follow.tick(idle, 0.04, 1.5)
        self.assertTrue(held["live"])
        later = follow.tick(idle, 0.04, 8.0)
        self.assertFalse(later["live"])
        self.assertIn(later["cycle"], ("halt", "idle"))

    def test_follow_reaches_hot_intensity_while_thinking(self):
        follow = self.kiosk.SceneFollow()
        hot = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "gpu": {"utilization_pct": 5},
            },
            True,
        )
        scene = None
        for step in range(40):
            scene = follow.tick(hot, 0.04, 1.0 + step * 0.04)
        self.assertEqual(scene["phase"], "thinking")
        self.assertGreater(scene["intensity"], 0.48)
        self.assertGreater(scene["weights"]["chat"], 0.8)

    def test_resume_on_idle_or_logout(self):
        resume = self.kiosk.should_resume_saver
        self.assertFalse(
            resume(
                now=10.0,
                last_input=9.0,
                idle_s=120.0,
                logout_idle_s=10.0,
                logged_in=False,
            )
        )
        self.assertTrue(
            resume(
                now=20.0,
                last_input=9.0,
                idle_s=120.0,
                logout_idle_s=10.0,
                logged_in=False,
            )
        )
        self.assertFalse(
            resume(
                now=11.0,
                last_input=10.5,
                idle_s=120.0,
                logout_idle_s=10.0,
                logged_in=False,
            )
        )
        self.assertFalse(
            resume(
                now=11.0,
                last_input=10.5,
                idle_s=120.0,
                logout_idle_s=10.0,
                logged_in=True,
            )
        )
        self.assertTrue(
            resume(
                now=140.0,
                last_input=10.5,
                idle_s=120.0,
                logout_idle_s=10.0,
                logged_in=True,
            )
        )

    def test_follow_fades_neuron_overlay(self):
        follow = self.kiosk.SceneFollow()
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True},
            True,
        )
        idle = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "busy": False},
            True,
        )
        now = 10.0
        scene = follow.tick(hot, 0.04, now)
        for _step in range(80):
            now += 0.04
            scene = follow.tick(hot, 0.04, now)
        self.assertGreater(scene["overlay"], 0.9)
        after = now
        for _step in range(20):
            after += 0.04
            scene = follow.tick(idle, 0.04, after)
        self.assertEqual(scene["cycle"], "halt")
        self.assertEqual(scene["phase"], "settling")
        after += 6.0
        scene = follow.tick(idle, 0.04, after)
        for _step in range(90):
            after += 0.04
            scene = follow.tick(idle, 0.04, after)
        self.assertLess(scene["overlay"], 0.05)
        self.assertEqual(scene["cycle"], "idle")

    def test_login_from_ps_ignores_getty(self):
        self.assertFalse(self.kiosk.login_from_ps("agetty\nlogin\n"))
        self.assertTrue(self.kiosk.login_from_ps("agetty\nbash\n"))

    def test_tty_nr_and_evdev(self):
        self.assertEqual(self.kiosk.tty_nr("tty8"), 8)
        self.assertEqual(self.kiosk.tty_nr("/dev/tty1"), 1)
        self.assertTrue(self.kiosk.evdev_is_activity(self.kiosk.EV_KEY))
        self.assertFalse(self.kiosk.evdev_is_activity(0))

    def test_kiosk_key_dismisses_window_esc_quits(self):
        pygame = SimpleNamespace(
            QUIT=256,
            KEYDOWN=768,
            KEYUP=769,
            MOUSEMOTION=1024,
            K_ESCAPE=27,
            K_q=113,
        )
        key = SimpleNamespace(type=768, key=97)
        esc = SimpleNamespace(type=768, key=27)
        mouse = SimpleNamespace(type=1024, key=0)
        self.assertEqual(self.kiosk.is_dismiss_event(key, pygame, False), "dismiss")
        self.assertEqual(self.kiosk.is_dismiss_event(mouse, pygame, False), "dismiss")
        self.assertEqual(self.kiosk.is_dismiss_event(esc, pygame, True), "quit")
        self.assertIsNone(self.kiosk.is_dismiss_event(key, pygame, True))

    def test_field_input_peeks_mouse_only_when_idle_hud_hidden(self):
        pygame = SimpleNamespace(
            QUIT=256,
            KEYDOWN=768,
            KEYUP=769,
            MOUSEMOTION=1024,
            MOUSEBUTTONDOWN=1025,
            K_ESCAPE=27,
            K_q=113,
        )
        key = SimpleNamespace(type=768, key=97)
        mouse = SimpleNamespace(type=1024, key=0)
        click = SimpleNamespace(type=1025, key=0)
        action = self.kiosk.field_input_action
        self.assertEqual(
            action(mouse, pygame, False, idle_quiet=True, hud_alpha=1.0),
            "dismiss",
        )
        self.assertEqual(
            action(mouse, pygame, False, idle_quiet=True, hud_alpha=0.5),
            "dismiss",
        )
        self.assertEqual(
            action(mouse, pygame, False, idle_quiet=True, hud_alpha=0.0),
            "peek",
        )
        self.assertEqual(
            action(key, pygame, False, idle_quiet=True, hud_alpha=0.0),
            "dismiss",
        )
        self.assertEqual(
            action(click, pygame, False, idle_quiet=True, hud_alpha=0.0),
            "dismiss",
        )
        self.assertEqual(
            action(mouse, pygame, False, idle_quiet=True, hud_alpha=0.0, hud_hold_s=0),
            "dismiss",
        )

    def test_peek_grace_covers_one_second_of_motion(self):
        self.assertEqual(self.kiosk.apply_peek_grace(0.0, 10.0), 11.0)
        self.assertEqual(self.kiosk.apply_peek_grace(20.0, 10.0), 20.0)

    def test_parse_args_idle_default_is_two_minutes(self):
        args = self.kiosk.parse_args([])
        self.assertEqual(args.idle, 120.0)
        self.assertEqual(args.logout_idle, 10.0)
        self.assertEqual(args.hud_idle, 300.0)
        self.assertEqual(args.poll, 0.1)
        self.assertEqual(args.width, 480)
        self.assertEqual(args.height, 270)
        args = self.kiosk.parse_args(
            ["--idle", "120", "--logout-idle", "10", "--user-tty", "tty1", "--saver-tty", "tty8"]
        )
        self.assertEqual(args.idle, 120.0)
        self.assertEqual(args.logout_idle, 10.0)
        self.assertEqual(args.user_tty, "tty1")
        self.assertEqual(args.saver_tty, "tty8")

    def test_hud_omits_zeros_when_gpu_cache_miss(self):
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            True,
        )
        self.assertFalse(hot["has_gpu"])
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), hot)
        text = " ".join(str(item) for item in screen.blits)
        self.assertNotIn("GPU 0%", text)
        self.assertNotIn("VRAM 0%", text)
        self.assertIn("Thinking", text)

    def test_hud_type_is_large_with_a_halo(self):
        large, small, info = self.kiosk.hud_font_sizes(1080)
        self.assertGreaterEqual(large, 60)
        self.assertGreaterEqual(small, 26)
        self.assertGreaterEqual(info, 20)
        self.assertGreater(large, small)
        self.assertGreater(small, info)
        halo = self.kiosk.hud_halo_offsets(3)
        self.assertGreaterEqual(len(halo), 8)
        self.assertNotIn((0, 0), halo)
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            True,
        )
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), hot)
        phase_blits = sum(1 for item in screen.blits if item and item[0] == "Thinking")
        self.assertGreaterEqual(phase_blits, 9)

    def test_hud_shows_image_file_and_what(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "comfy",
                "kind": "image",
                "busy": True,
                "image_file": "images/logo.png",
                "image_what": "a cafe logo",
                "image_n": 1,
                "image_of": 2,
            },
            True,
        )
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), scene)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("images/logo.png", text)
        self.assertIn("a cafe logo", text)
        self.assertIn("1/2", text)

    def test_hud_shows_chat_prompt(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "profile": "qwen",
                "stage": "decode",
                "image_what": "explain the occupancy queue",
            },
            True,
        )
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), scene)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("explain the occupancy queue", text)
        self.assertIn("Thinking", text)

    def test_hud_wraps_long_task(self):
        class WideFont(_FakeFont):
            def size(self, text: str) -> tuple[int, int]:
                return (len(text) * 20, 16)

        lines = self.kiosk._hud_wrap(
            WideFont(),
            "one two three four five six seven eight nine ten",
            80,
            4,
        )
        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(len(lines), 4)

    def test_hud_restarting_shows_clock_and_api_down(self):
        scene = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "profile": "qwen", "busy": True},
            False,
        )
        scene["runtime"] = "0:08"
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), scene)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("Restarting API", text)
        self.assertIn("0:08", text)
        self.assertIn("API Down", text)
        self.assertIn("Waiting For /health", text)

    def test_hud_shows_waiters_and_toks(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "profile": "qwen",
                "stage": "decode",
                "tokens": 1842,
                "waiters": 2,
            },
            True,
        )
        scene["token_rate"] = 12.0
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), scene)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("1842 tok", text)
        self.assertIn("12/s", text)
        self.assertIn("2 Waiting", text)

    def test_hud_shows_typical_switch(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "profile": "qwen",
                "busy": True,
                "switching": True,
                "stage": "switch",
                "switch_target": "llm",
                "typical_s": 66,
                "elapsed_s": 42,
            },
            True,
        )
        scene["runtime"] = "0:42"
        screen = _FakeScreen()
        self.kiosk.draw_hud(screen, _FakeFont(), _FakeFont(), scene)
        text = " ".join(str(item) for item in screen.blits)
        self.assertIn("Loading LLM", text)
        self.assertIn("0:42", text)
        self.assertIn("~1:06 Typical", text)

    def test_idle_facts_from_switch_times(self):
        facts = self.kiosk.idle_fact_lines(
            {
                "gpu": "RTX 4070 Ti 12 GB",
                "qwen": {"ready_s": 66},
                "comfy": {"flux_s": 202, "qwen_image_s": 230},
            },
            125.0,
        )
        self.assertIn("this box is a RTX 4070 Ti 12 GB", facts)
        self.assertIn("qwen warm switch ~66s", facts)
        self.assertIn("flux first picture ~3 min", facts)
        self.assertIn("asleep  2m", facts)
        self.assertEqual(
            self.kiosk.pick_idle_fact(["a", "b", "c"], 0.0),
            "a",
        )
        self.assertEqual(self.kiosk.pick_idle_fact(["a", "b", "c"], 45.0), "b")

    def test_idle_tod_hue_dusk_is_warm(self):
        self.assertLess(self.kiosk.idle_tod_hue(18.5), 0.0)
        self.assertGreater(self.kiosk.idle_tod_hue(1.0), 0.0)
        self.assertEqual(self.kiosk.idle_tod_hue(12.0), 0.0)

    def test_follow_prefers_occupancy_elapsed(self):
        follow = self.kiosk.SceneFollow()
        hot = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "elapsed_s": 40,
            },
            True,
        )
        scene = follow.tick(hot, 0.04, 10.0)
        for _step in range(40):
            scene = follow.tick(hot, 0.04, 10.0 + 0.04 * (_step + 1))
        self.assertEqual(scene["phase"], "thinking")
        self.assertEqual(scene["runtime"], "0:40")

    def test_follow_snaps_to_down_when_api_drops(self):
        follow = self.kiosk.SceneFollow()
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True},
            True,
        )
        follow.tick(hot, 0.04, 1.0)
        gone = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True, "profile": "qwen"},
            False,
        )
        scene = follow.tick(gone, 0.04, 1.5)
        self.assertEqual(scene["phase"], "restarting api")
        self.assertEqual(scene["palette"], "down")
        self.assertEqual(scene["weights"]["down"], 1.0)

    def test_follow_runtime_advances_on_a_task(self):
        follow = self.kiosk.SceneFollow()
        hot = self.kiosk.scene_from_state(
            {"gpu_mode": "llm", "kind": "chat", "busy": True},
            True,
        )
        now = 10.0
        scene = follow.tick(hot, 0.04, now)
        for _step in range(50):
            now += 0.04
            scene = follow.tick(hot, 0.04, now)
        self.assertEqual(scene["phase"], "thinking")
        self.assertGreater(scene["runtime_s"], 0.4)
        self.assertRegex(scene["runtime"], r"^\d+:\d{2}$")

    def test_fmt_runtime(self):
        self.assertEqual(self.kiosk._fmt_runtime(0), "0:00")
        self.assertEqual(self.kiosk._fmt_runtime(75), "1:15")
        self.assertEqual(self.kiosk._fmt_runtime(3661), "1:01:01")

    def test_decode_token_ticks_raise_fire(self):
        quiet = self.kiosk.scene_from_state(
            {
                "gpu_mode": "llm",
                "kind": "chat",
                "busy": True,
                "stage": "decode",
                "tokens": 0,
            },
            True,
        )
        quiet["st"] = 1.0
        quiet["token_rate"] = 0.0
        quiet["tokens"] = 0
        quiet["stage"] = "decode"
        loud = dict(quiet)
        loud["tokens"] = 48
        loud["token_rate"] = 24.0
        a = self.kiosk.neuron_overlay_state(quiet)
        b = self.kiosk.neuron_overlay_state(loud)
        assert a is not None and b is not None
        self.assertGreater(sum(b["fires"]), sum(a["fires"]))

    def test_image_restore_is_loading_llm(self):
        scene = self.kiosk.scene_from_state(
            {
                "gpu_mode": "comfy",
                "kind": "image",
                "busy": True,
                "switching": True,
                "stage": "switch",
                "switch_target": "llm",
            },
            True,
        )
        self.assertEqual(scene["phase"], "loading llm")
        self.assertEqual(scene["palette"], "switch")


class SaverComposeTests(unittest.TestCase):
    def test_decode_snapshot_wins_for_chat(self):
        weather = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": True, "kind": "chat", "waiters": 1, "elapsed_s": 4},
            decode={"tokens": 20, "stage": "decode"},
            job=None,
            flights=[],
        )
        self.assertEqual(weather["stage"], "decode")
        self.assertEqual(weather["tokens"], 20)
        self.assertEqual(weather["waiters"], 1)
        self.assertEqual(weather["elapsed_s"], 4)

    def test_decode_prefill_without_occupancy_is_still_thinking(self):
        weather = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": False, "kind": None},
            decode={"tokens": 0, "stage": "prefill"},
            job=None,
            flights=[],
        )
        self.assertEqual(weather["stage"], "prefill")
        self.assertEqual(weather["kind"], "chat")

    def test_flight_chars_when_decode_idle(self):
        flight = SimpleNamespace(
            done=False, assembled="hello world", reasoning="", kind="chat", steps=[]
        )
        weather = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": True, "kind": "chat"},
            decode={"tokens": 0, "stage": "idle"},
            job=None,
            flights=[flight],
        )
        self.assertEqual(weather["stage"], "decode")
        self.assertEqual(weather["tokens"], len("hello world"))
        self.assertEqual(weather["image_what"], "")

    def test_chat_prompt_from_queue_and_flight(self):
        from_queue = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": True, "kind": "chat", "prompt": "explain the occupancy queue"},
            decode={"tokens": 4, "stage": "decode"},
            job=None,
            flights=[],
        )
        self.assertEqual(from_queue["image_what"], "explain the occupancy queue")
        flight = SimpleNamespace(
            done=False,
            assembled="x",
            reasoning="",
            kind="chat",
            steps=[],
            prompt="rewrite the screensaver hud",
        )
        from_flight = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": True, "kind": "code"},
            decode={"tokens": 0, "stage": "idle"},
            job=None,
            flights=[flight],
        )
        self.assertEqual(from_flight["image_what"], "rewrite the screensaver hud")

    def test_tool_step_without_result(self):
        flight = SimpleNamespace(
            done=False,
            assembled="x",
            reasoning="",
            kind="code",
            steps=[{"type": "tool", "name": "Read"}],
        )
        weather = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": True, "kind": "code"},
            decode={"tokens": 0, "stage": "idle"},
            job=None,
            flights=[flight],
        )
        self.assertEqual(weather["stage"], "tool")
        self.assertEqual(weather["tokens"], 1)

    def test_image_job_progress(self):
        item = SimpleNamespace(prompt="qwen-image: a cafe logo", output_path="images/logo.png")
        job = SimpleNamespace(
            status="running",
            phase="generating",
            count=3,
            current_index=1,
            items=[item, item, item],
        )
        weather = saver._compose_weather(
            switching=False,
            restarting=False,
            queue={"busy": True, "kind": "image"},
            decode={"tokens": 0, "stage": "idle"},
            job=job,
            flights=[],
        )
        self.assertEqual(weather["stage"], "image")
        self.assertEqual(weather["image_n"], 2)
        self.assertEqual(weather["image_of"], 3)
        self.assertEqual(weather["image_file"], "images/logo.png")
        self.assertEqual(weather["image_what"], "qwen-image: a cafe logo")


class LiveDecodeTests(unittest.TestCase):
    def setUp(self):
        live_decode.reset_for_tests()

    def tearDown(self):
        live_decode.reset_for_tests()

    def test_prefill_then_decode_then_clear(self):
        live_decode.note_prefill("a")
        self.assertEqual(live_decode.snapshot()["stage"], "prefill")
        live_decode.note_decode("a", 7)
        self.assertEqual(live_decode.snapshot(), {"busy": True, "tokens": 7, "stage": "decode"})
        live_decode.clear("b")
        self.assertEqual(live_decode.snapshot()["tokens"], 7)
        live_decode.clear("a")
        self.assertEqual(live_decode.snapshot()["stage"], "idle")
        self.assertEqual(live_decode.snapshot()["tokens"], 0)

    def test_generate_post_paths(self):
        self.assertTrue(live_decode.is_generate_post("POST", "/v1/chat/completions"))
        self.assertTrue(live_decode.is_generate_post("POST", "/v1/completions"))
        self.assertTrue(live_decode.is_generate_post("POST", "/v1/ui/chat"))
        self.assertFalse(live_decode.is_generate_post("GET", "/v1/chat/completions"))
        self.assertFalse(live_decode.is_generate_post("POST", "/v1/ui/saver/state"))
        self.assertFalse(live_decode.is_generate_post("POST", "/v1/models"))

    def test_http_hold_shows_prefill_before_generate(self):
        live_decode.hold("http:1")
        self.assertEqual(live_decode.snapshot()["stage"], "prefill")
        live_decode.note_prefill("job")
        live_decode.clear("job")
        self.assertEqual(live_decode.snapshot()["stage"], "prefill")
        live_decode.release("http:1")
        self.assertEqual(live_decode.snapshot()["stage"], "idle")

    def test_overlay_live_file_marks_idle_http_busy(self):
        idle = {"busy": False, "stage": "idle", "kind": None, "profile": "qwen"}
        live = {"busy": True, "stage": "prefill", "tokens": 0}
        out = live_decode.overlay_live_file(idle, live)
        self.assertTrue(out["busy"])
        self.assertEqual(out["stage"], "prefill")
        self.assertEqual(out["kind"], "chat")
        self.assertEqual(out["profile"], "qwen")

    def test_hold_writes_live_sidecar(self):
        live_decode.hold("http:1")
        data = live_decode.read_live_file()
        self.assertIsNotNone(data)
        self.assertTrue(data["busy"])
        self.assertEqual(data["stage"], "prefill")
