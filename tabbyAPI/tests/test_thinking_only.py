import unittest
from pathlib import Path
from unittest import mock

from common import phrase_switch


class ThinkingOnlyNameTests(unittest.TestCase):
    def test_glm41_folder_is_thinking_only(self):
        self.assertTrue(
            phrase_switch.thinking_only_name("GLM-4.1V-9B-Thinking-exl3-6.00bpw")
        )
        self.assertTrue(phrase_switch.thinking_only_name("glm41", "turboderp/GLM-4.1V"))
        self.assertTrue(phrase_switch.thinking_only_name("Thinking chat only (no coding tools)"))

    def test_coding_profiles_are_not_thinking_only(self):
        self.assertFalse(phrase_switch.thinking_only_name("Qwen3.5-9B-exl3-4.00bpw"))
        self.assertFalse(phrase_switch.thinking_only_name("qwen"))
        self.assertFalse(phrase_switch.thinking_only_name("gemma26"))
        self.assertFalse(phrase_switch.thinking_only_name("glm45"))


class ThinkingOnlyProfileTests(unittest.TestCase):
    def test_shipped_glm_flag(self):
        self.assertTrue(phrase_switch.profile_is_thinking_only("glm"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("qwen"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("gemma"))

    def test_explicit_false_overrides_name(self):
        entry = {
            "alias": "glm41",
            "folder": "GLM-4.1V-9B-Thinking-exl3-6.00bpw",
            "pretty": "GLM thinking",
            "thinking_only": False,
        }
        with mock.patch.object(phrase_switch, "profile_map", return_value={"glm41": entry}):
            self.assertFalse(phrase_switch.profile_is_thinking_only("glm41"))

    def test_download_without_flag_uses_folder_name(self):
        entry = {
            "alias": "glm41",
            "folder": "GLM-4.1V-9B-Thinking-exl3-6.00bpw",
            "pretty": "turboderp/GLM-4.1V-9B-Thinking-exl3",
            "thinking_only": None,
        }
        with mock.patch.object(phrase_switch, "profile_map", return_value={"glm41": entry}):
            self.assertTrue(phrase_switch.profile_is_thinking_only("glm41"))


class ThinkingOnlyUiWiringTests(unittest.TestCase):
    def test_status_and_chat_expose_the_alert(self):
        root = Path(__file__).resolve().parents[1]
        manager = (root / "ui" / "manager.py").read_text(encoding="utf-8")
        chat_js = (root / "ui" / "static" / "chat.js").read_text(encoding="utf-8")
        utils_js = (root / "ui" / "static" / "utils.js").read_text(encoding="utf-8")
        chat_py = (root / "ui" / "chat.py").read_text(encoding="utf-8")
        self.assertIn("thinking_only", manager)
        self.assertIn("alertThinkingOnlyWrite", utils_js)
        self.assertIn("blockThinkingOnlyWrite", chat_js)
        self.assertIn("thinking_only", chat_py)


if __name__ == "__main__":
    unittest.main()
