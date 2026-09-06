#!/usr/bin/env bash
# tsos-installer.sh
#
# Install Arch Linux from the official live ISO or the TSOS installer ISO,
# then install tabbyapi-stack in the chroot (venvs, weights) so first boot
# only starts the API (linger). The TSOS ISO autologins tty1 into this script.
#
# Run as root from the Arch Linux live ISO. The target disk is wiped
# unless you pass --resume-tabby (finish install.sh on an already-mounted /mnt).
#
# Usage:
#   ./tsos-installer.sh
#   curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash -s -- --no-encrypt
#
# curl | bash is supported: questions are read from /dev/tty, not from the
# download pipe. You must run it from a real console (or ssh -t). Use bash,
# not sh.

set -euo pipefail

SCRIPT_NAME="${0##*/}"
if [[ "$SCRIPT_NAME" == "bash" || "$SCRIPT_NAME" == "-bash" || "$SCRIPT_NAME" == "sh" || "$SCRIPT_NAME" == "-sh" ]]; then
  SCRIPT_NAME="tsos-installer.sh"
fi
SCRIPT_VERSION="1.0.54"

# Generic defaults. Do not default TARGET_HOSTNAME from $HOSTNAME — the live
# ISO sets HOSTNAME=archiso.
TARGET_HOSTNAME="${TARGET_HOSTNAME:-tsos}"
TARGET_USER="${TARGET_USER:-tabby}"
TIMEZONE="${TIMEZONE:-UTC}"
LOCALE="${LOCALE:-en_US.UTF-8}"
KEYMAP="${KEYMAP:-us}"
ESP_SIZE="${ESP_SIZE:-2G}"
MAPPER_NAME="${MAPPER_NAME:-root}"
ENCRYPT="${ENCRYPT:-1}"
OMARCHY_MODE="${OMARCHY_MODE:-skip}" # now | skip
OMARCHY_USER_NAME="${OMARCHY_USER_NAME:-}"
OMARCHY_USER_EMAIL="${OMARCHY_USER_EMAIL:-}"
TABBY_REPO="${TABBY_REPO:-https://github.com/styelz/tabbyapi-stack-archlinux.git}"
TABBY_LOCAL_SRC="${TABBY_LOCAL_SRC:-}"
TABBY_MODELS="${TABBY_MODELS:-core}"
TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
TABBY_CACHE="${TABBY_CACHE:-}"
TABBY_PUBLIC_BASE="${TABBY_PUBLIC_BASE:-}"
TABBY_SSH_REMOTE="${TABBY_SSH_REMOTE:-}"
TABBY_SSH_FORWARD="${TABBY_SSH_FORWARD:-}"
TABBY_SSH_KEY="${TABBY_SSH_KEY:-}"
COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
DISK="${DISK:-}"
CONFIRM_WIPE="${CONFIRM_WIPE:-}"
PASSWORD="${PASSWORD:-}"
LUKS_PASSWORD="${LUKS_PASSWORD:-}"
USER_PASSWORD="${USER_PASSWORD:-}"
ROOT_PASSWORD="${ROOT_PASSWORD:-}"
DRY_RUN=0
CONFIG_PROVIDED=0
RESUME_TABBY=0
INSTALL_MODE="${INSTALL_MODE:-}" # simple | advanced | empty (ask)
ENCRYPT_FROM_CLI=""
OMARCHY_FROM_CLI=""
HOST_FROM_CLI=""
TIMEZONE_FROM_CLI=""
MODELS_FROM_CLI=""
CACHE_FROM_CLI=""
DEFAULT_DISK=/dev/sda
TUI=""
USE_TUI=0
BACKTITLE="tsos ${SCRIPT_VERSION}  ·  tabbyapi-stack"

TARGET="/mnt"
CRYPT_NAME="$MAPPER_NAME"
CACHE_STAGING=/run/tsos-weight-cache
CACHE_CHROOT_PATH=/mnt/tsos-cache
TABBY_CACHE_CHROOT=""
TSOS_OFFLINE_ROOT="${TSOS_OFFLINE_ROOT:-}"
TSOS_OFFLINE_CHROOT=/opt/tsos
TSOS_PACMAN_CONFIG=""
TSOS_PAYLOAD_ROOT="${TSOS_PAYLOAD_ROOT:-}"

if [[ -z "$TSOS_OFFLINE_ROOT" && -f /opt/tsos/pacman/tsos.db ]]; then
  TSOS_OFFLINE_ROOT=/opt/tsos
fi
if [[ -z "$TABBY_LOCAL_SRC" && -f /opt/tsos/tabbyapi-stack/install.sh ]]; then
  TABBY_LOCAL_SRC=/opt/tsos/tabbyapi-stack
fi
if [[ -z "$TSOS_PAYLOAD_ROOT" ]]; then
  if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    TSOS_PAYLOAD_ROOT="$TSOS_OFFLINE_ROOT"
  elif [[ -f /opt/tsos/tabbyapi-stack/install.sh ]]; then
    TSOS_PAYLOAD_ROOT=/opt/tsos
  fi
fi

usage() {
  cat <<EOF
${SCRIPT_NAME} v${SCRIPT_VERSION}

Install Arch Linux (btrfs + Limine, optional LUKS) from the live ISO, then
install tabbyapi-stack in the chroot before reboot. Omarchy is optional (now
or skip). Omarchy now requires LUKS.

USAGE
  ${SCRIPT_NAME} [options]
  curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash
  curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash -s -- [options]

With no --config file, the script starts with Simple setup. A review menu
lists disk, hostname, user, timezone, weights, optional GPU models, and who
can connect — open a row to change it, then start the install. Choose
Advanced for encryption, Omarchy, full model control, and SSH tunnels. It
uses the same dialog menus as install.sh when dialog is available
(installed on the live ISO if needed).

curl | bash needs a real terminal so the questions can be answered. Use
bash, not sh. Pass flags after bash -s -- .

OPTIONS
  --config FILE            Use FILE instead of the interactive settings prompts
  --disk PATH              Disk to wipe (default: first installable disk)
  --hostname NAME          Installed system hostname (default: tsos)
  --user NAME              Regular wheel user that runs tabbyapi-stack (default: tabby)
  --timezone ZONE          Timezone (default: UTC)
  --locale NAME            Locale, without the leading # (default: en_US.UTF-8)
  --keymap NAME            Console keymap (default: us)
  --esp-size SIZE          EFI partition size (default: 2G)
  --simple                 Review menu: disk, hostname, user, timezone,
                           weights source (Hugging Face / USB / path),
                           this PC vs LAN. Skips Omarchy (always). No
                           LUKS unless --encrypt.
  --advanced               Review menu for every setting (encryption,
                           Omarchy, cache, models, bind address, public
                           URL, SSH tunnel)
  --encrypt                LUKS on the root partition (Advanced default; Simple
                           is unencrypted unless you pass this)
  --no-encrypt             Unencrypted btrfs root
  --with-omarchy           Run the official Omarchy installer in the chroot
                           (requires LUKS; Advanced only — ignored in Simple)
  --skip-omarchy           Do not install Omarchy (default)
  --name "FULL NAME"       Git name passed to Omarchy as OMARCHY_USER_NAME
  --email ADDR             Git email passed to Omarchy as OMARCHY_USER_EMAIL
  --models SET             Models: core, all, or comma-separated ids (asked unless --config)
  --tabby-host ADDR        TabbyAPI listen address (default: 0.0.0.0 LAN)
  --tabby-port N           TabbyAPI listen port (asked in this UI unless --config)
  --tabby-cache PATH       Optional weights cache. Asked here (before wipe)
                           so a USB under /mnt can be bind-mounted aside.
  --tabby-repo URL         Git remote to clone (default: tabbyapi-stack-archlinux)
  --tabby-local-src PATH   Overlay this tabbyapi-stack tree after clone (install.sh, etc.)
  --resume-tabby           Do not wipe. Finish install.sh in an already-mounted
                           system at /mnt (after a chroot install.sh failure)
  --confirm-wipe PATH      Non-interactive wipe confirmation; must equal --disk
  --password-env           Read PASSWORD / LUKS_PASSWORD / USER_PASSWORD / ROOT_PASSWORD
                           from the environment instead of prompting
  --dry-run                Print the plan and exit (does not write the disk)
  --self-test              Run built-in helper tests
  --self-test-gauge        Draw the install page for 2s (needs a tty)
  -h, --help               Show this help

ENVIRONMENT
  DISK, TARGET_HOSTNAME, TARGET_USER, TIMEZONE, LOCALE, KEYMAP, ESP_SIZE
  ENCRYPT                  1 (LUKS) or 0 (plain btrfs)
  PASSWORD                 Used for LUKS + user + root if the split passwords are unset
  LUKS_PASSWORD, USER_PASSWORD, ROOT_PASSWORD
  INSTALL_MODE             simple | advanced (asked if unset)
  OMARCHY_USER_NAME, OMARCHY_USER_EMAIL, OMARCHY_MODE (now|skip)
  TABBY_REPO, TABBY_LOCAL_SRC, TABBY_MODELS, TABBY_NETWORK_HOST, TABBY_NETWORK_PORT
  TABBY_CACHE, TABBY_PUBLIC_BASE, COMFYUI_URL, HF_TOKEN

One password is used for the user, root, and disk encryption (when enabled)
unless you set the split password variables.

The live ISO's HOSTNAME (usually archiso) is ignored on purpose.

tabbyapi-stack install.sh runs in the chroot on the live ISO (Python, venvs,
weights) and must finish before reboot. Simple setup opens a review menu
(disk, hostname, user, timezone, weights, this PC vs LAN). Advanced adds
locale, encryption, Omarchy, models, and tunnels. After you confirm the wipe, the install screen stays up with
the step list, a progress bar, and the live log while Arch
and tabbyapi-stack install.
install.sh is non-interactive from here so it does not open a second
dialog. The NVIDIA driver loads on the first real boot; linger then
starts the API.

The kernel driver is nvidia-open (Arch dropped the nvidia package). That
covers Turing / RTX 20-series and newer. GTX 10xx and older need the AUR
580xx driver, which this installer does not install.
EOF
}

TSOS_LOG="${TSOS_LOG:-/tmp/tsos-installer.log}"
TSOS_WATCH_PID=""
TSOS_WORK_PID=""
TSOS_WORK_CHILD=0
TSOS_GAUGE_DIR=""
TSOS_GAUGE_FIFO=""
TSOS_GAUGE_H=""
TSOS_GAUGE_W=""
TSOS_SAVED_FD=""
TSOS_PAGE_OPEN=0
TSOS_UI_ROWS=24
TSOS_UI_COLS=80
TSOS_PRINTK=""

log() {
  printf '==> %s\n' "$*" >>"$TSOS_LOG"
  # Work-phase stdout is already the log; do not duplicate into it.
  if [[ -n "${TSOS_SAVED_FD:-}" ]]; then
    return 0
  fi
  printf '==> %s\n' "$*"
}
warn() {
  printf 'warning: %s\n' "$*" >>"$TSOS_LOG"
  printf 'warning: %s\n' "$*" >&2
}
die() {
  # Inside the background work process the parent still owns the gauge.
  # Printing to /dev/tty here is what put "error:" / percent lines on top
  # of the leftover password box. Signal the parent and exit this child.
  if [[ "${TSOS_UDEV_PAUSED:-0}" == 1 ]]; then
    udevadm control --start-exec-queue 2>/dev/null || true
    TSOS_UDEV_PAUSED=0
  fi
  if [[ "${TSOS_WORK_CHILD:-0}" == 1 ]]; then
    printf 'error: %s\n' "$*" >>"${TSOS_LOG:-/dev/null}"
    if [[ -n "${TSOS_GAUGE_DIR:-}" ]]; then
      printf '%s\n' "$*" >"$TSOS_GAUGE_DIR/error"
    fi
    exit 1
  fi
  if declare -F gauge_stop >/dev/null; then
    gauge_stop || true
  fi
  printf 'error: %s\n' "$*" >&2
  if [[ ! -t 2 ]] && have_console; then
    printf 'error: %s\n' "$*" >/dev/tty
  fi
  exit 1
}

# curl | bash puts the script on stdin. After bash has parsed this file,
# point stdin at the keyboard so child tools do not read the pipe.
# /dev/tty can exist as a node and still fail to open when there is no console.
have_console() {
  { : </dev/tty; } 2>/dev/null
}

attach_console() {
  if [[ -t 0 ]]; then
    return 0
  fi
  if have_console; then
    exec </dev/tty
  fi
}

