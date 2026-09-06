"""Apply a model profile to config.yml, optionally asking which one to use."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "model_profiles"
CONFIG_PATH = ROOT / "config.yml"
LAST_PATH = PROFILES_DIR / "last.json"
CATALOG_PATH = ROOT / "deploy" / "arch" / "models.json"


def available_profiles() -> list[str]:
    return sorted(path.stem for path in PROFILES_DIR.glob("*.yml"))


def profile_aliases() -> dict[str, str]:
    aliases = {}
    names = available_profiles()
    for index, name in enumerate(names, start=1):
        aliases[str(index)] = name
        aliases[name.upper()] = name
        aliases[name] = name
    return aliases


def load_yaml(path: Path):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with path.open(encoding="utf-8") as handle:
        return yaml, yaml.load(handle)


def save_yaml(yaml: YAML, data, path: Path):
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


def last_profile() -> str | None:
    if LAST_PATH.exists():
        try:
            return json.loads(LAST_PATH.read_text(encoding="utf-8")).get("profile")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def write_last(name: str):
    LAST_PATH.write_text(json.dumps({"profile": name}), encoding="utf-8")


def is_embedding_folder(folder_name: str) -> bool:
    return "embedding" in (folder_name or "").lower()


def model_folder_ready(folder_name: str, models_dir: Path | None = None) -> bool:
    if not folder_name or is_embedding_folder(folder_name):
        return False
    return ((models_dir or (ROOT / "models")) / folder_name / "config.json").is_file()


def profile_model_name(name: str, profiles_dir: Path | None = None) -> str | None:
    path = (profiles_dir or PROFILES_DIR) / f"{name}.yml"
    if not path.is_file():
        return None
    _, data = load_yaml(path)
    folder = (data.get("model") or {}).get("model_name")
    return str(folder) if folder else None


def ready_llm_folders(models_dir: Path | None = None) -> list[str]:
    base = models_dir or (ROOT / "models")
    if not base.is_dir():
        return []
    found: list[str] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return []
    for child in sorted(children, key=lambda path: path.name.lower()):
        if child.is_dir() and model_folder_ready(child.name, models_dir=base):
            found.append(child.name)
    return found


def profile_for_folder(folder: str, profiles_dir: Path | None = None) -> str | None:
    for path in sorted((profiles_dir or PROFILES_DIR).glob("*.yml")):
        if profile_model_name(path.stem, profiles_dir=profiles_dir) == folder:
            return path.stem
    return None


def folder_for_choice(name: str | None, models_dir: Path | None = None) -> str | None:
    if not name:
        return None
    folder = profile_model_name(name)
    if folder and model_folder_ready(folder, models_dir=models_dir):
        return folder
    if model_folder_ready(name, models_dir=models_dir):
        return name
    return None


def _load_catalog(path: Path | None = None) -> dict:
    target = path or CATALOG_PATH
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _id_tokens(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").replace(" ", ",").split(",") if part.strip()]


def expand_choice_ids(raw: str, catalog: dict | None = None) -> list[str]:
    tokens = _id_tokens(raw)
    data = catalog if catalog is not None else _load_catalog()
    sets = data.get("sets") or {}
    if len(tokens) == 1 and tokens[0] in sets:
        tokens = [str(item) for item in sets[tokens[0]] if item]
    picks = {
        str(row["id"]): [str(item) for item in (row.get("items") or []) if item]
        for row in (data.get("picks") or [])
        if isinstance(row, dict) and row.get("id")
    }
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        pieces = picks.get(token) or [token]
        for piece in pieces:
            if piece not in seen:
                seen.add(piece)
                out.append(piece)
    return out


def catalog_selected_ids(catalog: dict | None = None) -> list[str]:
    data = catalog if catalog is not None else _load_catalog()
    selected = (data.get("sets") or {}).get("selected") or []
    if isinstance(selected, list) and selected:
        return [str(item) for item in selected if item]
    return []


def folder_for_id(
    token: str,
    models_dir: Path | None = None,
    profiles_dir: Path | None = None,
    catalog: dict | None = None,
) -> str | None:
    folder = profile_model_name(token, profiles_dir=profiles_dir)
    if folder and model_folder_ready(folder, models_dir=models_dir):
        return folder
    item = ((catalog if catalog is not None else _load_catalog()).get("items") or {}).get(token) or {}
    dest = str(item.get("dest") or "")
    if dest.startswith("tabby/models/"):
        name = Path(dest).name
        if model_folder_ready(name, models_dir=models_dir):
            return name
    if token.startswith("local-"):
        for name in ready_llm_folders(models_dir=models_dir):
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name).strip("-")
            if f"local-{safe}" == token:
                return name
    if model_folder_ready(token, models_dir=models_dir):
        return token
    return None


def first_ready_folder(
    ids: str | None = None,
    models_dir: Path | None = None,
    profiles_dir: Path | None = None,
    catalog: dict | None = None,
) -> str | None:
    """Startup folder from the user's selected ids, else the only LLM on disk."""
    data = catalog if catalog is not None else _load_catalog()
    tokens = expand_choice_ids(ids or "", data)
    if not tokens:
        tokens = catalog_selected_ids(data)
    for token in tokens:
        folder = folder_for_id(
            token, models_dir=models_dir, profiles_dir=profiles_dir, catalog=data
        )
        if folder:
            return folder
    ready = ready_llm_folders(models_dir=models_dir)
    if len(ready) == 1:
        return ready[0]
    return None


