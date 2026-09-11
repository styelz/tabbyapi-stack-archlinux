"""python -m sidecar"""

from __future__ import annotations

import asyncio
import os

import uvicorn

from sidecar.app import create_app
from sidecar.backend_key import ensure_backend_key
from sidecar.settings import sidecar_host, sidecar_port


async def _serve() -> None:
    ensure_backend_key()
    config = uvicorn.Config(
        create_app(),
        host=sidecar_host(),
        port=sidecar_port(),
        loop=asyncio.get_running_loop(),
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    os.environ["TABBY_PROCESS"] = "sidecar"
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
