"""Restart TabbyAPI (and the stack sidecar) if a process dies."""

from __future__ import annotations

import sys
from pathlib import Path

_STACK = Path(__file__).resolve().parent.parent
if str(_STACK) not in sys.path:
    sys.path.insert(0, str(_STACK))

from sidecar.supervise import run


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
