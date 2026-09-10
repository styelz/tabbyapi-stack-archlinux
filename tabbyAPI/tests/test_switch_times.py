import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.phrase_switch import (
    LLM_NOT_READY_WAIT_S,
    comfy_not_running_text,
    llm_loading_text,
    llm_not_ready_text,
    switch_reply_text,
)
from common.switch_times import (
    DEFAULT_READY_S,
    blend_ready,
    clamp_sample,
    format_duration,
    load_switch_times,
    merge_times,
    ready_seconds,
    record_ready,
    should_record,
    wait_hint,
)


SAMPLE = {
    "qwen": {"ready_s": 12},
    "qwen35": {"ready_s": 118},
    "qwen36": {"ready_s": 95},
    "comfy": {"ready_s": 6, "flux_s": 40, "qwen_image_s": 80},
    "llm": {"ready_s": 55},
}


class SwitchTimesTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(1), "1 second")
        self.assertEqual(format_duration(12), "10 seconds")
        self.assertEqual(format_duration(15), "15 seconds")
        self.assertEqual(format_duration(60), "60 seconds")
        self.assertEqual(format_duration(89), "90 seconds")
        self.assertEqual(format_duration(90), "2 minutes")
        self.assertEqual(format_duration(118), "2 minutes")
        self.assertEqual(format_duration(150), "2 minutes")

    def test_wait_hint_from_table(self):
        self.assertEqual(wait_hint("qwen", SAMPLE), "Wait about 10 seconds")
        self.assertEqual(wait_hint("qwen35", SAMPLE), "Wait about 2 minutes")
        self.assertEqual(wait_hint("flux", SAMPLE), "Wait about 5 seconds")
        self.assertEqual(
            wait_hint("unknown", SAMPLE),
            f"Wait about {format_duration(DEFAULT_READY_S['qwen'])}",
        )

    def test_ready_seconds_reads_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "switch_times.json"
            path.write_text(json.dumps(SAMPLE), encoding="utf-8")
            with mock.patch("common.switch_times.TIMES_PATH", path):
                self.assertEqual(ready_seconds("qwen35"), 118)
                self.assertEqual(wait_hint("comfy"), "Wait about 5 seconds")

    def test_switch_reply_uses_table(self):
        with mock.patch("common.phrase_switch.wait_hint", side_effect=lambda name: wait_hint(name, SAMPLE)):
            comfy = switch_reply_text("comfy")
            self.assertIn("Wait about 5 seconds", comfy)
            qwen35 = switch_reply_text("qwen35")
            self.assertIn("Wait about 2 minutes", qwen35)
            llm = switch_reply_text("llm")
            self.assertIn("Wait about 55 seconds", llm)

    def test_loading_and_not_ready_copy(self):
        with mock.patch("common.phrase_switch.wait_hint", side_effect=lambda name: wait_hint(name, SAMPLE)):
            with mock.patch("common.phrase_switch.ready_seconds", side_effect=lambda name: ready_seconds(name, SAMPLE)):
                with mock.patch("common.phrase_switch.format_duration", side_effect=format_duration):
                    with mock.patch("common.phrase_switch.gpu_label", return_value="12 GB"):
                        not_ready = llm_not_ready_text()
                        self.assertIn("wait about 10 seconds", not_ready)
                        self.assertIn("2 minutes", not_ready)
                        loading = llm_loading_text("qwen35")
                        self.assertIn("Wait about 2 minutes", loading)
                        self.assertIn("qwen35 on 12 GB", loading)
                        console = llm_loading_text("qwen35", console=True)
                        self.assertIn("still loading", console.lower())
                        self.assertIn("Wait about 2 minutes", console)
                        self.assertNotIn("gpt-4o", console)
                        self.assertNotIn("not loaded", console.lower())
                        comfy = comfy_not_running_text()
                        self.assertIn("wait about 5 seconds", comfy)

    def test_not_ready_wait_constant_is_defined(self):
        self.assertGreaterEqual(LLM_NOT_READY_WAIT_S, 1)


