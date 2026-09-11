"""Stack occupancy, gated GPU/phrase switch, and Comfy kill-guard."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ui import occupancy


class OccupancySnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        occupancy.reset_for_tests()

    def tearDown(self):
        occupancy.reset_for_tests()

    def test_queue_text_names_the_holder(self):
        text = occupancy.queue_text(
            {
                "occupant": "alice",
                "kind": "image",
                "who": "bob",
                "busy": True,
            }
        )
        self.assertIn("alice is generating images", text)
        self.assertNotIn("You are in a queue", text)
        self.assertNotIn("Your request will wait", text)

    def test_queued_hint_includes_position(self):
        text = occupancy.queue_text(
            {
                "occupant": "alice",
                "kind": "chat",
                "who": "bob",
                "queued": True,
                "position": 2,
            }
        )
        self.assertIn("alice is chatting", text)
        self.assertIn("You are in a queue", text)
        self.assertIn("number 2", text)
        self_wait = occupancy.queue_text(
            {
                "occupant": "alice",
                "kind": "chat",
                "who": "alice",
                "queued": True,
                "mine": True,
            }
        )
        self.assertEqual(self_wait, occupancy.SELF_QUEUED_HINT)
        self.assertNotIn("You are in a queue", self_wait)

    def test_plain_queue_hint_unchanged(self):
        text = occupancy.queue_text({"position": 0})
        self.assertNotIn("You are in a queue", text)
        self.assertEqual(text, "The stack is being used.")

    async def test_snapshot_includes_occupant_and_mine(self):
        with (
            mock.patch("ui.occupancy._image_job", return_value=None),
            mock.patch("ui.occupancy._switch_busy", return_value=False),
            mock.patch("ui.occupancy._llm_jobs_active", return_value=False),
        ):
            oid = await occupancy.try_acquire("alice", kind="chat")
            self.assertTrue(oid)
            snap = occupancy.snapshot("alice")
            self.assertTrue(snap["busy"])
            self.assertTrue(snap["mine"])
            self.assertEqual(snap["occupant"], "alice")
            self.assertEqual(snap["kind"], "chat")
            self.assertFalse(snap["queued"])
            self.assertEqual(snap["hint"], occupancy.MINE_HINT)
            self.assertNotIn("You are in a queue", snap["hint"])
            other = occupancy.snapshot("bob")
            self.assertFalse(other["mine"])
            self.assertFalse(other["queued"])
            self.assertIn("alice is chatting", other["hint"])
            self.assertNotIn("You are in a queue", other["hint"])
            await occupancy.release(oid)

    async def test_live_flight_marks_kiosk_busy_without_occupant(self):
        from ui.flight import ConsoleFlight, register_flight

        register_flight(ConsoleFlight("alice", "c1", "chat", "hello"))
        with (
            mock.patch("ui.occupancy._image_job", return_value=None),
            mock.patch("ui.occupancy._switch_busy", return_value=False),
            mock.patch("ui.occupancy._llm_jobs_active", return_value=False),
        ):
            snap = occupancy.snapshot("")
        self.assertTrue(snap["busy"])
        self.assertTrue(snap["live"])
        self.assertEqual(snap["kind"], "chat")
        self.assertIsNone(snap["occupant"])

    async def test_live_flight_does_not_block_acquire_or_promote(self):
        """Streaming UI chat registers its flight, then takes the gate."""
        with (
            mock.patch("ui.occupancy._image_job", return_value=None),
            mock.patch("ui.occupancy._switch_busy", return_value=False),
            mock.patch("ui.occupancy._llm_jobs_active", return_value=False),
            mock.patch("ui.flight.iter_live_flights", return_value=[object()]),
        ):
            self.assertTrue(occupancy._lease_work_live())
            self.assertFalse(occupancy._externally_busy())
            oid = await occupancy.try_acquire("alice", kind="chat")
            self.assertIsNotNone(oid)
            await occupancy.release(oid)
            waiter = await occupancy.enqueue("alice", kind="chat")
            promoted = await occupancy.promote(waiter)
            self.assertIsNotNone(promoted)
            await occupancy.release(promoted)

    async def test_reclaim_skips_done_task_while_flight_live(self):
        with (
            mock.patch("ui.occupancy._image_job", return_value=None),
            mock.patch("ui.occupancy._switch_busy", return_value=False),
            mock.patch("ui.occupancy._llm_jobs_active", return_value=False),
        ):
            oid = await occupancy.try_acquire("alice", kind="chat")
        occupancy._occupant.task = mock.Mock(done=lambda: True)
        with mock.patch("ui.flight.iter_live_flights", return_value=[object()]):
            self.assertFalse(occupancy._reclaim_stale())
        self.assertEqual(occupancy._occupant.id, oid)
        occupancy._occupant = None

    async def test_reclaim_skips_done_task_while_llm_job_runs(self):
        with (
            mock.patch("ui.occupancy._image_job", return_value=None),
            mock.patch("ui.occupancy._switch_busy", return_value=False),
            mock.patch("ui.occupancy._llm_jobs_active", return_value=False),
        ):
            oid = await occupancy.try_acquire("alice", kind="chat")
        occupancy._occupant.task = mock.Mock(done=lambda: True)
        with mock.patch("ui.occupancy._llm_jobs_active", return_value=True):
            self.assertFalse(occupancy._reclaim_stale())
            snap = occupancy.snapshot("")
        self.assertEqual(occupancy._occupant.id, oid)
        self.assertTrue(snap["busy"])
        occupancy._occupant = None

    async def test_reclaim_skips_age_while_llm_job_runs(self):
        with (
            mock.patch("ui.occupancy._image_job", return_value=None),
            mock.patch("ui.occupancy._switch_busy", return_value=False),
            mock.patch("ui.occupancy._llm_jobs_active", return_value=False),
        ):
            oid = await occupancy.try_acquire("alice", kind="chat")
        occupancy._occupant.started_at = time.time() - occupancy._LEASE_MAX_S - 1
        with mock.patch("ui.occupancy._lease_work_live", return_value=True):
            self.assertFalse(occupancy._reclaim_stale())
        self.assertEqual(occupancy._occupant.id, oid)
        occupancy._occupant = None
        job = SimpleNamespace(
            status="running", phase="generating", owner="alice", chat_id="c1"
        )
        with mock.patch("images.jobs.active_mcp_image_job", return_value=job):
            snap = occupancy.snapshot("bob")
            mine = occupancy.snapshot("alice")
        self.assertTrue(snap["busy"])
        self.assertEqual(snap["kind"], "image")
        self.assertEqual(snap["occupant"], "alice")
        self.assertFalse(snap["mine"])
        self.assertFalse(snap["queued"])
        self.assertIn("alice is generating images", snap["hint"])
        self.assertNotIn("You are in a queue", snap["hint"])
        self.assertTrue(mine["mine"])
        self.assertEqual(mine["chat_id"], "c1")
        self.assertEqual(mine["hint"], occupancy.MINE_HINT)

    async def test_switch_lock_is_externally_busy(self):
        with mock.patch("common.phrase_switch.switch_lock_held", return_value=True):
            self.assertTrue(occupancy._externally_busy())
            snap = occupancy.snapshot("bob")
        self.assertTrue(snap["busy"])
        self.assertEqual(snap["kind"], "gpu")

    async def test_promote_blocked_while_image_job_runs(self):
        job = SimpleNamespace(status="running", phase="generating", owner="alice")
        waiter = await occupancy.enqueue("bob", kind="chat")
        with mock.patch("images.jobs.active_mcp_image_job", return_value=job):
            promoted = await occupancy.promote(waiter)
        self.assertIsNone(promoted)
        await occupancy.drop_waiter(waiter)


class WaitOutImageJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_job_does_not_wait_on_itself(self):
        from images.jobs import wait_out_generating_image_jobs

        with mock.patch("images.jobs.active_mcp_image_job") as active:
            active.return_value = SimpleNamespace(status="running", phase="restoring_llm")
            await wait_out_generating_image_jobs(from_job=True)
            active.assert_not_called()

    async def test_waits_until_generating_job_leaves(self):
        from images.jobs import wait_out_generating_image_jobs

        busy = SimpleNamespace(status="running", phase="generating")
        states = [busy, busy, None]
        with (
            mock.patch("images.jobs.active_mcp_image_job", side_effect=states),
            mock.patch("images.jobs.asyncio.sleep", new=mock.AsyncMock()) as sleep,
        ):
            await wait_out_generating_image_jobs(interval=0.01)
        self.assertGreaterEqual(sleep.await_count, 1)

    async def test_reload_last_llm_waits_before_stop_comfy(self):
        from images.jobs import reload_last_llm

        order = []

        async def wait(*, from_job=False, interval=0.5):
            order.append("wait")

        def stop(*_a, **_k):
            order.append("stop")

        with (
            mock.patch("images.jobs.wait_out_generating_image_jobs", wait),
            mock.patch("images.jobs.stop_comfy", stop),
            mock.patch("images.jobs.wait_gpu_vram_drain"),
            mock.patch("images.jobs.reset_cuda_memory"),
            mock.patch("images.jobs.available_profiles", return_value=["qwen"]),
            mock.patch("images.jobs.last_profile", return_value="qwen"),
            mock.patch("common.phrase_switch.set_switch_lock"),
            mock.patch("common.phrase_switch.clear_switch_lock"),
            mock.patch("images.jobs._load_profile", new=mock.AsyncMock()),
            mock.patch("images.jobs.write_mode"),
            mock.patch("images.jobs.loaded_tabby_name", return_value=None),
        ):
            await reload_last_llm("qwen")
        self.assertEqual(order[0], "wait")
        self.assertIn("stop", order)
        self.assertLess(order.index("wait"), order.index("stop"))


class PersistJobsBlockTests(unittest.TestCase):
    def test_running_job_blocks(self):
        from common.gpu_mode import persisted_jobs_block_llm_load

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp_jobs.json"
            path.write_text(json.dumps([{"id": "1", "status": "running", "phase": "generating"}]))
            self.assertTrue(persisted_jobs_block_llm_load(path))
            path.write_text(json.dumps([{"id": "1", "status": "done", "phase": "done"}]))
            self.assertFalse(persisted_jobs_block_llm_load(path))

    def test_wait_out_image_jobs_polls_disk(self):
        import switch_model

        with mock.patch("switch_model.persisted_jobs_block_llm_load", side_effect=[True, True, False]):
            with mock.patch("switch_model.time.sleep") as slept:
                switch_model.wait_out_image_jobs(poll_s=0.01)
        self.assertGreaterEqual(slept.call_count, 1)


class GatedGpuSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        occupancy.reset_for_tests()

    def tearDown(self):
        occupancy.reset_for_tests()

    async def test_ui_gpu_waits_until_gate_is_free(self):
        from ui.router import ui_gpu

        apply = mock.AsyncMock(return_value={"ok": True, "mode": "llm"})
        request = mock.Mock()
        request.json = mock.AsyncMock(return_value={"mode": "qwen"})
        request.is_disconnected = mock.AsyncMock(return_value=False)
        handler = mock.Mock()
        handler.poll = mock.AsyncMock()

        async def tiny_tick(_timeout=1.0):
            await asyncio.sleep(0)

        with (
            mock.patch("ui.router.apply_gpu_mode", apply),
            mock.patch("ui.occupancy.wait_tick", tiny_tick),
            mock.patch("ui.occupancy._externally_busy", return_value=False),
            mock.patch("common.networking.DisconnectHandler", return_value=handler),
        ):
            oid = await occupancy.try_acquire("alice", kind="chat")
            self.assertIsNotNone(oid)
            task = asyncio.create_task(ui_gpu(request, _user="bob"))
            await asyncio.sleep(0)
            self.assertFalse(apply.called)
            await occupancy.release(oid)
            result = await asyncio.wait_for(task, timeout=2)
        self.assertEqual(result["ok"], True)
        apply.assert_awaited_once_with("qwen")

    async def test_console_phrase_switch_waits_for_gate(self):
        from ui.chat import run_console_chat

        apply = mock.AsyncMock()
        handler = mock.Mock()
        handler.poll = mock.AsyncMock()
        request = mock.Mock()
        request.is_disconnected = mock.AsyncMock(return_value=False)
        body = {
            "messages": [{"role": "user", "content": "switch to qwen"}],
            "stream": False,
        }

        async def tiny_tick(_timeout=1.0):
            await asyncio.sleep(0)

        with (
            mock.patch("ui.chat.handle_if_requested", return_value=None),
            mock.patch("ui.chat.requested_profile", return_value="qwen"),
            mock.patch("ui.chat.missing_profile_reply", return_value=None),
            mock.patch("ui.router.apply_gpu_mode", apply),
            mock.patch("ui.chat.switch_reply_text", return_value="Switching to qwen."),
            mock.patch("ui.chat.DisconnectHandler", return_value=handler),
            mock.patch("ui.chat.public_api_base", return_value="http://x"),
            mock.patch("ui.occupancy.wait_tick", tiny_tick),
            mock.patch("ui.occupancy._externally_busy", return_value=False),
        ):
            oid = await occupancy.try_acquire("alice", kind="image")
            self.assertIsNotNone(oid)
            task = asyncio.create_task(run_console_chat(request, body, username="bob"))
            await asyncio.sleep(0)
            self.assertFalse(apply.called)
            await occupancy.release(oid)
            result = await asyncio.wait_for(task, timeout=2)
        apply.assert_awaited_once_with("qwen")
        self.assertIn("Switching to qwen", result.choices[0].message.content)

    def test_help_still_skips_the_gate(self):
        from common.phrase_switch import handle_if_requested
        from endpoints.OAI.types.chat_completion import ChatCompletionMessage, ChatCompletionRequest

        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="help")]
        )
        with mock.patch("common.phrase_switch.start_switch") as start:
            result = handle_if_requested(data, defer_switch=True)
        start.assert_not_called()
        self.assertIsNotNone(result)
        self.assertIn("help", result.choices[0].message.content.lower())

    def test_defer_switch_does_not_start_switch(self):
        from common.phrase_switch import handle_if_requested
        from endpoints.OAI.types.chat_completion import ChatCompletionMessage, ChatCompletionRequest

        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="switch to qwen")]
        )
        with (
            mock.patch("common.phrase_switch.requested_profile", return_value="qwen"),
            mock.patch("common.phrase_switch.start_switch") as start,
        ):
            result = handle_if_requested(data, defer_switch=True)
        start.assert_not_called()
        self.assertIsNone(result)

    def test_defer_restart_does_not_start_restart(self):
        from common.phrase_switch import handle_if_requested
        from endpoints.OAI.types.chat_completion import ChatCompletionMessage, ChatCompletionRequest

        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="restart")]
        )
        with mock.patch("common.phrase_switch.start_restart") as restart:
            result = handle_if_requested(data, defer_switch=True)
        restart.assert_not_called()
        self.assertIsNone(result)

    def test_oai_router_defers_phrase_switch(self):
        src = Path(__file__).resolve().parents[1] / "endpoints" / "OAI" / "router.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("defer_switch=True", text)
        self.assertIn("start_switch(name)", text)


if __name__ == "__main__":
    unittest.main()
