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

have_route() {
  ip -4 route show default 2>/dev/null | grep -q .
}

write_splash_dialogrc() {
  local f="${TMPDIR:-/tmp}/tsos-splash-dialogrc"
  cat >"$f" <<'EOF'
use_shadow = ON
use_colors = ON
screen_color = (CYAN,BLUE,ON)
dialog_color = (BLACK,WHITE,OFF)
title_color = (BLUE,WHITE,ON)
border_color = (WHITE,WHITE,ON)
border2_color = (BLACK,WHITE,OFF)
EOF
  export DIALOGRC="$f"
}

show_splash() {
  local msg=$1
  if command -v dialog >/dev/null && [[ -t 1 ]]; then
    write_splash_dialogrc
    dialog --backtitle "tsos  ·  tabbyapi-stack" --title "TSOS installer" \
      --infobox "$msg" 10 56
    return 0
  fi
  clear
  printf '\n  TSOS installer\n\n  %s\n' "$msg"
}

wait_for_network() {
  local i
  for i in $(seq 1 30); do
    if have_route; then
      show_splash "

  Network is up.
  Starting the installer..."
      return 0
    fi
    show_splash "

  Waiting for network (DHCP)...  ${i}s

  Ethernet: plug in a cable.
  Wi-Fi: Alt+F2, then iwctl."
    sleep 1
  done
  show_splash "

  No default route yet.
  Ethernet: plug in a cable and wait.
  Wi-Fi: Alt+F2, login as root, then:
    iwctl station wlan0 connect 'SSID'
  Return here with Alt+F1, then press Enter."
  read -r _ || true
}

if [[ ! -t 0 || ! -t 1 ]]; then
  exit 0
fi
if [[ -z "$INSTALLER" ]]; then
  printf 'TSOS installer missing (/usr/local/bin/tsos-installer.sh).\n'
  exit 1
fi

show_splash "

  Starting TSOS...
  Preparing the installer."
wait_for_network
# tsos-installer.sh checks GitHub for a newer copy of itself once HTTPS works.
set +e
bash "$INSTALLER"
status=$?
set -e
printf '\n'
if ((status != 0)); then
  printf 'Installer exited with status %s.\n' "$status"
fi
printf 'Run tsos-installer.sh to try again, or reboot.\n'
exit 0
