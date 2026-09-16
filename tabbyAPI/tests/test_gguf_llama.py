import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.llama_runtime import ngl_arg
from common.model import validate_backend
from sidecar.llama_adapter import (
    adapt_chat_payload,
    completion_tokens_from_sse_line,
    cut_at_stop,
    rewrite_sse_line,
)


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
            },
            caps={},
        )
        self.assertEqual(body["model"], "gpt-4o")
        self.assertNotIn("dry_multiplier", body)
        self.assertNotIn("dry_sequence_breakers", body)
        self.assertEqual(body["tools"][0]["function"]["name"], "grep")
        self.assertIn("<|im_end|>", body["stop"])

    def test_adapt_chat_payload_stops_chatml_and_fim(self):
        body = adapt_chat_payload(
            {"messages": [{"role": "user", "content": "hello?"}]},
            caps={},
        )
        self.assertIn("<|im_end|>", body["stop"])
        self.assertIn("<|fim_start|>", body["stop"])
        self.assertIn("<｜end▁of▁sentence｜>", body["stop"])
        self.assertIn("^^", body["stop"])

    def test_cut_at_stop_strips_trailing_carets(self):
        text, hit = cut_at_stop("I am glad to hear from you^^")
        self.assertEqual(text, "I am glad to hear from you")
        self.assertTrue(hit)

    def test_rewrite_sse_cuts_trailing_carets(self):
        state = {}
        line, _ = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"I am glad to hear from you^^"}}]}',
            in_think=False,
            state=state,
        )
        self.assertTrue(state.get("stopped"))
        event = json.loads(line[5:].strip())
        self.assertEqual(
            event["choices"][0]["delta"]["content"],
            "I am glad to hear from you",
        )

    def test_adapt_chat_payload_keeps_client_stop(self):
        body = adapt_chat_payload(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "stop": ["CUSTOM"],
            },
            caps={},
        )
        self.assertEqual(body["stop"][0], "CUSTOM")
        self.assertIn("<|im_end|>", body["stop"])

    def test_adapt_chat_payload_strips_tools_when_gguf_cannot(self):
        from sidecar.llama_adapter import NO_TOOLS_SYSTEM

        with mock.patch("sidecar.llama_adapter._runtime_blob", return_value=""):
            body = adapt_chat_payload(
                {
                    "messages": [
                        {
                            "role": "system",
                            "content": "Use the file tools (Grep, Glob, Write) to edit files.",
                        },
                        {"role": "user", "content": "hello?"},
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "1", "function": {"name": "Write"}}],
                        },
                        {"role": "tool", "content": "wrote it", "tool_call_id": "1"},
                    ],
                    "tools": [{"type": "function", "function": {"name": "Write"}}],
                    "tool_choice": "auto",
                },
                caps={"supports_tools": False, "eos_token": "<｜end▁of▁sentence｜>"},
            )
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["messages"][0]["content"], NO_TOOLS_SYSTEM)
        self.assertNotIn("tool_calls", body["messages"][2])
        self.assertEqual(body["messages"][3]["role"], "user")
        self.assertIn("wrote it", body["messages"][3]["content"])
        self.assertIn("<｜end▁of▁sentence｜>", body["stop"])

    def test_adapt_chat_payload_shortens_console_system_on_chatml_mismatch(self):
        from sidecar.llama_adapter import SIMPLE_CHAT_SYSTEM

        console = (
            "You are chatting in the TabbyAPI Stack web console. "
            "If the user asks for an image, the UI will show PNGs."
        )
        coder = "models/deepseek-coder-6.7b-base-GGUF/model.gguf"
        with mock.patch("sidecar.llama_adapter._runtime_blob", return_value=coder):
            body = adapt_chat_payload(
                {
                    "messages": [
                        {"role": "system", "content": console},
                        {"role": "user", "content": "hello?"},
                    ],
                },
                caps={
                    "chat_template": "<|im_start|>system\n{{ content }}<|im_end|>",
                    "eos_token": "<｜end▁of▁sentence｜>",
                },
            )
        self.assertEqual(body["messages"][0]["content"], SIMPLE_CHAT_SYSTEM)
        self.assertNotIn("PNG", body["messages"][0]["content"])
        self.assertIn("### Instruction:", body["stop"])
        self.assertIn("^^", body["stop"])

    def test_adapt_chat_payload_keeps_console_system_for_chatml_models(self):
        console = (
            "You are chatting in the TabbyAPI Stack web console. "
            "If the user asks for an image, the UI will show PNGs."
        )
        with mock.patch("sidecar.llama_adapter._runtime_blob", return_value=""):
            body = adapt_chat_payload(
                {
                    "messages": [
                        {"role": "system", "content": console},
                        {"role": "user", "content": "hello?"},
                    ],
                },
                caps={"chat_template": "<|im_start|>", "eos_token": "<|im_end|>"},
            )
        self.assertIn("PNG", body["messages"][0]["content"])
        self.assertNotIn("### Instruction:", body["stop"])

    def test_adapt_chat_payload_drops_idle_top_logprobs(self):
        body = adapt_chat_payload(
            {
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "logprobs": 0,
                "top_logprobs": 0,
            },
            caps={},
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
            },
            caps={},
        )
        self.assertTrue(body["logprobs"])
        self.assertEqual(body["top_logprobs"], 5)

    def test_adapt_chat_payload_disables_thinking_when_tools_present(self):
        body = adapt_chat_payload(
            {
                "messages": [{"role": "user", "content": "edit the css"}],
                "tools": [{"type": "function", "function": {"name": "Write"}}],
            },
            caps={},
        )
        self.assertEqual(body["chat_template_kwargs"]["enable_thinking"], False)
        self.assertEqual(body["tools"][0]["function"]["name"], "Write")

    def test_adapt_chat_payload_does_not_force_thinking_off_without_tools(self):
        body = adapt_chat_payload(
            {"messages": [{"role": "user", "content": "hi"}]},
            caps={},
        )
        self.assertNotIn("chat_template_kwargs", body)

    def test_adapt_chat_payload_maps_template_vars_to_chat_template_kwargs(self):
        body = adapt_chat_payload(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "template_vars": {"enable_thinking": True},
            },
            caps={},
        )
        self.assertTrue(body["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("template_vars", body)

    def test_adapt_chat_payload_keeps_client_thinking_with_tools(self):
        body = adapt_chat_payload(
            {
                "messages": [{"role": "user", "content": "edit"}],
                "tools": [{"type": "function", "function": {"name": "Write"}}],
                "enable_thinking": True,
            },
            caps={},
        )
        self.assertTrue(body["chat_template_kwargs"]["enable_thinking"])

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

    def test_rewrite_sse_cuts_chatml_continuation(self):
        state = {}
        line, in_think = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"Hello.<|im_end|>\\n<|im_start|>user\\nmore"}}]}',
            in_think=False,
            state=state,
        )
        self.assertFalse(in_think)
        self.assertTrue(state.get("stopped"))
        event = json.loads(line[5:].strip())
        self.assertEqual(event["choices"][0]["delta"]["content"], "Hello.")

        line, _ = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"<|im_start|>assistant\\nfake"}}]}',
            in_think=False,
            state=state,
        )
        event = json.loads(line[5:].strip())
        self.assertEqual(event["choices"][0]["delta"]["content"], "")

    def test_rewrite_sse_keeps_mixed_reasoning_and_content(self):
        line, _ = rewrite_sse_line(
            'data: {"choices":[{"delta":{"reasoning_content":"plan","content":"hello"}}]}',
            in_think=False,
        )
        event = json.loads(line[5:].strip())
        delta = event["choices"][0]["delta"]
        self.assertEqual(delta["reasoning_content"], "plan")
        self.assertEqual(delta["content"], "hello")

    def test_rewrite_sse_stop_in_reasoning_keeps_same_chunk_content(self):
        state = {}
        line, _ = rewrite_sse_line(
            'data: {"choices":[{"delta":{"reasoning_content":"plan<|im_end|>","content":"hello"}}]}',
            in_think=False,
            state=state,
        )
        event = json.loads(line[5:].strip())
        delta = event["choices"][0]["delta"]
        self.assertEqual(delta["reasoning_content"], "plan")
        self.assertEqual(delta["content"], "hello")
        self.assertFalse(state.get("stopped"))

    def test_completion_tokens_from_sse_counts_deltas_and_usage(self):
        first = completion_tokens_from_sse_line(
            'data: {"choices":[{"delta":{"content":"Hi"}}]}',
            0,
        )
        self.assertEqual(first, 1)
        used = completion_tokens_from_sse_line(
            'data: {"choices":[{"delta":{}}],"usage":{"completion_tokens":40}}',
            first,
        )
        self.assertEqual(used, 40)
        timed = completion_tokens_from_sse_line(
            'data: {"choices":[{"delta":{}}],"timings":{"predicted_n":41}}',
            used,
        )
        self.assertEqual(timed, 41)
        done = completion_tokens_from_sse_line("data: [DONE]", timed)
        self.assertEqual(done, 41)

    def test_rewrite_sse_stop_inside_think_does_not_blank_later_content(self):
        state = {}
        line, in_think = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"<think>plan<|im_end|>"}}]}',
            in_think=False,
            state=state,
        )
        self.assertTrue(in_think)
        self.assertFalse(state.get("stopped"))
        event = json.loads(line[5:].strip())
        self.assertEqual(event["choices"][0]["delta"]["reasoning_content"], "plan")
        line, in_think = rewrite_sse_line(
            'data: {"choices":[{"delta":{"content":"</think>hello"}}]}',
            in_think=in_think,
            state=state,
        )
        event = json.loads(line[5:].strip())
        self.assertEqual(event["choices"][0]["delta"]["content"], "hello")
        self.assertFalse(state.get("stopped"))


