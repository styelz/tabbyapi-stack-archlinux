import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from endpoints.core.image_jobs import generate_images_job, loaded_tabby_name


class ImageJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_restores_llm_after_handoff(self):
        png = Path("/tmp/generated-1.png")
        with (
            mock.patch("images.jobs.loaded_tabby_name", return_value="qwen"),
            mock.patch("images.jobs.last_profile", return_value="qwen"),
            mock.patch("images.jobs.ensure_comfy", new=mock.AsyncMock()) as ensure,
            mock.patch("images.jobs.reload_last_llm", new=mock.AsyncMock()) as reload,
            mock.patch("images.jobs.generate_image", return_value=b"\x89PNG"),
            mock.patch("images.jobs.save_generated_image", return_value=png),
        ):
            saved = await generate_images_job("a red cube", restore=True)
        self.assertEqual(saved, [png])
        ensure.assert_awaited_once()
        reload.assert_awaited_once_with("qwen")

    async def test_generate_stays_on_comfy_when_not_restoring(self):
        png = Path("/tmp/generated-2.png")
        with (
            mock.patch("images.jobs.loaded_tabby_name", return_value=None),
            mock.patch("images.jobs.last_profile", return_value="qwen"),
            mock.patch("images.jobs.ensure_comfy", new=mock.AsyncMock()) as ensure,
            mock.patch("images.jobs.reload_last_llm", new=mock.AsyncMock()) as reload,
            mock.patch("images.jobs.generate_image", return_value=b"\x89PNG"),
            mock.patch("images.jobs.save_generated_image", return_value=png),
        ):
            saved = await generate_images_job("a red cube", restore=False)
        self.assertEqual(saved, [png])
        ensure.assert_awaited_once()
        reload.assert_not_awaited()

    async def test_ensure_comfy_stops_llama_first(self):
        from images.jobs import ensure_comfy

        order = []

        def stop_llama(*_a, **_k):
            order.append("llama")

        def start_comfy(*_a, **_k):
            order.append("comfy")

        with (
            mock.patch("sidecar.settings.is_sidecar_process", return_value=False),
            mock.patch("images.jobs.comfy_up", side_effect=[False, False, True]),
            mock.patch("images.jobs.loaded_tabby_name", return_value=None),
            mock.patch("images.jobs.llama_up", return_value=True),
            mock.patch("images.jobs.stop_llama", stop_llama),
            mock.patch("images.jobs.start_comfy_if_needed", start_comfy),
            mock.patch("images.jobs.write_mode"),
            mock.patch("common.phrase_switch.set_switch_lock"),
            mock.patch("common.phrase_switch.clear_switch_lock"),
            mock.patch("common.switch_times.record_ready"),
        ):
            await ensure_comfy()
        self.assertEqual(order, ["llama", "comfy"])

    async def test_generate_items_restore_once(self):
        png = Path("/tmp/generated-3.png")
        with (
            mock.patch("images.jobs.loaded_tabby_name", return_value="qwen"),
            mock.patch("images.jobs.last_profile", return_value="qwen"),
            mock.patch("images.jobs.ensure_comfy", new=mock.AsyncMock()) as ensure,
            mock.patch("images.jobs.reload_last_llm", new=mock.AsyncMock()) as reload,
            mock.patch("images.jobs.generate_image", return_value=b"\x89PNG"),
            mock.patch("images.jobs.save_generated_image", return_value=png),
        ):
            saved = await generate_images_job(
                items=[
                    {"prompt": "qwen-image: logo", "size": "1024x1024"},
                    {"prompt": "a cafe interior"},
                    {"prompt": "latte art"},
                ],
                restore=True,
            )
        self.assertEqual(saved, [png, png, png])
        ensure.assert_awaited_once()
        reload.assert_awaited_once_with("qwen")

    def test_loaded_name_requires_a_ready_container(self):
        with mock.patch("images.jobs.model") as model_mod:
            model_mod.container = None
            with mock.patch("sidecar.settings.is_sidecar_process", return_value=False):
                self.assertIsNone(loaded_tabby_name())

    def test_loaded_name_uses_backend_when_sidecar(self):
        with mock.patch("sidecar.settings.is_sidecar_process", return_value=True):
            with mock.patch("sidecar.model_status.loaded_model_id", return_value="Qwen3.5-9B"):
                self.assertEqual(loaded_tabby_name(), "Qwen3.5-9B")

    def test_batch_wait_adds_renders_not_extra_llm_reloads(self):
        from common.phrase_switch import image_job_wait_seconds

        one = image_job_wait_seconds("a red cube", restore=True, count=1)
        three = image_job_wait_seconds(
            prompts=["a red cube", "a blue cube", "a green cube"], restore=True
        )
        self.assertGreater(three, one)
        self.assertLess(three, one * 3)

    def test_job_json_includes_paths_and_urls(self):
        from endpoints.core.image_jobs import McpImageItem, McpImageJob, mcp_job_to_dict

        job = McpImageJob(
            id="job-json",
            items=[
                McpImageItem(
                    prompt="logo",
                    output_path="images/logo.png",
                    urls=["https://gpu.example/v1/images/a.png"],
                    status="done",
                )
            ],
            restore=True,
            api_base="https://gpu.example/v1",
            wait_text="about 4 minutes",
            wait_s=240,
            status="done",
            phase="done",
            urls=["https://gpu.example/v1/images/a.png"],
        )
        payload = mcp_job_to_dict(job)
        self.assertEqual(payload["id"], "job-json")
        self.assertEqual(payload["items"][0]["output_path"], "images/logo.png")
        self.assertEqual(payload["urls"][0], "https://gpu.example/v1/images/a.png")
        self.assertFalse(payload["client_saved"])

    async def test_finished_job_survives_a_restart(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _persist_jobs,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-restart-done",
                    items=[
                        McpImageItem(
                            prompt="qwen-image: logo",
                            output_path="images/logo.png",
                            urls=["https://gpu.example/v1/images/a.png"],
                            status="done",
                        )
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="about 4 minutes",
                    wait_s=240,
                    status="done",
                    phase="done",
                    urls=["https://gpu.example/v1/images/a.png"],
                    client_saved=False,
                )
                from endpoints.core.image_jobs import _MCP_JOBS, _MCP_ORDER

                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                _persist_jobs()

                # Simulate the process restarting: RAM state is gone, disk isn't.
                await reset_mcp_image_jobs_for_tests()
                recovered = get_mcp_image_job("job-restart-done")
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.status, "done")
                self.assertEqual(recovered.urls, ["https://gpu.example/v1/images/a.png"])
                self.assertFalse(recovered.client_saved)
                await reset_mcp_image_jobs_for_tests()

    async def test_finished_job_keeps_client_saved_across_restart(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _persist_jobs,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-restart-saved",
                    items=[
                        McpImageItem(
                            prompt="qwen-image: logo",
                            output_path="images/logo.png",
                            urls=["https://gpu.example/v1/images/a.png"],
                            status="done",
                        )
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="about 4 minutes",
                    wait_s=240,
                    status="done",
                    phase="done",
                    urls=["https://gpu.example/v1/images/a.png"],
                    client_saved=True,
                )
                from endpoints.core.image_jobs import _MCP_JOBS, _MCP_ORDER

                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                _persist_jobs()
                await reset_mcp_image_jobs_for_tests()
                recovered = get_mcp_image_job("job-restart-saved")
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.status, "done")
                self.assertTrue(recovered.client_saved)
                await reset_mcp_image_jobs_for_tests()

    async def test_interrupted_job_keeps_finished_items_and_resumes(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _persist_jobs,
            active_mcp_image_job,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-restart-partial",
                    items=[
                        McpImageItem(
                            prompt="qwen-image: logo",
                            output_path="images/logo.png",
                            urls=["https://gpu.example/v1/images/a.png"],
                            status="done",
                        ),
                        McpImageItem(
                            prompt="a cafe interior",
                            output_path="images/hero.png",
                            status="queued",
                        ),
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="about 4 minutes",
                    wait_s=240,
                    status="running",
                    phase="generating",
                    urls=["https://gpu.example/v1/images/a.png"],
                )
                from endpoints.core.image_jobs import _MCP_JOBS, _MCP_ORDER

                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                _persist_jobs()

                await reset_mcp_image_jobs_for_tests()
                active = active_mcp_image_job()
                self.assertIsNotNone(active)
                self.assertEqual(active.id, "job-restart-partial")
                recovered = get_mcp_image_job("job-restart-partial")
                self.assertEqual(recovered.status, "queued")
                self.assertEqual(recovered.error, "")
                self.assertEqual(recovered.urls, ["https://gpu.example/v1/images/a.png"])
                self.assertEqual(recovered.items[0].status, "done")
                self.assertEqual(recovered.items[1].status, "queued")
                disk = json.loads((Path(raw) / "mcp_jobs.json").read_text(encoding="utf-8"))
                self.assertEqual(disk[-1]["status"], "queued")
                self.assertEqual(disk[-1]["items"][1]["status"], "queued")
                await reset_mcp_image_jobs_for_tests()

    async def test_coding_job_is_abandoned_on_restart(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _persist_jobs,
            active_mcp_image_job,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-coding",
                    items=[
                        McpImageItem(
                            prompt="qwen-image: logo",
                            output_path="images/logo.png",
                        )
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="coding",
                    phase="writing_code",
                    code_turns=2,
                )
                from endpoints.core.image_jobs import _MCP_JOBS, _MCP_ORDER

                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                _persist_jobs()
                await reset_mcp_image_jobs_for_tests()
                recovered = get_mcp_image_job("job-coding")
                self.assertEqual(recovered.status, "error")
                self.assertIn("restarted", (recovered.error or "").lower())
                self.assertIsNone(active_mcp_image_job())
                await reset_mcp_image_jobs_for_tests()

    async def test_foreign_coding_job_is_abandoned_for_a_new_chat(self):
        from endpoints.core.image_jobs import (
            CODING_ABANDON_REASON,
            McpImageItem,
            McpImageJob,
            abandon_foreign_coding_job,
            active_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
            start_mcp_image_job,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                leftover = McpImageJob(
                    id="844cd0b4-0704-45bf-8b07-9d18d18fa950",
                    items=[
                        McpImageItem(
                            prompt="logo",
                            output_path="images/logo.png",
                        )
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="coding",
                    phase="writing_code",
                    code_turns=1,
                )
                from endpoints.core.image_jobs import _MCP_JOBS, _MCP_ORDER

                _MCP_JOBS[leftover.id] = leftover
                _MCP_ORDER.append(leftover.id)
                self.assertTrue(
                    abandon_foreign_coding_job(
                        leftover, owner="pbp", chat_id="ws-fresh"
                    )
                )
                self.assertEqual(leftover.status, "error")
                self.assertIn("another chat", leftover.error)
                self.assertIsNone(active_mcp_image_job())
                self.assertFalse(
                    abandon_foreign_coding_job(
                        leftover, owner="pbp", chat_id="ws-fresh"
                    )
                )

                same = McpImageJob(
                    id="same-ws",
                    items=[
                        McpImageItem(
                            prompt="header",
                            output_path="images/header.png",
                        )
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="coding",
                    phase="writing_code",
                    owner="pbp",
                    chat_id="ws-fresh",
                )
                self.assertFalse(
                    abandon_foreign_coding_job(
                        same, owner="pbp", chat_id="ws-fresh"
                    )
                )
                self.assertEqual(same.status, "coding")

                ghost = McpImageJob(
                    id="ghost-coding",
                    items=[
                        McpImageItem(
                            prompt="logo",
                            output_path="images/logo.png",
                        )
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="coding",
                    phase="writing_code",
                )
                _MCP_JOBS[ghost.id] = ghost
                _MCP_ORDER.append(ghost.id)
                with (
                    mock.patch(
                        "images.jobs.launch_mcp_image_job", new=mock.AsyncMock()
                    ) as launch,
                    mock.patch(
                        "images.jobs.restore_llm_profile", return_value="qwen"
                    ),
                    mock.patch("images.jobs.refresh_job_wait"),
                ):
                    job, kind = await start_mcp_image_job(
                        seed=None,
                        restore=True,
                        api_base="https://gpu.example/v1",
                        items=[{"prompt": "logo", "output_path": "images/logo.png"}],
                        owner="pbp",
                        chat_id="ws-fresh",
                    )
                self.assertEqual(kind, "started")
                self.assertNotEqual(job.id, ghost.id)
                self.assertEqual(ghost.status, "error")
                self.assertEqual(ghost.error, CODING_ABANDON_REASON)
                launch.assert_awaited()
                await reset_mcp_image_jobs_for_tests()

    async def test_persisted_error_job_clears_unfinished_items(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _MCP_JOBS,
            _MCP_ORDER,
            _persist_jobs,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-error-leftover-items",
                    items=[
                        McpImageItem(
                            prompt="hero",
                            output_path="images/hero.png",
                            status="done",
                            urls=["https://gpu.example/v1/images/a.png"],
                        ),
                        McpImageItem(
                            prompt="logo",
                            output_path="images/logo.png",
                            status="running",
                        ),
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="error",
                    phase="error",
                    error="Image job was cancelled",
                    urls=["https://gpu.example/v1/images/a.png"],
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                _persist_jobs()
                await reset_mcp_image_jobs_for_tests()
                recovered = get_mcp_image_job("job-error-leftover-items")
                self.assertEqual(recovered.status, "error")
                self.assertEqual(recovered.items[0].status, "done")
                self.assertEqual(recovered.items[1].status, "error")
                await reset_mcp_image_jobs_for_tests()

    async def test_restart_abandon_error_resumes_unfinished_items(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            RESTART_ABANDON_REASON,
            _MCP_JOBS,
            _MCP_ORDER,
            _persist_jobs,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-restart-abandon-partial",
                    items=[
                        McpImageItem(
                            prompt="logo",
                            output_path="images/logo.png",
                            status="done",
                            urls=["https://gpu.example/v1/images/a.png"],
                        ),
                        McpImageItem(
                            prompt="hero",
                            output_path="images/hero.png",
                            status="error",
                            error=RESTART_ABANDON_REASON,
                        ),
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="error",
                    phase="error",
                    error=f"{RESTART_ABANDON_REASON} 1/2 image(s) already rendered — those URLs are still below.",
                    urls=["https://gpu.example/v1/images/a.png"],
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                _persist_jobs()
                await reset_mcp_image_jobs_for_tests()
                recovered = get_mcp_image_job("job-restart-abandon-partial")
                self.assertEqual(recovered.status, "queued")
                self.assertEqual(recovered.items[0].status, "done")
                self.assertEqual(recovered.items[1].status, "queued")
                self.assertEqual(recovered.error, "")
                await reset_mcp_image_jobs_for_tests()

    async def test_abandon_inflight_jobs_clears_running_and_disk(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _MCP_JOBS,
            _MCP_ORDER,
            abandon_inflight_jobs,
            active_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-abandon-now",
                    items=[
                        McpImageItem(
                            prompt="scene",
                            output_path="images/hero.png",
                            status="running",
                        ),
                        McpImageItem(
                            prompt="logo",
                            output_path="images/logo.png",
                            status="queued",
                        ),
                    ],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="running",
                    phase="generating",
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                cleared = abandon_inflight_jobs("TabbyAPI is restarting.")
                self.assertEqual(cleared, 1)
                self.assertIsNone(active_mcp_image_job())
                self.assertEqual(job.status, "error")
                self.assertEqual(job.items[0].status, "error")
                self.assertEqual(job.items[1].status, "error")
                disk = json.loads((Path(raw) / "mcp_jobs.json").read_text(encoding="utf-8"))
                self.assertEqual(disk[-1]["status"], "error")
                self.assertEqual(disk[-1]["items"][1]["status"], "error")
                await reset_mcp_image_jobs_for_tests()

    async def test_abandon_inflight_jobs_cancels_worker(self):
        import asyncio

        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            abandon_inflight_jobs,
            reset_mcp_image_jobs_for_tests,
        )
        import images.jobs as jobs

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-cancel-worker",
                    items=[McpImageItem(prompt="scene", output_path="images/generated.png")],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="running",
                    phase="generating",
                )
                jobs._MCP_JOBS[job.id] = job
                jobs._MCP_ORDER.append(job.id)

                async def sleeper():
                    await asyncio.sleep(60)

                task = asyncio.get_running_loop().create_task(sleeper())
                jobs._MCP_TASK = task
                jobs._MCP_JOB_ID = job.id
                cleared = abandon_inflight_jobs("TabbyAPI is restarting.")
                self.assertEqual(cleared, 1)
                self.assertIsNone(jobs._MCP_TASK)
                self.assertIsNone(jobs._MCP_JOB_ID)
                await asyncio.sleep(0)
                self.assertTrue(task.cancelled() or task.done())
                await reset_mcp_image_jobs_for_tests()

    async def test_get_job_requeues_orphan_running_status(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _MCP_JOBS,
            _MCP_ORDER,
            get_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-orphan-get",
                    items=[McpImageItem(prompt="scene", output_path="images/generated.png")],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="running",
                    phase="generating",
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                recovered = get_mcp_image_job("job-orphan-get")
                self.assertEqual(recovered.status, "queued")
                await reset_mcp_image_jobs_for_tests()

    async def test_running_job_with_no_worker_stays_active(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _MCP_JOBS,
            _MCP_ORDER,
            active_mcp_image_job,
            reset_mcp_image_jobs_for_tests,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-orphan",
                    items=[McpImageItem(prompt="scene", output_path="images/generated.png")],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="running",
                    phase="generating",
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                active = active_mcp_image_job()
                self.assertIsNotNone(active)
                self.assertEqual(active.id, "job-orphan")
                self.assertEqual(job.status, "queued")
                await reset_mcp_image_jobs_for_tests()

    def test_new_items_uniquify_paths(self):
        from endpoints.core.image_jobs import _new_items

        items = _new_items(
            items=[
                {"prompt": "qwen-image: cafe logo", "output_path": "images/logo.png"},
                {"prompt": "hero banner", "output_path": "images/header.png"},
                {"prompt": "a red cube"},
                {"prompt": "a blue cube"},
            ]
        )
        self.assertEqual(items[0].output_path, "images/logo.png")
        self.assertEqual(items[1].output_path, "images/header.png")
        self.assertEqual(items[2].output_path, "images/generated.png")
        self.assertEqual(items[3].output_path, "images/generated-2.png")
        self.assertTrue(items[0].prompt.lower().startswith("qwen-image:"))
        self.assertFalse(items[1].prompt.lower().startswith("qwen-image:"))
        self.assertEqual(items[1].size, "1536x768")

    def test_new_items_keep_explicit_size(self):
        from endpoints.core.image_jobs import _new_items

        items = _new_items(
            items=[
                {
                    "prompt": "a red cube",
                    "output_path": "images/cube.png",
                    "size": "1536x768",
                }
            ]
        )
        self.assertEqual(items[0].size, "1536x768")

    def test_new_items_keep_source_image(self):
        from endpoints.core.image_jobs import _new_items

        items = _new_items(
            items=[
                {
                    "prompt": "same scene, no border",
                    "output_path": "images/generated.png",
                    "source_image": "/tmp/star.png",
                    "denoise": 0.85,
                }
            ]
        )
        self.assertEqual(items[0].source_image, "/tmp/star.png")
        self.assertEqual(items[0].denoise, 0.85)

    async def test_resume_skips_stale_leftover_job(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _MCP_JOBS,
            _MCP_ORDER,
            reset_mcp_image_jobs_for_tests,
            resume_persisted_jobs,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-stale",
                    items=[McpImageItem(prompt="logo", output_path="images/logo.png")],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="queued",
                    phase="queued",
                    started_at=time.time() - 3 * 60 * 60,
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                with mock.patch(
                    "images.jobs.launch_mcp_image_job", new=mock.AsyncMock()
                ) as launch:
                    resumed = await resume_persisted_jobs()
                self.assertEqual(resumed, 0)
                self.assertEqual(job.status, "error")
                launch.assert_not_awaited()
                await reset_mcp_image_jobs_for_tests()

    async def test_resume_launches_recent_job(self):
        from endpoints.core.image_jobs import (
            McpImageItem,
            McpImageJob,
            _MCP_JOBS,
            _MCP_ORDER,
            reset_mcp_image_jobs_for_tests,
            resume_persisted_jobs,
        )

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("common.gpu_mode.GENERATED_DIR", Path(raw)):
                await reset_mcp_image_jobs_for_tests()
                job = McpImageJob(
                    id="job-fresh",
                    items=[McpImageItem(prompt="logo", output_path="images/logo.png")],
                    restore=True,
                    api_base="https://gpu.example/v1",
                    wait_text="",
                    wait_s=0,
                    status="queued",
                    phase="queued",
                    started_at=time.time() - 30,
                )
                _MCP_JOBS[job.id] = job
                _MCP_ORDER.append(job.id)
                with mock.patch(
                    "images.jobs.launch_mcp_image_job", new=mock.AsyncMock()
                ) as launch:
                    resumed = await resume_persisted_jobs()
                self.assertEqual(resumed, 1)
                launch.assert_awaited()
                await reset_mcp_image_jobs_for_tests()

    async def test_mixed_chat_removed_from_phrase_switch(self):
        import common.phrase_switch as ps

        self.assertFalse(hasattr(ps, "ensure_mixed_image_job"))


if __name__ == "__main__":
    unittest.main()
