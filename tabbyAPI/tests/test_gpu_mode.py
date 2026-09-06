"""Exclusive GPU / Comfy helpers. Mixed-chat hold tests live in test_images_chat."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from common.gpu_mode import (
    GPU_ALIASES,
    begin_image_turn,
    build_img2img_prompt,
    build_prompt,
    build_qwen_image_prompt,
    comfy_paths,
    comfy_user_unit_path,
    delete_generated_images,
    format_comfy_journal_line,
    gallery_page,
    list_generated_files,
    nvidia_lib_dirs,
    parse_size,
    public_image_url,
    qwen_image_prompt_text,
    recent_generated_files,
    should_skip_startup_load,
    strip_png_text,
    turn_images_ready,
    wants_qwen_image,
)
from common.ssh_forwarder import ensure_ssh_forwarder, ssh_command, ssh_forward
from switch_model import resolve_name


def temp_generated_dir(names: list[str]):
    from common import gpu_mode as gm

    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name)
    now = time.time()
    for offset, name in enumerate(names):
        path = folder / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        os.utime(path, (now - offset, now - offset))
    turn = folder / "turn.json"
    turn.write_text(json.dumps({"started": now - 3600, "prompt": "seed"}), encoding="utf-8")
    patch_dir = mock.patch.object(gm, "GENERATED_DIR", folder)
    patch_turn = mock.patch.object(gm, "TURN_PATH", turn)
    patch_dir.start()
    patch_turn.start()

    class _Guard:
        def __enter__(self):
            return folder

        def __exit__(self, *exc):
            patch_turn.stop()
            patch_dir.stop()
            tmp.cleanup()

    return _Guard()


class GpuModeTests(unittest.TestCase):
    def test_skip_startup_load_follows_gpu_mode(self):
        from common import gpu_mode as gm

        with tempfile.TemporaryDirectory() as raw:
            status = Path(raw) / "gpu_mode.json"
            with mock.patch.object(gm, "STATUS_PATH", status):
                self.assertFalse(should_skip_startup_load())
                status.write_text('{"mode": "comfy"}\n', encoding="utf-8")
                self.assertTrue(should_skip_startup_load())
                status.write_text('{"mode": "llm", "profile": "qwen"}\n', encoding="utf-8")
                self.assertFalse(should_skip_startup_load())

    def test_aliases_map_to_comfy(self):
        for alias in ("comfy", "flux", "image", "comfyui"):
            self.assertEqual(GPU_ALIASES[alias], "comfy")

    def test_resolve_comfy_aliases(self):
        self.assertEqual(resolve_name("comfy"), "comfy")
        self.assertEqual(resolve_name("FLUX"), "comfy")
        self.assertEqual(resolve_name("image"), "comfy")

    def test_comfy_user_unit_path_uses_xdg(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        try:
            os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-test"
            self.assertEqual(
                comfy_user_unit_path(),
                Path("/tmp/xdg-test/systemd/user/comfyui.service"),
            )
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    def test_format_comfy_journal_line_prefixes_once(self):
        self.assertEqual(
            format_comfy_journal_line("Starting server\n"),
            "[comfy] Starting server",
        )
        self.assertEqual(
            format_comfy_journal_line("[comfy] Starting server"),
            "[comfy] Starting server",
        )

    def test_nvidia_lib_dirs_finds_nested_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = (
                Path(tmp)
                / "venv"
                / "lib"
                / "python3.12"
                / "site-packages"
                / "nvidia"
                / "cu13"
                / "lib"
            )
            lib.mkdir(parents=True)
            (lib / "libcudart.so.13").write_text("", encoding="utf-8")
            self.assertEqual(nvidia_lib_dirs(Path(tmp)), [str(lib)])

    def test_comfy_paths_linux_and_windows(self):
        root, python = comfy_paths(Path("/data/ComfyUI"), windows=False)
        self.assertEqual(root, Path("/data/ComfyUI"))
        self.assertEqual(python, Path("/data/ComfyUI/venv/bin/python"))
        root, python = comfy_paths(Path(r"D:\tabbyapi-stack\ComfyUI"), windows=True)
        self.assertEqual(python.name, "python.exe")
        self.assertIn("Scripts", python.parts)

    def test_ssh_command_uses_key_and_forward(self):
        cmd = ssh_command(Path("/tmp/id_ed25519"), remote="user@host.example")
        self.assertIn(ssh_forward(), cmd)
        self.assertIn("user@host.example", cmd)
        self.assertIn("-R", cmd)

    def test_ensure_ssh_forwarder_skips_without_remote(self):
        old = os.environ.pop("TABBY_SSH_REMOTE", None)
        try:
            self.assertFalse(ensure_ssh_forwarder())
        finally:
            if old is not None:
                os.environ["TABBY_SSH_REMOTE"] = old

    def test_parse_size(self):
        self.assertEqual(parse_size("1024x1024"), (1024, 1024))
        self.assertEqual(parse_size("768x512"), (768, 512))
        self.assertEqual(parse_size("1025x1025"), (1024, 1024))

    def test_parse_size_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            parse_size("64x64")
        with self.assertRaises(ValueError):
            parse_size("wide")

    def test_strip_png_text_drops_workflow_chunk(self):
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            body = kind + data
            return (
                struct.pack(">I", len(data))
                + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\x00\x00\x00")
        raw = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"tEXt", b"prompt\x00hello workflow json")
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b"")
        )
        cleaned = strip_png_text(raw)
        self.assertTrue(cleaned.startswith(b"\x89PNG"))
        self.assertNotIn(b"hello workflow json", cleaned)
        self.assertIn(b"IDAT", cleaned)

    def test_public_image_url(self):
        from common.gpu_mode import public_api_base

        remote = public_image_url(
            "generated-latest.png", bust=False, api_base="http://gpu.example:5000/v1"
        )
        self.assertEqual(remote, "http://gpu.example:5000/v1/images/generated-latest.png")

        class _Req:
            headers = {"host": "192.168.1.20:5000", "x-forwarded-proto": "http"}
            url = None

        self.assertEqual(public_api_base(_Req()), "http://192.168.1.20:5000/v1")

    def test_recent_generated_files_skips_latest_alias(self):
        with temp_generated_dir(["generated-20260101-000001.png", "generated-latest.png"]):
            names = [path.name for path in recent_generated_files(window_sec=86400)]
        self.assertNotIn("generated-latest.png", names)
        self.assertEqual(names, ["generated-20260101-000001.png"])

    def test_gallery_page_clamps_and_slices(self):
        items = [Path(f"generated-{i}.png") for i in range(50)]
        shown, page, pages, per_page = gallery_page(items, page=2, per_page=24)
        self.assertEqual(per_page, 24)
        self.assertEqual(pages, 3)
        self.assertEqual(page, 2)
        self.assertEqual(len(shown), 24)

    def test_delete_generated_images_selected_and_all(self):
        names = [
            "generated-20260102-000002.png",
            "generated-20260101-000001.png",
            "generated-latest.png",
        ]
        with temp_generated_dir(names) as folder:
            thumbs = folder / "thumbs"
            thumbs.mkdir()
            (thumbs / "generated-20260102-000002.jpg").write_bytes(b"jpg")
            (folder / "turn.json").write_text("keep", encoding="utf-8")
            removed = delete_generated_images(["generated-20260102-000002.png"])
            self.assertEqual(removed, ["generated-20260102-000002.png"])
            gone = delete_generated_images(delete_all=True)
            self.assertIn("generated-20260101-000001.png", gone)
            self.assertTrue((folder / "turn.json").exists())

    def test_list_generated_files_newest_first(self):
        ordered = [
            "generated-20260102-000002.png",
            "generated-20260101-000001.png",
            "generated-latest.png",
        ]
        with temp_generated_dir(ordered):
            names = [path.name for path in list_generated_files()]
            self.assertNotIn("generated-latest.png", names)
            self.assertEqual(names, ordered[:2])

    def test_img2img_graph_uses_load_image_and_denoise(self):
        graph = build_img2img_prompt("cartoon style", "photo.png")
        self.assertEqual(graph["9"]["class_type"], "LoadImage")
        self.assertEqual(graph["6"]["inputs"]["denoise"], 0.75)
        stronger = build_img2img_prompt("cartoon style", "photo.png", denoise=0.85)
        self.assertEqual(stronger["6"]["inputs"]["denoise"], 0.85)

    def test_wants_qwen_image_for_text_and_prefix(self):
        self.assertTrue(wants_qwen_image("qwen-image: login form with Submit"))
        self.assertTrue(wants_qwen_image("a poster with the heading SALE"))
        self.assertFalse(wants_qwen_image("a fox asleep under maple trees"))
        self.assertFalse(wants_qwen_image("modern website hero banner, purple UI"))
        self.assertEqual(
            qwen_image_prompt_text("qwen-image: login form with Submit"),
            "login form with Submit",
        )

    def test_qwen_image_graph_uses_gguf_not_flux(self):
        graph = build_qwen_image_prompt("qwen-image: a logo that says Tabby")
        self.assertEqual(graph["1"]["class_type"], "UnetLoaderGGUF")
        self.assertEqual(graph["4"]["inputs"]["text"], "a logo that says Tabby")

    def test_txt2img_still_uses_empty_latent(self):
        graph = build_prompt("a fox asleep under maple trees")
        self.assertEqual(graph["5"]["class_type"], "EmptySD3LatentImage")
        self.assertEqual(graph["6"]["inputs"]["denoise"], 1.0)

    def test_new_prompt_starts_a_new_turn(self):
        from common.gpu_mode import _read_turn

        with temp_generated_dir([]):
            first = begin_image_turn("a red bicycle", force_new=True)
            same = begin_image_turn("a red bicycle", force_new=False)
            self.assertEqual(first, same)
            other = begin_image_turn("a blue cat", force_new=True)
            self.assertEqual(_read_turn().get("prompt"), "a blue cat")
            self.assertEqual(turn_images_ready("missing prompt", 5), [])


if __name__ == "__main__":
    unittest.main()
