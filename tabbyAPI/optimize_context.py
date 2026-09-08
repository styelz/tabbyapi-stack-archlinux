"""Climb cache_size / max_seq_len per profile until load fails. Run from the live install."""

from __future__ import annotations

import argparse
import subprocess
import time

from ruamel.yaml import YAML

from common.gpu_mode import comfy_up, start_comfy_if_needed
from select_model import PROFILES_DIR, apply_profile
from switch_model import (
    api_base,
    current_model,
    request_json,
    server_up,
    switch_to_llm,
    unload_tabby,
)

STEPS = (4096, 8192, 16384, 32768, 65536, 98304, 131072, 163840, 196608, 262144)
ARCH_MAX = {
    "qwen": 262144,
    "qwen35": 262144,
    "qwen36": 262144,
    "gemma": 262144,
    "gemma26": 262144,
    "glm": 65536,
}
RESTORE = "qwen"


def _yaml():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def read_profile(name: str):
    path = PROFILES_DIR / f"{name}.yml"
    yaml = _yaml()
    with path.open(encoding="utf-8") as handle:
        return yaml, yaml.load(handle), path


def write_seq(name: str, seq: int) -> None:
    yaml, data, path = read_profile(name)
    model_cfg = data.setdefault("model", {})
    model_cfg["max_seq_len"] = int(seq)
    model_cfg["cache_size"] = int(seq)
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


def current_seq(name: str) -> int:
    _, data, _ = read_profile(name)
    model_cfg = data.get("model") or {}
    return int(model_cfg.get("cache_size") or model_cfg.get("max_seq_len") or 65536)


def vram_mib() -> int | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
        )
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def wait_health(timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_up(api_base()):
            return
        time.sleep(2)
    raise SystemExit("TabbyAPI did not become healthy")


def recover() -> None:
    print("  recover: unload / restart on qwen")
    base = api_base()
    try:
        if server_up(base):
            unload_tabby(base)
    except SystemExit:
        pass
    apply_profile(RESTORE)
    subprocess.run(["systemctl", "--user", "reset-failed", "tabbyapi"], check=False)
    subprocess.run(["systemctl", "--user", "restart", "tabbyapi"], check=False)
    wait_health()
    start_comfy_if_needed()
    print(f"  recover: healthy, vram={vram_mib()}")


def ping(base: str) -> float:
    started = time.time()
    result = request_json(
        "POST",
        f"{base}/v1/chat/completions",
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "max_tokens": 8,
            "stream": False,
        },
        timeout=180,
    )
    elapsed = time.time() - started
    if not isinstance(result, dict) or not (result.get("choices") or []):
        raise RuntimeError(f"ping failed: {result}")
    return elapsed


def try_seq(name: str, seq: int) -> tuple[bool, str]:
    print(f"  try {name} ctx={seq}")
    write_seq(name, seq)
    base = api_base()
    try:
        info = switch_to_llm(name, base=base, force=True, recover=False, record=False)
    except (SystemExit, RuntimeError, OSError) as exc:
        print(f"  fail load: {exc}")
        recover()
        return False, f"load: {exc}"
    if info.get("offline") or not info.get("loaded"):
        recover()
        return False, "offline or empty load"
    try:
        ping_s = ping(base)
    except Exception as exc:
        print(f"  fail ping: {exc}")
        recover()
        return False, f"ping: {exc}"
    used = vram_mib()
    print(f"  ok ctx={seq} ready={info.get('ready_s'):.1f}s ping={ping_s:.1f}s vram={used}")
    return True, f"ready_s={info.get('ready_s'):.1f} vram={used}"


def climb(name: str) -> int:
    cap = ARCH_MAX[name]
    start = min(current_seq(name), cap)
    print(f"\n=== {name} current={start} cap={cap} ===")
    ok, _ = try_seq(name, start)
    if not ok:
        for seq in reversed([step for step in STEPS if step < start]):
            ok, _ = try_seq(name, seq)
            if ok:
                print(f"  settled {name} at {seq} (stepped down)")
                return seq
        print(f"  {name} did not load at any size")
        return start
    best = start
    for seq in STEPS:
        if seq <= start or seq > cap:
            continue
        ok, _ = try_seq(name, seq)
        if not ok:
            write_seq(name, best)
            print(f"  revert {name} to {best}")
            ok_best, _ = try_seq(name, best)
            if not ok_best:
                recover()
            break
        best = seq
    return best


def climb_all(profiles: list[str] | None = None) -> dict[str, int]:
    names = profiles or [name for name in ARCH_MAX if (PROFILES_DIR / f"{name}.yml").is_file()]
    results = {}
    for name in names:
        if name not in ARCH_MAX:
            raise SystemExit(f"unknown profile {name}")
        results[name] = climb(name)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Max out TabbyAPI context per profile")
    parser.add_argument("profiles", nargs="*", default=["qwen", "gemma", "qwen36", "qwen35", "gemma26"])
    args = parser.parse_args()

    if not server_up(api_base()):
        raise SystemExit("TabbyAPI is not running")
    if not comfy_up():
        start_comfy_if_needed()
    print(f"start vram={vram_mib()} model={current_model(api_base())} comfy={comfy_up()}")

    results = climb_all(args.profiles or None)

    print("\n=== results ===")
    for name, seq in results.items():
        print(f"  {name}: {seq}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
