#!/usr/bin/env bash
# First console after live boot: wait for network, then run the installer.
# Esc/Ctrl+C returns to a root shell. Other ttys stay as maintenance shells.
set -euo pipefail

INSTALLER=/usr/local/bin/tsos-installer.sh

have_route() {
  ip -4 route show default 2>/dev/null | grep -q .
}

wait_for_network() {
  local i
  printf '\nWaiting for network (DHCP)...\n'
  for i in $(seq 1 30); do
    if have_route; then
      printf 'Network is up.\n'
      return 0
    fi
    sleep 1
  done
  printf '\nNo default route yet.\n'
  printf 'Ethernet: plug in a cable and wait.\n'
  printf 'Wi-Fi: Alt+F2, login as root, then:\n'
  printf "  iwctl station wlan0 connect 'SSID'\n"
  printf 'Return here with Alt+F1, then press Enter.\n'
  read -r _ || true
}

tty=$(tty 2>/dev/null || true)
if [[ "$tty" != /dev/tty1 ]]; then
  exit 0
fi
if [[ ! -t 0 || ! -t 1 ]]; then
  exit 0
fi
if [[ ! -x "$INSTALLER" ]]; then
  printf 'TSOS installer missing (%s).\n' "$INSTALLER"
  exit 1
fi

clear
printf 'TSOS installer\n'
printf 'This installs Arch Linux and tabbyapi-stack.\n'
wait_for_network
set +e
"$INSTALLER"
status=$?
set -e
printf '\n'
if ((status != 0)); then
  printf 'Installer exited with status %s.\n' "$status"
fi
printf 'Run tsos-installer.sh to try again, or reboot.\n'
exit 0
