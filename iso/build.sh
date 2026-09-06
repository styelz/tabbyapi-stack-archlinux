#!/usr/bin/env bash
# Build a bootable TSOS installer ISO from the current Arch package snapshot.
# Run as root in Arch Linux (the workflow uses a privileged Arch container).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${TSOS_ISO_OUT:-$ROOT/out}"
WORK="${TSOS_ISO_WORK:-$ROOT/.iso-work}"
PROFILE="$WORK/profile"
PAYLOAD="$WORK/payload"
PYTHON_VERSION=3.12.5

log() { printf '\n==> %s\n' "$*"; }
disk() { df -h "$WORK" "$OUT" 2>/dev/null || true; }
need() { command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 1; }; }

[[ ${EUID} -eq 0 ]] || { echo "run this builder as root" >&2; exit 1; }
[[ -f "$ROOT/tsos-installer.sh" && -f "$ROOT/tabbyAPI/pyproject.toml" ]] || {
  echo "run iso/build.sh from a tabbyapi-stack checkout" >&2
  exit 1
}

pacman -Sy --noconfirm --needed \
  archiso git python curl rsync docker xorriso squashfs-tools \
  base-devel openssl zlib xz tk readline sqlite bzip2 ncurses gdbm libffi
for cmd in mkarchiso repo-add git curl rsync xorriso unsquashfs; do need "$cmd"; done

rm -rf "$WORK"
mkdir -p "$PROFILE" "$PAYLOAD"/{pacman,wheels,bundles,python,docker} "$OUT"
cp -a /usr/share/archiso/configs/releng/. "$PROFILE/"

