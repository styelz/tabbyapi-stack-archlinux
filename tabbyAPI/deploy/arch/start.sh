#!/usr/bin/env bash
# Start TabbyAPI from the tabbyapi-stack install root (not from tabbyAPI/).
# The installer copies this file to $DEST/start.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TABBY="$ROOT/tabbyAPI"
ENV_FILE="$TABBY/deploy/arch/tabby.env"

if [[ ! -x "$TABBY/venv/bin/python" ]]; then
  echo "TabbyAPI is not installed here ($TABBY/venv is missing)."
  echo "From the tabbyapi-stack source root run: bash install.sh"
  exit 1
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active --quiet tabbyapi 2>/dev/null; then
  echo "tabbyapi is already running via systemd."
  echo "  status: systemctl --user status tabbyapi"
  echo "  stop:   systemctl --user stop tabbyapi"
  exit 0
fi

cd "$TABBY"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
export TABBY_LOG_CONSOLE_WIDTH="${TABBY_LOG_CONSOLE_WIDTH:-256}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi
if command -v docker >/dev/null 2>&1 && [[ ! -S /var/run/docker.sock ]]; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -S /var/run/docker.sock ]] && break
    sleep 1
  done
fi
if [[ -w /var/run/docker.sock ]]; then
  exec "$TABBY/venv/bin/python" "$TABBY/watch_api.py" "$@"
fi
if command -v sg >/dev/null 2>&1 && sg docker -c true >/dev/null 2>&1; then
  exec sg docker -c "export XDG_RUNTIME_DIR=$(printf %q "$XDG_RUNTIME_DIR"); export DBUS_SESSION_BUS_ADDRESS=$(printf %q "$DBUS_SESSION_BUS_ADDRESS"); exec $(printf %q "$TABBY/venv/bin/python") $(printf %q "$TABBY/watch_api.py")"
fi
if command -v sudo >/dev/null 2>&1 && sudo -n -u "$USER" -g docker true >/dev/null 2>&1; then
  # sudo env_reset drops sourced tabby.env. Re-inject stack settings so the
  # reverse SSH tunnel and public URL still work. Also keep the user bus so
  # Tabby can systemctl --user stop comfyui.
  args=()
  names="$( { compgen -e TABBY_ || true; compgen -e COMFYUI_ || true; } )"
  for name in $names; do
    args+=("$name=${!name}")
  done
  args+=("XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR}")
  if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    args+=("DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS}")
  fi
  exec sudo -n -u "$USER" -g docker /usr/bin/env "${args[@]}" \
    "$TABBY/venv/bin/python" "$TABBY/watch_api.py" "$@"
fi
exec "$TABBY/venv/bin/python" "$TABBY/watch_api.py" "$@"