class RecordReadyTests(unittest.TestCase):
    """Real loads blend into switch_times.json so 'typical' tracks this box."""

    def test_blend_is_one_third_new(self):
        self.assertAlmostEqual(blend_ready(60.0, 90.0), 70.0)
        self.assertAlmostEqual(blend_ready(None, 90.0), 90.0)

    def test_should_record_skips_noop_loads(self):
        self.assertFalse(should_record(2.0))  # already loaded / instant
        self.assertTrue(should_record(90.0))
        self.assertFalse(should_record("x"))

    def test_outliers_are_clamped_not_dropped(self):
        # Cold Triton compile or weights evicted from page cache: one load
        # moves the typical a bounded amount; a slower reality still converges.
        self.assertEqual(clamp_sample(300.0, 60.0), 240.0)
        self.assertEqual(clamp_sample(90.0, 60.0), 90.0)
        self.assertEqual(clamp_sample(500.0, None), 500.0)

    @staticmethod
    def _paths(raw: str) -> tuple[Path, Path]:
        return Path(raw) / "switch_times.json", Path(raw) / "switch_times.local.json"

    def test_merge_overlay_fields_win(self):
        base = {"gpu": "G", "qwen": {"ready_s": 60, "first_token_s": 6.1}, "comfy": {"ready_s": 30}}
        overlay = {"qwen": {"ready_s": 70.5}, "live_updated_at": "t"}
        merged = merge_times(base, overlay)
        self.assertEqual(merged["qwen"], {"ready_s": 70.5, "first_token_s": 6.1})
        self.assertEqual(merged["comfy"], {"ready_s": 30})
        self.assertEqual(merged["gpu"], "G")
        self.assertEqual(merged["live_updated_at"], "t")

    def test_record_ready_writes_overlay_and_keeps_baseline_untouched(self):
        with tempfile.TemporaryDirectory() as raw:
            base, local = self._paths(raw)
            baseline = {
                "gpu": "Test GPU",
                "qwen": {"ready_s": 60, "first_token_s": 6.1, "error": "old bench note"},
                "comfy": {"ready_s": 30, "flux_s": 200},
            }
            base.write_text(json.dumps(baseline), encoding="utf-8")
            self.assertEqual(record_ready("qwen", 90.0, path=base, local_path=local), 70.0)
            # The tracked bench file is what git / the updater see: unchanged.
            self.assertEqual(json.loads(base.read_text(encoding="utf-8")), baseline)
            data = load_switch_times(base, local)
            self.assertEqual(data["qwen"]["ready_s"], 70.0)
            self.assertEqual(data["qwen"]["first_token_s"], 6.1)
            self.assertFalse(data["qwen"].get("error"))
            self.assertEqual(data["gpu"], "Test GPU")
            self.assertEqual(data["comfy"], {"ready_s": 30, "flux_s": 200})
            self.assertIn("live_updated_at", data)
            # Chat wait copy and the HUD read the merged view.
            self.assertEqual(ready_seconds("qwen", data), 70)
            # A second load keeps blending from the merged prior.
            self.assertEqual(record_ready("qwen", 90.0, path=base, local_path=local), 76.7)

    def test_record_ready_first_picture_fields(self):
        with tempfile.TemporaryDirectory() as raw:
            base, local = self._paths(raw)
            base.write_text(json.dumps({"comfy": {"ready_s": 30, "flux_s": 180}}), encoding="utf-8")
            self.assertEqual(record_ready("flux", 240.0, field="flux_s", path=base, local_path=local), 200.0)
            # No prior qwen_image_s: first sample stands as-is.
            self.assertEqual(
                record_ready("comfy", 250.0, field="qwen_image_s", path=base, local_path=local), 250.0
            )
            data = load_switch_times(base, local)
            self.assertEqual(data["comfy"]["ready_s"], 30)
            self.assertEqual(data["comfy"]["flux_s"], 200.0)
            self.assertEqual(data["comfy"]["qwen_image_s"], 250.0)

    def test_record_ready_skips_junk_samples(self):
        with tempfile.TemporaryDirectory() as raw:
            base, local = self._paths(raw)
            base.write_text(json.dumps({"qwen": {"ready_s": 60}}), encoding="utf-8")
            self.assertIsNone(record_ready("qwen", 1.2, path=base, local_path=local))
            self.assertIsNone(record_ready("", 60.0, path=base, local_path=local))
            self.assertFalse(local.exists())
            self.assertEqual(load_switch_times(base, local)["qwen"], {"ready_s": 60})

    def test_record_ready_clamps_a_cold_load(self):
        with tempfile.TemporaryDirectory() as raw:
            base, local = self._paths(raw)
            base.write_text(json.dumps({"qwen35": {"ready_s": 30}}), encoding="utf-8")
            # 280s after page-cache eviction, capped at 4x30 before blending.
            self.assertEqual(record_ready("qwen35", 280.0, path=base, local_path=local), 60.0)
            self.assertEqual(record_ready("qwen35", 280.0, path=base, local_path=local), 120.0)

    def test_record_ready_seeds_from_defaults_when_files_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "sub" / "switch_times.json"
            local = Path(raw) / "sub" / "switch_times.local.json"
            expected = round(blend_ready(float(DEFAULT_READY_S["qwen35"]), 150.0), 1)
            self.assertEqual(record_ready("qwen35", 150.0, path=base, local_path=local), expected)
            self.assertFalse(base.exists())
            self.assertEqual(load_switch_times(base, local)["qwen35"]["ready_s"], expected)

    def test_record_ready_never_raises_on_readonly_dir(self):
        with tempfile.TemporaryDirectory() as raw:
            base, local = self._paths(raw)
            base.write_text(json.dumps({"qwen": {"ready_s": 60}}), encoding="utf-8")
            with mock.patch("common.switch_times.os.replace", side_effect=OSError("ro")):
                self.assertIsNone(record_ready("qwen", 90.0, path=base, local_path=local))
            self.assertEqual(load_switch_times(base, local)["qwen"], {"ready_s": 60})
            self.assertEqual([p.name for p in Path(raw).iterdir()], ["switch_times.json"])

    def test_load_with_explicit_path_only_reads_that_file(self):
        # calibrate --out / bench read the raw baseline, no overlay.
        with tempfile.TemporaryDirectory() as raw:
            base, local = self._paths(raw)
            base.write_text(json.dumps({"qwen": {"ready_s": 60}}), encoding="utf-8")
            local.write_text(json.dumps({"qwen": {"ready_s": 99}}), encoding="utf-8")
            self.assertEqual(load_switch_times(base)["qwen"]["ready_s"], 60)
            self.assertEqual(load_switch_times(base, local)["qwen"]["ready_s"], 99)
            with (
                mock.patch("common.switch_times.TIMES_PATH", base),
                mock.patch("common.switch_times.LOCAL_TIMES_PATH", local),
            ):
                self.assertEqual(ready_seconds("qwen"), 99)


