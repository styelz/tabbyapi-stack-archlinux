"""Reasoning phrase-loops are collapsed and cut off."""

from __future__ import annotations

import unittest
from pathlib import Path

from common.stutter import collapse_stutter, tail_is_stutter

_LOOP = "I had to had had him now. "
_PLAN = (
    "Now I need to write styles.css and main.js in one response after the "
    "tools succeed. The hero keeps a full-bleed photograph, a gold headline, "
    "and two buttons. The garage grid filters classics, muscle, and race. "
    "The pit crew cards use the names already chosen. Stop after the summary."
)


class StutterTests(unittest.TestCase):
    def test_repeated_phrase_is_a_stutter(self):
        self.assertTrue(tail_is_stutter(_LOOP * 20))

    def test_near_repeat_still_counts(self):
        parts = []
        for index in range(16):
            if index % 5 == 0:
                parts.append("I had to had had had him now. ")
            else:
                parts.append(_LOOP)
        self.assertTrue(tail_is_stutter("".join(parts)))

    def test_planning_text_is_not_a_stutter(self):
        self.assertFalse(tail_is_stutter(_PLAN))
        self.assertEqual(collapse_stutter(_PLAN), _PLAN.strip())

    def test_loop_in_the_middle_is_removed_and_the_plan_stays(self):
        text = "Plan the page.\n\n" + (_LOOP * 40) + _PLAN
        self.assertFalse(tail_is_stutter(text))
        collapsed = collapse_stutter(text)
        self.assertIn("garage grid", collapsed)
        self.assertIn("Stop after the summary.", collapsed)
        self.assertIn("…", collapsed)
        self.assertLess(collapsed.count("I had to had had him now"), 10)
        self.assertGreater(text.count("I had to had had him now"), 30)

    def test_short_text_is_unchanged(self):
        text = "I had to write the CSS first."
        self.assertFalse(tail_is_stutter(text))
        self.assertEqual(collapse_stutter(text), text)

    def test_generation_cuts_reasoning_when_the_tail_loops(self):
        src = (
            Path(__file__).resolve().parents[1]
            / "endpoints/OAI/utils/chat_completion.py"
        ).read_text(encoding="utf-8")
        self.assertIn("tail_is_stutter(full_reasoning)", src)
        self.assertIn("_end_reasoning_after_stutter(mc, job_id)", src)
        self.assertIn("collapse_stutter(full_reasoning)", src)
