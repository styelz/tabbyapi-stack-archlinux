#!/bin/sh
# LightDM display-setup-script: let tabby-saver overlay this X seat after idle
# instead of chvt (NVIDIA cannot recover if the kiosk steals DRM).
[ -n "${DISPLAY:-}" ] || exit 0
user=$(systemctl show -p User --value tabby-saver 2>/dev/null) || exit 0
case "$user" in
  ""|"[not set]") exit 0 ;;
esac
home=$(getent passwd "$user" | cut -d: -f6) || exit 0
[ -n "$home" ] || exit 0
if command -v xhost >/dev/null 2>&1; then
  xhost "+SI:localuser:${user}" >/dev/null 2>&1 || true
fi
command -v xauth >/dev/null 2>&1 || exit 0
command -v runuser >/dev/null 2>&1 || exit 0
auth="$home/.Xauthority"
touch "$auth" || exit 0
chown "$user:$user" "$auth" 2>/dev/null || true
chmod 600 "$auth" 2>/dev/null || true
xauth nlist "$DISPLAY" 2>/dev/null | runuser -u "$user" -- env XAUTHORITY="$auth" xauth nmerge - 2>/dev/null || true
exit 0
