# Frozen TSOS Arch Linux ISO

The ISO workflow builds a bootable Arch live installer named
`tsos-archlinux.iso`. It preserves the package and source dependencies used by
`tsos-installer.sh` and `install.sh` at build time.

## Build a release

Run **Build frozen TSOS ISO** from the repository's Actions page, or push a tag
whose name starts with `iso-`:

```bash
git tag iso-$(date -u +%Y%m%d)
git push origin --tags
```

The workflow publishes a GitHub Release. GitHub limits individual Release
assets to 2 GiB, so large ISOs are uploaded as ordered parts.

## Download and verify

Download every `tsos-archlinux.iso.part-*` file and `SHA256SUMS` from the same
Release, then join and verify them:

```bash
cat tsos-archlinux.iso.part-* > tsos-archlinux.iso
sha256sum -c SHA256SUMS --ignore-missing
```

If the Release contains `tsos-archlinux.iso` directly, no join is needed.

## Put it on USB

`dd` erases the selected device. Use the whole USB device, not a partition:

```bash
lsblk
sudo dd if=tsos-archlinux.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Alternatively, copy the ISO to a Ventoy USB. Use exFAT rather than FAT32 when
the ISO exceeds 4 GiB.

Boot the USB with Secure Boot disabled and run:

```bash
tsos-installer.sh
```

## Preserved offline

The ISO contains:

- The complete Arch package closure used by the live and target installers
- The current tabbyapi-stack source tree
- Pyenv, CPython 3.12.5, ComfyUI, and ComfyUI-GGUF sources
- TabbyAPI, ComfyUI, CUDA PyTorch, and related Python wheels for CPython 3.12
- Monaco Editor
- The prebuilt `tabbyapi-stack-code:local` Docker image and its Debian base

The installer automatically detects `/opt/tsos`. Its local pacman repository is
used ahead of Arch mirrors, the payload is mounted read-only into the new
system during installation, and the temporary local repository entry is
removed before reboot.

## Not included

Model weights are deliberately not in the GitHub ISO. The core weights alone
are tens of GiB and GitHub-hosted runners and Release assets are unsuitable for
one monolithic image of that size.

For a completely disconnected installation, connect a second drive containing
the existing TSOS weight-cache layout and select it in the installer. Without
that cache, model weights still download from Hugging Face.

Omarchy is also not preserved. Selecting it requires a network connection to
its installer and package sources. The NVIDIA 580xx AUR driver for pre-Turing
GPUs is not included.

## Local build

The supported build environment is root in current Arch Linux with Docker
available:

```bash
sudo iso/build.sh
```

Output is written to `out/`. Building consumes substantial disk space and
network bandwidth. The Action performs the same build inside a privileged Arch
container and verifies that the resulting squashfs contains the frozen pacman
repository and installer tree.
