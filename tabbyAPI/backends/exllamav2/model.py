"""ExLlamaV2 container with the same generate surface as ExLlamaV3."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any, AsyncIterator, Dict, List, Optional

from common.logger import xlogger
from common.networking import DisconnectHandler
from common.sampling import BaseSamplerRequest
from common.transformers_utils import HFModel
from common.utils import unwrap
from endpoints.core.types.model import ModelCard, ModelCardParameters

try:
    from exllamav2 import (
        ExLlamaV2,
        ExLlamaV2Cache,
        ExLlamaV2Cache_8bit,
        ExLlamaV2Cache_Q4,
        ExLlamaV2Config,
        ExLlamaV2Tokenizer,
    )
    from exllamav2.generator import ExLlamaV2Sampler, ExLlamaV2StreamingGenerator
except ImportError:  # pragma: no cover - optional extra
    ExLlamaV2 = None  # type: ignore


class ExllamaV2Container:
    """Load and sample EXL2/GPTQ models through ExLlamaV2."""

    def __init__(self):
        self.model_dir: Optional[pathlib.Path] = None
        self.hf_model: Optional[HFModel] = None
        self.config = None
        self.model = None
        self.cache = None
        self.tokenizer = None
        self.generator = None
        self.prompt_template = None
        self.vision_model = None
        self.use_vision = False
        self.use_draft_model = False
        self.loaded = False
        self.max_seq_len = 4096
        self.cache_mode = "FP16"
        self.cache_size = 4096
        self.load_lock = asyncio.Lock()
        self.load_condition = asyncio.Condition()
        self.active_job_ids: dict[str, Any] = {}
        self.reasoning = False
        self.reasoning_start_token = "<think>"
        self.reasoning_end_token = "</think>"
        self.tool_format = None
        self.harmony = False
        self.muse_glimmer = False

    @classmethod
    async def create(cls, model_directory: pathlib.Path, hf_model: HFModel, **kwargs):
        if ExLlamaV2 is None:
            raise ValueError(
                "The exllamav2 backend is selected, but the exllamav2 package is not installed."
            )
        self = cls()
        self.model_dir = pathlib.Path(model_directory)
        self.hf_model = hf_model
        config = ExLlamaV2Config()
        config.model_dir = str(self.model_dir.resolve())
        config.prepare()
        max_seq = unwrap(kwargs.get("max_seq_len"), config.max_seq_len or 4096)
        if max_seq and int(max_seq) > 0:
            config.max_seq_len = int(max_seq)
        self.max_seq_len = int(config.max_seq_len)
        self.cache_size = unwrap(kwargs.get("cache_size"), self.max_seq_len)
        self.cache_mode = unwrap(kwargs.get("cache_mode"), "FP16")
        self.config = config
        self.model = ExLlamaV2(config)
        self.tokenizer = ExLlamaV2Tokenizer(config)
        self.use_vision = bool(kwargs.get("vision"))
        self.reasoning = bool(kwargs.get("reasoning"))
        if kwargs.get("reasoning_start_token"):
            self.reasoning_start_token = kwargs["reasoning_start_token"]
        if kwargs.get("reasoning_end_token"):
            self.reasoning_end_token = kwargs["reasoning_end_token"]
        self.tool_format = kwargs.get("tool_format")
        self.harmony = bool(kwargs.get("harmony"))
        self.muse_glimmer = bool(kwargs.get("muse_glimmer"))
        try:
            from common.templating import find_prompt_template

            self.prompt_template = await find_prompt_template(
                kwargs.get("prompt_template"), self.model_dir
            )
        except Exception:
            self.prompt_template = None
        return self

    def create_cache(self):
        mode = str(self.cache_mode or "FP16").upper()
        lazy = True
        if mode in {"Q4", "4,4"}:
            return ExLlamaV2Cache_Q4(self.model, max_seq_len=self.cache_size, lazy=lazy)
        if mode in {"Q8", "8,8"}:
            return ExLlamaV2Cache_8bit(self.model, max_seq_len=self.cache_size, lazy=lazy)
        return ExLlamaV2Cache(self.model, max_seq_len=self.cache_size, lazy=lazy)

    async def load_gen(self, progress_callback=None, **kwargs):
        def _load():
            cache = self.create_cache()
            loader = getattr(self.model, "load_autosplit", None)
            if callable(loader):
                loader(cache)
            else:
                list(self.model.load_autosplit_gen(cache))
            self.cache = cache
            self.generator = ExLlamaV2StreamingGenerator(self.model, cache, self.tokenizer)
            self.loaded = True
            return 1, 1

        loaded, total = await asyncio.to_thread(_load)
        yield loaded, total

    async def unload(self, loras_only: bool = False, **kwargs):
        self.loaded = False
        self.active_job_ids.clear()
        self.generator = None
        if self.model is not None:
            try:
                self.model.unload()
            except Exception:
                pass
        self.model = None
        self.cache = None
        xlogger.info("EXL2 model unloaded.")

    async def wait_for_jobs(self, skip_wait: bool = False):
        if skip_wait:
            return
        while self.active_job_ids:
            await asyncio.sleep(0.05)

    async def recover_after_crash(self, ex: BaseException) -> None:
        xlogger.error(f"EXL2 generation crashed: {ex}")
        await self.unload(skip_wait=True)

    def model_info(self) -> ModelCard:
        return ModelCard(
            id=self.model_dir.name if self.model_dir else "exl2",
            parameters=ModelCardParameters(
                max_seq_len=self.max_seq_len,
                cache_size=self.cache_size,
                cache_mode=self.cache_mode,
                use_vision=self.use_vision,
            ),
        )

    def encode_tokens(self, text: str, **kwargs) -> List[int]:
        ids = self.tokenizer.encode(text, add_bos=bool(kwargs.get("add_bos_token", True)))
        return ids[0].tolist() if hasattr(ids, "tolist") else list(ids)

    def decode_tokens(self, ids: List[int], **kwargs) -> str:
        import torch

        tensor = torch.tensor([ids])
        return self.tokenizer.decode(tensor)

    def get_special_tokens(self, add_bos_token: bool = True, ban_eos_token: bool = False):
        return {
            "bos_token": self.tokenizer.bos_token,
            "eos_token": self.tokenizer.eos_token,
            "pad_token": getattr(self.tokenizer, "pad_token", None),
            "unk_token": getattr(self.tokenizer, "unk_token", None),
        }

    def constrain_generation_output(self, request_id: str, text: str) -> bool:
        return False

    def job_max_rq_tokens(self, max_tokens: int) -> Optional[int]:
        return None

    def validate_context_length(self, prompt: str, params: BaseSamplerRequest, mm_embeddings=None):
        from common.errors import validate_context_requirements

        context_len = len(
            self.encode_tokens(
                prompt,
                add_bos_token=unwrap(params.add_bos_token, True),
            )
        )
        max_tokens = unwrap(params.max_tokens, 0)
        if max_tokens <= 0:
            max_tokens = self.max_seq_len - context_len - 1
        validate_context_requirements(
            context_len,
            self.max_seq_len,
            max_tokens,
            self.cache_size,
            None,
            256,
        )

    async def generate(
        self,
        request_id: str,
        prompt: str,
        params: BaseSamplerRequest,
        abort_event: Optional[asyncio.Event] = None,
        mm_embeddings=None,
        disconnect_handler: DisconnectHandler = None,
    ) -> Dict[str, Any]:
        generations = []
        handler = disconnect_handler or DisconnectHandler(
            description=f"generate {request_id}",
            abort_event=abort_event,
        )
        async for generation in self.stream_generate(
            request_id, prompt, params, handler, mm_embeddings
        ):
            if generation:
                generations.append(generation)
        joined = {
            "text": "",
            "prompt_tokens": 0,
            "generated_tokens": 0,
            "finish_reason": None,
        }
        for item in generations:
            joined["text"] += item.get("text") or ""
            joined["prompt_tokens"] = item.get("prompt_tokens", joined["prompt_tokens"])
            joined["generated_tokens"] += item.get("generated_tokens") or 0
            if item.get("finish_reason"):
                joined["finish_reason"] = item["finish_reason"]
        return joined

    async def stream_generate(
        self,
        request_id: str,
        prompt: str,
        params: BaseSamplerRequest,
        disconnect_handler: DisconnectHandler = None,
        mm_embeddings=None,
        filter_trigger: str = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        async with self.load_condition:
            await self.load_condition.wait_for(lambda: not self.load_lock.locked())
        if not self.loaded:
            raise RuntimeError("Model is being unloaded. Cannot process new generation requests.")
        self.active_job_ids[request_id] = True
        try:
            async for chunk in self.generate_gen(request_id, prompt, params, disconnect_handler):
                yield chunk
        finally:
            self.active_job_ids.pop(request_id, None)

    async def generate_gen(
        self,
        request_id: str,
        prompt: str,
        params: BaseSamplerRequest,
        disconnect_handler: DisconnectHandler = None,
        mm_embeddings=None,
        filter_trigger: str = None,
    ):
        settings = ExLlamaV2Sampler.Settings()
        settings.temperature = unwrap(params.temperature, 1.0)
        settings.top_k = unwrap(params.top_k, 0)
        settings.top_p = unwrap(params.top_p, 1.0)
        if hasattr(settings, "min_p"):
            settings.min_p = unwrap(params.min_p, 0.0)
        settings.token_repetition_penalty = unwrap(params.repetition_penalty, 1.0)
        add_bos = unwrap(params.add_bos_token, True)
        input_ids = self.tokenizer.encode(prompt, add_bos=add_bos)
        prompt_tokens = int(input_ids.shape[-1])
        max_tokens = unwrap(params.max_tokens, 0)
        if max_tokens <= 0:
            max_tokens = max(1, self.max_seq_len - prompt_tokens - 1)

        def _run():
            chunks: list[dict] = []
            self.generator.begin_stream(input_ids, settings)
            produced = 0
            while produced < max_tokens:
                chunk, eos, _tokens = self.generator.stream()
                produced += 1
                finish = "stop" if eos else None
                chunks.append(
                    {
                        "text": chunk or "",
                        "prompt_tokens": prompt_tokens,
                        "generated_tokens": 1 if chunk else 0,
                        "finish_reason": finish,
                    }
                )
                if eos:
                    break
            if not chunks or not chunks[-1].get("finish_reason"):
                chunks.append(
                    {
                        "text": "",
                        "prompt_tokens": prompt_tokens,
                        "generated_tokens": 0,
                        "finish_reason": "length",
                    }
                )
            return chunks

        chunks = await asyncio.to_thread(_run)
        handler = disconnect_handler
        for item in chunks:
            if handler and getattr(handler, "abort_event", None) and handler.abort_event.is_set():
                item["finish_reason"] = "abort"
                yield item
                return
            yield item
