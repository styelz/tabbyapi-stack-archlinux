import sys
from pathlib import Path

_STACK = Path(__file__).resolve().parents[2]
if str(_STACK) not in sys.path:
    sys.path.insert(0, str(_STACK))
