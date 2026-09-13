import json
import unittest

from endpoints.OAI.utils.toolcall_formats.qwen3_coder import parse_toolcalls
from endpoints.OAI.utils.tools import get_toolcall_tags, is_supported_format


class Qwen3CoderParseTests(unittest.TestCase):
    def test_wrapped_complete_call(self):
        text = (
            "<tool_call>\n"
            "<function=read>\n"
            "<parameter=path>\n"
            "/home/user/foo.py\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>"
        )
        calls = parse_toolcalls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "read")
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"path": "/home/user/foo.py"},
        )

    def test_bare_function_without_wrapper(self):
        text = "<function=read>\n<parameter=path>\n/home/user/foo.py\n</parameter>\n</function>"
        calls = parse_toolcalls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "read")
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"path": "/home/user/foo.py"},
        )

    def test_stops_after_function_and_parameter_tags(self):
        calls = parse_toolcalls("<function=read>\n<parameter=path>\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "read")
        self.assertEqual(json.loads(calls[0].function.arguments), {"path": ""})

    def test_unclosed_parameter_keeps_value(self):
        calls = parse_toolcalls("<function=read>\n<parameter=path>\n/home/user/foo.py\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"path": "/home/user/foo.py"},
        )

    def test_two_calls(self):
        text = (
            "<function=read>\n"
            "<parameter=path>\n"
            "/a.py\n"
            "</parameter>\n"
            "</function>\n"
            "<function=grep>\n"
            "<parameter=pattern>\n"
            "foo\n"
            "</parameter>\n"
            "</function>"
        )
        calls = parse_toolcalls(text)
        self.assertEqual([c.function.name for c in calls], ["read", "grep"])
        self.assertEqual(json.loads(calls[1].function.arguments), {"pattern": "foo"})

    def test_qwen3_6_alias_uses_coder_tags(self):
        self.assertTrue(is_supported_format("qwen3_6"))
        starts, ends = get_toolcall_tags("qwen3_6")
        self.assertEqual(starts, ("<tool_call>", "<function="))
        self.assertEqual(ends, ("</tool_call>", "</function>"))
        self.assertEqual(get_toolcall_tags("qwen3_6"), get_toolcall_tags("qwen3_5"))

    def test_unclosed_strreplace_params_stay_separate(self):
        text = (
            "<function=StrReplace>\n"
            "<parameter=path>\n"
            "style.css\n"
            "<parameter=old_string>\n"
            ".hero { color: red; }\n"
            "<parameter=new_string>\n"
            ".hero { color: blue; }\n"
        )
        calls = parse_toolcalls(text)
        self.assertEqual(len(calls), 1)
        args = json.loads(calls[0].function.arguments)
        self.assertEqual(args["path"], "style.css")
        self.assertEqual(args["old_string"], ".hero { color: red; }")
        self.assertEqual(args["new_string"], ".hero { color: blue; }")


class ToolArgumentCoerceTests(unittest.TestCase):
    def test_concatenated_json_keeps_first_object(self):
        from endpoints.OAI.utils.tools import coerce_tool_arguments, first_json_value

        doubled = '{"path":"about.css"}{"path":"about.css"}'
        self.assertEqual(first_json_value(doubled), {"path": "about.css"})
        self.assertEqual(json.loads(coerce_tool_arguments(doubled)), {"path": "about.css"})


if __name__ == "__main__":
    unittest.main()
