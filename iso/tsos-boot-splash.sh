#!/usr/bin/env bash
# Full-screen TSOS logo + spinner on the live console when Plymouth is not up.
# Usage: tsos-boot-splash "Waiting for network..."
#        tsos-boot-splash --quit
set -euo pipefail

FRAMES='⠋⠙⠹⠸⠼⠴⠦⠧'
NAVY='\033[48;2;11;28;58m'
CYAN='\033[38;2;51;204;255m'
WHITE='\033[38;2;255;255;255m'
MUTED='\033[38;2;160;190;220m'
RESET='\033[0m'
TICK_FILE="${TMPDIR:-/tmp}/tsos-boot-splash.tick"

have_plymouth() {
  command -v plymouth >/dev/null 2>&1 && plymouth --ping >/dev/null 2>&1
}

quit_splash() {
  if have_plymouth; then
    plymouth quit >/dev/null 2>&1 || true
  fi
  if [[ -t 1 ]]; then
    printf '\033[?25h\033[0m\033[H\033[J'
  fi
  rm -f "$TICK_FILE"
}

if [[ "${1:-}" == "--quit" ]]; then
  quit_splash
  exit 0
fi

MSG=${1:-Starting TSOS...}
MSG=${MSG//$'\n'/ }
MSG=${MSG//  / }

if have_plymouth; then
  plymouth display-message --text="$MSG" >/dev/null 2>&1 || true
  exit 0
fi

if [[ ! -t 1 ]]; then
  printf '%s\n' "$MSG"
  exit 0
fi

tick=0
if [[ -f "$TICK_FILE" ]]; then
  tick=$(cat "$TICK_FILE" 2>/dev/null || echo 0)
fi
tick=$((tick + 1))
printf '%s\n' "$tick" >"$TICK_FILE"
frame=${FRAMES:$((tick % ${#FRAMES})):1}

# Center the fallback from the actual console size instead of assuming 80x24.
cols=$(tput cols 2>/dev/null || echo 80)
rows=$(tput lines 2>/dev/null || echo 24)
[[ "$cols" =~ ^[0-9]+$ ]] || cols=80
[[ "$rows" =~ ^[0-9]+$ ]] || rows=24
((cols >= 40)) || cols=80
((rows >= 16)) || rows=24

center_col() {
  local text=$1
  local col=$(((cols - ${#text}) / 2 + 1))
  ((col > 0)) || col=1
  printf '%s' "$col"
}

top=$((rows / 2 - 6))
((top > 1)) || top=2

# Truecolor navy field, centered wordmark, spinner, and readable status.
# It falls back to a blue screen if the console cannot do 24-bit colour.
printf '\033[?25l\033[H'
printf '%b' "$NAVY"
printf '\033[2J\033[H'
printf '%b' "$CYAN"
for line in '╭─────────╮' '│  TSOS   │' '╰─────────╯'; do
  printf '\033[%d;%dH%s' "$top" "$(center_col "$line")" "$line"
  top=$((top + 1))
done
printf '%b' "$WHITE"
printf '\033[%d;%dH%s' "$((top + 1))" "$(center_col 'tabbyapi-stack')" 'tabbyapi-stack'
printf '\033[%d;%dH%b%s%b' "$((top + 3))" "$(center_col "$frame")" "$CYAN" "$frame" "$WHITE"
if [[ "$MSG" == "Starting TSOS..." ]]; then
  printf '\033[%d;%dH%b%s%b' "$((top + 5))" "$(center_col 'LOADING')" "$WHITE" 'LOADING' "$RESET"
  printf '\033[%d;%dH%b%s%b' "$((top + 7))" "$(center_col 'PLEASE WAIT')" "$MUTED" 'PLEASE WAIT' "$RESET"
else
  printf '\033[%d;%dH%b%s%b' "$((top + 6))" "$(center_col "$MSG")" "$MUTED" "$MSG" "$RESET"
fi
printf '%b' "$RESET"
