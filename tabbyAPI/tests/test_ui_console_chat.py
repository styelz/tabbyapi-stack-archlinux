import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from images import chat as images_chat
from ui.chat import completion_request_from_payload
from ui.manager import sanitize_chat_payload
from common.phrase_switch import (
    image_job_done_text,
    image_ready_response,
    is_help_request,
    is_list_request,
    is_restart_request,
    requested_profile,
)
from endpoints.OAI.types.chat_completion import ChatCompletionMessage, ChatCompletionRequest


class ConsoleImageChatTests(unittest.TestCase):
    def test_handle_accepts_console_flag(self):
        self.assertIn("console", images_chat.handle.__code__.co_varnames)

    def test_console_generate_skips_file_tools(self):
        async def go():
            data = ChatCompletionRequest(
                messages=[ChatCompletionMessage(role="user", content="draw a red cube")]
            )
            plan = mock.Mock(
                action="generate",
                items=[{"prompt": "red cube", "output_path": "images/a.png"}],
            )
            started = mock.Mock(id="job-ui")
            with (
                mock.patch.object(
                    images_chat, "classify_image_turn", new=mock.AsyncMock(return_value=plan)
                ),
                mock.patch.object(images_chat, "active_mcp_image_job", return_value=None),
                mock.patch.object(
                    images_chat, "_start_mixed_job", new=mock.AsyncMock(return_value=started)
                ) as start,
                mock.patch.object(images_chat, "_write_site_code", new=mock.AsyncMock()) as write,
                mock.patch.object(
                    images_chat, "_hold_then_reply", new=mock.AsyncMock(return_value="held")
                ) as hold,
            ):
                result = await images_chat.handle(
                    data, api_base="http://x", llm_ready=True, console=True, chat_id="conv-1"
                )
            write.assert_not_called()
            start.assert_awaited()
            hold.assert_awaited()
            self.assertTrue(hold.await_args.kwargs.get("console"))
            self.assertFalse(hold.await_args.kwargs.get("mixed"))
            self.assertEqual(start.await_args.kwargs.get("chat_id"), "conv-1")
            self.assertEqual(result, "held")

        asyncio.run(go())


