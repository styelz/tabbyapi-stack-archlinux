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
        self.assertTrue(phrase_switch.profile_writes_code_files("qwen36"))
        self.assertTrue(phrase_switch.profile_writes_code_files("gemma"))
        self.assertFalse(phrase_switch.profile_writes_code_files("glm"))

    def test_downloaded_qwen_without_yaml_tool_format_still_writes(self):
        entry = {
            "alias": "qwen38",
            "folder": "Qwen3.8-27B-exl3-SC_3.00bpw_H4_V4",
            "pretty": "turboderp/Qwen3.8-27B-exl3 SC_3.00bpw_H4_V4",
            "thinking_only": None,
            "tool_format": None,
            "backend": "",
        }
        mapping = {
            "qwen38": entry,
            "qwen3.8-27b-exl3-sc_3.00bpw_h4_v4": entry,
        }
        with mock.patch.object(phrase_switch, "profile_map", return_value=mapping):
            self.assertEqual(
                phrase_switch.guess_tool_format("qwen38", entry["pretty"], entry["folder"]),
                "qwen3_5",
            )
            self.assertTrue(phrase_switch.profile_parses_tools("qwen38"))
            self.assertTrue(phrase_switch.profile_writes_code_files("qwen38"))

    def test_llama_without_native_tools_still_blocked(self):
        entry = {
            "alias": "dsc67b",
            "folder": "deepseek-coder-6.7B-kexer-GGUF",
            "pretty": "lmstudio-community/deepseek-coder-6.7B-kexer-GGUF main",
            "thinking_only": None,
            "tool_format": None,
            "backend": "llamacpp",
        }
        with (
            mock.patch.object(phrase_switch, "profile_map", return_value={"dsc67b": entry}),
            mock.patch.object(phrase_switch, "llama_up", return_value=True),
            mock.patch.object(phrase_switch, "serving_profile_name", return_value="dsc67b"),
            mock.patch("sidecar.llama_adapter.llama_chat_caps", return_value={"supports_tools": False}),
        ):
            self.assertFalse(phrase_switch.profile_parses_tools("dsc67b"))
            self.assertFalse(phrase_switch.profile_writes_code_files("dsc67b"))

    def test_llama_with_native_tools_can_write(self):
        entry = {
            "alias": "qwen-gguf",
            "folder": "Qwen3-8B-Instruct-GGUF",
            "pretty": "Qwen3 instruct GGUF",
            "thinking_only": None,
            "tool_format": None,
            "backend": "llamacpp",
        }
        with (
            mock.patch.object(phrase_switch, "profile_map", return_value={"qwen-gguf": entry}),
            mock.patch.object(phrase_switch, "llama_up", return_value=True),
            mock.patch.object(phrase_switch, "serving_profile_name", return_value="qwen-gguf"),
            mock.patch("sidecar.llama_adapter.llama_chat_caps", return_value={"supports_tools": True}),
        ):
            self.assertFalse(phrase_switch.profile_parses_tools("qwen-gguf"))
            self.assertTrue(phrase_switch.profile_writes_code_files("qwen-gguf"))

    def test_guess_tool_format_families(self):
        self.assertEqual(phrase_switch.guess_tool_format("gemma26"), "gemma4")
        self.assertEqual(phrase_switch.guess_tool_format("glm45"), "glm4_5")
        self.assertEqual(phrase_switch.guess_tool_format("glm", "GLM-4.1V-9B-Thinking"), "")
        self.assertEqual(phrase_switch.guess_tool_format("dsc67b", "deepseek-coder-kexer"), "")

    def test_load_payload_fills_qwen_tool_format(self):
        from common.load_fields import load_payload

        payload = load_payload("Qwen3.8-27B-exl3-SC_3.00bpw_H4_V4", {"max_seq_len": 32768})
        self.assertEqual(payload["tool_format"], "qwen3_5")
        glm = load_payload("GLM-4.1V-9B-Thinking-exl3-4.00bpw", {"max_seq_len": 65536})
        self.assertNotIn("tool_format", glm)

    def test_profile_map_exposes_tool_format(self):
        entry = phrase_switch.profile_map().get("gemma26") or {}
        self.assertEqual(entry.get("tool_format"), "gemma4")

    def test_profile_map_rereads_only_when_profiles_change(self):
        phrase_switch.reset_profile_map_cache()
        first = phrase_switch.profile_map()
        with mock.patch.object(phrase_switch, "_load_yaml", side_effect=AssertionError("disk")):
            second = phrase_switch.profile_map()
        self.assertIs(first, second)
        phrase_switch.reset_profile_map_cache()
        with mock.patch.object(phrase_switch, "_load_yaml", return_value={"pretty": "cached-miss"}):
            third = phrase_switch.profile_map()
        self.assertIn("cached-miss", {row.get("pretty") for row in third.values()})
        phrase_switch.reset_profile_map_cache()