class SwitchModelRecordsTests(unittest.TestCase):
    """switch_model.py (CLI / phrase switch subprocess) writes clean warm loads."""

    def _run(self, *, loaded_after="Qwen3.5-9B", recover=None, already=False, from_comfy=False, record=True):
        import switch_model

        profile = {"model": {"model_name": "Qwen3.5-9B"}, "sampling": {"override_preset": None}}
        before = "Qwen3.5-9B" if already else "Other"
        clock = iter([1000.0, 1066.0])
        with (
            mock.patch("switch_model.apply_profile", return_value=profile),
            mock.patch("switch_model.server_up", return_value=True),
            mock.patch("switch_model.wait_out_image_jobs"),
            mock.patch("switch_model.comfy_up", return_value=from_comfy),
            mock.patch("switch_model.stop_comfy"),
            mock.patch("switch_model.write_mode"),
            mock.patch("switch_model.current_model", side_effect=[before, loaded_after]),
            mock.patch("switch_model.switch_sampler"),
            mock.patch("switch_model.load_model", side_effect=(SystemExit("Insufficient VRAM") if recover else None)),
            mock.patch("switch_model.is_vram_error", return_value=True),
            mock.patch(
                "switch_model.recover_after_vram",
                return_value={"profile": "qwen", "recovered": recover},
            ),
            mock.patch("switch_model.time.time", side_effect=lambda: next(clock, 1066.0)),
            mock.patch("switch_model.record_ready") as rec,
            mock.patch("builtins.print"),
        ):
            info = switch_model.switch_to_llm("qwen", base="http://x", record=record)
        return info, rec

    def test_clean_load_records_profile_only(self):
        info, rec = self._run()
        self.assertFalse(info["already"])
        rec.assert_called_once_with("qwen", 66.0)

    def test_load_from_comfy_also_records_llm(self):
        _, rec = self._run(from_comfy=True)
        self.assertEqual(rec.call_args_list, [mock.call("qwen", 66.0), mock.call("llm", 66.0)])

    def test_already_loaded_records_nothing(self):
        info, rec = self._run(already=True)
        self.assertTrue(info["already"])
        rec.assert_not_called()

    def test_recovery_records_nothing(self):
        info, rec = self._run(recover="bounce")
        self.assertEqual(info["recovered"], "bounce")
        rec.assert_not_called()

    def test_bench_opts_out(self):
        _, rec = self._run(record=False)
        rec.assert_not_called()

    def test_admin_headers_from_api_tokens(self):
        import switch_model

        with tempfile.TemporaryDirectory() as raw:
            auth = Path(raw) / "api_tokens.yml"
            auth.write_text("api_key: aaa\nadmin_key: secret-admin\n", encoding="utf-8")
            with (
                mock.patch.object(switch_model, "AUTH_FILE", auth),
                mock.patch.dict("os.environ", {"TABBY_ADMIN_KEY": ""}),
            ):
                headers = switch_model.admin_headers()
            self.assertEqual(headers["X-Admin-Key"], "secret-admin")
            self.assertEqual(headers["Authorization"], "Bearer secret-admin")
            with (
                mock.patch.object(switch_model, "AUTH_FILE", auth),
                mock.patch.dict("os.environ", {"TABBY_ADMIN_KEY": "env-wins"}),
            ):
                self.assertEqual(switch_model.admin_headers()["X-Admin-Key"], "env-wins")
            with (
                mock.patch.object(switch_model, "AUTH_FILE", Path(raw) / "missing.yml"),
                mock.patch.dict("os.environ", {"TABBY_ADMIN_KEY": ""}),
            ):
                self.assertEqual(switch_model.admin_headers(), {})

    def test_switch_to_comfy_records_a_real_start(self):
        import switch_model

        clock = iter([1000.0, 1037.0])
        with (
            mock.patch("switch_model.comfy_up", return_value=False),
            mock.patch("switch_model.server_up", return_value=True),
            mock.patch("switch_model.unload_tabby"),
            mock.patch("switch_model.write_mode"),
            mock.patch("switch_model.start_comfy_if_needed"),
            mock.patch("switch_model.time.time", side_effect=lambda: next(clock, 1037.0)),
            mock.patch("switch_model.record_ready") as rec,
            mock.patch("builtins.print"),
        ):
            switch_model.switch_to_comfy("http://x")
        rec.assert_called_once_with("comfy", 37.0)

    def test_switch_to_comfy_skips_when_already_up(self):
        import switch_model

        with (
            mock.patch("switch_model.comfy_up", return_value=True),
            mock.patch("switch_model.server_up", return_value=True),
            mock.patch("switch_model.unload_tabby"),
            mock.patch("switch_model.write_mode"),
            mock.patch("switch_model.start_comfy_if_needed"),
            mock.patch("switch_model.record_ready") as rec,
            mock.patch("builtins.print"),
        ):
            switch_model.switch_to_comfy("http://x")
        rec.assert_not_called()


