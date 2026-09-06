#!/usr/bin/env bash
# Pull the latest tabbyapi-stack commit into this install, then apply it.
#
# The live tree is the git checkout (clone into $HOME/tabbyapi-stack, or an
# older rsync dest that this script bootstraps). It always sits on
# origin's default branch (main). Runtime data stays: venv, models,
# ComfyUI, config.yml, tabby.env.
set -euo pipefail

DEST="$(cd "$(dirname "$0")" && pwd)"
ORIGIN="${TABBY_GIT_ORIGIN:-https://github.com/styelz/tabbyapi-stack-archlinux.git}"
UPDATE_COMFY=0
# git = git pull only (optional API restart at the end);
# all = pull then install.sh --update (pip, restart).
UPDATE_KIND="${TABBY_UPDATE_KIND:-}"
# empty = ask on TTY after a git pull; 1 = always restart; 0 = never.
RESTART_API="${TABBY_UPDATE_RESTART:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--git|--all] [--comfy] [--restart|--no-restart]

Pull origin into this install. At the start a dialog asks Update git or
Update all. If this script itself changes in that pull, it re-runs so the
new update.sh is used. config.yml, tabby.env, models, and venv stay.
Does not run pacman -Syu or upgrade already-installed OS packages.

Options
  --git         Git pull only. No pip or missing OS packages. A TTY asks
                before restarting tabbyapi. Status Update git restarts by
                itself when API Python files changed.
  --all         Pull, then apply code, Python deps, and reload tabbyapi.
  --comfy       Also git pull ComfyUI and ComfyUI-GGUF. Update all then
                reinstalls their Python requirements; git-only only pulls.
  --restart     At the end, restart tabbyapi and wait for /health (~65s).
                Skips the yes/no prompt. Use with --git when you want a
                bounce even if no Python files changed. Update all already
                restarts.
  --no-restart  Do not restart. Skips the yes/no prompt on Update git.
  -h, --help    This text

No flag and a TTY: dialog menu. No TTY: --all.
Or set TABBY_UPDATE_KIND=git or all, and TABBY_UPDATE_RESTART=1 or 0.

This folder:  $DEST
Origin:       $ORIGIN
EOF
}

while (($#)); do
  case "$1" in
    --git|--files|--files-only) UPDATE_KIND=git; shift ;;
    --all|--full) UPDATE_KIND=all; shift ;;
    --comfy) UPDATE_COMFY=1; shift ;;
    --restart) RESTART_API=1; shift ;;
    --no-restart) RESTART_API=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$UPDATE_KIND" in
  files) UPDATE_KIND=git ;;
  full) UPDATE_KIND=all ;;
esac
case "$UPDATE_KIND" in
  ""|git|all) ;;
  *)
    echo "TABBY_UPDATE_KIND must be git or all (got $UPDATE_KIND)." >&2
    exit 2
    ;;
esac
case "${RESTART_API,,}" in
  ""|0|1) ;;
  yes|true|on) RESTART_API=1 ;;
  no|false|off) RESTART_API=0 ;;
  *)
    echo "TABBY_UPDATE_RESTART must be 1 or 0 (got $RESTART_API)." >&2
    exit 2
    ;;
esac

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

BACKTITLE="tabbyapi-stack"
UPDATE_LOG=""
UI_STARTED=0
GAUGE_PID=""
GAUGE_FIFO=""
GAUGE_DIR=""
GAUGE_MODE=""

die() {
  if [[ "$UI_STARTED" -eq 1 ]]; then
    ui_fail "$*"
  fi
  echo "$*" >&2
  exit 1
}

restore_tty() {
  [[ -t 1 || -c /dev/tty ]] || return 0
  {
    command -v tput >/dev/null 2>&1 && {
      tput rmcup || true
      tput rmkx || true
      tput cnorm || true
      tput sgr0 || true
    }
    printf '\033[?1049l\033[?25h\033[m'
    stty sane
  } >/dev/tty 2>/dev/null || true
}

progress_stop() {
  case "${GAUGE_MODE:-}" in
    dialog)
      exec 3>&- || true
      wait "$GAUGE_PID" 2>/dev/null || true
      if [[ -n "$GAUGE_DIR" ]]; then
        rm -rf "$GAUGE_DIR"
      fi
      restore_tty
      ;;
    text)
      printf '\n' >/dev/tty 2>/dev/null || printf '\n'
      ;;
  esac
  GAUGE_MODE=""
  GAUGE_PID=""
  GAUGE_FIFO=""
  GAUGE_DIR=""
  UI_STARTED=0
}

progress() {
  local pct="$1" msg="$2"
  if [[ -n "$UPDATE_LOG" ]]; then
    printf '%s\n' "==> [$pct%] $msg" >> "$UPDATE_LOG"
  fi
  case "${GAUGE_MODE:-}" in
    dialog)
      printf 'XXX\n%s\n%s\nXXX\n' "$pct" "$msg" >&3 || true
      ;;
    text)
      local fill=$((pct / 2))
      printf '\r\033[K[%s%s] %3d%%  %s' \
        "$(printf '%*s' "$fill" '' | tr ' ' '#')" \
        "$(printf '%*s' $((50 - fill)) '')" \
        "$pct" "$msg" >/dev/tty
      ;;
    verbose)
      echo "==> $msg"
      ;;
  esac
}

