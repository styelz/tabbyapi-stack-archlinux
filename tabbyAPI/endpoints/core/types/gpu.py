from typing import List, Optional

from pydantic import BaseModel, Field


class GpuModeRequest(BaseModel):
    mode: str = Field(description="comfy/flux/image, llm, llama, or a profile alias such as qwen")


class GpuModeResponse(BaseModel):
    mode: str
    tabby_model: Optional[str] = None
    comfy_up: bool = False
    llama_up: bool = False
    message: str = ""


class ImageGenerationItem(BaseModel):
    prompt: str
    size: Optional[str] = None
    n: int = 1
    seed: Optional[int] = None


class ImageGenerationRequest(BaseModel):
    prompt: Optional[str] = None
    size: Optional[str] = "1024x1024"
    n: int = 1
    seed: Optional[int] = None
    model: Optional[str] = None
    restore: Optional[bool] = Field(
        default=None,
        description="Reload the last LLM after generating. Default true when an LLM was loaded.",
    )
    images: Optional[List[ImageGenerationItem]] = Field(
        default=None,
        description="Different prompts in one Comfy session. Prefer this over repeated POSTs.",
    )


class ImageGenerationData(BaseModel):
    b64_json: Optional[str] = None
    url: Optional[str] = None
    revised_prompt: Optional[str] = None


class ImageGenerationResponse(BaseModel):
    created: int
    data: List[ImageGenerationData] = Field(default_factory=list)
    message: str = ""
