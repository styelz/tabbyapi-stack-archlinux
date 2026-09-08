import tempfile
import unittest
from pathlib import Path

from calibrate import (
    DEPLOY_WAIT_RE,
    SWITCH_BLOCK_RE,
    agents_switch_block,
    deploy_wait_line,
    rewrite_agents_md,
)

SAMPLE = {
    "gpu": "Test GPU 8 GB",
    "qwen": {"ready_s": 12},
    "qwen35": {"ready_s": 118},
    "qwen36": {"ready_s": 95},
    "gemma": {"ready_s": 20},
    "gemma26": {"ready_s": 99},
    "glm": {"ready_s": 13},
    "comfy": {"ready_s": 6, "flux_s": 40, "qwen_image_s": 80},
    "llm": {"ready_s": 55},
}

PROFILES = {
    "qwen": {"seq": 262144, "vision": True},
    "qwen35": {"seq": 131072, "vision": True},
    "glm": {"seq": 65536, "vision": False},
}

AGENTS_STUB = """# notes

## Switch models

old table here

The GPU is exclusive: LLM or Comfy.

## Next
"""


class CalibrateDocsTests(unittest.TestCase):
    def test_switch_block_uses_measured_times(self):
        block = agents_switch_block(SAMPLE, "Test GPU 8 GB", PROFILES)
        self.assertIn("warm switches on this Test GPU 8 GB", block)
        self.assertIn("switch_times.json", block)
        self.assertNotIn("calibrate.py", block)
        self.assertIn("| `switch to qwen` | Daily coding, 9B | 262k | ~10 seconds |", block)
        self.assertIn("| `switch to qwen35` | Long or hard agent work | 131k | ~2 minutes |", block)
        self.assertIn("Thinking chat only (no coding tools; vision off on Test GPU 8 GB)", block)
        self.assertIn("65k (model max)", block)
        self.assertIn("Flux ~40 seconds", block)
        self.assertIn("~55 seconds", block)
        self.assertIn("| `restart` | Bounce the API; last model reloads | — | ~55 seconds |", block)
        self.assertNotIn("switch to gemma", block)

    def test_rewrite_agents_preserves_following_section(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "AGENTS.md"
            path.write_text(AGENTS_STUB, encoding="utf-8")
            self.assertTrue(rewrite_agents_md(path, SAMPLE, "Test GPU 8 GB", PROFILES))
            text = path.read_text(encoding="utf-8")
            self.assertIn("## Switch models", text)
            self.assertIn("switch to qwen", text)
            self.assertIn("\n\nThe GPU is exclusive: LLM or Comfy.", text)
            self.assertIn("## Next", text)
            self.assertNotIn("old table here", text)
            self.assertNotIn("CLI:", text)
            self.assertEqual(len(SWITCH_BLOCK_RE.findall(text)), 1)

    def test_deploy_wait_line(self):
        sample = (
            "- After `switch to …`, wait for the GPU (warm 12 GB 4070 Ti: qwen ~65s). "
            "GLM is thinking-only on 12 GB (vision off)."
        )
        self.assertTrue(DEPLOY_WAIT_RE.search(sample))
        line = deploy_wait_line(SAMPLE, "Test GPU 8 GB")
        self.assertTrue(line.startswith("- After `switch to …`, wait for the GPU"))
        self.assertIn("Test GPU 8 GB", line)


if __name__ == "__main__":
    unittest.main()
