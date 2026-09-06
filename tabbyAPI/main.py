"""The main tabbyAPI module. Contains the FastAPI server and endpoints."""

# Set this env var for cuda malloc async before torch is initalized
import os

import argparse
import asyncio
import pathlib
import platform
import signal
from loguru import logger
from typing import Optional

from common import gen_logging, sampling
from common.args import convert_args_to_dict, init_argparser
from common.auth import load_auth_keys
from common.actions import run_subcommand
from common.logger import setup_logger, xlogger
from common.networking import is_port_in_use
from common.optional_dependencies import dependencies
from common.signals import signal_handler
from common.tabby_config import config
from common.vram_recover import FALLBACK_PROFILE, is_vram_error, mark_fallback


async def _load_startup_model(model, model_name: str) -> None:
    """Load the configured LLM. On VRAM failure, fall back to qwen and stay up."""
    model_path = pathlib.Path(config.model.model_dir) / model_name
    try:
        await model.load_model(
            model_path.resolve(),
            **config.model.model_dump(exclude_none=True),
            draft_model=config.draft_model.model_dump(exclude_none=True),
        )
        if config.lora.loras:
            lora_dir = pathlib.Path(config.lora.lora_dir)
            await model.container.load_loras(lora_dir.resolve(), **config.lora.model_dump())
        return
    except Exception as exc:
        logger.error(f"Startup model load failed ({model_name}): {exc}")
        if model.container:
            try:
                await model.unload_model(skip_wait=True)
            except Exception as unload_exc:
                logger.warning(
                    f"Could not unload leftover model after startup failure: {unload_exc}"
                )
        if not is_vram_error(exc):
            raise

    from select_model import PROFILES_DIR, apply_profile, available_profiles, load_yaml

    if FALLBACK_PROFILE not in available_profiles():
        logger.error("No qwen profile to fall back to; starting with no LLM")
        return
    _, fallback = load_yaml(PROFILES_DIR / f"{FALLBACK_PROFILE}.yml")
    fb_model = fallback.get("model") or {}
    fb_name = fb_model.get("model_name")
    if not fb_name or fb_name == model_name:
        logger.error("Daily model also failed to load; starting with no LLM")
        return

    logger.warning(f"Falling back to {FALLBACK_PROFILE} so the API stays up")
    apply_profile(FALLBACK_PROFILE)
    mark_fallback(model_name, FALLBACK_PROFILE)
    fb_path = pathlib.Path(config.model.model_dir) / fb_name
    load_kwargs = {key: value for key, value in fb_model.items() if key != "model_name"}
    try:
        await model.load_model(
            fb_path.resolve(),
            **load_kwargs,
            draft_model=fallback.get("draft_model") or {},
        )
    except Exception as exc:
        logger.error(f"Fallback {FALLBACK_PROFILE} failed: {exc}; starting with no LLM")
        if model.container:
            try:
                await model.unload_model(skip_wait=True)
            except Exception:
                pass


async def entrypoint_async():
    from common import model
    from endpoints.server import start_api

    """Async entry function for program startup"""

    host = config.network.host
    port = config.network.port

    # Check if the port is available and attempt to bind a fallback
    if is_port_in_use(port):
        fallback_port = port + 1

        if is_port_in_use(fallback_port):
            logger.error(
                f"Ports {port} and {fallback_port} are in use by different services.\n"
                "Please free up those ports or specify a different one.\n"
                "Exiting."
            )

            return
        else:
            logger.warning(f"Port {port} is currently in use. Switching to {fallback_port}.")

            port = fallback_port

    # If an initial model name is specified, create a container
    # and load the model. Skip when Flux owns the GPU so a Tabby
    # restart does not OOM-loop against ComfyUI.
    model_name = config.model.model_name
    from common.gpu_mode import should_skip_startup_load

    if model_name and should_skip_startup_load():
        logger.info(
            "GPU mode is comfy; not loading the LLM. "
            "Send switch to qwen when you want the model back."
        )
        model_name = None
    if model_name:
        from select_model import retarget_startup_model

        new_name, changed = retarget_startup_model(model_name)
        if changed:
            logger.warning(
                f"Startup model {model_name} is not on disk; "
                f"switching to {new_name or '(none)'}"
            )
            config.load()
            model_name = config.model.model_name if new_name else None
        elif not new_name:
            logger.error(
                f"Startup model {model_name} is not on disk and no other LLM "
                "is installed; starting with no LLM"
            )
            model_name = None
        else:
            model_name = new_name
    if model_name:
        await _load_startup_model(model, model_name)

    # If an initial embedding model name is specified, create a separate container
    # and load the model
    embedding_model_name = config.embeddings.embedding_model_name
    if embedding_model_name:
        embedding_model_path = pathlib.Path(config.embeddings.embedding_model_dir)
        embedding_model_path = embedding_model_path / embedding_model_name

        try:
            await model.load_embedding_model(
                embedding_model_path,
                embeddings_device=config.embeddings.embeddings_device,
            )
        except ImportError as ex:
            logger.error(ex.msg)

    # Initialize auth keys
    await load_auth_keys(config.network.disable_auth)

    gen_logging.broadcast_status()

    # Set sampler parameter overrides if provided
    sampling_override_preset = config.sampling.override_preset
    if sampling_override_preset:
        try:
            await sampling.overrides_from_file(sampling_override_preset)
        except FileNotFoundError as e:
            logger.warning(str(e))

    from images.jobs import abandon_inflight_jobs, resume_persisted_jobs

    resumed = await resume_persisted_jobs()
    if resumed:
        logger.info(f"Resumed {resumed} interrupted image job(s)")

    await start_api(host, port)

    # Uvicorn has finished serving; unload any loaded models so pending
    # jobs are cancelled and the generator is closed cleanly
    abandon_inflight_jobs("TabbyAPI is shutting down.")
    if model.container:
        await model.unload_model(skip_wait=True, shutdown=True)

    if model.embeddings_container:
        await model.unload_embedding_model()