class ImageJobRecordsTests(unittest.IsolatedAsyncioTestCase):
    """In-process handoff (pictures) is the other real load path."""

    async def test_reload_last_llm_records_profile_and_llm(self):
        from images import jobs

        clock = iter([1000.0, 1064.0])
        with (
            mock.patch("images.jobs.wait_out_generating_image_jobs", new=mock.AsyncMock()),
            mock.patch("images.jobs.available_profiles", return_value=["qwen"]),
            mock.patch("images.jobs.restore_llm_profile", return_value="qwen"),
            mock.patch("images.jobs.comfy_up", return_value=True),
            mock.patch("images.jobs.stop_comfy"),
            mock.patch("images.jobs.wait_gpu_vram_drain"),
            mock.patch("images.jobs.reset_cuda_memory"),
            mock.patch("images.jobs._load_profile", new=mock.AsyncMock()),
            mock.patch("images.jobs.write_mode"),
            mock.patch("images.jobs.time.time", side_effect=lambda: next(clock, 1064.0)),
            mock.patch("common.phrase_switch.set_switch_lock"),
            mock.patch("common.phrase_switch.clear_switch_lock"),
            mock.patch("common.switch_times.record_ready") as rec,
        ):
            name = await jobs.reload_last_llm()
        self.assertEqual(name, "qwen")
        self.assertEqual(rec.call_args_list, [mock.call("qwen", 64.0), mock.call("llm", 64.0)])

    async def test_reload_last_llm_vram_bounce_records_nothing(self):
        from images import jobs

        with (
            mock.patch("images.jobs.wait_out_generating_image_jobs", new=mock.AsyncMock()),
            mock.patch("images.jobs.available_profiles", return_value=["qwen"]),
            mock.patch("images.jobs.restore_llm_profile", return_value="qwen"),
            mock.patch("images.jobs.comfy_up", return_value=True),
            mock.patch("images.jobs.stop_comfy"),
            mock.patch("images.jobs.wait_gpu_vram_drain"),
            mock.patch("images.jobs.reset_cuda_memory"),
            mock.patch(
                "images.jobs._load_profile",
                new=mock.AsyncMock(side_effect=RuntimeError("Insufficient VRAM")),
            ),
            mock.patch("images.jobs.is_vram_error", return_value=True),
            mock.patch("images.jobs.write_mode"),
            mock.patch("images.jobs._unload_tabby_leftovers", new=mock.AsyncMock()),
            mock.patch("images.jobs._bounce_after_vram_fail") as bounce,
            mock.patch("common.phrase_switch.set_switch_lock"),
            mock.patch("common.phrase_switch.clear_switch_lock"),
            mock.patch("common.switch_times.record_ready") as rec,
        ):
            await jobs.reload_last_llm()
        bounce.assert_called_once_with("qwen")
        rec.assert_not_called()

    async def test_ensure_comfy_records_only_a_real_start(self):
        from images import jobs

        clock = iter([1000.0, 1035.0])
        with (
            mock.patch("images.jobs.comfy_up", side_effect=[False, False, True]),
            mock.patch("images.jobs.loaded_tabby_name", return_value=None),
            mock.patch("images.jobs.write_mode"),
            mock.patch("images.jobs.start_comfy_if_needed"),
            mock.patch("images.jobs.time.time", side_effect=lambda: next(clock, 1035.0)),
            mock.patch("common.phrase_switch.set_switch_lock"),
            mock.patch("common.phrase_switch.clear_switch_lock"),
            mock.patch("common.switch_times.record_ready") as rec,
        ):
            await jobs.ensure_comfy()
        rec.assert_called_once_with("comfy", 35.0)

    def test_first_render_picks_backend_field(self):
        from images import jobs

        flux = jobs.McpImageItem(prompt="a red cube", output_path="a.png")
        qwen = jobs.McpImageItem(prompt="qwen-image: SALE poster", output_path="b.png")
        batch = jobs.McpImageItem(prompt="a red cube", output_path="c.png", count=3)
        img2img = jobs.McpImageItem(prompt="a red cube", output_path="d.png", source_image="/tmp/x.png")
        with mock.patch("common.switch_times.record_ready") as rec, mock.patch(
            "common.gpu_mode.flux_checkpoint_ready", return_value=True
        ):
            jobs._record_first_render(flux, 190.0)
            jobs._record_first_render(qwen, 240.0)
            jobs._record_first_render(batch, 400.0)
            jobs._record_first_render(img2img, 100.0)
        self.assertEqual(
            rec.call_args_list,
            [
                mock.call("comfy", 190.0, field="flux_s"),
                mock.call("comfy", 240.0, field="qwen_image_s"),
            ],
        )


class NotReadyWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_yield_and_not_ready_sleep_the_defined_wait(self):
        from common.phrase_switch import (
            llm_not_ready_response,
            yield_comfy_to_llm_response,
        )
        from endpoints.OAI.types.chat_completion import (
            ChatCompletionMessage,
            ChatCompletionRequest,
        )

        data = ChatCompletionRequest(
            messages=[ChatCompletionMessage(role="user", content="continue")],
            stream=False,
        )
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with (
            mock.patch("common.phrase_switch.asyncio.sleep", side_effect=fake_sleep),
            mock.patch("common.phrase_switch.switch_in_progress", return_value=True),
        ):
            await llm_not_ready_response(data)
        self.assertEqual(slept, [LLM_NOT_READY_WAIT_S])

        slept.clear()
        with (
            mock.patch("common.phrase_switch.asyncio.sleep", side_effect=fake_sleep),
            mock.patch("common.phrase_switch.switch_in_progress", return_value=False),
            mock.patch("common.phrase_switch.start_switch"),
        ):
            await yield_comfy_to_llm_response(data)
        self.assertEqual(slept, [LLM_NOT_READY_WAIT_S])

        slept.clear()
        with (
            mock.patch("common.phrase_switch.asyncio.sleep", side_effect=fake_sleep),
            mock.patch("common.phrase_switch.switch_in_progress", return_value=True),
        ):
            result = await llm_not_ready_response(data, console=True)
        self.assertEqual(slept, [])
        self.assertIn("still loading", result.choices[0].message.content.lower())


if __name__ == "__main__":
    unittest.main()
