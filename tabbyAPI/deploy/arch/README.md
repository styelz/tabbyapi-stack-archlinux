# Arch Linux install

Weights are not in git. `install.sh` copies them from a cache if you have one, otherwise it downloads from Hugging Face. Re-run skips files that already exist.

Stack overview: [repository README](../../../README.md). Bootable USB: [TSOS installer ISO](../../../iso/README.md).

After install, a copy of this how-to is `$DEST/tabbyAPI/HOW-TO-ARCH.txt`. The work log is `$DEST/tabby-install.log`.

## Install

**New machine:** boot the [TSOS ISO](../../../iso/README.md).

**Official Arch live USB:**

```bash
curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash
```

**Already running Arch** (NVIDIA GPU, internet). Run as **your user**, not root:

```bash
sudo pacman -S --needed git
git clone https://github.com/styelz/tabbyapi-stack-archlinux.git "$HOME/tabbyapi-stack"
cd "$HOME/tabbyapi-stack"
bash install.sh
```

Clone into `$HOME/tabbyapi-stack` so that folder is the git checkout.

**Simple** (default): disk, hostname, username, weights source, this PC vs LAN. No Omarchy, no disk encryption unless you pass `--encrypt`.

**Simple** includes minimal coding/image models and a GPU-filtered optional-model checklist with disk estimates. **Advanced** adds locale, encryption, Omarchy, full model control, bind address, public URL, and SSH tunnel. Flags: `--simple` / `--advanced`, or `INSTALL_MODE=simple|advanced`.

Non-interactive:

```bash
TABBY_INSTALL_ROOT="$HOME/tabbyapi-stack" TABBY_CACHE="" TABBY_MODELS=core \
  TABBY_NETWORK_HOST=127.0.0.1 TABBY_NETWORK_PORT=5000 \
  COMFYUI_URL=http://127.0.0.1:8188 \
  TABBY_PUBLIC_BASE="" TABBY_SSH_REMOTE="" \
  bash install.sh
```

Or `TABBY_NONINTERACTIVE=1` with the same variables.

Gated Hugging Face downloads: `huggingface-cli login` or `export HF_TOKEN=...`.

The installer asks for the root password once if `sudo` is missing, then writes passwordless sudo so Settings and `tsctl` can call `systemctl`. If this was the first NVIDIA driver install, reboot once; linger starts the API without a login.

## USB cache

If a USB copy of this tree (or another folder with the weights) is mounted, point the cache at it. Existing folders are copied; missing ones still download.

```bash
sudo pacman -S --needed ntfs-3g
sudo mkdir -p /mnt/usb
sudo mount /dev/sdXN /mnt/usb
# You should see /mnt/usb/tabbyapi-stack/tabbyAPI

bash install.sh
# Weights cache = /mnt/usb/tabbyapi-stack
```

Do not reuse Windows `venv` folders. Catalog: [`models.json`](models.json). Unmount the USB after a successful install.

## After install

Linger plus `tabbyapi` start the API at boot. Check: `loginctl show-user $USER -p Linger` should be `yes`.

```bash
sudo loginctl enable-linger "$USER"
systemctl --user enable --now tabbyapi
systemctl --user status tabbyapi
journalctl --user -u tabbyapi -f
```

- API: `http://127.0.0.1:5000`
- UI: `http://127.0.0.1:5000/v1/ui`
- Editor base URL: `http://<gpu-host>:5000/v1` (model name **`gpt-4o`**)
- Env keys: [`tabby.env.example`](tabby.env.example). For a reverse HTTPS tunnel set `TABBY_PUBLIC_BASE` and `TABBY_SSH_REMOTE`.

Do not run `start.bat`. Use the unit or `$HOME/tabbyapi-stack/start.sh`.

```bash
tsctl                         # Settings menu
tsctl list
tsctl network host=0.0.0.0
tsctl screensaver enable      # spare TTY; leave off if a desktop owns the GPU
tsctl gpu status
tsctl gpu quiet
```

Chat phrases and mixed page+images: `$HOME/tabbyapi-stack/AGENTS.md`.

## Update

```bash
bash "$HOME/tabbyapi-stack/update.sh"
```

**Update git** pulls. **Update all** also runs `install.sh --update` and restarts. Status can do the same. This does not overwrite `config.yml` or `tabby.env`, and it does not run `pacman -Syu`.

`--comfy` also updates ComfyUI. Leave that off unless you want image-gen to follow upstream.

## If something fails

| Problem | What to do |
|---|---|
| `nvidia-smi` fails after a new driver | Standalone `install.sh` reboots once and resumes. After a successful reboot, linger starts the API. If it still fails: `nvidia-smi` and `journalctl -k \| grep -i nvidia` |
| `TabbyAPI venv check failed` on the ISO | Current `install.sh` only requires CUDA-built wheels in the chroot. Re-run with this tree. |
| `cannot open tty output` on the first screen | `dialog` has no `/dev/tty` in `arch-chroot`. Current `install.sh` skips menus there. Copy this `install.sh` into `/mnt/home/USER/tabbyapi-stack/` and run it, or use `TABBY_NONINTERACTIVE=1`. |
| USB NTFS read-only / dirty | `sudo ntfsfix /dev/sdXN` then remount |
| Missing model folder | Re-run `install.sh`. It skips files that already exist. |
| Hugging Face 401/403 | `huggingface-cli login` or `export HF_TOKEN=...` then re-run |
| SSH key missing | Only needed for a public reverse tunnel |
| SSH rejects a cache-copied key | Windows CRLF: `sudo pacman -S dos2unix && dos2unix ~/.ssh/id_ed25519` |
| `~/.ssh` empty after ISO install | Current `install.sh` generates `id_ed25519`. Or: `ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519` |
| `systemctl --user` fails | Log in, or `export XDG_RUNTIME_DIR=/run/user/$(id -u)` |
| Tabby dies when you log out | `sudo loginctl enable-linger $USER` |
| No sudo / not in wheel | Re-run as your user; enter the root password when asked |
| System Python is 3.13/3.14 | Expected. Re-run `install.sh` (pyenv 3.12.5). Do not `pacman -S python312` |
| `curl: (6) Could not resolve host: pyenv.run` | Current `install.sh` clones pyenv from GitHub. Do not re-run `tsos-installer.sh` (that wipes the disk). Resume: `tsos-installer.sh --resume-tabby` |
| `mkfs.btrfs: Device or resource busy` | Reboot the live USB once, then install. Do not use `--resume-tabby` until Arch is mounted at `/mnt` |
| Interrupted download | Re-run `install.sh`; finished files are skipped |
| Chat `switch to …` returns 500 | Re-run `install.sh`, then `systemctl --user restart tabbyapi` |
| Reply says `ComfyUI is not running` after `switch to qwen` | Missing LLM. Re-run `install.sh`, restart, wait ~65s |
| First start hangs / no `:5000` | Model is loading. qwen ~65s; qwen35 ~3 min. First boot may compile Triton |
| `tabby-gpu` fan stays on auto | Passwordless sudo and `tabby-gpu.service`. `tsctl gpu status` |

`update.sh` is the usual way to pull new code. Re-running `install.sh` is still safe for missing weights.
