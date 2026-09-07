#!/usr/bin/env bash
# Build a bootable TSOS installer ISO. Arch packages, Python, PyTorch, ComfyUI,
# and Docker images are downloaded at install time. The ISO ships this tree
# plus 3rd-party git that may not stay online (ComfyUI-GGUF).
# Run as root in Arch Linux (the workflow uses a privileged Arch container).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${TSOS_ISO_OUT:-$ROOT/out}"
WORK="${TSOS_ISO_WORK:-$ROOT/.iso-work}"
PROFILE="$WORK/profile"
PAYLOAD="$WORK/payload"

log() { printf '\n==> %s\n' "$*"; }
disk() { df -h "$WORK" "$OUT" 2>/dev/null || true; }
need() { command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 1; }; }

[[ ${EUID} -eq 0 ]] || { echo "run this builder as root" >&2; exit 1; }
[[ -f "$ROOT/tsos-installer.sh" && -f "$ROOT/tabbyAPI/pyproject.toml" ]] || {
  echo "run iso/build.sh from a tabbyapi-stack checkout" >&2
  exit 1
}

pacman -Sy --noconfirm --needed archiso git python rsync xorriso squashfs-tools
for cmd in mkarchiso git rsync xorriso unsquashfs python3; do need "$cmd"; done

rm -rf "$WORK"
mkdir -p "$PROFILE" "$PAYLOAD"/{bundles,tabbyapi-stack} "$OUT"
cp -a /usr/share/archiso/configs/releng/. "$PROFILE/"

log "Bundling 3rd-party git that may disappear"
bundle_repo() {
  local url=$1 name=$2 dir="$WORK/git-$2"
  git clone --mirror "$url" "$dir"
  git -C "$dir" bundle create "$PAYLOAD/bundles/$name.bundle" --all
  rm -rf "$dir"
}
bundle_repo https://github.com/city96/ComfyUI-GGUF.git ComfyUI-GGUF

log "Adding tabbyapi-stack"
rsync -a --delete \
  --exclude '.git/' --exclude '.iso-work/' --exclude 'out/' --exclude 'ComfyUI/' \
  --exclude '**/venv/' --exclude '**/models/' \
  --exclude '**/config.yml' --exclude '**/api_tokens.yml' \
  --exclude '**/tabby.env' --exclude '**/.env' \
  --exclude '**/ui_users.json' --exclude '**/ui_sessions.json' \
  --exclude '**/*.key' --exclude '**/*.pem' --exclude '**/id_ed25519*' \
  --exclude '**/id_rsa*' --exclude '**/.ssh/' --exclude '**/auth.json' \
  --exclude '**/logs/' --exclude '**/pasted-images/' \
  "$ROOT/" "$PAYLOAD/tabbyapi-stack/"
if [[ -d "$ROOT/.git" ]]; then
  git -c "safe.directory=$ROOT" -C "$ROOT" \
    bundle create "$PAYLOAD/bundles/tabbyapi-stack.bundle" --all
else
  SNAPSHOT_REPO="$WORK/tabbyapi-stack-repo"
  mkdir -p "$SNAPSHOT_REPO"
  rsync -a --exclude '.git/' "$PAYLOAD/tabbyapi-stack/" "$SNAPSHOT_REPO/"
  git -C "$SNAPSHOT_REPO" init
  git -C "$SNAPSHOT_REPO" add -A
  GIT_AUTHOR_NAME=tsos GIT_AUTHOR_EMAIL=tsos@localhost \
  GIT_COMMITTER_NAME=tsos GIT_COMMITTER_EMAIL=tsos@localhost \
    git -C "$SNAPSHOT_REPO" commit -m "TSOS ISO source snapshot"
  git -C "$SNAPSHOT_REPO" bundle create "$PAYLOAD/bundles/tabbyapi-stack.bundle" --all
  rm -rf "$SNAPSHOT_REPO"
fi