class LlamaRuntimeTests(unittest.TestCase):
    def test_ngl_arg_maps_negative_to_fit(self):
        self.assertEqual(ngl_arg(-1), "auto")
        self.assertEqual(ngl_arg(12), "12")
        self.assertEqual(ngl_arg("nope"), "auto")

    def test_clamp_gguf_ctx_keeps_configured_length(self):
        from common.llama_runtime import clamp_gguf_ctx

        self.assertEqual(clamp_gguf_ctx(32768, size_bytes=13 * 1024**3), 32768)
        self.assertEqual(clamp_gguf_ctx(4096, size_bytes=13 * 1024**3), 4096)
        self.assertEqual(clamp_gguf_ctx(32768, size_bytes=6 * 1024**3), 32768)
        self.assertEqual(clamp_gguf_ctx(32768, size_bytes=3 * 1024**3), 32768)
        self.assertEqual(clamp_gguf_ctx(128, size_bytes=13 * 1024**3), 32768)
        self.assertEqual(clamp_gguf_ctx(8192, size_bytes=13 * 1024**3), 8192)

    def test_llama_launch_matches_restarts_when_ctx_was_uncapped(self):
        from common.llama_runtime import llama_launch_matches

        previous = {
            "model": "/models/big.gguf",
            "max_seq_len": 32768,
            "chat_template": "",
            "mmproj": "",
            "n_gpu_layers": -1,
        }
        runtime = {
            "model": "/models/big.gguf",
            "max_seq_len": 8192,
            "chat_template": "",
            "mmproj": "",
            "n_gpu_layers": -1,
            "parallel": 1,
            "fit": "on",
            "fit_target": 2048,
            "flash_attn": "on",
            "cache_k": "q8_0",
            "cache_v": "q8_0",
            "cache_ram": 0,
            "batch": 512,
            "ubatch": 256,
            "kv_unified": "on",
        }
        self.assertFalse(llama_launch_matches(previous, runtime))
        self.assertTrue(llama_launch_matches(runtime, runtime))
        self.assertFalse(llama_launch_matches({}, runtime))

    def test_llama_argv_uses_one_slot_and_vram_headroom(self):
        from common import llama_runtime

        gguf = Path("/tmp/Qwen3.8-27B.gguf")
        with mock.patch.object(llama_runtime, "llama_server_bin", return_value=Path("/usr/bin/llama-server")):
            args = llama_runtime.llama_argv(gguf, max_seq_len=32768)
        self.assertEqual(args[args.index("--parallel") + 1], "1")
        self.assertEqual(args[args.index("--fit") + 1], "on")
        self.assertEqual(args[args.index("--fit-target") + 1], "2048")
        self.assertEqual(args[args.index("--flash-attn") + 1], "on")
        self.assertEqual(args[args.index("--cache-type-k") + 1], "q8_0")
        self.assertEqual(args[args.index("--cache-type-v") + 1], "q8_0")
        self.assertEqual(args[args.index("--cache-ram") + 1], "0")
        self.assertEqual(args[args.index("--batch-size") + 1], "512")
        self.assertEqual(args[args.index("--ubatch-size") + 1], "256")
        self.assertIn("--kv-unified", args)

    def test_llama_argv_keeps_configured_ctx_for_heavy_gguf(self):
        from common import llama_runtime

        gguf = Path("/tmp/Qwen3.8-27B.gguf")
        with (
            mock.patch.object(llama_runtime, "llama_server_bin", return_value=Path("/usr/bin/llama-server")),
            mock.patch.object(llama_runtime, "gguf_weight_bytes", return_value=13 * 1024**3),
        ):
            args = llama_runtime.llama_argv(gguf, max_seq_len=32768)
        self.assertEqual(args[args.index("-c") + 1], "32768")

    def test_guess_chat_template_for_coder_base_and_instruct(self):
        from common.llama_runtime import guess_llama_chat_template, is_gguf_base_name

        coder_base = (
            "models/lmstudio-community_deepseek-coder-6.7b-base-GGUF/"
            "deepseek-coder-6.7b-base.Q4_K_M.gguf"
        )
        self.assertTrue(is_gguf_base_name(coder_base))
        self.assertEqual(guess_llama_chat_template(coder_base), "deepseek")
        self.assertEqual(
            guess_llama_chat_template("models/kexer-7b-Q4/kexer.gguf"),
            "deepseek",
        )
        self.assertFalse(is_gguf_base_name("deepseek-coder-6.7b-instruct-GGUF"))
        self.assertEqual(
            guess_llama_chat_template("models/Some-7B-base-Q4_K_M/model.gguf"),
            "vicuna",
        )
        self.assertEqual(
            guess_llama_chat_template("models/Qwen2.5-7B-Instruct-GGUF/model.gguf"),
            "",
        )
        self.assertEqual(
            guess_llama_chat_template(coder_base, override="vicuna"),
            "vicuna",
        )

    def test_llama_argv_passes_deepseek_template(self):
        from common import llama_runtime

        gguf = Path("/tmp/deepseek-coder-6.7b-base.gguf")
        with mock.patch.object(llama_runtime, "llama_server_bin", return_value=Path("/usr/bin/llama-server")):
            args = llama_runtime.llama_argv(gguf)
        self.assertIn("--chat-template", args)
        self.assertEqual(args[args.index("--chat-template") + 1], "deepseek")
        self.assertLess(args.index("--jinja"), args.index("--chat-template"))


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


