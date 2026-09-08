#!/usr/bin/env bash
# Periodic tabbyapi-stack update. The user timer runs this daily; the script
# only pulls when TABBY_AUTO_UPDATE is on and the interval has elapsed.
# Skip (exit 0) when the stack is busy so a chat or image job is not bounced.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${TABBY_INSTALL_ROOT:-}" ]]; then
  STACK_ROOT="$TABBY_INSTALL_ROOT"
  TABBY_DIR="$STACK_ROOT/tabbyAPI"
else
  TABBY_DIR="$(cd "$HERE/../.." && pwd)"
  STACK_ROOT="$(cd "$TABBY_DIR/.." && pwd)"
fi
ENV_FILE="$TABBY_DIR/deploy/arch/tabby.env"
STAMP="$STACK_ROOT/tabby-auto-update.stamp"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME=tabbyapi-auto-update
LOG="${TABBY_AUTO_UPDATE_LOG:-$STACK_ROOT/tabby-auto-update.log}"

DRY_RUN=0
FORCE=0
ACTION=run

usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run] [--force] [--install-units] [--status]

  (default)     Run update.sh when auto-update is due
  --dry-run     Print the decision and exit
  --force       Ignore the interval (still skips when disabled or busy)
  --install-units
                Write and enable/disable the systemd --user timer
  --status      Print enabled / last run / next due
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --install-units) ACTION=install; shift ;;
    --status) ACTION=status; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() {
  local line
  line="$(date -Iseconds) $*"
  mkdir -p "$(dirname "$LOG")"
  printf '%s\n' "$line" >>"$LOG"
  printf '%s\n' "$line"
}

read_env() {
  local line key value
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ "$line" == \#* || -z "$line" ]] && continue
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    value=${line#*=}
    value="${value%\"}"
    value="${value#\"}"
    case "$key" in
      TABBY_AUTO_UPDATE) TABBY_AUTO_UPDATE=$value ;;
      TABBY_AUTO_UPDATE_DAYS) TABBY_AUTO_UPDATE_DAYS=$value ;;
      TABBY_AUTO_UPDATE_FULL) TABBY_AUTO_UPDATE_FULL=$value ;;
      TABBY_NETWORK_PORT) TABBY_NETWORK_PORT=$value ;;
      TABBY_INSTALL_ROOT) STACK_ROOT=$value; STAMP="$STACK_ROOT/tabby-auto-update.stamp" ;;
    esac
  done < "$ENV_FILE"
}

enabled_flag() {
  case "${TABBY_AUTO_UPDATE:-1}" in
    1|yes|true|on) return 0 ;;
    *) return 1 ;;
  esac
}

full_flag() {
  case "${TABBY_AUTO_UPDATE_FULL:-1}" in
    1|yes|true|on) return 0 ;;
    *) return 1 ;;
  esac
}

interval_days() {
  local n="${TABBY_AUTO_UPDATE_DAYS:-7}"
  [[ "$n" =~ ^[0-9]+$ ]] || n=7
  ((n >= 1)) || n=1
  ((n <= 365)) || n=365
  printf '%s' "$n"
}

last_stamp() {
  local n=0
  if [[ -f "$STAMP" ]]; then
    n=$(head -n1 "$STAMP" 2>/dev/null || true)
  fi
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  printf '%s' "$n"
}

write_stamp() {
  mkdir -p "$(dirname "$STAMP")"
  date +%s >"$STAMP"
}

due_now() {
  local last now days elapsed
  ((FORCE)) && return 0
  last=$(last_stamp)
  ((last == 0)) && return 0
  now=$(date +%s)
  days=$(interval_days)
  elapsed=$((now - last))
  ((elapsed >= days * 86400))
}

stack_busy() {
  local port="${TABBY_NETWORK_PORT:-5000}"
  local url="http://127.0.0.1:${port}/v1/ui/saver/state"
  local body=""
  body=$(curl -fsS --connect-timeout 2 --max-time 4 "$url" 2>/dev/null || true)
  [[ -n "$body" ]] || return 1
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$body" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if data.get("busy") else 1)' 2>/dev/null
    return $?
  fi
  printf '%s' "$body" | grep -q '"busy"[[:space:]]*:[[:space:]]*true'
}