log "Snapshotting Arch packages"
packages=(
  base base-devel linux linux-firmware linux-headers
  btrfs-progs cryptsetup networkmanager iwd wireless-regdb
  sudo git curl wget iproute2 inetutils limine vim nano man-db
  pipewire pipewire-pulse pipewire-alsa wireplumber
  nvidia-utils nvidia-open docker openssh efibootmgr intel-ucode amd-ucode
  gptfdisk parted dosfstools dialog rsync ntfs-3g
  python python-pip cmake ninja pkgconf which procps-ng pciutils
  ca-certificates openssl zlib xz tk readline sqlite bzip2 ncurses gdbm libffi
  libjpeg-turbo libpng libtiff libwebp freetype2 openjpeg2 lcms2
  ffmpeg jack2 mesa libglvnd dos2unix nodejs npm python-pygame python-numpy
)
pacman -Sw --noconfirm --cachedir "$PAYLOAD/pacman" "${packages[@]}"
rm -f "$PAYLOAD/pacman"/*.part
mapfile -t package_files < <(
  find "$PAYLOAD/pacman" -maxdepth 1 -type f -name '*.pkg.tar.*' ! -name '*.sig' -print
)
((${#package_files[@]})) || { echo "no pacman packages were downloaded" >&2; exit 1; }
repo-add "$PAYLOAD/pacman/tsos.db.tar.zst" "${package_files[@]}"
disk

log "Bundling source repositories"
bundle_repo() {
  local url=$1 name=$2 dir="$WORK/git-$2"
  git clone --mirror "$url" "$dir"
  git -C "$dir" bundle create "$PAYLOAD/bundles/$name.bundle" --all
  rm -rf "$dir"
}
bundle_repo https://github.com/pyenv/pyenv.git pyenv
bundle_repo https://github.com/comfyanonymous/ComfyUI.git ComfyUI
bundle_repo https://github.com/city96/ComfyUI-GGUF.git ComfyUI-GGUF

log "Caching CPython and Monaco"
curl -fL --retry 3 -o "$PAYLOAD/python/Python-${PYTHON_VERSION}.tar.xz" \
  "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz"
python "$ROOT/tabbyAPI/ui/fetch_monaco.py"

log "Preparing Python 3.12 wheelhouse"
export PYENV_ROOT="$WORK/pyenv"
git clone "$PAYLOAD/bundles/pyenv.bundle" "$PYENV_ROOT"
export PATH="$PYENV_ROOT/bin:$PATH"
export PYTHON_BUILD_CACHE_PATH="$PAYLOAD/python"
pyenv install -s "$PYTHON_VERSION"
PY="$PYENV_ROOT/versions/$PYTHON_VERSION/bin/python"
"$PY" -m pip install -U pip
"$PY" -m pip download --dest "$PAYLOAD/wheels" \
  pip setuptools wheel packaging \
  "$ROOT/tabbyAPI[cu12,extras]" "numpy>=2.1.0"

COMFY="$WORK/ComfyUI"
GGUF="$WORK/ComfyUI-GGUF"
git clone "$PAYLOAD/bundles/ComfyUI.bundle" "$COMFY"
git clone "$PAYLOAD/bundles/ComfyUI-GGUF.bundle" "$GGUF"
"$PY" -m pip download --dest "$PAYLOAD/wheels" \
  --index-url https://download.pytorch.org/whl/cu130 \
  torch torchvision torchaudio
"$PY" -m pip download --dest "$PAYLOAD/wheels" -r "$COMFY/requirements.txt"
"$PY" -m pip download --dest "$PAYLOAD/wheels" -r "$GGUF/requirements.txt"
disk

log "Adding tabbyapi-stack and Docker images"
mkdir -p "$PAYLOAD/tabbyapi-stack"
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
if [[ -n "${TSOS_DOCKER_TAR:-}" && -f "$TSOS_DOCKER_TAR" ]]; then
  cp "$TSOS_DOCKER_TAR" "$PAYLOAD/docker/codebox-images.tar"
elif docker info >/dev/null 2>&1; then
  docker pull debian:bookworm-slim
  docker build -t tabbyapi-stack-code:local "$ROOT/tabbyAPI/ui/codebox"
  docker save -o "$PAYLOAD/docker/codebox-images.tar" \
    debian:bookworm-slim tabbyapi-stack-code:local
else
  echo "Docker daemon unavailable; set TSOS_DOCKER_TAR to a prebuilt docker save archive." >&2
  exit 1
fi

log "Overlaying archiso profile"
mkdir -p \
  "$PROFILE/airootfs/opt/tsos" \
  "$PROFILE/airootfs/usr/local/bin" \
  "$PROFILE/airootfs/etc/profile.d"
mv "$PAYLOAD"/* "$PROFILE/airootfs/opt/tsos/"
install -m 0755 "$ROOT/tsos-installer.sh" \
  "$PROFILE/airootfs/usr/local/bin/tsos-installer.sh"
cat >"$PROFILE/airootfs/etc/profile.d/tsos-iso.sh" <<'EOF'
export TSOS_OFFLINE_ROOT=/opt/tsos
export TABBY_LOCAL_SRC=/opt/tsos/tabbyapi-stack
if [[ $- == *i* && $(id -u) -eq 0 && -z ${TSOS_ISO_BANNER_SHOWN:-} ]]; then
  export TSOS_ISO_BANNER_SHOWN=1
  printf '\nTSOS offline installer ISO\n  Run: tsos-installer.sh\n\n'
fi
EOF
for package in dialog rsync; do
  grep -qxF "$package" "$PROFILE/packages.x86_64" || echo "$package" >>"$PROFILE/packages.x86_64"
done
sed -i \
  -e 's/^iso_name=.*/iso_name="tsos-archlinux"/' \
  -e "s/^iso_label=.*/iso_label=\"TSOS_$(date -u +%Y%m%d)\"/" \
  "$PROFILE/profiledef.sh"

log "Building ISO"
rm -rf "$WORK/pyenv" "$COMFY" "$GGUF"
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
    echo "ISO verification failed: offline tabbyapi-stack payload is missing" >&2
    exit 1
  }
grep -q 'squashfs-root/opt/tsos/pacman/tsos.db' "$VERIFY/airootfs.list" || {
    echo "ISO verification failed: frozen pacman repository is missing" >&2
    exit 1
  }
sha256sum "$OUT/tsos-archlinux.iso" >"$OUT/SHA256SUMS"
log "Built $OUT/tsos-archlinux.iso"
disk
