"""Detect a reasoning trace that has collapsed into one repeated phrase.

ExLlamaV3 stop-on-loop only fires when the last N tokens are one exact
cycle. A trace like "I had to had had him now" keeps adding or dropping a
word, so the token loop never matches, the model rambles for thousands of
characters, and the thought panel shows that ramble. A short tail with
almost no vocabulary is the same failure.
"""

from __future__ import annotations

import re
from collections import Counter

_WORD = re.compile(r"[A-Za-z']+")
# Last N words. Real planning text stays well above the unique-word cap.
_WINDOW = 48
_MAX_UNIQUE = 8

def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _WORD.finditer(text or "")]


def _window_is_stutter(words: list[str]) -> bool:
    if len(words) < _WINDOW:
        return False
    counts = Counter(words)
    top = counts.most_common(1)[0][1]
    return len(counts) <= _MAX_UNIQUE and top >= _WINDOW // 3


def tail_is_stutter(text: str) -> bool:
    """True when the latest words are the same handful of words on repeat."""
    return _window_is_stutter(_tokens(text)[-_WINDOW:])


def collapse_stutter(text: str) -> str:
    """Replace a repeated-phrase run with an ellipsis. Leave the rest."""
    raw = text or ""
    matches = list(_WORD.finditer(raw))
    if len(matches) < _WINDOW:
        return raw
    tokens = [match.group(0).lower() for match in matches]
    cut = [False] * len(tokens)
    last = len(tokens) - _WINDOW + 1
    for start in range(last):
        if _window_is_stutter(tokens[start : start + _WINDOW]):
            for index in range(start, start + _WINDOW):
                cut[index] = True
    if not any(cut):
        return raw
    parts: list[str] = []
    cursor = 0
    index = 0
    while index < len(tokens):
        if not cut[index]:
            index += 1
            continue
        start = matches[index].start()
        while index < len(tokens) and cut[index]:
            index += 1
        end = matches[index].start() if index < len(tokens) else len(raw)
        parts.append(raw[cursor:start])
        parts.append("…\n\n")
        cursor = end
    parts.append(raw[cursor:])
    collapsed = re.sub(r"\n{3,}", "\n\n", "".join(parts))
    return collapsed.strip()
