"""Apply a model profile and hot-swap TabbyAPI without restarting the process."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.gpu_mode import (
    COMFY_DIR,
    COMFY_PYTHON,
    GPU_ALIASES,
    comfy_up,
    persisted_jobs_block_llm_load,
    start_comfy_if_needed,
    stop_comfy,
    write_mode,
)
from common.switch_times import record_ready

from common.vram_recover import (
    FALLBACK_PROFILE,
    bounce_is_cooling,
    health_timeout_s,
    is_vram_error,
    mark_bounce,
    mark_fallback,
)
from select_model import (
    CONFIG_PATH,
    apply_profile,
    ask_profile,
    available_profiles,
    disable_profile_vision,
    last_profile,
    load_yaml,
    profile_aliases,
)

from common.load_fields import load_payload

LOCK = Path(__file__).resolve().parent / "switch-model.lock"
AUTH_FILE = Path(__file__).resolve().parent / "api_tokens.yml"


def admin_headers() -> dict[str, str]:
    """X-Admin-Key for /v1/model/* and the sampler switch.

    TABBY_ADMIN_KEY wins; otherwise the admin_key in api_tokens.yml next to
    this script. Empty when neither exists (disable_auth installs).
    """
    key = (os.environ.get("TABBY_ADMIN_KEY") or "").strip()
    if not key and AUTH_FILE.is_file():
        try:
            _, data = load_yaml(AUTH_FILE)
        except Exception:
            data = None
        if isinstance(data, dict):
            key = str(data.get("admin_key") or "").strip()
    if not key:
        return {}
    # Admin routes read X-Admin-Key; chat/model routes read Authorization.
    return {"X-Admin-Key": key, "Authorization": f"Bearer {key}"}


def api_base() -> str:
    _, config = load_yaml(CONFIG_PATH)
    network = config.get("network") or {}
    host = network.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = network.get("port") or 5000
    return f"http://{host}:{port}"


def request_json(method: str, url: str, payload: dict | None = None, timeout: float = 30):
    data = None
    headers = admin_headers()
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode())


def server_up(base: str) -> bool:
    try:
        request_json("GET", f"{base}/health", timeout=3)
        return True
    except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def current_model(base: str) -> str | None:
    try:
        card = request_json("GET", f"{base}/v1/model", timeout=10)
    except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    if isinstance(card, dict):
        return card.get("id")
    return None


def switch_sampler(base: str, preset: str | None):
    if not preset:
        return
    try:
        request_json(
            "POST",
            f"{base}/v1/sampling/override/switch",
            {"preset": preset},
            timeout=15,
        )
        print(f"  sampler: {preset}")
    except HTTPError as exc:
        print(f"  sampler switch failed: {exc.read().decode('utf-8', 'replace')}")
        raise SystemExit(1) from exc


def load_model(base: str, model_name: str, model_cfg: dict):
    payload = load_payload(model_name, model_cfg)

    print(f"Loading {model_name} (this can take a minute)...")
    req = Request(
        f"{base}/v1/model/load",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **admin_headers(),
        },
        method="POST",
    )
    finished = False
    error = None
    with urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                print(f"  {data}")
                continue
            if not isinstance(event, dict):
                continue
            if event.get("error"):
                error = event["error"]
                break
            status = event.get("status")
            module = event.get("module")
            modules = event.get("modules")
            model_type = event.get("model_type", "model")
            if status == "finished":
                print(f"  {model_type}: finished")
                finished = True
            elif status == "processing" and module and modules:
                print(f"  {model_type}: {module}/{modules}", end="\r")
    print()
    if error:
        raise SystemExit(f"Load failed: {error}")
    if not finished:
        raise SystemExit("Load stream ended before the model finished loading.")


def unload_tabby(base: str) -> None:
    loaded = current_model(base)
    if not loaded:
        print("TabbyAPI already unloaded")
        return
    print(f"Unloading {loaded}...")
    try:
        request_json("POST", f"{base}/v1/model/unload", timeout=180)
    except HTTPError as exc:
        if exc.code == 503:
            print("TabbyAPI already unloaded")
            return
        raise SystemExit(f"Unload failed: {exc.read().decode('utf-8', 'replace')}") from exc
    print("  LLM unloaded")


def wait_until_up(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_up(base):
            return True
        time.sleep(2)
    return False


def bounce_api(base: str, profile: str) -> bool:
    """One systemd bounce. reset-failed so start-limit cannot strand us."""
    from restart_stack import abandon_persisted_jobs, restart_units

    print("  bouncing TabbyAPI once for a fresh CUDA context")
    abandon_persisted_jobs()
    restart_units("llm")
    mark_bounce(profile)
    if not wait_until_up(base, health_timeout_s(profile)):
        print("  bounce: API did not become healthy")
        return False
    print("  bounce: healthy")
    return True


def fallback_name(failed: str) -> str | None:
    names = available_profiles()
    if FALLBACK_PROFILE in names and FALLBACK_PROFILE != failed:
        return FALLBACK_PROFILE
    for name in names:
        if name != failed:
            return name
    return None


def load_fallback(base: str, failed: str) -> dict:
    dest = fallback_name(failed)
    if not dest:
        raise SystemExit(f"Load failed and no fallback profile is available (tried {failed})")
    print(f"  falling back to {dest}")
    profile = apply_profile(dest)
    model_cfg = profile.get("model") or {}
    model_name = model_cfg.get("model_name")
    preset = (profile.get("sampling") or {}).get("override_preset")
    if not model_name:
        raise SystemExit(f"Profile {dest} has no model_name")
    write_mode("llm", profile=dest)
    switch_sampler(base, preset)
    load_model(base, model_name, model_cfg)
    mark_fallback(failed, dest)
    return {"profile": dest, "loaded": model_name, "recovered": "fallback"}


def recover_after_vram(base: str, name: str, model_name: str, model_cfg: dict, preset: str | None) -> dict:
    """Retry leftovers, bounce once, then fall back to qwen. Never loops."""
    if model_cfg.get("vision"):
        print("  vision disabled after VRAM failure; retrying text-only")
        try:
            updated = disable_profile_vision(name)
            fresh = dict(updated.get("model") or {})
            fresh.pop("model_name", None)
            model_cfg.clear()
            model_cfg.update(fresh)
        except SystemExit as exc:
            print(f"  could not persist vision off: {exc}")
        model_cfg["vision"] = False

    print("  freeing leftover VRAM and retrying once")
    try:
        unload_tabby(base)
    except SystemExit:
        pass
    stop_comfy()
    time.sleep(2)
    try:
        load_model(base, model_name, model_cfg)
        return {"profile": name, "loaded": model_name, "recovered": "retry"}
    except SystemExit as exc:
        if not is_vram_error(exc):
            raise
        print(f"  {exc}")

    if bounce_is_cooling():
        print("  skip bounce (already bounced recently); falling back")
        return load_fallback(base, name)

    if bounce_api(base, name):
        loaded = current_model(base)
        if loaded == model_name:
            print(f"  recovered after bounce: {loaded}")
            return {"profile": name, "loaded": loaded, "recovered": "bounce"}
        if loaded:
            print(f"  {name} did not fit after bounce; keeping {loaded}")
            mark_fallback(name, last_profile() or FALLBACK_PROFILE)
            return {
                "profile": last_profile() or name,
                "loaded": loaded,
                "recovered": "fallback",
            }
        try:
            switch_sampler(base, preset)
            load_model(base, model_name, model_cfg)
            return {"profile": name, "loaded": model_name, "recovered": "bounce"}
        except SystemExit as exc:
            if not is_vram_error(exc):
                raise
            print(f"  {exc}")

    if not server_up(base):
        raise SystemExit("TabbyAPI did not recover after a VRAM bounce")
    return load_fallback(base, name)


def switch_to_comfy(base: str, record: bool = True) -> dict:
    started = time.time()
    was_up = comfy_up()
    if server_up(base):
        unload_tabby(base)
    else:
        print(f"TabbyAPI is not running at {base}; starting ComfyUI anyway.")
    write_mode("comfy")
    start_comfy_if_needed()
    elapsed = time.time() - started
    print("GPU mode: comfy")
    print("Remote clients: describe the image in chat or POST /v1/images/generations")
    print(f"Comfy ready ({elapsed:.0f}s)")
    if record and not was_up:
        # Only a real Comfy start is a warm switch worth learning from.
        record_ready("comfy", elapsed)
    return {"ready_s": elapsed, "mode": "comfy"}


def wait_out_image_jobs(poll_s: float = 0.5) -> None:
    """Do not stop Comfy while a render is still generating."""
    if not persisted_jobs_block_llm_load():
        return
    print("Waiting for in-flight image job to finish before loading the LLM...")
    while persisted_jobs_block_llm_load():
        time.sleep(poll_s)


def switch_to_llm(
    name: str,
    base: str | None = None,
    force: bool = False,
    recover: bool = True,
    record: bool = True,
) -> dict:
    """Apply a profile, free Comfy, and load the model. Returns timing metadata.

    recover=True (chat / CLI): leftover retry, one bounce, then qwen.
    recover=False (calibrate / bench): raise on the first load failure.
    record=True: blend a clean warm load into switch_times.json (the
    screensaver / chat "typical"). Bench passes False; it writes raw numbers.
    """
    profile = apply_profile(name)
    model_cfg = profile.get("model") or {}
    model_name = model_cfg.get("model_name")
    preset = (profile.get("sampling") or {}).get("override_preset")
    if not model_name:
        raise SystemExit(f"Profile {name} has no model_name")

    if base is None:
        base = api_base()
    if not server_up(base):
        print(f"TabbyAPI is not running at {base}.")
        starter = "start.bat" if os.name == "nt" else "./start.sh"
        print(f"Profile is saved. Start it with {starter} (press Enter to keep this model).")
        return {"ready_s": 0.0, "already": False, "loaded": None, "profile": name, "offline": True}

    started = time.time()
    wait_out_image_jobs()
    from_comfy = comfy_up()
    stop_comfy()
    write_mode("llm", profile=name)
    loaded = current_model(base)
    already = loaded == model_name and not force
    if already:
        switch_sampler(base, preset)
        print(f"Already loaded: {loaded}")
        print("GPU mode: llm")
        return {
            "ready_s": time.time() - started,
            "already": True,
            "loaded": loaded,
            "profile": name,
        }
    if loaded == model_name and force:
        unload_tabby(base)
    switch_sampler(base, preset)
    recovered = None
    try:
        load_model(base, model_name, model_cfg)
    except SystemExit as exc:
        if not recover or not is_vram_error(exc):
            raise
        print(f"  {exc}")
        extra = recover_after_vram(base, name, model_name, model_cfg, preset)
        name = extra.get("profile") or name
        recovered = extra.get("recovered")
    loaded = current_model(base)
    elapsed = time.time() - started
    print(f"Now loaded: {loaded} ({elapsed:.0f}s)")
    if recovered:
        print(f"  recover: {recovered}")
    print("GPU mode: llm")
    if record and not recovered and loaded == model_name:
        record_ready(name, elapsed)
        if from_comfy:
            # "switch to llm" / restore-after-pictures includes the Comfy stop.
            record_ready("llm", elapsed)
    return {
        "ready_s": elapsed,
        "already": False,
        "loaded": loaded,
        "profile": name,
        "recovered": recovered,
    }


def resolve_name(raw: str | None) -> str:
    names = available_profiles()
    if not raw:
        if sys.stdin.isatty():
            return ask_profile()
        return last_profile() if last_profile() in names else (names[0] if names else "qwen")
    token = raw.strip()
    lowered = token.lower()
    if lowered == "llm":
        return last_profile() if last_profile() in names else (names[0] if names else "qwen")
    if lowered in GPU_ALIASES:
        return GPU_ALIASES[lowered]
    aliases = profile_aliases()
    name = aliases.get(token) or aliases.get(lowered) or aliases.get(token.upper())
    if not name:
        raise SystemExit(f"Unknown model {raw!r}. Use: {', '.join(names)}, comfy")
    return name


def main():
    parser = argparse.ArgumentParser(
        description="Switch TabbyAPI models or hand the GPU to ComfyUI"
    )
    parser.add_argument("profile", nargs="?", help="qwen, qwen35, comfy, flux, llm, ...")
    parser.add_argument(
        "--no-load",
        action="store_true",
        help="Write the profile only; do not call /v1/model/load",
    )
    args = parser.parse_args()

    name = resolve_name(args.profile)
    if name == "comfy":
        if args.no_load:
            write_mode("comfy")
            print(
                f"GPU mode set to comfy. Start ComfyUI with {COMFY_PYTHON} {COMFY_DIR / 'main.py'}"
            )
            return 0
        switch_to_comfy(api_base())
        return 0

    if args.no_load:
        apply_profile(name)
        print("Profile written. Start or restart TabbyAPI to load it.")
        return 0

    switch_to_llm(name)
    print("Cursor can stay on gpt-4o.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        LOCK.unlink(missing_ok=True)
