"""Blind image_url parts must stay visible when vision is off."""

import unittest

from endpoints.OAI.utils.chat_completion import (
    VISION_OFF_IMAGE_NOTE,
    flatten_message_text,
)


class FlattenMessageTextTests(unittest.TestCase):
    def test_vision_off_keeps_one_note(self):
        text = flatten_message_text(
            [
                {"type": "text", "text": "see the problem?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,yy"}},
            ],
            use_vision=False,
        )
        self.assertTrue(text.startswith("see the problem?"))
        self.assertEqual(text.count(VISION_OFF_IMAGE_NOTE), 1)
        self.assertNotIn("data:image", text)
        self.assertIn("switch to qwen", text)

    def test_vision_on_uses_aliases(self):
        text = flatten_message_text(
            [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
            ],
            use_vision=True,
            image_aliases=["<image>"],
        )
        self.assertEqual(text, "what is this?<image>")
        self.assertNotIn(VISION_OFF_IMAGE_NOTE, text)


if __name__ == "__main__":
    unittest.main()