log "Overlaying archiso profile"
mkdir -p \
  "$PROFILE/airootfs/opt/tsos" \
  "$PROFILE/airootfs/usr/local/bin" \
  "$PROFILE/airootfs/etc/profile.d" \
  "$PROFILE/airootfs/root" \
  "$PROFILE/airootfs/usr/lib/initcpio/hooks" \
  "$PROFILE/airootfs/usr/lib/initcpio/install"
mv "$PAYLOAD"/* "$PROFILE/airootfs/opt/tsos/"
install -m 0755 "$ROOT/tsos-installer.sh" \
  "$PROFILE/airootfs/usr/local/bin/tsos-installer.sh"
install -m 0755 "$ROOT/iso/tsos-live-install.sh" \
  "$PROFILE/airootfs/usr/local/bin/tsos-live-install"
install -m 0755 "$ROOT/iso/tsos-boot-splash.sh" \
  "$PROFILE/airootfs/usr/local/bin/tsos-boot-splash"
install -m 0644 "$ROOT/iso/initcpio/hooks/tsos_wait" \
  "$PROFILE/airootfs/usr/lib/initcpio/hooks/tsos_wait"
install -m 0644 "$ROOT/iso/initcpio/install/tsos_wait" \
  "$PROFILE/airootfs/usr/lib/initcpio/install/tsos_wait"
cat >"$PROFILE/airootfs/etc/profile.d/tsos-iso.sh" <<'EOF'
export TABBY_LOCAL_SRC=/opt/tsos/tabbyapi-stack
EOF
# releng autologins root on tty1 into zsh. Start the installer there, like
# Debian, instead of dropping to a live shell.
if [[ -f "$PROFILE/airootfs/root/.zlogin" ]]; then
  cat >>"$PROFILE/airootfs/root/.zlogin" <<'EOF'

# TSOS: boot into the installer on the first console.
if [[ -z ${TSOS_INSTALLER_STARTED:-} && -t 0 ]]; then
  _tsos_tty=$(tty 2>/dev/null || true)
  if [[ "$_tsos_tty" == /dev/tty1 ]]; then
    export TSOS_INSTALLER_STARTED=1
    bash /usr/local/bin/tsos-live-install
  fi
fi
EOF
else
  cat >"$PROFILE/airootfs/root/.zlogin" <<'EOF'
[[ -x ~/.automated_script.sh ]] && ~/.automated_script.sh
if [[ -z ${TSOS_INSTALLER_STARTED:-} && -t 0 ]]; then
  _tsos_tty=$(tty 2>/dev/null || true)
  if [[ "$_tsos_tty" == /dev/tty1 ]]; then
    export TSOS_INSTALLER_STARTED=1
    bash /usr/local/bin/tsos-live-install
  fi
fi
EOF
fi
cat >"$PROFILE/airootfs/root/.bash_profile" <<'EOF'
[[ -f ~/.bashrc ]] && . ~/.bashrc
if [[ -z ${TSOS_INSTALLER_STARTED:-} && -t 0 ]]; then
  _tsos_tty=$(tty 2>/dev/null || true)
  if [[ "$_tsos_tty" == /dev/tty1 ]]; then
    export TSOS_INSTALLER_STARTED=1
    bash /usr/local/bin/tsos-live-install
  fi
fi
EOF
# Boot menus should look like an installer, not a live rescue disk.
find "$PROFILE/syslinux" "$PROFILE/grub" "$PROFILE/efiboot" \
  \( -name '*.cfg' -o -name '*.conf' \) -type f -print0 2>/dev/null |
  xargs -0 -r sed -i \
    -e 's/Arch Linux install medium/TSOS installer/g' \
    -e 's/Arch Linux (x86_64, UEFI)/TSOS installer (UEFI)/g' \
    -e 's/Boot the Arch Linux install medium/Boot the TSOS installer/g' \
    -e 's/^MENU TITLE .*/MENU TITLE TSOS installer/' \
    -e 's/^title Arch Linux.*/title TSOS installer/'
