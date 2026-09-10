"""Admin read/write for config.yml and tabby.env."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import PydanticUndefined
from ruamel.yaml import YAML

from common.config_models import BaseConfigModel, TabbyConfigModel

TABBY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = TABBY_ROOT / "config.yml"
ENV_PATH = TABBY_ROOT / "deploy" / "arch" / "tabby.env"
ENV_EXAMPLE_PATH = TABBY_ROOT / "deploy" / "arch" / "tabby.env.example"

SECRET_KEYS = frozenset(
    {
        "seqlog_api_key",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    }
)

SYSTEM_FIELDS = (
    {
        "name": "COMFYUI_DIR",
        "label": "ComfyUI directory",
        "description": "ComfyUI tree on this host.",
    },
    {
        "name": "COMFYUI_URL",
        "label": "ComfyUI URL",
        "description": "ComfyUI HTTP API.",
    },
    {
        "name": "TABBY_PUBLIC_BASE",
        "label": "Public base URL",
        "description": "Base URL written into image markdown for remote clients. Blank uses the Host the client already called.",
    },
    {
        "name": "TABBY_SSH_REMOTE",
        "label": "SSH remote",
        "description": "Reverse tunnel. Empty disables ssh. Example: user@host.example",
    },
    {
        "name": "TABBY_SSH_FORWARD",
        "label": "SSH forward",
        "description": "Remote listen to local TabbyAPI.",
    },
    {
        "name": "TABBY_SSH_KEY",
        "label": "SSH key path",
        "description": "Private key for TABBY_SSH_REMOTE.",
    },
    {
        "name": "TABBY_LOG_CONSOLE_WIDTH",
        "label": "Log console width",
        "description": "Console wrap width.",
        "kind": "int",
    },
    {
        "name": "TABBY_LOG_LEVEL",
        "label": "Log level",
        "description": "DEBUG, INFO, WARNING, or ERROR.",
        "kind": "select",
        "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    {
        "name": "HF_TOKEN",
        "label": "Hugging Face token",
        "description": "Used by the Models tab, the installer, and fetch_models.py for gated repos.",
        "secret": True,
    },
    {
        "name": "HUGGING_FACE_HUB_TOKEN",
        "label": "Hugging Face hub token",
        "description": "Alternate Hugging Face token name.",
        "secret": True,
    },
)

SAVER_FIELDS = (
    {
        "name": "enabled",
        "env": "TABBY_SAVER_ENABLED",
        "label": "Enable screensaver",
        "description": "KMS kiosk on a spare TTY. Do not enable if a desktop already owns the GPU.",
        "kind": "bool",
        "optional": False,
        "default": True,
    },
    {
        "name": "timeout",
        "env": "TABBY_SAVER_IDLE_S",
        "label": "Idle timeout (seconds)",
        "description": "Seconds without keyboard or mouse while logged in on the console before the field returns.",
        "kind": "int",
        "optional": False,
        "default": 120,
    },
    {
        "name": "logout_timeout",
        "env": "TABBY_SAVER_LOGOUT_IDLE_S",
        "label": "Logout timeout (seconds)",
        "description": "Seconds after console logout, or idle at the login prompt, before the field returns. Boot and service start take the console immediately; this wait starts after you dismiss it.",
        "kind": "int",
        "optional": False,
        "default": 5,
    },
    {
        "name": "hud_timeout",
        "env": "TABBY_SAVER_HUD_S",
        "label": "Idle text (seconds)",
        "description": "Seconds the clock and labels stay on the idle field. 0 hides them. Mouse motion shows them again after they fade.",
        "kind": "int",
        "optional": False,
        "default": 300,
    },
)

SAVER_ALIASES = {
    "timeout": "timeout",
    "idle": "timeout",
    "idle_s": "timeout",
    "idle-timeout": "timeout",
    "logout-timeout": "logout_timeout",
    "logout_timeout": "logout_timeout",
    "logout_idle": "logout_timeout",
    "logout-idle": "logout_timeout",
    "hud_timeout": "hud_timeout",
    "hud-timeout": "hud_timeout",
    "hud": "hud_timeout",
    "hud_s": "hud_timeout",
    "hud-idle": "hud_timeout",
    "idle-text": "hud_timeout",
    "idle_text": "hud_timeout",
    "enabled": "enabled",
    "enable": "enabled",
}

SAVER_ENV_NAMES = frozenset(item["env"] for item in SAVER_FIELDS) | {
    "TABBY_SAVER_TTY",
    "TABBY_SAVER_USER_TTY",
    "TABBY_SAVER_URL",
}

UPDATE_FIELDS = (
    {
        "name": "enabled",
        "env": "TABBY_AUTO_UPDATE",
        "label": "Enable auto-update",
        "description": "Pull GitHub origin/main on a timer. Skips while a chat or image job is running.",
        "kind": "bool",
        "optional": False,
        "default": True,
    },
    {
        "name": "interval_days",
        "env": "TABBY_AUTO_UPDATE_DAYS",
        "label": "Interval (days)",
        "description": "Days between automatic updates. The timer checks daily. Default 7.",
        "kind": "int",
        "optional": False,
        "default": 7,
    },
    {
        "name": "full",
        "env": "TABBY_AUTO_UPDATE_FULL",
        "label": "Update all (git + deps)",
        "description": "On = Update all (refresh Python deps and restart). Off = git pull only.",
        "kind": "bool",
        "optional": False,
        "default": True,
    },
)

UPDATE_ALIASES = {
    "enabled": "enabled",
    "enable": "enabled",
    "interval": "interval_days",
    "interval_days": "interval_days",
    "days": "interval_days",
    "interval-days": "interval_days",
    "full": "full",
    "update_all": "full",
    "update-all": "full",
}

UPDATE_ENV_NAMES = frozenset(item["env"] for item in UPDATE_FIELDS)

GPU_FIELDS = (
    {
        "name": "profile",
        "env": "TABBY_GPU_PROFILE",
        "label": "Fan profile",
        "description": "auto leaves NVIDIA fan-stop. quiet / balanced / performance follow temperature. custom uses Fan speed %.",
        "kind": "select",
        "choices": ["auto", "quiet", "balanced", "performance", "custom"],
        "optional": False,
        "default": "auto",
    },
    {
        "name": "fan_speed",
        "env": "TABBY_GPU_FAN_SPEED",
        "label": "Fan speed %",
        "description": "Used when profile is custom. 0 = driver auto. Manual floor is usually 30%.",
        "kind": "int",
        "optional": True,
        "default": 0,
    },
    {
        "name": "power_limit",
        "env": "TABBY_GPU_POWER_LIMIT",
        "label": "Power limit (W)",
        "description": "0 = profile default (quiet is about half the card's range; others use the driver default). Clamped to the GPU min/max.",
        "kind": "int",
        "optional": True,
        "default": 0,
    },
    {
        "name": "persistence",
        "env": "TABBY_GPU_PERSISTENCE",
        "label": "Persistence mode",
        "description": "Keep the NVIDIA driver loaded with no clients. Helps CUDA start and keeps power/fan limits after nvidia-smi exits.",
        "kind": "bool",
        "optional": False,
        "default": True,
    },
)

GPU_ALIASES = {
    "profile": "profile",
    "fan": "fan_speed",
    "fan_speed": "fan_speed",
    "fan-speed": "fan_speed",
    "speed": "fan_speed",
    "power": "power_limit",
    "power_limit": "power_limit",
    "power-limit": "power_limit",
    "pl": "power_limit",
    "persistence": "persistence",
    "persist": "persistence",
    "pm": "persistence",
}

GPU_ENV_NAMES = frozenset(item["env"] for item in GPU_FIELDS)

_ENV_ASSIGN = re.compile(r"^(\s*)(?:#\s*)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9._/~:@%=+,+-]*$")
_BLOCKED_ENV = frozenset(
    {"ENV", "BASH_ENV", "SHELLOPTS", "GLOBIGNORE", "CDPATH", "IFS"}
)
_BLOCKED_ENV_PREFIXES = ("LD_", "PYTHON", "BASH_")


class SettingsError(ValueError):
    pass


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump())
    return str(value)


def _unwrap_optional(ann: Any) -> tuple[Any, bool]:
    origin = get_origin(ann)
    if origin is Union:
        args = [item for item in get_args(ann) if item is not type(None)]
        if len(args) == 1:
            return args[0], True
        return ann, True
    return ann, False


def _kind_from_annotation(ann: Any) -> dict[str, Any]:
    ann, optional = _unwrap_optional(ann)
    origin = get_origin(ann)
    args = get_args(ann)
    if ann is bool:
        return {"kind": "bool", "optional": optional}
    if origin is Literal:
        return {"kind": "select", "choices": [str(item) for item in args], "optional": optional}
    if origin is list:
        inner = args[0] if args else str
        inner_origin = get_origin(inner)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            return {"kind": "json", "optional": optional}
        if inner in (int, float) or inner_origin is Union:
            return {"kind": "list_number", "optional": optional}
        return {"kind": "list_text", "optional": optional}
    if origin is dict or ann is dict:
        return {"kind": "json", "optional": optional}
    if origin is Union:
        literals: list[str] = []
        for item in args:
            if get_origin(item) is Literal:
                literals.extend(str(part) for part in get_args(item))
        out: dict[str, Any] = {"kind": "text", "optional": True}
        if literals:
            out["choices"] = literals
        return out
    if ann is int:
        return {"kind": "int", "optional": optional}
    if ann is float:
        return {"kind": "float", "optional": optional}
    return {"kind": "text", "optional": optional}


def _field_default(info: Any) -> Any:
    if info.default is not PydanticUndefined:
        return _jsonable(info.default)
    factory = getattr(info, "default_factory", None)
    if factory is not None and factory is not PydanticUndefined:
        try:
            return _jsonable(factory())
        except Exception:
            return None
    return None


def _section_included(model_cls: type) -> bool:
    try:
        meta = getattr(model_cls(), "_metadata", None)
    except Exception:
        return True
    return bool(meta is None or getattr(meta, "include_in_config", True))


def tabby_schema() -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for name, info in TabbyConfigModel.model_fields.items():
        model_cls, _optional = _unwrap_optional(info.annotation)
        if not isinstance(model_cls, type) or not issubclass(model_cls, BaseConfigModel):
            continue
        if not _section_included(model_cls):
            continue
        fields = []
        for field_name, field_info in model_cls.model_fields.items():
            spec = _kind_from_annotation(field_info.annotation)
            spec.update(
                {
                    "name": field_name,
                    "label": field_name.replace("_", " "),
                    "description": (field_info.description or "").strip(),
                    "default": _field_default(field_info),
                    "secret": field_name in SECRET_KEYS,
                }
            )
            if spec["kind"] == "bool" and spec.get("optional") and spec.get("default") is None:
                spec["kind"] = "select"
                spec["choices"] = ["true", "false"]
                spec["blank"] = "auto"
            fields.append(spec)
        description = (model_cls.__doc__ or "").strip().split("\n", 1)[0]
        if name == "model":
            description = (
                f"{description} A profile switch overwrites load keys "
                "(context, cache, tool_format, reasoning)."
            ).strip()
        sections.append(
            {
                "name": name,
                "label": name.replace("_", " "),
                "description": description,
                "fields": fields,
            }
        )
    return sections


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _load_config_doc() -> tuple[YAML, Any]:
    yaml = _yaml()
    if not CONFIG_PATH.is_file():
        return yaml, {}
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        data = yaml.load(handle)
    return yaml, data if data is not None else {}


def _file_tabby_values() -> dict[str, dict[str, Any]]:
    _, data = _load_config_doc()
    plain = _plain(data) if isinstance(data, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for section in tabby_schema():
        raw = plain.get(section["name"])
        row = raw if isinstance(raw, dict) else {}
        out[section["name"]] = {
            field["name"]: _jsonable(row[field["name"]])
            for field in section["fields"]
            if field["name"] in row
        }
    return out


def _live_tabby_values() -> dict[str, dict[str, Any]]:
    from common.tabby_config import config

    out: dict[str, dict[str, Any]] = {}
    for section in tabby_schema():
        obj = getattr(config, section["name"], None)
        row: dict[str, Any] = {}
        for field in section["fields"]:
            row[field["name"]] = _jsonable(getattr(obj, field["name"], None)) if obj is not None else None
        out[section["name"]] = row
    return out


def _mask(section: str, name: str, value: Any) -> Any:
    if name in SECRET_KEYS or f"{section}.{name}" in SECRET_KEYS:
        return ""
    return value


def _env_unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_ASSIGN.match(line)
        if not match:
            continue
        values[match.group(2)] = _env_unquote(match.group(3))
    return values


def _system_schema(file_values: dict[str, str]) -> list[dict[str, Any]]:
    known = {item["name"] for item in SYSTEM_FIELDS} | set(SAVER_ENV_NAMES) | set(GPU_ENV_NAMES) | set(UPDATE_ENV_NAMES)
    fields = []
    for item in SYSTEM_FIELDS:
        field = dict(item)
        field.setdefault("kind", "text")
        field.setdefault("secret", field["name"] in SECRET_KEYS)
        field.setdefault("optional", True)
        fields.append(field)
    for name in sorted(file_values):
        if name in known or env_key_blocked(name):
            continue
        fields.append(
            {
                "name": name,
                "label": name.replace("_", " "),
                "description": "From tabby.env.",
                "kind": "text",
                "optional": True,
                "secret": name in SECRET_KEYS,
                "extra": True,
            }
        )
    return fields


def env_key_blocked(name: str) -> bool:
    if name in _BLOCKED_ENV:
        return True
    return name.startswith(_BLOCKED_ENV_PREFIXES)


def _known_env_keys() -> set[str]:
    return (
        {item["name"] for item in SYSTEM_FIELDS}
        | set(SAVER_ENV_NAMES)
        | set(GPU_ENV_NAMES)
        | set(UPDATE_ENV_NAMES)
    )


def _writable_env_keys(file_values: dict[str, str]) -> set[str]:
    extra = {name for name in file_values if not env_key_blocked(name)}
    return _known_env_keys() | extra


def _env_quote(value: str) -> str:
    text = str(value)
    if any(char in text for char in "\n\r\0"):
        raise SettingsError("Environment values cannot contain newlines")
    if _SAFE_UNQUOTED.fullmatch(text):
        return text
    if any(char in text for char in "$`\\'"):
        raise SettingsError(
            "Environment values cannot contain quotes or shell characters"
        )
    return "'" + text + "'"


def _write_env(updates: dict[str, Optional[str]]) -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_PATH.is_file():
        if ENV_EXAMPLE_PATH.is_file():
            shutil.copy(ENV_EXAMPLE_PATH, ENV_PATH)
        else:
            ENV_PATH.write_text("# tabby.env\n", encoding="utf-8")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _ENV_ASSIGN.match(line)
        if not match:
            out.append(line)
            continue
        key = match.group(2)
        if env_key_blocked(key):
            continue
        if key not in updates:
            out.append(line)
            continue
        seen.add(key)
        value = updates[key]
        if value is None:
            out.append(f"# {key}=")
        else:
            out.append(f"{key}={_env_quote(str(value))}")
    extras = [key for key in updates if key not in seen]
    if extras:
        if out and out[-1].strip():
            out.append("")
        for key in extras:
            if env_key_blocked(key):
                continue
            value = updates[key]
            if value is None:
                out.append(f"# {key}=")
            else:
                out.append(f"{key}={_env_quote(str(value))}")
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    tmp = ENV_PATH.with_suffix(ENV_PATH.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(ENV_PATH)
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def scrub_env_file(path: Path) -> None:
    """Drop linker/shell-hijack keys from tabby.env (restores, old files)."""
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    changed = False
    for line in lines:
        match = _ENV_ASSIGN.match(line)
        if match and env_key_blocked(match.group(2)):
            changed = True
            continue
        out.append(line)
    if not changed:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _coerce_field(spec: dict[str, Any], value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "" and spec.get("optional"):
        return None
    kind = spec.get("kind")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        if value == "" or value is None:
            return None
        return int(value)
    if kind == "float":
        if value == "" or value is None:
            return None
        return float(value)
    if kind == "select":
        if spec.get("blank") and (value in ("", None, "auto")):
            return None
        if spec.get("choices") == ["true", "false"]:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return None if value in ("", None) else str(value)
    if kind == "list_text":
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return []
    if kind == "list_number":
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return [float(part) if "." in part else int(part) for part in parts]
        if isinstance(value, list):
            return [float(item) if isinstance(item, float) else int(item) for item in value]
        return []
    if kind == "json":
        if isinstance(value, str):
            if not value.strip():
                return None
            import json

            return json.loads(value)
        return value
    if isinstance(value, str) and spec.get("choices") and value in spec["choices"]:
        return value
    if isinstance(value, str) and value.lower() == "auto":
        return "auto"
    return value


def _atomic_yaml(yaml: YAML, data: Any) -> None:
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    tmp.replace(CONFIG_PATH)


def _apply_tabby(updates: dict[str, dict[str, Any]]) -> None:
    schema = {section["name"]: {field["name"]: field for field in section["fields"]} for section in tabby_schema()}
    yaml, data = _load_config_doc()
    if not isinstance(data, dict):
        data = {}
    merged = _plain(data)
    for section, fields in updates.items():
        if section not in schema:
            raise SettingsError(f"Unknown section {section}")
        if not isinstance(fields, dict):
            raise SettingsError(f"{section} must be an object")
        row = dict(merged.get(section) or {})
        for key, raw in fields.items():
            spec = schema[section].get(key)
            if not spec:
                raise SettingsError(f"Unknown setting {section}.{key}")
            if spec.get("secret") and (raw is None or raw == ""):
                continue
            try:
                row[key] = _coerce_field(spec, raw)
            except Exception as exc:
                raise SettingsError(f"{section}.{key}: {exc}") from exc
        merged[section] = row
        if section not in data or data[section] is None:
            data[section] = {}
        for key, value in row.items():
            data[section][key] = value
    try:
        TabbyConfigModel.model_validate(merged)
    except Exception as exc:
        raise SettingsError(str(exc)) from exc
    if not CONFIG_PATH.is_file():
        from common.tabby_config import generate_config_file

        generate_config_file(filename=str(CONFIG_PATH))
        yaml, data = _load_config_doc()
        if not isinstance(data, dict):
            data = {}
        for section, row in merged.items():
            if section not in data or data[section] is None:
                data[section] = {}
            for key, value in (row or {}).items():
                data[section][key] = value
    _atomic_yaml(yaml, data)


def _reload_live() -> None:
    from common.tabby_config import config

    cwd = Path.cwd()
    try:
        os.chdir(TABBY_ROOT)
        config.load()
    finally:
        os.chdir(cwd)


def _env_truthy(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def normalize_saver_key(name: str) -> str:
    key = str(name or "").strip()
    if key in SAVER_ALIASES:
        return SAVER_ALIASES[key]
    env_match = next((item["name"] for item in SAVER_FIELDS if item["env"] == key), None)
    if env_match:
        return env_match
    raise SettingsError(f"Unknown screensaver setting {name}")


def _saver_payload(env_values: dict[str, str]) -> dict[str, Any]:
    fields = []
    for item in SAVER_FIELDS:
        field = dict(item)
        raw = env_values.get(item["env"], "")
        if item["kind"] == "bool":
            value: Any = _env_truthy(raw)
        elif item["kind"] == "int":
            if str(raw).strip() == "":
                value = item.get("default")
            else:
                try:
                    value = int(str(raw).strip())
                except ValueError:
                    value = item.get("default")
        else:
            value = raw
        field["value"] = value
        field["set"] = bool(str(raw).strip())
        field["live"] = value
        fields.append(field)
    return {
        "name": "screensaver",
        "label": "Screensaver",
        "description": "TTY activity kiosk. Timeouts apply after tabby-saver restarts; enable/disable talks to systemd.",
        "path": str(ENV_PATH),
        "fields": fields,
    }


def _sudo_systemctl(args: list[str], *, what: str = "screensaver") -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return f"tabby.env saved; run tsctl {what} from a terminal ({exc})"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        return f"tabby.env saved; systemd: {err}. Try: tsctl {what}"
    return ""


def _systemctl_is(action: str, unit: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", action, unit],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return False
    return result.returncode == 0


def apply_saver_unit(enabled: Optional[bool] = None) -> str:
    """Enable, disable, or restart tabby-saver so tabby.env timeouts apply."""
    if enabled is True:
        was_active = _systemctl_is("is-active", "tabby-saver")
        warning = _sudo_systemctl(["enable", "--now", "tabby-saver"], what="screensaver enable")
        if not warning and was_active:
            warning = _sudo_systemctl(["restart", "tabby-saver"], what="screensaver enable")
        return warning
    if enabled is False:
        return _sudo_systemctl(["disable", "--now", "tabby-saver"], what="screensaver disable")
    if _systemctl_is("is-active", "tabby-saver"):
        return _sudo_systemctl(["restart", "tabby-saver"], what="screensaver enable")
    return ""


def _apply_screensaver(updates: dict[str, Any]) -> str:
    env_updates: dict[str, Optional[str]] = {}
    enabled: Optional[bool] = None
    for key, raw in updates.items():
        name = normalize_saver_key(str(key))
        spec = next(item for item in SAVER_FIELDS if item["name"] == name)
        try:
            coerced = _coerce_field(spec, raw)
        except Exception as exc:
            raise SettingsError(f"screensaver.{name}: {exc}") from exc
        if spec["kind"] == "bool":
            flag = bool(coerced)
            env_updates[spec["env"]] = "1" if flag else "0"
            if name == "enabled":
                enabled = flag
        elif coerced is None:
            env_updates[spec["env"]] = str(spec.get("default", ""))
        else:
            env_updates[spec["env"]] = str(coerced)
    if env_updates:
        _write_env(env_updates)
    return apply_saver_unit(enabled=enabled)


def normalize_update_key(name: str) -> str:
    key = str(name or "").strip()
    if key in UPDATE_ALIASES:
        return UPDATE_ALIASES[key]
    env_match = next((item["name"] for item in UPDATE_FIELDS if item["env"] == key), None)
    if env_match:
        return env_match
    raise SettingsError(f"Unknown updates setting {name}")


def _update_stamp_path() -> Path:
    return TABBY_ROOT.parent / "tabby-auto-update.stamp"


def _user_systemctl(args: list[str], *, what: str = "updates") -> str:
    import subprocess

    from common.gpu_mode import user_systemd_env

    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=user_systemd_env(),
        )
    except Exception as exc:
        return f"tabby.env saved; run tsctl {what} from a terminal ({exc})"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        return f"tabby.env saved; systemd: {err}. Try: tsctl {what}"
    return ""


def _user_systemctl_is(action: str, unit: str) -> bool:
    import subprocess

    from common.gpu_mode import user_systemd_env

    try:
        result = subprocess.run(
            ["systemctl", "--user", action, unit],
            capture_output=True,
            text=True,
            timeout=8,
            env=user_systemd_env(),
        )
    except Exception:
        return False
    return result.returncode == 0


def apply_auto_update_unit(enabled: Optional[bool] = None) -> str:
    """Write tabby.env, then enable or disable the user auto-update timer."""
    import subprocess

    from common.gpu_mode import user_systemd_env

    script = TABBY_ROOT / "deploy" / "arch" / "auto-update.sh"
    if script.is_file():
        try:
            env = user_systemd_env()
            env["TABBY_INSTALL_ROOT"] = str(TABBY_ROOT.parent)
            proc = subprocess.run(
                ["bash", str(script), "--install-units"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
                return f"tabby.env saved; auto-update timer: {err}"
        except Exception as exc:
            return f"tabby.env saved; run tsctl updates ({exc})"
        return ""
    if enabled is False:
        return _user_systemctl(
            ["disable", "--now", "tabbyapi-auto-update.timer"],
            what="updates disable",
        )
    return _user_systemctl(
        ["enable", "--now", "tabbyapi-auto-update.timer"],
        what="updates enable",
    )


def _auto_update_payload(env_values: dict[str, str]) -> dict[str, Any]:
    fields = []
    for item in UPDATE_FIELDS:
        field = dict(item)
        raw = env_values.get(item["env"], "")
        if item["kind"] == "bool":
            if str(raw).strip() == "":
                value: Any = bool(item.get("default"))
            else:
                value = _env_truthy(raw)
        elif item["kind"] == "int":
            if str(raw).strip() == "":
                value = item.get("default")
            else:
                try:
                    value = int(str(raw).strip())
                except ValueError:
                    value = item.get("default")
        else:
            value = raw
        field["value"] = value
        field["set"] = bool(str(raw).strip())
        field["live"] = value
        fields.append(field)
    stamp = _update_stamp_path()
    last = ""
    if stamp.is_file():
        try:
            last = datetime.fromtimestamp(stamp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            last = "unknown"
    timer_on = _user_systemctl_is("is-enabled", "tabbyapi-auto-update.timer")
    enabled = bool(fields[0]["value"]) if fields else True
    days = fields[1]["value"] if len(fields) > 1 else 7
    bits = [
        f"timer {'on' if timer_on else 'off'}",
        f"every {days} days" if enabled else "disabled",
    ]
    if last:
        bits.append(f"last {last}")
    return {
        "name": "updates",
        "label": "Updates",
        "description": (
            "Automatic GitHub updates. The timer checks daily and runs Update all "
            f"when the interval has elapsed. {' · '.join(bits)}."
        ),
        "path": str(ENV_PATH),
        "fields": fields,
        "status": " · ".join(bits),
        "timer_enabled": timer_on,
        "last_run": last,
    }


def _apply_updates(updates: dict[str, Any]) -> str:
    env_updates: dict[str, Optional[str]] = {}
    enabled: Optional[bool] = None
    for key, raw in updates.items():
        name = normalize_update_key(str(key))
        spec = next(item for item in UPDATE_FIELDS if item["name"] == name)
        try:
            coerced = _coerce_field(spec, raw)
        except Exception as exc:
            raise SettingsError(f"updates.{name}: {exc}") from exc
        if spec["kind"] == "bool":
            flag = bool(coerced)
            env_updates[spec["env"]] = "1" if flag else "0"
            if name == "enabled":
                enabled = flag
        elif name == "interval_days":
            if coerced is None:
                env_updates[spec["env"]] = str(spec.get("default", 7))
            else:
                days = int(coerced)
                if days < 1 or days > 365:
                    raise SettingsError("updates.interval_days must be 1-365")
                env_updates[spec["env"]] = str(days)
        elif coerced is None:
            env_updates[spec["env"]] = str(spec.get("default", ""))
        else:
            env_updates[spec["env"]] = str(coerced)
    if env_updates:
        _write_env(env_updates)
    return apply_auto_update_unit(enabled=enabled)


def normalize_gpu_key(name: str) -> str:
    key = str(name or "").strip()
    if key in GPU_ALIASES:
        return GPU_ALIASES[key]
    env_match = next((item["name"] for item in GPU_FIELDS if item["env"] == key), None)
    if env_match:
        return env_match
    raise SettingsError(f"Unknown GPU setting {name}")


def _gpu_live_line() -> str:
    try:
        from common.gpu_control import format_status

        return format_status()
    except Exception as exc:
        return str(exc)


def _gpu_payload(env_values: dict[str, str]) -> dict[str, Any]:
    fields = []
    for item in GPU_FIELDS:
        field = dict(item)
        raw = env_values.get(item["env"], "")
        kind = item["kind"]
        if kind == "bool":
            if str(raw).strip() == "":
                value: Any = item.get("default")
            else:
                value = _env_truthy(raw)
        elif kind == "int":
            if str(raw).strip() == "":
                value = item.get("default")
            else:
                try:
                    value = int(str(raw).strip())
                except ValueError:
                    value = item.get("default")
        elif kind == "select":
            text = str(raw).strip().lower()
            choices = item.get("choices") or []
            value = text if text in choices else item.get("default")
        else:
            value = raw
        field["value"] = value
        field["set"] = bool(str(raw).strip())
        field["live"] = value
        fields.append(field)
    live = _gpu_live_line()
    description = (
        "NVIDIA fan curve and power limit. Applied by the tabby-gpu unit (root NVML). "
        f"Live: {live}"
    )
    return {
        "name": "gpu",
        "label": "GPU",
        "description": description,
        "path": str(ENV_PATH),
        "fields": fields,
        "status": live,
    }


def apply_gpu_unit() -> str:
    """Enable and restart tabby-gpu so tabby.env fan/power settings apply."""
    warning = _sudo_systemctl(["enable", "--now", "tabby-gpu"], what="gpu status")
    if not warning:
        restart = _sudo_systemctl(["restart", "tabby-gpu"], what="gpu status")
        return restart
    try:
        from common.gpu_control import apply_as_root, settings_from_env

        note = apply_as_root(settings_from_env(ENV_PATH))
        extra = f" Applied once ({note}). Install tabby-gpu.service so this survives reboot."
        return warning + extra
    except Exception as exc:
        return f"{warning} Direct apply failed: {exc}"


def _apply_gpu(updates: dict[str, Any]) -> str:
    env_updates: dict[str, Optional[str]] = {}
    names = {str(key): normalize_gpu_key(str(key)) for key in updates}
    if "fan_speed" in names.values() and "profile" not in names.values():
        fan_key = next(key for key, name in names.items() if name == "fan_speed")
        try:
            fan_val = int(updates[fan_key] or 0)
        except (TypeError, ValueError):
            fan_val = 0
        if fan_val > 0:
            updates = dict(updates)
            updates["profile"] = "custom"
            names["profile"] = "profile"
    for key, raw in updates.items():
        name = normalize_gpu_key(str(key))
        spec = next(item for item in GPU_FIELDS if item["name"] == name)
        try:
            coerced = _coerce_field(spec, raw)
        except Exception as exc:
            raise SettingsError(f"gpu.{name}: {exc}") from exc
        if name == "profile":
            text = str(coerced or "auto").strip().lower()
            if text not in (spec.get("choices") or []):
                raise SettingsError(f"gpu.profile must be one of {', '.join(spec['choices'])}")
            env_updates[spec["env"]] = text
        elif spec["kind"] == "bool":
            if coerced is None:
                env_updates[spec["env"]] = None
            else:
                env_updates[spec["env"]] = "1" if bool(coerced) else "0"
        elif coerced is None or coerced == "":
            env_updates[spec["env"]] = "0" if spec["kind"] == "int" else ""
        else:
            if name == "fan_speed":
                value = int(coerced)
                if value < 0 or value > 100:
                    raise SettingsError("gpu.fan_speed must be 0-100")
                env_updates[spec["env"]] = str(value)
            elif name == "power_limit":
                value = int(coerced)
                if value < 0:
                    raise SettingsError("gpu.power_limit must be >= 0")
                env_updates[spec["env"]] = str(value)
            else:
                env_updates[spec["env"]] = str(coerced)
    if env_updates:
        _write_env(env_updates)
    return apply_gpu_unit()


def load_settings() -> dict[str, Any]:
    file_values = _file_tabby_values()
    live_values = _live_tabby_values()
    env_values = _parse_env(ENV_PATH)
    process_env = {key: os.environ.get(key, "") for key in env_values}
    system_fields = _system_schema(env_values)
    tabby = []
    for section in tabby_schema():
        name = section["name"]
        fields = []
        for field in section["fields"]:
            row = file_values.get(name, {})
            if field["name"] in row:
                file_val = row[field["name"]]
            else:
                file_val = field.get("default")
            live_val = live_values.get(name, {}).get(field["name"])
            env_key = f"TABBY_{name}_{field['name']}".upper()
            item = dict(field)
            item["value"] = "" if field.get("secret") else file_val
            item["set"] = bool(row.get(field["name"])) if field.get("secret") else field["name"] in row
            item["live"] = None if field.get("secret") else live_val
            item["env"] = os.environ.get(env_key)
            fields.append(item)
        tabby.append({**section, "fields": fields})
    system = []
    for field in system_fields:
        name = field["name"]
        raw = env_values.get(name, "")
        item = dict(field)
        item["value"] = "" if field.get("secret") else raw
        item["set"] = bool(raw)
        item["live"] = None if field.get("secret") else process_env.get(name, os.environ.get(name, ""))
        system.append(item)
    return {
        "ok": True,
        "tabby": tabby,
        "system": {
            "name": "system",
            "label": "System",
            "description": "Stack settings from tabby.env. Applied on the next API restart.",
            "path": str(ENV_PATH),
            "fields": system,
        },
        "screensaver": _saver_payload(env_values),
        "updates": _auto_update_payload(env_values),
        "gpu": _gpu_payload(env_values),
        "paths": {"config": str(CONFIG_PATH), "env": str(ENV_PATH)},
        "restart_hint": "Network, model, and tabby.env changes apply after Restart API. Screensaver enable/timeouts apply to tabby-saver. GPU fan/power apply to tabby-gpu immediately. Auto-update enable/interval apply to the user timer.",
    }


def save_settings(body: dict[str, Any]) -> dict[str, Any]:
    tabby = body.get("tabby")
    system = body.get("system")
    screensaver = body.get("screensaver")
    updates = body.get("updates")
    gpu = body.get("gpu")
    if tabby is not None:
        if not isinstance(tabby, dict):
            raise SettingsError("tabby must be an object")
        _apply_tabby(tabby)
    if system is not None:
        if not isinstance(system, dict):
            raise SettingsError("system must be an object")
        env_updates: dict[str, Optional[str]] = {}
        existing = _parse_env(ENV_PATH)
        allowed = _writable_env_keys(existing)
        for key, raw in system.items():
            name = str(key).strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) or env_key_blocked(name):
                raise SettingsError(f"Invalid environment key {name}")
            if name not in allowed:
                raise SettingsError(f"Unknown environment key {name}")
            if raw is None:
                env_updates[name] = None
            elif name in SECRET_KEYS and str(raw) == "":
                continue
            else:
                env_updates[name] = str(raw)
        if env_updates:
            _write_env(env_updates)
    saver_warning = ""
    if screensaver is not None:
        if not isinstance(screensaver, dict):
            raise SettingsError("screensaver must be an object")
        saver_warning = _apply_screensaver(screensaver)
    update_warning = ""
    if updates is not None:
        if not isinstance(updates, dict):
            raise SettingsError("updates must be an object")
        update_warning = _apply_updates(updates)
    gpu_warning = ""
    if gpu is not None:
        if not isinstance(gpu, dict):
            raise SettingsError("gpu must be an object")
        gpu_warning = _apply_gpu(gpu)
    reload_warning = ""
    try:
        _reload_live()
    except Exception as exc:
        reload_warning = f"Saved, but live reload failed: {exc}"
    data = load_settings()
    warning = " ".join(
        part for part in (reload_warning, saver_warning, update_warning, gpu_warning) if part
    )
    if warning:
        data = dict(data)
        data["reload_warning"] = warning
    return data
