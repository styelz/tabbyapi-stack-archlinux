#!/usr/bin/env python3
"""Configure tabbyapi-stack from the shell: tsctl <section> key=value."""

from __future__ import annotations

import readline
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

TABBY_ROOT = Path(__file__).resolve().parents[1]
if str(TABBY_ROOT) not in sys.path:
    sys.path.insert(0, str(TABBY_ROOT))

from ui.settings import (  # noqa: E402
    SettingsError,
    load_settings,
    normalize_gpu_key,
    normalize_saver_key,
    save_settings,
)

GPU_PROFILE_NAMES = ("auto", "quiet", "balanced", "performance", "custom")

USAGE = """\
tsctl — tabbyapi-stack settings

  tsctl                         interactive menu (dialog) or shell
  tsctl list                    sections
  tsctl <section>               print keys and values
  tsctl <section> <key>         print one value
  tsctl <section> <key>=<value>
  tsctl <section> <key> <value>
  tsctl screensaver enable|disable|status
  tsctl screensaver hud-timeout=300   idle clock seconds; 0 hides it
  tsctl gpu                     settings plus live sensors
  tsctl gpu status              temperature, fan, power
  tsctl gpu auto|quiet|balanced|performance|custom
  tsctl gpu fan_speed=40        custom percent (driver min is often 30)
  tsctl gpu power_limit=220     watts; 0 = profile default
  tsctl gpu persistence=on
  tsctl restart                 restart TabbyAPI
  tsctl backup DEST [--config] [--users] [--chats] [--dry-run]
  tsctl restore SOURCE [--models] [--config] [--users] [--chats] [--dry-run]

Sections match Settings: network, model, screensaver, gpu, system, …
"""


