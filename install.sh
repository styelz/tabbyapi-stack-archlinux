#!/usr/bin/env bash
# Install TabbyAPI + ComfyUI on Arch. Weights are copied from an optional
# local cache (USB or a folder you point at) or downloaded from Hugging Face.
# Re-run skips files that already exist. The git tree does not ship LLMs.
# To pull later changes on the install itself, run update.sh (this script --update).
set -euo pipefail

STACK_ROOT="$(cd "$(dirname "$0")" && pwd)"
TABBY_SRC="$STACK_ROOT/tabbyAPI"
SCRIPT_DIR="$TABBY_SRC/deploy/arch"
CATALOG="$SCRIPT_DIR/models.json"
FETCH_MODELS="$SCRIPT_DIR/fetch_models.py"

EMBED_NAME="Qwen3-Embedding-0.6B"
# Official Arch python is 3.14; python312 is AUR-only. Match the first-boot workaround.
PYENV_VER="3.12.5"
TSOS_OFFLINE_ROOT="${TSOS_OFFLINE_ROOT:-}"
PIP_OFFLINE_ARGS=()
if [[ -n "$TSOS_OFFLINE_ROOT" && -d "$TSOS_OFFLINE_ROOT/wheels" ]]; then
  PIP_OFFLINE_ARGS=(--no-index --find-links "$TSOS_OFFLINE_ROOT/wheels")
  export PIP_NO_INDEX=1
  export PIP_FIND_LINKS="$TSOS_OFFLINE_ROOT/wheels"
fi

tsos_bundle() {
  local name=$1 path
  for path in \
    "${TSOS_OFFLINE_ROOT:-}/bundles/${name}.bundle" \
    /opt/tsos/bundles/${name}.bundle
  do
    [[ -n "$path" && -f "$path" ]] && { printf '%s' "$path"; return 0; }
  done
  return 1
}

BACKTITLE="tabbyapi-stack"
TUI=""
USE_TUI=0
INTERACTIVE=1
UPDATE_MODE=0
INSTALL_MODE="${INSTALL_MODE:-}" # simple | advanced | empty (ask)

usage_install() {
  cat <<EOF
Usage: $(basename "$0") [--update] [--simple|--advanced]

  (no args)     Interactive or env-driven install / re-run.
                Starts with Simple setup (review menu: this PC vs LAN;
                core models). Choose Advanced for cache, extra models,
                tunnels, screensaver.
  --simple      Review menu: this PC vs LAN. Install root is
                \$HOME/tabbyapi-stack, models core, Hugging Face.
  --advanced    Review menu for every setting
  --update      Apply code and deps after git pull. Prefer: bash update.sh
                Reuses tabby.env; does not overwrite config.yml or tabby.env.
                Does not pacman -Syu; only installs missing OS packages.
  -h, --help    This text

  INSTALL_MODE=simple|advanced  Same as --simple / --advanced

  ISO chroot (from tsos-installer, or a resume): dest, cache, model set,
  and API URLs come from tsos (TABBY_NONINTERACTIVE=1). Resume:
    arch-chroot /mnt /usr/bin/runuser -u USER -- env \\
      HOME=/home/USER USER=USER LOGNAME=USER TERM=linux \\
      TABBY_ISO_CHROOT=1 TABBY_SKIP_NVIDIA_REBOOT=1 \\
      TABBY_INSTALL_ROOT=/home/USER/tabbyapi-stack TABBY_SAVER_ENABLED=1 \\
      bash /home/USER/tabbyapi-stack/install.sh
EOF
}

while (($#)); do
  case "$1" in
    --update) UPDATE_MODE=1; TABBY_NONINTERACTIVE=1; shift ;;
    --simple) INSTALL_MODE=simple; shift ;;
    --advanced) INSTALL_MODE=advanced; shift ;;
    -h|--help) usage_install; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage_install >&2
      exit 2
      ;;
  esac
done

prompt() {
  local __var="$1"
  local __msg="$2"
  local __default="$3"
  local __value=""
  read -r -p "${__msg} [${__default}]: " __value || true
  printf -v "$__var" '%s' "${__value:-$__default}"
}

ui_cancel() {
  echo "Installer cancelled."
  exit 1
}

UI_ALLOW_BACK=0
UI_ROWS=24
UI_COLS=80

_ui_fail() {
  if [[ "${UI_ALLOW_BACK:-0}" == 1 ]]; then
    return 1
  fi
  ui_cancel
}

box_width() {
  local w=$((UI_COLS - 4))
  ((w > 74)) && w=74
  ((w < 46)) && w=46
  printf '%s' "$w"
}

text_rows() {
  local text=$1 width=$2
  printf '%s\n' "$text" | awk -v w="$((width - 4))" '
    { n = length($0); total += (n == 0 ? 1 : int((n + w - 1) / w)) }
    END { print (total ? total : 1) }'
}

box_rows_max() {
  local m=$((UI_ROWS - 2))
  ((m < 9)) && m=9
  printf '%s' "$m"
}

fit_text() {
  local text=$1 width=$2 avail=$3 inner
  ((avail < 1)) && avail=1
  inner=$((width - 4))
  ((inner < 8)) && inner=8
  while (($(text_rows "$text" "$width") > avail)) && [[ "$text" == *$'\n'* ]]; do
    text=${text%$'\n'*}
  done
  if (($(text_rows "$text" "$width") > avail)); then
    text=$(printf '%s' "$text" | head -c $((inner * avail)))
  fi
  printf '%s' "$text"
}

# dialog draws the widget on stdout and returns the typed value on stderr.
# Callers use DIALOG_OUT. The widget must go to /dev/tty (not a pipe).
dialog_read() {
  local tmp rc
  DIALOG_OUT=""
  tmp=$(mktemp "${TMPDIR:-/tmp}/tabby-dialog.XXXXXX") || return 1
  set +e
  if [[ -c /dev/tty ]] && { true >/dev/tty; } 2>/dev/null; then
    dialog --backtitle "$BACKTITLE" "$@" 2> "$tmp" >/dev/tty
  else
    dialog --backtitle "$BACKTITLE" "$@" 2> "$tmp"
  fi
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    rm -f "$tmp"
    return "$rc"
  fi
  DIALOG_OUT=$(cat "$tmp")
  rm -f "$tmp"
  return 0
}

# dialog (ncurses) if available, else whiptail, else printed how-to + read.
tui_cmd() {
  if need_cmd dialog; then
    TUI=dialog
  elif need_cmd whiptail; then
    TUI=whiptail
  else
    TUI=""
  fi
}

# Standard dialog colours. Same palette as tsos-installer.sh.
write_dialogrc() {
  local f="${TMPDIR:-/tmp}/tabby-dialogrc"
  cat >"$f" <<'EOF'
use_shadow = ON
use_colors = ON
use_scrollbar = ON
visit_items = OFF
aspect = 0
# Tab in a form jumps to OK by default and skips later fields. form_NEXT
# walks password then verify then the buttons, like a normal dialog.
bindkey formfield TAB form_NEXT
bindkey formbox TAB form_NEXT
bindkey formfield BTAB form_prev
bindkey formbox BTAB form_prev
screen_color = (CYAN,BLUE,ON)
shadow_color = (BLACK,BLACK,ON)
dialog_color = (BLACK,WHITE,OFF)
title_color = (BLUE,WHITE,ON)
border_color = (WHITE,WHITE,ON)
border2_color = (BLACK,WHITE,OFF)
gauge_color = (BLUE,WHITE,ON)
button_active_color = (WHITE,BLUE,ON)
button_inactive_color = (BLACK,WHITE,OFF)
button_key_active_color = (WHITE,BLUE,ON)
button_key_inactive_color = (RED,WHITE,OFF)
button_label_active_color = (YELLOW,BLUE,ON)
button_label_inactive_color = (BLACK,WHITE,ON)
menubox_color = (BLACK,WHITE,OFF)
# Shaded top/left, lit bottom/right: the list sits in a sunken well
# (menuconfig look). The outer box keeps the raised edge above.
menubox_border_color = (BLACK,WHITE,OFF)
menubox_border2_color = (WHITE,WHITE,ON)
item_color = (BLACK,WHITE,OFF)
item_selected_color = (WHITE,BLUE,ON)
tag_color = (BLUE,WHITE,ON)
tag_selected_color = (YELLOW,BLUE,ON)
tag_key_color = (RED,WHITE,OFF)
tag_key_selected_color = (RED,BLUE,ON)
check_color = (BLACK,WHITE,OFF)
check_selected_color = (WHITE,BLUE,ON)
form_active_text_color = (WHITE,BLUE,ON)
form_text_color = (WHITE,CYAN,ON)
form_item_readonly_color = (CYAN,WHITE,ON)
inputbox_color = (BLACK,WHITE,OFF)
inputbox_border_color = (BLACK,WHITE,OFF)
inputbox_border2_color = (BLACK,WHITE,OFF)
searchbox_color = (BLACK,WHITE,OFF)
searchbox_title_color = (BLUE,WHITE,ON)
searchbox_border_color = (WHITE,WHITE,ON)
position_indicator_color = (BLUE,WHITE,ON)
uarrow_color = (GREEN,WHITE,ON)
darrow_color = (GREEN,WHITE,ON)
itemhelp_color = (WHITE,BLACK,OFF)
EOF
  export DIALOGRC="$f"
  # A malformed theme must not prevent the installer from opening.
  if command -v dialog >/dev/null 2>&1 && ! dialog --version >/dev/null 2>&1; then
    unset DIALOGRC
    printf 'warning: custom dialog theme was rejected; using the built-in theme\n' >&2
  fi
}

ensure_dialog() {
  tui_cmd
  if [[ -n "$TUI" ]]; then
    [[ "$TUI" == dialog ]] && write_dialogrc
    return 0
  fi
  if [[ "$INTERACTIVE" -eq 0 ]] || [[ ! -t 0 ]]; then
    return 0
  fi
  echo "==> Installing dialog (ncurses menus and how-to screens)..."
  sudo pacman -S --needed --noconfirm dialog || true
  tui_cmd
  [[ "$TUI" == dialog ]] && write_dialogrc
}

dialog_tty() {
  if [[ -c /dev/tty ]] && { true >/dev/tty; } 2>/dev/null; then
    dialog "$@" >/dev/tty
  else
    dialog "$@"
  fi
}

ui_msg() {
  local title="$1"
  local text="$2"
  local height="${3:-}"
  local width="${4:-}"
  [[ -n "$width" ]] || width=$(box_width)
  local max=$(($(box_rows_max) - 6))
  text=$(fit_text "$text" "$width" "$max")
  local need=$(($(text_rows "$text" "$width") + 6))
  if [[ -z "$height" ]] || ((height > need)); then
    height=$need
  fi
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    dialog_tty --backtitle "$BACKTITLE" --title "$title" --msgbox "$text" "$height" "$width" || { _ui_fail; return 1; }
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    whiptail --backtitle "$BACKTITLE" --title "$title" --msgbox "$text" "$height" "$width" || { _ui_fail; return 1; }
  else
    echo
    echo "=== $title ==="
    echo "$text"
    echo
  fi
}

ui_input() {
  local title="$1"
  local text="$2"
  local default="$3"
  local out=""
  local width height
  width=$(box_width)
  text=$(fit_text "$text" "$width" "$(($(box_rows_max) - 7))")
  height=$(($(text_rows "$text" "$width") + 7))
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    dialog_read --title "$title" --inputbox "$text" "$height" "$width" "$default" || { _ui_fail; return 1; }
    out=$DIALOG_OUT
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    out="$(whiptail --backtitle "$BACKTITLE" --title "$title" --inputbox "$text" "$height" "$width" "$default" 3>&1 1>&2 2>&3)" || { _ui_fail; return 1; }
  else
    {
      echo
      echo "=== $title ==="
      echo "$text"
      echo
    } >&2
    read -r -p "Value [${default}]: " out || true
    out="${out:-$default}"
  fi
  printf '%s' "$out"
}

