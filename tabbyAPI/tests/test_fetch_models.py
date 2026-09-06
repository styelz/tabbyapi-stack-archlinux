import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ARCH = Path(__file__).resolve().parents[1] / "deploy" / "arch"
sys.path.insert(0, str(ARCH))

from fetch_models import (  # noqa: E402
    baseline_pick_ids,
    copy_from_cache,
    dest_path,
    disk_gib_for_ids,
    expand_pick_ids,
    extra_item_id,
    find_cache,
    format_pick_label,
    installer_tqdm_class,
    is_ready,
    list_pick_rows,
    load_catalog,
    select_ids,
    shards_complete,
    verify_tree,
    write_selected_catalog,
)


CATALOG = Path(__file__).resolve().parents[1] / "deploy" / "arch" / "models.json"


class FetchModelsTests(unittest.TestCase):
    def test_catalog_sets_are_known_items(self):
        catalog = load_catalog(CATALOG)
        items = catalog["items"]
        for name, members in catalog["sets"].items():
            for item_id in members:
                self.assertIn(item_id, items, msg=f"{name} references missing {item_id}")
        self.assertIn("qwen", catalog["sets"]["core"])
        self.assertIn("qwen36", catalog["sets"]["all"])
        self.assertNotIn("qwen36", catalog["sets"]["core"])

    def test_select_ids_rejects_unknown_set(self):
        with self.assertRaises(SystemExit):
            select_ids({"sets": {"core": ["qwen"]}}, "nope")

    def test_is_ready_file_and_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            missing = root / "missing.safetensors"
            self.assertFalse(is_ready(missing, {"kind": "file"}))
            present = root / "flux.safetensors"
            present.write_bytes(b"ok")
            self.assertTrue(is_ready(present, {"kind": "file"}))

            snap = root / "model"
            snap.mkdir()
            item = {"kind": "snapshot", "ready": ["model.safetensors"]}
            self.assertFalse(is_ready(snap, item))
            (snap / "model.safetensors").write_bytes(b"weights")
            self.assertTrue(is_ready(snap, item))

    def test_half_copied_sharded_model_is_not_ready(self):
        item = {"kind": "snapshot", "ready": ["model.safetensors", "quantization_config.json"]}
        with tempfile.TemporaryDirectory() as raw:
            snap = Path(raw)
            (snap / "quantization_config.json").write_text("{}", encoding="utf-8")
            (snap / "model-00001-of-00002.safetensors").write_bytes(b"shard one")
            self.assertIs(shards_complete(snap), False)
            self.assertFalse(is_ready(snap, item))

            (snap / "model-00002-of-00002.safetensors").write_bytes(b"shard two")
            self.assertIs(shards_complete(snap), True)
            self.assertTrue(is_ready(snap, item))

    def test_interrupted_download_marker_blocks_ready(self):
        item = {"kind": "snapshot", "ready": ["model.safetensors"]}
        with tempfile.TemporaryDirectory() as raw:
            snap = Path(raw)
            (snap / "model.safetensors").write_bytes(b"weights")
            self.assertTrue(is_ready(snap, item))
            partial = snap / ".cache" / "huggingface" / "download"
            partial.mkdir(parents=True)
            (partial / "model.safetensors.incomplete").write_bytes(b"partial")
            self.assertFalse(is_ready(snap, item))

    def test_file_copy_is_atomic_and_size_checked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "src.safetensors"
            src.write_bytes(b"x" * 4096)
            dest = root / "out" / "dest.safetensors"
            copy_from_cache(src, dest, "file")
            self.assertEqual(dest.read_bytes(), src.read_bytes())
            self.assertEqual(list(dest.parent.glob(".*.part")), [])

    def test_folder_copy_is_verified_against_the_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "src"
            src.mkdir()
            (src / "model.safetensors").write_bytes(b"y" * 2048)
            dest = root / "dest"
            copy_from_cache(src, dest, "snapshot")
            self.assertEqual((dest / "model.safetensors").stat().st_size, 2048)
            verify_tree(src, dest)

            (dest / "model.safetensors").write_bytes(b"y")
            with self.assertRaises(SystemExit):
                verify_tree(src, dest)

            (dest / "model.safetensors").unlink()
            with self.assertRaises(SystemExit):
                verify_tree(src, dest)

    def test_dest_and_cache_lookup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tabby = root / "tabby"
            comfy = root / "comfy"
            cache = root / "cache"
            dest = dest_path({"dest": "tabby/models/Qwen3.5-9B-exl3-4.00bpw"}, tabby, comfy)
            self.assertEqual(dest, tabby / "models" / "Qwen3.5-9B-exl3-4.00bpw")

            item = {
                "kind": "file",
                "dest": "comfy/models/checkpoints/flux1-schnell-fp8.safetensors",
                "cache": ["ComfyUI/models/checkpoints/flux1-schnell-fp8.safetensors"],
            }
            self.assertIsNone(find_cache(item, cache))
            cached = cache / "ComfyUI/models/checkpoints/flux1-schnell-fp8.safetensors"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"flux")
            self.assertEqual(find_cache(item, cache), cached.resolve())

    def test_find_cache_searches_layout_variants(self):
        snap_item = {
            "kind": "snapshot",
            "dest": "tabby/models/Qwen3.5-9B-exl3-4.00bpw",
            "cache": ["tabbyAPI/models/Qwen3.5-9B-exl3-4.00bpw"],
            "ready": ["model.safetensors"],
        }
        file_item = {
            "kind": "file",
            "dest": "comfy/models/checkpoints/flux1-schnell-fp8.safetensors",
            "cache": ["ComfyUI/models/checkpoints/flux1-schnell-fp8.safetensors"],
        }

        def ready_snap(path: Path) -> Path:
            path.mkdir(parents=True)
            (path / "model.safetensors").write_bytes(b"weights")
            return path

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            models_dir = root / "just-models"
            snap = ready_snap(models_dir / "Qwen3.5-9B-exl3-4.00bpw")
            self.assertEqual(find_cache(snap_item, models_dir), snap.resolve())

            tabby_root = root / "tabbyAPI"
            snap = ready_snap(tabby_root / "models" / "Qwen3.5-9B-exl3-4.00bpw")
            self.assertEqual(find_cache(snap_item, tabby_root), snap.resolve())

            usb = root / "usb"
            snap = ready_snap(
                usb / "tabbyapi-stack" / "tabbyAPI" / "models" / "Qwen3.5-9B-exl3-4.00bpw"
            )
            self.assertEqual(find_cache(snap_item, usb), snap.resolve())

            old_usb = root / "old-usb"
            snap = ready_snap(
                old_usb / "tabby-stack" / "tabbyAPI" / "models" / "Qwen3.5-9B-exl3-4.00bpw"
            )
            self.assertEqual(find_cache(snap_item, old_usb), snap.resolve())

            self.assertEqual(find_cache(snap_item, snap), snap.resolve())

            loose = root / "loose"
            loose.mkdir()
            flux = loose / "flux1-schnell-fp8.safetensors"
            flux.write_bytes(b"flux")
            self.assertEqual(find_cache(file_item, loose), flux.resolve())

            nested = root / "nested" / "copy"
            flux = nested / "extra" / "flux1-schnell-fp8.safetensors"
            flux.parent.mkdir(parents=True)
            flux.write_bytes(b"flux")
            self.assertEqual(find_cache(file_item, nested), flux.resolve())

    def test_find_cache_hub_snapshot(self):
        item = {
            "kind": "snapshot",
            "repo": "turboderp/Qwen3.5-9B-exl3",
            "revision": "4.00bpw",
            "dest": "tabby/models/Qwen3.5-9B-exl3-4.00bpw",
            "cache": ["tabbyAPI/models/Qwen3.5-9B-exl3-4.00bpw"],
            "ready": ["model.safetensors"],
        }
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            snap = (
                cache
                / "hub"
                / "models--turboderp--Qwen3.5-9B-exl3"
                / "snapshots"
                / "4.00bpw"
            )
            snap.mkdir(parents=True)
            (snap / "model.safetensors").write_bytes(b"weights")
            self.assertEqual(find_cache(item, cache), snap.resolve())

    def test_picks_reference_known_items(self):
        catalog = load_catalog(CATALOG)
        items = catalog["items"]
        for pick in catalog["picks"]:
            for item_id in pick["items"]:
                self.assertIn(item_id, items, msg=f"pick {pick['id']} missing {item_id}")

    def test_expand_picks_and_comma_ids(self):
        catalog = load_catalog(CATALOG)
        self.assertEqual(select_ids(catalog, "core"), catalog["sets"]["core"])
        ids = expand_pick_ids(catalog, "qwen,qwen-image")
        self.assertIn("qwen", ids)
        self.assertIn("qwen-image-unet", ids)
        self.assertIn("qwen-image-lora", ids)
        with self.assertRaises(SystemExit):
            expand_pick_ids(catalog, "nope")

    def test_list_picks_hf_filters_vram(self):
        catalog = load_catalog(CATALOG)
        rows_8g = list_pick_rows(catalog, vram_mib=8192, source="hf")
        ids_8g = {row["id"] for row in rows_8g}
        self.assertIn("qwen", ids_8g)
        self.assertIn("embed", ids_8g)
        self.assertIn("flux", ids_8g)
        self.assertNotIn("qwen35", ids_8g)
        self.assertNotIn("qwen-image", ids_8g)
        on_ids = {row["id"] for row in rows_8g if row["on"]}
        self.assertEqual(on_ids, {"qwen", "embed"})

        rows_12g = list_pick_rows(catalog, vram_mib=12288, source="hf")
        ids_12g = {row["id"] for row in rows_12g}
        self.assertIn("qwen35", ids_12g)
        self.assertIn("qwen-image", ids_12g)
        on_12g = {row["id"] for row in rows_12g if row["on"]}
        self.assertEqual(on_12g, {"qwen", "embed", "qwen-image"})

    def test_simple_baseline_falls_back_to_flux_on_8g(self):
        catalog = load_catalog(CATALOG)
        self.assertEqual(baseline_pick_ids(catalog, 8192), ["qwen", "embed", "flux"])
        self.assertEqual(baseline_pick_ids(catalog, 12288), ["qwen", "embed", "qwen-image"])
        self.assertEqual(baseline_pick_ids(catalog, 0), ["qwen", "embed", "qwen-image"])

        extras = list_pick_rows(catalog, vram_mib=12288, source="hf", extras_only=True)
        self.assertNotIn("qwen", {row["id"] for row in extras})
        self.assertNotIn("qwen-image", {row["id"] for row in extras})
        self.assertIn("flux", {row["id"] for row in extras})
        self.assertIn("~17 GiB", format_pick_label(next(row for row in extras if row["id"] == "flux")))

    def test_installer_progress_uses_newline_status(self):
        class FakeTqdm:
            def __init__(self, *args, **kwargs):
                self.total = kwargs.get("total")
                self.n = 0
                self.initial = kwargs.get("initial", 0)
                self.desc = kwargs.get("desc", "")

            def display(self, msg=None, pos=None):
                return None

            def update(self, amount=1):
                self.n += amount
                self.display()

        package = types.ModuleType("tqdm")
        package.__path__ = []
        auto = types.ModuleType("tqdm.auto")
        auto.tqdm = FakeTqdm
        package.auto = auto
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"tqdm": package, "tqdm.auto": auto}):
            with contextlib.redirect_stdout(output):
                bar = installer_tqdm_class("model.safetensors")(total=100)
                bar._tabby_last = 0
                bar.update(100)
        line = output.getvalue()
        self.assertIn("model.safetensors", line)
        self.assertIn("100%", line)
        self.assertIn("/s", line)

    def test_list_picks_cache_found_and_extra(self):
        catalog = load_catalog(CATALOG)
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            qwen = cache / "tabbyAPI" / "models" / "Qwen3.5-9B-exl3-4.00bpw"
            qwen.mkdir(parents=True)
            (qwen / "model.safetensors").write_bytes(b"weights")
            extra = cache / "tabbyAPI" / "models" / "MyLocal-7B"
            extra.mkdir()
            (extra / "model.safetensors").write_bytes(b"weights")
            rows = list_pick_rows(catalog, cache_root=cache, source="cache")
            ids = {row["id"] for row in rows}
            self.assertIn("qwen", ids)
            self.assertNotIn("flux", ids)
            self.assertIn(extra_item_id("MyLocal-7B"), ids)

    def test_write_selected_catalog_and_disk(self):
        catalog = load_catalog(CATALOG)
        self.assertEqual(disk_gib_for_ids(catalog, "core"), 29)
        self.assertGreater(disk_gib_for_ids(catalog, "all"), disk_gib_for_ids(catalog, "core"))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "models.json"
            selected = expand_pick_ids(catalog, "qwen,embed")
            write_selected_catalog(path, catalog, selected, {})
            saved = load_catalog(path)
            self.assertEqual(saved["sets"]["selected"], selected)
            self.assertEqual(select_ids(saved, "selected"), selected)


if __name__ == "__main__":
    unittest.main()