class LlamaModelCardTests(unittest.TestCase):
    def test_llama_model_card_from_runtime(self):
        from endpoints.core.utils.model import llama_model_card

        with (
            mock.patch(
                "common.gpu_mode.read_mode",
                return_value={"mode": "llama", "profile": "qwen38guff"},
            ),
            mock.patch(
                "common.llama_runtime.read_llama_runtime",
                return_value={
                    "profile": "qwen38guff",
                    "max_seq_len": 32768,
                    "mmproj": "",
                },
            ),
        ):
            card = llama_model_card()
        self.assertIsNotNone(card)
        self.assertEqual(card.id, "qwen38guff")
        self.assertEqual(card.parameters.cache_mode, "gguf")
        self.assertEqual(card.parameters.max_seq_len, 32768)

    def test_llama_model_card_none_in_llm_mode(self):
        from endpoints.core.utils.model import llama_model_card

        with mock.patch("common.gpu_mode.read_mode", return_value={"mode": "llm"}):
            self.assertIsNone(llama_model_card())


class StartupLlamaRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_starts_llama_when_mode_is_llama(self):
        import main as main_mod

        with (
            mock.patch(
                "common.gpu_mode.should_skip_startup_load", return_value=True
            ),
            mock.patch(
                "common.gpu_mode.read_mode",
                return_value={"mode": "llama", "profile": "qwen38guff"},
            ),
            mock.patch("select_model.last_llama_profile", return_value="qwen38guff"),
            mock.patch(
                "images.jobs._start_llama_profile",
                new=mock.AsyncMock(),
            ) as start,
        ):
            skipped = await main_mod._restore_gpu_owner()
        self.assertTrue(skipped)
        start.assert_awaited_once_with("qwen38guff")

    async def test_restore_skips_comfy_without_llama(self):
        import main as main_mod

        with (
            mock.patch(
                "common.gpu_mode.should_skip_startup_load", return_value=True
            ),
            mock.patch("common.gpu_mode.read_mode", return_value={"mode": "comfy"}),
            mock.patch(
                "images.jobs._start_llama_profile",
                new=mock.AsyncMock(),
            ) as start,
        ):
            skipped = await main_mod._restore_gpu_owner()
        self.assertTrue(skipped)
        start.assert_not_called()

    async def test_restore_loads_exl_when_mode_is_llm(self):
        import main as main_mod

        with mock.patch(
            "common.gpu_mode.should_skip_startup_load", return_value=False
        ):
            skipped = await main_mod._restore_gpu_owner()
        self.assertFalse(skipped)


