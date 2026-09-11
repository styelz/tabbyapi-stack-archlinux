"""Clone a pinned vanilla TabbyAPI tree into vendor/tabbyAPI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from sidecar.paths import STACK_ROOT, VENDOR_DIR

UPSTREAM_URL = "https://github.com/theroyallab/tabbyAPI.git"
# main tip as of the sidecar plan (2026-09-10).
PINNED_SHA = "de76ff88991477639a6a7f88c2d116f18c2ca24f"
PIN_FILE = STACK_ROOT / "vendor" / "UPSTREAM"


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def fetch(url: str = UPSTREAM_URL, sha: str = PINNED_SHA, dest: Path = VENDOR_DIR) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").is_dir():
        if dest.exists() and any(dest.iterdir()):
            raise SystemExit(f"{dest} exists and is not a git clone")
        _run(["git", "clone", "--filter=blob:none", url, str(dest)])
    _run(["git", "fetch", "--depth", "1", "origin", sha], cwd=dest)
    _run(["git", "checkout", "--force", sha], cwd=dest)
    PIN_FILE.write_text(f"url={url}\nsha={sha}\n", encoding="utf-8")
    print(f"Vanilla TabbyAPI at {dest} ({sha[:12]})", flush=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=UPSTREAM_URL)
    parser.add_argument("--sha", default=PINNED_SHA)
    parser.add_argument("--dest", default=str(VENDOR_DIR))
    args = parser.parse_args(argv)
    fetch(url=args.url, sha=args.sha, dest=Path(args.dest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
