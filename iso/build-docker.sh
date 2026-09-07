#!/usr/bin/env bash
# Same recipe as .github/workflows/iso.yml. Use on Ubuntu/Docker hosts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec docker run --rm --privileged \
  -v "$ROOT:/src" \
  archlinux:latest \
  bash -lc 'TSOS_ISO_OUT=/src/out /src/iso/build.sh'