need_tty() {
  have_console || die "No controlling terminal. curl | bash must run on the live ISO console or via ssh -t."
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

tui_cmd() {
  if need_cmd dialog; then
    TUI=dialog
  elif need_cmd whiptail; then
    TUI=whiptail
  else
    TUI=""
  fi
}

# Standard dialog colours (same palette as `dialog --create-rc`).
write_dialogrc() {
  local f="${TMPDIR:-/tmp}/tsos-dialogrc"
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
  # A bad dialogrc makes every widget exit before drawing. Validate it now
  # and fall back to dialog's built-in theme instead of aborting the installer.
  if command -v dialog >/dev/null 2>&1 && ! dialog --version >/dev/null 2>&1; then
    unset DIALOGRC
    warn "custom dialog theme was rejected; using the built-in theme"
  fi
}

ensure_dialog() {
  tui_cmd
  # whiptail has only a one-field passwordbox, so accepting it here makes
  # password and verification appear on separate pages. The interactive ISO
  # installer requires dialog's two-field --passwordform.
  [[ "$TUI" == dialog ]] && return 0
  ((DRY_RUN)) && return 0
  have_console || [[ -t 0 ]] || return 0
  log "Installing dialog (ncurses forms)"
  disable_live_mkinitcpio_hooks
  pacman -Sy --noconfirm --needed dialog || \
    die "could not install dialog; it is required for the one-page password form"
  tui_cmd
  [[ "$TUI" == dialog ]] || \
    die "dialog was installed but is not available in PATH"
}

enable_tui_if_possible() {
  if [[ -n "$TUI" ]] && { [[ -t 0 && -t 1 ]] || have_console; }; then
    USE_TUI=1
    # Every box is sized against the console, so read it before the questions.
    ensure_work_term
    [[ "$TUI" == dialog ]] && write_dialogrc
  fi
}

restore_tty() {
  # Non-interactive. Do not call `reset`/`tset`: those can print terminfo
  # settings and wait for RETURN, which looks like the installer dropped out
  # of the UI. RIS (\033c) clears leftover ncurses without prompting.
  {
    printf '\033c\033[?1049l\033[?25h\033[m'
    command -v tput >/dev/null 2>&1 && {
      tput rmcup || true
      tput rmkx || true
      tput cnorm || true
      tput sgr0 || true
    }
    stty sane
  } </dev/tty >/dev/tty 2>/dev/null || true
}

# Work phase: the install page is painted by this process on the console.
# Read the console size first so the page (and every dialog) fits it.
ensure_work_term() {
  case "${TERM:-}" in
    "" | dumb | unknown) export TERM=linux ;;
  esac
  # Insurance for a tty with tostop set: a background write would get SIGTTOU.
  stty -tostop </dev/tty >/dev/null 2>&1 || true
  local size
  size=$(stty size </dev/tty 2>/dev/null || true)
  if [[ "$size" =~ ^[0-9]+[[:space:]]+[0-9]+$ ]]; then
    TSOS_UI_ROWS=${size%%[[:space:]]*}
    TSOS_UI_COLS=${size##*[[:space:]]}
  fi
  ((TSOS_UI_ROWS >= 14)) || TSOS_UI_ROWS=24
  ((TSOS_UI_COLS >= 50)) || TSOS_UI_COLS=80
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

# Redirecting a command cannot stop the kernel: printk writes to the console
# device, so mount probes, btrfs and NVIDIA messages land on top of the box.
# Keep only emergencies on screen while the gauge owns it; everything is still
# in dmesg afterwards. First field of /proc/sys/kernel/printk is the console
# level, and writing one number sets just that field.
quiet_kernel_console() {
  [[ -w /proc/sys/kernel/printk ]] || return 0
  TSOS_PRINTK=$(awk '{print $1}' /proc/sys/kernel/printk 2>/dev/null || true)
  printf '%s\n' 1 >/proc/sys/kernel/printk 2>/dev/null || true
}

restore_kernel_console() {
  [[ -n "${TSOS_PRINTK:-}" ]] || return 0
  printf '%s\n' "$TSOS_PRINTK" >/proc/sys/kernel/printk 2>/dev/null || true
  TSOS_PRINTK=""
}

# dialog refuses to draw a box whose content does not fit ("Window too small
# for menu") and exits without painting anything, so every widget is sized from
# its own text and clamped to the console. The ISO console is 24x80.
box_width() {
  local w=$((TSOS_UI_COLS - 4))
  ((w > 74)) && w=74
  ((w < 46)) && w=46
  printf '%s' "$w"
}

# Rows the text needs after dialog wraps it to the inside of the box.
text_rows() {
  local text=$1 width=$2
  printf '%s\n' "$text" | awk -v w="$((width - 4))" '
    { n = length($0); total += (n == 0 ? 1 : int((n + w - 1) / w)) }
    END { print (total ? total : 1) }'
}

box_rows_max() {
  local m=$((TSOS_UI_ROWS - 2))
  ((m < 9)) && m=9
  printf '%s' "$m"
}

# Keep the start of the prompt (warnings live there). Drop trailing lines,
# then cap a leftover long paragraph so dialog never exits "Window too small".
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

# Last few log lines, printable ASCII only so a box never wraps.
tsos_log_snippet() {
  local lines=$1 width=$2
  [[ -f "$TSOS_LOG" ]] || return 0
  tail -n 80 "$TSOS_LOG" 2>/dev/null \
    | tr '\r' '\n' \
    | sed -e 's/\x1B\[[0-9;?]*[a-zA-Z]//g' -e 's/^XXX$//' -e 's/\\Z[0-7bBrRuUn]//g' \
    | tr -cd '\11\12\15\40-\176' \
    | grep -v '^[[:space:]]*$' \
    | tail -n "$lines" \
    | cut -c1-"$width" || true
}

# install.sh reports "==> [N%] step" in its own log. Map that onto 45-95.
nested_percent() {
  local marker="${TSOS_GAUGE_DIR:-}/nested" log pct
  [[ -s "$marker" ]] || return 1
  log=$(cat "$marker" 2>/dev/null) || return 1
  [[ -f "$log" ]] || return 1
  pct=$(grep -aoE '^==> \[[0-9]+%\]' "$log" 2>/dev/null | tail -n1 | tr -dc '0-9')
  [[ "$pct" =~ ^[0-9]+$ ]] || return 1
  pct=$((45 + pct * 50 / 100))
  ((pct > 95)) && pct=95
  printf '%s' "$pct"
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

# Title centered in a row of dashes, the way dialog draws --title.
ui_title_bar() {
  local inner=$1 title=$2
  local t=" $title "
  local rest left right
  ((inner < 1)) && return 0
  if ((${#t} >= inner)); then
    printf '%s' "${t:0:inner}"
    return 0
  fi
  rest=$((inner - ${#t}))
  left=$((rest / 2))
  right=$((rest - left))
  printf '%s%s%s' \
    "$(printf '%*s' "$left" '' | tr ' ' '-')" \
    "$t" \
    "$(printf '%*s' "$right" '' | tr ' ' '-')"
}

# minpct|short|heading substring. Used by the install stepper.
gauge_steps() {
  printf '%s\n' \
    '0|Disk|Preparing disk' \
    '8|Wipe|Wiping' \
    '15|Format|Formatting' \
    '22|Arch|Arch packages' \
    '38|Setup|Configuring' \
    '45|App|tabbyapi-stack'
  if [[ "${OMARCHY_MODE:-skip}" == now ]]; then
    printf '%s\n' '96|Desk|Omarchy'
  fi
  printf '%s\n' '98|Done|Cleaning'
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
P_SPIN=$'\033[1;36;47m'     # spinner, even tick
P_SPIN_ALT=$'\033[1;34;47m' # spinner, odd tick
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
# Finished steps green, the current spinner coloured, the rest plain.
page_chips() {
  local iw=$1 heading=$2 pct=$3 ticks=$4 spin=$5
  local cur i=0 piece mark short minpct match sc=$P_SPIN_ALT
  PAGE_P=""
  PAGE_V=0
  cur=$(gauge_step_index "$heading" "$pct")
  ((ticks % 2 == 0)) && sc=$P_SPIN
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
      PAGE_P+="$P_DLG[$sc$spin$P_DLG] $short"
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
  local iw=$((PAGE_W - 4)) t a b s right room i line r bw pos filled sc
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
  sc=$P_SPIN_ALT
  ((ticks % 2 == 0)) && sc=$P_SPIN
  page_inner 4 "$P_HEAD$PAGE_P $P_DIM$step_el  $sc$spin$P_DLG"

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

# Repaints so a long pacstrap / pip / download stays visibly alive.
# Writes one full frame of the install page to stdout (the saved tty).
watch_installer_ui() {
  local stop="$1"
  local pct heading last="" step_t=$SECONDS ticks=0 nested spin='|/-\' ch
  local info1 info2
  info1="Installing Arch Linux and tabbyapi-stack on ${DISK:-the disk} as ${TARGET_HOSTNAME:-tsos}."
  info2="Full log: ${TSOS_LOG}   Ctrl+C cancels."
  while [[ ! -f "$stop" ]]; do
    # A work process that vanished without its EXIT trap (SIGKILL) leaves
    # no stop marker; do not paint forever over a dead install.
    if [[ -n "${TSOS_WORK_PID:-}" ]] && ! kill -0 "$TSOS_WORK_PID" 2>/dev/null; then
      break
    fi
    pct=0
    heading="Working..."
    [[ -f "$TSOS_GAUGE_DIR/pct" ]] && pct=$(cat "$TSOS_GAUGE_DIR/pct" 2>/dev/null)
    [[ -f "$TSOS_GAUGE_DIR/heading" ]] && heading=$(cat "$TSOS_GAUGE_DIR/heading" 2>/dev/null)
    [[ "$pct" =~ ^[0-9]+$ ]] || pct=0
    ((pct > 100)) && pct=100
    if nested=$(nested_percent); then
      pct=$nested
    fi
    ticks=$((ticks + 1))
    if [[ "$heading" != "$last" ]]; then
      last=$heading
      step_t=$SECONDS
    fi
    ch=${spin:$((ticks % 4)):1}
    page_frame "$heading" "$pct" "$(fmt_elapsed $((SECONDS - step_t)))" "$(fmt_elapsed "$SECONDS")" \
      "$ch" "$ticks" "$info1" "$info2" \
      "$(tsos_log_snippet "$PAGE_LOG_N" "$((PAGE_W - 8))")"
    printf '%s' "$PAGE_BUF" || break
    sleep 0.12
  done
}

# gauge_update writes files only. Never printf a percent line to /dev/tty
# from the work process — that is the leak under the leftover password box.
gauge_update() {
  local pct="$1" msg="$2"
  if [[ -n "${TSOS_GAUGE_DIR:-}" ]]; then
    printf '%s\n' "$pct" >"$TSOS_GAUGE_DIR/pct"
    printf '%s\n' "$msg" >"$TSOS_GAUGE_DIR/heading"
  fi
  log "[${pct}%] $msg"
}

gauge_stop() {
  if [[ -n "${TSOS_GAUGE_DIR:-}" ]]; then
    touch "$TSOS_GAUGE_DIR/stop" 2>/dev/null || true
  fi
  rm -rf "${TSOS_GAUGE_DIR:-}"
  TSOS_GAUGE_DIR=""
  TSOS_GAUGE_H=""
  TSOS_GAUGE_W=""
  if [[ "${TSOS_PAGE_OPEN:-0}" == 1 ]]; then
    page_close
    TSOS_PAGE_OPEN=0
  fi
  if [[ -n "${TSOS_SAVED_FD:-}" ]]; then
    exec 1>&4 2>&5
    exec 4>&- 5>&-
    TSOS_SAVED_FD=""
    restore_tty
  fi
  restore_kernel_console
  printf '\033[?25h\033[0m' >/dev/tty 2>/dev/null || true
}

# Work runs in a background subshell that only writes files; this process
# keeps the console and paints the install page on the saved tty (fd 4/5)
# until the work drops the stop marker.
run_with_gauge() {
  local work_fn=$1
  local rc=0 err=""

  ensure_work_term
  quiet_kernel_console
  touch "$TSOS_LOG"
  if [[ -z "${TSOS_SAVED_FD:-}" ]]; then
    exec 4>&1 5>&2
    TSOS_SAVED_FD=1
    exec >>"$TSOS_LOG" 2>&1
  fi

  TSOS_GAUGE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/tsos-ui.XXXXXX")
  printf '%s\n' 0 >"$TSOS_GAUGE_DIR/pct"
  printf '%s\n' "Starting the install..." >"$TSOS_GAUGE_DIR/heading"

  (
    exec </dev/null
    TSOS_WORK_CHILD=1
    # A signal must not look like success: bash runs the EXIT trap on
    # HUP/TERM with $? of the last command (usually 0). Exit non-zero first.
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'rc=$?
          printf "%s\n" "$rc" >"$TSOS_GAUGE_DIR/rc" 2>/dev/null || true
          touch "$TSOS_GAUGE_DIR/stop" 2>/dev/null || true' EXIT
    "$work_fn"
    touch "$TSOS_GAUGE_DIR/done" 2>/dev/null || true
  ) &
  TSOS_WORK_PID=$!
  # Let the work child run its EXIT trap before the state dir goes away.
  trap '[[ -n "${TSOS_WORK_PID:-}" ]] && kill "$TSOS_WORK_PID" 2>/dev/null || true
        [[ -n "${TSOS_WORK_PID:-}" ]] && wait "$TSOS_WORK_PID" 2>/dev/null || true
        gauge_stop || true
        exit 130' INT TERM

  if [[ "${USE_TUI:-0}" -eq 1 ]] && have_console; then
    PAGE_TITLE="Installing"
    page_open "$BACKTITLE" "$TSOS_UI_ROWS" "$TSOS_UI_COLS" >&4
    TSOS_PAGE_OPEN=1
    log "install page: box ${PAGE_H}x${PAGE_W} on ${TSOS_UI_ROWS}x${TSOS_UI_COLS}, ${PAGE_LOG_N} log rows"
    set +e
    watch_installer_ui "$TSOS_GAUGE_DIR/stop" >&4
    set -e
  else
    wait "$TSOS_WORK_PID" || true
  fi

  wait "$TSOS_WORK_PID" || true
  TSOS_WORK_PID=""
  trap - INT TERM
  rc=1
  if [[ -f "$TSOS_GAUGE_DIR/rc" ]]; then
    rc=$(tr -dc '0-9' <"$TSOS_GAUGE_DIR/rc" 2>/dev/null || true)
  fi
  [[ "$rc" =~ ^[0-9]+$ ]] || rc=1
  if [[ -f "$TSOS_GAUGE_DIR/error" ]]; then
    err=$(cat "$TSOS_GAUGE_DIR/error" 2>/dev/null || true)
  fi
  # rc 0 without the done marker: the work process was interrupted before
  # the last step (killed, hangup). Never report that as a finished install.
  if [[ "$rc" == 0 && ! -f "$TSOS_GAUGE_DIR/done" ]]; then
    rc=1
    [[ -n "$err" ]] || err="The install was interrupted at $(cat "$TSOS_GAUGE_DIR/heading" 2>/dev/null || echo 'an unknown step') ($(cat "$TSOS_GAUGE_DIR/pct" 2>/dev/null || echo '?')%)."
    log "work process stopped early (rc 0, no done marker)"
  fi

  if [[ "$rc" != 0 ]]; then
    if [[ "${USE_TUI:-0}" -eq 1 && "${TUI:-}" == dialog ]] && need_cmd dialog && [[ -n "${TSOS_SAVED_FD:-}" ]]; then
      dialog --backtitle "$BACKTITLE" --title "Install failed" \
        --msgbox "${err:-The install stopped (exit ${rc}).}

Full log:
  ${TSOS_LOG}" 16 70 >&4 2>&5 || true
    fi
    gauge_stop
    printf 'error: %s\n' "${err:-exit ${rc}}" >&2
    exit "$rc"
  fi
  gauge_stop
}

ui_cancel() {
  die "Installer cancelled."
}

# Widgets abort the installer on Esc unless a review-hub editor set this.
UI_ALLOW_BACK=0

_ui_fail() {
  if [[ "${UI_ALLOW_BACK:-0}" == 1 ]]; then
    return 1
  fi
  ui_cancel
}

# dialog draws the widget on stdout and returns the typed value on stderr.
# The widget must go to /dev/tty (not a pipe). --stdout is not an option: it
# needs /dev/tty too and fails in some chroots.
# Sets DIALOG_OUT. Do not wrap this in $( ): that runs ncurses in a subshell
# and leaves the tty unrestored.
dialog_read() {
  local tmp rc
  DIALOG_OUT=""
  tmp=$(mktemp "${TMPDIR:-/tmp}/tsos-dialog.XXXXXX") || return 1
  set +e
  if [[ -c /dev/tty ]] && { true >/dev/tty; } 2>/dev/null; then
    dialog --backtitle "$BACKTITLE" "$@" 2> "$tmp" >/dev/tty
  else
    dialog --backtitle "$BACKTITLE" "$@" 2> "$tmp"
  fi
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    # dialog reports its own failures ("Window too small for menu") on stderr,
    # which is this temp file. Without logging it, a box that cannot be drawn
    # is indistinguishable from the user pressing Cancel.
    if [[ -s "$tmp" ]] && grep -qi 'error' "$tmp"; then
      warn "dialog failed (rc=$rc): $(tr '\n' ' ' <"$tmp")"
    fi
    rm -f "$tmp"
    return "$rc"
  fi
  # Command substitution strips a trailing newline; do not trim spaces
  # (a passwordbox can end with one).
  DIALOG_OUT=$(cat "$tmp")
  rm -f "$tmp"
  return 0
}

# Same reason as dialog_read: ui_msg and ui_yesno also run inside $( ) via
# ui_ask_until, so the widget must go to the terminal, not to stdout.
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
  # msgbox chrome: two borders, separator, button row, padding.
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
    printf '\n=== %s ===\n%s\n\n' "$title" "$text" >/dev/tty
  fi
}

ui_input() {
  local title="$1"
  local text="$2"
  local default="$3"
  local out=""
  local width height
  width=$(box_width)
  # inputbox chrome: borders, separator, button row, the entry field, padding.
  text=$(fit_text "$text" "$width" "$(($(box_rows_max) - 7))")
  height=$(($(text_rows "$text" "$width") + 7))
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    dialog_read --title "$title" --inputbox "$text" "$height" "$width" "$default" || { _ui_fail; return 1; }
    out=$DIALOG_OUT
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    out="$(whiptail --backtitle "$BACKTITLE" --title "$title" --inputbox "$text" "$height" "$width" "$default" 3>&1 1>&2 2>&3)" || { _ui_fail; return 1; }
  else
    out=$(ask "$title" "$default")
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
  # menu chrome: borders, separator, button row, list frame, padding.
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
    local i=1 tag
    local tags=()
    {
      printf '\n=== %s ===\n%s\n\n' "$title" "$text"
      while (($#)); do
        tag="$1"
        tags+=("$tag")
        printf "  %s) %s — %s\n" "$i" "$tag" "$2"
        shift 2
        i=$((i + 1))
      done
    } >/dev/tty
    local choice=""
    choice=$(read_tty "Choice [1]: ")
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
    local i=1 tag state
    local defaults=()
    {
      printf '\n=== %s ===\n%s\n\n' "$title" "$text"
      while (($#)); do
        tag="$1"
        state="$3"
        printf "  %s) [%s] %s — %s\n" "$i" "$([[ "$state" == on ]] && echo x || echo ' ')" "$tag" "$2"
        [[ "$state" == on ]] && defaults+=("$tag")
        shift 3
        i=$((i + 1))
      done
    } >/dev/tty
    local joined="${defaults[*]}"
    joined="${joined// /,}"
    local choice=""
    choice=$(read_tty "Comma-separated ids [${joined}]: ")
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
    # dialog: Yes=0, No=1, Esc=255. Esc used to look like No.
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
    printf '\n=== %s ===\n%s\n\n' "$title" "$text" >/dev/tty
    local ans=""
    ans=$(read_tty "Continue? [$yn]: ")
    ans="${ans:-$([[ "$default_yes" -eq 1 ]] && echo y || echo n)}"
    [[ "$ans" =~ ^[Yy] ]]
  fi
}

# Sets REPLY. Call this in the current shell (not $(ui_password)): capturing
# stdout runs the password box in a subshell and leaves the tty unrestored.
ui_password() {
  local title="$1"
  local text="$2"
  local out=""
  local width height
  REPLY=""
  width=$(box_width)
  text=$(fit_text "$text" "$width" "$(($(box_rows_max) - 7))")
  height=$(($(text_rows "$text" "$width") + 7))
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    dialog_read --title "$title" --insecure --passwordbox "$text" "$height" "$width" || { _ui_fail; return 1; }
    out=$DIALOG_OUT
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    out="$(whiptail --backtitle "$BACKTITLE" --title "$title" --passwordbox "$text" "$height" "$width" 3>&1 1>&2 2>&3)" || { _ui_fail; return 1; }
  else
    out=$(read_secret "$title: ")
  fi
  REPLY=$out
}

# Password + verify on one page. Sets REPLY and REPLY2. Same tty rule as
# ui_password: do not wrap this in $( ).
ui_password_pair() {
  local title="$1"
  local text="$2"
  local width height form_h flen lab_w tmp rc
  local -a lines=()
  REPLY=""
  REPLY2=""
  width=$(box_width)
  form_h=2
  lab_w=18
  flen=$((width - lab_w - 6))
  ((flen < 16)) && flen=16
  text=$(fit_text "$text" "$width" "$(($(box_rows_max) - form_h - 8))")
  height=$(($(text_rows "$text" "$width") + form_h + 8))
  if [[ "$USE_TUI" -eq 1 && "$TUI" == dialog ]]; then
    tmp=$(mktemp "${TMPDIR:-/tmp}/tsos-dialog.XXXXXX") || return 1
    set +e
    if [[ -c /dev/tty ]] && { true >/dev/tty; } 2>/dev/null; then
      dialog --backtitle "$BACKTITLE" --title "$title" --insecure \
        --passwordform "$text" "$height" "$width" "$form_h" \
        "Password:" 1 1 "" 1 "$lab_w" "$flen" 128 \
        "Verify password:" 2 1 "" 2 "$lab_w" "$flen" 128 \
        2> "$tmp" >/dev/tty
    else
      dialog --backtitle "$BACKTITLE" --title "$title" --insecure \
        --passwordform "$text" "$height" "$width" "$form_h" \
        "Password:" 1 1 "" 1 "$lab_w" "$flen" 128 \
        "Verify password:" 2 1 "" 2 "$lab_w" "$flen" 128 \
        2> "$tmp"
    fi
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
      rm -f "$tmp"
      _ui_fail || return 1
    fi
    mapfile -t lines < "$tmp"
    rm -f "$tmp"
    REPLY=${lines[0]-}
    REPLY2=${lines[1]-}
  elif [[ "$USE_TUI" -eq 1 && "$TUI" == whiptail ]]; then
    # whiptail has no two-field password form.
    ui_password "$title" "$text"
    local first=$REPLY
    ui_password "Verify password" "Type the same password again."
    REPLY2=$REPLY
    REPLY=$first
  else
    printf '%s\n' "$text" >/dev/tty
    REPLY=$(read_secret "Password: ")
    REPLY2=$(read_secret "Verify password: ")
  fi
}

ui_ask_until() {
  local title=$1
  local text=$2
  local default=$3
  local validator=$4
  local value
  while true; do
    value=$(ui_input "$title" "$text" "$default") || return 1
    if "$validator" "$value"; then
      printf '%s' "$value"
      return 0
    fi
    ui_msg "Invalid value" "Not accepted: ${value}" || return 1
  done
}

read_tty() {
  local prompt=$1
  local value=""
  need_tty
  # Always write the prompt to the console. read -p is silent when stdin is a pipe.
  printf '%s' "$prompt" >/dev/tty
  # Empty Enter is valid (optional fields, keep-default). read returns 1 on EOF.
  IFS= read -r value </dev/tty || true
  printf '%s' "$value"
}

read_secret() {
  local prompt=$1
  local value=""
  need_tty
  printf '%s' "$prompt" >/dev/tty
  IFS= read -r -s value </dev/tty || true
  printf '\n' >/dev/tty
  printf '%s' "$value"
}

# Prompt on the console. Empty reply keeps the default. Writes the value to stdout.
ask() {
  local prompt=$1
  local default=${2-}
  local reply=""
  if [[ -n "$default" ]]; then
    reply=$(read_tty "$prompt [$default]: ")
    printf '%s' "${reply:-$default}"
  else
    reply=$(read_tty "$prompt: ")
    printf '%s' "$reply"
  fi
  return 0
}

valid_hostname() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,62}$ ]]
}

valid_username() {
  [[ "$1" =~ ^[a-z_][a-z0-9_-]*$ && "$1" != "root" ]]
}

valid_omarchy_mode() {
  [[ "$1" == "now" || "$1" == "skip" ]]
}

valid_esp_size() {
  [[ "$1" =~ ^[0-9]+([KMGT]i?B?|[kmgt])?$ ]]
}

valid_yes_no() {
  [[ "$1" == "yes" || "$1" == "no" ]]
}

valid_models() {
  local s="${1:-}"
  [[ -n "$s" ]] || return 1
  [[ "$s" == "core" || "$s" == "all" || "$s" == "selected" ]] && return 0
  [[ "$s" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]]
}

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

ask_until() {
  local prompt=$1
  local default=$2
  local validator=$3
  local value
  while true; do
    value=$(ask "$prompt" "$default")
    if "$validator" "$value"; then
      printf '%s' "$value"
      return 0
    fi
    printf 'invalid value: %s\n' "$value" >/dev/tty
  done
}

show_available_disks() {
  local name size model iso="" marked=0
  iso=$(live_iso_disk || true)
  printf 'Disks on this machine:\n' >/dev/tty
  printf '%s\n' "$(lsblk -d -o NAME,SIZE,TYPE,MODEL)" >/dev/tty
  printf '\n' >/dev/tty
  while read -r name; do
    size=$(lsblk -dn -o SIZE "/dev/$name" 2>/dev/null | tr -d ' ')
    model=$(lsblk -dn -o MODEL "/dev/$name" 2>/dev/null | sed 's/[[:space:]]*$//')
    if [[ -n "$iso" && "/dev/$name" == "$iso" ]]; then
      printf '  /dev/%s\t%s\t%s\t(live ISO — cannot install here)\n' "$name" "$size" "$model" >/dev/tty
    else
      printf '  /dev/%s\t%s\t%s\n' "$name" "$size" "$model" >/dev/tty
      marked=1
    fi
  done < <(physical_disk_names)
  if ((marked == 0)); then
    printf '\nNo installable disk. The live USB/ISO is excluded so it is not wiped.\n' >/dev/tty
    printf 'Add another disk, or boot the ISO from a USB/DVD that is not the target drive.\n' >/dev/tty
  fi
}

ask_install_disk() {
  local default="" iso=""
  iso=$(live_iso_disk || true)
  default=$(first_install_disk || true)
  if [[ -n "$DISK" && "$DISK" != "$iso" && -b "$DISK" ]]; then
    default=$DISK
  elif [[ -z "$default" && -b "$DEFAULT_DISK" && "$DEFAULT_DISK" != "$iso" ]]; then
    default=$DEFAULT_DISK
  fi
  if [[ -z "$default" ]]; then
    die "No installable disk found. Attach a second drive, then run the script again.
$(lsblk -d -o NAME,SIZE,TYPE,MODEL)"
  fi

  if ((USE_TUI)); then
    local args=() path size model
    while IFS=$'\t' read -r path size model; do
      [[ -n "$path" ]] || continue
      args+=("$path" "${size}  ${model}")
    done < <(list_install_disks)
    ((${#args[@]})) || die "No installable disk found."
    DISK=$(ui_menu "Target disk" \
"This disk will be wiped. The live ISO / USB you booted from is hidden.

Choose the machine disk, not a second installer stick." \
      "${args[@]}") || return 1
    return 0
  fi

  local value
  show_available_disks
  printf '\n' >/dev/tty
  while true; do
    value=$(ask "Target disk (WILL BE WIPED)" "$default")
    if [[ ! -b "$value" ]]; then
      printf 'not a block device: %s (check the list above)\n' "$value" >/dev/tty
      continue
    fi
    if [[ -n "$iso" && "$value" == "$iso" ]]; then
      printf '%s is the live ISO. Choose a different disk.\n' "$value" >/dev/tty
      continue
    fi
    DISK=$value
    return 0
  done
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
  local current="${2:-0.0.0.0}"
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
    0.0.0.0) _listen_host_add "$current" "all interfaces — LAN clients can connect (default)" ;;
    127.0.0.1) _listen_host_add "$current" "this machine only" ;;
    "") ;;
    *) _listen_host_add "$current" "current choice" ;;
  esac
  _listen_host_add "0.0.0.0" "all interfaces — LAN clients can connect (default)"
  _listen_host_add "127.0.0.1" "this machine only"
  while read -r addr iface; do
    _listen_host_add "$addr" "this NIC (${iface})"
  done < <(listen_ipv4_ifaces)
  _listen_host_add "other" "type a different address"
  unset -f _listen_host_add
  choice="$(ui_menu "$title" \
"Which address should TabbyAPI bind on? Pick from this machine.

  0.0.0.0    — other devices on the LAN can connect (default)
  127.0.0.1  — this machine only
  a LAN IP   — only that NIC

Do not pick a public hostname. The TCP port is the next screen.
On the live ISO these IPs are the installer NIC; 0.0.0.0 still
means “all interfaces” after reboot." \
    "${items[@]}")" || return 1
  if [[ "$choice" == "other" ]]; then
    choice="$(ui_input "$title" \
"Address TabbyAPI binds on.

Examples: 0.0.0.0 (all NICs, default), 127.0.0.1 (this machine), or a LAN IPv4.
Do not put a public hostname here." \
      "${current:-0.0.0.0}")" || return 1
  fi
  printf '%s' "${choice:-0.0.0.0}"
}

# Simple-mode listen choice: this PC vs LAN. Always returns 0.
ui_listen_access() {
  local title="$1"
  local choice
  choice="$(ui_menu "$title" \
"Who should be able to open the API and browser UI?

  Other computers on my network — laptops and editors on the LAN (0.0.0.0).
  This PC only — Cursor and the UI on this machine (127.0.0.1).

LAN is the default. You can change this later in Settings." \
    lan "Other computers on my network (default)" \
    this-pc "This PC only")" || return 1
  case "$choice" in
    this-pc) printf '%s' "127.0.0.1" ;;
    *) printf '%s' "0.0.0.0" ;;
  esac
}

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

# Asked when --config is not passed. Defaults come from the script
# (or from a flag / env var if you already set one).
valid_install_mode() {
  [[ "$1" == simple || "$1" == advanced ]]
}

apply_simple_defaults() {
  INSTALL_MODE=simple
  TARGET_HOSTNAME="${TARGET_HOSTNAME:-tsos}"
  TIMEZONE="${TIMEZONE:-UTC}"
  LOCALE="${LOCALE:-en_US.UTF-8}"
  KEYMAP="${KEYMAP:-us}"
  ESP_SIZE="${ESP_SIZE:-2G}"
  if [[ -z "${ENCRYPT_FROM_CLI:-}" ]]; then
    ENCRYPT=0
  fi
  if [[ -n "${OMARCHY_FROM_CLI:-}" && "${OMARCHY_MODE:-}" == now ]]; then
    warn "Simple setup does not install Omarchy; ignoring --with-omarchy."
  fi
  OMARCHY_MODE=skip
  OMARCHY_USER_NAME=""
  OMARCHY_USER_EMAIL=""
  if [[ -z "${CACHE_FROM_CLI:-}" ]]; then
    TABBY_CACHE=""
  fi
  if [[ -z "${MODELS_FROM_CLI:-}" ]]; then
    TABBY_MODELS=core
  fi
  TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
  if [[ -z "${HOST_FROM_CLI:-}" ]]; then
    TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
  fi
  COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
  TABBY_PUBLIC_BASE=""
  TABBY_SSH_REMOTE=""
  TABBY_SSH_FORWARD=""
  TABBY_SSH_KEY=""
  TABBY_SAVER_ENABLED=1
}

simple_plan_notes() {
  local access="other computers on the network"
  if [[ "${TABBY_NETWORK_HOST:-0.0.0.0}" != "0.0.0.0" ]]; then
    access="this PC only"
  fi
  cat <<EOF
Simple setup — you can change listen address, models, and
tunnels later in Settings, or re-run and pick Advanced.

  hostname:      ${TARGET_HOSTNAME:-tsos}
  access:        ${access} (${TABBY_NETWORK_HOST:-0.0.0.0}:${TABBY_NETWORK_PORT:-5000})
  models:        ${TABBY_MODELS:-core}
  weights:       ${TABBY_CACHE:-Hugging Face}
  encryption:    $(encrypt_label)
  Omarchy:       not installed
  screensaver:   on (tty8)
  timezone:      ${TIMEZONE:-UTC}

EOF
}

# Live ISO is usually UTC. If timedatectl or /etc/localtime already names
# a zone, use that as the Simple/Advanced default so the prompt is closer.
maybe_detect_timezone() {
  [[ -z "${TIMEZONE_FROM_CLI:-}" ]] || return 0
  [[ "${TIMEZONE:-UTC}" == UTC ]] || return 0
  local tz=""
  tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
  if [[ -z "$tz" || "$tz" == UTC ]]; then
    if [[ -L /etc/localtime ]]; then
      tz=$(readlink -f /etc/localtime 2>/dev/null || true)
      tz=${tz#/usr/share/zoneinfo/}
    fi
  fi
  if [[ -n "$tz" && "$tz" != UTC && -e "/usr/share/zoneinfo/$tz" ]]; then
    TIMEZONE=$tz
  fi
}

apply_timezone() {
  local v=${1:-UTC}
  v=${v#/usr/share/zoneinfo/}
  v=${v#/}
  [[ -n "$v" ]] || v=UTC
  TIMEZONE=$v
}

warn_unknown_timezone() {
  if [[ -e "/usr/share/zoneinfo/${TIMEZONE}" ]]; then
    return 0
  fi
  if ((USE_TUI)); then
    ui_msg "Timezone not found" \
"No file at /usr/share/zoneinfo/${TIMEZONE}.
Continuing anyway — fix it after boot if the clock is wrong." || true
  else
    warn "timezone not found at /usr/share/zoneinfo/$TIMEZONE — continuing anyway"
  fi
}

prompt_timezone() {
  local v
  if ((USE_TUI)); then
    v=$(ui_input "Timezone" \
"Timezone from /usr/share/zoneinfo. This is the system clock
on first boot (not UTC unless you want UTC).

Examples: Australia/Sydney  America/New_York  Europe/London  UTC" \
      "$TIMEZONE") || return 1
  else
    printf '%s\n' >/dev/tty \
"Timezone from /usr/share/zoneinfo (system clock on first boot).
Examples: Australia/Sydney  America/New_York  Europe/London  UTC"
    v=$(ask "Timezone" "$TIMEZONE")
  fi
  apply_timezone "$v"
  warn_unknown_timezone
}

# Hugging Face, a USB copy, or a typed folder. --tabby-cache skips the ask.
prompt_weights_source() {
  local title="${1:-Weights source}"
  local cache_choice
  if [[ -z "${CACHE_FROM_CLI:-}" ]]; then
    if ((USE_TUI)); then
      cache_choice=$(ui_menu "$title" \
"Where should the installer get model weights?

Hugging Face — download models that fit this NVIDIA GPU.
A local path — search that folder (USB copy, old tabbyapi-stack,
or model dirs). Name it now; the new root mounts at /mnt next.

Mount the USB first if you want that option (not under /mnt)." \
        hf "Hugging Face (models that fit this GPU)" \
        usb "Use /run/media/usb/tabbyapi-stack" \
        custom "Type another path") || return 1
      case "$cache_choice" in
        hf|none) TABBY_CACHE="" ;;
        usb) TABBY_CACHE="/run/media/usb/tabbyapi-stack" ;;
        custom)
          TABBY_CACHE=$(ui_input "Weights cache path" \
"Folder to search for existing weights. Any of these work:

  /run/media/usb/tabbyapi-stack
  /run/media/usb/tabbyapi-stack/tabbyAPI/models
  /tmp/tabby-weights

The installer lists what it finds next. Blank = Hugging Face." \
            "$TABBY_CACHE") || return 1
          ;;
        *) TABBY_CACHE="$cache_choice" ;;
      esac
    else
      printf '%s\n' >/dev/tty \
"Weights: hf = Hugging Face, usb = /run/media/usb/tabbyapi-stack, or type a folder path."
      cache_choice=$(ask "Weights source (hf / usb / path)" "${TABBY_CACHE:-hf}")
      case "$cache_choice" in
        hf|HF|"") TABBY_CACHE="" ;;
        usb|USB) TABBY_CACHE="/run/media/usb/tabbyapi-stack" ;;
        *) TABBY_CACHE="$cache_choice" ;;
      esac
    fi
  fi

  if [[ -n "$TABBY_CACHE" && ! -d "$TABBY_CACHE" ]]; then
    if ((USE_TUI)); then
      if ui_yesno "Cache not found" \
"That path is not a directory:
  ${TABBY_CACHE}

Yes = Hugging Face models that fit this GPU.
No = leave the path (install will download anything missing)." 1; then
        TABBY_CACHE=""
      fi
    else
      printf 'warning: %s is not a directory; Hugging Face will fill gaps.\n' \
        "$TABBY_CACHE" >/dev/tty
    fi
  fi
}

pick_install_mode() {
  if [[ -n "${INSTALL_MODE:-}" ]]; then
    valid_install_mode "$INSTALL_MODE" || die "invalid INSTALL_MODE: $INSTALL_MODE (simple or advanced)"
    return 0
  fi
  local choice
  if ((USE_TUI)); then
    choice=$(ui_menu "Setup type" \
"Simple (recommended) opens a review menu: disk, hostname,
username, timezone, weights source, and whether other computers
on your network can connect. Open a row to change it.

Advanced adds locale, encryption, Omarchy, full model control,
bind address, public URL, and SSH tunnel.

Omarchy is not installed in Simple." \
      simple "Simple — disk, hostname, user, timezone, weights, this PC vs LAN" \
      advanced "Advanced — every setting")
  else
    printf '\n' >/dev/tty
    printf '%s\n' "Simple (recommended): disk, hostname, user, timezone, weights, this PC vs LAN." >/dev/tty
    printf '%s\n' "Advanced: encryption, Omarchy, full model control, tunnel." >/dev/tty
    printf '%s\n' "Omarchy is not installed in Simple." >/dev/tty
    choice=$(ask_until "Setup type (simple / advanced)" "simple" valid_install_mode)
  fi
  INSTALL_MODE="${choice:-simple}"
  valid_install_mode "$INSTALL_MODE" || INSTALL_MODE=simple
}

prompt_settings() {
  pick_install_mode
  if [[ "$INSTALL_MODE" == simple ]]; then
    apply_simple_defaults
  fi
  maybe_detect_timezone
  if [[ "$INSTALL_MODE" == simple ]]; then
    if ((USE_TUI)); then
      prompt_settings_simple_tui
    else
      prompt_settings_simple_text
    fi
  elif ((USE_TUI)); then
    prompt_settings_tui
  else
    prompt_settings_text
  fi
}

prompt_settings_simple_text() {
  log "Simple setup. Enter settings, or press Enter to keep the default."
  printf '\n' >/dev/tty
  show_available_disks
  printf '\n' >/dev/tty

  ask_install_disk "Target disk"
  TARGET_HOSTNAME=$(ask_until "Hostname" "$TARGET_HOSTNAME" valid_hostname)
  TARGET_USER=$(ask_until "Username" "$TARGET_USER" valid_username)
  prompt_timezone || true
  prompt_weights_source "Weights source"
  if [[ -z "${TABBY_CACHE:-}" && "${TABBY_MODELS:-core}" == core ]]; then
    TABBY_MODELS="$(simple_model_baseline "$(gpu_vram_mib)")"
  fi
  simple_edit_models
  if [[ -z "${HOST_FROM_CLI:-}" ]]; then
    TABBY_NETWORK_HOST=$(ui_listen_access "Who can connect")
  fi
  TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
  printf '\n' >/dev/tty
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

weights_label() {
  if [[ -n "${TABBY_CACHE:-}" ]]; then
    printf '%s' "$TABBY_CACHE"
  else
    printf 'Hugging Face'
  fi
}

access_label() {
  if [[ "${TABBY_NETWORK_HOST:-0.0.0.0}" == "0.0.0.0" ]]; then
    printf 'LAN (0.0.0.0:%s)' "${TABBY_NETWORK_PORT:-5000}"
  else
    printf 'this PC (%s:%s)' "${TABBY_NETWORK_HOST:-0.0.0.0}" "${TABBY_NETWORK_PORT:-5000}"
  fi
}

tunnel_label() {
  if [[ -n "${TABBY_SSH_REMOTE:-}" ]]; then
    printf '%s' "$TABBY_SSH_REMOTE"
  else
    printf '(none)'
  fi
}

encrypt_hub_label() {
  local d e
  if [[ "$OMARCHY_MODE" == now ]]; then
    d="Omarchy"
  else
    d="no desktop"
  fi
  if ((ENCRYPT)); then
    e="LUKS"
  else
    e="unencrypted"
  fi
  printf '%s, %s' "$d" "$e"
}

locale_hub_label() {
  printf '%s, %s, %s, EFI %s' "$TIMEZONE" "$LOCALE" "$KEYMAP" "$ESP_SIZE"
}

disk_hub_label() {
  if [[ -z "${DISK:-}" ]]; then
    printf '(not set)'
    return 0
  fi
  if [[ -b "$DISK" ]]; then
    printf '%s  %s' "$DISK" "$(lsblk -dn -o SIZE,MODEL "$DISK" 2>/dev/null | sed 's/[[:space:]]\{1,\}/ /g; s/[[:space:]]*$//')"
  else
    printf '%s' "$DISK"
  fi
}

# Esc in an editor returns here instead of aborting the installer.
hub_edit_hostname() {
  local v
  v=$(ui_ask_until "Hostname" \
"Name of the installed system (not the live ISO hostname).

Letters, digits, and hyphens. Example: tsos" \
    "$TARGET_HOSTNAME" valid_hostname) || return 0
  TARGET_HOSTNAME=$v
}

hub_edit_user() {
  local v
  v=$(ui_ask_until "Username" \
"Regular wheel user that runs tabbyapi-stack.

Lowercase, not root. Example: tabby" \
    "$TARGET_USER" valid_username) || return 0
  TARGET_USER=$v
}

hub_edit_timezone() {
  prompt_timezone || return 0
}

hub_edit_locale() {
  local v
  prompt_timezone || return 0
  v=$(ui_input "Locale" \
"Locale name without a leading #.

Example: en_US.UTF-8" \
    "$LOCALE") || return 0
  LOCALE="${v:-en_US.UTF-8}"
  v=$(ui_input "Console keymap" \
"Keyboard map for the console (and LUKS prompt).

Example: us" \
    "$KEYMAP") || return 0
  KEYMAP="${v:-us}"
  v=$(ui_ask_until "EFI partition size" \
"FAT32 /boot size. 2G is enough for the kernel and Limine.

Examples: 2G  512M" \
    "$ESP_SIZE" valid_esp_size) || return 0
  ESP_SIZE=$v
}

hub_edit_desktop() {
  local rc=0
  # --yesno returns 1 for No. `|| rc=$?` keeps set -e from aborting the
  # installer before the LUKS question.
  ui_yesno "Omarchy desktop" \
"Install the official Omarchy desktop in the chroot?

Yes requires LUKS on the root disk (encryption will be turned on).
No skips Omarchy; you can still encrypt on the next screen.

Default is no." \
    0 || rc=$?
  case "$rc" in
    2) return 0 ;;
    0)
      OMARCHY_MODE=now
      ENCRYPT=1
      ui_msg "Encryption required" \
"Omarchy is selected, so the disk will be encrypted with LUKS." || true
      OMARCHY_USER_NAME=$(ui_input "Git name" \
"Optional name passed to Omarchy as OMARCHY_USER_NAME.

Blank is fine." \
        "$OMARCHY_USER_NAME") || return 0
      OMARCHY_USER_EMAIL=$(ui_input "Git email" \
"Optional email passed to Omarchy as OMARCHY_USER_EMAIL.

Blank is fine." \
        "$OMARCHY_USER_EMAIL") || return 0
      ;;
    *)
      OMARCHY_MODE=skip
      rc=0
      ui_yesno "Disk encryption" \
"Encrypt the root disk with LUKS?

Yes = unlock password at boot (recommended).
No = unencrypted btrfs.

Default follows the current setting ($(encrypt_label))." \
        "$([[ "$(encrypt_label)" == yes ]] && echo 1 || echo 0)" || rc=$?
      case "$rc" in
        2) return 0 ;;
        0) ENCRYPT=1 ;;
        *) ENCRYPT=0 ;;
      esac
      ;;
  esac
}

hub_edit_models() {
  local gpu_label vram prev picked rc
  prev=${TABBY_MODELS:-core}
  vram=$(gpu_vram_mib)
  gpu_label=$(gpu_prompt_label "$vram")
  picked=""
  if [[ -n "$TABBY_CACHE" && -d "$TABBY_CACHE" ]]; then
    picked=$(pick_models_ui "Models found" \
"Weights found under:
  ${TABBY_CACHE}

Space toggles a row. Enter confirms. Selected models are copied
into the install; anything incomplete still downloads.

GPU: ${gpu_label}" \
      cache "$TABBY_CACHE")
    rc=$?
    case "$rc" in
      1) return 0 ;;
      0)
        if [[ -z "$picked" ]]; then
          ui_msg "Select at least one model" "Check at least one row, then press Enter." || true
          return 0
        fi
        TABBY_MODELS=$picked
        ;;
      *)
        rc=0
        ui_yesno "No catalog models in that folder" \
"Nothing matching the installer catalog was found in:
  ${TABBY_CACHE}

Yes = show Hugging Face models that fit this GPU instead.
No = keep the path and use the core preset." 1 || rc=$?
        case "$rc" in
          2) return 0 ;;
          0) TABBY_CACHE="" ;;
          *) TABBY_MODELS=core; return 0 ;;
        esac
        ;;
    esac
  fi
  if [[ -z "${picked:-}" && -z "$TABBY_CACHE" ]]; then
    picked=$(pick_models_ui "Hugging Face models" \
"Hugging Face models that fit this GPU:
  ${gpu_label}

Space toggles a row. Enter confirms. Checked rows are the usual
first-install set.

If VRAM could not be read, every catalog model is listed." \
      hf "")
    rc=$?
    case "$rc" in
      1) return 0 ;;
      0) TABBY_MODELS=${picked:-core} ;;
      *)
        picked=$(ui_menu "Model set" \
"Could not list individual models. Pick a preset.

core - qwen 9B, Qwen-Image, CPU embedder
all  - every switch-to profile" \
          core "qwen 9B + Qwen-Image + embedder" \
          all "every switch-to profile") || return 0
        TABBY_MODELS="${picked:-core}"
        ;;
    esac
  fi
  if [[ "$TABBY_MODELS" == *gemma* ]]; then
    ui_msg "Gemma / Hugging Face" \
"Gemma weights may be gated on Hugging Face.

If a later download returns 401 or 403:
  huggingface-cli login
  or:  export HF_TOKEN=...
  then re-run this installer (finished files are skipped).

You do not need a token for qwen / Flux / Qwen-Image." || true
  fi
}

hub_edit_network() {
  local host port comfy public
  host=$(ui_listen_host "API listen address" \
    "${TABBY_NETWORK_HOST:-0.0.0.0}") || return 0
  host="${host:-0.0.0.0}"
  port=$(ui_ask_until "API listen port" \
"TCP port for the API. Default 5000.

Health:  http://${host}:PORT/health
Cursor:  http://${host}:PORT/v1" \
    "${TABBY_NETWORK_PORT:-5000}" valid_port) || return 0
  comfy=$(ui_input "ComfyUI URL" \
"HTTP URL for ComfyUI after “switch to comfy”.

Usual value:  http://127.0.0.1:8188
Change this only if ComfyUI will listen somewhere else." \
    "${COMFYUI_URL:-http://127.0.0.1:8188}") || return 0
  public=$(ui_input "Public URL" \
"Optional URL written into image links and the public gallery.

Examples
  https://api.example.com/v1
  https://chat.example.com/api/v1

Blank = local only (http://${host}:${port}/v1).
Leave blank if you do not have a reverse proxy or tunnel." \
    "${TABBY_PUBLIC_BASE}") || return 0
  TABBY_NETWORK_HOST=$host
  TABBY_NETWORK_PORT=$port
  COMFYUI_URL="${comfy:-http://127.0.0.1:8188}"
  TABBY_PUBLIC_BASE=$public
}

hub_edit_tunnel() {
  local remote spec key
  remote=$(ui_input "SSH tunnel" \
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
  spec=$(ui_input "SSH forward" \
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
  ui_msg "SSH key" \
"Private key this GPU box uses to log in to ${remote}.

$(ssh_tunnel_help "$remote" "$spec" "$TABBY_PUBLIC_BASE")

The installer copies that key from a weights cache if present,
otherwise it creates a new ed25519 key. After install, copy the
matching .pub onto ${remote}." || return 0
  key=$(ui_input "SSH key" \
"Path to that private key for ${remote}.

Default is fine unless your key has another name." \
    "${TABBY_SSH_KEY:-/home/${TARGET_USER}/.ssh/id_ed25519}") || return 0
  TABBY_SSH_REMOTE=$remote
  TABBY_SSH_FORWARD=$spec
  TABBY_SSH_KEY="${key:-/home/${TARGET_USER}/.ssh/id_ed25519}"
}

hub_edit_access() {
  local host
  host=$(ui_listen_access "Who can connect") || return 0
  TABBY_NETWORK_HOST="${host:-0.0.0.0}"
}

# Review menu: every setting is a row. Esc on a row returns here.
# Esc on this menu aborts. Start install leaves the loop.
prompt_review_hub() {
  local kind=$1 choice v weight_gib model_desc
  TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
  TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
  TABBY_MODELS="${TABBY_MODELS:-core}"
  if [[ -z "${DISK:-}" ]]; then
    DISK=$(first_install_disk || true)
  fi
  while true; do
    local -a items=()
    items+=(disk "$(hub_desc "$(disk_hub_label)")")
    items+=(hostname "$(hub_desc "$TARGET_HOSTNAME")")
    items+=(user "$(hub_desc "$TARGET_USER")")
    if [[ "$kind" == advanced ]]; then
      items+=(locale "$(hub_desc "$(locale_hub_label)")")
      items+=(desktop "$(hub_desc "$(encrypt_hub_label)")")
    else
      items+=(timezone "$(hub_desc "$TIMEZONE")")
    fi
    items+=(weights "$(hub_desc "$(weights_label)")")
    weight_gib="$(model_disk_gib "${TABBY_MODELS:-core}")"
    if [[ "$weight_gib" =~ ^[0-9]+$ ]]; then
      model_desc="${TABBY_MODELS:-core} (~${weight_gib} GiB)"
    else
      model_desc="${TABBY_MODELS:-core}"
    fi
    items+=(models "$(hub_desc "$model_desc")")
    if [[ "$kind" == advanced ]]; then
      items+=(network "$(hub_desc "$(access_label)")")
      items+=(tunnel "$(hub_desc "$(tunnel_label)")")
    else
      items+=(access "$(hub_desc "$(access_label)")")
    fi
    items+=(go "Start install")
    choice=$(ui_menu "Review install plan" \
"Open a row to change it. Choose Start install when the plan
looks right.

Esc aborts. Next you type the disk path to confirm the wipe.
After that, the install screen stays up: steps, a progress
bar, and the live log." \
      "${items[@]}") || ui_cancel
    UI_ALLOW_BACK=1
    case "$choice" in
      disk) ask_install_disk "Target disk" || true ;;
      hostname) hub_edit_hostname ;;
      user) hub_edit_user ;;
      timezone) hub_edit_timezone ;;
      locale) hub_edit_locale ;;
      desktop) hub_edit_desktop ;;
      weights) prompt_weights_source "Weights source" || true ;;
      models)
        if [[ "$kind" == simple ]]; then
          simple_edit_models
        else
          hub_edit_models
        fi
        ;;
      network) hub_edit_network ;;
      tunnel) hub_edit_tunnel ;;
      access)
        if [[ -z "${HOST_FROM_CLI:-}" ]]; then
          hub_edit_access
        else
          ui_msg "Listen address" "Already set on the command line (--tabby-host)." || true
        fi
        ;;
      go)
        if [[ -z "${DISK:-}" ]]; then
          ui_msg "Pick a disk" "Choose the disk to wipe before starting." || true
          continue
        fi
        UI_ALLOW_BACK=0
        break
        ;;
    esac
    UI_ALLOW_BACK=0
  done
}

