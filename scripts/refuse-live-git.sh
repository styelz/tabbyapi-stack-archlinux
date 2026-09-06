#!/usr/bin/env bash
# pre-commit / pre-push: the running install must not grow its own commits.
# Commit in the git source tree. Live only pulls origin/main.
set -euo pipefail
root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$root" ]] || exit 0
if [[ -f "$root/.live-install" \
     || "$root" == "${HOME}/tabbyapi-stack" \
     || "$root" == "${HOME}/tabby-stack" ]]; then
  echo "Refuse: this is the live Tabby install (${root})." >&2
  echo "Commit and push in the git source tree. Live only pulls origin/main." >&2
  exit 1
fi
exit 0
