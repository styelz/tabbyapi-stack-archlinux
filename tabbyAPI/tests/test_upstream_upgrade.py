"""
Tests for the upstream (ExLlama 1.4.6) upgrade port.

Covers the three fork-specific integration points:
  * ExllamaV3Container.job_max_rq_tokens (KV page reservation clamp)
  * chat_completion._chat_stream_collector reasoning/content SSE split
  * common.networking.DisconnectHandler watcher / stand-in / abort variants

Each group guards its heavy imports (torch, exllamav3, fastapi, loguru, ...)
so the module still collects on a machine that only edits the source. On the
provisioned GPU host every group runs.
"""

import asyncio
import unittest
from types import SimpleNamespace

# --- job_max_rq_tokens ------------------------------------------------------

try:
    from backends.exllamav3.model import ExllamaV3Container

    _CONTAINER_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on torch/exllamav3
    ExllamaV3Container = None
    _CONTAINER_IMPORT_ERROR = exc


@unittest.skipUnless(
    ExllamaV3Container is not None,
    f"exllamav3 backend unavailable: {_CONTAINER_IMPORT_ERROR}",
)
class JobMaxRqTokensTests(unittest.TestCase):
    """Short completions must not over-reserve KV pages."""

    @staticmethod
    def _call(max_rq_tokens, max_tokens):
        # job_max_rq_tokens only reads self.max_rq_tokens, so a stand-in is
        # enough to exercise the real (unbound) method without loading a model.
        stub = SimpleNamespace(max_rq_tokens=max_rq_tokens)
        return ExllamaV3Container.job_max_rq_tokens(stub, max_tokens)

    def test_disabled_when_output_chunking_off(self):
        # max_rq_tokens is None when output_chunking is disabled: never clamp.
        self.assertIsNone(self._call(None, 10))
        self.assertIsNone(self._call(None, 100000))
        self.assertIsNone(self._call(None, 0))

    def test_fits_in_single_chunk_reserves_whole_completion(self):
        # max_tokens <= max_rq_tokens: one round, so reserve exactly it (None).
        self.assertIsNone(self._call(2048, 1))
        self.assertIsNone(self._call(2048, 2048))

    def test_long_completion_clamps_to_chunk(self):
        # Longer than a chunk: requeues, so cap the per-round reservation.
        self.assertEqual(self._call(2048, 2049), 2048)
        self.assertEqual(self._call(2048, 100000), 2048)

    def test_unbounded_max_tokens_keeps_chunk(self):
        # max_tokens <= 0 means "fill the context"; keep the chunk reservation.
        self.assertEqual(self._call(2048, 0), 2048)


# --- reasoning/content SSE split -------------------------------------------

try:
    from common import model as _model
    from endpoints.OAI.utils import chat_completion as _chat_completion

    _COLLECTOR_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on fastapi/torch/...
    _model = None
    _chat_completion = None
    _COLLECTOR_IMPORT_ERROR = exc


class _FakeContainer:
    """Minimal model.container stand-in for a tag-reasoning model."""

    harmony = False
    muse_glimmer = False
    tool_format = None
    reasoning = True
    reasoning_start_token = "<think>"
    reasoning_end_token = "</think>"
    tool_calls_in_reasoning = True
    reasoning_budget_tokens = None
    reasoning_budget_message = None

    def __init__(self, chunks):
        self._chunks = chunks

    async def stream_generate(
        self,
        job_id,
        prompt,
        params,
        disconnect_handler,
        mm_embeddings=None,
        filter_trigger=None,
    ):
        for chunk in self._chunks:
            yield chunk


