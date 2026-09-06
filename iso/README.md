# TSOS installer ISO

Boot this USB and it installs Arch Linux plus tabbyapi-stack. You need a network during install (packages, Python, models).

## Download

Get `tsos-archlinux.iso` from the latest GitHub Release, then:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

## Write to USB

This erases the whole device (`/dev/sdX`, not a partition):

```bash
lsblk
sudo dd if=tsos-archlinux.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Ventoy works too. Disable Secure Boot.

## Install

The installer starts on the first console. Plug in Ethernet (or set up Wi-Fi from Alt+F2 with `iwctl`). When it finishes, reboot.

If you quit it, a root shell is left; run `tsos-installer.sh` to start again.

## Build (optional)

On Arch, as root, from a tabbyapi-stack checkout:

```bash
sudo iso/build.sh
```

The image lands in `out/`. Releases are also built from the **Build TSOS ISO** Action.
