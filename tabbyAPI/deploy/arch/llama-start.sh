#!/bin/bash
set -euo pipefail
TABBY_DIR="${TABBY_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
ENV_FILE="${LLAMA_ENV_FILE:-$TABBY_DIR/model_profiles/llama.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
SERVER="${LLAMA_SERVER:-}"
if [[ -z "$SERVER" || ! -x "$SERVER" ]]; then
  for candidate in \
    "${LLAMA_DIR:-}/llama-server" \
    "${LLAMA_DIR:-}/build/bin/llama-server" \
    "$(dirname "$TABBY_DIR")/llama.cpp/llama-server" \
    "$(dirname "$TABBY_DIR")/llama.cpp/build/bin/llama-server"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      SERVER="$candidate"
      break
    fi
  done
fi
if [[ -z "$SERVER" ]]; then
  SERVER="$(command -v llama-server || true)"
fi
if [[ -z "$SERVER" || ! -x "$SERVER" ]]; then
  echo "llama-server binary not found. Set LLAMA_SERVER or install llama.cpp." >&2
  exit 1
fi
if [[ -z "${LLAMA_MODEL:-}" ]]; then
  echo "LLAMA_MODEL is not set. Switch to a GGUF profile first." >&2
  exit 1
fi
ARGS=(
  --host "${LLAMA_HOST:-127.0.0.1}"
  --port "${LLAMA_PORT:-5002}"
  --alias "${LLAMA_ALIAS:-gpt-4o}"
  --jinja
  -m "$LLAMA_MODEL"
  -c "${LLAMA_CTX:-32768}"
  -ngl "${LLAMA_NGL:-999}"
)
if [[ -n "${LLAMA_MMPROJ:-}" ]]; then
  ARGS+=(--mmproj "$LLAMA_MMPROJ")
fi
exec "$SERVER" "${ARGS[@]}"