def configured_model_name(config_path: Path | None = None) -> str | None:
    path = config_path or CONFIG_PATH
    if not path.is_file():
        return None
    try:
        _, data = load_yaml(path)
    except OSError:
        return None
    folder = ((data or {}).get("model") or {}).get("model_name")
    return str(folder) if folder else None


def _sync_gpu_mode_profile(name: str) -> None:
    status = PROFILES_DIR / "gpu_mode.json"
    try:
        data = json.loads(status.read_text(encoding="utf-8")) if status.is_file() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if str(data.get("mode") or "llm").lower() == "comfy":
        return
    extra = {key: value for key, value in data.items() if key not in ("mode", "profile")}
    status.write_text(
        json.dumps({"mode": "llm", "profile": name, **extra}, indent=2) + "\n",
        encoding="utf-8",
    )


def apply_bare_model(folder: str):
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH}")
    yaml, config = load_yaml(CONFIG_PATH)
    if not isinstance(config, dict):
        config = {}
    if not isinstance(config.get("model"), dict):
        config["model"] = {}
    config["model"]["model_name"] = folder
    save_yaml(yaml, config, CONFIG_PATH)
    write_last(folder)
    print(f"Using {folder}")
    print(f"  model: {folder}")
    return {"model": {"model_name": folder}}


def apply_startup_folder(folder: str) -> str:
    profile = profile_for_folder(folder)
    if profile:
        apply_profile(profile)
        _sync_gpu_mode_profile(profile)
        return profile
    apply_bare_model(folder)
    _sync_gpu_mode_profile(folder)
    return folder


def seed_install_profile(
    configured_folder: str | None = None,
    ids: str | None = None,
) -> str | None:
    """Point config at an LLM that is actually in models/, not a hardcoded default."""
    folder = configured_folder if configured_folder is not None else configured_model_name()
    if folder and model_folder_ready(folder):
        return last_profile() or profile_for_folder(folder) or folder
    current = last_profile()
    current_folder = folder_for_choice(current)
    if current_folder:
        return apply_startup_folder(current_folder)
    chosen = first_ready_folder(ids=ids)
    if not chosen:
        return None
    return apply_startup_folder(chosen)


def retarget_startup_model(model_name: str | None) -> tuple[str | None, bool]:
    """Return (folder to load, True if config.yml was rewritten)."""
    if model_name and model_folder_ready(model_name):
        return model_name, False
    before = last_profile()
    chosen = seed_install_profile(configured_folder=model_name)
    if not chosen:
        return None, False
    folder = folder_for_choice(chosen) or (
        chosen if model_folder_ready(chosen) else None
    )
    changed = chosen != before or (model_name != folder)
    return folder, changed


