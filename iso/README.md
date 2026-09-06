# TSOS Arch Linux ISO

The ISO workflow builds a bootable Arch live installer named
`tsos-archlinux.iso`. It is a **small** image: the live system plus
tabbyapi-stack. Arch packages, Python, PyTorch, ComfyUI, Docker, and model
weights download when you run `tsos-installer.sh`.

The only frozen 3rd-party source is **ComfyUI-GGUF**, which is small and not
as durable as Arch, python.org, PyPI, or the official ComfyUI repo.

Older **frozen** 9 GiB releases (split `tsos-archlinux.iso.part-*` files)
remain available. New tags build the small ISO only.

## Build a release

Run **Build TSOS ISO** from the repository's Actions page, or push a tag
whose name starts with `iso-`:

```bash
git tag iso-$(date -u +%Y%m%d)
git push origin --tags
```

The workflow publishes a GitHub Release with `tsos-archlinux.iso` and
`SHA256SUMS`.

## Download and verify

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

If you still have a split frozen Release:

```bash
cat tsos-archlinux.iso.part-* > tsos-archlinux.iso
sha256sum -c SHA256SUMS --ignore-missing
```

## Put it on USB

`dd` erases the selected device. Use the whole USB device, not a partition:

```bash
lsblk
sudo dd if=tsos-archlinux.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Alternatively, copy the ISO to a Ventoy USB.

Boot the USB with Secure Boot disabled, connect to the network, and run:

```bash
tsos-installer.sh
```

## What is on the ISO

- A current Arch live environment (releng)
- The tabbyapi-stack tree and a git bundle of this repository
- `ComfyUI-GGUF` as a git bundle

## Downloaded at install time

- Arch packages (kernel, NVIDIA, Docker, …)
- pyenv and CPython 3.12 from python.org
- TabbyAPI / ComfyUI Python packages and CUDA PyTorch
- Official ComfyUI
- `debian:bookworm-slim` and the Code sandbox image (`docker build`)
- Model weights from Hugging Face or a USB cache you select

Omarchy still needs a network. The NVIDIA 580xx AUR driver for pre-Turing
GPUs is not included.

## Local build

Root on current Arch Linux:

```bash
sudo iso/build.sh
```

Output is written to `out/`.