# Hide kernel/systemd noise; Plymouth (splash) keeps the logo up instead.
find "$PROFILE/syslinux" "$PROFILE/grub" "$PROFILE/efiboot" \
  \( -name '*.cfg' -o -name '*.conf' \) -type f -print0 2>/dev/null |
  xargs -0 -r sed -i \
    -e '/archisobasedir=/ s/[[:space:]]quiet\b//g' \
    -e '/archisobasedir=/ s/$/ quiet splash plymouth.use-simpledrm=1 loglevel=3 systemd.show_status=false rd.udev.log_level=3 vt.global_cursor_default=0/'
if [[ -f "$PROFILE/efiboot/loader/loader.conf" ]]; then
  sed -i -e 's/^timeout .*/timeout 5/' -e 's/^beep on/beep off/' \
    "$PROFILE/efiboot/loader/loader.conf"
fi
if [[ -f "$PROFILE/syslinux/archiso_sys.cfg" ]]; then
  sed -i 's/^TIMEOUT .*/TIMEOUT 50/' "$PROFILE/syslinux/archiso_sys.cfg"
fi
log "Writing boot splash and Plymouth theme"
python3 "$ROOT/iso/mk-splash.py" "$WORK/splash-assets"
install -m 0644 "$WORK/splash-assets/splash.png" "$PROFILE/syslinux/splash.png"
[[ -s "$PROFILE/syslinux/splash.png" ]] || {
  echo "boot splash PNG was not written" >&2
  exit 1
}
THEME="$PROFILE/airootfs/usr/share/plymouth/themes/tsos"
mkdir -p "$THEME" "$PROFILE/airootfs/etc/plymouth" "$PROFILE/airootfs/etc/systemd/system"
install -m 0644 "$ROOT/iso/plymouth/tsos.plymouth" "$THEME/tsos.plymouth"
install -m 0644 "$ROOT/iso/plymouth/tsos.script" "$THEME/tsos.script"
install -m 0644 "$WORK/splash-assets/logo.png" "$THEME/logo.png"
install -m 0644 "$WORK/splash-assets/spinner.png" "$THEME/spinner.png"
install -m 0644 "$WORK/splash-assets/loading.png" "$THEME/loading.png"
install -m 0644 "$WORK/splash-assets/please-wait.png" "$THEME/please-wait.png"
cat >"$PROFILE/airootfs/etc/plymouth/plymouthd.conf" <<'EOF'
[Daemon]
Theme=tsos
ShowDelay=0
UseSimpledrm=true
EOF
# Keep Plymouth up until the installer is ready for a question. Other
# consoles (Alt+F2) still get a getty.
ln -sfn /dev/null "$PROFILE/airootfs/etc/systemd/system/plymouth-quit.service"
ln -sfn /dev/null "$PROFILE/airootfs/etc/systemd/system/plymouth-quit-wait.service"
archiso_hooks="$PROFILE/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
[[ -f "$archiso_hooks" ]] || {
  echo "missing archiso mkinitcpio hooks: $archiso_hooks" >&2
  exit 1
}
# simpledrm is built into the Arch kernel, so there is no module file to add to
# MODULES. Start the text wait hook and Plymouth after udev, before full GPU KMS.
sed -i -E \
  -e 's/[[:space:]]+(tsos_wait|plymouth)([[:space:]]|\))/\2/g' \
  -e 's/\budev\b/udev tsos_wait plymouth/' \
  "$archiso_hooks"
grep -qE '\budev tsos_wait plymouth\b.*\bkms\b' "$archiso_hooks" || {
  echo "could not order tsos_wait and plymouth before kms" >&2
  exit 1
}
# Blank the Arch getty banner. tty1 autologins, then tsos-live-install paints.
# agetty has --noclear and --noissue. An unknown flag makes getty exit
# immediately and systemd restart it forever (blank screen + cursor).
: >"$PROFILE/airootfs/etc/motd"
printf '\n' >"$PROFILE/airootfs/etc/issue"
mkdir -p "$PROFILE/airootfs/etc/systemd/system/getty@tty1.service.d"
cat >"$PROFILE/airootfs/etc/systemd/system/getty@tty1.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/usr/bin/agetty --noissue --autologin root - linux
EOF
for package in dialog rsync git plymouth; do
  grep -qxF "$package" "$PROFILE/packages.x86_64" || echo "$package" >>"$PROFILE/packages.x86_64"
