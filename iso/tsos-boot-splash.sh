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

# Truecolor navy field, wordmark, spinner. Falls back to a blue screen if
# the console cannot do 24-bit colour (the logo still reads).
printf '\033[?25l\033[H'
printf '%b' "$NAVY"
printf '\033[2J\033[H'
printf '%b' "$CYAN"
cat <<'EOF'

                         ╭─────────╮
                         │  TSOS   │
                         ╰─────────╯
EOF
printf '%b' "$WHITE"
printf '\n                    tabbyapi-stack\n\n'
printf '                         %b%s%b\n' "$CYAN" "$frame" "$WHITE"
printf '\n%b                    %s\n' "$MUTED" "$MSG"
printf '%b' "$RESET"
