"""Time warm LLM / Comfy switches and first image jobs. Writes switch_times.json."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from common.gpu_mode import comfy_up, generate_image, save_generated_image
from common.switch_times import LOCAL_TIMES_PATH, TIMES_PATH, detect_gpu
from select_model import available_profiles, last_profile
from switch_model import (
    api_base,
    current_model,
    request_json,
    server_up,
    switch_to_comfy,
    switch_to_llm,
    unload_tabby,
)

LLM_ORDER = ("qwen35", "qwen36", "gemma", "gemma26", "glm", "qwen")
RESTORE_PROFILE = "qwen"


KEEP_KEYS = (
    "qwen",
    "qwen35",
    "qwen36",
    "gemma",
    "gemma26",
    "glm",
    "comfy",
    "llm",
)


def _write_times(path: Path, data: dict) -> None:
    merged = {}
    if path.is_file():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                merged.update(prev)
        except (json.JSONDecodeError, OSError):
            pass
    for key in KEEP_KEYS:
        if key in merged and key not in data:
            data[key] = merged[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if path.resolve() == TIMES_PATH.resolve():
        # A fresh bench is the new baseline; live EMA learning starts over.
        LOCAL_TIMES_PATH.unlink(missing_ok=True)


def _stop_comfy_unit() -> None:
    if sys.platform == "win32":
        return
    subprocess.run(["systemctl", "--user", "stop", "comfyui"], check=False)


def _start_comfy_unit() -> None:
    if sys.platform == "win32":
        return
    subprocess.run(["systemctl", "--user", "start", "comfyui"], check=False)


def ping_chat(base: str, timeout: float = 180) -> float:
    started = time.time()
    result = request_json(
        "POST",
        f"{base}/v1/chat/completions",
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "max_tokens": 16,
            "stream": False,
        },
        timeout=timeout,
    )
    elapsed = time.time() - started
    if not isinstance(result, dict):
        raise RuntimeError(f"chat ping failed: {result}")
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError(f"chat ping returned no choices: {result}")
    return elapsed


def _load_or_retry(name: str, base: str) -> dict:
    try:
        return switch_to_llm(name, base=base, force=True, recover=False, record=False)
    except (SystemExit, RuntimeError, OSError) as exc:
        print(f"  load failed ({exc}); unloading leftovers, stopping idle Comfy, retrying")
        try:
            unload_tabby(base)
        except SystemExit:
            pass
        _stop_comfy_unit()
        time.sleep(3)
        return switch_to_llm(name, base=base, force=True, recover=False, record=False)


def bench_llm(name: str, base: str, results: dict) -> None:
    print(f"\n=== LLM {name} ===")
    try:
        info = _load_or_retry(name, base)
    except (SystemExit, Exception) as exc:
        results[name] = {"ready_s": None, "error": str(exc)}
        print(f"  giving up: {exc}")
        _start_comfy_unit()
        return
    _start_comfy_unit()

    entry = {"ready_s": round(float(info.get("ready_s") or 0), 1)}
    try:
        entry["first_token_s"] = round(ping_chat(base), 1)
        print(f"  first token: {entry['first_token_s']:.1f}s")
    except Exception as exc:
        entry["first_token_error"] = str(exc)
        print(f"  first token failed: {exc}")
    results[name] = entry


def bench_comfy(base: str, results: dict, skip_images: bool) -> None:
    print("\n=== Comfy ===")
    info = switch_to_comfy(base, record=False)
    results["comfy"] = {
        "ready_s": round(float(info.get("ready_s") or 0), 1),
    }
    if not comfy_up():
        results["comfy"]["error"] = "ComfyUI did not answer /system_stats"
        return
    if skip_images:
        return

    print("  Flux Schnell 512x512...")
    started = time.time()
    try:
        raw = generate_image("a red cube on a white table, simple", size="512x512", timeout=300)
        dest = save_generated_image(raw)
        results["comfy"]["flux_s"] = round(time.time() - started, 1)
        results["comfy"]["flux_path"] = dest.name
        print(f"  flux: {results['comfy']['flux_s']:.1f}s -> {dest.name}")
    except Exception as exc:
        results["comfy"]["flux_error"] = str(exc)
        print(f"  flux failed: {exc}")

    print("  Qwen-Image 512x512...")
    started = time.time()
    try:
        raw = generate_image("qwen-image: SALE in bold red letters", size="512x512", timeout=600)
        dest = save_generated_image(raw)
        results["comfy"]["qwen_image_s"] = round(time.time() - started, 1)
        results["comfy"]["qwen_image_path"] = dest.name
        print(f"  qwen-image: {results['comfy']['qwen_image_s']:.1f}s -> {dest.name}")
    except Exception as exc:
        results["comfy"]["qwen_image_error"] = str(exc)
        print(f"  qwen-image failed: {exc}")


def bench_restore(base: str, results: dict) -> None:
    print("\n=== switch to llm ===")
    name = last_profile() or RESTORE_PROFILE
    info = switch_to_llm(name, base=base, force=False, recover=False, record=False)
    results["llm"] = {
        "ready_s": round(float(info.get("ready_s") or 0), 1),
        "profile": name,
    }
    try:
        results["llm"]["first_token_s"] = round(ping_chat(base), 1)
    except Exception as exc:
        results["llm"]["first_token_error"] = str(exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure TabbyAPI / Comfy switch times")
    parser.add_argument(
        "--only",
        nargs="*",
        help="Subset: profile names plus comfy, flux, qwen-image, llm",
    )
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--out", type=Path, default=TIMES_PATH)
    parser.add_argument("--no-restore", action="store_true", help="Do not force qwen at the end")
    return parser.parse_args()


def run_bench(
    only: list[str] | None = None,
    skip_images: bool = False,
    out: Path | None = None,
    no_restore: bool = False,
) -> dict:
    dest = out or TIMES_PATH
    base = api_base()
    if not server_up(base):
        raise SystemExit(f"TabbyAPI is not running at {base}")

    only_set = {item.lower() for item in (only or [])}
    installed = set(available_profiles())
    profiles = [name for name in LLM_ORDER if name in installed]
    if only_set:
        profiles = [name for name in profiles if name in only_set]

    gpu = detect_gpu()
    results: dict = {
        "gpu": gpu.get("label") or "unknown GPU",
        "gpu_name": gpu.get("name"),
        "vram_mib": gpu.get("vram_mib"),
        "note": "Warm switches. First boot may compile Triton longer.",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "from_model": current_model(base),
    }

    for name in profiles:
        bench_llm(name, base, results)
        _write_times(dest, results)

    want_comfy = (not only_set) or bool(only_set & {"comfy", "flux", "qwen-image"})
    if want_comfy:
        bench_comfy(base, results, skip_images=skip_images or (only_set == {"comfy"}))
        _write_times(dest, results)
        if (not only_set) or ("llm" in only_set) or not skip_images:
            bench_restore(base, results)
            _write_times(dest, results)

    if not no_restore:
        print(f"\n=== restore {RESTORE_PROFILE} ===")
        switch_to_llm(RESTORE_PROFILE, base=base, force=False, recover=False, record=False)
        results["restored"] = RESTORE_PROFILE
        _write_times(dest, results)

    print(f"\nWrote {dest}")
    print(json.dumps(results, indent=2))
    return results


def main() -> int:
    args = parse_args()
    run_bench(
        only=args.only,
        skip_images=args.skip_images,
        out=args.out,
        no_restore=args.no_restore,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