ui_gauge_only() {
  local title="${1:-Updating tabbyapi-stack}"
  UI_STARTED=1
  if [[ "${TABBY_INSTALL_VERBOSE:-}" == 1 ]]; then
    GAUGE_MODE="verbose"
    return 0
  fi
  if [[ -t 1 ]] && need_cmd dialog; then
    GAUGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tabby-update-gauge.XXXXXX")"
    GAUGE_FIFO="$GAUGE_DIR/gauge"
    mkfifo -m 600 "$GAUGE_FIFO"
    dialog --backtitle "$BACKTITLE" --title "$title" \
      --gauge "Starting..." 8 70 0 < "$GAUGE_FIFO" &
    GAUGE_PID=$!
    exec 3>"$GAUGE_FIFO"
    GAUGE_MODE="dialog"
    return 0
  fi
  if [[ -t 1 ]]; then
    GAUGE_MODE="text"
    return 0
  fi
  GAUGE_MODE="log"
}

ui_start() {
  UPDATE_LOG="$DEST/tabby-update.log"
  {
    echo "tabbyapi-stack update $(date -Iseconds)"
    echo "dest=$DEST kind=$UPDATE_KIND comfy=$UPDATE_COMFY restart=${RESTART_API:-auto}"
    echo
  } > "$UPDATE_LOG"
  ui_gauge_only
}

ui_msg() {
  local title="$1"
  local text="$2"
  if [[ -t 1 ]] && need_cmd dialog; then
    dialog --backtitle "$BACKTITLE" --title "$title" --msgbox "$text" 12 74 || true
  else
    echo
    echo "=== $title ==="
    echo "$text"
    echo
  fi
}

ui_yesno() {
  local title="$1"
  local text="$2"
  local default_yes="${3:-1}"
  if [[ -t 1 ]] && need_cmd dialog; then
    local extra=()
    [[ "$default_yes" -eq 0 ]] && extra=(--defaultno)
    dialog --backtitle "$BACKTITLE" --title "$title" "${extra[@]}" \
      --yes-label "Restart" --no-label "Skip" --yesno "$text" 16 74
    return $?
  fi
  if [[ -t 1 ]] && need_cmd whiptail; then
    local extra=()
    [[ "$default_yes" -eq 0 ]] && extra=(--defaultno)
    whiptail --backtitle "$BACKTITLE" --title "$title" "${extra[@]}" \
      --yes-button "Restart" --no-button "Skip" --yesno "$text" 16 74
    return $?
  fi
  local yn="Y/n"
  [[ "$default_yes" -eq 0 ]] && yn="y/N"
  echo
  echo "=== $title ==="
  echo "$text"
  echo
  local ans=""
  read -r -p "Restart now? [$yn]: " ans || true
  ans="${ans:-$([[ "$default_yes" -eq 1 ]] && echo y || echo n)}"
  [[ "$ans" =~ ^[Yy] ]]
}

ui_fail() {
  local msg="$1"
  local extra=""
  progress_stop
  if [[ -n "$UPDATE_LOG" && -f "$UPDATE_LOG" ]]; then
    extra="$(tail -n 16 "$UPDATE_LOG")"
    msg="$msg

$extra

Full log: $UPDATE_LOG"
  fi
  if [[ -t 1 ]] && need_cmd dialog; then
    dialog --backtitle "$BACKTITLE" --title "Update failed" --msgbox "$msg" 20 74 || true
  else
    echo "$msg" >&2
  fi
  exit 1
}