prompt_settings_simple_tui() {
  ui_msg "Simple setup" \
"This installs Arch and tabbyapi-stack on the disk you pick.

A review menu lists every setting. Open a row to change it,
then Start install.

  • disk to wipe (type the path to confirm)
  • hostname, username, and timezone (system clock)
  • model weights (Hugging Face, USB, or a path)
  • this PC vs other computers on the LAN
  • password after you confirm (login only; the disk is not
    encrypted unless you passed --encrypt)

Omarchy is not installed. Encryption, full model control, and SSH
tunnels are under Advanced.

After the wipe confirm, the install screen stays up with
the step list, a progress bar, and the live log.

Esc on the review menu cancels. Esc on a setting goes back."

  prompt_timezone || true
  if [[ -z "${TABBY_CACHE:-}" && "${TABBY_MODELS:-core}" == core ]]; then
    TABBY_MODELS="$(simple_model_baseline "$(gpu_vram_mib)")"
  fi
  simple_edit_models
  prompt_review_hub simple
}

prompt_settings_text() {
  log "No config file given. Enter settings, or press Enter to keep the default."
  printf '\n' >/dev/tty
  show_available_disks
  printf '\n' >/dev/tty

  ask_install_disk
  TARGET_HOSTNAME=$(ask_until "Hostname" "$TARGET_HOSTNAME" valid_hostname)
  TARGET_USER=$(ask_until "Username" "$TARGET_USER" valid_username)
  prompt_timezone || true
  LOCALE=$(ask "Locale" "$LOCALE")
  KEYMAP=$(ask "Console keymap" "$KEYMAP")
  ESP_SIZE=$(ask_until "EFI partition size" "$ESP_SIZE" valid_esp_size)

  local omarchy_answer
  omarchy_answer=$(ask_until "Install Omarchy desktop (requires LUKS) (yes / no)" "$(omarchy_yes_no)" valid_yes_no)
  if [[ "$omarchy_answer" == "yes" ]]; then
    OMARCHY_MODE=now
  else
    OMARCHY_MODE=skip
  fi
  if [[ "$OMARCHY_MODE" == "now" ]]; then
    ENCRYPT=1
    printf 'Omarchy selected — disk encryption is required and will be enabled.\n' >/dev/tty
    OMARCHY_USER_NAME=$(ask "Git name (optional, used by Omarchy)" "$OMARCHY_USER_NAME")
    OMARCHY_USER_EMAIL=$(ask "Git email (optional, used by Omarchy)" "$OMARCHY_USER_EMAIL")
  else
    local encrypt_answer
    encrypt_answer=$(ask_until "Encrypt the disk with LUKS (yes / no)" "$(encrypt_label)" valid_yes_no)
    if [[ "$encrypt_answer" == "yes" ]]; then
      ENCRYPT=1
    else
      ENCRYPT=0
    fi
  fi

  prompt_weights_source "Weights source"
  TABBY_MODELS=$(ask_until "Models (core / all / comma-separated ids)" "${TABBY_MODELS:-core}" valid_models)
  TABBY_NETWORK_HOST=$(ui_listen_host "TabbyAPI listen address" "${TABBY_NETWORK_HOST:-0.0.0.0}")
  TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
  TABBY_NETWORK_PORT=$(ask_until "TabbyAPI listen port" "${TABBY_NETWORK_PORT:-5000}" valid_port)
  COMFYUI_URL=$(ask "ComfyUI URL" "${COMFYUI_URL:-http://127.0.0.1:8188}")
  TABBY_PUBLIC_BASE=$(ask "Public URL (blank = local only)" "${TABBY_PUBLIC_BASE}")
  printf '%s\n' >/dev/tty \
"Optional SSH login for a reverse tunnel (user@host).
Blank = API stays on this machine. If you set a host you will
need to put this box's public key in authorized_keys there."
  TABBY_SSH_REMOTE=$(ask "SSH tunnel target (blank = none)" "${TABBY_SSH_REMOTE}")
  if [[ -n "$TABBY_SSH_REMOTE" ]]; then
    printf '%s\n' >/dev/tty \
"ssh -R spec: bind:remote_port:local_host:local_port
Default listens on ${TABBY_SSH_REMOTE} port 12345 and lands
on TabbyAPI here at 127.0.0.1:${TABBY_NETWORK_PORT}."
    TABBY_SSH_FORWARD=$(ask "SSH -R spec" \
      "${TABBY_SSH_FORWARD:-127.0.0.1:12345:127.0.0.1:${TABBY_NETWORK_PORT}}")
    ssh_tunnel_help "$TABBY_SSH_REMOTE" "$TABBY_SSH_FORWARD" "$TABBY_PUBLIC_BASE" >/dev/tty
    printf '\nUpload the matching .pub to %s after install.\n' "$TABBY_SSH_REMOTE" >/dev/tty
    TABBY_SSH_KEY=$(ask "SSH key path" \
      "${TABBY_SSH_KEY:-/home/${TARGET_USER}/.ssh/id_ed25519}")
  else
    TABBY_SSH_FORWARD=""
    TABBY_SSH_KEY=""
  fi
  printf '\n' >/dev/tty
}

