"""Locate the stack root and the TabbyAPI import tree."""

from __future__ import annotations

import sys
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parent.parent
TABBY_DIR = STACK_ROOT / "tabbyAPI"
VENDOR_DIR = STACK_ROOT / "vendor" / "tabbyAPI"


def ensure_import_path() -> None:
    for path in (str(STACK_ROOT), str(TABBY_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)
