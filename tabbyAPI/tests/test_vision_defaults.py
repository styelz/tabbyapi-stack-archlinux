import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.vision_defaults import (
    decide_vision,
    parse_param_billions,
    pretty_with_vision_note,
    weight_mib,
)


class ParseParamTests(unittest.TestCase):
    def test_reads_27b_not_qwen38_dot(self):
        self.assertEqual(
            parse_param_billions("Qwen3.8-27B-EXL3-K5K6-hydrated"),
            27,
        )

    def test_reads_9b_and_12b(self):
        self.assertEqual(parse_param_billions("Qwen3.5-9B-exl3-4.00bpw"), 9)
        self.assertEqual(parse_param_billions("gemma-4-12B-it-exl3"), 12)

    def test_prefers_largest_when_moe_experts_also_match(self):
        self.assertEqual(parse_param_billions("Qwen3.5-35B-A3B-exl3-2.13bpw"), 35)

    def test_missing_size(self):
        self.assertIsNone(parse_param_billions("custom-vl-exl3"))


class DecideVisionTests(unittest.TestCase):
    def test_not_capable(self):
        self.assertEqual(
            decide_vision(capable=False, vram_mib=12288, params_b=9),
            {"vision": False, "vision_offload": False},
        )

    def test_12gb_large_tower_off(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=12288, params_b=27),
            {"vision": False, "vision_offload": False},
        )

    def test_12gb_small_tower_offload(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=12288, params_b=9),
            {"vision": True, "vision_offload": True},
        )

    def test_12gb_heavy_weights_without_name(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=12288, weight_mib=8000),
            {"vision": False, "vision_offload": False},
        )

    def test_unknown_vram_large_is_off(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=0, params_b=27),
            {"vision": False, "vision_offload": False},
        )

    def test_unknown_vram_unknown_size_is_off(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=0),
            {"vision": False, "vision_offload": False},
        )

    def test_16gb_enables_with_offload(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=16384, params_b=27),
            {"vision": True, "vision_offload": True},
        )

    def test_24gb_enables_without_offload(self):
        self.assertEqual(
            decide_vision(capable=True, vram_mib=24576, params_b=27),
            {"vision": True, "vision_offload": False},
        )

    def test_pretty_note(self):
        self.assertEqual(
            pretty_with_vision_note("repo main", False, "12 GB"),
            "repo main (vision off on 12 GB)",
        )
        self.assertEqual(pretty_with_vision_note("repo main", True, "12 GB"), "repo main")


class WeightMibTests(unittest.TestCase):
    def test_sums_safetensors(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "a.safetensors").write_bytes(b"x" * (2 * 1024 * 1024))
            (folder / "skip.bin").write_bytes(b"x" * (8 * 1024 * 1024))
            self.assertEqual(weight_mib(folder), 2)


class ProfileDefaultMatrixTests(unittest.TestCase):
    def _vl_folder(self, root: Path, name: str) -> Path:
        folder = root / name
        folder.mkdir()
        (folder / "config.json").write_text(
            json.dumps(
                {
                    "max_position_embeddings": 65536,
                    "model_type": "qwen3_5",
                    "vision_config": {"hidden_size": 1280},
                }
            ),
            encoding="utf-8",
        )
        return folder

    def test_27b_on_12gb_disables_vision(self):
        from ui.models import profile_defaults_from_config

        with tempfile.TemporaryDirectory() as raw:
            folder = self._vl_folder(Path(raw), "Qwen3.8-27B-EXL3-K5K6-hydrated")
            data = profile_defaults_from_config(
                folder, pretty="malaiwah/Qwen3.8 main", vram_mib=12288
            )
        self.assertFalse(data["model"]["vision"])
        self.assertNotIn("vision_offload", data["model"])
        self.assertIn("vision off on 12 GB", data["pretty"])
        self.assertEqual(data["model"]["max_seq_len"], 32768)

    def test_9b_on_12gb_offloads_vision(self):
        from ui.models import profile_defaults_from_config

        with tempfile.TemporaryDirectory() as raw:
            folder = self._vl_folder(Path(raw), "Qwen3.5-9B-exl3-4.00bpw")
            data = profile_defaults_from_config(folder, vram_mib=12288)
        self.assertTrue(data["model"]["vision"])
        self.assertTrue(data["model"]["vision_offload"])
        self.assertNotIn("vision off", data["pretty"])

    def test_27b_on_24gb_enables_vision(self):
        from ui.models import profile_defaults_from_config

        with tempfile.TemporaryDirectory() as raw:
            folder = self._vl_folder(Path(raw), "Qwen3.8-27B-EXL3-K5K6-hydrated")
            data = profile_defaults_from_config(
                folder,
                pretty="malaiwah/Qwen3.8 main",
                vram_mib=24576,
                gpu={"label": "RTX 4090 24 GB", "vram_mib": 24576},
            )
        self.assertTrue(data["model"]["vision"])
        self.assertNotIn("vision_offload", data["model"])
        self.assertEqual(data["pretty"], "malaiwah/Qwen3.8 main")

    def test_glm_thinking_download_gets_reasoning_tokens(self):
        from ui.models import profile_defaults_from_config

        with tempfile.TemporaryDirectory() as raw:
            folder = self._vl_folder(Path(raw), "GLM-4.1V-9B-Thinking-exl3-6.00bpw")
            data = profile_defaults_from_config(folder, vram_mib=12288)
        model = data["model"]
        self.assertTrue(model["reasoning"])
        self.assertEqual(model["reasoning_start_token"], "<think>")
        self.assertEqual(model["answer_start_token"], "<answer>")
        self.assertEqual(model["start_in_reasoning"], "always")
        self.assertNotIn("tool_format", model)