prompt_settings_tui() {
  ui_msg "What this installer does" \
"Install Arch Linux from this live ISO, then tabbyapi-stack (Python,
venvs, model weights) before you reboot.

The target disk is wiped. First boot starts the API (linger).
Omarchy is optional and requires LUKS.

Needed
  • Official Arch live ISO, root, internet, x86_64
  • NVIDIA GPU (Turing / RTX 20-series or newer)
  • Secure Boot off

A review menu lists every setting. Open a row to change it,
then Start install. You type the disk path to confirm the
wipe. After that, the install screen stays up: steps, a
progress bar, elapsed time, and the live log. install.sh
does not open a second dialog.

Esc on the review menu cancels. Esc on a setting goes back."

  prompt_review_hub advanced
}

encrypt_label() {
  if ((ENCRYPT)); then
    printf 'yes'
  else
    printf 'no'
  fi
}

omarchy_yes_no() {
  if [[ "$OMARCHY_MODE" == "now" ]]; then
    printf 'yes'
  else
    printf 'no'
  fi
}

# /dev/sda 1 -> /dev/sda1
# /dev/nvme0n1 1 -> /dev/nvme0n1p1
# /dev/mmcblk0 2 -> /dev/mmcblk0p2
# /dev/vda 1 -> /dev/vda1
part_dev() {
  local disk=$1
  local n=$2
  case "$disk" in
    *[0-9]) printf '%sp%s\n' "$disk" "$n" ;;
    *) printf '%s%s\n' "$disk" "$n" ;;
  esac
}

is_uefi() {
  [[ -d /sys/firmware/efi ]]
}

btrfs_opts() {
  local opts="noatime,compress=zstd:1,space_cache=v2"
  if [[ -b "$DISK" ]] && [[ "$(lsblk -dn -o ROTA "$DISK" 2>/dev/null || echo 1)" == "0" ]]; then
    opts+=",ssd,discard=async"
  fi
  printf '%s\n' "$opts"
}

