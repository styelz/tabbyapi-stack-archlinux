import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import select_model


QWEN_YML = """pretty: Qwen 9B
model:
  model_name: Qwen3.5-9B-exl3-4.00bpw
"""
QWEN35_YML = """pretty: Qwen 35B
model:
  model_name: Qwen3.5-35B-A3B-exl3-2.13bpw
"""


def _ready(root: Path, *folders: str) -> Path:
    models = root / "models"
    for name in folders:
        snap = models / name
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")
    return models


class ReadyFolderTests(unittest.TestCase):
    def test_skips_embedding_and_uses_only_llm_on_disk(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            models = _ready(
                root,
                "Qwen3-Embedding-0.6B",
                "Qwen3.5-35B-A3B-exl3-2.13bpw",
            )
            self.assertEqual(
                select_model.ready_llm_folders(models),
                ["Qwen3.5-35B-A3B-exl3-2.13bpw"],
            )
            self.assertEqual(
                select_model.first_ready_folder(models_dir=models, catalog={}),
                "Qwen3.5-35B-A3B-exl3-2.13bpw",
            )

    def test_does_not_prefer_qwen_when_several_exist(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            models = _ready(
                root,
                "Qwen3.5-9B-exl3-4.00bpw",
                "Qwen3.5-35B-A3B-exl3-2.13bpw",
            )
            self.assertIsNone(
                select_model.first_ready_folder(models_dir=models, catalog={})
            )

    def test_uses_selected_ids_not_catalog_favorites(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profiles = root / "model_profiles"
            profiles.mkdir()
            (profiles / "qwen.yml").write_text(QWEN_YML, encoding="utf-8")
            (profiles / "qwen35.yml").write_text(QWEN35_YML, encoding="utf-8")
            models = _ready(
                root,
                "Qwen3.5-9B-exl3-4.00bpw",
                "Qwen3.5-35B-A3B-exl3-2.13bpw",
            )
            self.assertEqual(
                select_model.first_ready_folder(
                    ids="qwen35,embed,flux",
                    models_dir=models,
                    profiles_dir=profiles,
                    catalog={},
                ),
                "Qwen3.5-35B-A3B-exl3-2.13bpw",
            )

    def test_uses_arbitrary_local_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            models = _ready(root, "SomeVendor-Custom-exl3")
            self.assertEqual(
                select_model.first_ready_folder(
                    ids="local-SomeVendor-Custom-exl3",
                    models_dir=models,
                    catalog={},
                ),
                "SomeVendor-Custom-exl3",
            )
            self.assertEqual(
                select_model.first_ready_folder(models_dir=models, catalog={}),
                "SomeVendor-Custom-exl3",
            )

    def test_seed_rewrites_missing_configured_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profiles = root / "model_profiles"
            profiles.mkdir()
            (profiles / "qwen.yml").write_text(QWEN_YML, encoding="utf-8")
            (profiles / "qwen35.yml").write_text(QWEN35_YML, encoding="utf-8")
            _ready(root, "Qwen3.5-35B-A3B-exl3-2.13bpw")
            applied = []

            def fake_apply(name):
                applied.append(name)
                return {}

            with (
                mock.patch.object(select_model, "ROOT", root),
                mock.patch.object(select_model, "PROFILES_DIR", profiles),
                mock.patch.object(select_model, "LAST_PATH", profiles / "last.json"),
                mock.patch.object(select_model, "CATALOG_PATH", root / "missing.json"),
                mock.patch.object(select_model, "apply_profile", fake_apply),
            ):
                name = select_model.seed_install_profile(
                    configured_folder="Qwen3.5-9B-exl3-4.00bpw",
                    ids="qwen35",
                )
            self.assertEqual(name, "qwen35")
            self.assertEqual(applied, ["qwen35"])
            gpu = json.loads((profiles / "gpu_mode.json").read_text(encoding="utf-8"))
            self.assertEqual(gpu["profile"], "qwen35")

    def test_apply_profile_ignores_local_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profiles = root / "model_profiles"
            models = root / "models" / "Custom-exl3"
            profiles.mkdir()
            models.mkdir(parents=True)
            (models / "config.json").write_text("{}", encoding="utf-8")
            (profiles / "qwen38.yml").write_text(
                "pretty: Custom\nlocal: true\nmodel:\n  model_name: Custom-exl3\n",
                encoding="utf-8",
            )
            config = root / "config.yml"
            config.write_text("model:\n  model_name: other\n", encoding="utf-8")
            with (
                mock.patch.object(select_model, "ROOT", root),
                mock.patch.object(select_model, "PROFILES_DIR", profiles),
                mock.patch.object(select_model, "CONFIG_PATH", config),
                mock.patch.object(select_model, "LAST_PATH", profiles / "last.json"),
            ):
                profile = select_model.apply_profile("qwen38")
                aliases = select_model.profile_aliases()
            self.assertNotIn("local", profile)
            saved = config.read_text(encoding="utf-8")
            self.assertIn("Custom-exl3", saved)
            self.assertNotIn("local:", saved)
            last = json.loads((profiles / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(last["profile"], "qwen38")
            self.assertEqual(aliases["qwen38"], "qwen38")
            self.assertEqual(aliases["custom-exl3"], "qwen38")


class GgufProfileTests(unittest.TestCase):
    def test_folder_ready_with_gguf_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            models = root / "models"
            folder = models / "Some-20B"
            folder.mkdir(parents=True)
            (folder / "weights.gguf").write_bytes(b"gguf")
            with mock.patch.object(select_model, "ROOT", root):
                self.assertTrue(select_model.model_folder_ready("Some-20B", models_dir=models))
                self.assertEqual(
                    select_model.resolve_gguf_path("Some-20B", models_dir=models).name,
                    "weights.gguf",
                )

    def test_profile_backend_and_apply_skips_config_yml(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profiles = root / "model_profiles"
            models = root / "models" / "Some-20B"
            profiles.mkdir()
            models.mkdir(parents=True)
            (models / "weights.gguf").write_bytes(b"gguf")
            (profiles / "biggguf.yml").write_text(
                "pretty: Some 20B\nmodel:\n  backend: llamacpp\n  model_name: Some-20B\n  n_gpu_layers: -1\n",
                encoding="utf-8",
            )
            (profiles / "qwen.yml").write_text(
                "pretty: Qwen\nmodel:\n  model_name: Qwen3.5-9B-exl3-4.00bpw\n",
                encoding="utf-8",
            )
            config = root / "config.yml"
            config.write_text("model:\n  model_name: other\n", encoding="utf-8")
            with (
                mock.patch.object(select_model, "ROOT", root),
                mock.patch.object(select_model, "PROFILES_DIR", profiles),
                mock.patch.object(select_model, "CONFIG_PATH", config),
                mock.patch.object(select_model, "LAST_PATH", profiles / "last.json"),
            ):
                self.assertEqual(select_model.profile_backend("biggguf"), "llamacpp")
                self.assertEqual(select_model.profile_backend("qwen"), "exllamav3")
                select_model.apply_profile("biggguf")
                last = json.loads((profiles / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(last["profile"], "biggguf")
            self.assertEqual(last["llama"], "biggguf")
            self.assertNotIn("Some-20B", config.read_text(encoding="utf-8"))


class InstallShSeedTests(unittest.TestCase):
    def test_install_seeds_from_model_set_not_qwen(self):
        src = Path(__file__).resolve().parents[2] / "install.sh"
        text = src.read_text(encoding="utf-8")
        self.assertIn('--seed-installed --ids "$MODEL_SET"', text)
        self.assertNotIn('DEFAULT_MODEL="Qwen3.5-9B-exl3-4.00bpw"', text)
        self.assertNotIn('{"profile": "qwen"}', text)
        fetch_at = text.find('"$DEST_FETCH" "${FETCH_ARGS[@]}"')
        seed_at = text.find('select_model.py" \\\n  --seed-installed --ids "$MODEL_SET"')
        self.assertGreater(fetch_at, 0)
        self.assertGreater(seed_at, fetch_at)