run_git() {
  printf '+ %s\n' "$*" >> "$UPDATE_LOG"
  if ! GIT_TERMINAL_PROMPT=0 "$@" >>"$UPDATE_LOG" 2>&1; then
    local rc=$?
    echo "command failed ($rc)" >> "$UPDATE_LOG"
    die "Git command failed ($rc)."
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  die "Run this script on the Arch GPU host, not Windows."
fi
if [[ "${EUID}" -eq 0 ]]; then
  die "Do not run as root. Re-run as the user that owns this install."
fi
if [[ ! -f "$DEST/tabbyAPI/main.py" || ! -f "$DEST/install.sh" ]]; then
  die "This does not look like a tabbyapi-stack install ($DEST).
Run it from the install root (default \$HOME/tabbyapi-stack)."
fi
if ! need_cmd git; then
  die "git is not installed. On Arch: sudo pacman -S git"
fi

# Live checkout only. A separate git source tree must stay commitable.
install_live_git_hooks() {
  local dir="${1:-$DEST}"
  local hook_src="$dir/scripts/refuse-live-git.sh"
  local hook
  [[ -d "$dir/.git" ]] || return 0
  if [[ "$dir" != "$HOME/tabbyapi-stack" \
        && "$dir" != "$HOME/tabby-stack" \
        && ! -d "$dir/tabbyAPI/venv" ]]; then
    return 0
  fi
  printf 'live\n' >"$dir/.live-install"
  mkdir -p "$dir/.git/hooks"
  if [[ -f "$hook_src" ]]; then
    for hook in pre-commit pre-push; do
      install -m 0755 "$hook_src" "$dir/.git/hooks/$hook"
    done
    return 0
  fi
  for hook in pre-commit pre-push; do
    cat >"$dir/.git/hooks/$hook" <<'HOOK'
#!/usr/bin/env bash
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
HOOK
    chmod 0755 "$dir/.git/hooks/$hook"
  done
}

install_live_git_hooks "$DEST"

ask_update_kind() {
  if [[ -n "$UPDATE_KIND" ]]; then
    return
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    UPDATE_KIND=all
    echo "==> No TTY; update all (deps + restart). Pass --git for git pull only."
    return
  fi
  local out=""
  local title="Update tabbyapi-stack"
  local text="Update git pulls new code. At the end you can restart tabbyapi (or pass --restart).
Update all also refreshes Python deps, installs missing OS packages, and restarts the API."
  if need_cmd dialog; then
    local tmp rc
    tmp=$(mktemp "${TMPDIR:-/tmp}/tabby-dialog.XXXXXX")
    set +e
    dialog --backtitle "tabbyapi-stack" --title "$title" --menu "$text" 16 74 2 \
      git "Update git" \
      all "Update all" 2> "$tmp"
    rc=$?
    set -e
    out=$(cat "$tmp" || true)
    rm -f "$tmp"
    [[ "$rc" -eq 0 ]] || die "Update cancelled."
  elif need_cmd whiptail; then
    out="$(whiptail --backtitle "tabbyapi-stack" --title "$title" --menu "$text" 16 74 2 \
      git "Update git" \
      all "Update all" 3>&1 1>&2 2>&3)" || die "Update cancelled."
  else
    echo
    echo "$title"
    echo "$text"
    echo "  1) Update git"
    echo "  2) Update all"
    echo
    local ans
    read -r -p "Choice [1/2] (default 2): " ans || die "No choice given."
    out="$ans"
  fi
  case "${out,,}" in
    git|1) UPDATE_KIND=git ;;
    all|2|"") UPDATE_KIND=all ;;
    *) die "Unknown update choice: $out" ;;
  esac
}

reexec_args() {
  local -a args=()
  if [[ "$UPDATE_KIND" == git ]]; then
    args+=(--git)
  else
    args+=(--all)
  fi
  if [[ "$UPDATE_COMFY" -eq 1 ]]; then
    args+=(--comfy)
  fi
  if [[ "$RESTART_API" == 1 ]]; then
    args+=(--restart)
  elif [[ "$RESTART_API" == 0 ]]; then
    args+=(--no-restart)
  fi
  printf '%s\n' "${args[@]}"
}

