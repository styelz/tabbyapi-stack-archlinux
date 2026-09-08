import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.mcp_images import (
    GET_JOB_NAME,
    TOOL_NAME,
    dispatch,
    format_mcp_job_text,
    initialize_result,
    list_tools_result,
    normalize_prompt,
    parse_image_items,
)


class McpImagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from endpoints.core.image_jobs import reset_mcp_image_jobs_for_tests
        from ui.occupancy import reset_for_tests as reset_occupancy

        reset_occupancy()

        self._tmpdir = tempfile.TemporaryDirectory()
        self._gallery_patch = mock.patch(
            "common.gpu_mode.GENERATED_DIR", Path(self._tmpdir.name)
        )
        self._gallery_patch.start()
        await reset_mcp_image_jobs_for_tests()

    async def asyncTearDown(self):
        from endpoints.core.image_jobs import reset_mcp_image_jobs_for_tests

        await reset_mcp_image_jobs_for_tests()
        self._gallery_patch.stop()
        self._tmpdir.cleanup()

    def test_tools_list_exposes_generate_image(self):
        names = [tool["name"] for tool in list_tools_result()["tools"]]
        self.assertEqual(names, [TOOL_NAME, GET_JOB_NAME])
        self.assertIn("qwen-image", initialize_result()["instructions"])
        self.assertIn("images array", initialize_result()["instructions"])

    def test_qwen_prefix(self):
        self.assertEqual(
            normalize_prompt({"prompt": "a cafe logo", "qwen_image": True}),
            "qwen-image: a cafe logo",
        )
        self.assertEqual(
            normalize_prompt({"prompt": "qwen-image: SALE", "qwen_image": True}),
            "qwen-image: SALE",
        )

    def test_parse_image_items(self):
        items = parse_image_items(
            {
                "images": [
                    {
                        "prompt": "a cafe logo",
                        "output_path": "images/logo.png",
                        "qwen_image": True,
                    },
                    {"prompt": "a header banner", "output_path": "images/header.png"},
                ]
            }
        )
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["prompt"].lower().startswith("qwen-image:"))
        self.assertIn("cafe logo", items[0]["prompt"].lower())
        self.assertIn("isolated logo mark", items[0]["prompt"].lower())
        self.assertNotIn("website", items[0]["prompt"].lower())
        self.assertEqual(items[0]["output_path"], "images/logo.png")
        self.assertEqual(items[1]["output_path"], "images/header.png")
        abs_items = parse_image_items(
            {
                "images": [
                    {
                        "prompt": "a cafe logo",
                        "output_path": "/home/pbp/Cursor/llm-test/pbptours/images/logo.png",
                    },
                    {
                        "prompt": "photograph of planet Mercury",
                        "output_path": "/home/pbp/Cursor/llm-test/pbptours/images/mercury.png",
                    },
                ]
            }
        )
        self.assertEqual(abs_items[0]["output_path"], "pbptours/images/logo.png")
        self.assertEqual(abs_items[1]["output_path"], "pbptours/images/mercury.png")
        guessed = parse_image_items(
            {
                "images": [
                    {"prompt": "qwen-image: a cafe logo"},
                    {"prompt": "a header banner"},
                    {"prompt": "a red cube"},
                    {"prompt": "a blue cube"},
                ]
            }
        )
        self.assertEqual(guessed[0]["output_path"], "images/generated.png")
        self.assertEqual(guessed[1]["output_path"], "images/generated-2.png")
        self.assertEqual(guessed[2]["output_path"], "images/generated-3.png")
        self.assertEqual(guessed[3]["output_path"], "images/generated-4.png")
        self.assertIn("no website", guessed[1]["prompt"])

    def test_running_job_text_hides_finished_item_urls(self):
        """A partial batch must not leak generated-*.png URLs to the 9B."""
        logo = mock.Mock(
            output_path="images/logo.png",
            urls=["https://gpu.example/v1/images/generated-20260822-000945-234740.png"],
            prompt="logo that says Cosmos Tours",
            status="done",
        )
        mars = mock.Mock(
            output_path="images/mars.png",
            urls=[],
            prompt="photograph of planet Mars",
            status="running",
        )
        job = mock.Mock(
            id="job-partial",
            status="running",
            phase="generating",
            wait_text="About 12 minutes.",
            wait_s=720,
            prompt="2 images",
            output_path="images/",
            items=[logo, mars],
            urls=[logo.urls[0]],
            error="",
            started_at=0,
            current_index=1,
            done_count=1,
            count=2,
        )
        text = format_mcp_job_text(job)
        self.assertIn("[done] images/logo.png", text)
        self.assertIn("[running] images/mars.png", text)
        self.assertNotIn("generated-20260822-000945-234740.png", text)
        self.assertNotIn("https://gpu.example", text)
        self.assertIn("Do not invent generated-*.png URLs", text)
        done_job = mock.Mock(
            id="job-done",
            status="done",
            wait_text="About 4 minutes.",
            wait_s=240,
            prompt="2 images",
            output_path="images/",
            items=[logo, mars],
            urls=[logo.urls[0]],
            error="",
            started_at=0,
            current_index=1,
            done_count=2,
            count=2,
        )
        mars.urls = ["https://gpu.example/v1/images/generated-20260822-001312-234741.png"]
        mars.status = "done"
        done_text = format_mcp_job_text(done_job)
        self.assertIn("generated-20260822-000945-234740.png", done_text)
        self.assertIn("generated-20260822-001312-234741.png", done_text)

    async def test_initialize_and_list(self):
        init = await dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            }
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "tabby-images")
        listed = await dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(listed["result"]["tools"][0]["name"], "generate_image")
        self.assertEqual(listed["result"]["tools"][1]["name"], "get_image_job")
        schema = listed["result"]["tools"][0]["inputSchema"]["properties"]
        self.assertIn("images", schema)
        self.assertIn("wait_s", listed["result"]["tools"][1]["inputSchema"]["properties"])

    async def test_notification_has_no_payload(self):
        self.assertIsNone(
            await dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    async def test_unknown_method(self):
        reply = await dispatch({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(reply["error"]["code"], -32601)

    async def test_unknown_tool_is_tool_error(self):
        reply = await dispatch(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "browser_navigate", "arguments": {}},
            }
        )
        self.assertTrue(reply["result"]["isError"])

    def _patch_slow_job(self):
        png = mock.Mock()
        png.name = "generated-logo.png"
        gate = asyncio.Event()

        async def slow_render(*_args, **_kwargs):
            await gate.wait()
            return [png]

        patches = (
            mock.patch("images.jobs.MCP_HANDOFF_DELAY_S", 0),
            mock.patch("images.jobs._render_specs", new=slow_render),
            mock.patch("images.jobs.ensure_comfy", new=mock.AsyncMock()),
            mock.patch("images.jobs.reload_last_llm", new=mock.AsyncMock()),
            mock.patch("images.jobs.loaded_tabby_name", return_value="qwen"),
            mock.patch(
                "common.gpu_mode.public_api_base",
                return_value="https://gpu.example/v1",
            ),
            mock.patch(
                "common.gpu_mode.public_image_url",
                return_value="https://gpu.example/v1/images/generated-logo.png",
            ),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return gate

    async def _call_generate(self, rpc_id=5, **arguments):
        return await dispatch(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {
                    "name": "generate_image",
                    "arguments": arguments
                    or {
                        "prompt": "qwen-image: Cafe logo",
                        "output_path": "images/logo.png",
                    },
                },
            }
        )

    async def _poll(self, rpc_id=6, **arguments):
        args = {"wait_s": 0, **arguments}
        return await dispatch(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {"name": "get_image_job", "arguments": args},
            }
        )

    async def test_generate_waits_until_gpu_finishes(self):
        gate = self._patch_slow_job()

        async def open_gate():
            await asyncio.sleep(0.05)
            gate.set()

        opener = asyncio.create_task(open_gate())
        started = await self._call_generate(
            prompt="qwen-image: Cafe logo",
            output_path="images/logo.png",
        )
        await opener
        text = started["result"]["content"][0]["text"]
        self.assertIn("generated-logo.png", text)
        self.assertIn("images/logo.png", text)
        self.assertFalse(started["result"]["isError"])
        self.assertNotIn("b64_json", text)

    async def test_second_generate_appends_to_the_same_batch(self):
        gate = self._patch_slow_job()
        first_task = asyncio.create_task(
            self._call_generate(prompt="qwen-image: Cafe logo")
        )
        await asyncio.sleep(0.05)
        again_task = asyncio.create_task(
            self._call_generate(
                rpc_id=8,
                prompt="a cafe interior",
                output_path="images/hero.png",
            )
        )
        await asyncio.sleep(0.05)
        gate.set()
        first = await first_task
        again = await again_task
        again_text = again["result"]["content"][0]["text"]
        first_text = first["result"]["content"][0]["text"]
        self.assertTrue(
            "Added to the same GPU batch" in again_text or "2 image" in again_text
        )
        self.assertIn("2 image", first_text + again_text)

    async def test_empty_owner_does_not_append_across_clients(self):
        from images.jobs import start_mcp_image_job, wait_until_done

        gate = self._patch_slow_job()
        first, kind = await start_mcp_image_job(
            prompt="qwen-image: Cafe logo",
            output_path="images/logo.png",
            size="1024x1024",
            count=1,
            seed=None,
            restore=True,
            api_base="https://gpu.example/v1",
            delay=0.0,
            owner="",
        )
        self.assertEqual(kind, "started")
        second, kind2 = await start_mcp_image_job(
            prompt="a cafe interior",
            output_path="images/hero.png",
            size="1024x1024",
            count=1,
            seed=None,
            restore=True,
            api_base="https://gpu.example/v1",
            delay=0.0,
            owner="",
        )
        self.assertEqual(kind2, "busy")
        self.assertEqual(second.id, first.id)
        gate.set()
        await wait_until_done(first)

    async def test_images_array_is_one_job(self):
        gate = self._patch_slow_job()

        async def open_gate():
            await asyncio.sleep(0.05)
            gate.set()

        opener = asyncio.create_task(open_gate())
        started = await self._call_generate(
            images=[
                {"prompt": "qwen-image: Cafe logo", "output_path": "images/logo.png"},
                {"prompt": "header banner", "output_path": "images/header.png"},
                {"prompt": "latte art", "output_path": "images/latte.png"},
            ]
        )
        await opener
        text = started["result"]["content"][0]["text"]
        self.assertIn("images/logo.png", text)
        self.assertIn("images/header.png", text)
        self.assertIn("3 image", text)

    async def test_missing_prompt(self):
        reply = await dispatch(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "generate_image", "arguments": {}},
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("prompt is required", reply["result"]["content"][0]["text"])

    async def test_get_is_405(self):
        from endpoints.core.mcp import mcp_get

        response = await mcp_get()
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers.get("allow"), "POST")


if __name__ == "__main__":
    unittest.main()
