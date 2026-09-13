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
        self.assertFalse(
            phrase_switch.thinking_only_name("gemma", "Gemma 4 12B - general / longer think")
        )
        self.assertFalse(
            phrase_switch.thinking_only_name(
                "dsc67b",
                "lmstudio-community/deepseek-coder-6.7B-kexer-GGUF main",
                "deepseek-coder-6.7B-kexer-GGUF",
            )
        )
        self.assertFalse(
            phrase_switch.thinking_only_name(
                "dsc13", "TheBloke/deepseek-coder-1.3b-instruct-GGUF main"
            )
        )


class ThinkingOnlyProfileTests(unittest.TestCase):
    def test_shipped_glm_flag(self):
        self.assertTrue(phrase_switch.profile_is_thinking_only("glm"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("qwen"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("qwen35"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("qwen36"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("gemma"))
        self.assertFalse(phrase_switch.profile_is_thinking_only("gemma26"))

    def test_glm_folder_does_not_taint_other_profiles(self):
        qwen = {
            "alias": "qwen",
            "folder": "Qwen3.5-9B-exl3-4.00bpw",
            "pretty": "Qwen3.5-9B - coding",
            "thinking_only": None,
            "tool_format": "qwen3_5",
        }
        with (
            mock.patch.object(phrase_switch, "profile_map", return_value={"qwen": qwen}),
            mock.patch.object(
                phrase_switch,
                "current_folder",
                return_value="GLM-4.1V-9B-Thinking-exl3-4.00bpw",
            ),
        ):
            self.assertFalse(phrase_switch.profile_is_thinking_only("qwen"))

    def test_loaded_gguf_is_not_thinking_only_when_last_exl_was_glm(self):
        entry = {
            "alias": "dsc67b",
            "folder": "deepseek-coder-6.7B-kexer-GGUF",
            "pretty": "lmstudio-community/deepseek-coder-6.7B-kexer-GGUF main",
            "thinking_only": None,
            "backend": "llamacpp",
        }
        with (
            mock.patch.object(phrase_switch, "profile_map", return_value={"dsc67b": entry}),
            mock.patch.object(phrase_switch, "llama_up", return_value=True),
            mock.patch.object(phrase_switch, "last_llm_profile_name", return_value="glm"),
            mock.patch("select_model.last_llama_profile", return_value="dsc67b"),
            mock.patch("select_model.last_profile", return_value="dsc67b"),
            mock.patch.object(
                phrase_switch,
                "current_folder",
                return_value="GLM-4.1V-9B-Thinking-exl3-4.00bpw",
            ),
        ):
            self.assertFalse(phrase_switch.profile_is_thinking_only("dsc67b"))
            self.assertFalse(phrase_switch.profile_is_thinking_only())
            self.assertFalse(phrase_switch.profile_writes_code_files("dsc67b"))
            self.assertFalse(phrase_switch.profile_writes_code_files())

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


class ProfileParsesToolsTests(unittest.TestCase):
    def test_coding_profiles_parse_tools(self):
        self.assertTrue(phrase_switch.profile_parses_tools("qwen"))
        self.assertTrue(phrase_switch.profile_parses_tools("qwen35"))
        self.assertTrue(phrase_switch.profile_parses_tools("qwen36"))
        self.assertTrue(phrase_switch.profile_parses_tools("gemma"))
        self.assertTrue(phrase_switch.profile_parses_tools("gemma26"))
        self.assertFalse(phrase_switch.profile_parses_tools("glm"))

    def test_coding_profiles_write_code_files(self):
        self.assertTrue(phrase_switch.profile_writes_code_files("qwen"))
        self.assertTrue(phrase_switch.profile_writes_code_files("gemma"))
        self.assertFalse(phrase_switch.profile_writes_code_files("glm"))

    def test_profile_map_exposes_tool_format(self):
        entry = phrase_switch.profile_map().get("gemma26") or {}
        self.assertEqual(entry.get("tool_format"), "gemma4")


class ThinkingOnlyUiWiringTests(unittest.TestCase):
    def test_status_and_chat_expose_the_alert(self):
        root = Path(__file__).resolve().parents[1]
        manager = (root / "ui" / "manager.py").read_text(encoding="utf-8")
        chat_js = (root / "ui" / "static" / "chat.js").read_text(encoding="utf-8")
        utils_js = (root / "ui" / "static" / "utils.js").read_text(encoding="utf-8")
        chat_py = (root / "ui" / "chat.py").read_text(encoding="utf-8")
        self.assertIn("thinking_only", manager)
        self.assertIn("writes_files", manager)
        self.assertIn("alertThinkingOnlyWrite", utils_js)
        self.assertIn("codeWriteBlockKind", utils_js)
        self.assertIn("Cannot edit files", utils_js)
        self.assertIn("blockThinkingOnlyWrite", chat_js)
        self.assertIn("chat-code-write-hint", chat_js)
        self.assertIn("thinking_only", chat_py)
        self.assertIn("writes_files", chat_py)


if __name__ == "__main__":
    unittest.main()