class ConsoleImageReplyTests(unittest.TestCase):
    def _job(self, **kwargs):
        url = kwargs.pop(
            "url",
            "https://gpu.example/v1/images/generated-20260824-060727-122943.png",
        )
        defaults = dict(
            id="748b6eef-f8ac-4529-a6b3-be3c7ffd1730",
            error="",
            restore=True,
            wait_text=(
                "About 3 minutes to render (Flux), then about 65 seconds "
                "to reload the coding model."
            ),
            items=[],
            url=url,
        )
        defaults.update(kwargs)
        url = defaults.pop("url")
        job = SimpleNamespace(**defaults)
        return job, url

    def test_console_reply_hides_job_jargon(self):
        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="create an image of c3po and r2d2")]
        )
        job, url = self._job()
        with mock.patch.object(
            images_chat, "living_download_pairs", return_value=[(url, "images/generated.png")]
        ):
            response = images_chat._url_response(
                data, job, "https://gpu.example/v1", console=True
            )
        text = response.choices[0].message.content
        self.assertNotIn("tabby-image-job:", text)
        self.assertNotIn(job.id, text)
        self.assertNotIn("image(s) from this turn", text)
        self.assertNotIn("Another picture:", text)
        self.assertNotIn("This picture:", text)
        self.assertIn("Here's the picture", text)
        self.assertIn(f"![]({url})", text)
        self.assertEqual(text.count(url), 1)
        self.assertNotIn("to render", text)
        self.assertNotIn("to reload the coding model", text)
        self.assertNotIn(job.wait_text, text)
        self.assertIn("Rendered with Flux", text)
        self.assertIn("It's also in Gallery", text)

    def test_ide_reply_still_stamps_job(self):
        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="generate an image of a cube")]
        )
        job, url = self._job()
        with mock.patch.object(
            images_chat, "living_download_pairs", return_value=[(url, "images/generated.png")]
        ):
            response = images_chat._url_response(
                data, job, "https://gpu.example/v1", console=False
            )
        text = response.choices[0].message.content
        self.assertIn("tabby-image-job:", text)
        self.assertIn(job.id, text)
        self.assertNotIn("Another picture:", text)
        self.assertNotIn("This picture:", text)

    def test_image_ready_text_is_not_doubled(self):
        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="a red cube")]
        )
        text = image_ready_response(
            data, "generated-x.png", api_base="http://x"
        ).choices[0].message.content
        self.assertNotIn("Another picture:", text)
        self.assertNotIn("This picture:", text)
        self.assertNotIn("to render", text)
        self.assertNotIn("to reload the coding model", text)
        self.assertIn("Rendered with Flux", text)
        self.assertEqual(text.count("Rendered with"), 1)

    def test_done_text_names_backend_and_elapsed(self):
        qwen = image_job_done_text(
            "qwen-image: a logo that says Cafe",
            restore=True,
            elapsed_s=238,
        )
        self.assertIn("Rendered with Qwen-Image in 3m 58s", qwen)
        self.assertIn("The coding model is loaded again", qwen)
        self.assertNotIn("to render", qwen)
        flux = image_job_done_text("a red cube", restore=False, elapsed_s=12)
        self.assertIn("Rendered with Flux in 12s", flux)
        self.assertNotIn("coding model", flux)
        mixed = image_job_done_text(
            prompts=["a forest at dusk", "qwen-image: SALE poster"],
            restore=True,
        )
        self.assertIn("Rendered 2 pictures in one Comfy session", mixed)
        self.assertIn("Flux", mixed)
        self.assertIn("Qwen-Image", mixed)

    def test_job_progress_line(self):
        job = SimpleNamespace(
            phase="starting_comfy", status="running", count=1, current_index=0
        )
        self.assertEqual(images_chat.job_progress_line(job), "Starting Comfy")
        job.phase = "generating"
        self.assertEqual(images_chat.job_progress_line(job), "Rendering in Comfy")
        job.phase = "restoring_llm"
        self.assertEqual(images_chat.job_progress_line(job), "Reloading the coding model")

    def test_console_inflight_hold_is_not_mixed(self):
        async def go():
            data = ChatCompletionRequest(
                messages=[
                    ChatCompletionMessage(role="user", content="draw a cube"),
                    ChatCompletionMessage(
                        role="assistant", content="tabby-image-job: job-ui"
                    ),
                    ChatCompletionMessage(role="user", content="still going?"),
                ]
            )
            job = SimpleNamespace(
                id="job-ui",
                status="running",
                items=[SimpleNamespace(output_path="images/logo.png")],
            )
            with (
                mock.patch.object(images_chat, "job_id_from_history", return_value="job-ui"),
                mock.patch.object(images_chat, "get_mcp_image_job", return_value=job),
                mock.patch.object(
                    images_chat, "_hold_then_reply", new=mock.AsyncMock(return_value="held")
                ) as hold,
            ):
                result = await images_chat.handle(
                    data, api_base="http://x", llm_ready=True, console=True
                )
            self.assertEqual(result, "held")
            self.assertTrue(hold.await_args.kwargs.get("console"))
            self.assertFalse(hold.await_args.kwargs.get("mixed"))

        asyncio.run(go())

    def test_remove_border_starts_img2img_job(self):
        import tempfile
        from pathlib import Path

        from PIL import Image
        from common.phrase_switch import border_edit_prompt, wants_border_trim

        self.assertTrue(wants_border_trim("remove the white border from this image"))
        self.assertFalse(wants_border_trim("remove the border-radius on the button"))
        self.assertIn("no white border", border_edit_prompt("remove the white border"))

        canvas = Image.new("RGB", (80, 80), "white")
        source = Path(tempfile.gettempdir()) / "source-trim-test.png"
        canvas.save(source, format="PNG")

        async def go():
            data = ChatCompletionRequest(
                messages=[
                    ChatCompletionMessage(
                        role="user",
                        content="remove the white border from this image",
                    )
                ],
                stream=False,
            )
            started = mock.Mock(id="job-img2img")
            with (
                mock.patch.object(
                    images_chat, "_start_prompt_job", new=mock.AsyncMock(return_value=started)
                ) as start,
                mock.patch.object(
                    images_chat, "_hold_then_reply", new=mock.AsyncMock(return_value="held")
                ) as hold,
                mock.patch.object(
                    images_chat, "classify_image_turn", new=mock.AsyncMock()
                ) as classify,
            ):
                result = await images_chat.handle(
                    data,
                    api_base="http://x",
                    source_image=source,
                    llm_ready=True,
                    console=True,
                    chat_id="conv-1",
                )
            classify.assert_not_called()
            start.assert_awaited()
            kwargs = start.await_args.kwargs
            self.assertEqual(Path(kwargs.get("source_image")), source)
            self.assertEqual(kwargs.get("denoise"), 0.85)
            self.assertEqual(kwargs.get("chat_id"), "conv-1")
            self.assertIn("no white border", start.await_args.args[0])
            hold.assert_awaited()
            self.assertTrue(hold.await_args.kwargs.get("console"))
            self.assertEqual(result, "held")

        asyncio.run(go())