done
sed -i \
  -e 's/^iso_name=.*/iso_name="tsos-archlinux"/' \
  -e "s/^iso_label=.*/iso_label=\"TSOS_$(date -u +%Y%m%d)\"/" \
  "$PROFILE/profiledef.sh"
# mkarchiso copies airootfs with --no-preserve=mode (files become 644).
# Only paths listed in file_permissions keep the execute bit.
if grep -q 'file_permissions=(' "$PROFILE/profiledef.sh"; then
  sed -i '/file_permissions=(/a\
  ["/usr/local/bin/tsos-live-install"]="0:0:755"\
  ["/usr/local/bin/tsos-boot-splash"]="0:0:755"\
  ["/usr/local/bin/tsos-installer.sh"]="0:0:755"
' "$PROFILE/profiledef.sh"
else
  cat >>"$PROFILE/profiledef.sh" <<'EOF'
file_permissions+=(
  ["/usr/local/bin/tsos-live-install"]="0:0:755"
  ["/usr/local/bin/tsos-boot-splash"]="0:0:755"
  ["/usr/local/bin/tsos-installer.sh"]="0:0:755"
)
EOF
fi

log "Building ISO"
disk
mkarchiso -v -w "$WORK/mkarchiso" -o "$OUT" "$PROFILE"
iso="$(find "$OUT" -maxdepth 1 -type f -name '*.iso' -print -quit)"
[[ -n "$iso" ]] || { echo "mkarchiso produced no ISO" >&2; exit 1; }
mv "$iso" "$OUT/tsos-archlinux.iso"
VERIFY="$WORK/verify"
mkdir -p "$VERIFY"
xorriso -osirrox on -indev "$OUT/tsos-archlinux.iso" \
  -extract /arch/x86_64/airootfs.sfs "$VERIFY/airootfs.sfs"
unsquashfs -ll "$VERIFY/airootfs.sfs" >"$VERIFY/airootfs.list"
grep -q 'squashfs-root/opt/tsos/tabbyapi-stack/install.sh' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: tabbyapi-stack payload is missing" >&2
  exit 1
}
grep -qE '^-rwx.*squashfs-root/usr/local/bin/tsos-live-install$' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: tsos-live-install is missing or not executable" >&2
  exit 1
}
grep -qE '^-rwx.*squashfs-root/usr/local/bin/tsos-boot-splash$' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: tsos-boot-splash is missing or not executable" >&2
  exit 1
}
grep -q 'squashfs-root/usr/share/plymouth/themes/tsos/logo.png' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: Plymouth TSOS logo is missing" >&2
  exit 1
}
grep -q 'squashfs-root/usr/share/plymouth/themes/tsos/loading.png' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: Plymouth loading caption is missing" >&2
  exit 1
}
grep -q 'squashfs-root/usr/share/plymouth/themes/tsos/please-wait.png' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: Plymouth wait caption is missing" >&2
  exit 1
}
grep -q 'squashfs-root/usr/lib/initcpio/hooks/tsos_wait' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: early wait hook is missing" >&2
  exit 1
}
grep -qE '^-rwx.*squashfs-root/usr/local/bin/tsos-installer.sh$' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: tsos-installer.sh is missing or not executable" >&2
  exit 1
}
grep -q 'squashfs-root/opt/tsos/bundles/ComfyUI-GGUF.bundle' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: ComfyUI-GGUF bundle is missing" >&2
  exit 1
}
if grep -q 'squashfs-root/opt/tsos/pacman/tsos.db' "$VERIFY/airootfs.list"; then
  echo "ISO verification failed: frozen pacman repo should not be on the small ISO" >&2
  exit 1
fi
if grep -q 'squashfs-root/opt/tsos/wheels/' "$VERIFY/airootfs.list"; then
  echo "ISO verification failed: Python wheels should not be on the small ISO" >&2
  exit 1
fi
sha256sum "$OUT/tsos-archlinux.iso" >"$OUT/SHA256SUMS"
log "Built $OUT/tsos-archlinux.iso"
disk