class DisableProfileVisionTests(unittest.TestCase):
    def test_writes_vision_false_and_pretty_note(self):
        import select_model

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profiles = root / "model_profiles"
            models = root / "models" / "Qwen3.8-27B"
            profiles.mkdir()
            models.mkdir(parents=True)
            (profiles / "hf-qwen.yml").write_text(
                "pretty: Qwen 27B\nmodel:\n  model_name: Qwen3.8-27B\n  vision: true\n",
                encoding="utf-8",
            )
            (root / "config.yml").write_text("model:\n  model_name: Qwen3.8-27B\n", encoding="utf-8")
            with (
                mock.patch.object(select_model, "ROOT", root),
                mock.patch.object(select_model, "PROFILES_DIR", profiles),
                mock.patch.object(select_model, "CONFIG_PATH", root / "config.yml"),
                mock.patch.object(select_model, "LAST_PATH", profiles / "last.json"),
                mock.patch("common.switch_times.gpu_label", return_value="12 GB"),
            ):
                data = select_model.disable_profile_vision("hf-qwen")
            saved = (profiles / "hf-qwen.yml").read_text(encoding="utf-8")
            overlay = (models / "tabby_config.yml").read_text(encoding="utf-8")
        self.assertFalse(data["model"]["vision"])
        self.assertIn("vision: false", saved)
        self.assertIn("vision off on 12 GB", saved)
        self.assertIn("vision: false", overlay)


class RecoverAfterVramVisionTests(unittest.TestCase):
    def test_disables_vision_then_retries_same_model(self):
        import switch_model

        cfg = {"vision": True, "max_seq_len": 32768}
        updated = {"model": {"model_name": "Qwen3.8-27B", "vision": False, "max_seq_len": 32768}}
        with (
            mock.patch.object(switch_model, "disable_profile_vision", return_value=updated) as persist,
            mock.patch.object(switch_model, "unload_tabby"),
            mock.patch.object(switch_model, "stop_comfy"),
            mock.patch.object(switch_model, "load_model") as load,
            mock.patch.object(switch_model, "time") as time_mod,
            mock.patch("builtins.print"),
        ):
            time_mod.sleep = mock.Mock()
            info = switch_model.recover_after_vram(
                "http://x", "hf-qwen", "Qwen3.8-27B", cfg, None
            )
        persist.assert_called_once_with("hf-qwen")
        load.assert_called_once()
        self.assertEqual(load.call_args.args[2]["vision"], False)
        self.assertEqual(info["recovered"], "retry")
        self.assertEqual(info["profile"], "hf-qwen")

    def test_skips_persist_when_vision_already_off(self):
        import switch_model

        cfg = {"vision": False, "max_seq_len": 4096}
        with (
            mock.patch.object(switch_model, "disable_profile_vision") as persist,
            mock.patch.object(switch_model, "unload_tabby"),
            mock.patch.object(switch_model, "stop_comfy"),
            mock.patch.object(switch_model, "load_model"),
            mock.patch.object(switch_model, "time") as time_mod,
            mock.patch("builtins.print"),
        ):
            time_mod.sleep = mock.Mock()
            switch_model.recover_after_vram("http://x", "qwen36", "Qwen3.6-27B", cfg, None)
        persist.assert_not_called()


class StartupVisionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_text_only_before_qwen(self):
        import main as main_mod

        model_cfg = SimpleNamespace(
            vision=True,
            model_dir="/models",
            model_dump=lambda exclude_none=True: {"vision": True},
        )
        draft = SimpleNamespace(model_dump=lambda exclude_none=True: {})
        lora = SimpleNamespace(loras=None, model_dump=lambda: {})
        fake_config = SimpleNamespace(
            model=model_cfg,
            draft_model=draft,
            lora=lora,
            load=mock.Mock(),
        )
        model = mock.Mock()
        model.container = object()
        model.load_model = mock.AsyncMock(
            side_effect=[RuntimeError("Insufficient VRAM"), None]
        )
        model.unload_model = mock.AsyncMock()
        updated = {"model": {"model_name": "Qwen3.8-27B", "vision": False}}
        with (
            mock.patch.object(main_mod, "config", fake_config),
            mock.patch.object(main_mod, "is_vram_error", return_value=True),
            mock.patch(
                "select_model.disable_profile_vision", return_value=updated
            ) as persist,
            mock.patch("select_model.last_profile", return_value="hf-qwen"),
            mock.patch("select_model.apply_profile") as fallback,
            mock.patch("select_model.available_profiles", return_value=["qwen"]),
        ):
            await main_mod._load_startup_model(model, "Qwen3.8-27B")
        persist.assert_called_once_with("hf-qwen")
        fallback.assert_not_called()
        self.assertEqual(model.load_model.await_count, 2)
        self.assertEqual(model.load_model.await_args_list[1].kwargs.get("vision"), False)