class ConsoleChatRequestTests(unittest.TestCase):
    def test_missing_temperature_uses_default(self):
        payload = sanitize_chat_payload({"messages": [{"role": "user", "content": "hello?"}]})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("min_p", payload)
        req = completion_request_from_payload(payload)
        self.assertIsNotNone(req.temperature)
        self.assertGreater(req.temperature, 0)

    def test_sampler_keys_are_forwarded(self):
        payload = sanitize_chat_payload({
            "messages": [{"role": "user", "content": "hello?"}],
            "temperature": 0.2,
            "top_p": 0.9,
            "min_p": 0.05,
            "frequency_penalty": 0.1,
            "presence_penalty": 0.2,
            "max_tokens": 128,
        })
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["min_p"], 0.05)
        req = completion_request_from_payload(payload)
        self.assertEqual(req.temperature, 0.2)
        self.assertEqual(req.top_p, 0.9)
        self.assertEqual(req.max_tokens, 128)

    def test_ui_chat_requests_include_usage(self):
        payload = sanitize_chat_payload({"messages": [{"role": "user", "content": "hello?"}]})
        req = completion_request_from_payload(payload)
        self.assertTrue(req.stream_options)
        self.assertTrue(req.stream_options.include_usage)

    def test_explicit_null_temperature_is_coerced(self):
        req = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="hello?")],
            temperature=None,
        )
        self.assertIsNotNone(req.temperature)


class SlashCommandTests(unittest.TestCase):
    def _req(self, text):
        return ChatCompletionRequest(messages=[ChatCompletionMessage(role="user", content=text)])

    def test_slash_help_list_restart(self):
        self.assertTrue(is_help_request(self._req("/help")))
        self.assertTrue(is_list_request(self._req("/list models")))
        self.assertTrue(is_restart_request(self._req("/restart")))

    def test_slash_profile_aliases(self):
        self.assertEqual(requested_profile(self._req("/comfy")), "comfy")
        self.assertEqual(requested_profile(self._req("/llm")), "llm")
        self.assertEqual(requested_profile(self._req("/switch to comfy")), "comfy")


class UnpumpedAssistantTextTests(unittest.TestCase):
    def test_job_mark_and_hint_after_live_stream(self):
        from ui.chat import _unpumped_assistant_text

        extra = _unpumped_assistant_text(
            "writing the page",
            "tabby-image-job: abc-123\nwriting the page\nPoint img src at images/logo.png.",
        )
        self.assertIn("tabby-image-job: abc-123", extra)
        self.assertIn("Point img src at images/logo.png.", extra)
        self.assertNotIn("writing the page\nwriting the page", extra)

    def test_no_repeat_when_content_already_streamed(self):
        from ui.chat import _unpumped_assistant_text

        extra = _unpumped_assistant_text("hello", "hello")
        self.assertEqual(extra.strip(), "")


class ConsoleChatNotReadyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from ui.occupancy import reset_for_tests

        reset_for_tests()

    def tearDown(self):
        from ui.occupancy import reset_for_tests

        reset_for_tests()

    def _body(self):
        return {
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }

    def test_source_uses_loading_helper_not_status_error(self):
        from pathlib import Path
        import ui.chat as ui_chat

        src = Path(ui_chat.__file__).read_text(encoding="utf-8")
        self.assertIn("llm_not_ready_response", src)
        self.assertNotIn("The coding model is not loaded", src)

    async def test_chat_while_loading_says_still_loading(self):
        from fastapi import HTTPException
        from ui.chat import run_console_chat

        handler = mock.Mock()
        handler.poll = mock.AsyncMock()
        request = mock.Mock()
        with (
            mock.patch("ui.chat.model") as mdl,
            mock.patch("ui.chat.handle_if_requested", return_value=None),
            mock.patch("ui.chat.handle_image_chat", new=mock.AsyncMock(return_value=None)),
            mock.patch("ui.chat.gpu_is_comfy", return_value=False),
            mock.patch("ui.chat.public_api_base", return_value="http://x"),
            mock.patch("ui.chat.DisconnectHandler", return_value=handler),
            mock.patch("common.phrase_switch.switch_in_progress", return_value=True),
            mock.patch("common.phrase_switch.switch_lock_name", return_value="qwen"),
            mock.patch(
                "common.phrase_switch.wait_hint", return_value="Wait about 65 seconds"
            ),
        ):
            mdl.container = None
            result = await run_console_chat(request, self._body())
        self.assertNotIsInstance(result, HTTPException)
        text = result.choices[0].message.content
        self.assertIn("still loading", text.lower())
        self.assertNotIn("not loaded", text.lower())
        self.assertNotIn("Status", text)

    async def test_chat_when_idle_unloaded_is_not_loaded_copy(self):
        from ui.chat import run_console_chat

        handler = mock.Mock()
        handler.poll = mock.AsyncMock()
        request = mock.Mock()
        with (
            mock.patch("ui.chat.model") as mdl,
            mock.patch("ui.chat.handle_if_requested", return_value=None),
            mock.patch("ui.chat.handle_image_chat", new=mock.AsyncMock(return_value=None)),
            mock.patch("ui.chat.gpu_is_comfy", return_value=False),
            mock.patch("ui.chat.public_api_base", return_value="http://x"),
            mock.patch("ui.chat.DisconnectHandler", return_value=handler),
            mock.patch("common.phrase_switch.switch_in_progress", return_value=False),
            mock.patch("images.jobs.active_mcp_image_job", return_value=None),
        ):
            mdl.container = None
            result = await run_console_chat(request, self._body())
        text = result.choices[0].message.content
        self.assertIn("not loaded", text.lower())
