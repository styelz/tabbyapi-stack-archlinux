#!/usr/bin/env python3
"""Copy or Hugging Face–download installer weights. Skip anything already on disk."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.safetensors$")
INCOMPLETE_GLOBS = (
    "*.incomplete",
    "*.part",
    ".cache/huggingface/download/**/*.incomplete",
)


def load_catalog(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def unique_keep(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def catalog_picks(catalog: dict) -> list[dict]:
    picks = catalog.get("picks")
    if isinstance(picks, list) and picks:
        return [p for p in picks if isinstance(p, dict) and p.get("id")]
    # Older catalogs with no picks: one row per item.
    return [
        {"id": name, "items": [name], "label": name, "min_vram_mib": 0, "disk_gib": 5}
        for name in (catalog.get("items") or {})
    ]


def pick_map(catalog: dict) -> dict[str, dict]:
    return {str(p["id"]): p for p in catalog_picks(catalog)}


def pick_ids_for_items(catalog: dict, item_ids: set[str]) -> list[str]:
    """Return whole catalog picks fully represented by an item-id set."""
    return [
        str(pick["id"])
        for pick in catalog_picks(catalog)
        if pick.get("items")
        and all(str(item_id) in item_ids for item_id in pick.get("items") or [])
    ]


def baseline_pick_ids(catalog: dict, vram_mib: int = 0) -> list[str]:
    """Simple-mode picks, replacing Qwen-Image with Flux on sub-10 GiB GPUs."""
    core_items = set(str(x) for x in (catalog.get("sets") or {}).get("core") or [])
    baseline = pick_ids_for_items(catalog, core_items)
    picks = pick_map(catalog)
    qwen_image = picks.get("qwen-image") or {}
    image_need = int(qwen_image.get("min_vram_mib") or 0)
    if vram_mib > 0 and image_need > vram_mib and "qwen-image" in baseline:
        baseline.remove("qwen-image")
        flux = picks.get("flux") or {}
        flux_need = int(flux.get("min_vram_mib") or 0)
        if flux and (flux_need <= 0 or flux_need <= vram_mib):
            baseline.append("flux")
    return baseline


def expand_pick_ids(catalog: dict, raw: str) -> list[str]:
    """Turn a set name, pick ids, or item ids into catalog item ids."""
    text = (raw or "").strip()
    sets = catalog.get("sets") or {}
    if text in sets:
        return unique_keep(list(sets[text]))
    tokens = [part.strip() for part in text.replace(" ", ",").split(",") if part.strip()]
    if not tokens:
        raise SystemExit("No models selected.")
    items = catalog.get("items") or {}
    picks = pick_map(catalog)
    out: list[str] = []
    unknown: list[str] = []
    for token in tokens:
        if token in picks:
            out.extend(str(x) for x in picks[token].get("items") or [])
        elif token in items or token.startswith("local-"):
            out.append(token)
        else:
            unknown.append(token)
    if unknown:
        known = ", ".join(sorted(list(sets) + list(picks) + list(items))) or "(none)"
        raise SystemExit(f"Unknown model id {unknown[0]!r}. Use one of: {known}")
    return unique_keep(out)


def select_ids(catalog: dict, model_set: str) -> list[str]:
    return expand_pick_ids(catalog, model_set)


def vram_label(mib: int) -> str:
    if mib <= 0:
        return "CPU"
    gb = max(1, int(round(mib / 1024.0)))
    return f"{gb} GB"


def extra_item_id(folder_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", folder_name).strip("-")
    return f"local-{safe or 'model'}"


def extra_snapshot_item(folder: Path) -> dict:
    dest_name = folder.name
    return {
        "kind": "snapshot",
        "repo": "",
        "dest": f"tabby/models/{dest_name}",
        "cache": [f"tabbyAPI/models/{dest_name}", dest_name, f"models/{dest_name}"],
        "ready": ["model.safetensors", "quantization_config.json"],
    }


def known_dest_names(catalog: dict) -> set[str]:
    names: set[str] = set()
    for item in (catalog.get("items") or {}).values():
        dest = (item or {}).get("dest") or ""
        if dest:
            names.add(Path(dest).name)
        for rel in item.get("cache") or []:
            names.add(Path(rel).name)
    return names


def extra_model_dirs(cache_root: Path, catalog: dict) -> list[Path]:
    """Snapshot folders under the cache that are not already in the catalog."""
    known = known_dest_names(catalog)
    roots = [
        cache_root,
        cache_root / "models",
        cache_root / "tabbyAPI" / "models",
        cache_root / "tabbyapi-stack" / "tabbyAPI" / "models",
        cache_root / "tabby-stack" / "tabbyAPI" / "models",
    ]
    found: list[Path] = []
    seen: set[Path] = set()
    probe = {"kind": "snapshot", "ready": ["model.safetensors", "quantization_config.json"]}
    for base in roots:
        if not base.is_dir():
            continue
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name in SKIP_DIR_NAMES or child.name.startswith("."):
                continue
            if child.name in known:
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            if is_ready(child, probe) or any(child.glob("model*.safetensors")):
                seen.add(resolved)
                found.append(child)
    return found


def pick_is_found(catalog: dict, pick: dict, cache_root: Path | None) -> bool:
    if cache_root is None:
        return False
    items = catalog.get("items") or {}
    for item_id in pick.get("items") or []:
        item = items.get(item_id)
        if item and find_cache(item, cache_root) is not None:
            return True
    return False


def list_pick_rows(
    catalog: dict,
    *,
    cache_root: Path | None = None,
    vram_mib: int = 0,
    source: str = "hf",
    extras_only: bool = False,
    selected_ids: str = "",
) -> list[dict]:
    """Rows for the installer checklist: id, on, label."""
    core_ids = set((catalog.get("sets") or {}).get("core") or [])
    rows: list[dict] = []
    if source == "cache":
        for pick in catalog_picks(catalog):
            if not pick_is_found(catalog, pick, cache_root):
                continue
            rows.append(
                {
                    "id": str(pick["id"]),
                    "on": True,
                    "label": str(pick.get("label") or pick["id"]),
                    "min_vram_mib": int(pick.get("min_vram_mib") or 0),
                    "disk_gib": int(pick.get("disk_gib") or 0),
                }
            )
        if cache_root is not None:
            for folder in extra_model_dirs(cache_root, catalog):
                rows.append(
                    {
                        "id": extra_item_id(folder.name),
                        "on": True,
                        "label": f"{folder.name} (local)",
                        "min_vram_mib": 0,
                        "disk_gib": 0,
                        "extra_path": str(folder),
                    }
                )
        return rows

    selected_items = set(expand_pick_ids(catalog, selected_ids)) if selected_ids else set()
    baseline_ids = set(baseline_pick_ids(catalog, vram_mib))
    for pick in catalog_picks(catalog):
        pick_id = str(pick["id"])
        if extras_only and pick_id in baseline_ids:
            continue
        need = int(pick.get("min_vram_mib") or 0)
        if vram_mib > 0 and need > vram_mib:
            continue
        item_ids = [str(x) for x in pick.get("items") or []]
        default_on = bool(item_ids) and (
            all(i in selected_items for i in item_ids)
            if selected_ids
            else all(i in core_ids for i in item_ids)
        )
        rows.append(
            {
                "id": str(pick["id"]),
                "on": default_on,
                "label": str(pick.get("label") or pick["id"]),
                "min_vram_mib": need,
                "disk_gib": int(pick.get("disk_gib") or 0),
            }
        )
    return rows


def format_pick_label(row: dict, vram_mib: int = 0) -> str:
    label = str(row.get("label") or row.get("id") or "")
    need = int(row.get("min_vram_mib") or 0)
    disk = int(row.get("disk_gib") or 0)
    if disk > 0:
        label = f"{label} — ~{disk} GiB"
    if vram_mib > 0 and need > vram_mib:
        label = f"{label} (needs {vram_label(need)})"
    elif vram_mib <= 0 and need > 0:
        label = f"{label} ({vram_label(need)})"
    return label[:70]


def disk_gib_for_ids(catalog: dict, raw: str) -> int:
    picks = pick_map(catalog)
    item_ids = expand_pick_ids(catalog, raw)
    total = 0
    covered: set[str] = set()
    for pick in catalog_picks(catalog):
        members = [str(x) for x in pick.get("items") or []]
        if members and all(m in item_ids for m in members):
            total += int(pick.get("disk_gib") or 0)
            covered.update(members)
    for item_id in item_ids:
        if item_id not in covered:
            if item_id in picks:
                total += int(picks[item_id].get("disk_gib") or 0)
            else:
                total += 5
    return total


def extra_items_from_cache(catalog: dict, cache_root: Path | None, selected: list[str]) -> dict[str, dict]:
    extras: dict[str, dict] = {}
    if cache_root is None:
        return extras
    for folder in extra_model_dirs(cache_root, catalog):
        item_id = extra_item_id(folder.name)
        if item_id in selected:
            extras[item_id] = extra_snapshot_item(folder)
    return extras


def write_selected_catalog(path: Path, catalog: dict, selected: list[str], extras: dict[str, dict]) -> None:
    items = catalog.setdefault("items", {})
    items.update(extras)
    catalog.setdefault("sets", {})["selected"] = selected
    path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")


def dest_path(item: dict, tabby: Path, comfy: Path) -> Path:
    rel = item["dest"]
    if rel.startswith("tabby/"):
        return tabby / rel[len("tabby/") :]
    if rel.startswith("comfy/"):
        return comfy / rel[len("comfy/") :]
    raise SystemExit(f"dest must start with tabby/ or comfy/: {rel}")


def has_incomplete_downloads(dest: Path) -> bool:
    """True when an earlier interrupted run left partial files behind."""
    return any(next(dest.glob(pattern), None) is not None for pattern in INCOMPLETE_GLOBS)


def shards_complete(dest: Path) -> bool | None:
    """Whether every shard of a split safetensors model is present.

    Returns None when the folder is not sharded, so the caller falls back to
    the catalog's marker files. A single shard of a two-part model must not
    count as a finished download.
    """
    groups: dict[tuple[str, int], set[int]] = {}
    for path in dest.glob("*-of-*.safetensors"):
        match = SHARD_RE.match(path.name)
        if match and path.stat().st_size > 0:
            key = (match.group("stem"), int(match.group("total")))
            groups.setdefault(key, set()).add(int(match.group("index")))
    if not groups:
        return None
    return all(seen == set(range(1, total + 1)) for (_, total), seen in groups.items())


def is_ready(dest: Path, item: dict) -> bool:
    if item.get("kind") == "file":
        return dest.is_file() and dest.stat().st_size > 0
    if not dest.is_dir():
        return False
    if has_incomplete_downloads(dest):
        return False
    if shards_complete(dest) is False:
        return False
    for name in item.get("ready") or []:
        found = dest / name
        if found.is_file() and found.stat().st_size > 0:
            return True
    return any(dest.glob("model*.safetensors"))


SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pyenv",
    "pyenv",
    "lost+found",
    "$RECYCLE.BIN",
    "System Volume Information",
}
MAX_SEARCH_DEPTH = 6
MAX_WALK_DIRS = 4000


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _wanted_names(item: dict) -> list[str]:
    names: list[str] = []
    dest = item.get("dest") or ""
    if dest:
        names.append(Path(dest).name)
    for rel in item.get("cache") or []:
        names.append(Path(rel).name)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _relative_suffixes(item: dict) -> list[Path]:
    suffixes: list[Path] = []
    for rel in item.get("cache") or []:
        suffixes.append(Path(rel))
    dest = item.get("dest") or ""
    if dest.startswith("tabby/"):
        rel = Path(dest[len("tabby/") :])
        suffixes.extend(
            (
                rel,
                Path(rel.name),
                Path("tabbyapi-stack") / "tabbyAPI" / rel,
                Path("tabby-stack") / "tabbyAPI" / rel,
            )
        )
    elif dest.startswith("comfy/"):
        rel = Path(dest[len("comfy/") :])
        suffixes.extend(
            (
                rel,
                Path("ComfyUI") / rel,
                Path("tabbyapi-stack") / "ComfyUI" / rel,
                Path("tabby-stack") / "ComfyUI" / rel,
                Path(rel.name),
            )
        )
        if len(rel.parts) >= 2:
            suffixes.append(Path(*rel.parts[-2:]))
    return _unique_paths(suffixes)


def _hub_snapshot_paths(item: dict, cache_root: Path) -> list[Path]:
    repo = item.get("repo") or ""
    if "/" not in repo:
        return []
    hub_name = "models--" + repo.replace("/", "--")
    rev = item.get("revision") or "main"
    remote = item.get("remote") or ""
    remote_name = Path(remote).name if remote else ""
    out: list[Path] = []
    for base in (
        cache_root,
        cache_root / "hub",
        cache_root / "huggingface" / "hub",
        cache_root / ".cache" / "huggingface" / "hub",
    ):
        snap = base / hub_name / "snapshots" / rev
        if item.get("kind") == "file":
            if remote:
                out.append(snap / remote)
            if remote_name:
                out.append(snap / remote_name)
        else:
            out.append(snap)
    return _unique_paths(out)


def _cache_hit(item: dict, candidate: Path) -> bool:
    if item.get("kind") == "file":
        return candidate.is_file() and candidate.stat().st_size > 0
    return candidate.is_dir() and is_ready(candidate, item)


def find_cache(item: dict, cache_root: Path | None) -> Path | None:
    """Return an existing copy of this item under cache_root, if any.

    Exact catalog paths are tried first (a tabbyapi-stack tree). If those miss,
    the given folder is searched for the same file or directory names so a
    models/ dir, a USB mount, or a Hugging Face hub cache still copies.
    """
    if cache_root is None:
        return None
    try:
        cache_root = cache_root.resolve()
    except OSError:
        return None
    if not cache_root.is_dir():
        return None

    tried: set[Path] = set()

    def consider(candidate: Path) -> Path | None:
        try:
            candidate = candidate.resolve()
        except OSError:
            return None
        if candidate in tried:
            return None
        tried.add(candidate)
        if _cache_hit(item, candidate):
            return candidate
        return None

    wanted = set(_wanted_names(item))
    if cache_root.name in wanted:
        hit = consider(cache_root)
        if hit is not None:
            return hit

    for rel in _relative_suffixes(item):
        hit = consider(cache_root / rel)
        if hit is not None:
            return hit

    for candidate in _hub_snapshot_paths(item, cache_root):
        hit = consider(candidate)
        if hit is not None:
            return hit

    if not wanted:
        return None

    walked = 0
    root_depth = len(cache_root.parts)
    for dirpath, dirnames, filenames in os.walk(cache_root, followlinks=False):
        walked += 1
        if walked > MAX_WALK_DIRS:
            break
        here = Path(dirpath)
        depth = len(here.parts) - root_depth
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(".")
        ]
        if depth > MAX_SEARCH_DEPTH:
            dirnames.clear()
            continue
        if item.get("kind") == "file":
            for name in filenames:
                if name in wanted:
                    hit = consider(here / name)
                    if hit is not None:
                        return hit
        else:
            if here.name in wanted:
                hit = consider(here)
                if hit is not None:
                    return hit
            for name in list(dirnames):
                if name in wanted:
                    hit = consider(here / name)
                    if hit is not None:
                        return hit
    return None


def fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(n)} B"


def note(msg: str) -> None:
    print(msg, flush=True)


def installer_tqdm_class(download_label: str):
    """tqdm class that uses normal log lines when the installer owns stdout."""
    from tqdm.auto import tqdm

    use_log_lines = not sys.stdout.isatty() or os.environ.get("TABBY_NESTED_UI") == "1"

    class InstallerTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            self._tabby_started = time.monotonic()
            self._tabby_last = 0.0
            if use_log_lines:
                kwargs["disable"] = False
                kwargs["mininterval"] = 1.0
            super().__init__(*args, **kwargs)

        def display(self, msg=None, pos=None):
            if not use_log_lines:
                return super().display(msg=msg, pos=pos)
            now = time.monotonic()
            total = int(self.total or 0)
            done = int(self.n or 0)
            complete = total > 0 and done >= total
            if not complete and now - self._tabby_last < 1.0:
                return
            self._tabby_last = now
            elapsed = max(now - self._tabby_started, 0.001)
            rate = max(0.0, done - int(self.initial or 0)) / elapsed
            detail = str(self.desc or "").strip()
            name = download_label if not detail else f"{download_label} ({detail})"
            if total > 0:
                percent = min(100, int(done * 100 / total))
                progress = f"{percent}% ({fmt_bytes(done)}/{fmt_bytes(total)})"
            else:
                progress = fmt_bytes(done)
            note(f"      downloading {name}: {progress} at {fmt_bytes(int(rate))}/s")

    return InstallerTqdm


def copy_file_logged(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    src_path = Path(src)
    size = src_path.stat().st_size if src_path.is_file() else 0
    note(f"      {src_path.name} ({fmt_bytes(size)})")
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)


def verify_tree(src: Path, dest: Path) -> None:
    """Fail loudly when a folder copy dropped or truncated a file."""
    for path in src.rglob("*"):
        parts = path.relative_to(src).parts
        if not path.is_file() or ".cache" in parts or "__pycache__" in parts:
            continue
        mirror = dest / path.relative_to(src)
        if not mirror.is_file() or mirror.stat().st_size != path.stat().st_size:
            raise SystemExit(f"incomplete copy: {mirror} does not match {path}")


def copy_from_cache(src: Path, dest: Path, kind: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        note(f"      {src.name} ({fmt_bytes(src.stat().st_size)}) -> {dest}")
        # Land the bytes on a temp name and rename, so an interrupted copy
        # cannot leave a truncated file that is_ready() accepts forever.
        tmp = dest.with_name(f".{dest.name}.part")
        tmp.unlink(missing_ok=True)
        try:
            shutil.copy2(src, tmp)
            if tmp.stat().st_size != src.stat().st_size:
                raise SystemExit(f"short copy: {src} -> {dest}")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".cache", "__pycache__"),
        copy_function=copy_file_logged,
    )
    verify_tree(src, dest)


def download_item(item: dict, dest: Path) -> None:
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError as exc:
        raise SystemExit(
            "huggingface_hub is missing. Install Tabby extras / pip install huggingface_hub."
        ) from exc

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    repo = item["repo"]
    revision = item.get("revision") or None
    dest.parent.mkdir(parents=True, exist_ok=True)
    # The custom tqdm retains normal terminal bars but emits throttled,
    # newline-oriented progress when stdout feeds the installer's log well.
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
    try:
        from huggingface_hub.utils import enable_progress_bars

        enable_progress_bars()
    except Exception:
        pass
    download_label = str(item.get("remote") or repo)
    tqdm_class = installer_tqdm_class(download_label)
    try:
        if item.get("kind") == "file":
            tmp = dest.parent / f".hf-{dest.name}"
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                path = hf_hub_download(
                    repo_id=repo,
                    filename=item["remote"],
                    revision=revision,
                    local_dir=str(tmp),
                    token=token,
                    tqdm_class=tqdm_class,
                )
                shutil.move(path, dest)
            finally:
                # An interrupted download otherwise leaves a multi-GB .hf-* dir
                # that the next run neither sees nor reuses.
                shutil.rmtree(tmp, ignore_errors=True)
            return
        snapshot_download(
            repo_id=repo,
            revision=revision,
            local_dir=str(dest),
            token=token,
            tqdm_class=tqdm_class,
        )
    except Exception as exc:
        hint = ""
        text = str(exc)
        if "401" in text or "403" in text or "gated" in text.lower():
            hint = " (gated repo: huggingface-cli login, or set HF_TOKEN)"
        raise SystemExit(f"download failed for {repo}: {exc}{hint}") from exc


def ensure_item(name: str, item: dict, tabby: Path, comfy: Path, cache_root: Path | None) -> str:
    dest = dest_path(item, tabby, comfy)
    if is_ready(dest, item):
        note(f"    have {name} ({dest})")
        return "have"
    cached = find_cache(item, cache_root)
    if cached is not None:
        note(f"    copy {name} from {cached}")
        copy_from_cache(cached, dest, item.get("kind") or "snapshot")
        if is_ready(dest, item):
            return "copy"
        note(f"    copy of {name} was incomplete; downloading")
    repo = item.get("repo") or ""
    if not repo:
        raise SystemExit(f"{name} is local-only and was not found in the cache")
    note(f"    download {name} from {repo}")
    note(f"      dest {dest}")
    download_item(item, dest)
    if not is_ready(dest, item):
        raise SystemExit(f"{name} finished but marker files are missing in {dest}")
    return "download"


def print_pick_rows(rows: list[dict], vram_mib: int = 0) -> None:
    for row in rows:
        state = "on" if row.get("on") else "off"
        label = format_pick_label(row, vram_mib).replace("\t", " ").replace('"', "'")
        print(f"{row['id']}\t{state}\t{label}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--tabby", type=Path, default=None)
    parser.add_argument("--comfy", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--set", dest="model_set", default="core")
    parser.add_argument("--ids", default="", help="Comma-separated pick or item ids")
    parser.add_argument("--list-picks", action="store_true")
    parser.add_argument("--baseline", action="store_true", help="Print Simple-mode baseline pick ids")
    parser.add_argument("--extras-only", action="store_true", help="Omit Simple-mode baseline picks")
    parser.add_argument("--selected-ids", default="", help="Picks/items checked in --list-picks output")
    parser.add_argument("--source", choices=("hf", "cache"), default=None)
    parser.add_argument("--vram-mib", type=int, default=0)
    parser.add_argument("--disk-gib", action="store_true")
    parser.add_argument("--update-catalog", action="store_true")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    items = catalog.get("items") or {}
    cache = args.cache if args.cache and args.cache.is_dir() else None
    source = args.source or ("cache" if cache else "hf")
    selection = (args.ids or args.model_set or "core").strip()

    if args.baseline:
        print(",".join(baseline_pick_ids(catalog, args.vram_mib)), flush=True)
        return 0

    if args.list_picks:
        rows = list_pick_rows(
            catalog,
            cache_root=cache,
            vram_mib=args.vram_mib,
            source=source,
            extras_only=args.extras_only,
            selected_ids=args.selected_ids,
        )
        print_pick_rows(rows, args.vram_mib)
        return 0

    if args.disk_gib:
        print(disk_gib_for_ids(catalog, selection), flush=True)
        return 0

    selected = expand_pick_ids(catalog, selection)
    extras = extra_items_from_cache(catalog, cache, selected)
    if extras:
        items.update(extras)
        catalog.setdefault("items", {}).update(extras)
    if args.update_catalog:
        write_selected_catalog(args.catalog, catalog, selected, extras)
        note(f"    catalog selected: {', '.join(selected)}")

    if args.tabby is None or args.comfy is None:
        if args.update_catalog:
            return 0
        raise SystemExit("--tabby and --comfy are required unless --list-picks / --disk-gib / --update-catalog")

    print(f"==> Weights ({selection}): {', '.join(selected)}", flush=True)
    if cache:
        print(f"    cache: {cache}", flush=True)
    else:
        print("    cache: none (Hugging Face)", flush=True)

    for name in selected:
        item = items.get(name)
        if not item:
            raise SystemExit(f"catalog is missing item {name!r}")
        ensure_item(name, item, args.tabby, args.comfy, cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
