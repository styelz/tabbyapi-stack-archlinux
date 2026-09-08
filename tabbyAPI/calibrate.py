"""Retest this GPU and rewrite wait times / context for a new machine.

Run from the live install (systemd already owns port 5000):

    tabbyAPI/venv/bin/python tabbyAPI/calibrate.py
    tabbyAPI/venv/bin/python tabbyAPI/calibrate.py --context
    tabbyAPI/venv/bin/python tabbyAPI/calibrate.py --docs-only

Writes model_profiles/switch_times.json (chat wait copy) and refreshes
the switch table in AGENTS.md plus the Arch deploy wait line. Optional
--context climbs cache sizes, including stepping down if this card is
smaller than the last one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from bench_switch import run_bench
from common.gpu_mode import comfy_up, start_comfy_if_needed
from common.switch_times import (
    TIMES_PATH,
    detect_gpu,
    extra_seconds,
    format_duration,
    gpu_label,
    load_switch_times,
    ready_seconds,
)
from optimize_context import ARCH_MAX, climb_all, read_profile
from select_model import PROFILES_DIR, available_profiles
from switch_model import api_base, server_up, switch_to_llm

ROOT = Path(__file__).resolve().parent
INSTALL_ROOT = ROOT.parent
AGENTS_PATH = INSTALL_ROOT / "AGENTS.md"

USES = {
    "qwen": "Daily coding, 9B",
    "qwen35": "Long or hard agent work",
    "qwen36": "Long or hard agent work",
    "gemma": "General",
    "gemma26": "General",
    "glm": "Thinking chat only (no coding tools)",
}

ALIAS_ORDER = ("qwen", "qwen35", "qwen36", "gemma", "gemma26", "glm")

SWITCH_BLOCK_RE = re.compile(
    r"(## Switch models\n\n)(.*?)(\n\nThe GPU is exclusive:)",
    re.S,
)
DEPLOY_WAIT_RE = re.compile(
    r"- After `switch to .`, wait for the GPU[^\n]+"
)


def _ctx_label(seq: int | None) -> str:
    if not seq:
        return "—"
    if seq >= 1000:
        return f"{seq // 1000}k"
    return str(seq)


def _ready_label(entry: dict | None) -> str:
    if not entry:
        return "—"
    if entry.get("ready_s") is None and entry.get("error"):
        return "failed to load"
    try:
        secs = float(entry["ready_s"])
    except (TypeError, ValueError, KeyError):
        return "—"
    return f"~{format_duration(secs)}"


def _compact(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    secs = float(seconds)
    if secs < 90:
        rounded = int(5 * round(secs / 5.0)) if secs >= 5 else max(1, int(round(secs)))
        return f"~{rounded}s"
    return f"~{max(1, int(round(secs / 60.0)))} min"


def _profile_meta(name: str, info: dict[str, dict] | None = None) -> dict[str, Any]:
    if info is not None:
        return dict(info.get(name) or {})
    path = PROFILES_DIR / f"{name}.yml"
    if not path.is_file():
        return {}
    try:
        _, data, _ = read_profile(name)
    except OSError:
        return {}
    model_cfg = data.get("model") or {}
    raw = model_cfg.get("max_seq_len") or model_cfg.get("cache_size")
    try:
        seq = int(raw)
    except (TypeError, ValueError):
        seq = None
    return {"seq": seq, "vision": bool(model_cfg.get("vision"))}


def _aliases(info: dict[str, dict] | None = None) -> list[str]:
    if info is not None:
        return [name for name in ALIAS_ORDER if name in info]
    return [name for name in ALIAS_ORDER if (PROFILES_DIR / f"{name}.yml").is_file()]


def agents_switch_block(
    times: dict,
    label: str,
    profiles: dict[str, dict] | None = None,
) -> str:
    lines = [
        "Send a message that is **only** one of these. "
        f"Times are warm switches on this {label} "
        "(first boot can compile Triton longer). "
        "Chat replies use `tabbyAPI/model_profiles/switch_times.json`; "
        "every real load blends its time into the screensaver and chat typicals.",
        "",
        "| Phrase | Use | Context | Ready |",
        "|---|---|---|---|",
        "| `help` | Full usage guide | — | — |",
        "| `list models` | Show installed profiles | — | — |",
        f"| `restart` | Bounce the API; last model reloads | — | {_ready_label(times.get('llm') if isinstance(times.get('llm'), dict) else None)} |",
    ]
    for alias in _aliases(profiles):
        meta = _profile_meta(alias, profiles)
        use = USES.get(alias, alias)
        if alias == "glm" and meta.get("vision") is False:
            use = f"Thinking chat only (no coding tools; vision off on {label})"
        ctx = _ctx_label(meta.get("seq"))
        if alias == "glm" and meta.get("seq") == ARCH_MAX.get("glm"):
            ctx = f"{ctx} (model max)"
        raw = times.get(alias)
        ready = _ready_label(raw if isinstance(raw, dict) else None)
        lines.append(f"| `switch to {alias}` | {use} | {ctx} | {ready} |")

    comfy = times.get("comfy") if isinstance(times.get("comfy"), dict) else {}
    extra = []
    flux = extra_seconds("comfy", "flux_s", times)
    qwen_img = extra_seconds("comfy", "qwen_image_s", times)
    if flux:
        extra.append(f"Flux ~{format_duration(flux)}")
    if qwen_img:
        extra.append(f"Qwen-Image ~{format_duration(qwen_img)}")
    comfy_ready = _ready_label(comfy)
    comfy_ctx = comfy_ready
    if extra:
        comfy_ctx = f"{comfy_ready} (then {' / '.join(extra)} for the first picture)"
    lines.append(
        f"| `switch to comfy` / `flux` | Unload the LLM; image gen | — | {comfy_ctx} |"
    )
    llm = times.get("llm") if isinstance(times.get("llm"), dict) else {}
    lines.append(
        f"| `switch to llm` | Free Comfy; reload the last LLM | — | {_ready_label(llm)} |"
    )
    return "\n".join(lines)


def rewrite_agents_md(
    path: Path,
    times: dict,
    label: str,
    profiles: dict[str, dict] | None = None,
) -> bool:
    if not path.is_file():
        print(f"  skip docs: {path} missing")
        return False
    text = path.read_text(encoding="utf-8")
    block = agents_switch_block(times, label, profiles)
    updated, n = SWITCH_BLOCK_RE.subn(
        lambda match: match.group(1) + block + match.group(3),
        text,
        count=1,
    )
    if n != 1:
        print(f"  skip docs: no Switch models table in {path}")
        return False
    path.write_text(updated, encoding="utf-8")
    print(f"  updated {path}")
    return True


def deploy_wait_line(times: dict, label: str) -> str:
    qwen = _compact(ready_seconds("qwen", times))
    qwen36 = _compact(ready_seconds("qwen36", times))
    gemma26 = _compact(ready_seconds("gemma26", times))
    qwen35 = _compact(ready_seconds("qwen35", times))
    glm = _compact(ready_seconds("glm", times))
    comfy = format_duration(ready_seconds("comfy", times))
    flux = extra_seconds("comfy", "flux_s", times)
    qwen_img = extra_seconds("comfy", "qwen_image_s", times)
    flux_s = _compact(float(flux)) if flux else "—"
    qwen_s = _compact(float(qwen_img)) if qwen_img else "—"
    vision = _profile_meta("glm").get("vision")
    glm_note = f"GLM is thinking-only on {label} (vision off)." if vision is False else ""
    return (
        f"- After `switch to …`, wait for the GPU (warm {label}: "
        f"qwen / gemma {qwen}, qwen36 {qwen36}, gemma26 {gemma26}, "
        f"qwen35 {qwen35}, glm {glm}). After `switch to comfy`, wait about "
        f"**{comfy}** (first Flux {flux_s}, first Qwen-Image {qwen_s}). {glm_note}"
    ).rstrip()


def _rewrite_regex(path: Path, pattern: re.Pattern[str], replacement: str, label: str) -> bool:
    if not path.is_file():
        print(f"  skip {label}: {path} missing")
        return False
    text = path.read_text(encoding="utf-8")
    updated, n = pattern.subn(replacement, text, count=1)
    if n != 1:
        print(f"  skip {label}: no matching paragraph in {path}")
        return False
    path.write_text(updated, encoding="utf-8")
    print(f"  updated {path}")
    return True


def rewrite_docs(times: dict, label: str, agents: Path) -> None:
    rewrite_agents_md(agents, times, label)
    deploy = ROOT / "deploy" / "arch" / "README.md"
    _rewrite_regex(deploy, DEPLOY_WAIT_RE, deploy_wait_line(times, label), "deploy README")


def _set_glm_pretty(vision: bool) -> None:
    yaml, data, path = read_profile("glm")
    label = gpu_label()
    if vision:
        data["pretty"] = "GLM-4.1V-9B-Thinking - vision and think"
    else:
        data["pretty"] = f"GLM-4.1V-9B-Thinking - think (vision off on {label})"
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


def try_glm_vision() -> bool:
    """On a roomier card, try GLM with vision. Revert if it OOMs."""
    if not (PROFILES_DIR / "glm.yml").is_file():
        return False
    yaml, data, path = read_profile("glm")
    model_cfg = data.setdefault("model", {})
    if model_cfg.get("vision"):
        return True
    print("\n=== glm vision probe ===")
    model_cfg["vision"] = True
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    _set_glm_pretty(True)
    try:
        info = switch_to_llm("glm", force=True, recover=False, record=False)
        if info.get("offline") or not info.get("loaded"):
            raise RuntimeError("glm did not load")
        print("  glm vision: loaded")
        return True
    except (SystemExit, RuntimeError, OSError) as exc:
        print(f"  glm vision: failed ({exc}); leaving vision off")
        yaml, data, path = read_profile("glm")
        data.setdefault("model", {})["vision"] = False
        with path.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle)
        _set_glm_pretty(False)
        return False


def sync_source(source: Path) -> None:
    dest_root = source.resolve()
    pairs = [
        (TIMES_PATH, dest_root / "tabbyAPI" / "model_profiles" / "switch_times.json"),
        (AGENTS_PATH, dest_root / "AGENTS.md"),
    ]
    readme = INSTALL_ROOT / "README.md"
    if readme.is_file():
        pairs.append((readme, dest_root / "README.md"))
    deploy = ROOT / "deploy" / "arch" / "README.md"
    if deploy.is_file():
        pairs.append((deploy, dest_root / "tabbyAPI" / "deploy" / "arch" / "README.md"))
    for name in available_profiles():
        src = PROFILES_DIR / f"{name}.yml"
        if src.is_file():
            pairs.append((src, dest_root / "tabbyAPI" / "model_profiles" / src.name))
    for src, dest in pairs:
        if src.resolve() == dest.resolve():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  copied {src.name} -> {dest}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retest model speeds / context on this GPU and update wait copy"
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="Climb (or step down) cache_size / max_seq_len before timing",
    )
    parser.add_argument(
        "--context-only",
        action="store_true",
        help="Climb context, skip the speed bench, still refresh docs",
    )
    parser.add_argument(
        "--times-only",
        action="store_true",
        help="Skip context climb even if --context is also set",
    )
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="Rewrite AGENTS.md and the Arch deploy wait line from the current JSON (no GPU work)",
    )
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--only", nargs="*", help="Subset of profiles / comfy / llm")
    parser.add_argument("--no-restore", action="store_true")
    parser.add_argument(
        "--try-glm-vision",
        action="store_true",
        help="Try GLM vision even if VRAM is under 16 GB",
    )
    parser.add_argument(
        "--no-glm-vision",
        action="store_true",
        help="Do not probe GLM vision",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=os.environ.get("TABBY_SOURCE") or None,
        help="Also copy updated yml / switch_times.json / AGENTS.md here (git tree)",
    )
    parser.add_argument("--docs", type=Path, default=AGENTS_PATH)
    parser.add_argument("--out", type=Path, default=TIMES_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gpu = detect_gpu()
    print(f"GPU: {gpu['label']} ({gpu['vram_mib']} MiB)  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if args.docs_only:
        times = load_switch_times(args.out)
        if not times:
            raise SystemExit(f"no times in {args.out}; run without --docs-only first")
        label = gpu_label(times)
        rewrite_docs(times, label, args.docs)
        if args.source:
            sync_source(args.source)
        print("\nDocs refreshed from switch_times.json (no bench).")
        return 0

    if not server_up(api_base()):
        raise SystemExit(f"TabbyAPI is not running at {api_base()}")
    if not comfy_up():
        start_comfy_if_needed()

    do_context = (args.context or args.context_only) and not args.times_only
    if do_context:
        names = None
        if args.only:
            names = [name for name in args.only if name in ARCH_MAX]
        print("\n--- context ---")
        climb_all(names)
        vram = int(gpu.get("vram_mib") or 0)
        if not args.no_glm_vision and (args.try_glm_vision or vram >= 16000):
            try_glm_vision()

    times: dict[str, Any]
    if args.context_only:
        times = load_switch_times(args.out)
        if not times:
            times = {"gpu": gpu.get("label")}
    else:
        print("\n--- times ---")
        results = run_bench(
            only=args.only,
            skip_images=args.skip_images,
            out=args.out,
            no_restore=args.no_restore,
        )
        times = load_switch_times(args.out) or results

    label = str(times.get("gpu") or gpu.get("label") or "this GPU")
    rewrite_docs(times, label, args.docs)

    if args.source:
        sync_source(args.source)

    print("\nDone. Chat wait copy reads switch_times.json; send help to see the new numbers.")
    print(json.dumps({key: times.get(key) for key in ("gpu", "qwen", "comfy", "llm")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
