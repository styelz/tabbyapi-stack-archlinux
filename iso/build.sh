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

pacman -Sy --noconfirm --needed archiso git rsync xorriso squashfs-tools
for cmd in mkarchiso git rsync xorriso unsquashfs; do need "$cmd"; done

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
  "$PROFILE/airootfs/root"
mv "$PAYLOAD"/* "$PROFILE/airootfs/opt/tsos/"
install -m 0755 "$ROOT/tsos-installer.sh" \
  "$PROFILE/airootfs/usr/local/bin/tsos-installer.sh"
install -m 0755 "$ROOT/iso/tsos-live-install.sh" \
  "$PROFILE/airootfs/usr/local/bin/tsos-live-install"
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
    /usr/local/bin/tsos-live-install
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
    /usr/local/bin/tsos-live-install
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
    /usr/local/bin/tsos-live-install
  fi
fi
EOF
# Boot menus should look like an installer, not a live rescue disk.
find "$PROFILE/syslinux" "$PROFILE/grub" "$PROFILE/efiboot" \
  \( -name '*.cfg' -o -name '*.conf' \) -type f -print0 2>/dev/null |
  xargs -0 -r sed -i \
    -e 's/Arch Linux install medium/TSOS installer/g' \
    -e 's/Arch Linux (x86_64, UEFI)/TSOS installer (UEFI)/g' \
    -e 's/Boot the Arch Linux install medium/Boot the TSOS installer/g'
for package in dialog rsync git; do
  grep -qxF "$package" "$PROFILE/packages.x86_64" || echo "$package" >>"$PROFILE/packages.x86_64"
done
sed -i \
  -e 's/^iso_name=.*/iso_name="tsos-archlinux"/' \
  -e "s/^iso_label=.*/iso_label=\"TSOS_$(date -u +%Y%m%d)\"/" \
  "$PROFILE/profiledef.sh"

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
grep -q 'squashfs-root/usr/local/bin/tsos-live-install' "$VERIFY/airootfs.list" || {
  echo "ISO verification failed: live installer launcher is missing" >&2
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
