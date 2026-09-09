"""Mixed/coding chat holds until GPU PNGs exist, then curls real URLs."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from endpoints.OAI.types.chat_completion import (
    ChatCompletionMessage,
    ChatCompletionRequest,
    ChatCompletionRespChoice,
    ChatCompletionResponse,
)
from endpoints.OAI.types.tools import Tool, ToolCall
from images.chat import (
    _assistant_already_has_dest_hint,
    _code_reply,
    _dest_exists,
    _explicit_new_rasters,
    _named_image_dests,
    _plan_asset_dests,
    _plan_asset_rasters,
    _upgrade_missing_named_dests,
    handle,
    job_id_from_history,
)
from images.paths import (
    image_download_command,
    living_download_pairs,
    planned_dest_fact_list,
)
from images.plan import (
    CLASSIFY_SYSTEM,
    ImageTurnPlan,
    classify_blob,
    fallback_item,
    parse_plan_json,
    parse_turn_plan,
    plan_from_extracted,
)


def _user(text: str, *, stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[ChatCompletionMessage(role="user", content=text)],
        stream=stream,
    )


def _write_code_response(*, path: str = "index.html") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model="gpt-4o",
        choices=[
            ChatCompletionRespChoice(
                finish_reason="tool_calls",
                message=ChatCompletionMessage(
                    role="assistant",
                    content="writing the page",
                    tool_calls=[
                        ToolCall(
                            function=Tool(
                                name="Write",
                                arguments=(
                                    f'{{"path":"{path}","contents":"<html></html>"}}'
                                ),
                            )
                        )
                    ],
                ),
            )
        ],
    )


def _job(**kwargs):
    items = kwargs.pop(
        "items",
        [
            SimpleNamespace(
                prompt="logo",
                output_path="images/logo.png",
                urls=kwargs.pop("item_urls", []),
                status="done",
            )
        ],
    )
    defaults = dict(
        id="job-1",
        status="done",
        error="",
        restore=True,
        items=items,
        urls=[],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class DestExistsTests(unittest.TestCase):
    def test_png_dest_matches_webp_on_disk(self):
        self.assertTrue(_dest_exists("images/logo.png", ["images/logo.webp"]))
        self.assertFalse(_dest_exists("images/logo.png", ["images/hero.webp"]))


class CodeReplyHintTests(unittest.TestCase):
    def test_first_code_reply_lists_dests_once(self):
        job = _job(id="abc-123", status="coding", code_turns=1)
        data = _user("Create a website with a logo")
        response = _code_reply(data, job, _write_code_response())
        content = response.choices[0].message.content
        self.assertIn("tabby-image-job: abc-123", content)
        self.assertIn("Point img src", content)
        self.assertIn("images/logo.png", content)
        self.assertEqual(content.count("Do not Write PNG"), 1)

    def test_later_code_turn_skips_dest_hint_without_history(self):
        job = _job(id="abc-123", status="coding", code_turns=2)
        data = _user("Create a website with a logo")
        response = _code_reply(data, job, _write_code_response())
        content = response.choices[0].message.content
        self.assertIn("writing the page", content)
        self.assertNotIn("Point img src", content)
        self.assertNotIn("Write the page now", content)

    def test_followup_code_reply_does_not_repeat_dest_hint(self):
        job = _job(id="abc-123", status="coding", code_turns=2)
        first = (
            "tabby-image-job: abc-123\n"
            "Point img src or CSS url() at these exact paths: images/logo.png. "
            "Do not Write PNG, WebP, or placeholder image files."
        )
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(role="user", content="Create a website with a logo"),
                ChatCompletionMessage(role="assistant", content=first),
                ChatCompletionMessage(role="tool", content="Wrote index.html"),
            ]
        )
        self.assertTrue(_assistant_already_has_dest_hint(data))
        response = _code_reply(data, job, _write_code_response(path="styles.css"))
        content = response.choices[0].message.content
        self.assertIn("tabby-image-job: abc-123", content)
        self.assertIn("writing the page", content)
        self.assertNotIn("Point img src", content)
        self.assertNotIn("Write the page now", content)
        self.assertNotIn("Do not Write PNG", content)


class NestedGenerateHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_works_without_http_request(self):
        from common.networking import DisconnectHandler

        handler = DisconnectHandler(description="mixed dest extract")
        cancelled = []

        async def cancel():
            cancelled.append(True)

        await handler.add_cleanup_task("job", cancel, ())
        await handler.poll()
        await handler.finish("job")
        self.assertEqual(cancelled, [])

    async def test_abort_event_cancels_nested_job(self):
        from common.networking import DisconnectHandler

        abort = asyncio.Event()
        abort.set()
        handler = DisconnectHandler(description="nested", abort_event=abort)
        cancelled = []

        async def cancel():
            cancelled.append(True)

        await handler.add_cleanup_task("job", cancel, ())
        with self.assertRaises(asyncio.CancelledError):
            await handler.poll()
        self.assertEqual(cancelled, [True])

    def test_generate_passes_handler_not_abort_event(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1].joinpath(
            "backends/exllamav3/model.py"
        ).read_text()
        self.assertIn("params,\n            handler,\n            mm_embeddings", src)
        self.assertNotIn(
            "params,\n            abort_event,\n            mm_embeddings", src
        )

    async def test_llm_plan_forwards_disconnect_handler(self):
        from images.plan import llm_plan_images

        captured = {}

        async def fake_generate(*args, **kwargs):
            captured["kwargs"] = kwargs
            return {
                "text": '{"images":[{"filename":"logo.png","subject":"logo Cafe"}]}'
            }

        async def fake_template(_request):
            return "prompt", None

        handler = object()
        container = SimpleNamespace(
            loaded=True,
            prompt_template=object(),
            generate=fake_generate,
        )
        with mock.patch("common.model.container", container), mock.patch(
            "endpoints.OAI.utils.chat_completion.apply_chat_template",
            new=fake_template,
        ):
            items = await llm_plan_images(
                "create a website with a logo", disconnect_handler=handler
            )
        self.assertIs(captured["kwargs"].get("disconnect_handler"), handler)
        self.assertEqual(items[0]["output_path"], "images/logo.png")

    async def test_llm_plan_reuse_returns_no_dests(self):
        from images.plan import llm_plan_images

        async def fake_generate(*args, **kwargs):
            return {"text": '{"action":"reuse","images":[]}'}

        async def fake_template(_request):
            return "prompt", None

        container = SimpleNamespace(
            loaded=True,
            prompt_template=object(),
            generate=fake_generate,
        )
        with mock.patch("common.model.container", container), mock.patch(
            "endpoints.OAI.utils.chat_completion.apply_chat_template",
            new=fake_template,
        ):
            items = await llm_plan_images("implement the new images into the webpage")
        self.assertEqual(items, [])


class PlanTests(unittest.TestCase):
    def test_json_plan_not_regex_planets(self):
        blob = (
            '{"images":[{"filename":"logo.png","subject":"logo that says Cosmos"},'
            '{"filename":"mars.png","subject":"photograph of Mars"}]}'
        )
        rows = parse_plan_json(blob)
        items = plan_from_extracted("build a cosmos tours website with images", rows)
        dests = [row["output_path"] for row in items]
        self.assertEqual(dests, ["images/logo.png", "images/mars.png"])
        self.assertTrue(items[0]["prompt"].lower().startswith("qwen-image:"))
        self.assertNotIn("qwen-image:", items[1]["prompt"].lower())
        self.assertEqual(items[0]["size"], "1024x1024")

    def test_explicit_size_on_extracted_dest_survives(self):
        blob = (
            '{"images":[{"filename":"header.png","subject":"rolling hills",'
            '"size":"1536x768"}]}'
        )
        items = plan_from_extracted("build a site", parse_plan_json(blob))
        self.assertEqual(items[0]["size"], "1536x768")
        self.assertTrue(items[0]["output_path"].endswith("header.png"))

    def test_empty_generate_does_not_invent_a_png(self):
        plan = parse_turn_plan('{"action":"generate","images":[]}')
        self.assertEqual(plan.action, "generate")
        self.assertEqual(plan.items, [])
        items = plan_from_extracted("create a website under tours", [])
        self.assertEqual(items, [])

    def test_reuse_and_none_have_no_dests(self):
        reuse = parse_turn_plan('{"action":"reuse","images":[]}')
        none = parse_turn_plan('{"action":"none","images":[]}')
        self.assertEqual(reuse.action, "reuse")
        self.assertEqual(reuse.items, [])
        self.assertEqual(none.action, "none")
        self.assertEqual(none.items, [])

    def test_legacy_images_json_without_action_is_generate(self):
        plan = parse_turn_plan(
            '{"images":[{"filename":"logo.png","subject":"logo Cafe"}]}'
        )
        self.assertEqual(plan.action, "generate")

    def test_fallback_item_still_names_generated_png(self):
        items = fallback_item("create a website under tours")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["output_path"].endswith("generated.png"))

    def test_planned_dest_facts_ask_for_code_before_pngs(self):
        text = planned_dest_fact_list(
            [
                {"output_path": "images/logo.png"},
                {"output_path": "images/mars.png"},
            ]
        )
        self.assertIn("images/logo.png", text)
        self.assertIn("images/mars.png", text)
        self.assertIn("Write or update every HTML/CSS/JS file", text)
        self.assertIn("after you finish the page", text)

    def test_classify_blob_includes_history_priors_and_this_turn(self):
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123",
                ),
                ChatCompletionMessage(
                    role="user",
                    content="implement the new images into the webpage",
                ),
            ]
        )
        blob = classify_blob(data, prior_facts="These PNG files exist at: images/logo.png.")
        self.assertIn("Already generated in this chat", blob)
        self.assertIn("images/logo.png", blob)
        self.assertIn("tabby-image-job: abc-123", blob)
        self.assertIn("This turn:", blob)
        self.assertIn("implement the new images into the webpage", blob)

    def test_classify_system_build_uses_plan_assets(self):
        self.assertIn("## Assets PNG/WebP dests", CLASSIFY_SYSTEM)
        self.assertIn("CSS-only plan whose Assets are None", CLASSIFY_SYSTEM)
        self.assertNotIn("Build of a CSS plan, questions", CLASSIFY_SYSTEM)
        self.assertIn("WIDTHxHEIGHT", CLASSIFY_SYSTEM)
        self.assertNotIn("real-world scene", CLASSIFY_SYSTEM)


class BuildPlanAssetRasterTests(unittest.TestCase):
    def test_assets_list_is_explicit(self):
        from ui.code_agent import BUILD_PROMPT

        plan = (
            "## Goal\nSite.\n## Files\n- index.html\n## Steps\n1. Write.\n"
            "## Assets\n- images/liner-alpha.png — luxury liner\n"
            "- images/liner-beta.png\n## Risks\nGPU time.\n"
        )
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(role="user", content="design a space site"),
                ChatCompletionMessage(role="assistant", content=plan),
                ChatCompletionMessage(
                    role="user",
                    content=f"{BUILD_PROMPT}\n\n<approved_plan>\n{plan}\n</approved_plan>",
                ),
            ]
        )
        self.assertEqual(
            _plan_asset_rasters(f"<approved_plan>\n{plan}\n</approved_plan>"),
            ["liner-alpha.png", "liner-beta.png"],
        )
        self.assertTrue(_explicit_new_rasters(data))

    def test_assets_none_is_not_explicit(self):
        from ui.code_agent import BUILD_PROMPT

        plan = (
            "## Goal\nCSS only.\n## Files\n- styles.css\n## Steps\n1. Tweak.\n"
            "## Assets\nNone\n## Risks\nNone.\n"
        )
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(role="assistant", content=plan),
                ChatCompletionMessage(
                    role="user",
                    content=f"{BUILD_PROMPT}\n\n<approved_plan>\n{plan}\n</approved_plan>",
                ),
            ]
        )
        self.assertEqual(_plan_asset_rasters(plan), [])
        self.assertFalse(_explicit_new_rasters(data))


class UpgradeMissingNamedDestsTests(unittest.TestCase):
    def test_gallery_attachment_vision_stays_none(self):
        ask = (
            "What is in the picture?\n\n"
            "Attached project image: `generated-20260831-075225-3604973.png`"
        )
        plan = ImageTurnPlan(action="none", items=[])
        upgraded = _upgrade_missing_named_dests(plan, ask, [])
        self.assertEqual(upgraded.action, "none")
        self.assertEqual(upgraded.items, [])
        self.assertEqual(_plan_asset_dests(ask), [])
        self.assertEqual(_named_image_dests(ask), [])


class CurlFromLivingFilesTests(unittest.TestCase):
    def test_curl_lists_only_files_that_exist(self):
        job = _job(
            items=[
                SimpleNamespace(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=["https://gpu.example/v1/images/generated-logo.png"],
                    status="done",
                ),
                SimpleNamespace(
                    prompt="mars",
                    output_path="images/mars.png",
                    urls=["https://gpu.example/v1/images/generated-mars.png"],
                    status="done",
                ),
            ]
        )

        def missing(url: str) -> bool:
            return "mars" in url

        with mock.patch("images.paths.gpu_generated_file_missing", side_effect=missing):
            pairs = living_download_pairs(job)
        self.assertEqual(
            pairs,
            [("https://gpu.example/v1/images/generated-logo.png", "images/logo.png")],
        )
        command = image_download_command(pairs)
        self.assertIn("generated-logo.png", command)
        self.assertNotIn("generated-mars.png", command)
        self.assertNotIn("sleep ", command)
        self.assertIn("--connect-timeout 15", command)
        self.assertIn("--parallel", command)
        self.assertIn("ls -l -- 'images/logo.png'", command)

    def test_running_job_has_no_urls_to_curl(self):
        job = _job(status="running", items=[
            SimpleNamespace(
                prompt="logo",
                output_path="images/logo.png",
                urls=[],
                status="running",
            )
        ])
        with mock.patch("images.paths.gpu_generated_file_missing", return_value=False):
            self.assertEqual(living_download_pairs(job), [])


class ChatHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_ask_plans_dests_via_mocked_json_not_regex(self):
        planned = [
            {"prompt": "qwen-image: logo that says Cosmos", "output_path": "images/logo.png"},
            {"prompt": "photograph of Mars", "output_path": "images/mars.png"},
        ]
        job = _job(status="queued")
        done = _job(
            status="done",
            items=[
                SimpleNamespace(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=["https://gpu.example/v1/images/generated-logo.png"],
                    status="done",
                ),
                SimpleNamespace(
                    prompt="mars",
                    output_path="images/mars.png",
                    urls=["https://gpu.example/v1/images/generated-mars.png"],
                    status="done",
                ),
            ],
        )

        async def finish(j):
            j.status = "done"
            j.items = done.items
            j.urls = [
                "https://gpu.example/v1/images/generated-logo.png",
                "https://gpu.example/v1/images/generated-mars.png",
            ]
            return j

        data = _user(
            "Create a website for Cosmos Tours with a logo and a photo of Mars."
        )
        with (
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(action="generate", items=planned)
                ),
            ),
            mock.patch("images.chat.active_mcp_image_job", return_value=None),
            mock.patch(
                "images.chat.start_mcp_image_job",
                new=mock.AsyncMock(return_value=(job, "started")),
            ) as start,
            mock.patch("images.chat.wait_until_done", side_effect=finish),
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            response = await handle(data, "https://gpu.example/v1")
        kwargs = start.await_args.kwargs
        dests = [row["output_path"] for row in kwargs["items"]]
        self.assertEqual(dests, ["images/logo.png", "images/mars.png"])
        self.assertTrue(kwargs["restore"])
        args = response.choices[0].message.tool_calls[0].function.arguments
        self.assertIn("generated-logo.png", args)
        self.assertIn("generated-mars.png", args)
        self.assertIn("tabby-image-job:", response.choices[0].message.content)
        self.assertNotIn("sleep ", args)

    async def test_fresh_chat_does_not_curl_a_leftover_job(self):
        leftover = _job(
            id="old-job",
            status="done",
            items=[
                SimpleNamespace(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=["https://gpu.example/v1/images/generated-old.png"],
                    status="done",
                )
            ],
        )
        data = _user("Create a website with a logo and header image")
        new_job = _job(id="new-job", status="queued")

        async def finish(j):
            j.status = "done"
            j.items = [
                SimpleNamespace(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=["https://gpu.example/v1/images/generated-new.png"],
                    status="done",
                )
            ]
            return j

        with (
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(
                        action="generate",
                        items=[{"prompt": "logo", "output_path": "images/logo.png"}],
                    )
                ),
            ),
            mock.patch("images.chat.active_mcp_image_job", return_value=None),
            mock.patch("images.chat.get_mcp_image_job", return_value=leftover),
            mock.patch(
                "images.chat.start_mcp_image_job",
                new=mock.AsyncMock(return_value=(new_job, "started")),
            ) as start,
            mock.patch("images.chat.wait_until_done", side_effect=finish),
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            response = await handle(data, "https://gpu.example/v1")
        start.assert_awaited()
        args = response.choices[0].message.tool_calls[0].function.arguments
        self.assertIn("generated-new.png", args)
        self.assertNotIn("generated-old.png", args)

    async def test_resume_hold_when_history_has_job_id(self):
        job = _job(id="abc-123", status="running", items=[
            SimpleNamespace(
                prompt="logo",
                output_path="images/logo.png",
                urls=[],
                status="running",
            )
        ])
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123",
                ),
            ]
        )
        self.assertEqual(job_id_from_history(data), "abc-123")

        async def finish(j):
            j.status = "done"
            j.items[0].urls = ["https://gpu.example/v1/images/generated-logo.png"]
            j.items[0].status = "done"
            return j

        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
            mock.patch("images.chat.wait_until_done", side_effect=finish),
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            response = await handle(data, "https://gpu.example/v1")
        start.assert_not_called()
        args = response.choices[0].message.tool_calls[0].function.arguments
        self.assertIn("generated-logo.png", args)

    async def test_menu_section_ask_starts_mixed_job_not_pillow(self):
        planned = [
            {"prompt": "pizza", "output_path": "images/classic-pizzas.png"},
            {"prompt": "sides", "output_path": "images/sides-appetizers.png"},
            {"prompt": "dessert", "output_path": "images/desserts.png"},
        ]
        job = _job(status="queued")

        async def finish(j):
            j.status = "done"
            j.items = [
                SimpleNamespace(
                    prompt=row["prompt"],
                    output_path=row["output_path"],
                    urls=[f"https://gpu.example/v1/images/generated-{i}.png"],
                    status="done",
                )
                for i, row in enumerate(planned)
            ]
            return j

        data = _user(
            "create images each of the menu sections and use them on the menu. "
            "Dont add words/text to the images. Dont use svg's"
        )
        with (
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(action="generate", items=planned)
                ),
            ),
            mock.patch("images.chat.active_mcp_image_job", return_value=None),
            mock.patch(
                "images.chat.start_mcp_image_job",
                new=mock.AsyncMock(return_value=(job, "started")),
            ) as start,
            mock.patch("images.chat.wait_until_done", side_effect=finish),
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            response = await handle(data, "https://gpu.example/v1")
        start.assert_awaited()
        dests = [row["output_path"] for row in start.await_args.kwargs["items"]]
        self.assertEqual(
            dests,
            [
                "images/classic-pizzas.png",
                "images/sides-appetizers.png",
                "images/desserts.png",
            ],
        )
        args = response.choices[0].message.tool_calls[0].function.arguments
        self.assertIn("generated-0.png", args)
        self.assertNotIn("Pillow", args)

    async def test_layout_followup_after_curl_does_not_start_a_job(self):
        job = _job(id="abc-123", status="done")
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123",
                    tool_calls=[],
                ),
                ChatCompletionMessage(
                    role="user",
                    content="Make the header 80px and fix the CSS grid.",
                ),
            ]
        )
        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(action="none", items=[])
                ),
            ),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
        ):
            response = await handle(data, "https://gpu.example/v1")
        self.assertIsNone(response)
        start.assert_not_called()

    async def test_implement_existing_images_is_reuse_not_a_new_job(self):
        job = _job(
            id="abc-123",
            status="done",
            item_urls=["https://gpu.example/v1/images/generated-logo.png"],
        )
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo and planet photos",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123",
                    tool_calls=[],
                ),
                ChatCompletionMessage(
                    role="user",
                    content="implement the new images into the webpage",
                ),
            ]
        )
        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(action="reuse", items=[])
                ),
            ) as classify,
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            response = await handle(data, "https://gpu.example/v1")
        self.assertIsNone(response)
        start.assert_not_called()
        classify.assert_awaited()
        self.assertIn(
            "These image files exist at:",
            data.messages[-1].content,
        )

    async def test_busy_other_job_does_not_hijack_a_fresh_chat(self):
        busy = _job(id="other", status="running")
        data = _user("Create a website with a logo and photos")
        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=None),
            mock.patch("images.chat.active_mcp_image_job", return_value=busy),
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(
                        action="generate",
                        items=[{"prompt": "logo", "output_path": "images/logo.png"}],
                    )
                ),
            ),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
        ):
            response = await handle(data, "https://gpu.example/v1")
        start.assert_not_called()
        self.assertIn("already generating", response.choices[0].message.content)

    async def test_mixed_ask_while_llm_loading_does_not_start_comfy(self):
        data = _user("Create a website with a logo and photos")
        classify = mock.AsyncMock(
            return_value=ImageTurnPlan(action="generate", items=[])
        )
        with (
            mock.patch("images.chat.active_mcp_image_job", return_value=None),
            mock.patch("images.chat.classify_image_turn", new=classify),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
        ):
            response = await handle(
                data, "https://gpu.example/v1", llm_ready=False, gpu_is_comfy=True
            )
        start.assert_not_called()
        classify.assert_not_awaited()
        self.assertIsNone(response)

    async def test_empty_generate_plan_does_not_start_a_job(self):
        data = _user("Create a website with a logo")
        with (
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(action="generate", items=[])
                ),
            ),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
        ):
            response = await handle(data, "https://gpu.example/v1")
        self.assertIsNone(response)
        start.assert_not_called()

    async def test_mixed_writes_code_before_starting_comfy(self):
        planned = [
            {"prompt": "logo", "output_path": "images/logo.png"},
        ]
        job = _job(id="job-code", status="queued")
        order: list[str] = []
        captured = {}

        async def fake_write(data, _handler):
            order.append("code")
            captured["user"] = data.messages[-1].content
            return _write_code_response()

        async def fake_start(**kwargs):
            order.append("remember" if not kwargs.get("start", True) else "images")
            return job, "coding" if not kwargs.get("start", True) else "started"

        data = _user("Create a website with a logo")
        with (
            mock.patch(
                "images.chat.classify_image_turn",
                new=mock.AsyncMock(
                    return_value=ImageTurnPlan(action="generate", items=planned)
                ),
            ),
            mock.patch("images.chat.active_mcp_image_job", return_value=None),
            mock.patch("images.chat._write_site_code", side_effect=fake_write),
            mock.patch(
                "images.chat.start_mcp_image_job",
                new=mock.AsyncMock(side_effect=fake_start),
            ) as start,
            mock.patch("images.chat.launch_mcp_image_job", new=mock.AsyncMock()) as launch,
            mock.patch("images.chat.wait_until_done", new=mock.AsyncMock()) as wait,
        ):
            response = await handle(data, "https://gpu.example/v1")
        self.assertEqual(order, ["code", "remember"])
        self.assertFalse(start.await_args.kwargs.get("start", True))
        launch.assert_not_awaited()
        wait.assert_not_awaited()
        self.assertIn("images/logo.png", captured["user"])
        self.assertIn("Write or update every HTML/CSS/JS file", captured["user"])
        message = response.choices[0].message
        self.assertEqual(message.tool_calls[0].function.name, "Write")
        self.assertIn("index.html", message.tool_calls[0].function.arguments)
        self.assertIn("tabby-image-job: job-code", message.content)
        self.assertNotIn("curl ", message.tool_calls[0].function.arguments)

    async def test_coding_followup_keeps_llm_for_another_write(self):
        job = _job(id="abc-123", status="coding", code_turns=1)
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123\nwriting the page",
                    tool_calls=[
                        ToolCall(
                            function=Tool(
                                name="Write",
                                arguments='{"path":"index.html","contents":"<html></html>"}',
                            )
                        )
                    ],
                ),
                ChatCompletionMessage(
                    role="tool",
                    content="wrote index.html",
                    tool_call_id="call_1",
                ),
            ]
        )

        async def fake_write(_data, _handler):
            return _write_code_response(path="styles.css")

        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch("images.chat.note_coding_progress", return_value=2),
            mock.patch("images.chat._write_site_code", side_effect=fake_write),
            mock.patch("images.chat.launch_mcp_image_job", new=mock.AsyncMock()) as launch,
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
            mock.patch("images.chat.wait_until_done", new=mock.AsyncMock()) as wait,
        ):
            job.code_turns = 2
            response = await handle(data, "https://gpu.example/v1")
        start.assert_not_called()
        launch.assert_not_awaited()
        wait.assert_not_awaited()
        message = response.choices[0].message
        self.assertEqual(message.tool_calls[0].function.name, "Write")
        self.assertIn("styles.css", message.tool_calls[0].function.arguments)
        self.assertNotIn("curl ", message.tool_calls[0].function.arguments)

    async def test_coding_followup_without_file_tools_starts_comfy(self):
        job = _job(
            id="abc-123",
            status="coding",
            code_turns=2,
            items=[
                SimpleNamespace(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=[],
                    status="queued",
                )
            ],
        )
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123",
                ),
                ChatCompletionMessage(
                    role="tool",
                    content="wrote index.html",
                    tool_call_id="call_1",
                ),
            ]
        )

        async def fake_write(_data, _handler):
            return ChatCompletionResponse(
                model="gpt-4o",
                choices=[
                    ChatCompletionRespChoice(
                        finish_reason="stop",
                        message=ChatCompletionMessage(
                            role="assistant",
                            content="page is ready",
                        ),
                    )
                ],
            )

        async def finish(j):
            j.status = "done"
            j.items[0].urls = ["https://gpu.example/v1/images/generated-logo.png"]
            j.items[0].status = "done"
            return j

        async def fake_launch(j, delay=None):
            j.status = "queued"
            return j

        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch("images.chat.note_coding_progress", return_value=3),
            mock.patch("images.chat._write_site_code", side_effect=fake_write),
            mock.patch(
                "images.chat.launch_mcp_image_job",
                new=mock.AsyncMock(side_effect=fake_launch),
            ) as launch,
            mock.patch("images.chat.wait_until_done", side_effect=finish),
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            job.code_turns = 3
            response = await handle(data, "https://gpu.example/v1")
        launch.assert_awaited()
        args = response.choices[0].message.tool_calls[0].function.arguments
        self.assertIn("generated-logo.png", args)

    async def test_coding_followup_holds_until_named_pages_exist(self):
        job = _job(id="abc-123", status="coding", code_turns=2)
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content=(
                        "Create a website with index.html, tours.html, "
                        "about.html, and visit.html plus a logo"
                    ),
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123",
                ),
                ChatCompletionMessage(
                    role="tool",
                    content="wrote index.html",
                    tool_call_id="call_1",
                ),
            ]
        )
        replies = [
            ChatCompletionResponse(
                model="gpt-4o",
                choices=[
                    ChatCompletionRespChoice(
                        finish_reason="stop",
                        message=ChatCompletionMessage(
                            role="assistant",
                            content="page is ready",
                        ),
                    )
                ],
            ),
            _write_code_response(path="tours.html"),
        ]

        async def fake_write(_data, _handler):
            return replies.pop(0)

        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch("images.chat.note_coding_progress", return_value=3),
            mock.patch("images.chat._write_site_code", side_effect=fake_write),
            mock.patch("images.chat.launch_mcp_image_job", new=mock.AsyncMock()) as launch,
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
            mock.patch("images.chat.wait_until_done", new=mock.AsyncMock()) as wait,
        ):
            job.code_turns = 3
            response = await handle(data, "https://gpu.example/v1")
        start.assert_not_called()
        launch.assert_not_awaited()
        wait.assert_not_awaited()
        message = response.choices[0].message
        self.assertEqual(message.tool_calls[0].function.name, "Write")
        self.assertIn("tours.html", message.tool_calls[0].function.arguments)
        self.assertEqual(replies, [])

    async def test_running_job_without_id_does_not_say_no_llm(self):
        busy = _job(id="abc-123", status="running")
        data = _user("please write a title for this conversation")
        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=None),
            mock.patch("images.chat.active_mcp_image_job", return_value=busy),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
        ):
            response = await handle(
                data, "https://gpu.example/v1", llm_ready=False, gpu_is_comfy=False
            )
        start.assert_not_called()
        self.assertIn("still rendering", response.choices[0].message.content)
        self.assertNotIn("No LLM is loaded", response.choices[0].message.content)

    async def test_mixed_followup_after_code_holds_for_curl(self):
        job = _job(
            id="abc-123",
            status="running",
            items=[
                SimpleNamespace(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=[],
                    status="running",
                )
            ],
        )
        data = ChatCompletionRequest(
            messages=[
                ChatCompletionMessage(
                    role="user",
                    content="Create a website with a logo",
                ),
                ChatCompletionMessage(
                    role="assistant",
                    content="tabby-image-job: abc-123\nwriting the page",
                    tool_calls=[
                        ToolCall(
                            function=Tool(
                                name="Write",
                                arguments='{"path":"index.html","contents":"<html></html>"}',
                            )
                        )
                    ],
                ),
                ChatCompletionMessage(
                    role="tool",
                    content="wrote index.html",
                    tool_call_id="call_1",
                ),
            ]
        )

        async def finish(j):
            j.status = "done"
            j.items[0].urls = ["https://gpu.example/v1/images/generated-logo.png"]
            j.items[0].status = "done"
            return j

        with (
            mock.patch("images.chat.get_mcp_image_job", return_value=job),
            mock.patch("images.chat.start_mcp_image_job", new=mock.AsyncMock()) as start,
            mock.patch("images.chat.wait_until_done", side_effect=finish),
            mock.patch("images.paths.gpu_generated_file_missing", return_value=False),
        ):
            response = await handle(data, "https://gpu.example/v1")
        start.assert_not_called()
        args = response.choices[0].message.tool_calls[0].function.arguments
        self.assertIn("generated-logo.png", args)
        self.assertIn("images/logo.png", args)


class McpWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_tool_waits_until_job_is_done(self):
        from common.mcp_images import run_generate_tool

        job = _job(id="mcp-1", status="queued")

        async def finish(j):
            j.status = "done"
            j.items[0].urls = ["https://gpu.example/v1/images/generated-logo.png"]
            return j

        with (
            mock.patch(
                "images.jobs.start_mcp_image_job",
                new=mock.AsyncMock(return_value=(job, "started")),
            ),
            mock.patch("images.jobs.wait_until_done", side_effect=finish),
            mock.patch("images.jobs.get_mcp_image_job", return_value=None),
            mock.patch("images.jobs.loaded_tabby_name", return_value="qwen"),
            mock.patch(
                "common.gpu_mode.public_api_base", return_value="https://gpu.example/v1"
            ),
            mock.patch(
                "common.mcp_images.format_mcp_job_text",
                return_value="Job mcp-1: done\nhttps://gpu.example/v1/images/generated-logo.png",
            ),
        ):
            result = await run_generate_tool(
                {"prompt": "qwen-image: logo", "output_path": "images/logo.png"}
            )
        self.assertIn("generated-logo.png", result["content"][0]["text"])
        self.assertFalse(result["isError"])


class LiveCodeStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_response_from_stream_payloads(self):
        from images.chat import _response_from_stream_payloads

        response = _response_from_stream_payloads(
            [
                {"choices": [{"delta": {"content": "hi "}}]},
                {
                    "model": "qwen",
                    "choices": [
                        {"delta": {"content": "there"}, "finish_reason": "stop"}
                    ],
                },
            ],
            "base",
        )
        self.assertEqual(response.choices[0].message.content, "hi there")
        self.assertEqual(response.model, "qwen")

    async def test_write_site_code_streams_to_bound_flight(self):
        from images.chat import _write_site_code
        from ui.flight import (
            ConsoleFlight,
            bind_console_flight,
            reset_for_tests,
            unbind_console_flight,
        )

        reset_for_tests()
        flight = ConsoleFlight("u", "c", "code", "landing")
        token = bind_console_flight(flight)
        chunks = [
            '{"choices":[{"delta":{"content":"writing "}}],"model":"qwen"}',
            '{"choices":[{"delta":{"content":"the page"},"finish_reason":"stop"}]}',
            "[DONE]",
        ]

        async def fake_stream(*args, **kwargs):
            for chunk in chunks:
                yield chunk

        container = SimpleNamespace(
            loaded=True,
            prompt_template="tpl",
            model_dir=SimpleNamespace(name="qwen"),
        )
        try:
            with (
                mock.patch("common.model.container", container),
                mock.patch(
                    "endpoints.OAI.utils.chat_completion.apply_chat_template",
                    new=mock.AsyncMock(return_value=("prompt", None)),
                ),
                mock.patch(
                    "endpoints.OAI.utils.chat_completion.stream_generate_chat_completion",
                    new=fake_stream,
                ),
                mock.patch(
                    "endpoints.OAI.utils.chat_completion.generate_chat_completion",
                    new=mock.AsyncMock(),
                ) as blocked,
            ):
                response = await _write_site_code(_user("Create a landing page"), None)
        finally:
            unbind_console_flight(token)
            reset_for_tests()
        blocked.assert_not_awaited()
        self.assertTrue(flight.streamed_live)
        self.assertEqual(response.choices[0].message.content, "writing the page")
        self.assertIn("writing ", flight.assembled)


if __name__ == "__main__":
    unittest.main()
