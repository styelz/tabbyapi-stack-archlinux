"""Audio/video Comfy catalog, graphs, history fetch, MIME, and chat phrases."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
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
    chat_media_href,
    is_public_generated_png,
    media_disposition,
    media_kind_for_name,
    media_type_for_name,
    parse_wan_size,
    public_generated_href,
    requested_video_seconds,
    strip_media_prefix,
    wan_asked_too_long,
    wants_music,
)
from common.phrase_switch import (
    image_job_done_text,
    looks_like_chat_not_image,
    requested_image_prompt,
    requested_media_prompt,
    video_length_cap_text,
    video_length_followup,
)
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
        self.assertEqual(media_disposition("clip.mp4"), "inline")
        self.assertEqual(media_disposition("hit.wav"), "inline")
        self.assertEqual(media_disposition("shot.png"), "attachment")
        self.assertEqual(public_generated_href("generated-1.mp4"), "/v1/images/generated-1.mp4")
        self.assertEqual(
            chat_media_href("https://git.example.com/openai/v1/images/generated-1.mp4"),
            "/v1/images/generated-1.mp4",
        )

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
        self.assertEqual(sfx["8"]["class_type"], "SaveAudio")
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

    def test_video_duration_ask_stays_video_and_caps(self):
        long_ask = "generate a video of a spaceship flying through space for 20 seconds"
        self.assertEqual(requested_media_prompt(_chat(long_ask))[0], "video")
        self.assertFalse(looks_like_chat_not_image(long_ask))
        self.assertEqual(requested_video_seconds(long_ask), 20)
        self.assertEqual(wan_asked_too_long(long_ask), 20)
        self.assertIsNone(wan_asked_too_long("generate a video of a red bicycle"))
        job = SimpleNamespace(
            modality="video",
            items=[SimpleNamespace(prompt=long_ask)],
            restore=False,
            started_at=0,
        )
        done = image_job_done_text(job=job)
        self.assertIn("Rendered with Wan", done)
        self.assertIn("20-second clip is not available", done)
        self.assertIn("about 3 seconds", done)

    def test_video_length_followup_is_chat_not_flux(self):
        follow = "the video is only 3 seconds, i asked for a 20 seconds video"
        self.assertTrue(video_length_followup(follow))
        self.assertTrue(looks_like_chat_not_image(follow))
        self.assertIsNone(requested_media_prompt(_chat(follow)))
        self.assertIsNone(requested_image_prompt(_chat(follow)))
        reply = video_length_cap_text(follow)
        self.assertIn("20-second clip is not available", reply)
        self.assertIn("about 3 seconds", reply)
        self.assertFalse(video_length_followup("generate a video of a spaceship for 20 seconds"))

    def test_audio_save_prefers_simple_node(self):
        from common.gpu_mode import _apply_audio_save_node

        graph = build_audio_prompt("a door slam", music=False)
        with mock.patch(
            "common.gpu_mode.comfy_missing_nodes",
            return_value=[],
        ):
            applied = _apply_audio_save_node(graph)
        self.assertEqual(applied["8"]["class_type"], "SaveAudio")
        self.assertEqual(set(applied["8"]["inputs"]), {"audio", "filename_prefix"})

    def test_audio_save_fallback_when_simple_is_missing(self):
        from common.gpu_mode import _apply_audio_save_node

        graph = build_audio_prompt("a door slam", music=False)
        with mock.patch(
            "common.gpu_mode.comfy_missing_nodes",
            return_value=["SaveAudio"],
        ):
            swapped = _apply_audio_save_node(graph)
        self.assertEqual(swapped["8"]["class_type"], "SaveAudioAdvanced")
        self.assertEqual(swapped["8"]["inputs"]["format"], {"format": "flac"})

    def test_video_save_keeps_sibling_format_keys(self):
        from common.gpu_mode import _apply_video_save_node

        graph = build_wan_prompt("a lantern in fog")
        applied = _apply_video_save_node(graph)
        self.assertEqual(applied["11"]["inputs"]["format"], "mp4")
        self.assertEqual(applied["11"]["inputs"]["codec"], "h264")

    def test_generation_routes_exist(self):
        from endpoints.core.router import router

        paths = {getattr(route, "path", None) for route in router.routes}
        self.assertIn("/v1/audio/generations", paths)
        self.assertIn("/v1/videos/generations", paths)

    def test_persist_job_reply_skips_console_video_already_in_chat(self):
        from images.jobs import _persist_job_reply

        job = SimpleNamespace(
            id="job-1",
            status="done",
            owner="pbp",
            chat_id="c1",
            urls=["https://git.example.com/openai/v1/images/generated-20260919-042559-1.mp4"],
            modality="video",
        )
        store = {
            "chats": [
                {
                    "id": "c1",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": (
                                "Here's the video.\n\n"
                                "![](/v1/images/generated-20260919-042559-1.mp4)"
                            ),
                        }
                    ],
                }
            ]
        }
        with (
            mock.patch("ui.chats.load_store", return_value=store),
            mock.patch("ui.chats.append_flight_assistant") as append,
        ):
            _persist_job_reply(job)
        append.assert_not_called()

    def test_persist_job_reply_uses_video_lead_and_relative_href(self):
        from images.jobs import _persist_job_reply

        job = SimpleNamespace(
            id="job-2",
            status="done",
            owner="pbp",
            chat_id="c1",
            urls=["https://git.example.com/openai/v1/images/generated-20260919-042559-1.mp4"],
            modality="video",
        )
        store = {"chats": [{"id": "c1", "messages": []}]}
        with (
            mock.patch("ui.chats.load_store", return_value=store),
            mock.patch("ui.chats.append_flight_assistant") as append,
        ):
            _persist_job_reply(job)
        append.assert_called_once()
        text = append.call_args.kwargs["content"]
        self.assertIn("Here's the video.", text)
        self.assertIn("![](/v1/images/generated-20260919-042559-1.mp4)", text)
        self.assertNotIn("Here's the picture.", text)


if __name__ == "__main__":
    unittest.main()
