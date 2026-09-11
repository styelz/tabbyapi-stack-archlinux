#!/usr/bin/env bash
# First console after live boot: wait for network, then run the installer.
# Esc/Ctrl+C returns to a root shell. Other ttys stay as maintenance shells.
# archiso copies airootfs without mode bits, so this script must run the
# installer with bash even when the file is not +x.
set -euo pipefail

find_installer() {
  local candidate
  for candidate in \
    /usr/local/bin/tsos-installer.sh \
    /opt/tsos/tabbyapi-stack/tsos-installer.sh
  do
    if [[ -f "$candidate" && -r "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

INSTALLER="$(find_installer || true)"
SPLASH=/usr/local/bin/tsos-boot-splash

have_route() {
  ip -4 route show default 2>/dev/null | grep -q .
}

show_splash() {
  local msg=$1
  if [[ -x "$SPLASH" ]]; then
    bash "$SPLASH" "$msg" || true
    return 0
  fi
  if command -v plymouth >/dev/null 2>&1 && plymouth --ping >/dev/null 2>&1; then
    plymouth display-message --text="$msg" >/dev/null 2>&1 || true
    return 0
  fi
  printf '\n  TSOS installer\n\n  %s\n' "$msg"
}

wait_for_network() {
  local i
  for i in $(seq 1 30); do
    if have_route; then
      show_splash "Network is up. Starting the installer..."
      return 0
    fi
    show_splash "Waiting for network (DHCP)... ${i}s    Ethernet or Alt+F2 + iwctl"
    sleep 1
  done
  show_splash "No network yet. Alt+F2, then: iwctl station wlan0 connect 'SSID'    Return here and press Enter."
  read -r _ || true
}

if [[ ! -t 0 || ! -t 1 ]]; then
  exit 0
fi
if [[ -z "$INSTALLER" ]]; then
  printf 'TSOS installer missing (/usr/local/bin/tsos-installer.sh).\n'
  exit 1
fi

show_splash "Starting TSOS..."
wait_for_network
# tsos-installer.sh checks GitHub for a newer copy of itself once HTTPS works.
set +e
bash "$INSTALLER"
status=$?
set -e
if [[ -x "$SPLASH" ]]; then
  bash "$SPLASH" --quit || true
fi
{
  command -v tput >/dev/null 2>&1 && tput clear || printf '\033[H\033[2J'
  printf '\033[?25h\033[m'
} >/dev/tty 2>/dev/null || true
printf '\n'
if ((status != 0)); then
  printf 'Installer exited with status %s.\n' "$status"
fi
printf 'Run tsos-installer.sh to try again, or reboot.\n'
exit 0
