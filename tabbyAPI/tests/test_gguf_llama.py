import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.llama_runtime import ngl_arg
from common.model import validate_backend
from sidecar.llama_adapter import adapt_chat_payload, rewrite_sse_line


class FakeHF:
    def __init__(self, method=""):
        self._method = method

    def quant_method(self):
        return self._method


class LlamaAdapterTests(unittest.TestCase):
    def test_adapt_chat_payload_forces_gpt4o_and_strips_exl(self):
        body = adapt_chat_payload(
            {
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "dry_multiplier": 1.5,
                "dry_sequence_breakers": [],
                "tools": [{"type": "function", "function": {"name": "grep"}}],
            }
        )
        self.assertEqual(body["model"], "gpt-4o")
        self.assertNotIn("dry_multiplier", body)
        self.assertNotIn("dry_sequence_breakers", body)
        self.assertEqual(body["tools"][0]["function"]["name"], "grep")

    def test_adapt_chat_payload_drops_idle_top_logprobs(self):
        body = adapt_chat_payload(
            {
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "logprobs": 0,
                "top_logprobs": 0,
            }
        )
        self.assertNotIn("logprobs", body)
        self.assertNotIn("top_logprobs", body)

    def test_adapt_chat_payload_keeps_requested_logprobs(self):
        body = adapt_chat_payload(
            {
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "logprobs": True,
                "top_logprobs": 5,
            }
        )
        self.assertTrue(body["logprobs"])
        self.assertEqual(body["top_logprobs"], 5)

    def test_rewrite_sse_maps_think_tags(self):
        line, in_think = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"<think>plan"}}]}',
            in_think=False,
        )
        self.assertTrue(in_think)
        event = json.loads(line[5:].strip())
        delta = event["choices"][0]["delta"]
        self.assertEqual(delta["reasoning_content"], "plan")
        self.assertEqual(delta["content"], "")

        line, in_think = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"</think>hello"}}]}',
            in_think=True,
        )
        self.assertFalse(in_think)
        event = json.loads(line[5:].strip())
        delta = event["choices"][0]["delta"]
        self.assertEqual(delta["content"], "hello")


class LlamaRuntimeTests(unittest.TestCase):
    def test_ngl_arg_maps_negative_to_fit(self):
        self.assertEqual(ngl_arg(-1), "999")
        self.assertEqual(ngl_arg(12), "12")
        self.assertEqual(ngl_arg("nope"), "999")


class BackendValidationTests(unittest.TestCase):
    def test_gguf_is_rejected_in_tabby_process(self):
        with self.assertRaises(ValueError) as caught:
            validate_backend("llamacpp", FakeHF())
        self.assertIn("llama-server", str(caught.exception))

    def test_exl2_is_allowed_when_installed(self):
        with mock.patch("common.model.dependencies") as deps:
            deps.exllamav2 = True
            deps.exllamav3 = True
            validate_backend("exllamav2", FakeHF("exl2"))
            validate_backend(None, FakeHF("exl2"))

    def test_exl2_rejected_without_package(self):
        with mock.patch("common.model.dependencies") as deps:
            deps.exllamav2 = False
            deps.exllamav3 = True
            with self.assertRaises(ValueError) as caught:
                validate_backend("exllamav2", FakeHF("exl2"))
        self.assertIn("exllamav2", str(caught.exception))

    def test_exl2_container_requires_package(self):
        from backends.exllamav2.model import ExLlamaV2, ExllamaV2Container

        if ExLlamaV2 is not None:
            self.skipTest("exllamav2 is installed")

        async def _create():
            await ExllamaV2Container.create(Path("/tmp"), FakeHF())

        with self.assertRaises(ValueError):
            import asyncio

            asyncio.run(_create())


class SwitchRoutingTests(unittest.TestCase):
    def test_resolve_name_splits_llm_and_llama(self):
        import switch_model

        with (
            mock.patch.object(switch_model, "available_profiles", return_value=["qwen", "biggguf"]),
            mock.patch.object(switch_model, "last_exl_profile", return_value="qwen"),
            mock.patch.object(switch_model, "last_llama_profile", return_value="biggguf"),
            mock.patch.object(
                switch_model,
                "profile_aliases",
                return_value={"qwen": "qwen", "biggguf": "biggguf"},
            ),
        ):
            self.assertEqual(switch_model.resolve_name("llm"), "qwen")
            self.assertEqual(switch_model.resolve_name("llama"), "biggguf")
            self.assertEqual(switch_model.resolve_name("gguf"), "biggguf")
            self.assertEqual(switch_model.resolve_name("comfy"), "comfy")
            self.assertEqual(switch_model.resolve_name("qwen"), "qwen")

    def test_chat_backend_url_follows_gpu_mode(self):
        from common import gpu_mode as gm
        from sidecar.settings import chat_backend_url

        with tempfile.TemporaryDirectory() as raw:
            status = Path(raw) / "gpu_mode.json"
            with mock.patch.object(gm, "STATUS_PATH", status):
                status.write_text('{"mode": "llama"}\n', encoding="utf-8")
                self.assertTrue(chat_backend_url().rstrip("/").endswith(":5002"))
                status.write_text('{"mode": "llm"}\n', encoding="utf-8")
                self.assertTrue(chat_backend_url().rstrip("/").endswith(":5001"))

    def test_llm_is_ready_when_llama_is_up(self):
        from common import gpu_mode as gm
        from sidecar import model_status

        with tempfile.TemporaryDirectory() as raw:
            status = Path(raw) / "gpu_mode.json"
            status.write_text('{"mode": "llama"}\n', encoding="utf-8")
            with (
                mock.patch.object(gm, "STATUS_PATH", status),
                mock.patch.object(gm, "llama_up", return_value=True),
            ):
                self.assertTrue(model_status.llm_is_ready())

    def test_help_mentions_gguf_and_llama(self):
        from common.phrase_switch import help_text

        text = help_text("http://127.0.0.1:5000/v1")
        self.assertIn("llama.cpp", text)
        self.assertIn("switch to llama", text)
        self.assertIn("GGUF", text)


class InstallerLlamaTests(unittest.TestCase):
    def test_install_sh_builds_llama_and_unit(self):
        src = Path(__file__).resolve().parents[2] / "install.sh"
        text = src.read_text(encoding="utf-8")
        self.assertIn("Installing llama.cpp (GGUF)", text)
        self.assertIn("llamacpp.service", text)
        self.assertIn("llama-start.sh", text)
        self.assertNotIn("20B-Q4", text)

    def test_llama_unit_and_start_script_exist(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "deploy/arch/llamacpp.service").is_file())
        start = root / "deploy/arch/llama-start.sh"
        self.assertTrue(start.is_file())
        self.assertIn("--alias", start.read_text(encoding="utf-8"))
        self.assertIn("gpt-4o", start.read_text(encoding="utf-8"))
