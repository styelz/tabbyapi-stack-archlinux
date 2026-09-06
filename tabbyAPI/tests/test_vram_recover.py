import unittest
from collections import deque
from types import SimpleNamespace

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