live_iso_disk() {
  local src disk
  for src in /run/archiso/bootmnt /run/archiso/copytoram /iso; do
    if src=$(findmnt -n -o SOURCE "$src" 2>/dev/null); then
      disk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1 || true)
      if [[ -z "$disk" && "$src" == /dev/* ]]; then
        disk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1 || true)
      fi
      if [[ -n "$disk" ]]; then
        printf '/dev/%s\n' "$disk"
        return 0
      fi
    fi
  done
  return 1
}

# NAME and TYPE only — MODEL often contains spaces and breaks field splitting.
physical_disk_names() {
  local name type
  while read -r name type; do
    [[ "$type" == "disk" ]] || continue
    [[ "$name" == loop* ]] && continue
    printf '%s\n' "$name"
  done < <(lsblk -dn -o NAME,TYPE)
}

list_install_disks() {
  local iso name size model
  iso=$(live_iso_disk || true)
  while read -r name; do
    [[ -n "$name" ]] || continue
    if [[ -n "$iso" && "/dev/$name" == "$iso" ]]; then
      continue
    fi
    size=$(lsblk -dn -o SIZE "/dev/$name" 2>/dev/null | tr -d ' ')
    model=$(lsblk -dn -o MODEL "/dev/$name" 2>/dev/null | sed 's/[[:space:]]*$//')
    printf '/dev/%s\t%s\t%s\n' "$name" "$size" "$model"
  done < <(physical_disk_names)
}

first_install_disk() {
  local line
  line=$(list_install_disks | head -n1 || true)
  [[ -n "$line" ]] || return 1
  printf '%s\n' "${line%%$'\t'*}"
}

cpu_ucode_pkg() {
  if grep -q AuthenticAMD /proc/cpuinfo 2>/dev/null; then
    printf '%s\n' amd-ucode
  elif grep -q GenuineIntel /proc/cpuinfo 2>/dev/null; then
    printf '%s\n' intel-ucode
  fi
}

# Arch removed the proprietary `nvidia` package when the 590 driver dropped
# Pascal. Official repos now ship nvidia-open (Turing / RTX 20-series+).
# nvidia-open Provides: NVIDIA-MODULE and Conflicts: nvidia — it does not
# provide the name `nvidia`, so pacman -S nvidia is "target not found".
# An outdated live ISO database may not list nvidia-open until after -Sy.
pacman_pkg_available() {
  if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    pacman --config "$(offline_pacman_config)" -Si "$1" >/dev/null 2>&1
  else
    pacman -Si "$1" >/dev/null 2>&1
  fi
}

sync_live_pacman() {
  disable_live_mkinitcpio_hooks
  if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    log "Using frozen TSOS package repository"
    offline_pacman_config >/dev/null
    return 0
  fi
  log "Refreshing package databases"
  pacman -Sy --noconfirm
}

offline_pacman_config() {
  [[ -n "$TSOS_OFFLINE_ROOT" && -f "$TSOS_OFFLINE_ROOT/pacman/tsos.db" ]] || return 1
  if [[ -n "$TSOS_PACMAN_CONFIG" && -f "$TSOS_PACMAN_CONFIG" ]]; then
    printf '%s\n' "$TSOS_PACMAN_CONFIG"
    return 0
  fi
  TSOS_PACMAN_CONFIG=/tmp/tsos-pacman.conf
  {
    printf '[options]\nArchitecture = auto\nSigLevel = Required DatabaseOptional\n'
    printf 'LocalFileSigLevel = Optional\n\n'
    printf '[tsos]\nSigLevel = Optional TrustAll\nServer = file://%s/pacman\n\n' "$TSOS_OFFLINE_ROOT"
    awk 'BEGIN { keep=0 } /^\[[^]]+\]/ { keep=($0 != "[options]") } keep { print }' /etc/pacman.conf
  } >"$TSOS_PACMAN_CONFIG"
  printf '%s\n' "$TSOS_PACMAN_CONFIG"
}

nvidia_pkg() {
  if pacman_pkg_available nvidia-open; then
    printf '%s\n' nvidia-open
  elif pacman_pkg_available nvidia; then
    printf '%s\n' nvidia
  else
    return 1
  fi
}

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

gpu_vram_mib() {
  local mem name lower
  if command -v nvidia-smi >/dev/null 2>&1; then
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
  if command -v nvidia-smi >/dev/null 2>&1; then
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

repo_raw_base() {
  local url="${TABBY_REPO:-https://github.com/styelz/tabbyapi-stack-archlinux.git}"
  url="${url%.git}"
  url="${url%/}"
  case "$url" in
    https://github.com/*)
      printf 'https://raw.githubusercontent.com/%s/main' "${url#https://github.com/}"
      ;;
    *)
      printf 'https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main'
      ;;
  esac
}

ensure_fetch_tools() {
  local script src base
  TSOS_CATALOG="${TSOS_CATALOG:-}"
  TSOS_FETCH="${TSOS_FETCH:-}"
  if [[ -n "$TSOS_FETCH" && -f "$TSOS_FETCH" && -n "$TSOS_CATALOG" && -f "$TSOS_CATALOG" ]]; then
    return 0
  fi
  if [[ -n "$TABBY_LOCAL_SRC" && -f "$TABBY_LOCAL_SRC/tabbyAPI/deploy/arch/fetch_models.py" ]]; then
    TSOS_FETCH="$TABBY_LOCAL_SRC/tabbyAPI/deploy/arch/fetch_models.py"
    TSOS_CATALOG="$TABBY_LOCAL_SRC/tabbyAPI/deploy/arch/models.json"
    return 0
  fi
  script="${BASH_SOURCE[0]:-}"
  case "$script" in
    "" | /dev/fd/* | /proc/self/fd/* | -) script="" ;;
  esac
  if [[ -n "$script" ]]; then
    [[ "$script" == /* ]] || script="$PWD/$script"
    src="$(cd "$(dirname "$script")" && pwd)"
    if [[ -f "$src/tabbyAPI/deploy/arch/fetch_models.py" ]]; then
      TSOS_FETCH="$src/tabbyAPI/deploy/arch/fetch_models.py"
      TSOS_CATALOG="$src/tabbyAPI/deploy/arch/models.json"
      return 0
    fi
  fi
  base="$(repo_raw_base)"
  TSOS_CATALOG="${TMPDIR:-/tmp}/tsos-models.json"
  TSOS_FETCH="${TMPDIR:-/tmp}/tsos-fetch_models.py"
  curl -fsSL "$base/tabbyAPI/deploy/arch/models.json" -o "$TSOS_CATALOG" || return 1
  curl -fsSL "$base/tabbyAPI/deploy/arch/fetch_models.py" -o "$TSOS_FETCH" || return 1
  [[ -s "$TSOS_CATALOG" && -s "$TSOS_FETCH" ]]
}

pick_models_ui() {
  local title="$1"
  local text="$2"
  local source="$3"
  local cache="${4:-}"
  local mode="${5:-}"
  local selected="${6:-}"
  local vram rows id state label
  local args=()
  ensure_fetch_tools || return 2
  if ! command -v python3 >/dev/null 2>&1 || [[ ! -f "$TSOS_FETCH" ]]; then
    return 2
  fi
  vram="$(gpu_vram_mib)"
  local py=(python3 -u "$TSOS_FETCH" --catalog "$TSOS_CATALOG" --list-picks --source "$source" --vram-mib "$vram")
  if [[ -n "$cache" && -d "$cache" ]]; then
    py+=(--cache "$cache")
  fi
  [[ "$mode" == extras ]] && py+=(--extras-only)
  [[ -n "$selected" ]] && py+=(--selected-ids "$selected")
  rows="$("${py[@]}" 2>/dev/null || true)"
  [[ -n "$rows" ]] || return 2
  while IFS=$'\t' read -r id state label; do
    [[ -n "${id:-}" ]] || continue
    args+=("$id" "$label" "$state")
  done <<< "$rows"
  ((${#args[@]} >= 3)) || return 2
  ui_checklist "$title" "$text" "${args[@]}"
}

model_disk_gib() {
  local ids="${1:-core}"
  ensure_fetch_tools || return 0
  if command -v python3 >/dev/null 2>&1 && [[ -f "$TSOS_FETCH" ]]; then
    python3 -u "$TSOS_FETCH" --catalog "$TSOS_CATALOG" --ids "$ids" --disk-gib 2>/dev/null || true
  fi
}

simple_model_baseline() {
  local vram="${1:-0}" baseline=""
  ensure_fetch_tools || true
  if command -v python3 >/dev/null 2>&1 && [[ -f "${TSOS_FETCH:-}" ]]; then
    baseline="$(python3 -u "$TSOS_FETCH" --catalog "$TSOS_CATALOG" --baseline --vram-mib "$vram" 2>/dev/null || true)"
  fi
  printf '%s' "${baseline:-core}"
}

simple_edit_models() {
  local vram gpu_label baseline picked candidate weight_gib rc
  if [[ -n "${TABBY_CACHE:-}" ]]; then
    hub_edit_models
    return 0
  fi
  vram="$(gpu_vram_mib)"
  gpu_label="$(gpu_prompt_label "$vram")"
  baseline="$(simple_model_baseline "$vram")"
  while true; do
    rc=0
    picked=$(pick_models_ui "Additional Hugging Face models" \
"The coding, embedding, and image baseline is selected automatically:
  ${baseline}

Optional models that fit ${gpu_label} are below.
Each row shows its estimated download size.

Space toggles a row. Enter confirms." \
      hf "" extras "${TABBY_MODELS:-$baseline}") || rc=$?
    case "$rc" in
      1) return 0 ;;
      2)
        TABBY_MODELS="$baseline"
        return 0
        ;;
    esac
    candidate="$baseline"
    [[ -n "$picked" ]] && candidate="${candidate},${picked}"
    weight_gib="$(model_disk_gib "$candidate")"
    [[ "$weight_gib" =~ ^[0-9]+$ ]] || weight_gib="unknown"
    rc=0
    ui_yesno "Confirm model downloads" \
"Selected: ${candidate}

Model weights: about ${weight_gib} GiB
Python, CUDA, and environments: about 15 GiB extra

Yes = use this selection.
No = return to the model checklist." 1 || rc=$?
    case "$rc" in
      0) TABBY_MODELS="$candidate"; break ;;
      2) return 0 ;;
    esac
  done
}

self_test() {
  local failed=0
  check() {
    local got=$1 expected=$2 label=$3
    if [[ "$got" != "$expected" ]]; then
      printf 'FAIL %s: got %q expected %q\n' "$label" "$got" "$expected" >&2
      failed=1
    else
      printf 'ok   %s\n' "$label"
    fi
  }
  check "$(part_dev /dev/sda 1)" /dev/sda1 "sda p1"
  check "$(part_dev /dev/sda 2)" /dev/sda2 "sda p2"
  check "$(part_dev /dev/vda 1)" /dev/vda1 "vda p1"
  check "$(part_dev /dev/nvme0n1 1)" /dev/nvme0n1p1 "nvme p1"
  check "$(part_dev /dev/nvme0n1 2)" /dev/nvme0n1p2 "nvme p2"
  check "$(part_dev /dev/mmcblk0 1)" /dev/mmcblk0p1 "mmc p1"
  check "$(part_dev /dev/loop0 1)" /dev/loop0p1 "loop p1"
  local offline_test
  offline_test=$(mktemp -d)
  mkdir -p "$offline_test"/{pacman,wheels,tabbyapi-stack}
  touch "$offline_test/pacman/tsos.db" "$offline_test/tabbyapi-stack/install.sh"
  on=0
  offline_payload_available "$offline_test" || on=1
  check "$on" 0 "complete offline payload"
  rm -f "$offline_test/pacman/tsos.db"
  on=0
  offline_payload_available "$offline_test" || on=1
  check "$on" 1 "incomplete offline payload rejected"
  rm -rf "$offline_test"
  on=0
  is_on_disk /dev/sda /dev/sda || on=1
  check "$on" 0 "disk matches itself"
  on=0
  is_on_disk /dev/sda /dev/sda2 || on=1
  check "$on" 0 "sda2 on sda"
  on=0
  is_on_disk /dev/sda /dev/sdb || on=1
  check "$on" 1 "sdb not on sda"
  on=0
  is_on_disk /dev/nvme0n1 /dev/nvme0n1p2 || on=1
  check "$on" 0 "nvme p2 on disk"

  DISK=/dev/sda
  BOOT_N=1
  DATA_N=2
  BIOS_N=""
  BIOS_PART="stale"
  setup_partitions
  check "$BOOT_PART" /dev/sda1 "uefi boot part"
  check "$DATA_PART" /dev/sda2 "uefi data part"
  check "${BIOS_PART}" "" "uefi has no bios part"

  BIOS_N=1
  BOOT_N=2
  DATA_N=3
  setup_partitions
  check "$BIOS_PART" /dev/sda1 "bios bios part"
  check "$BOOT_PART" /dev/sda2 "bios boot part"
  check "$DATA_PART" /dev/sda3 "bios data part"

  ENCRYPT=1
  check "$(encrypt_label)" yes "encrypt label yes"
  ENCRYPT=0
  check "$(encrypt_label)" no "encrypt label no"

  ENCRYPT=1
  ENCRYPT_FROM_CLI=""
  OMARCHY_MODE=now
  OMARCHY_FROM_CLI=""
  apply_simple_defaults
  check "$OMARCHY_MODE" skip "simple skips omarchy"
  check "$ENCRYPT" 0 "simple encrypt off"
  check "$TABBY_MODELS" core "simple models core"
  check "$TABBY_SAVER_ENABLED" 1 "simple screensaver on"
  ENCRYPT=1
  ENCRYPT_FROM_CLI=1
  OMARCHY_MODE=now
  OMARCHY_FROM_CLI=1
  apply_simple_defaults
  check "$ENCRYPT" 1 "simple --encrypt kept"
  check "$OMARCHY_MODE" skip "simple still skips omarchy"
  TARGET_HOSTNAME=studio
  apply_simple_defaults
  check "$TARGET_HOSTNAME" studio "simple keeps hostname"
  TIMEZONE=Australia/Sydney
  apply_simple_defaults
  check "$TIMEZONE" Australia/Sydney "simple keeps timezone"
  apply_timezone "/usr/share/zoneinfo/America/New_York"
  check "$TIMEZONE" America/New_York "timezone strips zoneinfo prefix"
  apply_timezone ""
  check "$TIMEZONE" UTC "empty timezone becomes UTC"
  TIMEZONE=Europe/Paris
  TIMEZONE_FROM_CLI=""
  maybe_detect_timezone
  check "$TIMEZONE" Europe/Paris "maybe_detect keeps a set timezone"
  TIMEZONE=UTC
  TIMEZONE_FROM_CLI=1
  maybe_detect_timezone
  check "$TIMEZONE" UTC "maybe_detect respects --timezone"
  TIMEZONE_FROM_CLI=""
  TABBY_NETWORK_HOST=""
  HOST_FROM_CLI=""
  apply_simple_defaults
  check "$TABBY_NETWORK_HOST" "0.0.0.0" "simple access defaults to LAN"
  TABBY_NETWORK_HOST=127.0.0.1
  apply_simple_defaults
  check "$TABBY_NETWORK_HOST" "127.0.0.1" "simple keeps this-pc host"
  HOST_FROM_CLI=1
  TABBY_NETWORK_HOST=127.0.0.1
  apply_simple_defaults
  check "$TABBY_NETWORK_HOST" "127.0.0.1" "simple --tabby-host kept"
  HOST_FROM_CLI=""

  if valid_omarchy_mode now && valid_omarchy_mode skip && ! valid_omarchy_mode later; then
    printf 'ok   omarchy now/skip\n'
  else
    printf 'FAIL omarchy mode: now/skip only\n' >&2
    failed=1
  fi

  ENCRYPT=1
  PASSWORD=test-password
  LUKS_PASSWORD=""
  USER_PASSWORD=""
  ROOT_PASSWORD=""
  if collect_passwords; then
    printf 'ok   encrypted password collection returns success\n'
  else
    printf 'FAIL encrypted password collection returned failure\n' >&2
    failed=1
  fi
  check "$LUKS_PASSWORD" test-password "encrypted password assigned"

  # The percent meter must never be printed by gauge_update (that was the
  # leak under the leftover password box). It writes files; the foreground
  # dialog paints them.
  local gdir gout saved_log tb
  gdir=$(mktemp -d "${TMPDIR:-/tmp}/tsos-gauge-test.XXXXXX")
  saved_log=$TSOS_LOG
  TSOS_GAUGE_DIR=$gdir
  TSOS_SAVED_FD=1
  TSOS_LOG="$gdir/log"
  : >"$TSOS_LOG"
  gout=$(gauge_update 22 "Installing Arch packages" 2>&1)
  check "$gout" "" "gauge_update silent on stdout"
  check "$(cat "$gdir/pct")" "22" "gauge pct file"
  check "$(cat "$gdir/heading")" "Installing Arch packages" "gauge heading file"
  check "$(ui_pad "ab" 5)" "ab   " "ui_pad pads"
  check "$(ui_pad "abcdef" 4)" "abcd" "ui_pad truncates"
  check "$(hub_desc "short")" "short" "hub_desc short"
  check "$(hub_desc "$(printf 'a%.0s' {1..60})" 20)" "aaaaaaaaaaaaaaaaa..." "hub_desc truncates"
  tb=$(ui_title_bar 10 Hi)
  check "$tb" "--- Hi ---" "ui_title_bar"
  OMARCHY_MODE=skip
  DISK=/dev/sda
  TARGET_HOSTNAME=studio
  check "$(gauge_step_index "Installing Arch packages" 22)" "3" "arch step index"
  check "$(gauge_step_index "Starting the install..." 0)" "0" "start step index"
  check "$(gauge_step_index "Cleaning up" 98)" "6" "done step index"
  check "$(gauge_step_index "Installing tabbyapi-stack" 45)" "5" "app step index"
  # Install page: geometry for the 24x80 ISO console, chips, one frame.
  page_layout 24 80
  check "$PAGE_H $PAGE_W $PAGE_LOG_N $PAGE_Y $PAGE_X" "19 74 7 2 2" "page layout 24x80"
  page_layout 67 240
  check "$PAGE_H $PAGE_W $PAGE_LOG_N" "24 96 12" "page layout 67x240"
  page_layout 24 80
  page_chips 70 "Installing Arch packages" 22 3 '|'
  case "$PAGE_P" in
    *"$P_DONE"'[x] Disk'*"$P_DLG"'['"$P_SPIN_ALT"'|'"$P_DLG"'] Arch'*"$P_DLG"'[ ] App'*) printf 'ok   page chips\n' ;;
    *) printf 'FAIL page chips: %q\n' "$PAGE_P" >&2; failed=1 ;;
  esac
  if ((PAGE_V > 40 && PAGE_V <= 70)); then
    printf 'ok   page chips width (%s)\n' "$PAGE_V"
  else
    printf 'FAIL page chips width: %s\n' "$PAGE_V" >&2
    failed=1
  fi
  page_chips 70 "Installing Arch packages" 22 4 '|'
  case "$PAGE_P" in
    *"$P_DLG"'['"$P_SPIN"'|'"$P_DLG"'] Arch'*) printf 'ok   page chips spinner\n' ;;
    *) printf 'FAIL page chips spinner: %q\n' "$PAGE_P" >&2; failed=1 ;;
  esac
  PAGE_UTF8=1
  page_frame "Installing Arch packages" 22 "5s" "1m 30s" '|' 1 \
    "Installing Arch Linux and tabbyapi-stack on /dev/sda as studio." \
    "Full log: /tmp/tsos-installer.log   Ctrl+C cancels." $'hello\nworld'
  local moves
  moves=$(printf '%s' "$PAGE_BUF" | grep -o $'\033\\[[0-9]*;[0-9]*H' | wc -l)
  check "$moves" "$PAGE_H" "page frame paints every box row"
  case "$PAGE_BUF" in
    *' Installing '*'/dev/sda as studio.'*"$P_KEY"'Ctrl+C'*'['"$P_SPIN_ALT"'|'"$P_DLG"'] Arch'*"$P_HEAD"'Installing Arch packages'*'│'*' hello '*'│'*' world '*"$P_BAR_ON"*' 22%'*'┘') printf 'ok   page frame content\n' ;;
    *) printf 'FAIL page frame content: %q\n' "$PAGE_BUF" >&2; failed=1 ;;
  esac
  PAGE_UTF8=0
  page_glyph tl
  check "$PAGE_G" $'\033(0l\033(B' "page glyph acs"
  PAGE_UTF8=""
  # The disk-release sweep must never target this installer or its session.
  local -a TSOS_OWN_PIDS=()
  mapfile -t TSOS_OWN_PIDS < <(own_process_pids)
  if is_own_pid "$$" && is_own_pid "$BASHPID" && is_own_pid "$PPID" && is_own_pid 1 && ! is_own_pid 999999; then
    printf 'ok   own pids protected (%s)\n' "${#TSOS_OWN_PIDS[@]}"
  else
    printf 'FAIL own pids: %s\n' "${TSOS_OWN_PIDS[*]}" >&2
    failed=1
  fi
  TSOS_GAUGE_DIR=""
  TSOS_SAVED_FD=""
  TSOS_LOG=$saved_log
  rm -rf "$gdir"

  if ((failed)); then
    die "self-test failed"
  fi
  log "self-test passed"
}

self_test_gauge() {
  USE_TUI=1
  TUI=dialog
  need_cmd dialog || die "dialog is not installed (needed for --self-test-gauge)"
  write_dialogrc
  TSOS_LOG="${TMPDIR:-/tmp}/tsos-gauge-selftest.log"
  : >"$TSOS_LOG"
  # Same sequence as the ISO after the last question: a dialog widget on
  # /dev/tty, then the gauge. Do not restore_tty in between — that is the
  # leftover-password-box path. `dialog --timeout` does not fire on a
  # password field; GNU timeout needs --foreground so dialog still owns the
  # tty. The real installer waits for OK.
  set +e
  timeout --foreground 1 dialog --backtitle "$BACKTITLE" --title "Password" \
    --insecure --passwordbox "Set a password for tabby (and root)." 8 50 >/dev/tty
  set -e
  dummy_gauge_work() {
    gauge_update 10 "Preparing disk"
    sleep 0.4
    gauge_update 22 "Installing Arch packages"
    sleep 0.8
    gauge_update 100 "Finished"
  }
  run_with_gauge dummy_gauge_work
  printf 'GAUGE_TEST_OK\n'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --disk)
        DISK=${2:?--disk requires a path}
        shift 2
        ;;
      --hostname)
        TARGET_HOSTNAME=${2:?}
        shift 2
        ;;
      --user)
        TARGET_USER=${2:?}
        shift 2
        ;;
      --timezone)
        TIMEZONE=${2:?}
        TIMEZONE_FROM_CLI=1
        shift 2
        ;;
      --locale)
        LOCALE=${2:?}
        shift 2
        ;;
      --keymap)
        KEYMAP=${2:?}
        shift 2
        ;;
      --esp-size)
        ESP_SIZE=${2:?}
        shift 2
        ;;
      --encrypt)
        ENCRYPT=1
        ENCRYPT_FROM_CLI=1
        shift
        ;;
      --no-encrypt)
        ENCRYPT=0
        ENCRYPT_FROM_CLI=1
        shift
        ;;
      --simple)
        INSTALL_MODE=simple
        shift
        ;;
      --advanced)
        INSTALL_MODE=advanced
        shift
        ;;
      --with-omarchy)
        OMARCHY_MODE=now
        OMARCHY_FROM_CLI=1
        shift
        ;;
      --skip-omarchy)
        OMARCHY_MODE=skip
        OMARCHY_FROM_CLI=1
        shift
        ;;
      --name)
        OMARCHY_USER_NAME=${2:?}
        shift 2
        ;;
      --email)
        OMARCHY_USER_EMAIL=${2:?}
        shift 2
        ;;
      --models)
        TABBY_MODELS=${2:?}
        MODELS_FROM_CLI=1
        shift 2
        ;;
      --tabby-host)
        TABBY_NETWORK_HOST=${2:?}
        HOST_FROM_CLI=1
        shift 2
        ;;
      --tabby-port)
        TABBY_NETWORK_PORT=${2:?}
        shift 2
        ;;
      --tabby-cache)
        TABBY_CACHE=${2:?}
        CACHE_FROM_CLI=1
        shift 2
        ;;
      --tabby-repo)
        TABBY_REPO=${2:?}
        shift 2
        ;;
      --tabby-local-src)
        TABBY_LOCAL_SRC=${2:?}
        shift 2
        ;;
      --resume-tabby)
        RESUME_TABBY=1
        shift
        ;;
      --config)
        [[ -f "${2:?}" ]] || die "config file not found: $2"
        # shellcheck disable=SC1090
        source "$2"
        CONFIG_PROVIDED=1
        shift 2
        ;;
      --confirm-wipe)
        CONFIRM_WIPE=${2:?}
        shift 2
        ;;
      --password-env)
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --self-test)
        self_test
        exit 0
        ;;
      --self-test-gauge)
        self_test_gauge
        exit 0
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

normalize_encrypt() {
  case "${ENCRYPT}" in
    1 | yes | true | on) ENCRYPT=1 ;;
    0 | no | false | off) ENCRYPT=0 ;;
    *) die "invalid ENCRYPT: $ENCRYPT (use 1/0 or yes/no)" ;;
  esac
}

validate_names() {
  valid_username "$TARGET_USER" || die "invalid user name: $TARGET_USER"
  valid_hostname "$TARGET_HOSTNAME" || die "invalid hostname: $TARGET_HOSTNAME"
  valid_omarchy_mode "$OMARCHY_MODE" || die "invalid OMARCHY_MODE: $OMARCHY_MODE (now or skip)"
  valid_esp_size "$ESP_SIZE" || die "invalid EFI size: $ESP_SIZE"
  valid_models "$TABBY_MODELS" || die "invalid TABBY_MODELS: $TABBY_MODELS (core, all, or comma-separated ids)"
  valid_port "$TABBY_NETWORK_PORT" || die "invalid TabbyAPI port: $TABBY_NETWORK_PORT"
  normalize_encrypt
  if [[ "$OMARCHY_MODE" == "now" && "$ENCRYPT" -eq 0 ]]; then
    die "Omarchy requires LUKS. Re-run with encryption, or skip Omarchy."
  fi
  if [[ -n "$TABBY_LOCAL_SRC" ]]; then
    [[ -f "$TABBY_LOCAL_SRC/install.sh" && -f "$TABBY_LOCAL_SRC/tabbyAPI/pyproject.toml" ]] || \
      die "TABBY_LOCAL_SRC is not a tabbyapi-stack tree: $TABBY_LOCAL_SRC"
  fi
}

pick_disk_if_needed() {
  if [[ -n "$DISK" ]]; then
    return 0
  fi
  local disks=() line
  mapfile -t disks < <(list_install_disks)
  ((${#disks[@]})) || die "no candidate disks found"
  if ((${#disks[@]} == 1)) && [[ -n "${CONFIRM_WIPE:-}" ]]; then
    DISK=${disks[0]%%$'\t'*}
    return 0
  fi
  if ((USE_TUI)); then
    local args=() path size model
    for line in "${disks[@]}"; do
      path=${line%%$'\t'*}
      size=${line#*$'\t'}
      model=${size#*$'\t'}
      size=${size%%$'\t'*}
      args+=("$path" "${size}  ${model}")
    done
    DISK=$(ui_menu "Target disk" \
"This disk will be wiped. The live ISO device is hidden." \
      "${args[@]}")
    return 0
  fi
  printf 'Available disks (the live ISO device is hidden):\n' >/dev/tty
  local i=1
  for line in "${disks[@]}"; do
    printf '  %d) %s\n' "$i" "$line" >/dev/tty
    i=$((i + 1))
  done
  local choice
  choice=$(read_tty "Select disk number: ")
  [[ "$choice" =~ ^[0-9]+$ ]] || die "invalid selection"
  ((choice >= 1 && choice <= ${#disks[@]})) || die "invalid selection"
  DISK=${disks[$((choice - 1))]%%$'\t'*}
}

require_disk() {
  [[ -n "$DISK" ]] || die "no disk selected (pass --disk /dev/sda)"
  if ((DRY_RUN)); then
    return 0
  fi
  if [[ ! -b "$DISK" ]]; then
    die "$DISK is not a disk on this machine. Run the installer from the Arch live ISO (or pass --dry-run to preview)."
  fi
  local iso
  if iso=$(live_iso_disk); then
    if [[ "$DISK" == "$iso" ]]; then
      die "$DISK looks like the live ISO. Refusing to wipe the USB/DVD you booted from."
    fi
  fi
}

confirm_wipe() {
  log "THIS WILL ERASE EVERYTHING ON $DISK"
  local disk_tree
  disk_tree=$(lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$DISK" 2>/dev/null || true)
  if [[ -n "$CONFIRM_WIPE" ]]; then
    [[ "$CONFIRM_WIPE" == "$DISK" ]] || die "--confirm-wipe must match --disk exactly (got $CONFIRM_WIPE)"
    return 0
  fi
  local answer
  if ((USE_TUI)); then
    # Warning first so fit_text cannot drop it. One box: plan + type-to-confirm.
    answer=$(ui_input "Confirm wipe" \
"Type this path exactly to erase the disk:

    ${DISK}

THIS ERASES EVERYTHING ON ${DISK}.

${disk_tree}

$(print_plan)

Anything else aborts." \
      "")
  else
    printf '\n' >/dev/tty
    printf '%s\n' "Settings are done. The installer is waiting for a wipe confirmation." >/dev/tty
    printf '%s\n' "$disk_tree" >/dev/tty
    printf '%s\n' "Type the disk path exactly, then press Enter:" >/dev/tty
    printf '    %s\n' "$DISK" >/dev/tty
    answer=$(read_tty "Confirm wipe: ")
  fi
  [[ "$answer" == "$DISK" ]] || die "aborted (typed '$answer', needed '$DISK')"
}

collect_passwords() {
  if [[ -n "$PASSWORD" ]]; then
    LUKS_PASSWORD=${LUKS_PASSWORD:-$PASSWORD}
    USER_PASSWORD=${USER_PASSWORD:-$PASSWORD}
    ROOT_PASSWORD=${ROOT_PASSWORD:-$PASSWORD}
  fi
  local need=0
  if [[ -z "$USER_PASSWORD" || -z "$ROOT_PASSWORD" ]]; then
    need=1
  fi
  if ((ENCRYPT)) && [[ -z "$LUKS_PASSWORD" ]]; then
    need=1
  fi
  if ((need)); then
    local pw_text
    if ((ENCRYPT)); then
      pw_text="One password is used for LUKS, your user, and root unless you set the split variables."
    else
      pw_text="One password is used for your user and root unless you set the split variables."
    fi
    log "$pw_text"
    local first second
    if ((USE_TUI)); then
      while true; do
        ui_password_pair "Password" "$pw_text"
        first=$REPLY
        second=$REPLY2
        if [[ -z "$first" ]]; then
          ui_msg "Password required" "The password cannot be empty."
          continue
        fi
        if [[ "$first" != "$second" ]]; then
          ui_msg "Passwords did not match" "Try again."
          continue
        fi
        break
      done
    else
      printf '%s\n' "$pw_text" >/dev/tty
      first=$(read_secret "Password: ")
      second=$(read_secret "Verify password: ")
      [[ -n "$first" ]] || die "password cannot be empty"
      [[ "$first" == "$second" ]] || die "passwords did not match"
    fi
    PASSWORD=$first
    USER_PASSWORD=${USER_PASSWORD:-$PASSWORD}
    ROOT_PASSWORD=${ROOT_PASSWORD:-$PASSWORD}
    if ((ENCRYPT)); then
      LUKS_PASSWORD=${LUKS_PASSWORD:-$PASSWORD}
    fi
  fi
  if ((ENCRYPT == 0)); then
    LUKS_PASSWORD=""
  fi
  # A false final arithmetic test becomes the function's return status.
  # Without an explicit success here, encrypted installs return 1 and the
  # script's `set -e` exits immediately after the password form.
  return 0
}

have_network() {
  if command -v curl >/dev/null; then
    curl -fsSL --connect-timeout 5 --max-time 10 -o /dev/null https://geo.mirror.pkgbuild.com/ && return 0
    curl -fsSL --connect-timeout 5 --max-time 10 -o /dev/null https://archlinux.org/ && return 0
    return 1
  fi
  ping -c1 -W5 geo.mirror.pkgbuild.com >/dev/null 2>&1 || ping -c1 -W5 archlinux.org >/dev/null 2>&1
}

offline_payload_available() {
  local root="${1:-}"
  [[ -n "$root" &&
     -f "$root/pacman/tsos.db" &&
     -f "$root/tabbyapi-stack/install.sh" &&
     -d "$root/wheels" ]]
}

secure_boot_enabled() {
  command -v bootctl >/dev/null || return 1
  local status=""
  if command -v timeout >/dev/null; then
    status=$(timeout 3 bootctl status 2>/dev/null || true)
  else
    status=$(bootctl status 2>/dev/null || true)
  fi
  grep -q 'Secure Boot: enabled' <<<"$status"
}

# Cheap checks before the questionnaire so a missing ISO/network fails immediately.
early_preflight() {
  ((DRY_RUN)) && return 0
  log "Checking this is an Arch live ISO..."
  [[ $(id -u) -eq 0 ]] || die "run as root from the Arch live ISO"
  [[ $(uname -m) == x86_64 ]] || die "this installer requires x86_64"
  command -v pacstrap >/dev/null || die "pacstrap not found. Boot the official Arch Linux ISO, then run this script again."
  command -v sgdisk >/dev/null || die "sgdisk not found (install gptfdisk on the live ISO)"
  if [[ -z "$TSOS_OFFLINE_ROOT" ]] && ! have_network; then
    die "no network. On Wi-Fi run: iwctl station wlan0 connect 'SSID'"
  fi
  if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    offline_payload_available "$TSOS_OFFLINE_ROOT" ||
      die "offline payload is incomplete under $TSOS_OFFLINE_ROOT (need pacman repo, wheels, and tabbyapi-stack)"
    log "Offline payload: $TSOS_OFFLINE_ROOT"
  fi
  if secure_boot_enabled; then
    die "Secure Boot is enabled. Turn it off in firmware before installing (NVIDIA + Limine)."
  fi
}

preflight() {
  ((DRY_RUN)) && return 0
  command -v cryptsetup >/dev/null || {
    log "Installing disk tools on the live ISO"
    disable_live_mkinitcpio_hooks
    if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
      pacman --config "$(offline_pacman_config)" -S --noconfirm --needed \
        cryptsetup btrfs-progs gptfdisk parted dosfstools
    else
      pacman -Sy --noconfirm --needed cryptsetup btrfs-progs gptfdisk parted dosfstools
    fi
  }
  if [[ -d /sys/firmware/efi ]]; then
    log "Firmware: UEFI"
  else
    log "Firmware: BIOS/legacy (Limine will be installed for BIOS + GPT)"
  fi
}

# Installing packages on the live ISO can trip broken mkinitcpio hooks.
# Neutralize only the live ISO hooks — never the installed system's.
disable_live_mkinitcpio_hooks() {
  local hook
  for hook in /usr/share/libalpm/hooks/*mkinitcpio*; do
    [[ -e "$hook" ]] || continue
    ln -sfn /dev/null "$hook"
  done
}

print_plan() {
  if [[ "${INSTALL_MODE:-}" == simple ]]; then
    simple_plan_notes
  fi
  local root_line data_kind
  if ((ENCRYPT)); then
    data_kind="LUKS      btrfs"
    root_line="  mapper:        /dev/mapper/$CRYPT_NAME"
  else
    data_kind="btrfs"
    root_line="  root fs:       $DATA_PART (unencrypted)"
  fi
  cat <<EOF

Install plan
  disk:          $DISK
  efi:           $BOOT_PART
  data:          $DATA_PART
$root_line
  hostname:      $TARGET_HOSTNAME
  user:          $TARGET_USER
  timezone:      $TIMEZONE
  locale:        $LOCALE
  keymap:        $KEYMAP
  firmware:      $(is_uefi && echo UEFI || echo BIOS)
  encryption:    $(encrypt_label)
  omarchy:       $OMARCHY_MODE
  tabby models:  ${TABBY_MODELS:-core}
  tabby listen:  ${TABBY_NETWORK_HOST:-0.0.0.0}:${TABBY_NETWORK_PORT:-5000}
  tabby public:  ${TABBY_PUBLIC_BASE:-'(none — local only)'}
  tabby ssh:     ${TABBY_SSH_REMOTE:-'(none — no tunnel)'}
  tabby cache:   ${TABBY_CACHE:-'(none — Hugging Face)'}
  tabby repo:    $TABBY_REPO
  tabby overlay: ${TABBY_LOCAL_SRC:-'(script dir if it contains install.sh)'}
  nvidia:        nvidia-open (Turing / RTX 20-series+; Arch no longer ships nvidia)

Layout
EOF
  if [[ -n "${BIOS_PART:-}" ]]; then
    printf '  %s   BIOS boot (unformatted, 1M)\n' "$BIOS_PART"
  fi
  cat <<EOF
  ${BOOT_PART}   FAT32     /boot     Limine + kernel
  ${DATA_PART}  ${data_kind}     @ @home @log @pkg @snapshots

EOF
}

setup_partitions() {
  BOOT_PART=$(part_dev "$DISK" "$BOOT_N")
  DATA_PART=$(part_dev "$DISK" "$DATA_N")
  # Must not end with a failing `&&` — with set -e that exits the script
  # immediately after the settings prompts on UEFI (BIOS_N is empty).
  if [[ -n "${BIOS_N:-}" ]]; then
    BIOS_PART=$(part_dev "$DISK" "$BIOS_N")
  else
    BIOS_PART=""
  fi
}

assign_partition_numbers() {
  if is_uefi; then
    BOOT_N=1
    DATA_N=2
    BIOS_N=""
  else
    BIOS_N=1
    BOOT_N=2
    DATA_N=3
  fi
  setup_partitions
}

# True when DEV is DISK or a partition of it (/dev/sda vs /dev/sda2,
# /dev/nvme0n1 vs /dev/nvme0n1p2). /dev/sda does not match /dev/sdb.
is_on_disk() {
  local disk=$1 dev=$2
  [[ -n "$disk" && -n "$dev" ]] || return 1
  [[ "$dev" == "$disk" || "$dev" == "$disk"[0-9]* || "$dev" == "$disk"p[0-9]* ]]
}

pause_udev_queue() {
  udevadm control --stop-exec-queue 2>/dev/null || return 0
  TSOS_UDEV_PAUSED=1
}

resume_udev_queue() {
  [[ "${TSOS_UDEV_PAUSED:-0}" == 1 ]] || {
    udevadm settle --timeout=8 2>/dev/null || true
    return 0
  }
  udevadm control --start-exec-queue 2>/dev/null || true
  TSOS_UDEV_PAUSED=0
  udevadm settle --timeout=15 2>/dev/null || true
}

log_disk_holders() {
  local disk=$1 part=${2:-}
  log "lsblk:"
  lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$disk" 2>/dev/null | while IFS= read -r line; do
    log "  $line"
  done
  if [[ -n "$part" ]]; then
    local sys="/sys/class/block/${part##*/}/holders"
    if [[ -d "$sys" ]]; then
      log "holders of $part: $(ls -1 "$sys" 2>/dev/null | tr '\n' ' ')"
    fi
    if command -v fuser >/dev/null 2>&1; then
      log "fuser $part: $(fuser -vm "$part" 2>&1 | tr '\n' ' ')"
    fi
    if command -v findmnt >/dev/null 2>&1; then
      log "findmnt $part: $(findmnt -n -S "$part" 2>/dev/null | tr '\n' ' ')"
    fi
  fi
  # Do not run `btrfs filesystem show` here: it can scan and re-register the
  # old device. sysfs is enough to see whether the module still holds it.
  if [[ -d /sys/fs/btrfs ]]; then
    log "sysfs btrfs: $(ls -1 /sys/fs/btrfs 2>/dev/null | grep -v '^features$' | tr '\n' ' ')"
  fi
}