class VisibleProfileNamesTests(unittest.TestCase):
    def test_keeps_one_local_short_name_per_folder(self):
        mapping = {
            "hf-foo": {
                "alias": "hf-foo",
                "folder": "Qwen3.8-Uncensored",
                "local": True,
                "pretty": "Qwen3.8-27B Uncensored",
            },
            "qwen38g": {
                "alias": "qwen38g",
                "folder": "Qwen3.8-Uncensored",
                "local": True,
                "pretty": "Qwen3.8-27B Uncensored",
            },
            "qwen38guff": {
                "alias": "qwen38guff",
                "folder": "Qwen3.8-Uncensored",
                "local": True,
                "pretty": "Qwen3.8-27B Uncensored",
            },
            "qwen": {
                "alias": "qwen",
                "folder": "Qwen3.5-9B-exl3-4.00bpw",
                "local": False,
                "pretty": "Qwen3.5 9B",
            },
            "qwen38": {
                "alias": "qwen38",
                "folder": "Qwen3.8-27B-exl3",
                "local": True,
                "pretty": "Qwen3.8-27B",
            },
        }
        with mock.patch.object(phrase_switch, "profile_map", return_value=mapping):
            shown = phrase_switch.visible_profile_names(
                ["hf-foo", "qwen", "qwen38", "qwen38g", "qwen38guff"]
            )
        self.assertEqual(shown, ["qwen", "qwen38", "qwen38guff"])

    def test_hides_shipped_alias_when_local_covers_folder(self):
        mapping = {
            "qwen": {
                "alias": "qwen",
                "folder": "Qwen3.5-9B-exl3-4.00bpw",
                "local": False,
                "pretty": "Qwen",
            },
            "daily": {
                "alias": "daily",
                "folder": "Qwen3.5-9B-exl3-4.00bpw",
                "local": True,
                "pretty": "Daily 9B",
            },
        }
        with mock.patch.object(phrase_switch, "profile_map", return_value=mapping):
            shown = phrase_switch.visible_profile_names(["qwen", "daily"])
        self.assertEqual(shown, ["daily"])

    def test_hides_settings_model_sidecar(self):
        mapping = {
            "qwen": {
                "alias": "qwen",
                "folder": "Qwen3.5-9B-exl3-4.00bpw",
                "local": False,
                "pretty": "Qwen",
            },
            "settings_model": {
                "alias": "settings_model",
                "folder": "",
                "local": False,
                "pretty": "settings_model",
            },
        }
        with mock.patch.object(phrase_switch, "profile_map", return_value=mapping):
            shown = phrase_switch.visible_profile_names(["qwen", "settings_model"])
        self.assertEqual(shown, ["qwen"])


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
        self.assertIn("codingFamilyStatus", utils_js)
        self.assertIn("Cannot edit files", utils_js)
        self.assertIn("blockThinkingOnlyWrite", chat_js)
        self.assertIn("chat-code-write-hint", chat_js)
        self.assertIn("thinking_only", chat_py)
        self.assertIn("writes_files", chat_py)


if __name__ == "__main__":
    unittest.main()