def write_tabby_overlay(profile: dict):
    """Write per-folder tabby_config.yml so /v1/model/load picks up tool/reasoning settings."""
    model_cfg = dict(profile.get("model") or {})
    model_name = model_cfg.pop("model_name", None)
    if not model_name:
        return

    dest = ROOT / "models" / model_name / "tabby_config.yml"
    if not dest.parent.exists():
        print(f"  skip overlay: {dest.parent} does not exist")
        return

    overlay = {"model": model_cfg}
    draft_cfg = profile.get("draft_model")
    if draft_cfg:
        overlay["draft_model"] = dict(draft_cfg)

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with dest.open("w", encoding="utf-8") as handle:
        yaml.dump(overlay, handle)
    print(f"  overlay: {dest}")


def apply_profile(name: str):
    profile_path = PROFILES_DIR / f"{name}.yml"
    if not profile_path.exists():
        raise SystemExit(f"Missing profile: {profile_path}")
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH}")

    _, profile = load_yaml(profile_path)
    yaml, config = load_yaml(CONFIG_PATH)

    pretty = profile.pop("pretty", name)
    write_tabby_overlay(profile)
    for section, values in profile.items():
        if not isinstance(values, dict):
            raise SystemExit(
                f"Profile {name}: '{section}' must be a section of key/value pairs, "
                f"got {type(values).__name__}"
            )
        if section not in config or config[section] is None:
            config[section] = {}
        for key, value in values.items():
            config[section][key] = value

    save_yaml(yaml, config, CONFIG_PATH)
    write_last(name)
    print(f"Using {pretty}")
    print(f"  model: {profile.get('model', {}).get('model_name', name)}")
    return profile


def timed_input(prompt: str, timeout: float, default: str) -> str:
    """Read a line. If nothing is typed before timeout, return default."""
    print(prompt, end="", flush=True)
    if sys.platform == "win32":
        import msvcrt

        chars: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return "".join(chars).strip() or default
                if ch == "\x08":
                    if chars:
                        chars.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    continue
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                if ch == "\x03":
                    raise KeyboardInterrupt
                chars.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
                continue
            if not chars and time.monotonic() >= deadline:
                print()
                return default
            time.sleep(0.05)

    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip() or default
    print()
    return default


def ask_profile() -> str:
    names = available_profiles()
    current = last_profile() if last_profile() in names else (names[0] if names else "qwen")
    print()
    print("Which model?")
    print()
    for index, name in enumerate(names, start=1):
        _, data = load_yaml(PROFILES_DIR / f"{name}.yml")
        pretty = data.get("pretty", name)
        print(f"  {index}) {pretty}  [{name}]")
    print()
    default_key = str(names.index(current) + 1) if current in names else "1"
    raw = timed_input(
        f"Input [{default_key}] ({current}, auto in 5s)> ",
        timeout=5,
        default="",
    ).strip()
    if not raw:
        print(f"No choice. Using {current}.")
        return current
    aliases = profile_aliases()
    name = aliases.get(raw) or aliases.get(raw.upper()) or aliases.get(raw.lower())
    if not name:
        print("Invalid choice. Keeping the last model.")
        return current
    return name


def main():
    names = available_profiles()
    parser = argparse.ArgumentParser(description="Select a TabbyAPI model profile")
    parser.add_argument("profile", nargs="?", choices=names)
    parser.add_argument("--ask", action="store_true", help="Prompt even if a profile is given")
    parser.add_argument(
        "--seed-installed",
        action="store_true",
        help="Point startup at an LLM folder that is actually in models/",
    )
    parser.add_argument(
        "--ids",
        default="",
        help="Installer pick ids (same as TABBY_MODELS). Used with --seed-installed",
    )
    args = parser.parse_args()

    if args.seed_installed:
        name = seed_install_profile(ids=args.ids or None)
        if name:
            print(f"Startup profile: {name}")
        else:
            print("No installed LLM; API will start without a model.")
        return 0

    name = args.profile
    if args.ask or not name:
        if sys.stdin.isatty():
            name = ask_profile()
        else:
            name = name or last_profile() or "qwen"
            print(f"No TTY; using {name}")

    apply_profile(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