@unittest.skipUnless(
    _chat_completion is not None,
    f"chat_completion unavailable: {_COLLECTOR_IMPORT_ERROR}",
)
class ReasoningContentSplitTests(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, chunks, start_in_reasoning=True):
        params = SimpleNamespace(
            reasoning_budget_tokens=None,
            reasoning=None,
            reasoning_budget_message=None,
            tool_choice="none",
            json_schema=None,
            regex_pattern=None,
            grammar_string=None,
        )
        gen_queue: asyncio.Queue = asyncio.Queue()

        original = getattr(_model, "container", None)
        _model.container = _FakeContainer(chunks)
        try:
            await _chat_completion._chat_stream_collector(
                task_idx=0,
                gen_queue=gen_queue,
                request_id="req-test",
                prompt="prompt",
                params=params,
                start_in_reasoning_mode=start_in_reasoning,
                streaming_mode=True,
                disconnect_handler=SimpleNamespace(),
            )
        finally:
            _model.container = original

        frames = []
        while not gen_queue.empty():
            frames.append(gen_queue.get_nowait())
        return frames

    async def test_mixed_chunk_splits_into_two_frames(self):
        # One generator chunk crosses the end of the reasoning phase.
        frames = await self._collect(
            [{"text": "think</think>answer", "finish_reason": "stop", "token_ids": [1, 2, 3]}]
        )

        # No frame may carry both reasoning_content and content.
        for frame in frames:
            self.assertFalse(
                frame.get("delta_reasoning_content") and frame.get("delta_content"),
                f"mixed reasoning+content frame emitted: {frame}",
            )

        reasoning_frames = [f for f in frames if f.get("delta_reasoning_content")]
        content_frames = [f for f in frames if f.get("delta_content")]

        self.assertEqual(len(reasoning_frames), 1)
        self.assertEqual(reasoning_frames[0]["delta_reasoning_content"], "think")
        # The reasoning-only frame is not terminal; the content frame carries it.
        self.assertIsNone(reasoning_frames[0].get("finish_reason"))

        self.assertEqual(len(content_frames), 1)
        self.assertEqual(content_frames[0]["delta_content"], "answer")
        self.assertEqual(content_frames[0].get("finish_reason"), "stop")

        # Ordering: reasoning tail is emitted before the content delta.
        self.assertIs(frames[0], reasoning_frames[0])

    async def test_content_only_chunk_is_single_frame(self):
        # Already past reasoning (start_in_reasoning=False): no split, and no
        # spurious reasoning frame is emitted.
        frames = await self._collect(
            [{"text": "answer", "finish_reason": "stop", "token_ids": [1]}],
            start_in_reasoning=False,
        )
        content_frames = [f for f in frames if f.get("delta_content")]
        reasoning_frames = [f for f in frames if f.get("delta_reasoning_content")]
        self.assertEqual(len(content_frames), 1)
        self.assertEqual(content_frames[0]["delta_content"], "answer")
        self.assertEqual(reasoning_frames, [])


# --- DisconnectHandler variants --------------------------------------------

try:
    from common.networking import DisconnectHandler

    _NETWORKING_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on fastapi/loguru/...
    DisconnectHandler = None
    _NETWORKING_IMPORT_ERROR = exc


class _FakeReceiveRequest:
    """A real-request stand-in that exposes an ASGI receive() channel."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.is_disconnected_calls = 0

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        # Nothing more to deliver: block like a live connection.
        await asyncio.sleep(3600)

    async def is_disconnected(self):
        # The watcher path must never poll this in a tight loop.
        self.is_disconnected_calls += 1
        return False


@unittest.skipUnless(
    DisconnectHandler is not None,
    f"common.networking unavailable: {_NETWORKING_IMPORT_ERROR}",
)
class DisconnectHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_event_only_no_request(self):
        # Nested generate(): no request, cancellation only via abort_event.
        abort = asyncio.Event()
        handler = DisconnectHandler(description="nested", abort_event=abort)
        self.assertIsNone(handler._watcher)

        cleaned = []
        await handler.add_cleanup_task("k", _record_cleanup, (cleaned,))

        # Not aborted yet: poll is a no-op.
        await handler.poll()
        self.assertEqual(cleaned, [])

        abort.set()
        with self.assertRaises(asyncio.CancelledError):
            await handler.poll()
        self.assertEqual(cleaned, ["done"])
        self.assertTrue(handler.disconnected)

    async def test_standin_is_disconnected_flag(self):
        # Console-flight stand-in: has is_disconnected() but no receive().
        state = {"gone": False}

        async def is_disconnected():
            return state["gone"]

        request = SimpleNamespace(is_disconnected=is_disconnected)
        handler = DisconnectHandler(request=request, description="standin")
        # No receive() -> no background watcher, keeps the poll path.
        self.assertIsNone(handler._watcher)

        await handler.poll()  # connected -> no raise

        state["gone"] = True
        handler.last_poll = 0  # is_disconnected() is throttled to 20/s
        with self.assertRaises(asyncio.CancelledError):
            await handler.poll()

    async def test_real_receive_disconnect_sets_flag(self):
        request = _FakeReceiveRequest([{"type": "http.disconnect"}])
        handler = DisconnectHandler(request=request, description="real")
        self.assertIsNotNone(handler._watcher)

        # Let the watcher consume the disconnect message.
        await asyncio.wait_for(handler._watcher, timeout=1)

        self.assertTrue(handler.disconnected)
        self.assertTrue(handler.abort_event.is_set())
        # Watcher mode does not poll is_disconnected() per token.
        self.assertEqual(request.is_disconnected_calls, 0)

        with self.assertRaises(asyncio.CancelledError):
            await handler.poll()

    async def test_cleanup_cancels_watcher(self):
        # A connected real request: watcher blocks on receive() until cleanup.
        request = _FakeReceiveRequest([])
        handler = DisconnectHandler(request=request, description="cancel")
        watcher = handler._watcher
        self.assertIsNotNone(watcher)

        await asyncio.sleep(0)  # let the watcher start awaiting receive()
        await handler.cleanup()

        with self.assertRaises(asyncio.CancelledError):
            await watcher
        self.assertTrue(watcher.cancelled())


async def _record_cleanup(sink):
    sink.append("done")


if __name__ == "__main__":
    unittest.main()
