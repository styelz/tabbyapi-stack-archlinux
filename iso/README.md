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

After the boot menu, the live initramfs uses fast Zstd decompression to shorten
the kernel handoff where no userspace can draw. Plymouth then starts on the
UEFI SimpleDRM framebuffer from the first initramfs hook, before udev enumerates
hardware, so the centered TSOS logo and spinner cover early userspace. They
stay up until the first installer question. Plug in Ethernet (or set up Wi-Fi
from Alt+F2 with `iwctl`). The first menu is Simple, Advanced, or Restore from
backup (a Status
/ `tsctl` folder on USB). Choose **Mount a drive or device** there to mount a
backup or model-weight filesystem under `/run/media/tsos`, then return to the
setup menu. The weights picker lists mounted storage and likely model folders,
then opens the selected path for editing. Restore searches mounted storage for
valid stack backup manifests, including backups in dated subfolders. When it
finishes, reboot.

If you quit it, a root shell is left; run `tsos-installer.sh` to start again.

## Build (optional)

On Arch, as root, from a tabbyapi-stack checkout:

```bash
sudo iso/build.sh
```

The image lands in `out/`. Releases are also built from the **Build TSOS ISO** Action.
