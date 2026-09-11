import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ui import models as ui_models
from ui.models import (
    JOB_DONE_TTL_SEC,
    ModelPaths,
    ModelsError,
    cancel_download,
    delete_model,
    hf_folder_name,
    inspect_repo,
    is_gguf_only,
    revision_vram_fit,
    job_is_visible,
    job_state,
    library_state,
    matches_format,
    maybe_write_hf_profile,
    normalize_alias,
    parse_repo_id,
    profile_slug,
    public_job,
    recover_stale_job,
    sanitize_folder_name,
    search_models,
    set_profile_alias,
    start_download,
    write_job,
)


DISK_OK = {
    "free_bytes": 80 * 1024**3,
    "total_bytes": 100 * 1024**3,
    "used_bytes": 20 * 1024**3,
}


def _paths(root: Path) -> ModelPaths:
    models_dir = root / "models"
    profiles = root / "model_profiles"
    comfy = root / "ComfyUI"
    models_dir.mkdir()
    profiles.mkdir()
    (comfy / "models" / "checkpoints").mkdir(parents=True)
    return ModelPaths(
        root=root,
        models_dir=models_dir,
        profiles_dir=profiles,
        catalog_path=Path(__file__).resolve().parents[1] / "deploy" / "arch" / "models.json",
        job_path=profiles / "download_job.json",
        comfy_dir=comfy,
    )


def _llm_folder(models_dir: Path, name: str, *, seq: int = 8192) -> Path:
    folder = models_dir / name
    folder.mkdir()
    (folder / "config.json").write_text(
        json.dumps({"max_position_embeddings": seq, "model_type": "qwen2"}),
        encoding="utf-8",
    )
    (folder / "model.safetensors").write_bytes(b"weights")
    return folder


class FakeModel:
    def __init__(self, repo_id, tags=None, downloads=0, likes=0, gated=False):
        self.id = repo_id
        self.tags = tags or []
        self.downloads = downloads
        self.likes = likes
        self.gated = gated
        self.private = False
        self.pipeline_tag = "text-generation"
        self.last_modified = None
        self.siblings = []