# True when some btrfs mount is NOT this install disk (the live ISO itself,
# or another drive). Leftover /mnt from a previous tsos run does not count.
btrfs_held_by_live_iso() {
  local disk=$1 src tgt fstype raw
  while read -r src tgt fstype; do
    [[ "$fstype" == btrfs ]] || continue
    raw=${src%%[*}
    if is_on_disk "$disk" "$raw"; then
      continue
    fi
    if [[ "$tgt" == "$TARGET" || "$tgt" == "$TARGET"/* ]]; then
      continue
    fi
    return 0
  done < <(findmnt -n -o SOURCE,TARGET,FSTYPE -t btrfs 2>/dev/null || true)
  return 1
}

# Re-running the installer leaves the previous btrfs registered in the
# kernel even after umount. lsblk then shows sda2 unmounted, fuser is
# empty, and mkfs.btrfs still gets EBUSY (it opens O_EXCL). Same layout
# (2G EFI + rest root) means the old /dev/sda2 node never goes away.
forget_btrfs_on_disk() {
  local disk=$1 name type
  if command -v btrfs >/dev/null 2>&1; then
    btrfs device scan --forget "$disk" >/dev/null 2>&1 || true
    while read -r name type; do
      [[ "$type" == part ]] || continue
      btrfs device scan --forget "$name" >/dev/null 2>&1 || true
    done < <(lsblk -lnp -o NAME,TYPE "$disk" 2>/dev/null || true)
  fi
  # Arch live ISO is squashfs/overlay. Unload the module so it cannot keep
  # a struct btrfs_fs_info after a lazy umount from the previous run.
  if btrfs_held_by_live_iso "$disk"; then
    log "not unloading btrfs: another filesystem on this live system uses it"
    return 0
  fi
  if lsmod 2>/dev/null | grep -q '^btrfs'; then
    log "unloading btrfs module so it cannot hold $disk"
    if modprobe -r btrfs >/dev/null 2>&1; then
      modprobe btrfs >/dev/null 2>&1 || true
    else
      warn "could not unload btrfs (device still referenced)"
      log "sysfs btrfs: $(ls -1 /sys/fs/btrfs 2>/dev/null | grep -v '^features$' | tr '\n' ' ')"
    fi
  fi
}

# PIDs this installer must never kill: itself, the work child, the gauge
# pipeline, and every ancestor up to init (login shell, sshd, agetty).
# $$ is the main script even inside the work subshell; $BASHPID is not.
own_process_pids() {
  local p=$BASHPID line
  printf '%s\n' "$$" "$BASHPID" "$PPID"
  while [[ "$p" =~ ^[0-9]+$ ]] && ((p > 1)); do
    line=$(grep -m1 '^PPid:' "/proc/$p/status" 2>/dev/null || true)
    p=${line##*[[:space:]]}
    [[ -n "$p" ]] && printf '%s\n' "$p"
  done
  # Children of the main script (dialog, the repaint loop, sleep).
  pgrep -P "$$" 2>/dev/null || true
  pgrep -P "$BASHPID" 2>/dev/null || true
}

is_own_pid() {
  local p=$1 own
  [[ "$p" == 1 ]] && return 0
  for own in "${TSOS_OWN_PIDS[@]}"; do
    [[ "$p" == "$own" ]] && return 0
  done
  return 1
}

kill_disk_users() {
  local disk=$1 mp name type pid cwd root p mi my_ns ns
  local base=${disk##*/}
  local -a TSOS_OWN_PIDS=()
  mapfile -t TSOS_OWN_PIDS < <(own_process_pids)
  my_ns=$(readlink /proc/self/ns/mnt 2>/dev/null || true)
  if command -v fuser >/dev/null 2>&1; then
    # `fuser -m /mnt` when /mnt is only a directory means "the live root
    # filesystem" and kills the installer itself. Use -m only for a real mount.
    if mountpoint -q "$TARGET" 2>/dev/null; then
      fuser -km "$TARGET" >/dev/null 2>&1 || true
    fi
    while read -r mp; do
      [[ -n "$mp" && "$mp" != / ]] || continue
      fuser -km "$mp" >/dev/null 2>&1 || true
    done < <(lsblk -lnr -o MOUNTPOINT "$disk" 2>/dev/null || true)
    # No -m on the raw nodes: that would mean "every process on this fs".
    # We only want whoever still has the block device open (blkid, mkfs).
    fuser -k "$disk" >/dev/null 2>&1 || true
    while read -r name type; do
      [[ "$type" == part ]] || continue
      fuser -k "$name" >/dev/null 2>&1 || true
    done < <(lsblk -lnp -o NAME,TYPE "$disk" 2>/dev/null || true)
  fi
  for pid in /proc/[0-9]*; do
    p=${pid#/proc/}
    is_own_pid "$p" && continue
    cwd=$(readlink "$pid/cwd" 2>/dev/null || true)
    root=$(readlink "$pid/root" 2>/dev/null || true)
    if [[ "$cwd" == "$TARGET" || "$cwd" == "$TARGET"/* ||
          "$root" == "$TARGET" || "$root" == "$TARGET"/* ]]; then
      log "killing pid $p (cwd/root inside $TARGET)"
      kill -KILL "$p" 2>/dev/null || true
    fi
  done
  # Processes that hold the disk in a private mount namespace (arch-chroot
  # runs under unshare). Only look at foreign namespaces: in our own
  # namespace every process lists /mnt in mountinfo once it is mounted, and
  # killing on that match takes down sshd, logind, and the login session
  # that runs this installer.
  for mi in /proc/[0-9]*/mountinfo; do
    [[ -e "$mi" ]] || continue
    p=${mi#/proc/}
    p=${p%/mountinfo}
    is_own_pid "$p" && continue
    ns=$(readlink "/proc/$p/ns/mnt" 2>/dev/null || true)
    [[ -n "$ns" && -n "$my_ns" && "$ns" != "$my_ns" ]] || continue
    if grep -Eq "/dev/${base}(p?[0-9]+)?( |$|\\[)" "$mi" 2>/dev/null || \
       grep -Eq " ${TARGET}(/| )" "$mi" 2>/dev/null; then
      log "killing pid $p (holds $disk in another mount namespace)"
      kill -KILL "$p" 2>/dev/null || true
    fi
  done
}

# Drop mounts, swap, LUKS, LVM, md, and the kernel's leftover btrfs
# registration so mkfs.btrfs can open the root partition exclusively.
release_disk() {
  local disk=$1
  local tries=0 name type mp vg pv
  swapoff -a || true
  if [[ -e "/dev/mapper/$CRYPT_NAME" ]]; then
    cryptsetup close "$CRYPT_NAME" || true
  fi
  while ((tries < 8)); do
    tries=$((tries + 1))
    kill_disk_users "$disk"
    sleep 1
    while read -r mp; do
      [[ -n "$mp" && "$mp" != / ]] || continue
      if ((tries < 6)); then
        umount -R "$mp" 2>/dev/null || true
      else
        umount -R "$mp" 2>/dev/null || umount -l "$mp" 2>/dev/null || true
      fi
    done < <(lsblk -lnr -o MOUNTPOINT "$disk" 2>/dev/null | awk 'NF && $1 != "/"' | sort -r)
    if mountpoint -q "$TARGET"; then
      umount -R "$TARGET" 2>/dev/null || true
    fi
    local -a rows=()
    mapfile -t rows < <(lsblk -lnp -o NAME,TYPE "$disk" 2>/dev/null || true)
    local i
    for ((i = ${#rows[@]} - 1; i >= 0; i--)); do
      read -r name type <<<"${rows[i]}"
      [[ -n "$name" ]] || continue
      case "$type" in
        crypt)
          cryptsetup close "$name" 2>/dev/null || \
            cryptsetup close "${name##*/}" 2>/dev/null || true
          ;;
        lvm|dm)
          if command -v lvchange >/dev/null 2>&1; then
            lvchange -an "$name" 2>/dev/null || true
          fi
          ;;
      esac
    done
    if command -v pvs >/dev/null 2>&1 && command -v vgchange >/dev/null 2>&1; then
      while read -r pv vg; do
        [[ -n "$vg" ]] || continue
        if is_on_disk "$disk" "$pv"; then
          vgchange -an "$vg" 2>/dev/null || true
        fi
      done < <(pvs --noheadings -o pv_name,vg_name 2>/dev/null || true)
    fi
    if command -v mdadm >/dev/null 2>&1; then
      mdadm --stop --scan 2>/dev/null || true
    fi
    forget_btrfs_on_disk "$disk"
    if ! lsblk -lnr -o MOUNTPOINT "$disk" 2>/dev/null | awk 'NF && $1 != "/" { found=1 } END { exit !found }'; then
      return 0
    fi
  done
  warn "could not fully release $disk; continuing"
  log_disk_holders "$disk"
}

wipe_signatures() {
  local disk=$1 name type
  while read -r name type; do
    [[ "$type" == part ]] || continue
    wipefs -af "$name" 2>/dev/null || true
  done < <(lsblk -lnp -o NAME,TYPE "$disk" 2>/dev/null || true)
  wipefs -af "$disk" || warn "wipefs on $disk failed; sgdisk will zap the table anyway"
}

# Drop the in-kernel partition table after zap so sgdisk is not writing a
# "new" GPT the kernel ignores (same 2G+rest layout as the previous run).
reread_partition_table() {
  local disk=$1 i
  for i in $(seq 1 8); do
    kill_disk_users "$disk"
    forget_btrfs_on_disk "$disk"
    partx -d "$disk" 2>/dev/null || true
    if blockdev --rereadpt "$disk" 2>/dev/null; then
      udevadm settle --timeout=15 2>/dev/null || true
      return 0
    fi
    log "kernel still using the old partition table on $disk (try $i)"
    sleep 1
  done
  warn "kernel did not drop the old partition table on $disk; continuing"
  return 0
}

# sgdisk already notifies the kernel. A full partprobe (BLKRRPART) while
# udev is opening the new nodes is a common "device busy" on sda2.
notify_kernel_parts() {
  udevadm settle --timeout=15 2>/dev/null || true
  setup_partitions
  forget_btrfs_on_disk "$DISK"
  if [[ -b "$BOOT_PART" && -b "$DATA_PART" ]]; then
    return 0
  fi
  partx -u "$DISK" 2>/dev/null || true
  udevadm settle --timeout=10 2>/dev/null || true
  setup_partitions
  if [[ -b "$BOOT_PART" && -b "$DATA_PART" ]]; then
    return 0
  fi
  partprobe "$DISK" 2>/dev/null || blockdev --rereadpt "$DISK" 2>/dev/null || true
  udevadm settle --timeout=10 2>/dev/null || true
  setup_partitions
}

wait_for_parts() {
  local i
  for i in $(seq 1 40); do
    setup_partitions
    if [[ -b "$BOOT_PART" && -b "$DATA_PART" ]]; then
      return 0
    fi
    sleep 0.25
    if ((i % 8 == 0)); then
      notify_kernel_parts
    fi
  done
  die "partitions did not appear after partitioning:
  EFI  $BOOT_PART
  root $DATA_PART
$(lsblk -o NAME,SIZE,TYPE "$DISK" 2>/dev/null || true)"
}

# Run mkfs / wipefs / cryptsetup with udev's helper queue stopped so blkid
# cannot hold the new partition (EBUSY) the instant it appears.
run_on_free_block() {
  local part=$1
  shift
  local i rc=1
  for i in 1 2 3 4 5 6 7 8; do
    kill_disk_users "$DISK"
    udevadm settle --timeout=10 2>/dev/null || true
    pause_udev_queue
    forget_btrfs_on_disk "$DISK"
    set +e
    "$@"
    rc=$?
    set -e
    resume_udev_queue
    if ((rc == 0)); then
      return 0
    fi
    log "attempt $i on $part exited $rc; retrying"
    log_disk_holders "$DISK" "$part"
    release_disk "$DISK"
    sleep 2
  done
  die "Could not use $part (last exit $rc). Often leftover btrfs from a previous run in this live session (mkfs uses O_EXCL), or udev/LUKS/LVM.
$(lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$DISK" 2>/dev/null || true)"
}

wipe_and_partition() {
  log "Unmounting stale targets and releasing $DISK"
  if mountpoint -q "$TARGET"; then
    umount -R "$TARGET" || true
  fi
  release_disk "$DISK"

  log "Wiping $DISK"
  wipe_signatures "$DISK"
  sgdisk --zap-all "$DISK"
  reread_partition_table "$DISK"
  sgdisk -og "$DISK"

  local data_type=8300
  local data_name=root
  if ((ENCRYPT)); then
    data_type=8309
    data_name=cryptroot
  fi

  if [[ -n "$BIOS_N" ]]; then
    log "Creating BIOS boot + EFI + root partitions"
    sgdisk -n "${BIOS_N}:0:+1M" -t "${BIOS_N}:ef02" -c "${BIOS_N}:BIOS" "$DISK"
    sgdisk -n "${BOOT_N}:0:+${ESP_SIZE}" -t "${BOOT_N}:ef00" -c "${BOOT_N}:EFI" "$DISK"
    sgdisk -n "${DATA_N}:0:0" -t "${DATA_N}:${data_type}" -c "${DATA_N}:${data_name}" "$DISK"
  else
    log "Creating EFI + root partitions"
    sgdisk -n "${BOOT_N}:0:+${ESP_SIZE}" -t "${BOOT_N}:ef00" -c "${BOOT_N}:EFI" "$DISK"
    sgdisk -n "${DATA_N}:0:0" -t "${DATA_N}:${data_type}" -c "${DATA_N}:${data_name}" "$DISK"
  fi

  sgdisk -p "$DISK"
  notify_kernel_parts
  wait_for_parts
  [[ -b "$BOOT_PART" ]] || die "EFI partition missing: $BOOT_PART"
  [[ -b "$DATA_PART" ]] || die "Root partition missing: $DATA_PART"
}

setup_storage() {
  log "Formatting EFI $BOOT_PART"
  run_on_free_block "$BOOT_PART" mkfs.fat -F32 -n EFI "$BOOT_PART"
  # udisks/udev often mounts the new EFI filesystem; that holds the disk
  # and is a common "busy" on the root partition next.
  umount "$BOOT_PART" 2>/dev/null || true
  while read -r mp; do
    [[ -n "$mp" ]] || continue
    umount "$mp" 2>/dev/null || umount -l "$mp" 2>/dev/null || true
  done < <(lsblk -lnr -o MOUNTPOINT "$BOOT_PART" 2>/dev/null || true)

  local mapper
  if ((ENCRYPT)); then
    log "Creating LUKS on $DATA_PART"
    run_on_free_block "$DATA_PART" wipefs -af "$DATA_PART"
    run_on_free_block "$DATA_PART" \
      bash -c 'printf "%s" "$1" | cryptsetup luksFormat --batch-mode -q --type luks2 --iter-time 2000 --key-file=- "$2"' \
      _ "$LUKS_PASSWORD" "$DATA_PART"
    printf '%s' "$LUKS_PASSWORD" | cryptsetup open -q --key-file=- "$DATA_PART" "$CRYPT_NAME"
    mapper="/dev/mapper/$CRYPT_NAME"
  else
    log "Formatting btrfs on $DATA_PART (no LUKS)"
    run_on_free_block "$DATA_PART" wipefs -af "$DATA_PART"
    mapper="$DATA_PART"
  fi

  log "Creating btrfs on $mapper"
  run_on_free_block "$mapper" mkfs.btrfs -f -L tsos "$mapper"
  # -t matters: without it the kernel probes ext4 first and prints
  # "VFS: Can't find ext4 filesystem" straight to the console, over the gauge.
  mount -t btrfs "$mapper" "$TARGET"
  btrfs subvolume create "$TARGET/@"
  btrfs subvolume create "$TARGET/@home"
  btrfs subvolume create "$TARGET/@log"
  btrfs subvolume create "$TARGET/@pkg"
  btrfs subvolume create "$TARGET/@snapshots"
  umount "$TARGET"

  local opts
  opts=$(btrfs_opts)
  log "Mounting subvolumes ($opts)"
  mount -t btrfs -o "${opts},subvol=@" "$mapper" "$TARGET"
  mkdir -p "$TARGET"/{boot,home,var/log,var/cache/pacman/pkg,.snapshots}
  mount -t btrfs -o "${opts},subvol=@home" "$mapper" "$TARGET/home"
  mount -t btrfs -o "${opts},subvol=@log" "$mapper" "$TARGET/var/log"
  mount -t btrfs -o "${opts},subvol=@pkg" "$mapper" "$TARGET/var/cache/pacman/pkg"
  mount -t btrfs -o "${opts},subvol=@snapshots" "$mapper" "$TARGET/.snapshots"
  mount -t vfat "$BOOT_PART" "$TARGET/boot"
}

bind_offline_payload_into_target() {
  [[ -n "$TSOS_PAYLOAD_ROOT" ]] || return 0
  mkdir -p "$TARGET$TSOS_OFFLINE_CHROOT"
  if ! mountpoint -q "$TARGET$TSOS_OFFLINE_CHROOT"; then
    log "Binding ISO payload into the new system"
    mount --bind "$TSOS_PAYLOAD_ROOT" "$TARGET$TSOS_OFFLINE_CHROOT"
    mount -o remount,bind,ro "$TARGET$TSOS_OFFLINE_CHROOT" 2>/dev/null || true
  fi
}

enable_target_offline_repo() {
  [[ -n "$TSOS_OFFLINE_ROOT" ]] || return 0
  local conf="$TARGET/etc/pacman.conf"
  grep -q '^# BEGIN TSOS OFFLINE$' "$conf" 2>/dev/null && return 0
  local tmp
  tmp=$(mktemp)
  awk '
    /^\[[^]]+\]/ && $0 != "[options]" && !inserted {
      print "# BEGIN TSOS OFFLINE"
      print "[tsos]"
      print "SigLevel = Optional TrustAll"
      print "Server = file:///opt/tsos/pacman"
      print "# END TSOS OFFLINE"
      print ""
      inserted=1
    }
    { print }
  ' "$conf" >"$tmp"
  cat "$tmp" >"$conf"
  rm -f "$tmp"
}

install_base() {
  sync_live_pacman

  local ucode nvidia
  ucode=$(cpu_ucode_pkg || true)
  nvidia=$(nvidia_pkg) || die "NVIDIA kernel package not in the repos (tried nvidia-open, then nvidia). Enable the extra repository and check that pacman -Sy succeeded."
  log "NVIDIA kernel package: $nvidia"

  local packages=(
    base base-devel linux linux-firmware linux-headers
    btrfs-progs cryptsetup
    networkmanager iwd wireless-regdb
    sudo git curl wget
    iproute2 inetutils   # ip + hostname for the login MOTD
    limine
    vim nano man-db
    pipewire pipewire-pulse pipewire-alsa wireplumber
    nvidia-utils
    docker
    openssh
  )
  [[ -n "$ucode" ]] && packages+=("$ucode")
  packages+=("$nvidia")
  if is_uefi; then
    packages+=(efibootmgr)
  fi

  log "Installing Arch packages"
  if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    pacstrap -C "$(offline_pacman_config)" -K "$TARGET" --noconfirm "${packages[@]}"
  else
    pacstrap -K "$TARGET" --noconfirm "${packages[@]}"
  fi
  genfstab -U "$TARGET" >>"$TARGET/etc/fstab"
  enable_target_offline_repo
}

write_chroot_files() {
  local luks_uuid="" root_uuid=""
  if ((ENCRYPT)); then
    luks_uuid=$(blkid -s UUID -o value "$DATA_PART")
    [[ -n "$luks_uuid" ]] || die "could not read LUKS UUID from $DATA_PART"
    root_uuid=""
  else
    root_uuid=$(blkid -s UUID -o value "$DATA_PART")
    [[ -n "$root_uuid" ]] || die "could not read filesystem UUID from $DATA_PART"
  fi

  install -d "$TARGET/root"

  {
    printf 'TARGET_HOSTNAME=%q\n' "$TARGET_HOSTNAME"
    printf 'TARGET_USER=%q\n' "$TARGET_USER"
    printf 'TIMEZONE=%q\n' "$TIMEZONE"
    printf 'LOCALE=%q\n' "$LOCALE"
    printf 'KEYMAP=%q\n' "$KEYMAP"
    printf 'DISK=%q\n' "$DISK"
    printf 'BOOT_N=%q\n' "$BOOT_N"
    printf 'CRYPT_NAME=%q\n' "$CRYPT_NAME"
    printf 'ENCRYPT=%q\n' "$ENCRYPT"
    printf 'LUKS_UUID=%q\n' "$luks_uuid"
    printf 'ROOT_UUID=%q\n' "$root_uuid"
    printf 'UEFI=%q\n' "$(is_uefi && echo 1 || echo 0)"
    printf 'USER_PASSWORD=%q\n' "$USER_PASSWORD"
    printf 'ROOT_PASSWORD=%q\n' "$ROOT_PASSWORD"
    printf 'OMARCHY_MODE=%q\n' "$OMARCHY_MODE"
    printf 'OMARCHY_USER_NAME=%q\n' "$OMARCHY_USER_NAME"
    printf 'OMARCHY_USER_EMAIL=%q\n' "$OMARCHY_USER_EMAIL"
  } >"$TARGET/root/install-vars.sh"

  cat >"$TARGET/root/configure-arch.sh" <<'CHROOT'
#!/usr/bin/env bash
set -euo pipefail
source /root/install-vars.sh

echo "$TARGET_HOSTNAME" >/etc/hostname
cat >/etc/hosts <<EOF
127.0.0.1   localhost
::1         localhost
127.0.1.1   ${TARGET_HOSTNAME}.localdomain ${TARGET_HOSTNAME}
EOF

ln -sf "/usr/share/zoneinfo/${TIMEZONE}" /etc/localtime
hwclock --systohc

loc_esc=$(printf '%s' "$LOCALE" | sed 's/[.[\*^$\\]/\\&/g')
sed -i "s/^#${loc_esc}/${LOCALE}/" /etc/locale.gen
sed -i 's/^#en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
locale-gen
printf 'LANG=%s\n' "$LOCALE" >/etc/locale.conf
printf 'KEYMAP=%s\n' "$KEYMAP" >/etc/vconsole.conf

echo "root:${ROOT_PASSWORD}" | chpasswd
extra_groups=wheel
if getent group docker >/dev/null 2>&1; then
  extra_groups+=,docker
fi
for g in video input tty; do
  if getent group "$g" >/dev/null 2>&1; then
    extra_groups+=",$g"
  fi
done
if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
  useradd -m -G "$extra_groups" -s /bin/bash "$TARGET_USER"
else
  usermod -aG "$extra_groups" "$TARGET_USER"
fi
echo "${TARGET_USER}:${USER_PASSWORD}" | chpasswd

install -d -m 0750 /etc/sudoers.d
# Named 10-wheel so it sorts before the NOPASSWD drop-in.
# A file named "wheel" sorts last and cancels NOPASSWD (sudo last-match wins).
printf '%s\n' '%wheel ALL=(ALL:ALL) ALL' >/etc/sudoers.d/10-wheel
chmod 0440 /etc/sudoers.d/10-wheel
# Passwordless sudo for the stack user (Settings systemd, tsctl, updates).
printf 'Defaults:%s !use_pty,!requiretty,!pam_session\n' "$TARGET_USER" >/etc/sudoers.d/zz-tsos-nopasswd
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$TARGET_USER" >>/etc/sudoers.d/zz-tsos-nopasswd
chmod 0440 /etc/sudoers.d/zz-tsos-nopasswd
rm -f /etc/sudoers.d/zz-tsos-firstboot /etc/sudoers.d/99-tsos-firstboot
if [[ "$OMARCHY_MODE" != "skip" ]]; then
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$TARGET_USER" >/etc/sudoers.d/99-omarchy-installer
  chmod 0440 /etc/sudoers.d/99-omarchy-installer
fi

systemctl enable NetworkManager
systemctl enable NetworkManager-wait-online.service || true
systemctl enable docker.service || true
systemctl enable sshd.service
install -d -m 0755 /var/lib/systemd/linger
touch "/var/lib/systemd/linger/${TARGET_USER}"
loginctl enable-linger "$TARGET_USER" || true

if [[ "$ENCRYPT" == "1" ]]; then
  if grep -q '^HOOKS=' /etc/mkinitcpio.conf; then
    sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block encrypt filesystems fsck)/' /etc/mkinitcpio.conf
  else
    printf '%s\n' 'HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block encrypt filesystems fsck)' >>/etc/mkinitcpio.conf
  fi
else
  if grep -q '^HOOKS=' /etc/mkinitcpio.conf; then
    sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block filesystems fsck)/' /etc/mkinitcpio.conf
  else
    printf '%s\n' 'HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block filesystems fsck)' >>/etc/mkinitcpio.conf
  fi
fi
if grep -q '^MODULES=' /etc/mkinitcpio.conf; then
  sed -i 's/^MODULES=.*/MODULES=(btrfs)/' /etc/mkinitcpio.conf
else
  printf '%s\n' 'MODULES=(btrfs)' >>/etc/mkinitcpio.conf
fi
mkinitcpio -P

# nvidia-drm.modeset=1 is required for the TTY KMS screensaver.
if [[ "$ENCRYPT" == "1" ]]; then
  CMDLINE="cryptdevice=UUID=${LUKS_UUID}:${CRYPT_NAME} root=/dev/mapper/${CRYPT_NAME} rw rootfstype=btrfs rootflags=subvol=@ nvidia-drm.modeset=1"
else
  CMDLINE="root=UUID=${ROOT_UUID} rw rootfstype=btrfs rootflags=subvol=@ nvidia-drm.modeset=1"
fi
install -d /etc/modprobe.d
printf '%s\n' 'options nvidia_drm modeset=1' >/etc/modprobe.d/nvidia-drm.conf

write_limine_conf() {
  local dest=$1
  install -d "$(dirname "$dest")"
  cat >"$dest" <<EOF
timeout: 5

/Arch Linux
    protocol: linux
    path: boot():/vmlinuz-linux
    cmdline: ${CMDLINE}
    module_path: boot():/initramfs-linux.img

/Arch Linux (fallback)
    protocol: linux
    path: boot():/vmlinuz-linux
    cmdline: ${CMDLINE}
    module_path: boot():/initramfs-linux-fallback.img
EOF
}

install -d /boot/EFI/arch-limine /boot/EFI/BOOT /boot/limine /etc/pacman.d/hooks

if [[ "$UEFI" == "1" ]]; then
  cp /usr/share/limine/BOOTX64.EFI /boot/EFI/arch-limine/BOOTX64.EFI
  cp /usr/share/limine/BOOTX64.EFI /boot/EFI/BOOT/BOOTX64.EFI
  if [[ -f /usr/share/limine/BOOTIA32.EFI ]]; then
    cp /usr/share/limine/BOOTIA32.EFI /boot/EFI/arch-limine/BOOTIA32.EFI
    cp /usr/share/limine/BOOTIA32.EFI /boot/EFI/BOOT/BOOTIA32.EFI
  fi
  write_limine_conf /boot/EFI/arch-limine/limine.conf
  write_limine_conf /boot/EFI/BOOT/limine.conf
  write_limine_conf /boot/limine.conf

  if command -v efibootmgr >/dev/null; then
    efibootmgr --create \
      --disk "$DISK" \
      --part "$BOOT_N" \
      --label "Arch Linux Limine Bootloader" \
      --loader '\EFI\arch-limine\BOOTX64.EFI' \
      --unicode || true
  fi

  cat >/etc/pacman.d/hooks/99-limine.hook <<'HOOK'
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = limine

[Action]
Description = Deploying Limine after upgrade...
When = PostTransaction
Exec = /bin/sh -c "/usr/bin/cp /usr/share/limine/BOOTX64.EFI /boot/EFI/arch-limine/ && /usr/bin/cp /usr/share/limine/BOOTX64.EFI /boot/EFI/BOOT/"
HOOK
else
  cp /usr/share/limine/limine-bios.sys /boot/limine/limine-bios.sys
  limine bios-install "$DISK"
  write_limine_conf /boot/limine/limine.conf
  write_limine_conf /boot/limine.conf

  cat >/etc/pacman.d/hooks/99-limine.hook <<HOOK
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = limine

[Action]
Description = Deploying Limine after upgrade...
When = PostTransaction
Exec = /bin/sh -c "/usr/bin/limine bios-install ${DISK} && /usr/bin/cp /usr/share/limine/limine-bios.sys /boot/limine/"
HOOK
fi

install -d -m 0755 /usr/local/bin
cat >/usr/local/bin/install-omarchy <<'HELPER'
#!/usr/bin/env bash
set -euo pipefail
if ((EUID == 0)); then
  echo "Run this as your regular user, not root:" >&2
  echo "  install-omarchy" >&2
  exit 1
fi
export OMARCHY_ONLINE_INSTALL=true
if [[ -n "${OMARCHY_USER_NAME:-}" ]]; then
  export OMARCHY_USER_NAME
fi
if [[ -n "${OMARCHY_USER_EMAIL:-}" ]]; then
  export OMARCHY_USER_EMAIL
fi
echo "Starting official Omarchy installer..."
exec bash -c 'curl -fsSL https://omarchy.org/install | bash'
HELPER
chmod 0755 /usr/local/bin/install-omarchy

if [[ -n "$OMARCHY_USER_NAME" || -n "$OMARCHY_USER_EMAIL" ]]; then
  install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 0755 "/home/${TARGET_USER}"
  {
    if [[ -n "$OMARCHY_USER_NAME" ]]; then
      printf 'export OMARCHY_USER_NAME=%q\n' "$OMARCHY_USER_NAME"
    fi
    if [[ -n "$OMARCHY_USER_EMAIL" ]]; then
      printf 'export OMARCHY_USER_EMAIL=%q\n' "$OMARCHY_USER_EMAIL"
    fi
  } >"/home/${TARGET_USER}/.omarchy-identity"
  chown "${TARGET_USER}:${TARGET_USER}" "/home/${TARGET_USER}/.omarchy-identity"
  chmod 0600 "/home/${TARGET_USER}/.omarchy-identity"
  touch "/home/${TARGET_USER}/.bash_profile"
  if ! grep -q omarchy-identity "/home/${TARGET_USER}/.bash_profile"; then
    printf '\n[[ -f ~/.omarchy-identity ]] && source ~/.omarchy-identity\n' >>"/home/${TARGET_USER}/.bash_profile"
  fi
  chown "${TARGET_USER}:${TARGET_USER}" "/home/${TARGET_USER}/.bash_profile"
fi

# Drop secrets from the chroot copy of the vars file.
sed -i '/_PASSWORD=/d' /root/install-vars.sh
CHROOT
  chmod 0755 "$TARGET/root/configure-arch.sh"
}

write_tabby_bootstrap() {
  local stack_home="/home/${TARGET_USER}/tabbyapi-stack"
  local conf_dir="$TARGET/etc/tsos"
  install -d -m 0755 "$conf_dir" "$TARGET/usr/local/bin" "$TARGET/etc/profile.d" \
    "$TARGET/var/lib/tsos" "$TARGET/etc/systemd/system"

  {
    printf 'TARGET_HOSTNAME=%q\n' "$TARGET_HOSTNAME"
    printf 'TARGET_USER=%q\n' "$TARGET_USER"
    printf 'ENCRYPT=%q\n' "$ENCRYPT"
    printf 'OMARCHY_MODE=%q\n' "$OMARCHY_MODE"
    printf 'TABBY_REPO=%q\n' "$TABBY_REPO"
    printf 'TABBY_MODELS=%q\n' "$TABBY_MODELS"
    printf 'TABBY_NETWORK_HOST=%q\n' "$TABBY_NETWORK_HOST"
    printf 'TABBY_NETWORK_PORT=%q\n' "$TABBY_NETWORK_PORT"
    printf 'TABBY_CACHE=%q\n' "$TABBY_CACHE"
    printf 'TABBY_PUBLIC_BASE=%q\n' "$TABBY_PUBLIC_BASE"
    printf 'TABBY_SSH_REMOTE=%q\n' "$TABBY_SSH_REMOTE"
    printf 'TABBY_SSH_FORWARD=%q\n' "$TABBY_SSH_FORWARD"
    printf 'TABBY_SSH_KEY=%q\n' "$TABBY_SSH_KEY"
    printf 'COMFYUI_URL=%q\n' "$COMFYUI_URL"
    printf 'TABBY_INSTALL_ROOT=%q\n' "$stack_home"
  } >"$conf_dir/install.conf"
  chmod 0644 "$conf_dir/install.conf"
  if [[ -n "${HF_TOKEN:-}" ]]; then
    printf 'HF_TOKEN=%q\n' "$HF_TOKEN" >"$conf_dir/secrets.env"
    chmod 0600 "$conf_dir/secrets.env"
  fi

  cat >"$TARGET/etc/motd" <<EOF

tabbyapi-stack OS — log in as ${TARGET_USER} for API URLs and install status.

EOF

  # Fallback if the cloned tree has no tsos-motd. Keep in sync with
  # tabbyAPI/deploy/arch/tsos-motd
  cat >"$TARGET/usr/local/bin/tsos-motd" <<'MOTD'
#!/usr/bin/env bash
# Printed on interactive login. Safe to run any time.
set -euo pipefail

CONF=/etc/tsos/install.conf
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
fi

TARGET_USER="${TARGET_USER:-$USER}"
TABBY_INSTALL_ROOT="${TABBY_INSTALL_ROOT:-$HOME/tabbyapi-stack}"
TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
ENCRYPT="${ENCRYPT:-1}"
OMARCHY_MODE="${OMARCHY_MODE:-skip}"
TABBY_MODELS="${TABBY_MODELS:-core}"
TABBY_PUBLIC_BASE="${TABBY_PUBLIC_BASE:-}"

ENV_FILE="${TABBY_INSTALL_ROOT}/tabbyAPI/deploy/arch/tabby.env"
CONFIG_FILE="${TABBY_INSTALL_ROOT}/tabbyAPI/config.yml"
TOKENS_FILE="${TABBY_INSTALL_ROOT}/tabbyAPI/api_tokens.yml"

STATUS_FILE="/home/${TARGET_USER}/.config/tabbyapi-stack/tsos-firstboot.status"
DONE_FILE=/var/lib/tsos/tabby-firstboot.done
RESUME_FILE="/home/${TARGET_USER}/.config/tabbyapi-stack/install-resume.env"
LOG_FILE="${TABBY_INSTALL_ROOT}/tabby-install.log"
FIRSTBOOT_LOG=/var/log/tsos-firstboot.log

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
  C0=$'\033[0m'
  C_HDR=$'\033[1;36m'
  C_KEY=$'\033[1;37m'
  C_DIM=$'\033[2m'
  C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'
else
  C0= C_HDR= C_KEY= C_DIM= C_GREEN= C_YELLOW= C_RED=
fi

line() {
  printf '  %s%-12s%s %s\n' "$C_KEY" "$1" "$C0" "$2"
}

note() {
  printf '  %s%-12s%s %s%s%s\n' "$C_KEY" "" "$C0" "$C_DIM" "$1" "$C0"
}

rule() {
  printf '%s================================================================%s\n' "$C_HDR" "$C0"
}

paint() {
  printf '%s%s%s' "$1" "$2" "$C0"
}

host_name() {
  local h=""
  if [[ -r /etc/hostname ]]; then
    h=$(tr -d ' \t\r\n' </etc/hostname)
  fi
  if [[ -z "$h" ]] && command -v hostnamectl >/dev/null 2>&1; then
    h=$(hostnamectl --static 2>/dev/null || true)
  fi
  if [[ -z "$h" ]]; then
    h=$(uname -n 2>/dev/null || true)
  fi
  if [[ -z "$h" ]] && command -v hostname >/dev/null 2>&1; then
    h=$(hostname 2>/dev/null || true)
  fi
  if [[ -z "$h" && -r /proc/sys/kernel/hostname ]]; then
    h=$(tr -d ' \t\r\n' </proc/sys/kernel/hostname)
  fi
  if [[ -z "$h" && -n "${TARGET_HOSTNAME:-}" ]]; then
    h="$TARGET_HOSTNAME"
  fi
  printf '%s' "${h:-unknown}"
}

lan_ips() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global 2>/dev/null | awk '{
      gsub(/\/.*/, "", $4)
      if ($4 != "") { if (n++) printf " "; printf "%s", $4 }
    }'
  elif command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | awk '{
      for (i = 1; i <= NF; i++) {
        if ($i !~ /^127\./ && $i != "::1") {
          if (n++) printf " "
          printf "%s", $i
        }
      }
    }'
  fi
}

tabby_active_state() {
  local state="" uid
  state=$(systemctl --user show -p ActiveState --value tabbyapi 2>/dev/null || true)
  if [[ -z "$state" || "$state" == "inactive" ]]; then
    uid=$(id -u "$TARGET_USER" 2>/dev/null || true)
    if [[ -n "$uid" && -d "/run/user/$uid" ]]; then
      state=$(XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user show -p ActiveState --value tabbyapi 2>/dev/null || true)
    fi
  fi
  printf '%s' "${state:-}"
}

health_line() {
  local url="http://127.0.0.1:${TABBY_NETWORK_PORT}/health"
  local body state up
  # Do not use curl -f: a 503 still means the API is up and loading.
  body=$(curl -sS --connect-timeout 2 --max-time 3 "$url" 2>/dev/null || true)
  if [[ "$body" == *'"status":"healthy"'* || "$body" == *'"status": "healthy"'* ]]; then
    paint "$C_GREEN" "healthy"
    return 0
  fi
  if [[ -n "$body" ]]; then
    paint "$C_YELLOW" "initializing"
    return 0
  fi
  state=$(tabby_active_state)
  case "$state" in
    active|activating|reloading)
      paint "$C_YELLOW" "initializing"
      return 0
      ;;
    failed)
      paint "$C_RED" "failed"
      return 0
      ;;
  esac
  up=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 9999)
  if [[ -f "/var/lib/systemd/linger/${TARGET_USER}" ]] \
     && [[ "$up" =~ ^[0-9]+$ ]] && ((up < 180)); then
    paint "$C_YELLOW" "initializing"
    return 0
  fi
  paint "$C_RED" "not listening"
}

