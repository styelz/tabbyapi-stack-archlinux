import aiohttp
import base64
import io
import re

from fastapi import HTTPException
from PIL import Image

from common.networking import (
    handle_request_error,
)
from common.tabby_config import config


async def get_image(url: str) -> Image:
    if url.startswith("data:image"):
        # Handle base64 image
        match = re.match(r"^data:image\/[a-zA-Z0-9]+;base64,(.*)$", url)
        if match:
            base64_image = match.group(1)
            bytes_image = base64.b64decode(base64_image)
        else:
            error_message = handle_request_error(
                "Failed to read base64 image input.",
                exc_info=False,
            ).error.message

            raise HTTPException(400, error_message)

    else:
        # Handle image URL
        if config.network.disable_fetch_requests:
            error_message = handle_request_error(
                f"Failed to fetch image from {url} as fetch requests are disabled.",
                exc_info=False,
            ).error.message

            raise HTTPException(400, error_message)

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    bytes_image = await response.read()
                else:
                    error_message = handle_request_error(
                        f"Failed to fetch image from {url}.",
                        exc_info=False,
                    ).error.message

                    raise HTTPException(400, error_message)

    image = Image.open(io.BytesIO(bytes_image))
    return _ensure_vision_size(image)


# Qwen2/3-VL smart_resize rejects edges <= 32. Nearest-neighbor keeps a 1x1
# pixel's color instead of 500-ing the chat.
_VISION_MIN_EDGE = 64


def _ensure_vision_size(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width >= _VISION_MIN_EDGE and height >= _VISION_MIN_EDGE:
        return image
    scale = max(
        _VISION_MIN_EDGE / max(width, 1),
        _VISION_MIN_EDGE / max(height, 1),
    )
    return image.resize(
        (
            max(_VISION_MIN_EDGE, int(round(width * scale))),
            max(_VISION_MIN_EDGE, int(round(height * scale))),
        ),
        Image.NEAREST,
    )
