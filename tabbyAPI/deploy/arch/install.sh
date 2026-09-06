#!/usr/bin/env bash
# Back-compat: the installer lives at the tabbyapi-stack root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
exec "$ROOT/install.sh" "$@"