def _sections(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = data or load_settings()
    out = list(payload.get("tabby") or [])
    if payload.get("screensaver"):
        out.append(payload["screensaver"])
    if payload.get("gpu"):
        out.append(payload["gpu"])
    if payload.get("system"):
        out.append(payload["system"])
    return out


def find_section(name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    want = name.strip().lower().replace("-", "_")
    for section in _sections(data):
        if str(section.get("name") or "").lower() == want:
            return section
        if str(section.get("label") or "").lower().replace(" ", "_") == want:
            return section
    raise SettingsError(f"Unknown section {name}. Try: tsctl list")


def field_by_name(section: dict[str, Any], key: str) -> dict[str, Any]:
    want = key.strip()
    if section.get("name") == "screensaver":
        want = normalize_saver_key(want)
    elif section.get("name") == "gpu":
        want = normalize_gpu_key(want)
    for field in section.get("fields") or []:
        if field.get("name") == want:
            return field
        if field.get("env") == want:
            return field
    raise SettingsError(f"Unknown setting {section.get('name')}.{key}")


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def print_section(section: dict[str, Any]) -> int:
    print(f"{section.get('label') or section.get('name')}")
    desc = (section.get("description") or "").strip()
    if desc:
        print(desc)
    print()
    width = max((len(str(field.get("name") or "")) for field in section.get("fields") or []), default=8)
    for field in section.get("fields") or []:
        print(f"  {field['name']:<{width}}  {format_value(field.get('value'))}")
    return 0


def coerce_cli(spec: dict[str, Any], raw: str) -> Any:
    kind = spec.get("kind")
    text = raw.strip()
    if kind == "bool":
        return text.lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


def apply_sets(section_name: str, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    data = load_settings()
    section = find_section(section_name, data)
    name = str(section["name"])
    updates: dict[str, Any] = {}
    for key, raw in pairs:
        field = field_by_name(section, key)
        updates[field["name"]] = coerce_cli(field, raw)
    if name == "screensaver":
        return save_settings({"screensaver": updates})
    if name == "gpu":
        return save_settings({"gpu": updates})
    if name == "system":
        return save_settings({"system": updates})
    return save_settings({"tabby": {name: updates}})


def saver_status() -> int:
    data = load_settings()
    section = find_section("screensaver", data)
    print_section(section)
    active = subprocess.run(
        ["systemctl", "is-active", "tabby-saver"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    enabled = subprocess.run(
        ["systemctl", "is-enabled", "tabby-saver"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    print()
    print(f"  systemd     {(active.stdout or '').strip() or 'unknown'} / {(enabled.stdout or '').strip() or 'unknown'}")
    return 0


def gpu_status() -> int:
    data = load_settings()
    section = find_section("gpu", data)
    print_section(section)
    active = subprocess.run(
        ["systemctl", "is-active", "tabby-gpu"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    enabled = subprocess.run(
        ["systemctl", "is-enabled", "tabby-gpu"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    print()
    print(f"  systemd     {(active.stdout or '').strip() or 'unknown'} / {(enabled.stdout or '').strip() or 'unknown'}")
    return 0


def restart_api() -> int:
    result = subprocess.run(
        ["systemctl", "--user", "restart", "tabbyapi"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        print(f"tsctl: restart failed: {err}", file=sys.stderr)
        return 1
    print("Restarted tabbyapi.")
    return 0


def report_save(data: dict[str, Any]) -> int:
    warning = str(data.get("reload_warning") or "").strip()
    if warning:
        print(warning, file=sys.stderr)
        return 1 if "systemd:" in warning else 0
    print("ok")
    return 0


def complete_words(cword: int, words: list[str]) -> list[str]:
    data = load_settings()
    sections = [str(section["name"]) for section in _sections(data)]
    extra = ["list", "help", "restart", "backup", "restore", "screensaver", "gpu"]
    if cword <= 1:
        return sorted(set(sections + extra))
    if len(words) > 1 and words[1] in ("backup", "restore"):
        if cword >= 3:
            flags = ["--config", "--users", "--chats", "--dry-run", "--yes"]
            if words[1] == "restore":
                flags.append("--models")
            return flags
        return []
    try:
        section = find_section(words[1], data)
    except SettingsError:
        return []
    names = [str(field["name"]) for field in section.get("fields") or []]
    if section.get("name") == "screensaver":
        names.extend(["enable", "disable", "status", "timeout", "logout-timeout"])
    if section.get("name") == "gpu":
        names.extend([*GPU_PROFILE_NAMES, "status", "apply", "fan-speed", "power-limit"])
    if cword == 2:
        return sorted(set(names))
    if cword == 3 and section.get("name") == "gpu" and words[2] in ("profile",):
        return list(GPU_PROFILE_NAMES)
    return []


def parse_pairs(tokens: list[str]) -> list[tuple[str, str]]:
    if not tokens:
        return []
    if len(tokens) == 2 and "=" not in tokens[0]:
        return [(tokens[0], tokens[1])]
    pairs = []
    for token in tokens:
        if "=" not in token:
            raise SettingsError(f"Expected key=value (got {token})")
        key, value = token.split("=", 1)
        pairs.append((key, value))
    return pairs


def run_dialog(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["dialog", "--backtitle", "tabbyapi-stack", *args],
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode, (result.stderr or "").strip()


def dialog_available() -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    from shutil import which

    return which("dialog") is not None


def tui() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(USAGE)
        return 0
    if dialog_available():
        return tui_dialog()
    return repl()


def tui_dialog() -> int:
    while True:
        data = load_settings()
        sections = _sections(data)
        items: list[str] = [
            "__backup__",
            "Backup models and stack data",
            "__restore__",
            "Restore a stack backup",
        ]
        for section in sections:
            items.extend([str(section["name"]), str(section.get("label") or section["name"])[:40]])
        code, choice = run_dialog(
            ["--title", "tsctl", "--menu", "Pick a section. Esc quits.", "20", "72", "12", *items]
        )
        if code != 0 or not choice:
            return 0
        if choice in ("__backup__", "__restore__"):
            dialog_stack_backup("backup" if choice == "__backup__" else "restore")
            continue
        section = find_section(choice, data)
        while True:
            fields = list(section.get("fields") or [])
            rows: list[str] = []
            for field in fields:
                rows.extend(
                    [
                        str(field["name"]),
                        f"{format_value(field.get('value'))}"[:42],
                    ]
                )
            extra = []
            if section["name"] == "screensaver":
                extra = ["enable", "Turn unit on", "disable", "Turn unit off"]
            elif section["name"] == "gpu":
                extra = [
                    "status",
                    "Live sensors",
                    "auto",
                    "Driver fan-stop",
                    "quiet",
                    "Slow curve / lower TDP",
                    "balanced",
                    "Normal curve",
                    "performance",
                    "Aggressive curve",
                    "custom",
                    "Use Fan speed %",
                ]
            prompt = str(section.get("description") or "Edit a setting")
            if section["name"] == "gpu":
                prompt = "Fan profile and power. Status shows live temperature and fans."
            code, key = run_dialog(
                [
                    "--title",
                    str(section.get("label") or section["name"]),
                    "--menu",
                    prompt,
                    "22",
                    "74",
                    "14",
                    *extra,
                    *rows,
                ]
            )
            if code != 0 or not key:
                break
            if key in ("enable", "disable"):
                body = save_settings({"screensaver": {"enabled": key == "enable"}})
                run_dialog(["--msgbox", str(body.get("reload_warning") or "ok"), "8", "60"])
                data = load_settings()
                section = find_section("screensaver", data)
                continue
            if section["name"] == "gpu" and key == "status":
                from common.gpu_control import format_status

                run_dialog(["--msgbox", format_status() or "No GPU", "14", "70"])
                continue
            if section["name"] == "gpu" and key in GPU_PROFILE_NAMES:
                body = save_settings({"gpu": {"profile": key}})
                run_dialog(["--msgbox", str(body.get("reload_warning") or "ok"), "10", "70"])
                data = load_settings()
                section = find_section("gpu", data)
                continue
            field = field_by_name(section, key)
            if field.get("kind") == "select" and field.get("choices"):
                items: list[str] = []
                for choice in field["choices"]:
                    items.extend([str(choice), str(choice)])
                code, value = run_dialog(
                    [
                        "--title",
                        str(field.get("label") or field["name"]),
                        "--menu",
                        str(field.get("description") or field["name"]),
                        "16",
                        "70",
                        "8",
                        *items,
                    ]
                )
            elif field.get("kind") == "bool":
                yes_args = ["--title", str(field.get("label") or field["name"])]
                if not field.get("value"):
                    yes_args.append("--defaultno")
                code, _ignored = run_dialog(
                    [
                        *yes_args,
                        "--yesno",
                        str(field.get("description") or field["name"]),
                        "10",
                        "70",
                    ]
                )
                if code not in (0, 1):
                    continue
                value = "true" if code == 0 else "false"
                code = 0
            else:
                code, value = run_dialog(
                    [
                        "--title",
                        str(field.get("label") or field["name"]),
                        "--inputbox",
                        str(field.get("description") or field["name"]),
                        "12",
                        "70",
                        format_value(field.get("value")),
                    ]
                )
            if code != 0:
                continue
            try:
                body = apply_sets(str(section["name"]), [(str(field["name"]), value)])
            except (SettingsError, ValueError) as exc:
                run_dialog(["--msgbox", str(exc), "8", "60"])
                continue
            note = str(body.get("reload_warning") or "Saved.")
            run_dialog(["--msgbox", note, "8", "60"])
            data = load_settings()
            section = find_section(str(section["name"]), data)
    return 0


def repl() -> int:
    data = load_settings()
    names = [str(section["name"]) for section in _sections(data)]

    def completer(text: str, state: int) -> str | None:
        buf = readline.get_line_buffer()
        parts = buf.split()
        if len(parts) <= 1:
            options = [
                name
                for name in names + ["list", "help", "backup", "restore", "quit"]
                if name.startswith(text)
            ]
        else:
            try:
                section = find_section(parts[0], data)
                options = [
                    str(field["name"])
                    for field in section.get("fields") or []
                    if str(field["name"]).startswith(text)
                ]
                if section.get("name") == "gpu":
                    options.extend(
                        name for name in (*GPU_PROFILE_NAMES, "status") if name.startswith(text)
                    )
            except SettingsError:
                options = []
        return options[state] if state < len(options) else None

    try:
        readline.parse_and_bind("tab: complete")
        readline.set_completer(completer)
    except Exception:
        pass
    print("tsctl  (quit / q to leave, tab completes)")
    while True:
        try:
            line = input("tsctl> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line or line in ("q", "quit", "exit"):
            return 0
        try:
            code = dispatch(shlex.split(line))
        except SettingsError as exc:
            print(exc, file=sys.stderr)
            code = 1
        if code and code != 0:
            pass
    return 0


def _stack_backup_args(action: str, argv: list[str]) -> tuple[str, dict[str, bool]]:
    if not argv or argv[0].startswith("-"):
        raise SettingsError(
            f"Usage: tsctl {action} PATH "
            "[--models] [--config] [--users] [--chats] [--dry-run]"
        )
    path = argv[0]
    allowed = {"--models", "--config", "--users", "--chats", "--dry-run", "--yes"}
    unknown = [token for token in argv[1:] if token not in allowed]
    if unknown:
        raise SettingsError(f"Unknown {action} option: {unknown[0]}")
    selected = set(argv[1:])
    exact_restore_groups = action == "restore" and bool(
        selected.intersection({"--models", "--config", "--users", "--chats"})
    )
    return path, {
        "include_models": action == "backup" or not exact_restore_groups or "--models" in selected,
        "include_config": "--config" in selected,
        "include_users": "--users" in selected,
        "include_chats": "--chats" in selected,
        "dry_run": "--dry-run" in selected,
        "yes": "--yes" in selected,
    }


def _print_stack_plan(action: str, plan: dict[str, Any]) -> None:
    from ui.stack_backup import format_bytes

    location = plan.get("destination") if action == "backup" else plan.get("source")
    print(f"{'Destination' if action == 'backup' else 'Source'}: {location}")
    print(f"Files: {plan.get('files', 0)}")
    for group in ("models", "config", "users", "chats"):
        size = int((plan.get("totals") or {}).get(group) or 0)
        if size or group in (plan.get("groups") or []):
            print(f"{group.capitalize()}: {format_bytes(size)}")
    print(f"To copy: {format_bytes(int(plan.get('needed_bytes') or 0))}")
    if action == "backup":
        print(f"Free space: {format_bytes(int(plan.get('free_bytes') or 0))}")


def stack_backup_command(action: str, argv: list[str]) -> int:
    from ui.stack_backup import (
        StackBackupError,
        plan_backup,
        plan_restore,
        run_backup,
        run_restore,
    )

    path, options = _stack_backup_args(action, argv)
    common = {
        "include_config": options["include_config"],
        "include_users": options["include_users"],
        "include_chats": options["include_chats"],
    }
    try:
        if action == "backup":
            plan = plan_backup(path, **common)
        else:
            plan = plan_restore(path, include_models=options["include_models"], **common)
    except StackBackupError as exc:
        raise SettingsError(str(exc)) from exc
    _print_stack_plan(action, plan)
    if options["dry_run"]:
        return 0
    if action == "backup" and not plan.get("enough_space"):
        raise SettingsError("Not enough free space for this backup")
    if sys.stdin.isatty() and not options["yes"]:
        answer = input(f"{action.capitalize()} now? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 0
    progress = lambda line: print(line, flush=True)
    try:
        if action == "backup":
            result = run_backup(path, on_progress=progress, **common)
        else:
            result = run_restore(
                path,
                include_models=options["include_models"],
                on_progress=progress,
                **common,
            )
    except StackBackupError as exc:
        raise SettingsError(str(exc)) from exc
    print(f"{action.capitalize()} complete: {result.get('manifest') or result.get('source')}")
    if action == "restore" and result.get("restart_recommended"):
        print("Config was restored. Run: tsctl restart")
    return 0


def dialog_stack_backup(action: str) -> int:
    code, path = run_dialog(
        [
            "--title",
            f"Stack {action}",
            "--dselect",
            str(Path.home()) + "/",
            "12",
            "72",
        ]
    )
    if code != 0 or not path:
        return 0
    checklist = [
        "config",
        "Config and system settings",
        "off",
        "users",
        "UI users and API tokens",
        "off",
        "chats",
        "All chats, Code files and gallery",
        "off",
    ]
    if action == "restore":
        checklist = ["models", "Model weights", "on", *checklist]
    code, raw = run_dialog(
        [
            "--title",
            f"Stack {action}",
            "--checklist",
            "Select data. Models are always included in backups.",
            "16",
            "76",
            "8",
            *checklist,
        ]
    )
    if code != 0:
        return 0
    selected = set(shlex.split(raw))
    flags = [f"--{name}" for name in ("models", "config", "users", "chats") if name in selected]
    if action == "backup":
        flags = [flag for flag in flags if flag != "--models"]
    path_value, options = _stack_backup_args(action, [path, *flags])
    from ui.stack_backup import plan_backup, plan_restore, run_backup, run_restore, summary_lines

    common = {
        "include_config": options["include_config"],
        "include_users": options["include_users"],
        "include_chats": options["include_chats"],
    }
    try:
        plan = (
            plan_backup(path_value, **common)
            if action == "backup"
            else plan_restore(path_value, include_models=options["include_models"], **common)
        )
        lines = summary_lines(plan) if action == "backup" else [
            f"Source: {plan['source']}",
            f"Files: {plan['files']}",
            f"To copy: {plan['needed_bytes']} bytes",
        ]
        code, _ = run_dialog(
            [
                "--title",
                f"Confirm {action}",
                "--yesno",
                "\n".join(lines),
                "16",
                "72",
            ]
        )
        if code != 0:
            return 0
        progress = lambda line: print(line, flush=True)
        result = (
            run_backup(path_value, on_progress=progress, **common)
            if action == "backup"
            else run_restore(
                path_value,
                include_models=options["include_models"],
                on_progress=progress,
                **common,
            )
        )
        note = f"{action.capitalize()} complete."
        if result.get("restart_recommended"):
            note += "\nRun tsctl restart to apply restored config."
        run_dialog(["--title", f"Stack {action}", "--msgbox", note, "9", "64"])
    except (SettingsError, ValueError) as exc:
        run_dialog(["--title", f"Stack {action} failed", "--msgbox", str(exc), "10", "70"])
        return 1
    return 0


def dispatch(argv: list[str]) -> int:
    if not argv:
        return tui()
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "--complete":
        cword = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 1
        words = argv[2:] if len(argv) > 2 else []
        print(" ".join(complete_words(cword, words)))
        return 0
    if argv[0] == "list":
        for section in _sections():
            print(f"{section['name']}\t{section.get('label') or ''}")
        return 0
    if argv[0] == "restart":
        return restart_api()
    if argv[0] in ("backup", "restore"):
        return stack_backup_command(argv[0], argv[1:])

    section_name = argv[0]
    rest = argv[1:]
    if section_name == "screensaver" and rest and rest[0] in ("enable", "disable", "status"):
        if rest[0] == "status":
            return saver_status()
        body = save_settings({"screensaver": {"enabled": rest[0] == "enable"}})
        return report_save(body)
    if section_name == "gpu":
        if not rest or rest[0] == "status":
            return gpu_status()
        if rest[0] == "apply":
            from ui.settings import apply_gpu_unit

            warning = apply_gpu_unit()
            if warning:
                print(warning, file=sys.stderr)
                return 1
            print("ok")
            return 0
        if rest[0] in GPU_PROFILE_NAMES and "=" not in rest[0] and len(rest) == 1:
            body = save_settings({"gpu": {"profile": rest[0]}})
            return report_save(body)

    data = load_settings()
    section = find_section(section_name, data)
    if not rest:
        return print_section(section)
    if len(rest) == 1 and "=" not in rest[0]:
        field = field_by_name(section, rest[0])
        print(format_value(field.get("value")))
        return 0
    pairs = parse_pairs(rest)
    return report_save(apply_sets(str(section["name"]), pairs))


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return dispatch(args)
    except SettingsError as exc:
        print(f"tsctl: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