class InstallerLlamaTests(unittest.TestCase):
    def test_install_sh_builds_llama_and_unit(self):
        src = Path(__file__).resolve().parents[2] / "install.sh"
        text = src.read_text(encoding="utf-8")
        self.assertIn("Installing llama.cpp (GGUF)", text)
        self.assertIn("llamacpp.service", text)
        self.assertIn("llama-start.sh", text)
        self.assertIn("GGML_CUDA", text)
        self.assertIn("GGML_VULKAN", text)
        self.assertIn("libggml-cuda.so", text)
        self.assertIn("libggml-vulkan.so", text)
        self.assertIn("llama.cpp is CPU-only", text)
        self.assertIn("vulkan-headers", text)
        self.assertIn("spirv-headers", text)
        self.assertNotIn("20B-Q4", text)

    def test_tabby_startup_restores_llama_mode(self):
        src = Path(__file__).resolve().parents[1] / "main.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("_restore_gpu_owner", text)
        self.assertIn("starting llama-server", text)
        self.assertIn("_start_llama_profile", text)

    def test_llama_unit_and_start_script_exist(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "deploy/arch/llamacpp.service").is_file())
        start = root / "deploy/arch/llama-start.sh"
        self.assertTrue(start.is_file())
        self.assertIn("--alias", start.read_text(encoding="utf-8"))
        self.assertIn("gpt-4o", start.read_text(encoding="utf-8"))
        self.assertIn("auto", start.read_text(encoding="utf-8"))
        self.assertIn('ngl=auto', start.read_text(encoding="utf-8"))
        self.assertIn("LLAMA_CHAT_TEMPLATE", start.read_text(encoding="utf-8"))
        self.assertIn("--chat-template", start.read_text(encoding="utf-8"))
        self.assertIn("--parallel", start.read_text(encoding="utf-8"))
        self.assertIn("--fit-target", start.read_text(encoding="utf-8"))
        self.assertIn("--cache-ram", start.read_text(encoding="utf-8"))
        self.assertIn("LLAMA_PARALLEL:-1", start.read_text(encoding="utf-8"))
        self.assertIn("LLAMA_CACHE_RAM:-0", start.read_text(encoding="utf-8"))
        self.assertIn("--batch-size", start.read_text(encoding="utf-8"))
        self.assertIn("--kv-unified", start.read_text(encoding="utf-8"))
        unit = (root / "deploy/arch/llamacpp.service").read_text(encoding="utf-8")
        self.assertIn("MemoryMax=18G", unit)
        self.assertIn("OOMScoreAdjust=800", unit)
        src = Path(__file__).resolve().parents[2] / "install.sh"
        text = src.read_text(encoding="utf-8")
        self.assertIn("ensure_tabby_swap()", text)
        self.assertIn("/swapfile", text)
        self.assertIn("vm.swappiness", text)
        self.assertIn("ensure_earlyoom()", text)
        self.assertIn("earlyoom", text)

    def test_wait_llama_healthy_fails_fast_when_process_dies(self):
        from common import llama_runtime

        clock = {"t": 0.0}

        def now():
            return clock["t"]

        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["t"] += seconds

        with (
            mock.patch.object(llama_runtime, "llama_up", return_value=False),
            mock.patch.object(llama_runtime, "llama_pids", side_effect=[[42], [], [], []]),
            mock.patch.object(llama_runtime.time, "time", side_effect=now),
            mock.patch.object(llama_runtime.time, "sleep", side_effect=fake_sleep),
        ):
            self.assertFalse(llama_runtime._wait_llama_healthy(180))
        self.assertLess(clock["t"], 10)