class FakeApi:
    def __init__(self, models=None, refs=None, info=None, sizes=None):
        self.models = models or []
        self.refs = refs
        self.info = info
        self.sizes = sizes or {}
        self.list_calls = []

    def list_models(self, **kwargs):
        self.list_calls.append(kwargs)
        models = list(self.models)
        fmt = kwargs.get("filter")
        if fmt:
            models = [
                model
                for model in models
                if matches_format(model.id, model.tags, str(fmt))
            ]
        return models

    def model_info(self, repo_id, revision=None, files_metadata=False):
        if files_metadata:
            size, files = self.sizes.get((repo_id, revision or "main"), (100, 1))
            siblings = [
                SimpleNamespace(rfilename=f"file-{i}.safetensors", size=size // max(files, 1))
                for i in range(files)
            ]
            return SimpleNamespace(
                id=repo_id,
                siblings=siblings,
                tags=["exl3"],
                gated=False,
                private=False,
            )
        if self.info is not None:
            return self.info
        for model in self.models:
            if model.id == repo_id:
                return model
        return FakeModel(repo_id, tags=["exl3"])

    def list_repo_refs(self, repo_id):
        names = self.refs or ["main", "4.00bpw", "3.00bpw"]
        return SimpleNamespace(branches=[SimpleNamespace(name=name) for name in names])


class ParseAndFilterTests(unittest.TestCase):
    def test_parse_repo_id_from_url_and_slug(self):
        self.assertEqual(parse_repo_id("turboderp/Qwen3.5-9B-exl3"), "turboderp/Qwen3.5-9B-exl3")
        self.assertEqual(
            parse_repo_id("https://huggingface.co/turboderp/Qwen3.5-9B-exl3"),
            "turboderp/Qwen3.5-9B-exl3",
        )
        self.assertIsNone(parse_repo_id("just words"))
        self.assertIsNone(parse_repo_id("org/repo/extra"))

    def test_sanitize_rejects_traversal(self):
        for bad in ("..", "../x", "a/b", "a\\b", "x\0y"):
            with self.subTest(bad=bad):
                with self.assertRaises(ModelsError):
                    sanitize_folder_name(bad)

    def test_hf_folder_name_appends_revision(self):
        self.assertEqual(
            hf_folder_name("turboderp/Qwen3.5-9B-exl3", "4.00bpw"),
            "Qwen3.5-9B-exl3-4.00bpw",
        )
        self.assertEqual(hf_folder_name("org/MyModel", "main"), "MyModel")

    def test_search_filter_keeps_exl3_drops_gguf(self):
        self.assertTrue(matches_format("org/foo-exl3", ["text-generation"], "exl3"))
        self.assertFalse(matches_format("org/foo-exl2", ["exl2"], "exl3"))
        self.assertTrue(is_gguf_only("org/foo-gguf", ["gguf"]))
        self.assertFalse(is_gguf_only("org/foo-exl3", ["exl3", "gguf"]))

    def test_search_models_filters_and_caps(self):
        api = FakeApi(
            [
                FakeModel("org/keep-exl3", ["exl3"], downloads=9),
                FakeModel("org/skip-gguf", ["gguf"], downloads=99),
                FakeModel("org/skip-exl2", ["exl2"], downloads=8),
            ]
        )
        payload = search_models("qwen", "exl3", hf_api=api)
        ids = [row["id"] for row in payload["results"]]
        self.assertEqual(ids, ["org/keep-exl3"])
        self.assertTrue(payload["results"][0]["compatible"])
        self.assertEqual(api.list_calls[0].get("filter"), "exl3")
        self.assertNotIn("direction", api.list_calls[0])

    def test_search_exact_repo_even_if_gguf(self):
        api = FakeApi([FakeModel("someone/llama-gguf", ["gguf"])])
        payload = search_models("someone/llama-gguf", "exl3", hf_api=api)
        self.assertEqual(len(payload["results"]), 1)
        self.assertFalse(payload["results"][0]["compatible"])

    def test_inspect_repo_lists_quant_revisions(self):
        api = FakeApi(
            info=FakeModel("turboderp/Qwen3.5-9B-exl3", ["exl3"]),
            refs=["main", "4.00bpw", "docs"],
            sizes={
                ("turboderp/Qwen3.5-9B-exl3", "main"): (10, 1),
                ("turboderp/Qwen3.5-9B-exl3", "4.00bpw"): (20, 2),
            },
        )
        payload = inspect_repo(
            "turboderp/Qwen3.5-9B-exl3",
            hf_api=api,
            gpu={"vram_mib": 12288, "label": "RTX 4070 Ti 12 GB"},
        )
        names = [row["name"] for row in payload["revisions"]]
        self.assertIn("4.00bpw", names)
        self.assertIn("main", names)
        self.assertNotIn("docs", names)
        self.assertTrue(payload["compatible"])
        self.assertEqual(payload["vram_gb"], 12)

    def test_revision_vram_fit_uses_file_size(self):
        over = revision_vram_fit(22 * 1024**3, 12288)
        self.assertFalse(over["fits"])
        self.assertEqual(over["vram_badge"], "won't fit 12 GB")
        ok = revision_vram_fit(9 * 1024**3, 12288)
        self.assertTrue(ok["fits"])
        self.assertEqual(ok["vram_badge"], "")
        self.assertIsNone(revision_vram_fit(None, 12288)["fits"])
        self.assertIsNone(revision_vram_fit(22 * 1024**3, 0)["fits"])

    def test_inspect_repo_flags_revision_over_vram(self):
        repo = "malaiwah/Qwen3.8-27B-EXL3-K5K6-hydrated"
        api = FakeApi(
            info=FakeModel(repo, ["exl3"]),
            refs=["main", "2.00bpw"],
            sizes={
                (repo, "main"): (22 * 1024**3, 3),
                (repo, "2.00bpw"): (9 * 1024**3, 1),
            },
        )
        payload = inspect_repo(
            repo,
            hf_api=api,
            gpu={"vram_mib": 12288, "label": "RTX 4070 Ti 12 GB"},
        )
        by_name = {row["name"]: row for row in payload["revisions"]}
        self.assertEqual(payload["vram_gb"], 12)
        self.assertFalse(by_name["main"]["fits"])
        self.assertEqual(by_name["main"]["vram_badge"], "won't fit 12 GB")
        self.assertTrue(by_name["2.00bpw"]["fits"])
        self.assertEqual(by_name["2.00bpw"]["vram_badge"], "")


class LibraryAndDeleteTests(unittest.TestCase):
    def setUp(self):
        ui_models._CANCEL.clear()
        ui_models._THREAD = None
        ui_models._ACTIVE_PATHS = None

    def test_library_lists_llm_and_catalog_status(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            _llm_folder(paths.models_dir, "Qwen3.5-9B-exl3-4.00bpw")
            (paths.profiles_dir / "qwen.yml").write_text(
                "pretty: Qwen 9B\nmodel:\n  model_name: Qwen3.5-9B-exl3-4.00bpw\n",
                encoding="utf-8",
            )
            data = library_state(paths, loaded="Qwen3.5-9B-exl3-4.00bpw")
            self.assertEqual(len(data["llms"]), 1)
            self.assertEqual(data["llms"][0]["profile"], "qwen")
            self.assertTrue(data["llms"][0]["loaded"])
            qwen = next(row for row in data["catalog"] if row["id"] == "qwen")
            self.assertTrue(qwen["installed"])
            embed = next(row for row in data["catalog"] if row["id"] == "embed")
            self.assertEqual(embed["kind"], "embed")
            flux = next(row for row in data["catalog"] if row["id"] == "flux")
            self.assertFalse(flux["installed"])
            self.assertEqual(flux["kind"], "image")
            self.assertIn("free_bytes", data["disk"])

    def test_delete_refuses_loaded_and_removes_hf_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            _llm_folder(paths.models_dir, "Custom-exl3-4.00bpw")
            alias = maybe_write_hf_profile(
                "Custom-exl3-4.00bpw",
                repo_id="org/Custom-exl3",
                revision="4.00bpw",
                paths=paths,
            )
            self.assertTrue((paths.profiles_dir / f"{alias}.yml").is_file())
            with self.assertRaises(ModelsError) as caught:
                delete_model(
                    {"kind": "llm", "id": "Custom-exl3-4.00bpw"},
                    paths=paths,
                    loaded="Custom-exl3-4.00bpw",
                )
            self.assertEqual(caught.exception.status, 409)
            delete_model(
                {"kind": "llm", "id": "Custom-exl3-4.00bpw"},
                paths=paths,
                loaded="other",
            )
            self.assertFalse((paths.models_dir / "Custom-exl3-4.00bpw").exists())
            self.assertFalse((paths.profiles_dir / f"{alias}.yml").exists())

    def test_delete_does_not_remove_shipped_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            _llm_folder(paths.models_dir, "Qwen3.5-9B-exl3-4.00bpw")
            shipped = paths.profiles_dir / "qwen.yml"
            shipped.write_text(
                "pretty: Qwen\nmodel:\n  model_name: Qwen3.5-9B-exl3-4.00bpw\n",
                encoding="utf-8",
            )
            delete_model(
                {"kind": "llm", "id": "Qwen3.5-9B-exl3-4.00bpw"},
                paths=paths,
                loaded="",
            )
            self.assertTrue(shipped.is_file())

    def test_delete_image_unlinks_catalog_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            dest = paths.comfy_dir / "models" / "checkpoints" / "flux1-schnell-fp8.safetensors"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"flux")
            result = delete_model({"kind": "image", "id": "flux"}, paths=paths, loaded="")
            self.assertTrue(result["ok"])
            self.assertFalse(dest.exists())

    def test_profile_slug_collision_increments(self):
        self.assertEqual(profile_slug("Qwen3.5-9B-exl3-4.00bpw"), "qwen3-5-9b-exl3-4-00bpw")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            _llm_folder(paths.models_dir, "Foo-exl3")
            (paths.profiles_dir / "hf-foo-exl3.yml").write_text(
                "pretty: other\nmodel:\n  model_name: Other\n",
                encoding="utf-8",
            )
            alias = maybe_write_hf_profile("Foo-exl3", paths=paths)
            self.assertEqual(alias, "hf-foo-exl3-2")
            saved = (paths.profiles_dir / "hf-foo-exl3-2.yml").read_text(encoding="utf-8")
            self.assertIn("local: true", saved)

    def test_normalize_alias_accepts_qwen38(self):
        self.assertEqual(normalize_alias("Qwen38"), "qwen38")
        self.assertEqual(normalize_alias("qwen-38.yml"), "qwen-38")
        for bad in ("q", "1qwen", "qwen 38", "qwen_38", "comfy", "qwen", "help"):
            with self.subTest(bad=bad):
                with self.assertRaises(ModelsError):
                    normalize_alias(bad)

    def test_custom_alias_profile_and_rename(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            _llm_folder(paths.models_dir, "Qwen3.8-27B-exl3-4.00bpw")
            alias = maybe_write_hf_profile(
                "Qwen3.8-27B-exl3-4.00bpw",
                repo_id="org/Qwen3.8-27B-exl3",
                revision="4.00bpw",
                alias="qwen38",
                paths=paths,
            )
            self.assertEqual(alias, "qwen38")
            self.assertTrue((paths.profiles_dir / "qwen38.yml").is_file())
            data = library_state(paths, loaded="")
            self.assertEqual(data["llms"][0]["profile"], "qwen38")
            self.assertTrue(data["llms"][0]["local_profile"])
            (paths.profiles_dir / "last.json").write_text(
                json.dumps({"profile": "qwen38"}), encoding="utf-8"
            )
            (paths.profiles_dir / "gpu_mode.json").write_text(
                json.dumps({"mode": "llm", "profile": "qwen38"}), encoding="utf-8"
            )
            (paths.profiles_dir / "switch_times.local.json").write_text(
                json.dumps({"qwen38": {"ready_s": 90}}), encoding="utf-8"
            )
            renamed = set_profile_alias(
                {"folder": "Qwen3.8-27B-exl3-4.00bpw", "alias": "qwen-big"},
                paths=paths,
            )
            self.assertEqual(renamed["alias"], "qwen-big")
            self.assertFalse((paths.profiles_dir / "qwen38.yml").exists())
            self.assertTrue((paths.profiles_dir / "qwen-big.yml").is_file())
            last = json.loads((paths.profiles_dir / "last.json").read_text(encoding="utf-8"))
            gpu = json.loads((paths.profiles_dir / "gpu_mode.json").read_text(encoding="utf-8"))
            times = json.loads(
                (paths.profiles_dir / "switch_times.local.json").read_text(encoding="utf-8")
            )
            self.assertEqual(last["profile"], "qwen-big")
            self.assertEqual(gpu["profile"], "qwen-big")
            self.assertIn("qwen-big", times)
            self.assertNotIn("qwen38", times)
            delete_model(
                {"kind": "llm", "id": "Qwen3.8-27B-exl3-4.00bpw"},
                paths=paths,
                loaded="",
            )
            self.assertFalse((paths.profiles_dir / "qwen-big.yml").exists())

    def test_rename_rejects_shipped_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            _llm_folder(paths.models_dir, "Qwen3.5-9B-exl3-4.00bpw")
            (paths.profiles_dir / "qwen.yml").write_text(
                "pretty: Qwen\nmodel:\n  model_name: Qwen3.5-9B-exl3-4.00bpw\n",
                encoding="utf-8",
            )
            with self.assertRaises(ModelsError):
                set_profile_alias(
                    {"folder": "Qwen3.5-9B-exl3-4.00bpw", "alias": "daily"},
                    paths=paths,
                )


class JobStateTests(unittest.TestCase):
    def setUp(self):
        ui_models._CANCEL.clear()
        ui_models._THREAD = None
        ui_models._ACTIVE_PATHS = None

    def test_busy_job_returns_409(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            write_job({"id": "1", "status": "running", "dests": []}, paths)
            alive = mock.Mock()
            alive.is_alive.return_value = True
            with mock.patch.object(ui_models, "_THREAD", alive):
                with self.assertRaises(ModelsError) as caught:
                    start_download({"kind": "catalog", "pick_id": "flux"}, paths=paths, spawn=False)
            self.assertEqual(caught.exception.status, 409)

    def test_catalog_already_installed_409(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            dest = paths.comfy_dir / "models" / "checkpoints" / "flux1-schnell-fp8.safetensors"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"flux")
            with self.assertRaises(ModelsError) as caught:
                start_download({"kind": "catalog", "pick_id": "flux"}, paths=paths, spawn=False)
            self.assertEqual(caught.exception.status, 409)

    def test_start_hf_job_without_spawn(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            api = FakeApi(sizes={("org/My-exl3", "4.00bpw"): (50, 1)})
            with mock.patch.object(ui_models, "disk_usage_for", return_value=DISK_OK):
                payload = start_download(
                    {
                        "kind": "hf",
                        "repo_id": "org/My-exl3",
                        "revision": "4.00bpw",
                        "size_bytes": 50,
                    },
                    paths=paths,
                    spawn=False,
                    hf_api=api,
                )
            job = payload["job"]
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["folder"], "My-exl3-4.00bpw")
            self.assertIsNone(job["alias"])
            self.assertTrue(job["dests"][0].endswith("My-exl3-4.00bpw"))

    def test_start_hf_job_stores_alias(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            with mock.patch.object(ui_models, "disk_usage_for", return_value=DISK_OK):
                payload = start_download(
                    {
                        "kind": "hf",
                        "repo_id": "org/My-exl3",
                        "revision": "4.00bpw",
                        "size_bytes": 50,
                        "alias": "qwen38",
                    },
                    paths=paths,
                    spawn=False,
                )
            self.assertEqual(payload["job"]["alias"], "qwen38")

    def test_run_hf_job_writes_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            with mock.patch.object(ui_models, "disk_usage_for", return_value=DISK_OK):
                payload = start_download(
                    {
                        "kind": "hf",
                        "repo_id": "org/My-exl3",
                        "revision": "main",
                        "size_bytes": 10,
                    },
                    paths=paths,
                    spawn=False,
                )
            job = payload["job"]

            def fake_download(item, dest, tqdm_class=None):
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "config.json").write_text("{}", encoding="utf-8")
                (dest / "model.safetensors").write_bytes(b"w")

            fm = ui_models.fetch_models_mod()
            ui_models._ACTIVE_PATHS = paths
            with mock.patch.object(fm, "download_item", side_effect=fake_download):
                ui_models._run_job(job["id"])
            finished = ui_models.read_job(paths)
            self.assertEqual(finished["status"], "done")
            self.assertTrue((paths.models_dir / "My-exl3" / "config.json").is_file())
            self.assertTrue((paths.profiles_dir / "hf-my-exl3.yml").is_file())
            self.assertIn(
                "local: true",
                (paths.profiles_dir / "hf-my-exl3.yml").read_text(encoding="utf-8"),
            )

    def test_run_hf_job_uses_custom_alias(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            with mock.patch.object(ui_models, "disk_usage_for", return_value=DISK_OK):
                payload = start_download(
                    {
                        "kind": "hf",
                        "repo_id": "org/My-exl3",
                        "revision": "main",
                        "size_bytes": 10,
                        "alias": "qwen38",
                    },
                    paths=paths,
                    spawn=False,
                )
            job = payload["job"]

            def fake_download(item, dest, tqdm_class=None):
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "config.json").write_text("{}", encoding="utf-8")
                (dest / "model.safetensors").write_bytes(b"w")

            fm = ui_models.fetch_models_mod()
            ui_models._ACTIVE_PATHS = paths
            with mock.patch.object(fm, "download_item", side_effect=fake_download):
                ui_models._run_job(job["id"])
            finished = ui_models.read_job(paths)
            self.assertEqual(finished["status"], "done")
            self.assertEqual(finished["profile"], "qwen38")
            self.assertTrue((paths.profiles_dir / "qwen38.yml").is_file())
            self.assertFalse((paths.profiles_dir / "hf-my-exl3.yml").exists())

    def test_cancel_sets_event(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            write_job({"id": "1", "status": "running"}, paths)
            cancel_download(paths)
            self.assertTrue(ui_models._CANCEL.is_set())
            self.assertEqual(ui_models.read_job(paths)["status"], "cancelling")

    def test_finished_job_drops_off_after_ttl(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 9, 11, 2, 30, tzinfo=timezone.utc)
        fresh = {
            "id": "1",
            "status": "done",
            "message": "Download finished",
            "updated_at": (now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        stale = {
            "id": "2",
            "status": "done",
            "message": "Download finished",
            "updated_at": (now - timedelta(seconds=JOB_DONE_TTL_SEC + 5)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        running = {"id": "3", "status": "running", "updated_at": "2020-01-01T00:00:00Z"}
        self.assertTrue(job_is_visible(fresh, now))
        self.assertFalse(job_is_visible(stale, now))
        self.assertTrue(job_is_visible(running, now))
        self.assertIsNone(public_job(stale, now))
        self.assertEqual(public_job(fresh, now)["id"], "1")
        self.assertIsNone(public_job({"status": "done"}, now))

    def test_library_omits_old_finished_job(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            stale = {
                "id": "old",
                "status": "done",
                "message": "Download finished",
                "updated_at": "2020-01-01T00:00:00Z",
            }
            paths.job_path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
            empty = {
                "llms": [],
                "images": [],
                "catalog": [],
                "disk": DISK_OK,
            }
            with mock.patch.object(ui_models, "library_llms", return_value=[]):
                with mock.patch.object(ui_models, "library_images", return_value=[]):
                    with mock.patch.object(ui_models, "catalog_pick_rows", return_value=[]):
                        data = library_state(paths, loaded="")
                        payload = job_state(paths)
            self.assertIsNone(data["job"])
            self.assertIsNone(payload["job"])
            self.assertEqual(data.get("ok"), True)

    def test_library_keeps_fresh_finished_job(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            write_job({"id": "new", "status": "done", "message": "Download finished"}, paths)
            with mock.patch.object(ui_models, "library_llms", return_value=[]):
                with mock.patch.object(ui_models, "library_images", return_value=[]):
                    with mock.patch.object(ui_models, "catalog_pick_rows", return_value=[]):
                        data = library_state(paths, loaded="")
                        payload = job_state(paths)
            self.assertEqual(data["job"]["status"], "done")
            self.assertEqual(payload["job"]["id"], "new")

    def test_recover_stale_running_job(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            write_job({"id": "1", "status": "running"}, paths)
            job = recover_stale_job(paths)
            self.assertEqual(job["status"], "error")

    def test_delete_refuses_active_download_dest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = _paths(root)
            folder = _llm_folder(paths.models_dir, "Downloading")
            write_job({"id": "1", "status": "running", "dests": [str(folder)]}, paths)
            alive = mock.Mock()
            alive.is_alive.return_value = True
            with mock.patch.object(ui_models, "_THREAD", alive):
                with self.assertRaises(ModelsError) as caught:
                    delete_model({"kind": "llm", "id": "Downloading"}, paths=paths, loaded="")
            self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