# Python the running process already imported. Tests, docs, and install
# wrappers do not need a bounce.
path_needs_api_restart() {
  case "$1" in
    tabbyAPI/tests/*) return 1 ;;
    tabbyAPI/*.py) return 0 ;;
    *) return 1 ;;
  esac
}

collect_restart_files() {
  local from="$1" to="$2"
  local f
  RESTART_FILES=()
  [[ -n "$to" ]] || return 0
  if [[ -z "$from" || "$from" == none || "$from" == "$to" ]]; then
    return 0
  fi
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if path_needs_api_restart "$f"; then
      RESTART_FILES+=("$f")
    fi
  done < <(git -C "$DEST" diff --name-only "$from" "$to")
}

format_restart_file_list() {
  local f i=0 max=8
  local -a shown=()
  for f in "${RESTART_FILES[@]}"; do
    if ((i < max)); then
      shown+=("  $f")
    fi
    i=$((i + 1))
  done
  printf '%s\n' "${shown[@]}"
  if ((i > max)); then
    printf '  ... and %s more\n' "$((i - max))"
  fi
}

ensure_user_bus() {
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
  fi
}

api_unit_running() {
  need_cmd systemctl || return 1
  ensure_user_bus
  systemctl --user is-active --quiet tabbyapi 2>/dev/null && return 0
  systemctl --user is-failed --quiet tabbyapi 2>/dev/null && return 0
  return 1
}

load_tabby_port() {
  local f p
  TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
  for f in "$DEST/tabbyAPI/deploy/arch/tabby.env" "$DEST/tabbyAPI/tabby.env"; do
    [[ -f "$f" ]] || continue
    p="$(sed -n 's/^TABBY_NETWORK_PORT=//p' "$f" | tail -n 1 | tr -d "\"' ")"
    [[ -n "$p" ]] && TABBY_NETWORK_PORT="$p"
  done
}

wait_for_tabby_health() {
  local port="${TABBY_NETWORK_PORT:-5000}"
  local url="http://127.0.0.1:${port}/health"
  local tries="${TABBY_HEALTH_TRIES:-180}"
  local i body=""
  if ! need_cmd curl; then
    echo "curl not found; not waiting on $url" >> "$UPDATE_LOG"
    return 0
  fi
  for ((i = 1; i <= tries; i++)); do
    body="$(curl -sf "$url" 2>/dev/null || true)"
    if [[ "$body" == *'"status":"healthy"'* || "$body" == *'"status": "healthy"'* ]]; then
      echo "API healthy at $url (${i}s)." >> "$UPDATE_LOG"
      return 0
    fi
    progress $((5 + i * 90 / tries)) "Waiting for API health (${i}s)"
    sleep 1
  done
  echo "Timed out after ${tries}s waiting for $url" >> "$UPDATE_LOG"
  [[ -n "${body:-}" ]] && echo "Last body: $body" >> "$UPDATE_LOG"
  return 1
}

restart_tabbyapi() {
  local done_msg="${1:-Pulled the latest code and restarted tabbyapi.}"
  load_tabby_port
  ensure_user_bus
  trap 'rc=$?; if [[ "$UI_STARTED" -eq 1 ]]; then progress_stop; fi; exit "$rc"' EXIT
  ui_gauge_only "Restarting tabbyapi"
  progress 2 "Restarting tabbyapi"
  echo "==> systemctl --user restart tabbyapi" >> "$UPDATE_LOG"
  if ! systemctl --user restart tabbyapi >>"$UPDATE_LOG" 2>&1; then
    die "Failed to restart tabbyapi.
Check: journalctl --user -u tabbyapi -e
Log: $UPDATE_LOG"
  fi
  progress 5 "Waiting for API health"
  if ! wait_for_tabby_health; then
    die "tabbyapi restarted but did not become healthy.
Check: journalctl --user -u tabbyapi -e
Log: $UPDATE_LOG"
  fi
  progress 100 "API healthy"
  trap - EXIT
  progress_stop
  ui_msg "Update git" "$done_msg

Log: $UPDATE_LOG"
}

restart_prompt_text() {
  local new_head=""
  new_head="$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)"
  if ! api_unit_running; then
    printf '%s' "tabbyapi is not running. Start it now so it loads the current files (about 65 seconds)?"
  elif [[ "${TABBY_UPDATE_FROM_REV:-none}" == none ]]; then
    printf '%s' "This install was checked out from git. Restart tabbyapi now so it loads the new files (about 65 seconds)?"
  elif ((${#RESTART_FILES[@]})); then
    printf '%s\n\n%s' \
      "The pull changed API code. Restart tabbyapi now so it loads (about 65 seconds)?" \
      "$(format_restart_file_list)"
  elif [[ "${TABBY_UPDATE_FROM_REV:-}" == "$new_head" ]]; then
    printf '%s' "Already up to date. Restart tabbyapi anyway (about 65 seconds)?"
  else
    printf '%s' "The pull updated this install. Restart tabbyapi now so it loads the new files (about 65 seconds)?"
  fi
}

ask_restart_api() {
  ui_yesno "Restart API?" "$(restart_prompt_text)" 1
}

# Sidecar for /v1/ui Status: same title/text as the TTY Restart/Skip dialog.
write_restart_prompt_json() {
  local path="$DEST/tabby-update-prompt.json"
  local new_head pulled=0 summary
  new_head="$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)"
  if [[ "${TABBY_UPDATE_FROM_REV:-none}" == none || "${TABBY_UPDATE_FROM_REV:-}" != "$new_head" ]]; then
    pulled=1
  fi
  if [[ "$pulled" -eq 1 ]]; then
    summary="Pulled the latest code."
  else
    summary="Already up to date."
  fi
  command -v python3 >/dev/null 2>&1 || return 0
  TABBY_PROMPT_SUMMARY="$summary" TABBY_PROMPT_PULLED="$pulled" python3 -c '
import json, os, sys
json.dump(
    {
        "title": "Restart API?",
        "text": sys.stdin.read().rstrip("\n"),
        "summary": os.environ["TABBY_PROMPT_SUMMARY"],
        "pulled": os.environ["TABBY_PROMPT_PULLED"] == "1",
        "yes_label": "Restart",
        "no_label": "Skip",
    },
    open(sys.argv[1], "w", encoding="utf-8"),
    indent=2,
)
' "$path" <<<"$(restart_prompt_text)" || true
}

# Settings uses sudo -n from the API (no TTY). Keep the stack user passwordless.
ensure_nopasswd_sudo() {
  if ! command -v sudo >/dev/null 2>&1; then
    return 0
  fi
  if ! sudo -n true >/dev/null 2>&1; then
    [[ -t 0 ]] || return 0
    echo "==> sudo password once: write passwordless sudo for $USER (Settings / tsctl)"
    sudo -v || return 0
  fi
  sudo -n bash -c '
    set -euo pipefail
    user="${1:?}"
    dest=/etc/sudoers.d/zz-tsos-nopasswd
    install -d -m 0750 /etc/sudoers.d
    if [[ -f /etc/sudoers.d/wheel ]]; then
      mv /etc/sudoers.d/wheel /etc/sudoers.d/10-wheel
    fi
    tmp=$(mktemp)
    {
      printf "Defaults:%s !use_pty,!requiretty,!pam_session\n" "$user"
      printf "%s ALL=(ALL) NOPASSWD: ALL\n" "$user"
    } >"$tmp"
    chmod 0440 "$tmp"
    if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$tmp" >/dev/null 2>&1; then
      rm -f "$tmp"
      exit 1
    fi
    install -m 0440 "$tmp" "$dest"
    rm -f "$tmp" /etc/sudoers.d/zz-tsos-firstboot /etc/sudoers.d/99-tsos-firstboot
  ' _ "$USER" 2>/dev/null || true
}

install_tsos_motd() {
  local src="$DEST/tabbyAPI/deploy/arch/tsos-motd"
  [[ -f "$src" ]] || return 0
  if [[ -e /usr/local/bin/tsos-motd || -d /etc/tsos ]]; then
    sudo -n install -D -m 0755 "$src" /usr/local/bin/tsos-motd 2>/dev/null || true
  fi
}

install_tsctl() {
  local wrap="$DEST/tabbyAPI/deploy/arch/tsctl"
  [[ -f "$wrap" ]] || return 0
  sudo -n install -D -m 0755 "$wrap" /usr/local/bin/tsctl 2>/dev/null || true
  local bashc="$DEST/tabbyAPI/deploy/arch/tsctl.bash-completion"
  [[ -f "$bashc" ]] && sudo -n install -D -m 0644 "$bashc" /usr/share/bash-completion/completions/tsctl 2>/dev/null || true
  local zshc="$DEST/tabbyAPI/deploy/arch/_tsctl"
  [[ -f "$zshc" ]] && sudo -n install -D -m 0644 "$zshc" /usr/share/zsh/site-functions/_tsctl 2>/dev/null || true
}

install_tabby_gpu() {
  local src="$DEST/tabbyAPI/deploy/arch/tabby-gpu.service"
  local tabby="$DEST/tabbyAPI"
  [[ -f "$src" ]] || return 0
  local tmp
  tmp="$(mktemp)"
  sed -e "s|__TABBY_DIR__|$tabby|g" "$src" > "$tmp"
  if sudo -n install -m 644 "$tmp" /etc/systemd/system/tabby-gpu.service 2>/dev/null; then
    sudo -n systemctl daemon-reload 2>/dev/null || true
    sudo -n systemctl enable --now tabby-gpu 2>/dev/null || true
  fi
  rm -f "$tmp"
}

codebox_image_present() {
  docker image inspect tabbyapi-stack-code:local >/dev/null 2>&1 && return 0
  sudo -n docker image inspect tabbyapi-stack-code:local >/dev/null 2>&1
}

codebox_sources_changed() {
  local from="${TABBY_UPDATE_FROM_REV:-none}" to="${1:-}"
  [[ -n "$to" && "$from" != none && "$from" != "$to" ]] || return 1
  git -C "$DEST" diff --name-only "$from" "$to" | grep -q '^tabbyAPI/ui/codebox/'
}

ensure_codebox_image() {
  local df="$DEST/tabbyAPI/ui/codebox/Dockerfile"
  local new_head="${1:-}"
  [[ -f "$df" ]] || return 0
  command -v docker >/dev/null 2>&1 || return 0
  if codebox_image_present && ! codebox_sources_changed "$new_head"; then
    echo "==> Code sandbox image already present; skipping rebuild" >> "$UPDATE_LOG"
    return 0
  fi
  progress 85 "Building Code sandbox image"
  echo "==> Building Code sandbox image" >> "$UPDATE_LOG"
  if DOCKER_BUILDKIT=1 docker build -t tabbyapi-stack-code:local \
    -f "$df" "$DEST/tabbyAPI/ui/codebox" >> "$UPDATE_LOG" 2>&1; then
    :
  elif DOCKER_BUILDKIT=1 sudo -n docker build -t tabbyapi-stack-code:local \
    -f "$df" "$DEST/tabbyAPI/ui/codebox" >> "$UPDATE_LOG" 2>&1; then
    :
  else
    echo "WARNING: tabbyapi-stack-code image build failed" >> "$UPDATE_LOG"
    return 0
  fi
  local box_ids=()
  mapfile -t box_ids < <(docker ps -aq --filter label=tabby.stack=code 2>/dev/null || true)
  if ((${#box_ids[@]})); then
    docker rm -f "${box_ids[@]}" >> "$UPDATE_LOG" 2>&1 || \
      sudo -n docker rm -f "${box_ids[@]}" >> "$UPDATE_LOG" 2>&1 || true
  fi
}

git_should_auto_restart() {
  ((${#RESTART_FILES[@]} > 0)) && return 0
  api_unit_running || return 0
  return 1
}

finish_git_update() {
  local new_head=""
  new_head="$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)"
  ensure_nopasswd_sudo
  install_tsos_motd
  install_tsctl
  install_tabby_gpu
  collect_restart_files "${TABBY_UPDATE_FROM_REV:-none}" "$new_head"
  printf '%s\n' "==> from_rev=${TABBY_UPDATE_FROM_REV:-none} to_rev=$new_head restart_files=${#RESTART_FILES[@]} restart=${RESTART_API:-auto}" >> "$UPDATE_LOG"
  write_restart_prompt_json
  ensure_codebox_image "$new_head"

  local pulled=0
  if [[ "${TABBY_UPDATE_FROM_REV:-none}" == none || "${TABBY_UPDATE_FROM_REV:-}" != "$new_head" ]]; then
    pulled=1
  fi

  local done_ok="Pulled the latest code."
  [[ "$pulled" -eq 0 ]] && done_ok="Already up to date."
  local restart_msg="$done_ok Restarted tabbyapi."
  [[ "$pulled" -eq 0 ]] && restart_msg="Already up to date. Restarted tabbyapi."

  do_restart() {
    trap - EXIT
    progress_stop
    restart_tabbyapi "$restart_msg"
  }

  skip_restart() {
    progress 100 "Git update finished"
    trap - EXIT
    progress_stop
    ui_msg "Update git" "$done_ok The API was not restarted.

Reload later with:
  systemctl --user restart tabbyapi

Log: $UPDATE_LOG"
  }

  if [[ "$RESTART_API" == 0 ]]; then
    skip_restart
    exit 0
  fi

  if [[ "$RESTART_API" == 1 ]]; then
    do_restart
    exit 0
  fi

  if [[ ! -t 1 && ! -c /dev/tty ]]; then
    if git_should_auto_restart; then
      do_restart
    else
      skip_restart
    fi
    exit 0
  fi

  if ask_restart_api; then
    do_restart
  else
    skip_restart
  fi
  exit 0
}

tracked_dirty() {
  local dir="$1"
  [[ -n "$(git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ]]
}

# Content change, not CRLF vs LF. Copy-to-live on Linux often strips CRs that
# Windows committed; that is not a local edit.
real_tracked_diff() {
  local dir="$1"
  [[ -n "$(git -C "$dir" diff --ignore-cr-at-eol 2>/dev/null)" ]] && return 0
  [[ -n "$(git -C "$dir" diff --cached --ignore-cr-at-eol 2>/dev/null)" ]] && return 0
  return 1
}

restore_crlf_only() {
  local dir="$1"
  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    git -C "$dir" restore --worktree --source=HEAD -- "$f"
  done < <(git -C "$dir" diff --name-only)
}

hash_ignore_cr() {
  tr -d '\r' | git hash-object --stdin
}

matches_origin_blob() {
  local dir="$1" spec="$2" file="$3"
  local want have
  want="$(git -C "$dir" cat-file -p "$spec:$file" | hash_ignore_cr)"
  have="$(hash_ignore_cr <"$dir/$file")"
  [[ "$want" == "$have" ]]
}

# Copy-deploy leaves new repo files untracked. git merge will not overwrite them
# even when they already match origin. Older copies are moved aside; origin wins.
clear_matching_untracked() {
  local dir="$1" spec="$2"
  local f bak=""
  local -a conflicts=() matches=()
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    git -C "$dir" cat-file -e "$spec:$f" 2>/dev/null || continue
    if matches_origin_blob "$dir" "$spec" "$f"; then
      matches+=("$f")
    else
      conflicts+=("$f")
    fi
  done < <(git -C "$dir" ls-files --others --exclude-standard)
  if ((${#matches[@]})); then
    printf '%s\n' "==> Removing untracked copies that already match $spec" >> "${UPDATE_LOG:-/dev/null}"
    for f in "${matches[@]}"; do
      rm -f "$dir/$f"
    done
  fi
  if ((${#conflicts[@]})); then
    bak="$(mktemp -d "${TMPDIR:-/tmp}/tabbyapi-stack-update-untracked.XXXXXX")"
    printf '%s\n' "==> Untracked files differ from $spec; moving aside to $bak" >> "${UPDATE_LOG:-/dev/null}"
    for f in "${conflicts[@]}"; do
      mkdir -p "$bak/$(dirname "$f")"
      mv "$dir/$f" "$bak/$f"
      printf '    %s\n' "$f" >> "${UPDATE_LOG:-/dev/null}"
    done
  fi
}

origin_branch() {
  local dir="$1"
  local branch=""
  branch="$(git -C "$dir" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)"
  branch="${branch#origin/}"
  if [[ -z "$branch" ]]; then
    if git -C "$dir" rev-parse --verify -q origin/main >/dev/null; then
      branch=main
    elif git -C "$dir" rev-parse --verify -q origin/master >/dev/null; then
      branch=master
    fi
  fi
  printf '%s' "$branch"
}

ensure_stack_origin() {
  if ! git -C "$DEST" remote get-url origin >/dev/null 2>&1; then
    run_git git -C "$DEST" remote add origin "$ORIGIN"
  fi
}

is_stack_wrapper() {
  case "$1" in
    install.sh|uninstall.sh|update.sh) return 0 ;;
    *) return 1 ;;
  esac
}

dirty_tracked_names() {
  local dir="$1"
  git -C "$dir" diff --name-only
  git -C "$dir" diff --cached --name-only
}

restore_head_file() {
  local dir="$1" file="$2"
  git -C "$dir" restore --source=HEAD --staged --worktree -- "$file" 2>/dev/null \
    || git -C "$dir" restore --worktree --source=HEAD -- "$file"
}

# Live is a deploy checkout. Copy-to-live and leftover patches must not
# abort a fast-forward: origin wins for tracked source. Divergent copies
# are moved aside first. Dirty install/update/uninstall wrappers are held
# separately; origin wins only if that wrapper changed in the pull.
# Always putting the local wrappers back hid the new update.sh on live.
backup_divergent_tracked() {
  local dir="$1" spec="$2"
  shift 2
  local stamp bak f
  (($#)) || return 0
  stamp="$(date +%Y%m%d-%H%M%S)"
  bak="$dir/.tabby-update-backup/$stamp"
  mkdir -p "$bak"
  printf '%s\n' "==> Moving tracked copies that are not on $spec aside to $bak" >> "${UPDATE_LOG:-/dev/null}"
  for f in "$@"; do
    [[ -n "$f" ]] || continue
    printf '    %s\n' "$f" >> "${UPDATE_LOG:-/dev/null}"
    if [[ -e "$dir/$f" ]]; then
      mkdir -p "$bak/$(dirname "$f")"
      cp -a "$dir/$f" "$bak/$f"
    fi
    restore_head_file "$dir" "$f"
  done
}

take_origin_copies() {
  local dir="$1" spec="$2"
  local f
  local -A seen=()
  MATCHED_ORIGIN=()
  AHEAD_WRAPPERS=()
  REAL_EDITS=()
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    [[ -z "${seen[$f]:-}" ]] || continue
    seen[$f]=1
    [[ -f "$dir/$f" ]] || {
      REAL_EDITS+=("$f")
      continue
    }
    if git -C "$dir" cat-file -e "$spec:$f" 2>/dev/null && matches_origin_blob "$dir" "$spec" "$f"; then
      MATCHED_ORIGIN+=("$f")
      restore_head_file "$dir" "$f"
      continue
    fi
    if is_stack_wrapper "$f"; then
      AHEAD_WRAPPERS+=("$f")
    else
      REAL_EDITS+=("$f")
    fi
  done < <(dirty_tracked_names "$dir")
}

ff_pull() {
  local dir="$1"
  local label="$2"
  local branch=""
  local current=""
  local wrappers_tmp=""
  local wrap f
  local pct_fetch="${3:-15}"
  local pct_merge="${4:-75}"
  progress "$pct_fetch" "Fetching $label"
  run_git git -C "$dir" fetch origin
  branch="$(origin_branch "$dir")"
  if [[ -z "$branch" ]]; then
    branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD)"
  fi
  [[ -n "$branch" && "$branch" != "HEAD" ]] || die "Could not determine the branch for $label."
  take_origin_copies "$dir" "origin/$branch"
  if ((${#MATCHED_ORIGIN[@]})); then
    printf '%s\n' "==> Local copies already match origin/$branch" >> "$UPDATE_LOG"
  fi
  if ((${#AHEAD_WRAPPERS[@]})); then
    wrappers_tmp="$(mktemp -d "${TMPDIR:-/tmp}/tabbyapi-stack-wrappers.XXXXXX")"
    for wrap in "${AHEAD_WRAPPERS[@]}"; do
      printf '%s\n' "==> Holding local $wrap (differs from origin/$branch)" >> "$UPDATE_LOG"
      cp "$dir/$wrap" "$wrappers_tmp/$wrap"
      restore_head_file "$dir" "$wrap"
    done
  fi
  if ((${#REAL_EDITS[@]})); then
    backup_divergent_tracked "$dir" "origin/$branch" "${REAL_EDITS[@]}"
  fi
  if real_tracked_diff "$dir"; then
    mapfile -t _leftover < <(dirty_tracked_names "$dir")
    if ((${#_leftover[@]})); then
      backup_divergent_tracked "$dir" "origin/$branch" "${_leftover[@]}"
    fi
  fi
  if tracked_dirty "$dir"; then
    printf '%s\n' "==> Resetting tracked files in $label to HEAD for the pull" >> "$UPDATE_LOG"
    git -C "$dir" restore --source=HEAD --staged --worktree -- . >>"$UPDATE_LOG" 2>&1 \
      || git -C "$dir" reset --hard HEAD >>"$UPDATE_LOG" 2>&1 \
      || true
  fi
  if tracked_dirty "$dir"; then
    printf '%s\n' "==> Ignoring CRLF-only line-ending drift in $label" >> "$UPDATE_LOG"
    restore_crlf_only "$dir"
  fi
  clear_matching_untracked "$dir" "origin/$branch"
  progress "$pct_merge" "Checking out origin/$branch"
  current="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [[ -n "$current" && "$current" != "HEAD" && "$current" != "$branch" ]]; then
    printf '%s\n' "==> Switching $label from $current to $branch" >> "$UPDATE_LOG"
  fi
  # Deploy checkout: sit on origin's branch, not a leftover local name
  # (rewrite, etc.). -B moves that branch to origin and checks it out.
  run_git git -C "$dir" checkout -B "$branch" "origin/$branch"
  git -C "$dir" branch --set-upstream-to="origin/$branch" "$branch" >>"$UPDATE_LOG" 2>&1 || true
  if [[ -n "$wrappers_tmp" ]]; then
    local keep_origin wrap_old wrap_new
    for wrap in "${AHEAD_WRAPPERS[@]}"; do
      keep_origin=0
      if [[ -z "${TABBY_UPDATE_FROM_REV:-}" || "${TABBY_UPDATE_FROM_REV}" == none ]]; then
        keep_origin=1
      elif ! git -C "$dir" cat-file -e "${TABBY_UPDATE_FROM_REV}:$wrap" 2>/dev/null; then
        keep_origin=1
      else
        wrap_old="$(git -C "$dir" cat-file -p "${TABBY_UPDATE_FROM_REV}:$wrap" | hash_ignore_cr)"
        wrap_new="$(git -C "$dir" cat-file -p "HEAD:$wrap" | hash_ignore_cr)"
        [[ "$wrap_old" != "$wrap_new" ]] && keep_origin=1
      fi
      if [[ "$keep_origin" -eq 1 ]]; then
        printf '%s\n' "==> Keeping origin/$branch $wrap" >> "$UPDATE_LOG"
      else
        cp "$wrappers_tmp/$wrap" "$dir/$wrap"
        printf '%s\n' "==> Restored local $wrap (unchanged on origin/$branch)" >> "$UPDATE_LOG"
      fi
    done
    rm -rf "$wrappers_tmp"
  fi
}

ask_update_kind
ui_start
trap 'rc=$?; if [[ "$UI_STARTED" -eq 1 ]]; then progress_stop; fi; exit "$rc"' EXIT
if [[ -z "${TABBY_UPDATE_FROM_REV:-}" ]]; then
  if [[ -d "$DEST/.git" ]]; then
    TABBY_UPDATE_FROM_REV="$(git -C "$DEST" rev-parse HEAD 2>/dev/null || echo none)"
  else
    TABBY_UPDATE_FROM_REV=none
  fi
fi
printf '%s\n' "==> TABBY_UPDATE_FROM_REV=$TABBY_UPDATE_FROM_REV" >> "$UPDATE_LOG"
BEFORE_UPDATE_SH="$(hash_ignore_cr <"$DEST/update.sh")"

if [[ -d "$DEST/.git" ]]; then
  ensure_stack_origin
  ff_pull "$DEST" "tabbyapi-stack" 20 70
  install_live_git_hooks "$DEST"
else
  progress 15 "Bootstrapping git from origin"
  run_git git -C "$DEST" init
  ensure_stack_origin
  run_git git -C "$DEST" fetch origin
  branch="$(origin_branch "$DEST")"
  [[ -n "$branch" ]] || die "Could not find origin/main or origin/master at $ORIGIN."
  progress 55 "Checking out origin/$branch"
  if ! GIT_TERMINAL_PROMPT=0 git -C "$DEST" checkout -f -B "$branch" "origin/$branch" >>"$UPDATE_LOG" 2>&1; then
    progress 70 "Resetting tracked paths to origin/$branch"
    run_git git -C "$DEST" reset --hard "origin/$branch"
  fi
  install_live_git_hooks "$DEST"
fi

if [[ "$UPDATE_COMFY" -eq 1 ]]; then
  export TABBY_UPDATE_COMFY=1
  if [[ -d "$DEST/ComfyUI/.git" ]]; then
    ff_pull "$DEST/ComfyUI" "ComfyUI" 80 88
  else
    printf '%s\n' "WARNING: $DEST/ComfyUI is not a git checkout; skipping ComfyUI pull." >> "$UPDATE_LOG"
  fi
  if [[ -d "$DEST/ComfyUI/custom_nodes/ComfyUI-GGUF/.git" ]]; then
    ff_pull "$DEST/ComfyUI/custom_nodes/ComfyUI-GGUF" "ComfyUI-GGUF" 90 95
  else
    printf '%s\n' "WARNING: ComfyUI-GGUF is not a git checkout; skipping its pull." >> "$UPDATE_LOG"
  fi
fi

AFTER_UPDATE_SH="$(hash_ignore_cr <"$DEST/update.sh")"
if [[ "$BEFORE_UPDATE_SH" != "$AFTER_UPDATE_SH" ]]; then
  progress 98 "Restarting with the new update.sh"
  trap - EXIT
  progress_stop
  mapfile -t _reexec < <(reexec_args)
  exec env TABBY_UPDATE_FROM_REV="$TABBY_UPDATE_FROM_REV" bash "$DEST/update.sh" "${_reexec[@]}"
fi

if [[ "$UPDATE_KIND" == git ]]; then
  finish_git_update
fi

progress 100 "Code pulled; applying deps and restart"
trap - EXIT
progress_stop
export TABBY_UPDATE_LOG="$UPDATE_LOG"
printf '%s\n' "==> handing off to install.sh --update" >> "$UPDATE_LOG"
exec bash "$DEST/install.sh" --update