install_status() {
  if [[ -f "$DONE_FILE" ]]; then
    paint "$C_GREEN" "finished"
    return 0
  fi
  if [[ -f "$RESUME_FILE" ]]; then
    paint "$C_YELLOW" "waiting for NVIDIA reboot resume"
    return 0
  fi
  if [[ -f "$STATUS_FILE" ]]; then
    paint "$C_YELLOW" "$(tr -d '\n' <"$STATUS_FILE")"
    return 0
  fi
  paint "$C_YELLOW" "not finished on the live ISO"
}

enc_label() {
  if [[ "$ENCRYPT" == "1" || "$ENCRYPT" == "yes" ]]; then
    printf 'LUKS + btrfs'
  else
    printf 'btrfs (unencrypted)'
  fi
}

omarchy_label() {
  case "$OMARCHY_MODE" in
    now) printf 'installed during setup (or run: install-omarchy)' ;;
    *) printf 'not selected — optional later: install-omarchy' ;;
  esac
}

listen_urls() {
  local host="$TABBY_NETWORK_HOST"
  local port="$TABBY_NETWORK_PORT"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    local ips
    ips=$(lan_ips)
    if [[ -n "$ips" ]]; then
      printf 'http://%s:%s  (also LAN: %s)' "127.0.0.1" "$port" "$ips"
    else
      printf 'http://127.0.0.1:%s  (listening on all interfaces)' "$port"
    fi
  else
    printf 'http://%s:%s' "$host" "$port"
  fi
}

api_url=$(listen_urls)
if [[ -n "$TABBY_PUBLIC_BASE" ]]; then
  public_line="$TABBY_PUBLIC_BASE"
else
  public_line="(none — local / LAN only)"
fi

printf '\n'
rule
printf '  %stabbyapi-stack OS%s\n' "$C_HDR" "$C0"
rule
printf '\n'

line "Host:" "$(host_name)"
line "User:" "${TARGET_USER}"
line "Disk:" "$(enc_label)"
line "Desktop:" "$(omarchy_label)"
line "Models:" "${TABBY_MODELS}"
printf '\n'
line "Install:" "${TABBY_INSTALL_ROOT}"
line "API:" "${api_url}"
line "UI:" "http://127.0.0.1:${TABBY_NETWORK_PORT}/v1/ui"
line "Health:" "curl -sS http://127.0.0.1:${TABBY_NETWORK_PORT}/health"
line "Editor:" "http://<this-host>:${TABBY_NETWORK_PORT}/v1"
line "Model name:" "gpt-4o   (leave it — compatibility label only)"
line "Public:" "${public_line}"
printf '\n'
line "Password:" "Linux login for ${TARGET_USER} — that is the UI / API key"
note "change with: passwd"
line "Env:" "${ENV_FILE}"
line "Config:" "${CONFIG_FILE}"
note "Settings in /v1/ui (admin) edits the same files"
line "Extra keys:" "${TOKENS_FILE}"
printf '\n'
line "Status:" "$(install_status)"
api_health=$(health_line)
line "API health:" "$api_health"
if [[ "$api_health" == *initializing* ]]; then
  note "loading the model — first boot compiles CUDA kernels (~2–3 min)"
fi
line "Unit:" "systemctl --user status tabbyapi"
line "Logs:" "journalctl --user -u tabbyapi -f"
line "tsctl:" "tsctl   (stack settings; same as Settings in /v1/ui)"
line "Update:" "bash ${TABBY_INSTALL_ROOT}/update.sh"
line "How-to:" "${TABBY_INSTALL_ROOT}/tabbyAPI/HOW-TO-ARCH.txt"
line "MOTD:" "tsos-motd"
printf '\n'

if [[ "$ENCRYPT" == "1" || "$ENCRYPT" == "yes" ]]; then
  printf '  %sDisk unlock:%s enter the LUKS password at the Limine/unlock prompt.\n\n' "$C_YELLOW" "$C0"
fi

if [[ ! -f "$DONE_FILE" ]]; then
  printf '  %stabbyapi-stack did not finish on the live ISO. Re-run tsos-installer.sh%s\n' "$C_YELLOW" "$C0"
  printf '  %sfrom the Arch ISO — install.sh is not run after reboot.%s\n' "$C_YELLOW" "$C0"
  printf '  Log:    %s\n\n' "${LOG_FILE}"
fi

rule
printf '\n'
MOTD
  chmod 0755 "$TARGET/usr/local/bin/tsos-motd"

  cat >"$TARGET/etc/profile.d/tsos-motd.sh" <<'PROFILE'
# tabbyapi-stack login banner
if [[ -t 1 && $- == *i* ]] && command -v tsos-motd >/dev/null 2>&1; then
  tsos-motd
fi
PROFILE
  chmod 0644 "$TARGET/etc/profile.d/tsos-motd.sh"

  log "Cloning tabbyapi-stack for the chroot install"
  local stack_bundle=""
  for stack_bundle in \
    "${TSOS_PAYLOAD_ROOT:-}/bundles/tabbyapi-stack.bundle" \
    "${TSOS_OFFLINE_ROOT:-}/bundles/tabbyapi-stack.bundle" \
    /opt/tsos/bundles/tabbyapi-stack.bundle
  do
    [[ -f "$stack_bundle" ]] && break
    stack_bundle=""
  done
  if [[ -n "$stack_bundle" ]]; then
    if ! arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- \
      git clone "$TSOS_OFFLINE_CHROOT/bundles/tabbyapi-stack.bundle" "$stack_home"; then
      die "local tabbyapi-stack bundle could not be cloned"
    fi
    arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- \
      git -C "$stack_home" remote set-url origin "$TABBY_REPO"
  elif ! arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- \
    git clone "$TABBY_REPO" "$stack_home"; then
    die "git clone failed. Check network, then re-run. Repo: $TABBY_REPO"
  fi
  overlay_local_tabby_sources "$TARGET$stack_home"
  install_tsos_motd_from_tree
}