class LlamaLiveTests(unittest.TestCase):
    def tearDown(self):
        from common.llama_live import reset_for_tests

        reset_for_tests()

    def test_weather_from_slots_decode(self):
        from common.llama_live import weather_from_slots

        weather = weather_from_slots(
            [
                {
                    "id": 0,
                    "is_processing": True,
                    "id_task": 181,
                    "n_prompt_tokens": 23189,
                    "n_prompt_tokens_processed": 23189,
                    "next_token": [{"has_next_token": True, "n_decoded": 3546}],
                }
            ]
        )
        self.assertEqual(weather["stage"], "decode")
        self.assertEqual(weather["tokens"], 3546)
        self.assertTrue(weather["busy"])

    def test_weather_from_slots_prefill(self):
        from common.llama_live import weather_from_slots

        weather = weather_from_slots(
            [
                {
                    "is_processing": True,
                    "n_prompt_tokens": 8000,
                    "n_prompt_tokens_processed": 1200,
                    "next_token": [{"has_next_token": True, "n_decoded": 0}],
                }
            ]
        )
        self.assertEqual(weather["stage"], "prefill")
        self.assertEqual(weather["tokens"], 0)

    def test_weather_from_slots_idle(self):
        from common.llama_live import weather_from_slots

        self.assertEqual(weather_from_slots([{"is_processing": False}])["stage"], "idle")

    def test_snapshot_accumulates_run_across_tasks(self):
        from common import llama_live

        first = {
            "busy": True,
            "tokens": 40,
            "run_tokens": 40,
            "stage": "decode",
            "task_id": 1,
        }
        second = {
            "busy": True,
            "tokens": 15,
            "run_tokens": 15,
            "stage": "decode",
            "task_id": 2,
        }
        with mock.patch.object(llama_live, "_fetch_slots", return_value=["x"]):
            with mock.patch.object(llama_live, "weather_from_slots", return_value=first):
                self.assertEqual(llama_live.snapshot()["run_tokens"], 40)
            with mock.patch.object(llama_live, "weather_from_slots", return_value=second):
                out = llama_live.snapshot()
                self.assertEqual(out["tokens"], 15)
                self.assertEqual(out["run_tokens"], 55)