update_already_running() {
  systemctl --user is-active --quiet tabbyapi-stack-update 2>/dev/null
}

user_systemctl() {
  systemctl --user "$@"
}

install_units() {
  local stack tabby days
  stack="$STACK_ROOT"
  tabby="$TABBY_DIR"
  mkdir -p "$UNIT_DIR" "$UNIT_DIR/timers.target.wants"
  if [[ -f "$HERE/tabbyapi-auto-update.service" ]]; then
    sed -e "s|__STACK_ROOT__|$stack|g" -e "s|__TABBY_DIR__|$tabby|g" \
      "$HERE/tabbyapi-auto-update.service" >"$UNIT_DIR/${SERVICE_NAME}.service"
  fi
  if [[ -f "$HERE/tabbyapi-auto-update.timer" ]]; then
    sed -e "s|__STACK_ROOT__|$stack|g" -e "s|__TABBY_DIR__|$tabby|g" \
      "$HERE/tabbyapi-auto-update.timer" >"$UNIT_DIR/${SERVICE_NAME}.timer"
  fi
  user_systemctl daemon-reload >/dev/null 2>&1 || true
  if enabled_flag; then
    if user_systemctl enable --now "${SERVICE_NAME}.timer" >/dev/null 2>&1; then
      :
    else
      ln -sfn "../${SERVICE_NAME}.timer" \
        "$UNIT_DIR/timers.target.wants/${SERVICE_NAME}.timer"
    fi
  else
    user_systemctl disable --now "${SERVICE_NAME}.timer" >/dev/null 2>&1 || true
    user_systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/timers.target.wants/${SERVICE_NAME}.timer"
  fi
  days=$(interval_days)
  if enabled_flag; then
    [[ -f "$STAMP" ]] || write_stamp
    log "Auto-update timer enabled (every ${days} days)"
  else
    log "Auto-update timer disabled"
  fi
}

print_status() {
  local last days due="no" enabled="off" timer="unknown"
  days=$(interval_days)
  enabled_flag && enabled="on"
  last=$(last_stamp)
  if due_now; then
    due="yes"
  fi
  timer=$(user_systemctl is-enabled "${SERVICE_NAME}.timer" 2>/dev/null || echo missing)
  printf 'enabled=%s days=%s due=%s timer=%s last=%s stamp=%s\n' \
    "$enabled" "$days" "$due" "$timer" "$last" "$STAMP"
}

run_update() {
  local script="$STACK_ROOT/update.sh"
  local -a args
  [[ -f "$script" ]] || { log "update.sh missing at $script"; return 1; }
  args=(bash "$script")
  if full_flag; then
    args+=(--all --restart)
  else
    # Unattended: update.sh restarts only when API Python changed (or the unit is down).
    args+=(--git)
  fi
  if ((DRY_RUN)); then
    log "dry-run: ${args[*]}"
    return 0
  fi
  log "Starting auto-update: ${args[*]}"
  "${args[@]}"
  write_stamp
  log "Auto-update finished"
}

decide() {
  if ! enabled_flag; then
    printf '%s\n' "disabled"
    return 1
  fi
  if update_already_running; then
    printf '%s\n' "already-running"
    return 1
  fi
  if ! due_now; then
    printf '%s\n' "not-due"
    return 1
  fi
  if stack_busy; then
    printf '%s\n' "busy"
    return 1
  fi
  printf '%s\n' "run"
  return 0
}

read_env
TABBY_AUTO_UPDATE="${TABBY_AUTO_UPDATE:-1}"
TABBY_AUTO_UPDATE_DAYS="${TABBY_AUTO_UPDATE_DAYS:-7}"
TABBY_AUTO_UPDATE_FULL="${TABBY_AUTO_UPDATE_FULL:-1}"
TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"

case "$ACTION" in
  install)
    install_units
    exit 0
    ;;
  status)
    print_status
    exit 0
    ;;
esac

reason=$(decide || true)
if [[ "$reason" != run ]]; then
  if ((DRY_RUN)); then
    log "dry-run: skip ($reason)"
  else
    log "Skip auto-update ($reason)"
  fi
  exit 0
fi
if ((DRY_RUN)); then
  log "dry-run: would update (full=$(full_flag && echo 1 || echo 0))"
  exit 0
fi
run_update