# curl | bash clones GitHub. A local tree (this script's directory, or
# TABBY_LOCAL_SRC) is copied over that clone so ISO testing picks up
# install.sh fixes that are not on origin yet.
overlay_local_tabby_sources() {
  local dest="$1"
  local src=""
  if [[ -n "$TABBY_LOCAL_SRC" ]]; then
    [[ -d "$TABBY_LOCAL_SRC" ]] || die "TABBY_LOCAL_SRC is not a directory: $TABBY_LOCAL_SRC"
    src="$(cd "$TABBY_LOCAL_SRC" && pwd)"
  else
    local script="${BASH_SOURCE[0]:-}"
    case "$script" in
      "" | /dev/fd/* | /proc/self/fd/* | -) return 0 ;;
    esac
    [[ "$script" == /* ]] || script="$PWD/$script"
    src="$(cd "$(dirname "$script")" && pwd)"
  fi
  [[ -f "$src/install.sh" && -f "$src/tabbyAPI/pyproject.toml" ]] || {
    [[ -n "$TABBY_LOCAL_SRC" ]] && die "TABBY_LOCAL_SRC is not a tabbyapi-stack tree: $src"
    return 0
  }
  [[ "$src" == "$dest" ]] && return 0
  log "Overlaying local tabbyapi-stack from $src"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude '.git/' \
      --exclude 'tabbyAPI/venv/' \
      --exclude 'tabbyAPI/models/' \
      --exclude 'ComfyUI/' \
      --exclude 'tabby-install.log' \
      --exclude '.tabby-update-backup/' \
      "$src/" "$dest/"
  else
    local name
    for name in install.sh update.sh uninstall.sh tsos-installer.sh AGENTS.md README.md; do
      [[ -e "$src/$name" ]] && cp -a "$src/$name" "$dest/$name"
    done
    mkdir -p "$dest/tabbyAPI"
    cp -a "$src/tabbyAPI/." "$dest/tabbyAPI/"
    rm -rf "$dest/tabbyAPI/venv" "$dest/tabbyAPI/models"
  fi
  chown_target_user_tree "/home/${TARGET_USER}/tabbyapi-stack"
}

# Root writing into /home/USER leaves root-owned files. install.sh runs as
# TARGET_USER and dies on the first append (tabby-install.log).
chown_target_user_tree() {
  local rel="$1"
  arch-chroot "$TARGET" /usr/bin/chown -R "${TARGET_USER}:${TARGET_USER}" "$rel" || true
}

ensure_target_user_file() {
  local host_path="$1"
  local rel="${host_path#"$TARGET"}"
  [[ "$rel" == /* ]] || rel="/$rel"
  local rel_dir
  rel_dir=$(dirname "$rel")
  arch-chroot "$TARGET" /usr/bin/install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 0755 "$rel_dir"
  if [[ ! -e "$host_path" ]]; then
    arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- touch "$rel"
  fi
  arch-chroot "$TARGET" /usr/bin/chown "${TARGET_USER}:${TARGET_USER}" "$rel"
  chmod 0644 "$host_path" || true
}

# After a failed chroot install.sh, pull origin/main then overlay a local
# tree so fixes that are not on the ISO copy get used.
refresh_tabbyapi_stack_in_target() {
  local stack_home="/home/${TARGET_USER}/tabbyapi-stack"
  [[ -d "$TARGET$stack_home" ]] || die "missing $stack_home on the new system"
  if [[ -d "$TARGET$stack_home/.git" && -z "$TSOS_OFFLINE_ROOT" ]]; then
    log "Updating tabbyapi-stack in the chroot from origin"
    arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- \
      git -C "$stack_home" fetch --prune origin || \
      warn "git fetch failed; using the tree already on disk"
    if ! arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- \
         git -C "$stack_home" merge --ff-only origin/main; then
      arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- \
        git -C "$stack_home" pull --ff-only || \
        warn "git pull failed; using the tree already on disk"
    fi
  elif [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
    log "Using tabbyapi-stack sources from the offline ISO"
  fi
  overlay_local_tabby_sources "$TARGET$stack_home"
  install_tsos_motd_from_tree
}

# Prefer the tree copy so update.sh / overlay can refresh the login banner.
install_tsos_motd_from_tree() {
  local src="$TARGET/home/${TARGET_USER}/tabbyapi-stack/tabbyAPI/deploy/arch/tsos-motd"
  [[ -f "$src" ]] || return 0
  install -m 0755 "$src" "$TARGET/usr/local/bin/tsos-motd"
}

# If the weights cache is under $TARGET (often /mnt/usb), mounting the new
# root there would hide it. Bind it aside before wipe/mount.
preserve_tabby_cache() {
  [[ -n "$TABBY_CACHE" ]] || return 0
  if [[ ! -d "$TABBY_CACHE" ]]; then
    warn "TABBY_CACHE is not a directory: $TABBY_CACHE — ignoring"
    TABBY_CACHE=""
    return 0
  fi
  local cache_abs
  cache_abs=$(cd "$TABBY_CACHE" && pwd)
  if [[ "$cache_abs" == "$TARGET" || "$cache_abs" == "$TARGET"/* ]]; then
    log "Moving weights cache off $TARGET so the new root can mount there"
    mkdir -p "$CACHE_STAGING"
    mount --bind "$cache_abs" "$CACHE_STAGING"
    TABBY_CACHE="$CACHE_STAGING"
  fi
}

bind_tabby_cache_into_target() {
  TABBY_CACHE_CHROOT=""
  [[ -n "$TABBY_CACHE" && -d "$TABBY_CACHE" ]] || return 0
  local cache_abs
  cache_abs=$(cd "$TABBY_CACHE" && pwd)
  if [[ "$cache_abs" == "$TARGET" || "$cache_abs" == "$TARGET"/* ]]; then
    TABBY_CACHE_CHROOT="${cache_abs#"$TARGET"}"
    [[ -n "$TABBY_CACHE_CHROOT" ]] || TABBY_CACHE_CHROOT="/"
    return 0
  fi
  if mountpoint -q "$TARGET$CACHE_CHROOT_PATH" 2>/dev/null; then
    TABBY_CACHE_CHROOT="$CACHE_CHROOT_PATH"
    return 0
  fi
  log "Binding weights cache into the new system at $CACHE_CHROOT_PATH"
  mkdir -p "$TARGET$CACHE_CHROOT_PATH"
  mount --bind "$cache_abs" "$TARGET$CACHE_CHROOT_PATH"
  TABBY_CACHE_CHROOT="$CACHE_CHROOT_PATH"
}

run_tabby_install_chroot() {
  local stack_home="/home/${TARGET_USER}/tabbyapi-stack"
  [[ -f "$TARGET$stack_home/install.sh" ]] || die "missing $stack_home/install.sh on the new system"

  bind_offline_payload_into_target
  enable_target_offline_repo
  bind_tabby_cache_into_target
  write_nopasswd_sudoers "$TARGET"

  log "Installing tabbyapi-stack in the new system (Python, venvs, model files)"
  log "This stays on the live ISO until it finishes. Full log: $stack_home/tabby-install.log"
  gauge_update 45 "Installing tabbyapi-stack"

  local saver_default=1
  if [[ "${OMARCHY_MODE:-skip}" == "now" ]]; then
    saver_default=0
  fi
  local -a run_env=(
    HOME="/home/${TARGET_USER}"
    USER="$TARGET_USER"
    LOGNAME="$TARGET_USER"
    TERM="${TERM:-linux}"
    TABBY_SKIP_NVIDIA_REBOOT=1
    TABBY_INSTALL_ROOT="$stack_home"
    TABBY_CACHE="${TABBY_CACHE_CHROOT:-}"
    TABBY_NONINTERACTIVE=1
    TABBY_NESTED_UI=1
    TABBY_INSTALL_VERBOSE=1
    PYTHONUNBUFFERED=1
    TABBY_ISO_CHROOT=1
    TSOS_OFFLINE_ROOT="$([[ -n "$TSOS_OFFLINE_ROOT" ]] && printf '%s' "$TSOS_OFFLINE_CHROOT")"
    TABBY_MODELS="${TABBY_MODELS:-core}"
    TABBY_NETWORK_HOST="${TABBY_NETWORK_HOST:-0.0.0.0}"
    TABBY_NETWORK_PORT="${TABBY_NETWORK_PORT:-5000}"
    TABBY_PUBLIC_BASE="${TABBY_PUBLIC_BASE:-}"
    TABBY_SSH_REMOTE="${TABBY_SSH_REMOTE:-}"
    TABBY_SSH_FORWARD="${TABBY_SSH_FORWARD:-}"
    TABBY_SSH_KEY="${TABBY_SSH_KEY:-}"
    COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
    TABBY_SAVER_ENABLED="${TABBY_SAVER_ENABLED:-$saver_default}"
    DISPLAY=
    WAYLAND_DISPLAY=
  )
  log "install.sh will use the settings from this UI (no second dialog)"
  if [[ -n "${HF_TOKEN:-}" ]]; then
    run_env+=(HF_TOKEN="$HF_TOKEN" HUGGING_FACE_HUB_TOKEN="$HF_TOKEN")
  fi

  local status=0
  local tabby_log="$TARGET$stack_home/tabby-install.log"
  chown_target_user_tree "$stack_home"
  ensure_target_user_file "$tabby_log"
  {
    echo "launching install.sh $(date -Iseconds)"
    echo "nested=1 verbose=1 (output stays in the installer dialog)"
  } >>"$tabby_log"
  chown_target_user_tree "$stack_home/tabby-install.log"
  # Tells the UI watcher to take the percentage from install.sh's own log.
  if [[ -n "${TSOS_GAUGE_DIR:-}" ]]; then
    printf '%s\n' "$tabby_log" >"$TSOS_GAUGE_DIR/nested"
  fi

  # 3>&- everywhere: nothing in this pipeline may hold the gauge FIFO open.
  # Output goes to install.sh's log and (via this shell's stdout) to the
  # tsos log the dialog box is reading. Never to /dev/tty.
  set +e
  {
    if command -v stdbuf >/dev/null 2>&1; then
      arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- env "${run_env[@]}" \
        bash "$stack_home/install.sh" </dev/null 2>&1 | stdbuf -oL tee -a "$tabby_log"
    else
      arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- env "${run_env[@]}" \
        bash "$stack_home/install.sh" </dev/null 2>&1 | tee -a "$tabby_log"
    fi
    status=${PIPESTATUS[0]}
  } 3>&-
  set -e
  rm -f "${TSOS_GAUGE_DIR:-/nonexistent}/nested"
  chown_target_user_tree "$stack_home/tabby-install.log"

  if ((status != 0)); then
    die "install.sh failed in the chroot (exit ${status}). Not rebooting.
Log: ${TARGET}${stack_home}/tabby-install.log
Do not run this installer again from scratch — that wipes the disk.
Fix the tree (git pull or --tabby-local-src), then resume:
  ${SCRIPT_NAME} --resume-tabby
or:
  arch-chroot ${TARGET} /usr/bin/runuser -u ${TARGET_USER} -- env \\
    HOME=/home/${TARGET_USER} USER=${TARGET_USER} LOGNAME=${TARGET_USER} TERM=linux \\
    TABBY_ISO_CHROOT=1 TABBY_SKIP_NVIDIA_REBOOT=1 \\
    TABBY_INSTALL_ROOT=${stack_home} TABBY_SAVER_ENABLED=1 \\
    bash ${stack_home}/install.sh"
  fi

  refresh_tsos_conf_from_tabby_env
  install -d -m 0755 "$TARGET/var/lib/tsos"
  touch "$TARGET/var/lib/tsos/tabby-firstboot.done"
  write_nopasswd_sudoers "$TARGET"
  log "tabbyapi-stack installed. After reboot, linger starts the API."
}

# MOTD reads /etc/tsos/install.conf. After install.sh, tabby.env has the
# listen address and model set the user actually chose.
refresh_tsos_conf_from_tabby_env() {
  local conf="$TARGET/etc/tsos/install.conf"
  local envf="$TARGET/home/${TARGET_USER}/tabbyapi-stack/tabbyAPI/deploy/arch/tabby.env"
  [[ -f "$envf" && -f "$conf" ]] || return 0
  local line key
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      TABBY_NETWORK_HOST=*|TABBY_NETWORK_PORT=*|TABBY_PUBLIC_BASE=*|COMFYUI_URL=*|TABBY_INSTALL_ROOT=*|TABBY_MODELS=*|TABBY_SSH_REMOTE=*|TABBY_SSH_FORWARD=*|TABBY_SSH_KEY=*)
        key="${line%%=*}"
        sed -i "/^${key}=/d" "$conf"
        printf '%s\n' "$line" >> "$conf"
        ;;
    esac
  done < "$envf"
}

# sudoers.d is included in lexical order; last matching rule wins.
# A drop-in named "wheel" sorts after 99-* and cancels NOPASSWD.
write_nopasswd_sudoers() {
  local root=$1
  install -d -m 0750 "$root/etc/sudoers.d"
  if [[ -f "$root/etc/sudoers.d/wheel" ]]; then
    mv "$root/etc/sudoers.d/wheel" "$root/etc/sudoers.d/10-wheel"
  fi
  if [[ -f "$root/etc/sudoers" ]] && ! grep -qE '^[[:space:]]*[@#]includedir[[:space:]]+/etc/sudoers.d' "$root/etc/sudoers"; then
    printf '\n@includedir /etc/sudoers.d\n' >>"$root/etc/sudoers"
  fi
  {
    printf 'Defaults:%s !use_pty,!requiretty,!pam_session\n' "$TARGET_USER"
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$TARGET_USER"
  } >"$root/etc/sudoers.d/zz-tsos-nopasswd"
  chmod 0440 "$root/etc/sudoers.d/zz-tsos-nopasswd"
  rm -f "$root/etc/sudoers.d/zz-tsos-firstboot" "$root/etc/sudoers.d/99-tsos-firstboot"
}

configure_chroot() {
  log "Configuring the installed system"
  arch-chroot "$TARGET" /bin/bash /root/configure-arch.sh
}

install_omarchy_chroot() {
  if [[ "$OMARCHY_MODE" != "now" ]]; then
    return 0
  fi

  log "Installing Omarchy as ${TARGET_USER} (not root)"
  log "This is the official installer. It can take a long time."
  log "When Omarchy says Reboot Now, that prompt is skipped — this script reboots the ISO at the end."

  # Omarchy's finished.sh blocks forever on: gum confirm "Reboot Now"
  # Even with OMARCHY_CHROOT_INSTALL=1 it still waits. PATH hits this first.
  cat >"$TARGET/usr/local/bin/gum" <<'GUM'
#!/usr/bin/env bash
if [[ "${1:-}" == "confirm" ]]; then
  case "$*" in
    *"Reboot Now"*) exit 0 ;;
  esac
fi
exec /usr/bin/gum "$@"
GUM
  chmod 0755 "$TARGET/usr/local/bin/gum"

  local runner="$TARGET/home/${TARGET_USER}/run-omarchy.sh"
  cat >"$runner" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export HOME="/home/${TARGET_USER}"
export USER="${TARGET_USER}"
export LOGNAME="${TARGET_USER}"
export OMARCHY_CHROOT_INSTALL=1
export OMARCHY_ONLINE_INSTALL=true
export PATH="/usr/local/sbin:/usr/local/bin:/usr/bin"
cd "\$HOME"
if [[ -f "\$HOME/.omarchy-identity" ]]; then
  # shellcheck disable=SC1091
  source "\$HOME/.omarchy-identity"
fi
curl -fsSL https://omarchy.org/install | bash
EOF
  chown --reference="$TARGET/home/${TARGET_USER}" "$runner"
  chmod 0755 "$runner"

  local status=0
  set +e
  {
    arch-chroot "$TARGET" /usr/bin/runuser -u "$TARGET_USER" -- /bin/bash "/home/${TARGET_USER}/run-omarchy.sh"
    status=$?
  } >>"$TSOS_LOG" 2>&1 3>&-
  set -e
  rm -f "$runner" "$TARGET/usr/local/bin/gum"

  if ((status != 0)); then
    warn "Omarchy installer exited with status $status."
    warn "The Arch base is installed and should boot."
    warn "After you unlock the disk (if encrypted) and log in as ${TARGET_USER}, run: install-omarchy"
  else
    log "Omarchy installer finished"
  fi
  # Omarchy removes 99-omarchy-installer when it finishes. Keep passwordless
  # sudo so Settings / tsctl / install.sh can use sudo -n after reboot.
  write_nopasswd_sudoers "$TARGET"
}

cleanup() {
  log "Cleaning installer files from the target"
  rm -f "$TARGET/root/configure-arch.sh" "$TARGET/usr/local/bin/gum"
  if [[ -f "$TARGET/root/install-vars.sh" ]]; then
    sed -i '/_PASSWORD=/d' "$TARGET/root/install-vars.sh" || true
  fi
  log "Unmounting"
  sync || true
  if mountpoint -q "$TARGET$CACHE_CHROOT_PATH" 2>/dev/null; then
    umount "$TARGET$CACHE_CHROOT_PATH" 2>/dev/null || umount -l "$TARGET$CACHE_CHROOT_PATH" 2>/dev/null || true
  fi
  if [[ -n "$TSOS_PAYLOAD_ROOT" ]]; then
    if [[ -n "$TSOS_OFFLINE_ROOT" ]]; then
      sed -i '/^# BEGIN TSOS OFFLINE$/,/^# END TSOS OFFLINE$/d' "$TARGET/etc/pacman.conf" 2>/dev/null || true
    fi
    if mountpoint -q "$TARGET$TSOS_OFFLINE_CHROOT" 2>/dev/null; then
      umount "$TARGET$TSOS_OFFLINE_CHROOT" 2>/dev/null ||
        umount -l "$TARGET$TSOS_OFFLINE_CHROOT" 2>/dev/null || true
    fi
  fi
  # Chroot leftover processes keep btrfs busy after a successful install.
  # Lazy umount hides that from lsblk, so the next wipe gets EBUSY on sda2.
  kill_disk_users "$DISK"
  sleep 1
  if ! umount -R "$TARGET" 2>/dev/null; then
    kill_disk_users "$DISK"
    sleep 1
    umount -R "$TARGET" 2>/dev/null || umount -R -l "$TARGET" 2>/dev/null || true
  fi
  forget_btrfs_on_disk "$DISK"
  if ((ENCRYPT)) && [[ -e "/dev/mapper/$CRYPT_NAME" ]]; then
    cryptsetup close "$CRYPT_NAME" || true
  fi
  if mountpoint -q "$CACHE_STAGING" 2>/dev/null; then
    umount "$CACHE_STAGING" 2>/dev/null || umount -l "$CACHE_STAGING" 2>/dev/null || true
  fi
}

final_message() {
  if [[ -f "$TARGET/etc/tsos/install.conf" ]]; then
    # shellcheck disable=SC1090
    source "$TARGET/etc/tsos/install.conf"
  fi
  cat <<EOF

Arch base is installed on $DISK.

Next boot:
  1. Remove the live ISO
EOF
  if ((ENCRYPT)); then
    cat <<EOF
  2. Enter the LUKS password at the Limine/unlock prompt
  3. Log in as ${TARGET_USER}
EOF
  else
    cat <<EOF
  2. Log in as ${TARGET_USER}
EOF
  fi
  cat <<EOF

tabbyapi-stack
  Installed in the chroot at /home/${TARGET_USER}/tabbyapi-stack
  After reboot, linger starts the API. install.sh is not run again.
  Models: ${TABBY_MODELS}
  API:       http://${TABBY_NETWORK_HOST}:${TABBY_NETWORK_PORT}
  UI:        http://127.0.0.1:${TABBY_NETWORK_PORT}/v1/ui
  Watch:     journalctl --user -u tabbyapi -f
  Banner:    tsos-motd   (also printed on login)

EOF
  case "$OMARCHY_MODE" in
    now)
      cat <<EOF
Omarchy was requested in the chroot. If it finished, you should get that
desktop after boot. If it did not, log in and run:

  install-omarchy

EOF
      ;;
    skip)
      cat <<EOF
Omarchy was skipped. To add it later:

  install-omarchy

EOF
      ;;
  esac
}

offer_reboot() {
  if ((USE_TUI)); then
    if ui_yesno "Reboot" \
"Install finished. Remove the live USB/ISO now.

Reboot into the new system?" 1; then
      log "Rebooting"
      reboot || systemctl reboot || true
    else
      log "Staying on the live ISO"
    fi
    return 0
  fi
  printf '\n' >/dev/tty
  printf '%s\n' "Install finished. Remove the live USB/ISO now." >/dev/tty
  printf '%s\n' "Press Enter to reboot into the new system (Ctrl+C stays on the ISO)." >/dev/tty
  if have_console; then
    read_tty "Reboot: " >/dev/null
  else
    log "No console; rebooting in 8 seconds"
    sleep 8
  fi
  log "Rebooting"
  reboot || systemctl reboot || true
}

load_existing_tsos_conf() {
  local conf="$TARGET/etc/tsos/install.conf"
  [[ -f "$conf" ]] || return 0
  # shellcheck disable=SC1090
  source "$conf"
  if [[ -f "$TARGET/etc/tsos/secrets.env" ]]; then
    # shellcheck disable=SC1090
    source "$TARGET/etc/tsos/secrets.env"
  fi
}

# Finish install.sh after a chroot failure. /mnt must still be the new system.
resume_tabby_work() {
  gauge_update 40 "Resuming tabbyapi-stack"
  run_tabby_install_chroot
  if [[ "$OMARCHY_MODE" == "now" ]]; then
    gauge_update 96 "Installing Omarchy"
  fi
  install_omarchy_chroot
  cleanup
}

resume_tabby_install() {
  [[ -f "$TARGET/etc/arch-release" ]] || \
    die "$TARGET is not an Arch install. Leave the new system mounted at $TARGET (do not reboot off the ISO)."
  local cli_cache="$TABBY_CACHE"
  load_existing_tsos_conf
  if [[ -n "$cli_cache" && -d "$cli_cache" ]]; then
    TABBY_CACHE="$cli_cache"
  fi
  valid_username "$TARGET_USER" || die "invalid user name: $TARGET_USER"
  local stack_home="/home/${TARGET_USER}/tabbyapi-stack"
  [[ -f "$TARGET$stack_home/install.sh" ]] || \
    die "missing $stack_home/install.sh under $TARGET"
  write_nopasswd_sudoers "$TARGET"
  refresh_tabbyapi_stack_in_target
  run_with_gauge resume_tabby_work
  final_message
  offer_reboot
}

install_os_work() {
  log "Starting the install..."
  gauge_update 2 "Preparing disk"
  preflight
  disable_live_mkinitcpio_hooks
  timedatectl set-ntp true || true
  preserve_tabby_cache
  gauge_update 8 "Wiping and partitioning"
  wipe_and_partition
  gauge_update 15 "Formatting and mounting"
  setup_storage
  gauge_update 22 "Installing Arch packages"
  install_base
  # Bind only after genfstab so the live ISO payload is never persisted as a
  # target mount. pacstrap reads the same file:// repository from the host.
  bind_offline_payload_into_target
  gauge_update 38 "Configuring the new system"
  write_chroot_files
  configure_chroot
  write_tabby_bootstrap
  run_tabby_install_chroot
  if [[ "$OMARCHY_MODE" == "now" ]]; then
    gauge_update 96 "Installing Omarchy"
  fi
  install_omarchy_chroot
  gauge_update 98 "Cleaning up"
  cleanup
}

main() {
  parse_args "$@"
  attach_console
  # A leftover dialog from a killed run owns tty1 and swallows the first OK.
  pkill -x dialog 2>/dev/null || true
  restore_tty
  early_preflight
  if ((RESUME_TABBY)); then
    log "Resuming tabbyapi-stack in the already-mounted system at $TARGET (no disk wipe)"
    if ((DRY_RUN)); then
      log "dry-run: would resume install.sh at $TARGET"
      exit 0
    fi
    resume_tabby_install
    exit 0
  fi
  if ((CONFIG_PROVIDED == 0)) || [[ -z "$DISK" ]]; then
    ensure_dialog
    enable_tui_if_possible
  fi
  if ((CONFIG_PROVIDED)); then
    pick_disk_if_needed
  else
    prompt_settings
  fi
  validate_names
  require_disk
  assign_partition_numbers
  if ((USE_TUI == 0)) || ((DRY_RUN)); then
    print_plan
  fi
  if ((DRY_RUN)); then
    log "dry-run: no changes made"
    exit 0
  fi
  confirm_wipe
  collect_passwords
  run_with_gauge install_os_work
  final_message
  offer_reboot
}

main "$@"
