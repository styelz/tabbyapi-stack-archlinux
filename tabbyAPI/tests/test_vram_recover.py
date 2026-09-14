import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

from common import vram_recover
from common.health import HealthManager


class VramRecoverTests(unittest.TestCase):
    def tearDown(self):
        vram_recover.clear_notice()

    def test_allocation_on_device_is_vram(self):
        self.assertTrue(vram_recover.is_vram_error("torch.OutOfMemoryError: Allocation on device"))
        self.assertTrue(vram_recover.is_vram_error(AssertionError("Cannot create new state: no available slots")))
        self.assertFalse(vram_recover.is_vram_error("syntax error"))

    def test_reset_recurrent_slots_refills_pool(self):
        cache = SimpleNamespace(num_slots=4, free_list=deque())
        vram_recover.reset_recurrent_slots(cache)
        self.assertEqual(list(cache.free_list), [0, 1, 2, 3])

    def test_notice_roundtrip(self):
        vram_recover.set_notice("resetting generator", "GPU ran out of memory")
        self.assertEqual(vram_recover.current_notice()["phase"], "resetting generator")
        self.assertIn("GPU", vram_recover.current_notice()["detail"])
        vram_recover.clear_notice()
        self.assertEqual(vram_recover.current_notice(), {})

    def test_vram_abort_message_includes_cause_and_switch_hint(self):
        with (
            mock.patch.object(
                vram_recover,
                "_gpu_memory_line",
                return_value="RTX 4070 Ti (11809 / 12282 MiB used, 62 MiB free)",
            ),
            mock.patch.object(
                vram_recover,
                "_loaded_model_bits",
                return_value=("Qwen3.8-27B-exl3-SC_3.00bpw_H4_V4", "qwen36"),
            ),
        ):
            msg = vram_recover.generation_abort_message(
                RuntimeError("Allocation on device")
            )
        self.assertIn("Chat completion aborted", msg)
        self.assertIn("out of memory", msg.lower())
        self.assertIn("Allocation on device", msg)
        self.assertIn("Qwen3.8-27B-exl3-SC_3.00bpw_H4_V4", msg)
        self.assertIn("qwen36", msg)
        self.assertIn("11809", msg)
        self.assertIn("tool results", msg)
        self.assertIn("switch to qwen", msg)
        self.assertIn("generator was reset", msg.lower())
        self.assertNotIn("server console", msg.lower())

    def test_vram_abort_on_qwen_skips_switch_hint(self):
        with (
            mock.patch.object(vram_recover, "_gpu_memory_line", return_value=""),
            mock.patch.object(
                vram_recover, "_loaded_model_bits", return_value=("Qwen3.5-9B-exl3-4.00bpw", "qwen")
            ),
        ):
            msg = vram_recover.generation_abort_message("CUDA out of memory")
        self.assertIn("out of memory", msg.lower())
        self.assertNotIn("switch to qwen", msg)
        self.assertIn("Start a new chat", msg)

    def test_slot_abort_mentions_cache_slots(self):
        with (
            mock.patch.object(vram_recover, "_gpu_memory_line", return_value=""),
            mock.patch.object(vram_recover, "_loaded_model_bits", return_value=("", "")),
        ):
            msg = vram_recover.generation_abort_message(
                AssertionError("Cannot create new state: no available slots")
            )
        self.assertIn("cache ran out of slots", msg)
        self.assertIn("no available slots", msg)

    def test_non_vram_abort_includes_cause(self):
        msg = vram_recover.generation_abort_message(RuntimeError("tokenizer exploded"))
        self.assertIn("Chat completion aborted", msg)
        self.assertIn("tokenizer exploded", msg)
        self.assertIn("Retry the message", msg)
        self.assertNotIn("server console", msg.lower())


class HealthClearTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await HealthManager.clear()

    async def test_clear_drops_unhealthy_events(self):
        await HealthManager.clear()
        await HealthManager.add_unhealthy_event(RuntimeError("Allocation on device"))
        healthy, issues = await HealthManager.is_service_healthy()
        self.assertFalse(healthy)
        self.assertTrue(issues)
        await HealthManager.clear()
        healthy, issues = await HealthManager.is_service_healthy()
        self.assertTrue(healthy)
        self.assertEqual(issues, [])