def entrypoint(
    args: Optional[argparse.Namespace] = None,
    parser: Optional[argparse.ArgumentParser] = None,
):
    setup_logger()

    # Set up signal aborting
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if platform.system() == "Windows":
        from winloop import install
    else:
        from uvloop import install

    # Set loop event policy
    install()

    # Parse and override config from args
    if args is None:
        parser = init_argparser()
        args = parser.parse_args()

    dict_args = convert_args_to_dict(args, parser)

    # load config
    config.load(dict_args)

    # optionally enable seqlog logging
    if config.developer.seqlog:
        xlogger.setup(
            seqlog_url=config.developer.seqlog_server_url,
            api_key=config.developer.seqlog_api_key,
        )

    # We need to configure the allocator before importing Torch
    if config.memory.cuda_malloc_async:
        env_key1 = "PYTORCH_ALLOC_CONF"
        env_key2 = "PYTORCH_CUDA_ALLOC_CONF"
        new_alloc_config = "backend:cudaMallocAsync"
        prev_alloc_config = os.environ.get(env_key1, os.environ.get(env_key2))
        os.environ[env_key1] = new_alloc_config
        os.environ[env_key2] = new_alloc_config
        import sys

        if "torch" in sys.modules and prev_alloc_config != new_alloc_config:
            xlogger.warning(
                "`torch` was imported before config could be loaded. Unable to configure "
                "allocator backend, using existing env setting: "
                + (prev_alloc_config or "(Torch default)")
            )
        else:
            xlogger.info("Configured backend: cudaMallocAsync")

    # branch to default paths if required
    if run_subcommand(args):
        return

    # Check inference dependencies and give a descriptive error if they are missing
    # Skip if launching unsafely
    if config.developer.unsafe_launch:
        logger.warning(
            "UNSAFE: Skipping ExllamaV3 version check.\n"
            "If you aren't a developer, please keep this off!"
        )
    elif not dependencies.inference:
        install_message = (
            f"ERROR: Inference dependencies for TabbyAPI are not installed.\n"
            "Please update your environment by running an update script "
            "(update_scripts/"
            f"update_deps.{'bat' if platform.system() == 'Windows' else 'sh'})\n\n"
            "Or you can manually run a requirements update "
            "using the following command:\n\n"
            "For CUDA 12.x:\n"
            "pip install --upgrade .[cu12]\n\n"
            "For CUDA 13.x:\n"
            "pip install --upgrade .[cu13]\n\n"
        )

        raise SystemExit(install_message)

    # Set the process priority
    if config.developer.realtime_process_priority:
        import psutil

        current_process = psutil.Process(os.getpid())
        if platform.system() == "Windows":
            current_process.nice(psutil.REALTIME_PRIORITY_CLASS)
        else:
            current_process.nice(psutil.IOPRIO_CLASS_RT)

        logger.warning(
            "EXPERIMENTAL: Process priority set to Realtime. \n"
            "If you're not running on administrator/sudo, the priority is set to high."
        )

    try:
        from common.ssh_forwarder import ensure_ssh_forwarder

        ensure_ssh_forwarder()
    except Exception as exc:
        logger.warning(f"SSH reverse tunnel not started: {exc}")

    # Enter into the async event loop
    asyncio.run(entrypoint_async())


if __name__ == "__main__":
    entrypoint()