ui_menu() {
  local title="$1"
  local text="$2"
  shift 2
  local out=""
  local width height list max
  width=$(box_width)
  max=$(box_rows_max)
  list=$(($# / 2))
  ((list > 12)) && list=12
  local avail=$((max - list - 7))
  if ((avail < 2)); then
    list=$((max - 9))
    ((list < 2)) && list=2
    avail=$((max - list - 7))
  fi
  text=$(fit_text "$text" "$width" "$avail")
  height=$(($(text_rows "$text" "$width") + list + 7))
  ((height > max)) && height=$max
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    dialog_read --title "$title" --menu "$text" "$height" "$width" "$list" "$@" || { _ui_fail; return 1; }
    out=$DIALOG_OUT
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    out="$(whiptail --backtitle "$BACKTITLE" --title "$title" --menu "$text" "$height" "$width" "$list" "$@" 3>&1 1>&2 2>&3)" || { _ui_fail; return 1; }
  else
    {
      echo
      echo "=== $title ==="
      echo "$text"
      echo
    } >&2
    local i=1 tag
    local tags=()
    while (($#)); do
      tag="$1"
      tags+=("$tag")
      printf "  %s) %s — %s\n" "$i" "$tag" "$2" >&2
      shift 2
      i=$((i + 1))
    done
    local choice=""
    read -r -p "Choice [1]: " choice || true
    choice="${choice:-1}"
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#tags[@]})); then
      out="${tags[$((choice - 1))]}"
    else
      out="$choice"
    fi
  fi
  printf '%s' "$out"
}

ui_checklist() {
  local title="$1"
  local text="$2"
  shift 2
  local out=""
  local width height list max
  width=$(box_width)
  max=$(box_rows_max)
  list=$(($# / 3))
  ((list > 10)) && list=10
  local avail=$((max - list - 7))
  if ((avail < 2)); then
    list=$((max - 9))
    ((list < 2)) && list=2
    avail=$((max - list - 7))
  fi
  text=$(fit_text "$text" "$width" "$avail")
  height=$(($(text_rows "$text" "$width") + list + 7))
  ((height > max)) && height=$max
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    dialog_read --title "$title" --checklist "$text" "$height" "$width" "$list" "$@" || { _ui_fail; return 1; }
    out=$DIALOG_OUT
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    out="$(whiptail --backtitle "$BACKTITLE" --title "$title" --checklist "$text" "$height" "$width" "$list" "$@" 3>&1 1>&2 2>&3)" || { _ui_fail; return 1; }
  else
    {
      echo
      echo "=== $title ==="
      echo "$text"
      echo
    } >&2
    local i=1 tag state
    local tags=()
    local defaults=()
    while (($#)); do
      tag="$1"
      tags+=("$tag")
      state="$3"
      printf "  %s) [%s] %s — %s\n" "$i" "$([[ "$state" == on ]] && echo x || echo ' ')" "$tag" "$2" >&2
      [[ "$state" == on ]] && defaults+=("$tag")
      shift 3
      i=$((i + 1))
    done
    local choice=""
    local joined="${defaults[*]}"
    joined="${joined// /,}"
    read -r -p "Comma-separated ids [${joined}]: " choice || true
    if [[ -z "$choice" ]]; then
      out="${defaults[*]}"
    else
      out="${choice//,/ }"
    fi
  fi
  local -a chosen=()
  eval "chosen=(${out})"
  local IFS=,
  printf '%s' "${chosen[*]}"
}

ui_yesno() {
  local title="$1"
  local text="$2"
  local default_yes="${3:-1}"
  local width height rc
  width=$(box_width)
  text=$(fit_text "$text" "$width" "$(($(box_rows_max) - 6))")
  height=$(($(text_rows "$text" "$width") + 6))
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    local extra=()
    [[ "$default_yes" -eq 0 ]] && extra=(--defaultno)
    # Yes=0, No=1. Capture under set +e so No is not an installer crash.
    set +e
    dialog_tty --backtitle "$BACKTITLE" --title "$title" "${extra[@]}" --yesno "$text" "$height" "$width"
    rc=$?
    set -e
    if [[ "$rc" -eq 255 ]]; then
      if [[ "${UI_ALLOW_BACK:-0}" == 1 ]]; then
        return 2
      fi
      ui_cancel
    fi
    return "$rc"
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    local extra=()
    [[ "$default_yes" -eq 0 ]] && extra=(--defaultno)
    set +e
    whiptail --backtitle "$BACKTITLE" --title "$title" "${extra[@]}" --yesno "$text" "$height" "$width"
    rc=$?
    set -e
    if [[ "$rc" -eq 255 ]]; then
      if [[ "${UI_ALLOW_BACK:-0}" == 1 ]]; then
        return 2
      fi
      ui_cancel
    fi
    return "$rc"
  else
    local yn="Y/n"
    [[ "$default_yes" -eq 0 ]] && yn="y/N"
    echo
    echo "=== $title ==="
    echo "$text"
    echo
    local ans=""
    read -r -p "Continue? [$yn]: " ans || true
    ans="${ans:-$([[ "$default_yes" -eq 1 ]] && echo y || echo n)}"
    [[ "$ans" =~ ^[Yy] ]]
  fi
}


need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

# ffmpeg depends on virtual "jack". Desktop Arch often has pipewire-jack instead.
package_missing() {
  local p="$1"
  if pacman -Q "$p" >/dev/null 2>&1; then
    return 1
  fi
  if [[ "$p" == jack2 ]] && pacman -Q pipewire-jack >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# Free GiB on the filesystem that will hold a path that may not exist yet.
free_gib() {
  local p="$1"
  while [[ -n "$p" && ! -d "$p" ]]; do
    p="$(dirname "$p")"
  done
  df -P "$p" 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}'
}

port_in_use() {
  local port="$1"
  if need_cmd ss; then
    ss -ltn 2>/dev/null | awk -v p=":$port" '$4 ~ p"$" {found=1} END {exit !found}'
  else
    return 1
  fi
}

# Poll until GET /health reports status healthy (LLM reload ~65s).
wait_for_tabby_health() {
  local port="${TABBY_NETWORK_PORT:-5000}"
  local url="http://127.0.0.1:${port}/health"
  local tries="${TABBY_HEALTH_TRIES:-180}"
  local i body
  for ((i = 1; i <= tries; i++)); do
    body="$(curl -sf "$url" 2>/dev/null || true)"
    if [[ "$body" == *'"status":"healthy"'* || "$body" == *'"status": "healthy"'* ]]; then
      echo "API healthy at $url (${i}s)." >> "${INSTALL_LOG:-/dev/null}"
      append_update_log "API healthy at $url (${i}s)."
      return 0
    fi
    sleep 1
  done
  echo "Timed out after ${tries}s waiting for $url" >> "${INSTALL_LOG:-/dev/null}"
  append_update_log "Timed out after ${tries}s waiting for $url"
  [[ -n "$body" ]] && echo "Last body: $body" >> "${INSTALL_LOG:-/dev/null}"
  return 1
}

load_tabby_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  # shellcheck disable=SC1090
  set -a
  . "$env_file"
  set +a
}

INSTALL_LOG=""
GAUGE_WATCH_PID=""
GAUGE_DIR=""
GAUGE_MODE=""
GAUGE_SAVED_FD=""
SUDO_KEEPALIVE_PID=""
INSTALL_FAILED=0

# tsos-installer used to create this log as root. Reclaim it so later
# appends (and progress_start's truncate) do not die with Permission denied.
adopt_install_log() {
  local dest="$1"
  if [[ -e "$dest" && ! -w "$dest" ]]; then
    sudo -n chown "$USER:$USER" "$dest" 2>/dev/null || true
    sudo -n chmod u+rw "$dest" 2>/dev/null || true
  fi
  if [[ -e "$dest" && ! -w "$dest" ]]; then
    return 1
  fi
  mkdir -p "$(dirname "$dest")" 2>/dev/null || true
  touch "$dest" 2>/dev/null
}

fmt_elapsed() {
  local s=$1
  if ((s < 60)); then
    printf '%ss' "$s"
  elif ((s < 3600)); then
    printf '%dm %02ds' $((s / 60)) $((s % 60))
  else
    printf '%dh %02dm' $((s / 3600)) $(((s % 3600) / 60))
  fi
}

work_term() {
  case "${TERM:-}" in
    "" | dumb | unknown) export TERM=linux ;;
  esac
  stty -tostop </dev/tty >/dev/null 2>&1 || true
  local size
  size=$(stty size </dev/tty 2>/dev/null || true)
  if [[ "$size" =~ ^[0-9]+[[:space:]]+[0-9]+$ ]]; then
    UI_ROWS=${size%%[[:space:]]*}
    UI_COLS=${size##*[[:space:]]}
  fi
  ((UI_ROWS >= 14)) || UI_ROWS=24
  ((UI_COLS >= 50)) || UI_COLS=80
}

install_log_snippet() {
  local lines=$1 width=$2
  [[ -f "$INSTALL_LOG" ]] || return 0
  tail -n 80 "$INSTALL_LOG" 2>/dev/null \
    | tr '\r' '\n' \
    | sed -e 's/\x1B\[[0-9;?]*[a-zA-Z]//g' -e 's/^XXX$//' -e 's/\\Z[0-7bBrRuUn]//g' \
    | grep -v '^[[:space:]]*$' \
    | tail -n "$lines" \
    | cut -c1-"$width" || true
}

ui_pad() {
  local s=$1 n=$2
  s=${s//$'\r'/ }
  s=${s//$'\t'/ }
  s=${s//$'\n'/ }
  if (( n > 0 && ${#s} > n )); then
    s=${s:0:n}
  fi
  printf '%-*s' "$n" "$s"
}

gauge_steps() {
  printf '%s\n' \
    '0|Setup|' \
    '16|Python|' \
    '22|Stack|' \
    '40|API|' \
    '84|Models|' \
    '94|Finish|'
}

gauge_step_index() {
  local heading=$1 pct=$2
  local minpct short match i=0 best=0
  while IFS='|' read -r minpct short match; do
    [[ -n "$short" ]] || continue
    if [[ -n "$match" && "$heading" == *"$match"* ]]; then
      printf '%s' "$i"
      return 0
    fi
    i=$((i + 1))
  done < <(gauge_steps)
  i=0
  while IFS='|' read -r minpct short match; do
    [[ -n "$short" ]] || continue
    if [[ "$pct" =~ ^[0-9]+$ ]] && ((pct >= minpct)); then
      best=$i
    fi
    i=$((i + 1))
  done < <(gauge_steps)
  printf '%s' "$best"
}

# ---------------------------------------------------------------------------
# Installing page. dialog --gauge cannot draw an inner box and drops text
# past 1024 bytes, so this page is painted straight onto the console in the
# lxdialog (menuconfig) style the question screens already use: blue screen
# with the backtitle and its rule, a white box with a light top-left / dark
# bottom-right edge and a black shadow, coloured step chips, the live log in
# a sunken well, and a blue meter. Frames are absolute cursor moves, so the
# page is redrawn in place without flicker.
# ---------------------------------------------------------------------------
PAGE_ROWS=24
PAGE_COLS=80
PAGE_H=0
PAGE_W=0
PAGE_Y=0
PAGE_X=0
PAGE_LOG_N=0
PAGE_TITLE="Installing"
PAGE_BUF=""
PAGE_G=""
PAGE_P=""
PAGE_V=0
PAGE_UTF8=""

# Same pairs as the dialogrc above (SGR: attr;fg;bg).
P_SCREEN=$'\033[0;36;44m'   # screen_color
P_BACK=$'\033[1;36;44m'     # backtitle text
P_DLG=$'\033[0;30;47m'      # dialog_color
P_LT=$'\033[1;37;47m'       # border_color: lit edge
P_DK=$'\033[0;30;47m'       # border2_color: shaded edge
P_TITLE=$'\033[1;34;47m'    # title_color
P_SHADOW=$'\033[0;30;40m'   # shadow_color
P_DONE=$'\033[0;32;47m'     # finished chips (uarrow green)
P_CUR=$'\033[1;37;44m'      # current chip (item_selected)
P_CUR_ALT=$'\033[1;34;47m'  # current chip, off pulse
P_HEAD=$'\033[1;34;47m'     # step heading
P_DIM=$'\033[0;34;47m'      # times, spinner
P_KEY=$'\033[0;31;47m'      # hotkey red (Ctrl+C)
P_BAR_ON=$'\033[1;37;44m'   # gauge_color, filled
P_BAR_OFF=$'\033[1;34;47m'  # gauge_color, empty

# A UTF-8 console ignores the VT100 graphics set, so draw the frame with the
# Unicode box characters there; elsewhere switch G0 to graphics (\e(0), the
# same thing ncurses does in a C locale).
page_utf8() {
  if [[ -z "$PAGE_UTF8" ]]; then
    local l="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}"
    PAGE_UTF8=0
    case "${l,,}" in
      *utf-8* | *utf8*) PAGE_UTF8=1 ;;
      *)
        if [[ "${TERM:-}" == linux* ]] &&
          [[ "$(cat /sys/module/vt/parameters/default_utf8 2>/dev/null)" == 1 ]]; then
          PAGE_UTF8=1
        fi
        ;;
    esac
  fi
  ((PAGE_UTF8))
}

# page_glyph tl|tr|bl|br|v -> PAGE_G
page_glyph() {
  local u a
  case $1 in
    tl) u='┌' a=l ;;
    tr) u='┐' a=k ;;
    bl) u='└' a=m ;;
    br) u='┘' a=j ;;
    v) u='│' a=x ;;
    *) u='─' a=q ;;
  esac
  if page_utf8; then
    PAGE_G=$u
  else
    PAGE_G=$'\033(0'"$a"$'\033(B'
  fi
}

# page_hline n -> PAGE_G (n horizontal line cells)
page_hline() {
  local n=$1 s
  ((n < 0)) && n=0
  printf -v s '%*s' "$n" ''
  if page_utf8; then
    PAGE_G=${s// /─}
  else
    PAGE_G=$'\033(0'"${s// /q}"$'\033(B'
  fi
}

# page_pad text n -> PAGE_P (exactly n cells, ASCII in, control chars out)
page_pad() {
  local s=$1 n=$2
  s=${s//$'\r'/ }
  s=${s//$'\t'/ }
  s=${s//$'\n'/ }
  ((n > 0 && ${#s} > n)) && s=${s:0:n}
  printf -v PAGE_P '%-*s' "$n" "$s"
}

# Box size and place from the console size. Rows 0-1 hold the backtitle and
# its rule; the box is centred below like dialog does, shadow included.
page_layout() {
  PAGE_ROWS=$1
  PAGE_COLS=$2
  PAGE_H=$((PAGE_ROWS - 5))
  ((PAGE_H > 24)) && PAGE_H=24
  ((PAGE_H < 15)) && PAGE_H=15
  PAGE_W=$((PAGE_COLS - 6))
  ((PAGE_W > 96)) && PAGE_W=96
  ((PAGE_W < 46)) && PAGE_W=46
  # 2 edges + 2 info + chips + heading + well edges (2) + gap + meter (3).
  PAGE_LOG_N=$((PAGE_H - 12))
  PAGE_Y=$(((PAGE_ROWS - PAGE_H - 1) / 2))
  ((PAGE_Y < 2)) && PAGE_Y=2
  PAGE_X=$(((PAGE_COLS - PAGE_W - 2) / 2))
  ((PAGE_X < 0)) && PAGE_X=0
  return 0
}

# Append a cursor move to screen row/col (0-based) to the frame.
page_at() {
  PAGE_BUF+=$'\033['"$(($1 + 1));$(($2 + 1))H"
}

# Interior row r of the box: lit left edge, one cell margin, iw cells of
# content (colour codes allowed, width already exact), margin, shaded edge.
page_inner() {
  local r=$1 content=$2
  page_glyph v
  page_at $((PAGE_Y + r)) "$PAGE_X"
  PAGE_BUF+="$P_LT$PAGE_G$P_DLG $content$P_DLG $P_DK$PAGE_G"
}

# page_chips iw heading pct ticks spin -> PAGE_P (coded), PAGE_V (cells).
# Finished steps green, the current one highlighted like a selected menu
# item and pulsing, the rest plain.
page_chips() {
  local iw=$1 heading=$2 pct=$3 ticks=$4 spin=$5
  local cur i=0 piece mark short minpct match
  PAGE_P=""
  PAGE_V=0
  cur=$(gauge_step_index "$heading" "$pct")
  while IFS='|' read -r minpct short match; do
    [[ -n "$short" ]] || continue
    if ((i < cur)); then
      mark="[x]"
    elif ((i == cur)); then
      mark="[${spin}]"
    else
      mark="[ ]"
    fi
    piece="$mark $short"
    if ((PAGE_V > 0)) && ((PAGE_V + 2 + ${#piece} > iw)); then
      break
    fi
    if ((PAGE_V > 0)); then
      PAGE_P+="$P_DLG  "
      PAGE_V=$((PAGE_V + 2))
    fi
    if ((i < cur)); then
      PAGE_P+="$P_DONE$piece"
    elif ((i == cur)); then
      if ((ticks % 2 == 0)); then
        PAGE_P+="$P_CUR$piece"
      else
        PAGE_P+="$P_CUR_ALT$piece"
      fi
    else
      PAGE_P+="$P_DLG$piece"
    fi
    PAGE_V=$((PAGE_V + ${#piece}))
    i=$((i + 1))
  done < <(gauge_steps)
  PAGE_P+="$P_DLG"
}

# Static parts: blue screen, backtitle with rule, the box shadow. Cursor off,
# echo off so stray keys do not land on the page.
page_open() {
  local backtitle=$1 r s
  page_layout "${2:-$PAGE_ROWS}" "${3:-$PAGE_COLS}"
  stty -echo </dev/tty >/dev/null 2>&1 || true
  PAGE_BUF=$'\033[?25l'"$P_SCREEN"$'\033[H\033[2J'
  # Paint the blue screen cell by cell: not every terminal erases with the
  # current background colour.
  printf -v s '%*s' "$PAGE_COLS" ''
  for ((r = 0; r < PAGE_ROWS; r++)); do
    page_at "$r" 0
    PAGE_BUF+="$s"
  done
  page_at 0 1
  PAGE_BUF+="$P_BACK$backtitle"
  page_hline $((PAGE_COLS - 2))
  page_at 1 1
  PAGE_BUF+="$P_SCREEN$PAGE_G"
  for ((r = 1; r <= PAGE_H; r++)); do
    page_at $((PAGE_Y + r)) $((PAGE_X + PAGE_W))
    PAGE_BUF+="$P_SHADOW  "
  done
  printf -v s '%*s' "$PAGE_W" ''
  page_at $((PAGE_Y + PAGE_H)) $((PAGE_X + 2))
  PAGE_BUF+="$P_SHADOW$s"
  printf '%s' "$PAGE_BUF"
}

# Swallow keys typed while the page was up so they do not answer the next
# dialog, then cursor back on. restore_tty puts the terminal right after.
page_close() {
  local junk
  if stty -icanon min 0 time 0 </dev/tty >/dev/null 2>&1; then
    while IFS= read -r -n 512 -t 0.05 junk </dev/tty 2>/dev/null && [[ -n "$junk" ]]; do :; done
  fi
  printf '\033[?25h\033[0m' >/dev/tty 2>/dev/null || true
}

# page_frame heading pct step_elapsed total_elapsed spin ticks info1 info2 log
# Builds one full repaint of the box into PAGE_BUF.
page_frame() {
  local heading=$1 pct=$2 step_el=$3 total_el=$4 spin=$5 ticks=$6
  local info1=$7 info2=$8 logtext=${9:-}
  local iw=$((PAGE_W - 4)) t a b s right room i line r bw pos filled
  PAGE_BUF=""

  # Top edge with the title: lit corner and rule, shaded far corner.
  t=" $PAGE_TITLE "
  a=$(((PAGE_W - 2 - ${#t}) / 2))
  b=$((PAGE_W - 2 - ${#t} - a))
  page_glyph tl
  s="$P_LT$PAGE_G"
  page_hline "$a"
  s+="$PAGE_G$P_TITLE$t$P_LT"
  page_hline "$b"
  s+="$PAGE_G"
  page_glyph tr
  s+="$P_DK$PAGE_G"
  page_at "$PAGE_Y" "$PAGE_X"
  PAGE_BUF+="$s"

  # Info: what is being installed, where the log is, total time.
  page_pad "$info1" "$iw"
  page_inner 1 "$P_DLG$PAGE_P"
  right=$total_el
  page_pad "$info2" $((iw - ${#right} - 1))
  s=${PAGE_P/Ctrl+C/$P_KEY"Ctrl+C"$P_DLG}
  page_inner 2 "$P_DLG$s $P_DIM$right"

  # Step chips, then the current step with its own time and a spinner.
  page_chips "$iw" "$heading" "$pct" "$ticks" "$spin"
  printf -v s '%*s' $((iw - PAGE_V)) ''
  page_inner 3 "$PAGE_P$s"
  right="$step_el  $spin"
  room=$((iw - ${#right} - 1))
  ((room < 10)) && room=10
  page_pad "$heading" "$room"
  page_inner 4 "$P_HEAD$PAGE_P $P_DIM$right"

  # Sunken well: shaded top/left, lit bottom/right (dialog's menubox).
  page_glyph tl
  s="$P_DK$PAGE_G"
  page_hline $((iw - 2))
  s+="$PAGE_G"
  page_glyph tr
  s+="$P_LT$PAGE_G"
  page_inner 5 "$s"
  page_glyph v
  t=$PAGE_G
  for ((i = 0; i < PAGE_LOG_N; i++)); do
    line=""
    if [[ -n "$logtext" ]]; then
      if [[ "$logtext" == *$'\n'* ]]; then
        line=${logtext%%$'\n'*}
        logtext=${logtext#*$'\n'}
      else
        line=$logtext
        logtext=""
      fi
    fi
    page_pad "$line" $((iw - 4))
    page_inner $((6 + i)) "$P_DK$t$P_DLG $PAGE_P $P_LT$t"
  done
  r=$((6 + PAGE_LOG_N))
  page_glyph bl
  s="$P_DK$PAGE_G"
  page_hline $((iw - 2))
  s+="$P_LT$PAGE_G"
  page_glyph br
  s+="$PAGE_G"
  page_inner "$r" "$s"

  # Gap, then the meter in a raised box like dialog's gauge.
  printf -v s '%*s' "$iw" ''
  page_inner $((r + 1)) "$P_DLG$s"
  page_glyph tl
  s="$P_LT$PAGE_G"
  page_hline $((iw - 2))
  s+="$PAGE_G"
  page_glyph tr
  s+="$P_DK$PAGE_G"
  page_inner $((r + 2)) "$s"
  bw=$((iw - 2))
  printf -v s '%*s' "$bw" ''
  printf -v t '%3d%%' "$pct"
  pos=$((bw / 2 - 2))
  ((pos < 0)) && pos=0
  s="${s:0:pos}${t}${s:pos+4}"
  s=${s:0:bw}
  filled=$((pct * bw / 100))
  ((filled > bw)) && filled=$bw
  page_glyph v
  page_inner $((r + 3)) "$P_LT$PAGE_G$P_BAR_ON${s:0:filled}$P_BAR_OFF${s:filled}$P_DK$PAGE_G"
  page_glyph bl
  s="$P_LT$PAGE_G$P_DK"
  page_hline $((iw - 2))
  s+="$PAGE_G"
  page_glyph br
  s+="$PAGE_G"
  page_inner $((r + 4)) "$s"

  # Bottom edge: lit corner, shaded rule and far corner.
  page_glyph bl
  s="$P_LT$PAGE_G$P_DK"
  page_hline $((PAGE_W - 2))
  s+="$PAGE_G"
  page_glyph br
  s+="$PAGE_G"
  page_at $((PAGE_Y + PAGE_H - 1)) "$PAGE_X"
  PAGE_BUF+="$s"
}

# Repaint the install page so a long pip / pacman / download stays visibly
# alive. Runs in the background and writes frames straight to /dev/tty.
watch_progress_ui() {
  local stop="$1"
  local pct heading last="" step_t=$SECONDS ticks=0 spin='|/-\' ch
  local info1 info2 t0=$SECONDS
  info1="Installing tabbyapi-stack in ${DEST:-tabbyapi-stack}."
  [[ "${UPDATE_MODE:-0}" -eq 1 ]] && info1="Updating tabbyapi-stack in ${DEST:-tabbyapi-stack}."
  info2="Full log: ${INSTALL_LOG}   Ctrl+C cancels."
  while [[ ! -f "$stop" ]]; do
    pct=0
    heading="Working..."
    [[ -f "$GAUGE_DIR/pct" ]] && pct=$(cat "$GAUGE_DIR/pct" 2>/dev/null)
    [[ -f "$GAUGE_DIR/heading" ]] && heading=$(cat "$GAUGE_DIR/heading" 2>/dev/null)
    [[ "$pct" =~ ^[0-9]+$ ]] || pct=0
    ((pct > 100)) && pct=100
    ticks=$((ticks + 1))
    if [[ "$heading" != "$last" ]]; then
      last=$heading
      step_t=$SECONDS
    fi
    ch=${spin:$((ticks % 4)):1}
    page_frame "$heading" "$pct" "$(fmt_elapsed $((SECONDS - step_t)))" "$(fmt_elapsed $((SECONDS - t0)))" \
      "$ch" "$ticks" "$info1" "$info2" \
      "$(install_log_snippet "$PAGE_LOG_N" "$((PAGE_W - 8))")"
    printf '%s' "$PAGE_BUF" >/dev/tty 2>/dev/null || break
    sleep 0.5
  done
}

redirect_work_output() {
  [[ -z "${GAUGE_SAVED_FD:-}" ]] || return 0
  [[ -n "$INSTALL_LOG" && "$INSTALL_LOG" != /dev/null ]] || return 0
  exec 4>&1 5>&2
  GAUGE_SAVED_FD=1
  exec >>"$INSTALL_LOG" 2>&1
}

restore_work_output() {
  [[ -n "${GAUGE_SAVED_FD:-}" ]] || return 0
  exec 1>&4 2>&5
  exec 4>&- 5>&-
  GAUGE_SAVED_FD=""
}

progress_start() {
  mkdir -p "$DEST"
  INSTALL_LOG="$DEST/tabby-install.log"
  if ! adopt_install_log "$INSTALL_LOG"; then
    INSTALL_LOG="/tmp/tabby-install-${USER}.log"
    adopt_install_log "$INSTALL_LOG" || INSTALL_LOG="/dev/null"
  fi
  if [[ "${TABBY_NVIDIA_REBOOT_DONE:-}" == 1 && -f "$INSTALL_LOG" ]]; then
    {
      echo
      echo "tabbyapi-stack install resume $(date -Iseconds) (after NVIDIA reboot)"
      echo "dest=$DEST tabby=$DEST_TABBY comfy=$DEST_COMFY"
      echo "cache=${WIN_ROOT:-} models=$MODEL_SET api=$API_URL"
      echo
    } >> "$INSTALL_LOG"
  else
    {
      echo "tabbyapi-stack install $(date -Iseconds)"
      echo "dest=$DEST tabby=$DEST_TABBY comfy=$DEST_COMFY"
      echo "cache=${WIN_ROOT:-} models=$MODEL_SET api=$API_URL"
      echo
    } > "$INSTALL_LOG"
  fi
  if [[ "${TABBY_INSTALL_VERBOSE:-}" == 1 ]]; then
    GAUGE_MODE="verbose"
    return 0
  fi
  if [[ "${TABBY_NESTED_UI:-}" == 1 ]]; then
    GAUGE_MODE="log"
    return 0
  fi

  # After the question screens, keep every command inside this UI (or the
  # log). Do not drop back to a raw pacman/pip dump on the console.
  work_term
  redirect_work_output

  # The question screens ran in the TUI, so the work gets the install page:
  # painted by a background watcher on /dev/tty, same palette as the dialogs.
  if [[ "${USE_TUI:-0}" -eq 1 && -c /dev/tty ]] && { true >/dev/tty; } 2>/dev/null; then
    PAGE_TITLE="Installing tabbyapi-stack"
    [[ "$UPDATE_MODE" -eq 1 ]] && PAGE_TITLE="Updating tabbyapi-stack"
    GAUGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tabby-gauge.XXXXXX")"
    printf '%s\n' 0 >"$GAUGE_DIR/pct"
    printf '%s\n' "Starting..." >"$GAUGE_DIR/heading"
    page_open "$BACKTITLE" "$UI_ROWS" "$UI_COLS" >/dev/tty
    watch_progress_ui "$GAUGE_DIR/stop" &
    GAUGE_WATCH_PID=$!
    GAUGE_MODE="page"
    return 0
  fi
  if [[ -c /dev/tty ]]; then
    GAUGE_MODE="text"
    return 0
  fi
  GAUGE_MODE="log"
}

append_update_log() {
  if [[ -n "${TABBY_UPDATE_LOG:-}" ]]; then
    printf '%s\n' "$1" >> "$TABBY_UPDATE_LOG"
  fi
}

progress() {
  local pct="$1" msg="$2"
  if [[ -n "$INSTALL_LOG" ]]; then
    printf '%s\n' "==> [$pct%] $msg" >> "$INSTALL_LOG"
  fi
  append_update_log "==> [$pct%] $msg"
  case "${GAUGE_MODE:-}" in
    page)
      if [[ -n "${GAUGE_DIR:-}" ]]; then
        printf '%s\n' "$pct" >"$GAUGE_DIR/pct"
        printf '%s\n' "$msg" >"$GAUGE_DIR/heading"
      fi
      ;;
    text)
      local fill=$((pct / 2))
      printf '\r\033[K[%s%s] %3d%%  %s' \
        "$(printf '%*s' "$fill" '' | tr ' ' '#')" \
        "$(printf '%*s' $((50 - fill)) '')" \
        "$pct" "$msg" >/dev/tty
      ;;
    verbose)
      echo "==> [$pct%] $msg"
      ;;
  esac
}

restore_tty() {
  # Nested under tsos-installer: the outer dialog owns /dev/tty. rmcup / stty
  # sane here drops that UI onto the raw console.
  [[ "${TABBY_NESTED_UI:-}" == 1 ]] && return 0
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
  if [[ -n "${SUDO_KEEPALIVE_PID:-}" ]]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
  if [[ -n "${GAUGE_DIR:-}" ]]; then
    touch "$GAUGE_DIR/stop" 2>/dev/null || true
  fi
  if [[ -n "${GAUGE_WATCH_PID:-}" ]]; then
    kill "$GAUGE_WATCH_PID" 2>/dev/null || true
    wait "$GAUGE_WATCH_PID" 2>/dev/null || true
    GAUGE_WATCH_PID=""
  fi
  case "${GAUGE_MODE:-}" in
    page)
      page_close
      rm -rf "${GAUGE_DIR:-}"
      ;;
    text)
      printf '\n' >/dev/tty 2>/dev/null || true
      ;;
  esac
  restore_work_output
  restore_tty
  GAUGE_MODE=""
  GAUGE_DIR=""
}

progress_fail() {
  local rc="${1:-1}"
  local tail_txt=""
  INSTALL_FAILED=1
  progress_stop
  [[ -n "$INSTALL_LOG" && -f "$INSTALL_LOG" ]] && tail_txt=$(tail -n 24 "$INSTALL_LOG")
  if [[ "${USE_TUI:-0}" -eq 1 && "$TUI" == dialog ]]; then
    dialog_tty --backtitle "$BACKTITLE" --title "Install failed" \
      --msgbox "The install stopped (exit ${rc}).

${tail_txt}

Full log:
  ${INSTALL_LOG}" \
      22 74 || true
  fi
  echo
  echo "Install failed. Last lines of ${INSTALL_LOG:-the log}:"
  [[ -n "$tail_txt" ]] && printf '%s\n' "$tail_txt"
  echo
  echo "Full log: ${INSTALL_LOG:-}"
  append_update_log "Install failed. Full log: ${INSTALL_LOG:-}"
  exit "$rc"
}

run_quiet() {
  if [[ "${GAUGE_MODE:-}" == "verbose" ]]; then
    printf '+ %s\n' "$*"
    if ! "$@"; then
      local rc=$?
      echo "Command failed ($rc): $*" >> "${INSTALL_LOG:-/dev/null}"
      append_update_log "Command failed ($rc): $*"
      progress_fail "$rc"
    fi
    return 0
  fi
  if ! "$@" >>"$INSTALL_LOG" 2>&1; then
    local rc=$?
    echo "Command failed ($rc): $*" >> "$INSTALL_LOG"
    append_update_log "Command failed ($rc): $*"
    progress_fail "$rc"
  fi
}

RESUME_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tabbyapi-stack"

nvidia_smi_ok() {
  nvidia-smi >/dev/null 2>&1
}

pci_has_nvidia() {
  if need_cmd lspci; then
    lspci 2>/dev/null | grep -qi nvidia
  else
    return 0
  fi
}

try_load_nvidia() {
  sudo -n modprobe nvidia >/dev/null 2>&1 || true
  sudo -n modprobe nvidia_uvm >/dev/null 2>&1 || true
  nvidia_smi_ok
}

write_resume_env() {
  local f="$1"
  mkdir -p "$(dirname "$f")"
  # Subshell: umask is not function-scoped and would otherwise apply to every
  # later file the installer writes.
  (
  umask 077
  {
    echo "TABBY_NONINTERACTIVE=1"
    echo "TABBY_NVIDIA_REBOOT_DONE=1"
    printf 'TABBY_INSTALL_ROOT=%q\n' "$DEST"
    printf 'TABBY_CACHE=%q\n' "${WIN_ROOT-}"
    printf 'TABBY_MODELS=%q\n' "$MODEL_SET"
    printf 'TABBY_NETWORK_HOST=%q\n' "$TABBY_NETWORK_HOST"
    printf 'TABBY_NETWORK_PORT=%q\n' "$TABBY_NETWORK_PORT"
    printf 'COMFYUI_URL=%q\n' "$COMFYUI_URL"
    printf 'TABBY_PUBLIC_BASE=%q\n' "${TABBY_PUBLIC_BASE-}"
    printf 'TABBY_SSH_REMOTE=%q\n' "${TABBY_SSH_REMOTE-}"
    printf 'TABBY_SSH_FORWARD=%q\n' "${TABBY_SSH_FORWARD-}"
    printf 'TABBY_SSH_KEY=%q\n' "${TABBY_SSH_KEY-}"
    printf 'TABBY_SAVER_ENABLED=%q\n' "${TABBY_SAVER_ENABLED-}"
    printf 'TABBY_SAVER_IDLE_S=%q\n' "${TABBY_SAVER_IDLE_S-}"
    printf 'TABBY_SAVER_LOGOUT_IDLE_S=%q\n' "${TABBY_SAVER_LOGOUT_IDLE_S-}"
    printf 'TABBY_SAVER_HUD_S=%q\n' "${TABBY_SAVER_HUD_S-}"
    printf 'TABBY_INSTALL_VERBOSE=%q\n' "${TABBY_INSTALL_VERBOSE-}"
    printf 'TABBY_INSTALL_SH=%q\n' "$DEST/install.sh"
  } > "$f"
  )
}

write_resume_launch() {
  mkdir -p "$RESUME_DIR"
  cat > "$RESUME_DIR/resume-launch.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
RESUME_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/tabbyapi-stack/install-resume.env"
LOCK="${XDG_CONFIG_HOME:-$HOME/.config}/tabbyapi-stack/install-resume.lock"
mkdir -p "$(dirname "$LOCK")"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "tabbyapi-stack install resume is already running."
  exit 0
fi
if [[ ! -f "$RESUME_ENV" ]]; then
  exit 0
fi
# shellcheck disable=SC1090
set -a
. "$RESUME_ENV"
set +a
if [[ "${1:-}" != "--in-term" && "${1:-}" != "--headless" && ! -t 1 ]]; then
  if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    self="$(readlink -f "$0" 2>/dev/null || echo "$0")"
    for cmd in kgx gnome-terminal konsole xfce4-terminal mate-terminal \
               lxterminal tilix kitty alacritty wezterm xterm; do
      command -v "$cmd" >/dev/null 2>&1 || continue
      case "$cmd" in
        kgx|gnome-terminal|tilix) exec "$cmd" -- bash "$self" --in-term ;;
        konsole|xfce4-terminal|mate-terminal|lxterminal|alacritty|wezterm|xterm|kitty)
          exec "$cmd" -e bash "$self" --in-term ;;
      esac
    done
  fi
fi
exec bash "${TABBY_INSTALL_SH:?missing TABBY_INSTALL_SH}"
EOF
  chmod 755 "$RESUME_DIR/resume-launch.sh"
}

install_resume_hooks() {
  write_resume_env "$RESUME_DIR/install-resume.env"
  write_resume_env "$DEST/tabby-install-resume.env"
  write_resume_launch
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/tabbyapi-stack-install-resume.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=tabbyapi-stack install resume
Comment=Finish tabbyapi-stack after the NVIDIA driver reboot
Exec=$RESUME_DIR/resume-launch.sh
X-GNOME-Autostart-enabled=true
Terminal=false
EOF
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/tabby-install-resume.service" <<EOF
[Unit]
Description=Resume tabbyapi-stack install after NVIDIA driver reboot
After=network-online.target
Wants=network-online.target
ConditionPathExists=$RESUME_DIR/install-resume.env

[Service]
Type=oneshot
ExecStart=$RESUME_DIR/resume-launch.sh --headless
TimeoutStartSec=infinity

[Install]
WantedBy=default.target
EOF
  sudo -n loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  if [[ -n "${XDG_RUNTIME_DIR:-}" ]] && need_cmd systemctl; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user enable tabby-install-resume.service >/dev/null 2>&1 || true
  fi
}

clear_install_resume() {
  # Whole directory, not named files: it also holds install-resume.lock.
  rm -rf "$RESUME_DIR"
  rm -f "$HOME/.config/autostart/tabbyapi-stack-install-resume.desktop" \
        "${DEST:-}/tabby-install-resume.env"
  if [[ -n "${XDG_RUNTIME_DIR:-}" ]] && need_cmd systemctl; then
    systemctl --user disable --now tabby-install-resume.service >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/tabby-install-resume.service"
}

RSYNC_EXCLUDES=(
  --exclude 'venv/'
  --exclude 'models/'
  --exclude 'ComfyUI/'
  --exclude 'pasted-images/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'logs/'
  --exclude 'build/'
  --exclude '*.egg-info/'
  --exclude '.pytest_cache/'
  --exclude '*.log'
  --exclude 'config.yml'
  --exclude 'tabby.env'
  --exclude 'deploy/arch/tabby.env'
  --exclude 'tabbyAPI/deploy/arch/tabby.env'
  --exclude 'HOW-TO-ARCH.txt'
  --exclude 'CURSOR.md'
  --exclude 'HANDOFF.md'
  --exclude '.cursor/'
  --exclude 'api_tokens.yml'
  --exclude 'ui_users.json'
  --exclude 'ui_sessions.json'
  --exclude 'tabbyAPI/ui_users.json'
  --exclude 'tabbyAPI/ui_sessions.json'
)

# Copy the git tree (including .git when present) to the install root so the
# live copy can git pull. Skip when dest is already this checkout.
# Runtime dirs and secrets listed above are left alone.
sync_tabby_sources_to_dest() {
  mkdir -p "$DEST" "$DEST_TABBY"
  local src_abs dest_abs
  src_abs="$(cd "$STACK_ROOT" && pwd)"
  dest_abs="$(cd "$DEST" && pwd)"
  if [[ "$src_abs" != "$dest_abs" ]]; then
    rsync -a "${RSYNC_EXCLUDES[@]}" "$STACK_ROOT/" "$DEST/"
  fi
  local script
  for script in install.sh uninstall.sh update.sh; do
    [[ -f "$STACK_ROOT/$script" ]] || continue
    if [[ "$STACK_ROOT/$script" -ef "$DEST/$script" ]]; then
      chmod 755 "$DEST/$script"
    else
      install -m 755 "$STACK_ROOT/$script" "$DEST/$script"
    fi
  done
}

schedule_nvidia_reboot() {
  local delay="${TABBY_REBOOT_DELAY:-300}"
  echo "NVIDIA driver installed but not loaded; scheduling reboot + resume." >> "$INSTALL_LOG"
  sync_tabby_sources_to_dest >>"$INSTALL_LOG" 2>&1 || progress_fail
  install_resume_hooks
  progress_stop
  trap - EXIT
  local mins=$((delay / 60))
  local msg
  msg="The NVIDIA driver package is installed, but the kernel module is not
loaded yet. That is normal on the first driver install. Reboot is
only used in this case — not for other install failures.

This computer will reboot in ${mins} minutes (${delay} seconds).
Enter / OK reboots now.  Ctrl+C cancels.

After reboot the installer resumes automatically with your saved
answers (no questions again):
  • at boot, via the user systemd unit tabby-install-resume
    (linger is enabled so it can start without a login)
  • or when you log into a desktop, a terminal opens and continues

Install root:  ${DEST}
Resume with:   bash ${DEST}/install.sh
Log:           ${INSTALL_LOG}

If you chose a USB or other weights cache, remount it after reboot
(or missing files will download from Hugging Face).

If resume does not start, run the command above. After a successful
install the resume hooks are removed."
  if [[ "$INTERACTIVE" -eq 1 ]]; then
    if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
      dialog_tty --backtitle "$BACKTITLE" --title "NVIDIA driver — reboot required" \
        --timeout "$delay" --msgbox "$msg" 24 74 || true
    else
      echo
      echo "=== NVIDIA driver — reboot required ==="
      echo "$msg"
      echo
      sleep "$delay"
    fi
  else
    echo
    echo "NVIDIA driver — reboot required"
    echo "$msg"
    echo
    sleep "$delay"
  fi
  if ! sudo -n reboot; then
    echo "Could not reboot. Reboot this machine, then: bash $DEST/install.sh"
    exit 1
  fi
  exit 0
}

pkg_exists() {
  pacman -Si "$1" >/dev/null 2>&1
}

# Arch removed the proprietary `nvidia` package (590+ / Dec 2025). Official
# repos ship nvidia-open. nvidia-open does not Provide the name `nvidia`, so
# falling back to pacman -S nvidia fails with "target not found: nvidia".
nvidia_kernel_pkg() {
  if pkg_exists nvidia-open; then
    printf '%s\n' nvidia-open
  elif pkg_exists nvidia; then
    printf '%s\n' nvidia
  else
    return 1
  fi
}

init_pyenv() {
  export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
  if [[ -d "$PYENV_ROOT/bin" ]]; then
    export PATH="$PYENV_ROOT/bin:$PATH"
  fi
  if need_cmd pyenv; then
    eval "$(pyenv init - bash)"
  fi
}

pyenv_tree_ok() {
  local root="$1"
  [[ -x "$root/bin/pyenv" || -x "$root/libexec/pyenv" ]]
}

copy_tree() {
  local src="$1" dest="$2"
  mkdir -p "$dest"
  if need_cmd rsync; then
    rsync -a "$src/" "$dest/"
  else
    cp -a "$src/." "$dest/"
  fi
}

# pyenv.run is a short domain and often fails DNS in the live-ISO chroot
# even when Arch mirrors and github.com work. Clone pyenv from GitHub.
install_pyenv() {
  export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
  if pyenv_tree_ok "$PYENV_ROOT"; then
    return 0
  fi
  if [[ -e "$PYENV_ROOT" ]]; then
    rm -rf "$PYENV_ROOT"
  fi

  local cache_src=""
  if [[ -n "${WIN_ROOT:-}" ]]; then
    if pyenv_tree_ok "${WIN_ROOT}/.pyenv"; then
      cache_src="${WIN_ROOT}/.pyenv"
    elif pyenv_tree_ok "${WIN_ROOT}/pyenv"; then
      cache_src="${WIN_ROOT}/pyenv"
    fi
  fi
  if [[ -n "$cache_src" ]]; then
    echo "    Copying pyenv from $cache_src"
    copy_tree "$cache_src" "$PYENV_ROOT"
    if pyenv_tree_ok "$PYENV_ROOT"; then
      return 0
    fi
    rm -rf "$PYENV_ROOT"
  fi

  local pyenv_bundle=""
  if pyenv_bundle=$(tsos_bundle pyenv); then
    echo "    Cloning pyenv from the TSOS ISO..."
    git clone "$pyenv_bundle" "$PYENV_ROOT"
    pyenv_tree_ok "$PYENV_ROOT" && return 0
    rm -rf "$PYENV_ROOT"
    echo "The pyenv bundle on the TSOS ISO is invalid."
    return 1
  fi

  echo "    Cloning pyenv from GitHub..."
  local i
  for ((i = 1; i <= 3; i++)); do
    if GIT_TERMINAL_PROMPT=0 git clone --depth 1 \
         https://github.com/pyenv/pyenv.git "$PYENV_ROOT"; then
      if pyenv_tree_ok "$PYENV_ROOT"; then
        return 0
      fi
    fi
    rm -rf "$PYENV_ROOT"
    echo "    git clone pyenv failed (try $i/3)"
    sleep 2
  done

  echo "    git clone failed; fetching the pyenv installer from GitHub..."
  if curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 120 \
       https://raw.githubusercontent.com/pyenv/pyenv-installer/master/bin/pyenv-installer | bash; then
    if pyenv_tree_ok "$PYENV_ROOT"; then
      return 0
    fi
  fi
  echo "pyenv install failed (GitHub clone). Not using pyenv.run — that host often fails DNS on the live ISO."
  return 1
}

maybe_copy_cached_python312() {
  local dest="${PYENV_ROOT:-$HOME/.pyenv}/versions/$PYENV_VER"
  [[ -x "$dest/bin/python" ]] && return 0
  [[ -n "${WIN_ROOT:-}" ]] || return 0
  local src
  for src in "$WIN_ROOT/.pyenv/versions/$PYENV_VER" "$WIN_ROOT/pyenv/versions/$PYENV_VER"; do
    if [[ -x "$src/bin/python" ]]; then
      echo "    Copying Python $PYENV_VER from $src"
      copy_tree "$src" "$dest"
      if [[ -x "$dest/bin/python" ]] && "$dest/bin/python" -c 'import sys' >/dev/null 2>&1; then
        return 0
      fi
      rm -rf "$dest"
    fi
  done
  return 0
}

# Settings, tsctl, and install.sh use sudo -n (no TTY). Last matching
# sudoers.d rule wins; a drop-in named "wheel" sorts after 99-* and
# cancels NOPASSWD, so this file is zz-tsos-nopasswd.
_write_nopasswd_sudoers_root() {
  local user="${1:?}"
  local dest=/etc/sudoers.d/zz-tsos-nopasswd
  local tmp
  install -d -m 0750 /etc/sudoers.d
  if [[ -f /etc/sudoers.d/wheel ]]; then
    mv /etc/sudoers.d/wheel /etc/sudoers.d/10-wheel
  fi
  if [[ -f /etc/sudoers ]] && ! grep -qE '^[[:space:]]*[@#]includedir[[:space:]]+/etc/sudoers.d' /etc/sudoers; then
    printf '\n@includedir /etc/sudoers.d\n' >> /etc/sudoers
  fi
  tmp=$(mktemp)
  {
    printf 'Defaults:%s !use_pty,!requiretty,!pam_session\n' "$user"
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user"
  } >"$tmp"
  chmod 0440 "$tmp"
  if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"
    echo "visudo rejected passwordless sudoers for $user" >&2
    return 1
  fi
  install -m 0440 "$tmp" "$dest"
  rm -f "$tmp" /etc/sudoers.d/zz-tsos-firstboot /etc/sudoers.d/99-tsos-firstboot
}

write_nopasswd_sudoers() {
  if [[ "${EUID}" -eq 0 ]]; then
    _write_nopasswd_sudoers_root "${1:-$USER}"
    return
  fi
  need_cmd sudo || return 1
  sudo -n bash -c "$(declare -f _write_nopasswd_sudoers_root); _write_nopasswd_sudoers_root \"\$1\"" _ "${1:-$USER}"
}

sudo_n_ok() {
  if ! need_cmd sudo; then
    return 1
  fi
  if need_cmd timeout; then
    timeout 15 sudo -n true >/dev/null 2>&1
  else
    sudo -n true >/dev/null 2>&1
  fi
}

ensure_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    echo "Do not run as root. Re-run as your user."
    echo "If sudo is missing, the script will ask for the root password once to install it (passwordless after that)."
    exit 1
  fi
  # ISO chroot: sudo use_pty / pam_systemd can block forever. Cap the check.
  if sudo_n_ok; then
    write_nopasswd_sudoers || true
    return 0
  fi
  # systemd / ISO chroot has no TTY. sudo -v and su both fail with
  # "a terminal is required" / "Authentication token manipulation error".
  if [[ ! -t 0 ]]; then
    echo "sudo cannot prompt for a password in this non-interactive session."
    echo "The installer should have written /etc/sudoers.d/zz-tsos-nopasswd."
    echo "As root: printf '%s ALL=(ALL) NOPASSWD: ALL\\n' \"$USER\" > /etc/sudoers.d/zz-tsos-nopasswd"
    echo "         chmod 0440 /etc/sudoers.d/zz-tsos-nopasswd"
    echo "         mv /etc/sudoers.d/wheel /etc/sudoers.d/10-wheel 2>/dev/null || true"
    echo "Then re-run this script from a terminal."
    exit 1
  fi
  if need_cmd sudo && sudo -v; then
    write_nopasswd_sudoers || true
    return 0
  fi
  echo
  echo "==> sudo is not installed or not usable (common on a fresh Arch install)."
  echo "    Enter the root password once to install sudo and allow $USER to use it without a password."
  su -c "set -euo pipefail
    pacman -Sy --needed --noconfirm sudo
    usermod -aG wheel ${USER}
    $(declare -f _write_nopasswd_sudoers_root)
    _write_nopasswd_sudoers_root '${USER}'
  "
  hash -r 2>/dev/null || true
  if ! sudo_n_ok && { ! need_cmd sudo || ! sudo -v; }; then
    echo "sudo is still not usable. Run: newgrp wheel"
    echo "Then re-run this script."
    exit 1
  fi
  write_nopasswd_sudoers || true
  echo "sudo is ready (passwordless for $USER)."
}

ensure_python312() {
  init_pyenv
  # Prefer the real interpreter. After `pyenv init`, `python3.12` is a shim
  # that fails with "command not found" unless a version is selected
  # (`The python3.12 command exists in these Python versions: 3.12.5`).
  local pyenv_python="${PYENV_ROOT:-$HOME/.pyenv}/versions/$PYENV_VER/bin/python"
  if [[ -x "$pyenv_python" ]]; then
    PY="$pyenv_python"
    return 0
  fi
  if need_cmd python3.12; then
    local resolved
    resolved=$(command -v python3.12)
    if [[ "$resolved" != *"/shims/python3.12" ]]; then
      PY=python3.12
      return 0
    fi
  fi

  if need_cmd yay; then
    echo "==> Installing python312 from the AUR (yay)..."
    yay -S --needed --noconfirm python312 || true
  elif need_cmd paru; then
    echo "==> Installing python312 from the AUR (paru)..."
    paru -S --needed --noconfirm python312 || true
  fi
  hash -r 2>/dev/null || true
  if need_cmd python3.12; then
    local resolved
    resolved=$(command -v python3.12)
    if [[ "$resolved" != *"/shims/python3.12" ]]; then
      PY=python3.12
      return 0
    fi
  fi

  echo
  echo "==> Arch repos only ship current Python (3.14). python312 is not in pacman."
  echo "    Installing pyenv and Python $PYENV_VER (do not use system 3.13/3.14)."
  if ! need_cmd pyenv && ! pyenv_tree_ok "${PYENV_ROOT:-$HOME/.pyenv}"; then
    install_pyenv || return 1
  fi
  init_pyenv
  if ! need_cmd pyenv; then
    echo "pyenv is not on PATH after install."
    return 1
  fi
  local line_root='export PYENV_ROOT="$HOME/.pyenv"'
  local line_path='[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"'
  local line_init='eval "$(pyenv init - bash)"'
  touch "$HOME/.bashrc"
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" ]] || continue
    if ! grep -Fq 'PYENV_ROOT' "$rc"; then
      printf '\n%s\n%s\n%s\n' "$line_root" "$line_path" "$line_init" >> "$rc"
    fi
  done
  maybe_copy_cached_python312
  if [[ ! -x "$PYENV_ROOT/versions/$PYENV_VER/bin/python" ]]; then
    echo "    Compiling Python $PYENV_VER (several minutes)..."
    if [[ -n "$TSOS_OFFLINE_ROOT" && -f "$TSOS_OFFLINE_ROOT/python/Python-${PYENV_VER}.tar.xz" ]]; then
      export PYTHON_BUILD_CACHE_PATH="$TSOS_OFFLINE_ROOT/python"
    fi
    pyenv install -s "$PYENV_VER"
  fi
  # Deliberately no `pyenv global`: the venvs below use the absolute path, and
  # repointing the user's default python is not this installer's business.
  if [[ -x "$PYENV_ROOT/versions/$PYENV_VER/bin/python" ]]; then
    PY="$PYENV_ROOT/versions/$PYENV_VER/bin/python"
    return 0
  fi
  echo "Python 3.12 is required for Tabby cu12 / ExLlamaV3 wheels."
  echo "pyenv install $PYENV_VER failed."
  return 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run this script on Arch Linux, not Windows."
  exit 1
fi
if [[ ! -f /etc/arch-release ]]; then
  echo "This installer expects Arch Linux (pacman)."
  exit 1
fi
if [[ "${EUID}" -eq 0 ]]; then
  echo "Do not run as root. Re-run as your user."
  echo "If sudo is missing, the script will ask for the root password once to install it (passwordless after that)."
  exit 1
fi
if [[ ! -f "$TABBY_SRC/pyproject.toml" || ! -f "$TABBY_SRC/main.py" ]]; then
  echo "Cannot find TabbyAPI at $TABBY_SRC (missing pyproject.toml or main.py)."
  echo "Run: bash install.sh  (from the tabbyapi-stack root)"
  exit 1
fi
if [[ ! -f "$CATALOG" || ! -f "$FETCH_MODELS" ]]; then
  echo "Missing $CATALOG or $FETCH_MODELS."
  exit 1
fi

# Resume after an NVIDIA driver reboot (hooks or a manual re-run).
# update.sh must not pick up a leftover resume env.
if [[ "$UPDATE_MODE" -eq 0 && -z "${TABBY_NVIDIA_REBOOT_DONE:-}" && -f "${XDG_CONFIG_HOME:-$HOME/.config}/tabbyapi-stack/install-resume.env" ]]; then
  # shellcheck disable=SC1090
  set -a
  . "${XDG_CONFIG_HOME:-$HOME/.config}/tabbyapi-stack/install-resume.env"
  set +a
fi
if [[ "${TABBY_NVIDIA_REBOOT_DONE:-}" == 1 ]]; then
  echo "Resuming tabbyapi-stack install after the NVIDIA driver reboot."
fi

# Env-driven install: skip menus when the three knobs are already set.
# TABBY_NESTED_UI: tsos-installer already asked and owns the progress bar.
if [[ "${TABBY_NONINTERACTIVE:-}" == 1 || "${TABBY_NESTED_UI:-}" == 1 ]] || [[ ! -t 0 ]]; then
  INTERACTIVE=0
elif [[ -n "${TABBY_INSTALL_ROOT:-}" && -n "${TABBY_MODELS:-}" ]]; then
  INTERACTIVE=0
fi
if [[ "${TABBY_NESTED_UI:-}" == 1 ]]; then
  _early_log="${TABBY_INSTALL_ROOT:-$STACK_ROOT}/tabby-install.log"
  if ! adopt_install_log "$_early_log"; then
    _early_log="/tmp/tabby-install-${USER}.log"
    adopt_install_log "$_early_log" || _early_log="/dev/null"
  fi
  echo "install.sh nested start $(date -Iseconds)" >> "$_early_log"
  unset _early_log
fi
ensure_sudo
if [[ "$INTERACTIVE" -eq 1 ]]; then
  ensure_dialog
  case "${TERM:-}" in
    "" | dumb | unknown) export TERM=linux ;;
  esac
  if [[ -n "$TUI" && -t 0 && -t 1 ]]; then
    USE_TUI=1
    work_term
  fi
fi

DEFAULT_CACHE=""

# Expand a leading ~ and make the path absolute. A relative dest would end up
# in the systemd unit as a relative WorkingDirectory and fail at boot.
abs_path() {
  local p="$1"
  case "$p" in
    "~") p="$HOME" ;;
    "~/"*) p="$HOME/${p#\~/}" ;;
  esac
  [[ "$p" == /* ]] || p="$PWD/$p"
  printf '%s' "$p"
}

dest_is_sane() {
  case "$DEST" in
    "" | "/" | "$HOME" | /usr | /usr/* | /etc | /etc/* | /var | /var/* | /boot | /boot/*)
      return 1
      ;;
  esac
  return 0
}

apply_choices() {
  DEST="$(abs_path "${DEST%/}")"
  DEST="${DEST%/}"
  WIN_ROOT="${WIN_ROOT%/}"
  if [[ -n "$WIN_ROOT" ]]; then
    WIN_ROOT="$(abs_path "$WIN_ROOT")"
    WIN_ROOT="${WIN_ROOT%/}"
  fi
  WIN_TABBY=""
  if [[ -n "$WIN_ROOT" ]]; then
    WIN_TABBY="${WIN_ROOT}/tabbyAPI"
  fi
  DEST_TABBY="${DEST}/tabbyAPI"
  DEST_COMFY="${DEST}/ComfyUI"
}

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

# Non-loopback IPv4 this host would use on the LAN. Empty if none.
# Always returns 0 so set -e does not abort the installer.
lan_ipv4() {
  local ip=""
  if need_cmd ip; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{
      for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }
    }')" || true
    if [[ -z "$ip" ]]; then
      ip="$(ip -4 -o addr show scope global 2>/dev/null | awk 'NR==1 {gsub(/\/.*/, "", $4); print $4}')" || true
    fi
  fi
  if [[ -z "$ip" ]] && need_cmd hostname; then
    ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '
      /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $0 !~ /^127\./ { print; exit }
    ')" || true
  fi
  case "$ip" in
    "" | 127.* | 0.0.0.0) ip="" ;;
  esac
  if [[ "$ip" == *:* ]]; then
    ip=""
  fi
  printf '%s' "$ip"
  return 0
}

# Extra global IPv4s besides the primary, space-separated. Always returns 0.
lan_ipv4_extras() {
  local primary="$1"
  [[ -z "$primary" ]] && return 0
  need_cmd ip || return 0
  ip -4 -o addr show scope global 2>/dev/null | awk -v primary="$primary" '{
    gsub(/\/.*/, "", $4)
    if ($4 == "" || $4 == primary || index($4, ":")) next
    if (n++) printf " "
    printf "%s", $4
  }' || true
  return 0
}

# "addr iface" lines for the listen-host menu. Always returns 0.
listen_ipv4_ifaces() {
  if need_cmd ip; then
    ip -4 -o addr show scope global 2>/dev/null | awk '{
      iface=$2
      gsub(/\/.*/, "", $4)
      if ($4 == "" || index($4, ":")) next
      print $4, iface
    }' || true
    return 0
  fi
  if need_cmd hostname; then
    hostname -I 2>/dev/null | tr ' ' '\n' | awk '
      /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $0 !~ /^127\./ { print $1, "lan" }
    ' || true
  fi
  return 0
}

ui_listen_host() {
  local title="$1"
  local current="${2:-127.0.0.1}"
  local -a items=()
  local seen="|"
  local addr iface choice
  _listen_host_add() {
    local ip="$1" desc="$2"
    [[ -n "$ip" ]] || return 0
    [[ "$seen" == *"|$ip|"* ]] && return 0
    seen+="${ip}|"
    items+=("$ip" "$desc")
  }
  case "$current" in
    127.0.0.1) _listen_host_add "$current" "this machine only (usual)" ;;
    0.0.0.0) _listen_host_add "$current" "all interfaces — LAN clients can connect" ;;
    "") ;;
    *) _listen_host_add "$current" "current choice" ;;
  esac
  _listen_host_add "127.0.0.1" "this machine only (usual)"
  while read -r addr iface; do
    _listen_host_add "$addr" "this NIC (${iface})"
  done < <(listen_ipv4_ifaces)
  _listen_host_add "0.0.0.0" "all interfaces — LAN clients can connect"
  _listen_host_add "other" "type a different address"
  unset -f _listen_host_add
  choice="$(ui_menu "$title" \
"Which address should TabbyAPI bind on? Pick from this machine.

  127.0.0.1  — this machine only (usual)
  a LAN IP   — only that NIC
  0.0.0.0    — other devices on the LAN can connect

Do not pick a public hostname. The TCP port is the next screen." \
    "${items[@]}")" || return 1
  if [[ "$choice" == "other" ]]; then
    choice="$(ui_input "$title" \
"Address TabbyAPI binds on.

Examples: 127.0.0.1 (this machine), 0.0.0.0 (all NICs), or a LAN IPv4.
Do not put a public hostname here." \
      "${current:-127.0.0.1}")" || return 1
  fi
  printf '%s' "${choice:-127.0.0.1}"
}

# Simple-mode listen choice: this PC vs LAN.
ui_listen_access() {
  local title="$1"
  local choice
  choice="$(ui_menu "$title" \
"Who should be able to open the API and browser UI?

  This PC only — Cursor and the UI on this machine (127.0.0.1).
  Other computers on my network — laptops and editors on the LAN (0.0.0.0).

You can change this later in Settings." \
    this-pc "This PC only" \
    lan "Other computers on my network")" || return 1
  case "$choice" in
    lan) printf '%s' "0.0.0.0" ;;
    *) printf '%s' "127.0.0.1" ;;
  esac
}

valid_install_mode() {
  [[ "$1" == simple || "$1" == advanced ]]
}

pick_install_mode() {
  if [[ -n "${INSTALL_MODE:-}" ]]; then
    valid_install_mode "$INSTALL_MODE" || {
      echo "invalid INSTALL_MODE: $INSTALL_MODE (simple or advanced)" >&2
      exit 1
    }
    return 0
  fi
  local choice
  choice="$(ui_menu "Setup type" \
"Simple (recommended) opens a review menu: this PC vs LAN,
core models from Hugging Face into \$HOME/tabbyapi-stack.

Advanced lists every setting as a row you can open: install
root, weights cache, models, bind address, public URL, SSH
tunnel, and screensaver.

You can re-run later and pick Advanced to change those." \
    simple "Simple — this PC vs LAN, core models" \
    advanced "Advanced — every setting")"
  INSTALL_MODE="${choice:-simple}"
  valid_install_mode "$INSTALL_MODE" || INSTALL_MODE=simple
}

prompt_simple_install() {
  ui_msg "Simple setup" \
"tabbyapi-stack: local OpenAI-compatible API for coding and agents,
plus ComfyUI image generation on Arch.

Simple setup installs into:
  ${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}

A review menu lists who can connect. Open that row to change
it, then Start install. Extra models, a USB cache, a public
URL, and an SSH tunnel are under Advanced. The TTY screensaver
is on by default (disable later with tsctl screensaver disable).

Needed
  • Arch Linux, your user (not root), internet
  • NVIDIA GPU (docs assume 12 GB)

Esc on the review menu cancels. Esc on a setting goes back."

  DEST="${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}"
  DEST="${DEST:-$DEFAULT_DEST}"
  WIN_ROOT=""
  MODEL_SET="${TABBY_MODELS:-core}"
  MODEL_SET="${MODEL_SET:-core}"
  TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
  COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
  TABBY_PUBLIC_BASE=""
  TABBY_SSH_REMOTE=""
  TABBY_SSH_FORWARD=""
  TABBY_SSH_KEY=""
  TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-127.0.0.1}"
  apply_saver_defaults
  local choice host access
  while true; do
    apply_choices
    apply_network_defaults
    TABBY_SSH_REMOTE=""
    TABBY_SSH_FORWARD=""
    TABBY_SSH_KEY=""
    if [[ "${TABBY_SAVER_ENABLED}" == "1" ]]; then
      SAVER_CONFIRM="on (auto; idle ${TABBY_SAVER_IDLE_S}s)"
    else
      SAVER_CONFIRM="off (auto)"
    fi
    if [[ "$TABBY_NETWORK_HOST" == "0.0.0.0" ]]; then
      access="LAN (0.0.0.0:${TABBY_NETWORK_PORT})"
    else
      access="this PC (${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT})"
    fi
    choice=$(ui_menu "Review install plan" \
"Open a row to change it. Start install when the plan looks right.

  Dest:     ${DEST}
  Weights:  Hugging Face
  Models:   ${MODEL_SET}
  Screensaver: ${SAVER_CONFIRM}

Esc aborts." \
      access "$(printf '%s' "$access" | cut -c1-48)" \
      go "Start install") || ui_cancel
    case "$choice" in
      access)
        UI_ALLOW_BACK=1
        host=$(ui_listen_access "Who can connect") && TABBY_NETWORK_HOST="${host:-127.0.0.1}"
        UI_ALLOW_BACK=0
        ;;
      go)
        if ! valid_model_set "$MODEL_SET"; then
          ui_msg "Invalid model set" "Pick at least one model (got ${MODEL_SET:-empty})."
          MODEL_SET=core
          continue
        fi
        if ! valid_port "$TABBY_NETWORK_PORT"; then
          ui_msg "Invalid port" "The listen port must be a number from 1 to 65535 (got ${TABBY_NETWORK_PORT})."
          TABBY_NETWORK_PORT=5000
          continue
        fi
        if ! dest_is_sane; then
          ui_msg "Invalid install root" \
"Refusing to install into:
  ${DEST}

That is your home directory or a system folder. Pick a dedicated
folder such as ${HOME}/tabbyapi-stack or /data/tabbyapi-stack."
          return 1
        fi
        API_URL="http://${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}"
        break
        ;;
    esac
  done
}

# Diagram for the reverse SSH tunnel screens. Always returns 0.
ssh_tunnel_help() {
  local remote="${1:-${TABBY_SSH_REMOTE:-user@your-vps}}"
  local spec="${2:-${TABBY_SSH_FORWARD:-}}"
  local public="${3:-${TABBY_PUBLIC_BASE:-}}"
  local port="${TABBY_NETWORK_PORT:-5000}"
  local bind="127.0.0.1" rport="12345" lhost="127.0.0.1" lport="$port"
  local a="" b="" c="" d=""
  [[ -n "$spec" ]] || spec="127.0.0.1:12345:127.0.0.1:${port}"
  IFS=: read -r a b c d <<< "$spec" || true
  if [[ -n "${d:-}" ]]; then
    bind=$a; rport=$b; lhost=$c; lport=$d
  elif [[ -n "${c:-}" ]]; then
    bind="0.0.0.0"; rport=$a; lhost=$b; lport=$c
  fi
  [[ -n "$public" ]] || public="https://YOUR-HOST/v1"
  cat <<EOF
What this tunnel is for

  ${public}
    →  SSH reverse listen on ${remote}
       (${bind}:${rport} on that host)
    →  TabbyAPI on this GPU box
       (${lhost}:${lport})

Editors and the browser hit the HTTPS URL (or that SSH host).
They do not connect to this machine's LAN IP. This box opens
ssh -R and holds the path open.

You must upload this account's public key to ${remote}
(authorized_keys on that host) so the tunnel can log in.
EOF
  return 0
}

apply_network_defaults() {
  TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-127.0.0.1}"
  TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
  COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
  local hostport="${COMFYUI_URL#*://}"
  hostport="${hostport%%/*}"
  COMFY_LISTEN_HOST="${hostport%%:*}"
  COMFY_LISTEN_PORT="${hostport##*:}"
  [[ -n "$COMFY_LISTEN_HOST" ]] || COMFY_LISTEN_HOST="127.0.0.1"
  if [[ "$COMFY_LISTEN_PORT" == "$hostport" ]] || ! valid_port "$COMFY_LISTEN_PORT"; then
    COMFY_LISTEN_PORT=8188
  fi
  TABBY_PUBLIC_BASE="${TABBY_PUBLIC_BASE:-}"
  TABBY_SSH_REMOTE="${TABBY_SSH_REMOTE:-}"
  TABBY_SSH_FORWARD="${TABBY_SSH_FORWARD:-127.0.0.1:12345:127.0.0.1:${TABBY_NETWORK_PORT}}"
  TABBY_SSH_KEY="${TABBY_SSH_KEY:-$HOME/.ssh/id_ed25519}"
  API_URL="http://${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}"
}

graphical_session_present() {
  [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && return 0
  if need_cmd systemctl; then
    systemctl is-active --quiet display-manager 2>/dev/null && return 0
  fi
  if need_cmd loginctl; then
    local sess type
    while read -r sess _uid _user _seat _tty; do
      [[ -n "$sess" ]] || continue
      type="$(loginctl show-session "$sess" -p Type --value 2>/dev/null || true)"
      case "$type" in
        wayland|x11) return 0 ;;
      esac
    done < <(loginctl list-sessions --no-legend 2>/dev/null || true)
  fi
  return 1
}

valid_seconds() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v n="$1" 'BEGIN { exit !(n >= 0 && n <= 86400) }'
}

apply_saver_defaults() {
  TABBY_SAVER_IDLE_S="${TABBY_SAVER_IDLE_S:-120}"
  TABBY_SAVER_LOGOUT_IDLE_S="${TABBY_SAVER_LOGOUT_IDLE_S:-10}"
  TABBY_SAVER_HUD_S="${TABBY_SAVER_HUD_S:-300}"
  TABBY_SAVER_TTY="${TABBY_SAVER_TTY:-tty8}"
  TABBY_SAVER_USER_TTY="${TABBY_SAVER_USER_TTY:-tty1}"
  if [[ -z "${TABBY_SAVER_ENABLED:-}" ]]; then
    if [[ "${UPDATE_MODE:-0}" -eq 1 ]]; then
      # Update all must not flip an existing box on. tabby.env wins if set.
      TABBY_SAVER_ENABLED=0
    elif [[ "${TABBY_ISO_CHROOT:-}" == 1 ]]; then
      # Live-ISO DISPLAY/loginctl is not the installed machine. TSOS is a TTY box.
      TABBY_SAVER_ENABLED=1
    elif graphical_session_present; then
      TABBY_SAVER_ENABLED=0
    else
      TABBY_SAVER_ENABLED=1
    fi
  fi
}

write_tabby_env() {
  local env_file="$1"
  mkdir -p "$(dirname "$env_file")"
  cat > "$env_file" <<EOF
COMFYUI_DIR=$DEST_COMFY
COMFYUI_URL=$COMFYUI_URL
TABBY_PUBLIC_BASE=$TABBY_PUBLIC_BASE
TABBY_INSTALL_ROOT=$DEST
TABBY_NETWORK_HOST=$TABBY_NETWORK_HOST
TABBY_NETWORK_PORT=$TABBY_NETWORK_PORT
TABBY_MODELS=$MODEL_SET
TABBY_SAVER_ENABLED=${TABBY_SAVER_ENABLED:-1}
TABBY_SAVER_IDLE_S=${TABBY_SAVER_IDLE_S:-120}
TABBY_SAVER_LOGOUT_IDLE_S=${TABBY_SAVER_LOGOUT_IDLE_S:-10}
TABBY_SAVER_HUD_S=${TABBY_SAVER_HUD_S:-300}
TABBY_SAVER_TTY=${TABBY_SAVER_TTY:-tty8}
TABBY_SAVER_USER_TTY=${TABBY_SAVER_USER_TTY:-tty1}
EOF
  if [[ -n "$TABBY_SSH_REMOTE" ]]; then
    cat >> "$env_file" <<EOF
TABBY_SSH_REMOTE=$TABBY_SSH_REMOTE
TABBY_SSH_FORWARD=$TABBY_SSH_FORWARD
TABBY_SSH_KEY=$TABBY_SSH_KEY
EOF
  fi
}

cache_on_dest() {
  [[ -n "$WIN_ROOT" && ( "$DEST" == "$WIN_ROOT" || "$DEST" == "$WIN_ROOT"/* || "$DEST_TABBY" == "$WIN_TABBY" ) ]]
}

# True when PATH looks like it already holds installer weights (any layout
# fetch_models.py will search: tabbyapi-stack tree, models/, or weight files).
cache_has_any_weights() {
  local root="${1:-}"
  local hit=""
  [[ -n "$root" && -d "$root" ]] || return 1
  [[ -d "$root/tabbyAPI" || -d "$root/ComfyUI" || -d "$root/models" || -d "$root/hub" ]] && return 0
  hit="$(find -P "$root" -maxdepth 4 \( \
      -name 'model.safetensors' -o \
      -name 'quantization_config.json' -o \
      -name '*.gguf' -o \
      -name 'flux1-schnell-fp8.safetensors' \
    \) -print -quit 2>/dev/null || true)"
  [[ -n "$hit" ]]
}

valid_model_set() {
  local s="${1:-}"
  [[ -n "$s" ]] || return 1
  [[ "$s" == "core" || "$s" == "all" || "$s" == "selected" ]] && return 0
  [[ "$s" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]]
}

gpu_vram_mib() {
  local mem name lower
  if need_cmd nvidia-smi; then
    mem="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '[:space:]')"
    if [[ "$mem" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
      printf '%s\n' "${mem%%.*}"
      return 0
    fi
  fi
  name="$(lspci 2>/dev/null | grep -iE 'VGA|3D|Display' | grep -i nvidia | head -1 || true)"
  lower="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    *4090*) printf '%s\n' 24576 ;;
    *4080*) printf '%s\n' 16384 ;;
    *4070*ti*super*) printf '%s\n' 16384 ;;
    *4070*ti*) printf '%s\n' 12288 ;;
    *4070*) printf '%s\n' 12288 ;;
    *4060*ti*16*) printf '%s\n' 16384 ;;
    *4060*ti*) printf '%s\n' 8192 ;;
    *4060*) printf '%s\n' 8192 ;;
    *3090*) printf '%s\n' 24576 ;;
    *3080*ti*) printf '%s\n' 12288 ;;
    *3080*) printf '%s\n' 10240 ;;
    *3070*) printf '%s\n' 8192 ;;
    *3060*ti*) printf '%s\n' 8192 ;;
    *3060*) printf '%s\n' 12288 ;;
    *a6000*) printf '%s\n' 49152 ;;
    *a5000*) printf '%s\n' 24576 ;;
    *a4000*) printf '%s\n' 16384 ;;
    *) printf '%s\n' 0 ;;
  esac
}

gpu_short_name() {
  local name=""
  if need_cmd nvidia-smi; then
    name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | sed 's/^NVIDIA GeForce //; s/^NVIDIA //')"
  fi
  if [[ -z "$name" ]]; then
    name="$(lspci 2>/dev/null | grep -iE 'VGA|3D|Display' | grep -i nvidia | head -1 | sed 's/.*NVIDIA Corporation //; s/ (rev.*//' || true)"
  fi
  printf '%s\n' "${name:-NVIDIA GPU}"
}

gpu_prompt_label() {
  local vram="$1"
  local name gb
  name="$(gpu_short_name)"
  if [[ "$vram" =~ ^[1-9][0-9]*$ ]]; then
    gb=$(( (vram + 512) / 1024 ))
    printf '%s, %s GB' "$name" "$gb"
  else
    printf '%s (VRAM unknown)' "$name"
  fi
}

# Prints comma-separated pick ids. Returns 1 if none were listed.
pick_models_ui() {
  local title="$1"
  local text="$2"
  local source="$3"
  local cache="${4:-}"
  local vram rows id state label
  local args=()
  if ! need_cmd python3 || [[ ! -f "$FETCH_MODELS" ]]; then
    return 2
  fi
  vram="$(gpu_vram_mib)"
  local py=(python3 -u "$FETCH_MODELS" --catalog "$CATALOG" --list-picks --source "$source" --vram-mib "$vram")
  if [[ -n "$cache" && -d "$cache" ]]; then
    py+=(--cache "$cache")
  fi
  rows="$("${py[@]}" 2>/dev/null || true)"
  [[ -n "$rows" ]] || return 2
  while IFS=$'\t' read -r id state label; do
    [[ -n "${id:-}" ]] || continue
    args+=("$id" "$label" "$state")
  done <<< "$rows"
  ((${#args[@]} >= 3)) || return 2
  ui_checklist "$title" "$text" "${args[@]}"
}

hub_desc() {
  local s=$1 n=${2:-48}
  s=${s//$'\n'/ }
  s=${s//$'\t'/ }
  if ((${#s} > n)); then
    s="${s:0:$((n - 3))}..."
  fi
  printf '%s' "$s"
}

inst_edit_dest() {
  local v
  v=$(ui_input "Arch install root" \
"Linux disk folder that will contain tabbyAPI/ and ComfyUI/.

Examples
  ${HOME}/tabbyapi-stack  →  ${HOME}/tabbyapi-stack/tabbyAPI  and  ${HOME}/tabbyapi-stack/ComfyUI
  /data/tabbyapi-stack    →  /data/tabbyapi-stack/tabbyAPI   and  /data/tabbyapi-stack/ComfyUI

Do NOT use a USB or other removable mount as the install root.
Those mounts are only a weights cache on a later row.

Default is \$HOME/tabbyapi-stack." \
"${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}") || return 0
  DEST="${v:-$DEFAULT_DEST}"
}

inst_edit_weights() {
  local cache_choice v
  cache_choice=$(ui_menu "Weights source" \
"Where should the installer get model weights?

Hugging Face — download models that fit this NVIDIA GPU.
A local path — search that folder for weights you already have
(USB copy, old tabbyapi-stack, or a folder of model dirs). Missing
pieces still download.

Mount a USB first if you want that option:
  sudo pacman -S --needed ntfs-3g
  sudo mkdir -p /mnt/usb
  sudo mount /dev/sdXN /mnt/usb" \
      hf "Hugging Face (models that fit this GPU)" \
      usb "Use /mnt/usb/tabbyapi-stack (USB copy)" \
      custom "Type another path") || return 0
  case "$cache_choice" in
    hf|none) WIN_ROOT="" ;;
    usb) WIN_ROOT="/mnt/usb/tabbyapi-stack" ;;
    custom)
      v=$(ui_input "Weights cache path" \
"Folder to search for existing weights. Any of these work:

  /mnt/usb/tabbyapi-stack          (full tree: tabbyAPI/ + ComfyUI/)
  /mnt/usb/tabbyapi-stack/tabbyAPI/models
  /data/weights                 (model dirs / .safetensors / .gguf)

The installer lists what it finds next. Blank = Hugging Face." \
"${TABBY_CACHE:-$DEFAULT_CACHE}") || return 0
      WIN_ROOT=$v
      ;;
    *) WIN_ROOT="$cache_choice" ;;
  esac
  if [[ -n "$WIN_ROOT" && ! -d "$WIN_ROOT" ]]; then
    if ui_yesno "Cache not found" \
"That path is not a directory:
  ${WIN_ROOT}

Typical cause: the USB is not mounted, or the path is wrong.

Yes = continue with Hugging Face for anything missing.
No = keep the path." 0; then
      WIN_ROOT=""
    fi
  elif [[ -n "$WIN_ROOT" ]] && ! cache_has_any_weights "$WIN_ROOT"; then
    if ui_yesno "No weights seen in cache" \
"Opened:
  ${WIN_ROOT}

Did not find tabbyAPI/, ComfyUI/, models/, or weight files there.
Yes = pick Hugging Face models that fit this GPU instead.
No = keep the path." 0; then
      WIN_ROOT=""
    fi
  fi
}

inst_edit_models() {
  local gpu_label picked rc
  GPU_VRAM_MIB="$(gpu_vram_mib)"
  gpu_label="$(gpu_prompt_label "$GPU_VRAM_MIB")"
  picked=""
  if [[ -n "$WIN_ROOT" ]]; then
    picked=$(pick_models_ui "Models found" \
"Weights found under:
  ${WIN_ROOT}

Space toggles a row. Enter confirms. Selected models are copied
into the install; anything incomplete still downloads.

GPU: ${gpu_label}" \
      cache "$WIN_ROOT")
    rc=$?
    case "$rc" in
      1) return 0 ;;
      0)
        if [[ -z "$picked" ]]; then
          ui_msg "Select at least one model" "Check at least one row, then press Enter." || true
          return 0
        fi
        MODEL_SET=$picked
        ;;
      *)
        if ui_yesno "No catalog models in that folder" \
"Nothing matching the installer catalog was found in:
  ${WIN_ROOT}

Yes = show Hugging Face models that fit this GPU instead.
No = keep core." 1; then
          WIN_ROOT=""
        else
          MODEL_SET=core
          return 0
        fi
        ;;
    esac
  fi
  if [[ -z "${picked:-}" && -z "$WIN_ROOT" ]]; then
    picked=$(pick_models_ui "Hugging Face models" \
"Hugging Face models that fit this GPU:
  ${gpu_label}

Space toggles a row. Enter confirms. Checked rows are the usual
first-install set. Re-run later to add more; finished files are skipped.

If VRAM could not be read, every catalog model is listed." \
      hf "")
    rc=$?
    case "$rc" in
      1) return 0 ;;
      0) MODEL_SET=${picked:-core} ;;
      *)
        picked=$(ui_menu "Model set" \
"Could not list individual models. Pick a preset.

core  — qwen 9B, Flux, Qwen-Image, CPU embedder
all   — every switch-to profile (needs more disk and VRAM)" \
          core "qwen 9B + Flux + Qwen-Image + embedder" \
          all "every switch-to profile") || return 0
        MODEL_SET="${picked:-core}"
        ;;
    esac
  fi
  if [[ "$MODEL_SET" == *gemma* ]]; then
    ui_msg "Gemma / Hugging Face" \
"Gemma weights may be gated on Hugging Face.

If a later download returns 401 or 403:
  huggingface-cli login
  or:  export HF_TOKEN=...
  then re-run this installer (finished files are skipped).

You do not need a token for qwen / Flux / Qwen-Image." || true
  fi
}

inst_edit_network() {
  local host port comfy
  host=$(ui_listen_host "TabbyAPI listen host" "${TABBY_NETWORK_HOST:-127.0.0.1}") || return 0
  host="${host:-127.0.0.1}"
  port=$(ui_input "TabbyAPI listen port" \
"TCP port for the API. Default 5000.

Health:  http://${host}:PORT/health
Cursor:  http://${host}:PORT/v1" \
"${TABBY_NETWORK_PORT:-5000}") || return 0
  port="${port:-5000}"
  if ! valid_port "$port"; then
    ui_msg "Invalid port" "The listen port must be a number from 1 to 65535 (got ${port})." || true
    return 0
  fi
  local lan_hint="" lan_ip lan_extras
  lan_ip="$(lan_ipv4)"
  if [[ -n "$lan_ip" ]]; then
    lan_hint="
This machine:  http://${lan_ip}:8188"
    lan_extras="$(lan_ipv4_extras "$lan_ip")"
    if [[ -n "$lan_extras" ]]; then
      lan_hint+="  (also ${lan_extras})"
    fi
  fi
  comfy=$(ui_input "ComfyUI URL" \
"HTTP URL for ComfyUI after “switch to comfy”.

Usual value:  http://127.0.0.1:8188${lan_hint}
Change this only if ComfyUI will listen somewhere else." \
"${COMFYUI_URL:-http://127.0.0.1:8188}") || return 0
  TABBY_NETWORK_HOST=$host
  TABBY_NETWORK_PORT=$port
  COMFYUI_URL="${comfy:-http://127.0.0.1:8188}"
}

inst_edit_public() {
  local v
  v=$(ui_input "Public API base URL" \
"Optional URL written into image links and the public gallery.

Examples
  https://api.example.com/v1
  https://chat.example.com/api/v1

Blank = local only (http://${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}/v1).
Leave blank if you do not have a reverse proxy or tunnel." \
"${TABBY_PUBLIC_BASE}") || return 0
  TABBY_PUBLIC_BASE=$v
}

inst_edit_tunnel() {
  local remote spec key
  remote=$(ui_input "Reverse SSH tunnel" \
"Optional SSH login for a reverse tunnel (user@host).

Leave blank if the API should stay on this machine only.

If you set a host, the next screens ask how traffic reaches
TabbyAPI, then which key this box uses to log in. You will
need to put the matching public key in authorized_keys on
that host." \
"${TABBY_SSH_REMOTE}") || return 0
  if [[ -z "$remote" ]]; then
    TABBY_SSH_REMOTE=""
    TABBY_SSH_FORWARD=""
    TABBY_SSH_KEY=""
    return 0
  fi
  spec=$(ui_input "SSH forward spec" \
"ssh -R spec: where ${remote} listens, and where
that lands on this GPU box.

  bind:remote_port:local_host:local_port

Default 127.0.0.1:12345:127.0.0.1:${TABBY_NETWORK_PORT} means:
  On ${remote}, listen on 127.0.0.1:12345
  Forward to TabbyAPI here on 127.0.0.1:${TABBY_NETWORK_PORT}

If HTTPS sits in front of that remote port, that is the
Public URL from the previous screen." \
"${TABBY_SSH_FORWARD:-127.0.0.1:12345:127.0.0.1:${TABBY_NETWORK_PORT}}") || return 0
  spec="${spec:-127.0.0.1:12345:127.0.0.1:${TABBY_NETWORK_PORT}}"
  ui_msg "SSH private key" \
"Private key this GPU box uses to log in to ${remote}.

$(ssh_tunnel_help "$remote" "$spec" "$TABBY_PUBLIC_BASE")

The installer copies that key from a weights cache if present,
otherwise it creates a new ed25519 key. After install, copy the
matching .pub onto ${remote}." || return 0
  key=$(ui_input "SSH private key" \
"Path to that private key for ${remote}.

Default is fine unless your key has another name." \
"${TABBY_SSH_KEY:-$HOME/.ssh/id_ed25519}") || return 0
  TABBY_SSH_REMOTE=$remote
  TABBY_SSH_FORWARD=$spec
  TABBY_SSH_KEY="${key:-$HOME/.ssh/id_ed25519}"
}

inst_edit_saver() {
  local rc=0 v
  apply_saver_defaults
  local yn=0
  [[ "${TABBY_SAVER_ENABLED}" == "1" ]] && yn=1
  ui_yesno "Screensaver" \
"Enable the TTY activity screensaver?

A CPU-rendered field on a spare VT (default tty8). tty1 stays a
login prompt. A key or mouse hides it. While logged in it waits
${TABBY_SAVER_IDLE_S}s with no input; after logout it waits
${TABBY_SAVER_LOGOUT_IDLE_S}s.

Do not enable if Omarchy or another desktop already owns the GPU." \
    "$yn" || rc=$?
  case "$rc" in
    2) return 0 ;;
    0)
      TABBY_SAVER_ENABLED=1
      v=$(ui_input "Screensaver" \
"Seconds of no keyboard/mouse while logged in on the console
before the screensaver returns. Default 120 (2 minutes)." \
"${TABBY_SAVER_IDLE_S}") || return 0
      TABBY_SAVER_IDLE_S="${v:-120}"
      if ! valid_seconds "$TABBY_SAVER_IDLE_S"; then
        ui_msg "Invalid idle timeout" "Use a number of seconds from 0 to 86400." || true
        TABBY_SAVER_IDLE_S=120
      fi
      v=$(ui_input "Screensaver" \
"Seconds after logging out of the console (or idle at the login
prompt) before the screensaver returns. Default 10." \
"${TABBY_SAVER_LOGOUT_IDLE_S}") || return 0
      TABBY_SAVER_LOGOUT_IDLE_S="${v:-10}"
      if ! valid_seconds "$TABBY_SAVER_LOGOUT_IDLE_S"; then
        ui_msg "Invalid logout timeout" "Use a number of seconds from 0 to 86400." || true
        TABBY_SAVER_LOGOUT_IDLE_S=10
      fi
      ;;
    *) TABBY_SAVER_ENABLED=0 ;;
  esac
}

prompt_advanced_install() {
  if [[ "${TABBY_ISO_CHROOT:-}" == 1 ]]; then
    ui_msg "tabbyapi-stack" \
"Arch is on the disk. Dest is already
  ${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}
The weights cache was set before the wipe (or Hugging Face).

A review menu lists models, listen address, optional public
URL / SSH, and screensaver. Open a row to change it."
    DEST="${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}"
    DEST="${DEST:-$DEFAULT_DEST}"
    WIN_ROOT="${TABBY_CACHE:-}"
  else
    ui_msg "What this installer does" \
"tabbyapi-stack: local OpenAI-compatible API for coding and agents,
plus ComfyUI image generation on Arch. Any client that speaks /v1
works — Cursor is one example.

Use gpt-4o as the model name in your editor, and leave it.
That is not ChatGPT — it is only a name. Many editors sandbox
or block tools unless they see a known OpenAI name. The GPU
still runs the local model you switched to.

Needed
  • Arch Linux, your user (not root), internet
  • NVIDIA GPU (docs assume 12 GB)
  • Python 3.12 — this script installs it (pyenv ${PYENV_VER} if needed)
  • sudo — installed if missing (root password once); passwordless for this user

A review menu lists every setting. Open a row to change it,
then Start install.

Source: ${TABBY_SRC}
More detail: ${SCRIPT_DIR}/README.md

Esc on the review menu cancels. Esc on a setting goes back."
    DEST="${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}"
    WIN_ROOT="${TABBY_CACHE:-}"
  fi
  MODEL_SET="${TABBY_MODELS:-core}"
  apply_network_defaults
  apply_saver_defaults
  local choice wlabel slabel
  while true; do
    apply_choices
    if [[ -z "$TABBY_SSH_REMOTE" ]]; then
      TABBY_SSH_FORWARD=""
      TABBY_SSH_KEY=""
    fi
    if [[ -n "$WIN_ROOT" ]]; then
      wlabel=$WIN_ROOT
    else
      wlabel="Hugging Face"
    fi
    if [[ "${TABBY_SAVER_ENABLED}" == "1" ]]; then
      slabel="on (idle ${TABBY_SAVER_IDLE_S}s)"
    else
      slabel="off"
    fi
    local -a items=()
    if [[ "${TABBY_ISO_CHROOT:-}" != 1 ]]; then
      items+=(dest "$(hub_desc "$DEST")")
      items+=(weights "$(hub_desc "$wlabel")")
    fi
    items+=(models "$(hub_desc "${MODEL_SET:-core}")")
    items+=(network "$(hub_desc "${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}")")
    items+=(public "$(hub_desc "${TABBY_PUBLIC_BASE:- (local only)}")")
    items+=(tunnel "$(hub_desc "${TABBY_SSH_REMOTE:- (none)}")")
    items+=(saver "$(hub_desc "$slabel")")
    items+=(go "Start install")
    choice=$(ui_menu "Review install plan" \
"Open a row to change it. Choose Start install when the plan
looks right.

Esc aborts. The next screen is a progress bar and the live log." \
      "${items[@]}") || ui_cancel
    UI_ALLOW_BACK=1
    case "$choice" in
      dest) inst_edit_dest ;;
      weights) inst_edit_weights ;;
      models) inst_edit_models ;;
      network) inst_edit_network ;;
      public) inst_edit_public ;;
      tunnel) inst_edit_tunnel ;;
      saver) inst_edit_saver ;;
      go)
        UI_ALLOW_BACK=0
        apply_choices
        apply_network_defaults
        if [[ -z "$TABBY_SSH_REMOTE" ]]; then
          TABBY_SSH_FORWARD=""
          TABBY_SSH_KEY=""
        fi
        apply_saver_defaults
        if ! valid_model_set "$MODEL_SET"; then
          ui_msg "Invalid model set" "Pick at least one model (got ${MODEL_SET:-empty})."
          continue
        fi
        if ! dest_is_sane; then
          ui_msg "Invalid install root" \
"Refusing to install into:
  ${DEST}

That is your home directory or a system folder. Pick a dedicated
folder such as ${HOME}/tabbyapi-stack or /data/tabbyapi-stack."
          continue
        fi
        if cache_on_dest; then
          ui_msg "Invalid paths" \
"Arch dest must not be the weights cache mount.

  Cache:     ${WIN_ROOT}
  Arch dest: ${DEST}

Use the Linux disk, for example ${HOME}/tabbyapi-stack or /data/tabbyapi-stack."
          continue
        fi
        API_URL="http://${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}"
        break
        ;;
    esac
    UI_ALLOW_BACK=0
  done
}

DEFAULT_DEST="$HOME/tabbyapi-stack"

if [[ "$INTERACTIVE" -eq 0 ]]; then
  if [[ "$UPDATE_MODE" -eq 1 ]]; then
    DEST="${TABBY_INSTALL_ROOT:-$STACK_ROOT}"
    DEST="$(abs_path "${DEST%/}")"
    DEST="${DEST%/}"
    load_tabby_env_file "$DEST/tabbyAPI/deploy/arch/tabby.env"
    DEST="$(abs_path "${TABBY_INSTALL_ROOT:-$DEST}")"
    DEST="${DEST%/}"
  else
    DEST="${TABBY_INSTALL_ROOT:-$DEFAULT_DEST}"
  fi
  if [[ -n "${TABBY_CACHE+x}" ]]; then
    WIN_ROOT="$TABBY_CACHE"
  else
    WIN_ROOT="$DEFAULT_CACHE"
  fi
  MODEL_SET="${TABBY_MODELS:-core}"
  apply_choices
  apply_network_defaults
  apply_saver_defaults
  if ! valid_model_set "$MODEL_SET"; then
    echo "Model set must be core, all, or comma-separated ids (got $MODEL_SET)."
    exit 1
  fi
  if ! valid_port "$TABBY_NETWORK_PORT"; then
    echo "TABBY_NETWORK_PORT must be 1-65535 (got $TABBY_NETWORK_PORT)."
    exit 1
  fi
  if ! dest_is_sane; then
    echo "Refusing to install into $DEST. Pick a dedicated folder, e.g. $HOME/tabbyapi-stack."
    exit 1
  fi
  if [[ "$UPDATE_MODE" -eq 1 && ! -x "$DEST_TABBY/venv/bin/python" ]]; then
    echo "No TabbyAPI venv at $DEST_TABBY."
    echo "Run bash update.sh from the install root (default \$HOME/tabbyapi-stack),"
    echo "not from a source checkout that has not been installed."
    exit 1
  fi
  if cache_on_dest; then
    echo "Arch dest must not be the weights cache mount."
    echo "  Cache:     $WIN_ROOT"
    echo "  Arch dest: $DEST"
    echo "Use the Arch disk, e.g. $HOME/tabbyapi-stack or /data/tabbyapi-stack"
    exit 1
  fi
else
  pick_install_mode
  if [[ "$INSTALL_MODE" == simple ]]; then
    prompt_simple_install
  else
    prompt_advanced_install
  fi
fi


apply_network_defaults
apply_saver_defaults
if [[ -z "$TABBY_SSH_REMOTE" ]]; then
  TABBY_SSH_FORWARD=""
  TABBY_SSH_KEY=""
fi
API_URL="http://${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}"

# Rough floor: venvs and CUDA wheels are ~15 GiB, weights are the rest.
NEED_GIB=45
if need_cmd python3 && [[ -f "$FETCH_MODELS" ]]; then
  WEIGHT_GIB="$(python3 -u "$FETCH_MODELS" --catalog "$CATALOG" --ids "$MODEL_SET" --disk-gib 2>/dev/null || true)"
  if [[ "$WEIGHT_GIB" =~ ^[0-9]+$ ]]; then
    NEED_GIB=$((WEIGHT_GIB + 15))
  fi
fi
[[ "$MODEL_SET" == "all" && "$NEED_GIB" -lt 90 ]] && NEED_GIB=90
HAVE_GIB="$(free_gib "$DEST")"
if [[ -n "$HAVE_GIB" ]] && ((HAVE_GIB < NEED_GIB)); then
  SPACE_MSG="Only ${HAVE_GIB} GiB free on the filesystem holding ${DEST}.
The selected models plus the two Python environments need about
${NEED_GIB} GiB. The install will fail part-way through a download.

Free some space, or pick fewer models / a different disk."
  if [[ "$INTERACTIVE" -eq 1 ]]; then
    if ! ui_yesno "Low disk space" "$SPACE_MSG

Continue anyway?" 0; then
      exit 1
    fi
  else
    echo "WARNING: $SPACE_MSG"
  fi
fi

OUR_UNIT_ACTIVE=0
if need_cmd systemctl && systemctl --user is-active --quiet tabbyapi 2>/dev/null; then
  OUR_UNIT_ACTIVE=1
fi
if [[ "$OUR_UNIT_ACTIVE" -eq 0 ]] && port_in_use "$TABBY_NETWORK_PORT"; then
  PORT_MSG="Something is already listening on port ${TABBY_NETWORK_PORT}.
That is usually an older TabbyAPI (systemctl --user status tabbyapi) or a
manual start.sh. Two copies would both load the model and exhaust the GPU.

The installer will not start the service while the port is taken."
  if [[ "$INTERACTIVE" -eq 1 ]]; then
    if ! ui_yesno "Port ${TABBY_NETWORK_PORT} is in use" "$PORT_MSG

Continue installing anyway?" 0; then
      exit 1
    fi
  else
    echo "WARNING: $PORT_MSG"
  fi
fi

PACKAGES=(
  sudo
  nvidia-utils
  python
  python-pip
  git
  rsync
  openssh
  ntfs-3g
  base-devel
  cmake
  ninja
  pkgconf
  wget
  curl
  which
  procps-ng
  pciutils
  iproute2
  inetutils   # hostname for the TSOS login MOTD
  ca-certificates
  openssl
  zlib
  xz
  tk
  readline
  sqlite
  bzip2
  ncurses
  gdbm
  libffi
  libjpeg-turbo
  libpng
  libtiff
  libwebp
  freetype2
  openjpeg2
  lcms2
  ffmpeg
  # ffmpeg depends on virtual "jack"; pin jack2 so pacman does not stop
  # for jack2 vs pipewire-jack on the ISO.
  jack2
  mesa
  libglvnd
  dos2unix
  dialog
  nodejs
  npm
  docker
  # Screensaver deps: always install so Settings / tsctl enable works later.
  python-pygame
  python-numpy
)

ensure_sudo
if need_cmd sudo; then
  ( while true; do sudo -n true && sleep 50 || exit; done ) >/dev/null 2>&1 &
  SUDO_KEEPALIVE_PID=$!
fi
progress_start
trap 'rc=$?; if [[ "$INSTALL_FAILED" -eq 0 && "$rc" -ne 0 ]]; then progress_fail "$rc"; else progress_stop; fi' EXIT

NVIDIA_DRIVER_INSTALLED_NOW=0
NVIDIA_SMI_OK=0
if [[ "$UPDATE_MODE" -eq 0 ]]; then
  progress 4 "Syncing packages"
  if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    echo "Using frozen TSOS package repository at $TSOS_OFFLINE_ROOT/pacman" >>"$INSTALL_LOG"
  else
    run_quiet sudo -n pacman -Sy --noconfirm
  fi
fi

if nvidia_smi_ok; then
  NVIDIA_SMI_OK=1
else
  nvidia_kmod=$(nvidia_kernel_pkg) || {
    echo "NVIDIA kernel package not in the repos (tried nvidia-open, then nvidia)." >> "$INSTALL_LOG"
    progress_fail 1
  }
  PACKAGES+=("$nvidia_kmod")
  echo "NVIDIA kernel package: $nvidia_kmod" >> "$INSTALL_LOG"
  # Only a package that was missing this run needs a reboot. tsos-installer
  # already pacstrapped nvidia-open; --needed is a no-op after the ISO reboot.
  if ! pacman -Q "$nvidia_kmod" >/dev/null 2>&1; then
    NVIDIA_DRIVER_INSTALLED_NOW=1
  fi
  if pacman -Q linux >/dev/null 2>&1; then
    PACKAGES+=(linux-headers)
  fi
  if pacman -Q linux-lts >/dev/null 2>&1; then
    PACKAGES+=(linux-lts-headers)
  fi
fi

if [[ "$UPDATE_MODE" -eq 1 ]]; then
  # Do not pacman -Sy / upgrade installed pkgs. That is pacman -Syu.
  # Only install names the stack needs that are not on the system yet.
  progress 10 "Checking packages"
  missing=()
  for p in "${PACKAGES[@]}"; do
    package_missing "$p" && missing+=("$p")
  done
  if ((${#missing[@]})); then
    echo "Installing missing packages: ${missing[*]}" >> "$INSTALL_LOG"
    run_quiet sudo -n pacman -S --needed --noconfirm "${missing[@]}"
  fi
else
  progress 10 "Installing packages"
  install_packages=()
  for p in "${PACKAGES[@]}"; do
    if [[ "$p" == jack2 ]] && pacman -Q pipewire-jack >/dev/null 2>&1; then
      continue
    fi
    install_packages+=("$p")
  done
  run_quiet sudo -n pacman -S --needed --noconfirm "${install_packages[@]}"
fi

if ! need_cmd nvidia-smi; then
  echo "nvidia-smi not found after package install." >> "$INSTALL_LOG"
  progress_fail 1
fi
if nvidia_smi_ok; then
  NVIDIA_SMI_OK=1
elif try_load_nvidia; then
  echo "Loaded NVIDIA kernel module without reboot." >> "$INSTALL_LOG"
  NVIDIA_SMI_OK=1
elif [[ "${TABBY_NVIDIA_REBOOT_DONE:-}" == 1 ]]; then
  echo "nvidia-smi still fails after the NVIDIA reboot." >> "$INSTALL_LOG"
  nvidia-smi >>"$INSTALL_LOG" 2>&1 || true
  progress_fail 1
elif [[ "$NVIDIA_DRIVER_INSTALLED_NOW" -eq 1 ]] && pci_has_nvidia && \
     [[ "${TABBY_SKIP_NVIDIA_REBOOT:-}" != 1 ]]; then
  if [[ "$UPDATE_MODE" -eq 1 ]]; then
    echo "nvidia-smi failed during update; not rebooting." >> "$INSTALL_LOG"
    progress_fail 1
  fi
  schedule_nvidia_reboot
elif pci_has_nvidia; then
  # tsos already rebooted off the ISO with nvidia-open on disk. venvs and
  # weights do not need the GPU. Enable tabbyapi; start it only if smi works.
  echo "nvidia-smi failed; skipping mid-install reboot (driver already installed)." >> "$INSTALL_LOG"
  nvidia-smi >>"$INSTALL_LOG" 2>&1 || true
  NVIDIA_SMI_OK=0
else
  echo "nvidia-smi failed and a reboot will not help." >> "$INSTALL_LOG"
  nvidia-smi >>"$INSTALL_LOG" 2>&1 || true
  progress_fail 1
fi
if [[ "$NVIDIA_SMI_OK" -eq 1 ]]; then
  run_quiet nvidia-smi
fi

enable_docker() {
  # ISO chroot has no systemd. Enable the unit; start it only on a real boot.
  if [[ -d /run/systemd/system ]]; then
    sudo -n systemctl enable --now docker >>"$INSTALL_LOG" 2>&1 || \
      echo "WARNING: could not enable docker.service" >> "$INSTALL_LOG"
  else
    sudo -n systemctl enable docker >>"$INSTALL_LOG" 2>&1 || \
      echo "WARNING: could not enable docker.service" >> "$INSTALL_LOG"
    echo "systemd is not running; docker will start on the first real boot." >> "$INSTALL_LOG"
  fi
  sudo -n usermod -aG docker "$USER" >>"$INSTALL_LOG" 2>&1 || true
}

drop_codebox_containers() {
  local ids=()
  if need_cmd docker; then
    mapfile -t ids < <(docker ps -aq --filter label=tabby.stack=code 2>/dev/null || true)
    if ((${#ids[@]})); then
      docker rm -f "${ids[@]}" >>"$INSTALL_LOG" 2>&1 || \
        sudo -n docker rm -f "${ids[@]}" >>"$INSTALL_LOG" 2>&1 || true
    fi
  fi
}

build_codebox_image() {
  local df="$DEST_TABBY/ui/codebox/Dockerfile"
  local dir="$DEST_TABBY/ui/codebox"
  [[ -f "$df" ]] || return 0
  [[ -f /var/lib/tsos/offline-docker-loaded ]] && return 0
  if sudo -n docker image inspect tabbyapi-stack-code:local >/dev/null 2>&1; then
    return 0
  fi
  if sudo -n docker build -t tabbyapi-stack-code:local -f "$df" "$dir" >>"$INSTALL_LOG" 2>&1; then
    drop_codebox_containers
    return 0
  fi
  if need_cmd docker && docker build -t tabbyapi-stack-code:local -f "$df" "$dir" >>"$INSTALL_LOG" 2>&1; then
    drop_codebox_containers
    return 0
  fi
  echo "WARNING: tabbyapi-stack-code image build failed" >> "$INSTALL_LOG"
}

enable_docker

load_offline_docker_images() {
  local archive="${TSOS_OFFLINE_ROOT:-}/docker/codebox-images.tar"
  [[ -f "$archive" ]] || return 0
  if sudo -n docker load -i "$archive" >>"$INSTALL_LOG" 2>&1; then
    sudo -n install -D -m 0644 /dev/null /var/lib/tsos/offline-docker-loaded
    return 0
  fi
  # systemd is not running in the installer chroot. Start a private daemon
  # long enough to import the image into /var/lib/docker for first boot.
  local socket=/run/tsos-offline-docker.sock pid i
  sudo -n rm -f "$socket" /run/tsos-offline-docker.pid
  sudo -n dockerd --host "unix://$socket" --pidfile /run/tsos-offline-docker.pid \
    >>"$INSTALL_LOG" 2>&1 &
  pid=$!
  for i in $(seq 1 45); do
    [[ -S "$socket" ]] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if [[ -S "$socket" ]]; then
    if sudo -n env DOCKER_HOST="unix://$socket" docker load -i "$archive" >>"$INSTALL_LOG" 2>&1; then
      sudo -n install -D -m 0644 /dev/null /var/lib/tsos/offline-docker-loaded
    fi
  fi
  sudo -n kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

load_offline_docker_images

progress 16 "Checking Python 3.12"
run_quiet ensure_python312
PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PY_VER" != "3.12" ]]; then
  echo "Need Python 3.12 (got $PY_VER)." >> "$INSTALL_LOG"
  progress_fail 1
fi

progress 22 "Syncing tabbyapi-stack sources"
run_quiet sync_tabby_sources_to_dest
if need_cmd dos2unix; then
  run_quiet find "$DEST_TABBY" -type f -name '*.sh' -exec dos2unix -q {} +
else
  run_quiet find "$DEST_TABBY" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
fi
progress 24 "Building Code sandbox image"
build_codebox_image
chmod 755 "$DEST_TABBY/deploy/arch/run-api.sh" 2>/dev/null || true
if [[ -d "$DEST_TABBY/venv/Scripts" ]]; then
  rm -rf "$DEST_TABBY/venv"
fi
PATCH_SPAWN="$DEST_TABBY/deploy/arch/patch_linux_spawn.py"
if [[ ! -f "$PATCH_SPAWN" ]]; then
  PATCH_SPAWN="$SCRIPT_DIR/patch_linux_spawn.py"
fi
if [[ -f "$PATCH_SPAWN" ]]; then
  run_quiet "$PY" "$PATCH_SPAWN" "$DEST_TABBY"
fi

CREATED_CONFIG=0
if [[ ! -f "$DEST_TABBY/config.yml" && -f "$DEST_TABBY/config_sample.yml" ]]; then
  cp "$DEST_TABBY/config_sample.yml" "$DEST_TABBY/config.yml"
  CREATED_CONFIG=1
fi
# Embedding only here. The LLM folder is set after weights copy from what
# actually landed in models/ (select_model.py --seed-installed --ids).
if [[ "$CREATED_CONFIG" -eq 1 ]]; then
  "$PY" -c "
from pathlib import Path
p = Path(r'''$DEST_TABBY/config.yml''')
text = p.read_text(encoding='utf-8')
out = []
for line in text.splitlines(True):
    if line.startswith('  embedding_model_name:'):
        out.append('  embedding_model_name: $EMBED_NAME\n')
    else:
        out.append(line)
p.write_text(''.join(out), encoding='utf-8')
" >>"$INSTALL_LOG" 2>&1 || progress_fail
fi
mkdir -p "$DEST_TABBY/model_profiles" "$DEST_TABBY/models"
if [[ ! -s "$DEST_TABBY/model_profiles/gpu_mode.json" ]]; then
  printf '%s\n' '{"mode": "llm"}' > "$DEST_TABBY/model_profiles/gpu_mode.json"
fi

progress 28 "Installing ComfyUI"
if [[ ! -f "$DEST_COMFY/main.py" ]]; then
  if comfy_bundle=$(tsos_bundle ComfyUI); then
    run_quiet git clone "$comfy_bundle" "$DEST_COMFY"
    run_quiet git -C "$DEST_COMFY" remote set-url origin https://github.com/comfyanonymous/ComfyUI.git
  else
    run_quiet git clone https://github.com/comfyanonymous/ComfyUI.git "$DEST_COMFY"
  fi
fi
mkdir -p "$DEST_COMFY/models/checkpoints"
if [[ -d "$DEST_COMFY/venv/Scripts" ]]; then
  rm -rf "$DEST_COMFY/venv"
fi

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
SSH_KEY_NAME="$(basename "${TABBY_SSH_KEY:-id_ed25519}")"
if [[ -n "$WIN_ROOT" && -f "$WIN_ROOT/.ssh/$SSH_KEY_NAME" ]]; then
  cp -f "$WIN_ROOT/.ssh/$SSH_KEY_NAME" "$HOME/.ssh/$SSH_KEY_NAME"
  chmod 600 "$HOME/.ssh/$SSH_KEY_NAME"
  if need_cmd dos2unix; then
    dos2unix -q "$HOME/.ssh/$SSH_KEY_NAME" 2>/dev/null || dos2unix "$HOME/.ssh/$SSH_KEY_NAME"
  else
    sed -i 's/\r$//' "$HOME/.ssh/$SSH_KEY_NAME"
  fi
  if [[ -f "$WIN_ROOT/.ssh/${SSH_KEY_NAME}.pub" ]]; then
    cp -f "$WIN_ROOT/.ssh/${SSH_KEY_NAME}.pub" "$HOME/.ssh/${SSH_KEY_NAME}.pub"
    if need_cmd dos2unix; then
      dos2unix -q "$HOME/.ssh/${SSH_KEY_NAME}.pub" 2>/dev/null || dos2unix "$HOME/.ssh/${SSH_KEY_NAME}.pub"
    else
      sed -i 's/\r$//' "$HOME/.ssh/${SSH_KEY_NAME}.pub"
    fi
  fi
fi
# Cache copy is optional. A reverse tunnel (or a bare login key) still needs
# a key on this account — generate one when nothing was copied.
if [[ ! -f "$HOME/.ssh/$SSH_KEY_NAME" ]]; then
  if need_cmd ssh-keygen; then
    comment="${USER}@$(hostname -s 2>/dev/null || echo tsos)-tabbyapi-stack"
    ssh-keygen -q -t ed25519 -N '' -f "$HOME/.ssh/$SSH_KEY_NAME" -C "$comment"
    chmod 600 "$HOME/.ssh/$SSH_KEY_NAME"
    [[ -f "$HOME/.ssh/${SSH_KEY_NAME}.pub" ]] && chmod 644 "$HOME/.ssh/${SSH_KEY_NAME}.pub"
    echo "Created SSH key $HOME/.ssh/$SSH_KEY_NAME" >> "$INSTALL_LOG"
    if [[ -n "${TABBY_SSH_REMOTE:-}" ]]; then
      echo "Install this public key on ${TABBY_SSH_REMOTE}:" >> "$INSTALL_LOG"
      cat "$HOME/.ssh/${SSH_KEY_NAME}.pub" >> "$INSTALL_LOG"
    fi
  else
    echo "WARNING: ssh-keygen missing; no $HOME/.ssh/$SSH_KEY_NAME" >> "$INSTALL_LOG"
  fi
fi

# Wheels + imports. Runtime GPU only when nvidia-smi already works — the ISO
# chroot has nvidia-open on disk but no loaded driver, so
# torch.cuda.is_available() is false even with a good cu12/cu13 install.
venv_cuda_ok() {
  local py="$1"
  local imports="$2"
  local err
  [[ -x "$py" ]] || return 1
  local pycode
  pycode="import ${imports}
assert torch.version.cuda, 'torch is a CPU build (torch.version.cuda is empty)'"
  if [[ "${NVIDIA_SMI_OK:-0}" -eq 1 ]]; then
    pycode+=$'\n'"assert torch.cuda.is_available(), 'torch.cuda.is_available() is False'"
  fi
  if err=$("$py" -c "$pycode" 2>&1); then
    return 0
  fi
  echo "$err" >> "$INSTALL_LOG"
  return 1
}

tabby_venv_ok() {
  venv_cuda_ok "$DEST_TABBY/venv/bin/python" "torch, exllamav3"
}

install_tabby_cu12() {
  local py="$DEST_TABBY/venv/bin/python"
  if ((${#PIP_OFFLINE_ARGS[@]})); then
    # cu12 uses direct HTTPS wheel references in pyproject.toml. Direct URLs
    # bypass --find-links, so install their frozen equivalents first and then
    # install the base project without evaluating that extra.
    run_quiet "$py" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U \
      'torch==2.9.0+cu128' \
      'exllamav3==1.4.6+cu128.torch2.9.0' \
      triton 'flash-linear-attention>=0.5.0'
    run_quiet env -C "$DEST_TABBY" "$py" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U .
  else
    run_quiet env -C "$DEST_TABBY" "$py" -m pip install -U ".[cu12]"
  fi
}

progress 40 "TabbyAPI Python environment"
if ! tabby_venv_ok; then
  rm -rf "$DEST_TABBY/venv"
  run_quiet "$PY" -m venv "$DEST_TABBY/venv"
  run_quiet "$DEST_TABBY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U pip setuptools wheel packaging
  install_tabby_cu12
  if ! tabby_venv_ok; then
    echo "TabbyAPI venv check failed (torch/exllamav3 import or CUDA-built torch)." >> "$INSTALL_LOG"
    if [[ "${NVIDIA_SMI_OK:-0}" -eq 0 ]]; then
      echo "nvidia-smi is down (expected in the ISO chroot); runtime CUDA was not required." >> "$INSTALL_LOG"
    fi
    progress_fail 1
  fi
elif [[ "$UPDATE_MODE" -eq 1 ]]; then
  progress 45 "Updating TabbyAPI Python packages"
  install_tabby_cu12
fi
if [[ "$UPDATE_MODE" -eq 1 ]] || ! "$DEST_TABBY/venv/bin/python" -c "import infinity_emb, sentence_transformers" >/dev/null 2>&1; then
  progress 55 "TabbyAPI extras"
  run_quiet env -C "$DEST_TABBY" "$DEST_TABBY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U ".[extras]"
fi
run_quiet "$DEST_TABBY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U 'numpy>=2.1.0'
if [[ -f "$DEST_TABBY/ui/fetch_monaco.py" &&
      ! -f "$DEST_TABBY/ui/static/vs/loader.js" ]]; then
  progress 56 "Monaco editor"
  run_quiet "$DEST_TABBY/venv/bin/python" "$DEST_TABBY/ui/fetch_monaco.py"
fi
if [[ "$UPDATE_MODE" -eq 0 && -x "$DEST_TABBY/venv/bin/python" && -f "$DEST_TABBY/switch_model.py" ]]; then
  ( cd "$DEST_TABBY" && "$DEST_TABBY/venv/bin/python" switch_model.py qwen --no-load ) >>"$INSTALL_LOG" 2>&1 || true
fi

comfy_venv_ok() {
  venv_cuda_ok "$DEST_COMFY/venv/bin/python" "torch"
}

# Comfy kitchen CUDA kernels need a PyTorch build for CUDA 13.0+.
comfy_torch_cu13() {
  [[ -x "$DEST_COMFY/venv/bin/python" ]] && \
    "$DEST_COMFY/venv/bin/python" -c "import torch; v=torch.version.cuda or '0'; assert tuple(int(x) for x in v.split('.')[:2]) >= (13, 0)"
}

install_comfy_torch() {
  # cu130 torch first: requirements.txt would otherwise pull the PyPI build and
  # this would download ~2.5 GB of wheels twice.
  if ((${#PIP_OFFLINE_ARGS[@]})); then
    run_quiet "$DEST_COMFY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" \
      -U torch torchvision torchaudio
  else
    run_quiet "$DEST_COMFY/venv/bin/python" -m pip install -U torch torchvision torchaudio \
      --index-url https://download.pytorch.org/whl/cu130
  fi
  # Drop leftover CUDA 12 NVIDIA wheels so start.sh does not prepend their lib dirs.
  # Those packages share nvidia/{cudnn,nccl,...} paths with the cu13 builds, so
  # uninstall can delete the CUDA 13 .so files; put the same cu13 pins back.
  mapfile -t oldcuda < <("$DEST_COMFY/venv/bin/python" -m pip freeze | awk -F== 'tolower($1) ~ /-cu12$/ {print $1}')
  mapfile -t cu13nvidia < <("$DEST_COMFY/venv/bin/python" -m pip freeze | awk -F== 'tolower($1) ~ /^nvidia-.*-cu13$/ {print}')
  if ((${#oldcuda[@]})); then
    run_quiet "$DEST_COMFY/venv/bin/python" -m pip uninstall -y "${oldcuda[@]}"
    if ((${#cu13nvidia[@]})); then
      if ((${#PIP_OFFLINE_ARGS[@]})); then
        run_quiet "$DEST_COMFY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" \
          --force-reinstall --no-deps "${cu13nvidia[@]}"
      else
        run_quiet "$DEST_COMFY/venv/bin/python" -m pip install --force-reinstall --no-deps \
          "${cu13nvidia[@]}" --index-url https://download.pytorch.org/whl/cu130
      fi
    fi
  fi
}

progress 62 "ComfyUI Python environment"
if ! comfy_venv_ok; then
  rm -rf "$DEST_COMFY/venv"
  run_quiet "$PY" -m venv "$DEST_COMFY/venv"
  run_quiet "$DEST_COMFY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U pip setuptools wheel
  install_comfy_torch
  if [[ -f "$DEST_COMFY/requirements.txt" ]]; then
    run_quiet "$DEST_COMFY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -r "$DEST_COMFY/requirements.txt"
  fi
  if ! comfy_venv_ok || ! comfy_torch_cu13; then
    echo "ComfyUI venv check failed (import or CUDA 13 torch build)." >> "$INSTALL_LOG"
    if [[ "${NVIDIA_SMI_OK:-0}" -eq 0 ]]; then
      echo "nvidia-smi is down (expected in the ISO chroot); runtime CUDA was not required." >> "$INSTALL_LOG"
    fi
    progress_fail 1
  fi
elif ! comfy_torch_cu13; then
  install_comfy_torch
  if ! comfy_torch_cu13; then
    echo "ComfyUI torch CUDA 13 upgrade failed." >> "$INSTALL_LOG"
    progress_fail 1
  fi
elif [[ "${TABBY_UPDATE_COMFY:-}" == 1 && -f "$DEST_COMFY/requirements.txt" ]]; then
  progress 65 "Updating ComfyUI Python packages"
  run_quiet "$DEST_COMFY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U -r "$DEST_COMFY/requirements.txt"
fi

cat > "$DEST_COMFY/start.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
extra=()
for lib in venv/lib/python*/site-packages/nvidia/*/lib; do
  [[ -d "$lib" ]] && extra+=("$lib")
done
if ((${#extra[@]})); then
  joined=$(IFS=:; echo "${extra[*]}")
  export LD_LIBRARY_PATH="${joined}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
EOF
printf 'exec ./venv/bin/python -u main.py --listen %s --port %s "$@"\n' \
  "$COMFY_LISTEN_HOST" "$COMFY_LISTEN_PORT" >> "$DEST_COMFY/start.sh"
chmod +x "$DEST_COMFY/start.sh"

progress 78 "ComfyUI-GGUF"
mkdir -p "$DEST_COMFY/custom_nodes"
if [[ ! -f "$DEST_COMFY/custom_nodes/ComfyUI-GGUF/nodes.py" ]]; then
  if gguf_bundle=$(tsos_bundle ComfyUI-GGUF); then
    run_quiet git clone "$gguf_bundle" "$DEST_COMFY/custom_nodes/ComfyUI-GGUF"
    run_quiet git -C "$DEST_COMFY/custom_nodes/ComfyUI-GGUF" remote set-url origin https://github.com/city96/ComfyUI-GGUF.git
  else
    run_quiet git clone --depth 1 https://github.com/city96/ComfyUI-GGUF "$DEST_COMFY/custom_nodes/ComfyUI-GGUF"
  fi
fi
if [[ -f "$DEST_COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt" ]]; then
  run_quiet "$DEST_COMFY/venv/bin/python" -m pip install "${PIP_OFFLINE_ARGS[@]}" -U -r "$DEST_COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"
fi

progress 84 "Copying model weights"
mkdir -p \
  "$DEST_TABBY/models" \
  "$DEST_COMFY/models/checkpoints" \
  "$DEST_COMFY/models/unet" \
  "$DEST_COMFY/models/text_encoders" \
  "$DEST_COMFY/models/vae" \
  "$DEST_COMFY/models/loras"
DEST_CATALOG="$DEST_TABBY/deploy/arch/models.json"
[[ -f "$DEST_CATALOG" ]] || DEST_CATALOG="$CATALOG"
DEST_FETCH="$DEST_TABBY/deploy/arch/fetch_models.py"
[[ -f "$DEST_FETCH" ]] || DEST_FETCH="$FETCH_MODELS"
FETCH_ARGS=(
  --catalog "$DEST_CATALOG"
  --tabby "$DEST_TABBY"
  --comfy "$DEST_COMFY"
  --ids "$MODEL_SET"
  --update-catalog
)
if [[ -n "$WIN_ROOT" && -d "$WIN_ROOT" ]]; then
  FETCH_ARGS+=(--cache "$WIN_ROOT")
fi
run_quiet "$DEST_TABBY/venv/bin/python" -u "$DEST_FETCH" "${FETCH_ARGS[@]}"

# Point last.json / model_name at an LLM that was actually copied. Do not
# assume qwen 9B — USB subset and extra local folders are valid too.
run_quiet "$DEST_TABBY/venv/bin/python" -u "$DEST_TABBY/select_model.py" \
  --seed-installed --ids "$MODEL_SET"

progress 94 "Writing config and enabling service"
install_unless_same() {
  local mode="$1" src="$2" dest="$3"
  if [[ "$src" -ef "$dest" ]]; then
    chmod "$mode" "$dest" || true
    return 0
  fi
  install -m "$mode" "$src" "$dest"
}

if [[ -f "$SCRIPT_DIR/start.sh" ]]; then
  run_quiet install_unless_same 755 "$SCRIPT_DIR/start.sh" "$DEST/start.sh"
else
  echo "Missing $SCRIPT_DIR/start.sh" >> "$INSTALL_LOG"
  progress_fail 1
fi
if [[ -f "$STACK_ROOT/AGENTS.md" ]]; then
  run_quiet install_unless_same 644 "$STACK_ROOT/AGENTS.md" "$DEST/AGENTS.md"
else
  echo "Missing $STACK_ROOT/AGENTS.md" >> "$INSTALL_LOG"
  progress_fail 1
fi
if [[ "$UPDATE_MODE" -eq 0 || ! -f "$DEST_TABBY/deploy/arch/tabby.env" ]]; then
  write_tabby_env "$DEST_TABBY/deploy/arch/tabby.env"
fi

UNIT_SRC="$DEST_TABBY/deploy/arch/tabbyapi.service"
if [[ ! -f "$UNIT_SRC" ]]; then
  UNIT_SRC="$SCRIPT_DIR/tabbyapi.service"
fi
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|__TABBY_DIR__|$DEST_TABBY|g" "$UNIT_SRC" > "$UNIT_DIR/tabbyapi.service"

COMFY_UNIT_SRC="$DEST_TABBY/deploy/arch/comfyui.service"
if [[ ! -f "$COMFY_UNIT_SRC" ]]; then
  COMFY_UNIT_SRC="$SCRIPT_DIR/comfyui.service"
fi
if [[ -f "$COMFY_UNIT_SRC" ]]; then
  sed "s|__COMFY_DIR__|$DEST_COMFY|g" "$COMFY_UNIT_SRC" > "$UNIT_DIR/comfyui.service"
fi

# KMS kiosk on a spare VT. Default on for TTY/TSOS; off if a desktop owns the GPU.
# python-pygame/numpy and video/input/tty are always installed so enable later works.
SAVER_UNIT_SRC="$DEST_TABBY/deploy/arch/tabby-saver.service"
if [[ ! -f "$SAVER_UNIT_SRC" ]]; then
  SAVER_UNIT_SRC="$SCRIPT_DIR/tabby-saver.service"
fi
if [[ -f "$SAVER_UNIT_SRC" ]]; then
  SAVER_TTY="${TABBY_SAVER_TTY:-tty8}"
  USER_TTY="${TABBY_SAVER_USER_TTY:-tty1}"
  if [[ "$SAVER_TTY" == "$USER_TTY" ]]; then
    SAVER_TTY=tty8
  fi
  SAVER_TMP="$(mktemp)"
  sed \
    -e "s|__TABBY_DIR__|$DEST_TABBY|g" \
    -e "s|__SAVER_USER__|$USER|g" \
    -e "s|__SAVER_HOME__|$HOME|g" \
    -e "s|__SAVER_TTY__|$SAVER_TTY|g" \
    -e "s|__USER_TTY__|$USER_TTY|g" \
    -e "s|__SAVER_URL__|http://127.0.0.1:${TABBY_NETWORK_PORT}|g" \
    "$SAVER_UNIT_SRC" > "$SAVER_TMP"
  if sudo -n install -m 644 "$SAVER_TMP" /etc/systemd/system/tabby-saver.service \
       >>"$INSTALL_LOG" 2>&1; then
    sudo -n systemctl daemon-reload >>"$INSTALL_LOG" 2>&1 || true
    echo "Wrote /etc/systemd/system/tabby-saver.service" >> "$INSTALL_LOG"
    sudo -n usermod -aG video,input,tty "$USER" >>"$INSTALL_LOG" 2>&1 || true
    if [[ "${TABBY_SAVER_ENABLED:-1}" == "1" ]]; then
      if sudo -n systemctl enable tabby-saver >>"$INSTALL_LOG" 2>&1; then
        echo "Enabled tabby-saver" >> "$INSTALL_LOG"
      else
        sudo -n mkdir -p /etc/systemd/system/multi-user.target.wants >>"$INSTALL_LOG" 2>&1 || true
        if sudo -n ln -sfn /etc/systemd/system/tabby-saver.service \
             /etc/systemd/system/multi-user.target.wants/tabby-saver.service \
             >>"$INSTALL_LOG" 2>&1; then
          echo "Enabled tabby-saver via wants symlink" >> "$INSTALL_LOG"
        else
          echo "WARNING: could not enable tabby-saver" >> "$INSTALL_LOG"
        fi
      fi
      # ISO chroot has no target GPU/TTY; first real boot starts the enabled unit.
      if [[ "${TABBY_ISO_CHROOT:-}" != 1 ]]; then
        sudo -n systemctl start tabby-saver >>"$INSTALL_LOG" 2>&1 || true
      fi
    fi
  else
    echo "WARNING: could not write /etc/systemd/system/tabby-saver.service" >> "$INSTALL_LOG"
  fi
  rm -f "$SAVER_TMP"
fi

# NVIDIA fan/power controller. Root NVML; Settings / tsctl gpu write tabby.env.
GPU_UNIT_SRC="$DEST_TABBY/deploy/arch/tabby-gpu.service"
if [[ ! -f "$GPU_UNIT_SRC" ]]; then
  GPU_UNIT_SRC="$SCRIPT_DIR/tabby-gpu.service"
fi
if [[ -f "$GPU_UNIT_SRC" ]]; then
  GPU_TMP="$(mktemp)"
  sed -e "s|__TABBY_DIR__|$DEST_TABBY|g" "$GPU_UNIT_SRC" > "$GPU_TMP"
  if sudo -n install -m 644 "$GPU_TMP" /etc/systemd/system/tabby-gpu.service \
       >>"$INSTALL_LOG" 2>&1; then
    sudo -n systemctl daemon-reload >>"$INSTALL_LOG" 2>&1 || true
    if sudo -n systemctl enable --now tabby-gpu >>"$INSTALL_LOG" 2>&1; then
      echo "Enabled tabby-gpu" >> "$INSTALL_LOG"
    else
      echo "WARNING: could not enable tabby-gpu" >> "$INSTALL_LOG"
    fi
  else
    echo "WARNING: could not write /etc/systemd/system/tabby-gpu.service" >> "$INSTALL_LOG"
  fi
  rm -f "$GPU_TMP"
fi

if ! sudo -n loginctl enable-linger "$USER" >>"$INSTALL_LOG" 2>&1; then
  echo "WARNING: linger failed. Run: sudo loginctl enable-linger $USER" >> "$INSTALL_LOG"
fi
# systemctl --user is missing in an ISO chroot. The wants symlink is what
# linger uses on the first real boot.
WANTS_DIR="$UNIT_DIR/default.target.wants"
mkdir -p "$WANTS_DIR"
ln -sfn ../tabbyapi.service "$WANTS_DIR/tabbyapi.service"
echo "Enabled tabbyapi via $WANTS_DIR/tabbyapi.service" >> "$INSTALL_LOG"

# Refresh the TSOS login banner when this is a tabbyapi-stack OS install.
if [[ -f "$DEST_TABBY/deploy/arch/tsos-motd" ]] && \
   { [[ -e /usr/local/bin/tsos-motd ]] || [[ -d /etc/tsos ]]; }; then
  if sudo -n install -D -m 0755 "$DEST_TABBY/deploy/arch/tsos-motd" /usr/local/bin/tsos-motd \
       >>"$INSTALL_LOG" 2>&1; then
    echo "Installed /usr/local/bin/tsos-motd" >> "$INSTALL_LOG"
  else
    echo "WARNING: could not refresh /usr/local/bin/tsos-motd" >> "$INSTALL_LOG"
  fi
fi

install_tsctl() {
  local wrap="$DEST_TABBY/deploy/arch/tsctl"
  [[ -f "$wrap" ]] || return 0
  if sudo -n install -D -m 0755 "$wrap" /usr/local/bin/tsctl >>"$INSTALL_LOG" 2>&1; then
    echo "Installed /usr/local/bin/tsctl" >> "$INSTALL_LOG"
  else
    echo "WARNING: could not install /usr/local/bin/tsctl" >> "$INSTALL_LOG"
  fi
  local bashc="$DEST_TABBY/deploy/arch/tsctl.bash-completion"
  if [[ -f "$bashc" ]]; then
    sudo -n install -D -m 0644 "$bashc" /usr/share/bash-completion/completions/tsctl \
      >>"$INSTALL_LOG" 2>&1 || true
  fi
  local zshc="$DEST_TABBY/deploy/arch/_tsctl"
  if [[ -f "$zshc" ]]; then
    sudo -n install -D -m 0644 "$zshc" /usr/share/zsh/site-functions/_tsctl \
      >>"$INSTALL_LOG" 2>&1 || true
  fi
}
install_tsctl
START_NOTE=""
if [[ -n "${XDG_RUNTIME_DIR:-}" ]] && need_cmd systemctl && \
   systemctl --user daemon-reload >>"$INSTALL_LOG" 2>&1; then
  systemctl --user enable tabbyapi >>"$INSTALL_LOG" 2>&1 || true
  if systemctl --user is-active --quiet tabbyapi; then
    # The unit file was just rewritten for this dest; restart so it takes effect
    # instead of leaving an old process on the port.
    systemctl --user restart tabbyapi >>"$INSTALL_LOG" 2>&1 || true
  elif [[ "${NVIDIA_SMI_OK:-0}" -eq 0 ]]; then
    START_NOTE="NVIDIA is not loaded yet, so tabbyapi was enabled but not started. After nvidia-smi works: systemctl --user start tabbyapi"
    echo "WARNING: $START_NOTE" >> "$INSTALL_LOG"
  elif port_in_use "$TABBY_NETWORK_PORT"; then
    START_NOTE="Port $TABBY_NETWORK_PORT is in use by another process, so tabbyapi was enabled but not started."
    echo "WARNING: $START_NOTE" >> "$INSTALL_LOG"
  else
    systemctl --user start tabbyapi >>"$INSTALL_LOG" 2>&1 || true
  fi
else
  echo "systemctl --user unavailable; linger will pick up the wants symlink." >> "$INSTALL_LOG"
  if [[ "${NVIDIA_SMI_OK:-0}" -eq 0 ]]; then
    START_NOTE="NVIDIA is not loaded yet, so tabbyapi was enabled but not started. After nvidia-smi works: systemctl --user start tabbyapi"
    echo "WARNING: $START_NOTE" >> "$INSTALL_LOG"
  fi
fi
if [[ "$UPDATE_MODE" -eq 1 ]]; then
  progress 97 "Waiting for API health"
  if [[ -n "$START_NOTE" ]]; then
    echo "WARNING: skipping health wait ($START_NOTE)" >> "$INSTALL_LOG"
  elif ! wait_for_tabby_health; then
    echo "API did not become healthy. Check: journalctl --user -u tabbyapi -e" >> "$INSTALL_LOG"
    progress_fail 1
  fi
fi

progress 100 "Finished"
progress_stop
trap - EXIT
restore_tty
clear_install_resume

HOWTO="$DEST_TABBY/HOW-TO-ARCH.txt"
cat > "$HOWTO" <<EOF
TabbyAPI on Arch — how to use this install
==========================================
Written by install.sh. You do not need the Windows chat for this.

Paths
  Install:   $DEST
  Start:     $DEST/start.sh
  TabbyAPI:  $DEST_TABBY
  ComfyUI:   $DEST_COMFY
  Python:    $PY ($PY_VER)
  SSH key:   ${TABBY_SSH_KEY:- (none)}
  How-to:    $HOWTO
  Agents:    $DEST/AGENTS.md
  README:    $DEST_TABBY/deploy/arch/README.md
  tsctl:     tsctl   (stack settings; also $DEST_TABBY/deploy/arch/tsctl)

Start / stop
  Starts at boot (no login) via linger + systemctl --user enable tabbyapi
  systemctl --user enable --now tabbyapi
  systemctl --user status tabbyapi
  systemctl --user stop tabbyapi
  journalctl --user -u tabbyapi -f
  (ComfyUI lines show up there as [comfy] ...)
  linger: sudo loginctl enable-linger $USER
  check:  loginctl show-user $USER -p Linger

  API:     $API_URL
  Health:  GET $API_URL/health
  UI:      $API_URL/v1/ui   (Linux account or a Tabby-only user)
  Manual:  $DEST/start.sh

  Do not run start.bat.
  If you used a USB cache you can unmount it.

TTY screensaver (spare VT, default tty8; on unless a desktop owns the GPU)
  Do not leave it enabled beside Omarchy. Settings / tsctl can disable it.
  tsctl screensaver enable
  tsctl screensaver timeout=120
  tsctl screensaver logout-timeout=10
  Idle 2 min while logged in; 10 s after logout (defaults). Key/mouse hides it.
  Probe in a window: /usr/bin/python $DEST_TABBY/deploy/arch/tabby-saver.py --window
  Stop: tsctl screensaver disable
  VTs: TABBY_SAVER_TTY=tty8 TABBY_SAVER_USER_TTY=tty1

  tsctl                         interactive settings (dialog)
  tsctl list                    every Settings section
  tsctl network host=0.0.0.0
  tsctl gpu status              NVIDIA temp / fan / power
  tsctl gpu quiet               fan curve + lower TDP
  tsctl gpu auto                driver fan-stop
  tsctl gpu fan_speed=40        custom percent
  tsctl gpu power_limit=220     watts; 0 = profile default

Management UI ($API_URL/v1/ui)
  Sign in with the Linux user that runs tabbyapi (admin), or a Tabby-only account.
  Chat     conversations, vision, model commands, image generation; follow-up queue
  Code     project folder on this host (Monaco, file tools, preview, container terminal)
  Status   GPU mode, occupancy, profile, health; load LLM / Comfy; restart; Update git / Update all
  Gallery  generated images (admin can see all users)
  Logs     live journalctl for TabbyAPI (and Comfy when up)
  Users    admin-only: create/reset/delete Tabby accounts (not Linux users)
  Extra users can use Chat, Code, Status, Gallery, and Logs.

  Editor coding uses your editor pointed at /v1. Browser Code is the on-host alternative.

Your editor or IDE
  Full notes (any editor):  $DEST/AGENTS.md
  Base URL:  $API_URL/v1
  Model:     gpt-4o   (leave it — not ChatGPT; else your editor or IDE may sandbox / block tools)
  Public base: ${TABBY_PUBLIC_BASE:- (none — local only)}
  SSH tunnel:  ${TABBY_SSH_REMOTE:- (none)}
  UI via tunnel: same /v1/ui path under your public /v1 prefix
$(
  if [[ -n "${TABBY_SSH_REMOTE:-}" ]]; then
    ssh_tunnel_help "$TABBY_SSH_REMOTE" "$TABBY_SSH_FORWARD" "$TABBY_PUBLIC_BASE"
    echo "  Public key to upload: $HOME/.ssh/${SSH_KEY_NAME}.pub"
  fi
)

Switch models (warm 12 GB: qwen ~65s; qwen35 ~3 min; comfy ~35s)
  In chat (editor or /v1/ui), send only:
    help                    full usage guide
    list models
    restart                 bounce the API; last model reloads
    switch to qwen          daily coding, 9B, faster (~65s)
    switch to qwen35        long/hard Agent (~3 min on 12 GB)
    switch to qwen36        (~85s)
    switch to gemma         (~65s)
    switch to gemma26       (~2 min)
    switch to glm           thinking; vision off on 12 GB (~15s)
    switch to comfy         images; unloads the LLM (~35s ready)
    switch to llm           free Comfy, reload last LLM (~65s)

  GPU is exclusive: LLM or Comfy, not both.
  First start loads qwen 9B (about 65s; first Linux boot may compile Triton longer).
  qwen35 can take about 3 minutes. Chat is not ComfyUI — only switch to comfy for images.
  Short messages can still be slow on qwen35 if the client sends a large agent prompt. Use qwen for daily work.

Images (clients are remote — chat and HTTP only)
  switch to comfy, wait ~35s, then a short prompt (first Flux ~3 min, Qwen-Image ~4 min)
  or one line: generate an image of a login form  (API hands off GPU, returns a URL, reloads LLM)
  or POST $API_URL/v1/images/generations   (OpenAI-shaped; b64_json + url)
  Flux Schnell: drafts (a red bicycle in the rain)
  Qwen-Image: text / posters / UI / buttons, or prefix qwen-image:
  paste a photo in the same turn for Flux img2img
  The chat reply includes a PNG URL on this API host. The markdown preview is the picture.

Embeddings (CPU, no GPU switch)
  POST $API_URL/v1/embeddings
  model: $EMBED_NAME
  stays loaded beside the LLM

If something fails
  nvidia-smi fails       standalone install reboots once if it just installed the
                         driver; tsos ISO chroot skips that reboot and continues.
                         if it still fails: nvidia-smi ; journalctl -k | grep -i nvidia
  venv check / CUDA      ISO chroot has no loaded driver. The check requires
                         CUDA-built wheels (torch.version.cuda), not
                         torch.cuda.is_available(). Re-run with current install.sh.
  cannot open tty output arch-chroot + su has no /dev/tty for dialog --stdout.
                         Use runuser (not su -). Current install.sh does not
                         use dialog --stdout. ISO path: TABBY_ISO_CHROOT=1.
  USB NTFS dirty/read-only  sudo ntfsfix /dev/sdXN then remount
  missing models         re-run install.sh (downloads from Hugging Face; skips what exists)
  HF 401/403 gated       huggingface-cli login  or  export HF_TOKEN=...
  SSH key missing        optional; only for a public reverse tunnel
  SSH key missing        installer creates ~/.ssh/id_ed25519 when none exists
                         (needed for TABBY_SSH_REMOTE). Put the .pub on the tunnel host.
  SSH key CRLF / invalid  installer runs dos2unix on a cache-copied ~/.ssh/id_ed25519
  public URL dead        optional tunnel; local API is $API_URL
  systemctl --user fails export XDG_RUNTIME_DIR=/run/user/\$(id -u)
  port already in use    another TabbyAPI or a manual start.sh owns the port.
                         The GPU is exclusive, so stop the old one first:
                         systemctl --user stop tabbyapi ; ss -ltnp | grep $TABBY_NETWORK_PORT
  dies on logout        sudo loginctl enable-linger $USER   (installer does this)
  no sudo               re-run as your user; enter the root password when asked
                        (installer then writes passwordless sudo for this user)
  Python 3.13/3.14      re-run install.sh (it clones pyenv from GitHub
                         and builds 3.12.5). Do not use pyenv.run.
  pyenv.run DNS fail    expected on some live ISOs. Current install.sh
                         clones github.com/pyenv/pyenv instead.
  copy interrupted       re-run install.sh (rsync resumes)
  switch 500 creationflags  re-run install.sh (patches Linux spawn) then:
                         systemctl --user restart tabbyapi
  ComfyUI is not running  you asked for chat, not images. Send switch to qwen
                         and wait; first start should already load the 9B model
  no LLM loaded          wait for startup, or send switch to qwen in chat

Update
  $DEST/update.sh              asks Update git vs Update all (dialog menu)
  $DEST/update.sh --git        git pull only; offers an API restart at the end
  $DEST/update.sh --git --restart
                              git pull, then restart tabbyapi (no prompt)
  $DEST/update.sh --no-restart skip the restart prompt on Update git
  $DEST/update.sh --all        pull, then apply deps and restart
  $DEST/update.sh --comfy      also pull ComfyUI and ComfyUI-GGUF

  This folder is the git checkout. You do not need a second clone.
  config.yml, tabby.env, models, venv, and ComfyUI weights are kept.
  If update.sh changes in the pull, it restarts itself.
  Update all reloads the API until GET /health is healthy (~65s).
  Update git offers that restart at the end; --restart skips the prompt.

Uninstall
  $DEST/uninstall.sh              stop services, then remove the install
  $DEST/uninstall.sh --dry-run    show what it would do
  $DEST/uninstall.sh --purge      also delete the model weights

  It stops the user services and any leftover process before deleting files.
  Do not just rm -rf this folder: the enabled user unit would keep a running
  process on port $TABBY_NETWORK_PORT with no files behind it, and linger
  would start it again at boot.

  Weights and generated images are kept unless you pass --purge. Packages,
  the NVIDIA driver, pyenv and ~/.ssh are never touched.

Re-run is safe. Existing weights are not downloaded again.
A code update uses update.sh, not a fresh clone.
EOF

if [[ "$UPDATE_MODE" -eq 1 ]]; then
  append_update_log "Update finished."
fi

if [[ "$INTERACTIVE" -eq 1 && -n "${TABBY_SSH_REMOTE:-}" ]]; then
  SSH_PUB="$HOME/.ssh/${SSH_KEY_NAME}.pub"
  SSH_PUB_TEXT="(no .pub yet at ${SSH_PUB})"
  if [[ -f "$SSH_PUB" ]]; then
    SSH_PUB_TEXT="$(tr -d '\r' <"$SSH_PUB" | head -n 1)"
  fi
  ui_msg "Upload this public key to ${TABBY_SSH_REMOTE}" \
"$(ssh_tunnel_help "$TABBY_SSH_REMOTE" "$TABBY_SSH_FORWARD" "$TABBY_PUBLIC_BASE")

Public key (${SSH_PUB}):
  ${SSH_PUB_TEXT}

Append that one line to authorized_keys on ${TABBY_SSH_REMOTE}.
The tunnel will not stay up until this key is on that host." \
    22
fi

if [[ "$INTERACTIVE" -eq 1 ]]; then
  ui_msg "Install finished" \
"TabbyAPI and ComfyUI are set up.
${START_NOTE:+
  NOTE: $START_NOTE
}
  API:     $API_URL
  Start:   $DEST/start.sh
  Health:  GET $API_URL/health
  Editor:  $API_URL/v1   model gpt-4o  (leave it — else your editor or IDE may sandbox / block tools)
  Agents:  $DEST/AGENTS.md
  Images:  chat “generate an image of …” or POST /v1/images/generations
  UI:      Chat, Code, Status, Gallery, Logs (Users is admin-only)

Chat phrases (send as the whole message)
  help
  list models
  restart
  switch to qwen / qwen35 / qwen36 / gemma / gemma26 / glm
  switch to comfy   then wait ~35s for images
  switch to llm     to unload Comfy

IDE / agent notes (not Cursor-only):
  ${DEST}/AGENTS.md

To remove this install later:
  ${DEST}/uninstall.sh            (stops the services first — do not rm -rf)
  ${DEST}/uninstall.sh --dry-run  to preview

To pull later git changes on this install:
  ${DEST}/update.sh

The same how-to is in:
  ${HOWTO}

Linger starts TabbyAPI at boot (no login)."
fi

if [[ "$UPDATE_MODE" -eq 1 ]]; then
  echo "Update finished."
else
  echo "Install finished."
fi
[[ -n "$START_NOTE" ]] && echo "  NOTE: $START_NOTE"
echo "  API:     $API_URL"
echo "  UI:      $API_URL/v1/ui"
echo "  Log:     $INSTALL_LOG"
echo "  How-to:  $HOWTO"
echo "  Update:  $DEST/update.sh"

