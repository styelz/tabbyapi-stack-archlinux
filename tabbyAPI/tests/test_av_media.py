"""Audio/video Comfy catalog, graphs, history fetch, MIME, and chat phrases."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from common.gpu_mode import (
    STABLE_AUDIO_CLIP,
    STABLE_AUDIO_MUSIC,
    STABLE_AUDIO_SFX,
    WAN_CLIP,
    WAN_UNET,
    WAN_VAE,
    _first_media_ref,
    build_audio_prompt,
    build_wan_prompt,
    is_public_generated_png,
    media_kind_for_name,
    media_type_for_name,
    parse_wan_size,
    strip_media_prefix,
    wants_music,
)
from common.phrase_switch import looks_like_chat_not_image, requested_media_prompt
from endpoints.OAI.types.chat_completion import ChatCompletionMessage, ChatCompletionRequest
from images.jobs import _new_items


ROOT = Path(__file__).resolve().parents[1]


def _chat(text: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[ChatCompletionMessage(role="user", content=text)]
    )


class AvMediaTests(unittest.TestCase):
    def test_catalog_kinds_and_not_core(self):
        catalog = json.loads((ROOT / "deploy" / "arch" / "models.json").read_text())
        self.assertNotIn("stable-audio", catalog["sets"]["core"])
        self.assertNotIn("wan", catalog["sets"]["core"])
        self.assertIn("stable-audio-sfx", catalog["sets"]["all"])
        self.assertIn("wan-unet", catalog["sets"]["all"])
        picks = {pick["id"]: pick for pick in catalog["picks"]}
        self.assertEqual(picks["stable-audio"]["kind"], "audio")
        self.assertEqual(picks["wan"]["kind"], "video")

    def test_mime_and_public_suffixes(self):
        self.assertEqual(media_type_for_name("generated-1.png"), "image/png")
        self.assertEqual(media_type_for_name("generated-1.wav"), "audio/wav")
        self.assertEqual(media_type_for_name("generated-1.mp4"), "video/mp4")
        self.assertEqual(media_kind_for_name("clip.mp4"), "video")
        self.assertEqual(media_kind_for_name("hit.wav"), "audio")
        self.assertTrue(is_public_generated_png("generated-20260919-031700-1.wav"))
        self.assertTrue(is_public_generated_png("generated-20260919-031700-1.mp4"))
        self.assertFalse(is_public_generated_png("generated-latest.png"))

    def test_history_prefers_audio_and_video_keys(self):
        audio = _first_media_ref(
            {"outputs": {"8": {"audio": [{"filename": "out.wav", "type": "output"}]}}}
        )
        self.assertEqual(audio["filename"], "out.wav")
        video = _first_media_ref(
            {"outputs": {"11": {"videos": [{"filename": "out.mp4", "type": "output"}]}}}
        )
        self.assertEqual(video["filename"], "out.mp4")
        still = _first_media_ref(
            {"outputs": {"8": {"images": [{"filename": "out.png", "type": "output"}]}}}
        )
        self.assertEqual(still["filename"], "out.png")

    def test_audio_and_wan_builders(self):
        sfx = build_audio_prompt("sfx: rain on a tin roof", music=False, seconds=10, seed=3)
        self.assertEqual(sfx["1"]["inputs"]["ckpt_name"], STABLE_AUDIO_SFX)
        self.assertEqual(sfx["2"]["inputs"]["clip_name"], STABLE_AUDIO_CLIP)
        self.assertEqual(sfx["3"]["inputs"]["text"], "rain on a tin roof")
        self.assertEqual(sfx["5"]["inputs"]["seconds"], 10)
        self.assertEqual(sfx["8"]["class_type"], "SaveAudioAdvanced")
        music = build_audio_prompt("music: lo-fi beat", music=True, seconds=30, seed=1)
        self.assertEqual(music["1"]["inputs"]["ckpt_name"], STABLE_AUDIO_MUSIC)
        t2v = build_wan_prompt("a red bicycle rolling downhill", width=640, height=640, seed=2)
        self.assertEqual(t2v["1"]["inputs"]["unet_name"], WAN_UNET)
        self.assertEqual(t2v["2"]["inputs"]["clip_name"], WAN_CLIP)
        self.assertEqual(t2v["3"]["inputs"]["vae_name"], WAN_VAE)
        self.assertEqual(t2v["6"]["inputs"]["length"], 81)
        self.assertEqual(t2v["10"]["class_type"], "CreateVideo")
        self.assertEqual(t2v["11"]["class_type"], "SaveVideo")
        self.assertNotIn("12", t2v)
        i2v = build_wan_prompt("gentle camera pan", source_image="still.png", length=81)
        self.assertEqual(i2v["12"]["class_type"], "LoadImage")
        self.assertEqual(i2v["12"]["inputs"]["image"], "still.png")
        self.assertEqual(i2v["6"]["inputs"]["start_image"], ["12", 0])

    def test_wan_size_stays_on_12gb(self):
        width, height = parse_wan_size("1920x1080")
        self.assertLessEqual(width, 768)
        self.assertLessEqual(height, 768)
        self.assertEqual(width % 32, 0)

    def test_job_dests_keep_media_suffixes(self):
        audio = _new_items(items=[{"prompt": "rain on a roof", "modality": "audio"}])
        self.assertEqual(audio[0].modality, "audio")
        self.assertEqual(audio[0].output_path, "audio/generated.wav")
        video = _new_items(items=[{"prompt": "a bicycle rolling", "modality": "video"}])
        self.assertEqual(video[0].modality, "video")
        self.assertEqual(video[0].output_path, "videos/generated.mp4")
        self.assertEqual(video[0].size, "640x640")

    def test_media_phrases(self):
        self.assertEqual(
            requested_media_prompt(_chat("sfx: rain on a tin roof")),
            ("audio", "rain on a tin roof"),
        )
        self.assertEqual(
            requested_media_prompt(_chat("generate audio of a door slam")),
            ("audio", "a door slam"),
        )
        self.assertEqual(
            requested_media_prompt(_chat("music: lo-fi beat with warm piano"))[0],
            "music",
        )
        self.assertEqual(
            requested_media_prompt(_chat("generate a video of a red bicycle"))[0],
            "video",
        )
        self.assertEqual(
            requested_media_prompt(_chat("animate this"))[0],
            "i2v",
        )
        self.assertIsNone(requested_media_prompt(_chat("generate an image of a cube")))
        self.assertFalse(looks_like_chat_not_image("sfx: rain"))
        self.assertFalse(looks_like_chat_not_image("wan: a lantern in fog"))
        self.assertTrue(wants_music("music: a short piano loop"))
        self.assertFalse(wants_music("sfx: a thunder crack"))
        self.assertEqual(strip_media_prefix("video: cobblestone street"), "cobblestone street")

    def test_audio_save_fallback_when_advanced_is_missing(self):
        from common.gpu_mode import _apply_audio_save_node

        graph = build_audio_prompt("a door slam", music=False)
        with mock.patch(
            "common.gpu_mode.comfy_missing_nodes",
            return_value=["SaveAudioAdvanced"],
        ):
            swapped = _apply_audio_save_node(graph)
        self.assertEqual(swapped["8"]["class_type"], "SaveAudio")
        self.assertIn("filename_prefix", swapped["8"]["inputs"])

    def test_generation_routes_exist(self):
        from endpoints.core.router import router

        paths = {getattr(route, "path", None) for route in router.routes}
        self.assertIn("/v1/audio/generations", paths)
        self.assertIn("/v1/videos/generations", paths)


if __name__ == "__main__":
    unittest.main()
