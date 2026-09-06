# Arch Linux install

The git tree is **code only**. It does not ship LLMs, Flux, or Qwen-Image weights. `install.sh` copies a file from an optional local cache if that file already exists, otherwise it downloads it from Hugging Face. Re-run skips anything already on disk.

Stack overview lives in the [repository root README](../../../README.md).

To preserve installer dependencies against disappearing upstream downloads,
the repository can build a [frozen bootable TSOS ISO](../../../iso/README.md).
It includes Arch packages, Python wheels, source bundles, Monaco, and the Code
sandbox image. Model weights remain a separate USB cache; Omarchy remains
network-only.

After install you do not need this chat. The same how-to is written to `HOW-TO-ARCH.txt` next to TabbyAPI. Command output from the work phase is in `$DEST/tabby-install.log`.

## 1. Fresh machine (GitHub)

From the official Arch live ISO, `tsos-installer.sh` starts with **Simple** setup. A review menu lists disk, hostname, username, weights source, and this PC vs LAN — open a row to change it, then start the install. It does not ask about Omarchy and does not install it. Disk encryption is off unless you pass `--encrypt`. After you type the disk path to confirm the wipe, the same dialog stays up: a progress bar, elapsed time, and a live log while Arch and `install.sh` run. `install.sh` does not open a second dialog. It does not reboot until that finishes. After reboot, linger starts the API. There is no first-boot `install.sh`.

Choose **Advanced** for a review menu that also covers encryption, Omarchy (`now` requires LUKS, or `skip`), extra models, bind address, public URL, and SSH tunnel.

```bash
curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash
```

On an already-installed Arch machine, run as **your user**, not root. Needs an NVIDIA GPU and internet.

```bash
sudo pacman -S --needed git
git clone https://github.com/styelz/tabbyapi-stack-archlinux.git "$HOME/tabbyapi-stack"
cd "$HOME/tabbyapi-stack"
bash install.sh
```

Clone into `$HOME/tabbyapi-stack` so this folder *is* the git checkout. A leftover `tabbyapi-stack-archlinux` clone is optional; if you still clone elsewhere, the installer copies the tree (including `.git`) into the dest you pick.

The installer is a how-to as well as a script. On a terminal it uses **dialog** (ncurses menus). If `dialog` is missing it installs it, or falls back to printed questions. Each screen explains what is needed and gives examples. Esc on the review menu cancels; Esc on a setting goes back. After you confirm, the work stays in that same installer: a **progress bar**, elapsed time, and a live tail of `$DEST/tabby-install.log`. Set `TABBY_INSTALL_VERBOSE=1` to print every command on the console instead.

**Simple** (recommended) opens a review menu:

1. **Setup type** — Simple or Advanced (Simple is the default)
2. **Review** — disk, hostname, username, weights source, this PC vs LAN. Open a row to change it, then Start install.
3. **Confirm wipe** — type the disk path. Password is next (login only; the disk is not encrypted).

From the live ISO, Simple does not show Omarchy and does not install it.

**Advanced** uses the same review menu plus locale, encryption, Omarchy, models, bind address, public URL, and SSH tunnel. Flags: `--simple` / `--advanced`, or `INSTALL_MODE=simple|advanced`.

Non-interactive (no menus):

```bash
TABBY_INSTALL_ROOT="$HOME/tabbyapi-stack" TABBY_CACHE="" TABBY_MODELS=core \
  TABBY_NETWORK_HOST=127.0.0.1 TABBY_NETWORK_PORT=5000 \
  COMFYUI_URL=http://127.0.0.1:8188 \
  TABBY_PUBLIC_BASE="" TABBY_SSH_REMOTE="" \
  bash install.sh
```

Or `TABBY_NONINTERACTIVE=1` with the same variables.

Gemma downloads may be gated. If Hugging Face returns 401/403: `huggingface-cli login` or `export HF_TOKEN=...`.

## 2. Optional: reuse weights you already have

If a USB copy of this tree (or another folder with the model weights) is mounted, point the cache at it. Existing folders are copied; missing ones still download.

```bash
sudo pacman -S --needed ntfs-3g
sudo mkdir -p /mnt/usb
sudo mount /dev/sdXN /mnt/usb
# You should see /mnt/usb/tabbyapi-stack/tabbyAPI

bash install.sh
# Weights cache = /mnt/usb/tabbyapi-stack
```

Do not reuse Windows `venv` folders.

Catalog of Hugging Face repos: [`models.json`](models.json).

It will:

- Install `sudo` if missing (asks for the root password once) and give your user passwordless sudo
- Install pacman packages (NVIDIA userspace, git, rsync, openssh, build tools, `dos2unix`, image/FFmpeg libs)
- Install **Python 3.12** via pyenv 3.12.5 if `python3.12` is not on PATH (does not use system 3.13/3.14)
- Skip any weight that already exists; copy from the cache if present; otherwise download
- Clone **ComfyUI** and **ComfyUI-GGUF**
- Install Tabby **extras**, pin **numpy ≥ 2.1**, and keep **Qwen3-Embedding-0.6B** on CPU
- Create Linux venvs and `pip install .[cu12]` plus ComfyUI + CUDA Torch
- Patch Linux spawn / chat-switch so `switch to …` does not 500 or look like Comfy
- Set the startup model to **qwen 9B** and `embedding_model_name` to **Qwen3-Embedding-0.6B**
- Enable **linger** + `tabbyapi` so it **starts at boot with no login**
- If a newly installed NVIDIA driver does not load on a running system, reboot once and resume automatically. `tsos-installer.sh` runs `install.sh` in the live-ISO chroot and will not reboot if that fails. Venv checks require CUDA-built torch wheels, not `torch.cuda.is_available()` (no driver in the chroot). After a successful reboot, linger starts the API. Simple setup never installs Omarchy. Advanced can choose `now` (LUKS required) or `skip`.
- Write `$DEST/start.sh` at the install root
- Write `$DEST/AGENTS.md` (IDE / agent notes for any editor)
- Write `HOW-TO-ARCH.txt`

It does **not** install the full `cuda` toolkit. Torch and ExLlamaV3 wheels already include CUDA 12.8.

It uses **Python 3.12 only**. Official Arch `python` is 3.14, and `python312` is AUR-only, so if `python3.12` is missing it installs **pyenv 3.12.5** (same workaround as the first Arch boot). It will not use system 3.13/3.14.

Fresh Arch often has **no sudo**. Run as your user (not root). The script asks for the root password once, installs `sudo`, and gives your user passwordless sudo (`/etc/sudoers.d/zz-tsos-nopasswd`) so Settings and `tsctl` can run `systemctl` without a TTY.

If `nvidia-smi` is missing it installs `nvidia-open` (Arch dropped the proprietary `nvidia` package). It will not request `nvidia` unless that name still exists in the repos.

If you mounted a USB cache you can unmount it after a successful install.

If this was the first NVIDIA driver install, **reboot once**. Linger will start TabbyAPI at boot; you do not need to log in.

## 3. Start TabbyAPI

The installer enables **linger** and `tabbyapi`. After a reboot it starts **without a login**. Check: `loginctl show-user $USER -p Linger` should be `yes`.

You only need these if linger was not set or you stopped the unit:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user enable --now tabbyapi
systemctl --user status tabbyapi
```

- API: `http://127.0.0.1:5000`
- Health: `GET /health` on that origin
- Management UI: `http://127.0.0.1:5000/v1/ui` — sign in with the Linux account that runs the stack (admin), or a Tabby-only account created on the Users page. Chat (no file tools), Code (browser IDE: Chat Completions plus Grep/Glob/Read/Write/Shell on a jailed workspace, Monaco, preview, per-chat container terminal), Status (occupancy, GPU, restart, Update git / Update all), Gallery, Logs, and Settings (`config.yml` and `tabby.env`, admin). Extra users cannot create accounts. Through an SSH forwarder use the same `/v1/ui` path under your API prefix.
- OpenAI-compatible base URL for remote IDEs: `http://<gpu-host>:5000/v1` (model name **`gpt-4o`** — leave it)
- Agent / IDE notes: `$HOME/tabbyapi-stack/AGENTS.md` (copied by the installer)
- A public reverse tunnel is optional. Set `TABBY_PUBLIC_BASE` and `TABBY_SSH_REMOTE` in `deploy/arch/tabby.env` if you have one. Every env key is listed in [`tabby.env.example`](tabby.env.example).

Do **not** run `start.bat`. On Arch use the unit above or `$HOME/tabbyapi-stack/start.sh` at the install root.

Stop:

```bash
systemctl --user stop tabbyapi
```

Logs:

```bash
journalctl --user -u tabbyapi -f
```

Manual start (same as the unit):

```bash
/path/to/tabbyAPI/venv/bin/python /path/to/tabbyAPI/watch_api.py
```

### TTY activity screensaver

A CPU-rendered KMS kiosk on a spare TTY that paints GPU / occupancy as a thermal field. It is **not** a real attention heatmap. **Do not enable this if Omarchy or any graphical session already owns the GPU.** Fresh TTY/TSOS installs enable it by default and always install `python-pygame`, `python-numpy`, and the `video`/`input`/`tty` groups. Advanced can turn it off; Omarchy installs leave it off.

```bash
tsctl screensaver enable
tsctl screensaver timeout=120
tsctl screensaver logout-timeout=10
tsctl screensaver hud-timeout=300
tsctl screensaver status
# or: Settings → Screensaver in /v1/ui
```

- Runs on **tty8** by default so **tty1** stays a login prompt. A key or mouse drops the field while the idle clock is up; after the idle-text timeout (default **5 minutes**) the clock and labels fade out (mouse motion brings them back for that same hold, with 1 second before dismiss is armed again). **0** hides the idle text. **2 minutes** idle while logged in, or **10 seconds** after logout / at getty, starts the field again. Timeouts live in `tabby.env` (`TABBY_SAVER_IDLE_S`, `TABBY_SAVER_LOGOUT_IDLE_S`, `TABBY_SAVER_HUD_S`).
- Needs `python-pygame`, `python-numpy`, `nvidia-drm.modeset=1`, and a connector on `/dev/dri/card*`.
- Software SDL only (`SDL_VIDEODRIVER=kmsdrm`, `SDL_RENDER_DRIVER=software`) so it does not steal LLM VRAM.
- Feed: `GET http://127.0.0.1:5000/v1/ui/saver/state` — localhost only; no usernames. The current LLM ask or Comfy prompt is included for the HUD.
- Windowed probe: `/usr/bin/python "$HOME/tabbyapi-stack/tabbyAPI/deploy/arch/tabby-saver.py" --window`
- Stop: `tsctl screensaver disable`

### tsctl

`tsctl` is installed to `/usr/local/bin/tsctl`. Bare `tsctl` opens a dialog menu (or a readline shell). The same keys as Settings:

```bash
tsctl list
tsctl network host=0.0.0.0
tsctl screensaver enable
tsctl gpu status
tsctl gpu quiet
tsctl gpu auto
tsctl gpu fan_speed=40
```

### GPU fan / power

`tsctl gpu` (and Settings → GPU) writes `tabby.env` and the `tabby-gpu` systemd unit applies it as root via NVML. `auto` leaves NVIDIA idle fan-stop. `quiet` / `balanced` / `performance` are temperature curves (`quiet` also lowers the power limit). `custom` uses `fan_speed` percent (manual floor is often 30%). Power limit `0` means the profile default. Persistence mode is optional (`tsctl gpu persistence=on`).

Needs passwordless sudo (the installer writes that). Fan control is not available as a normal user.

```bash
tsctl gpu status
journalctl -u tabby-gpu -e
```

## 4. IDE / agents

Chat phrases, images, and mixed page+images: `$HOME/tabbyapi-stack/AGENTS.md` (copied by the installer). Editor model name is **`gpt-4o`** (a label only).

- One Chat Completions API, two clients. **Editor:** your disk and your tools at `/v1`. **Browser Code:** jailed project on this host; the page runs the tool loop. Do not send Tabby workspace tools from an editor.
- Browser Chat and Code live at `/v1/ui`. Editor mixed page+images still follow AGENTS.md (your file tools, then one Shell curl of GPU URLs). Browser Code copies finished PNGs into the workspace.
- After `switch to …`, wait for the GPU (warm RTX 4070 Ti 12 GB: qwen / gemma ~65s, qwen36 ~85s, gemma26 ~2 min, qwen35 ~3 min, glm ~15s). After `switch to comfy`, wait about **35 seconds** (first Flux ~3 min, first Qwen-Image ~4 min). GLM is thinking chat only on RTX 4070 Ti 12 GB (no coding tools; vision off). You can also load an LLM or hand the GPU to Comfy from the Status page in `/v1/ui`.

## 5. Update

The install root is the git checkout. On the GPU host:

```bash
bash "$HOME/tabbyapi-stack/update.sh"
```

A dialog asks **Update git** vs **Update all** (or `--git` / `--all`). Update git is a pull only; at the end a **Restart** / **Skip** dialog can bounce the unit and wait until `GET /health` is healthy (~65s), including when the tree was already up to date. Pass `--restart` to skip that prompt and bounce the API, or `--no-restart` to leave it running. Update all then runs `install.sh --update` (pip -U, skip existing weights, rewrite systemd units), shows the same progress gauge as install, restarts `tabbyapi`, and waits for health. If `update.sh` changed in the pull, that new script is executed again with the same choice. It does not overwrite `config.yml` or `tabby.env`, and it does not run `pacman -Syu` (keep the OS updated yourself). Missing stack packages are installed only on Update all; already-installed ones are left alone. The Status page in `/v1/ui` can trigger the same Update git / Update all actions.

- First run on an older rsync-only dest bootstraps `.git` from `https://github.com/styelz/tabbyapi-stack-archlinux.git`.
- `--comfy` also pulls ComfyUI and ComfyUI-GGUF. Leave that off unless you want image-gen to move with upstream.
- Tracked copy-to-live files are moved aside under `.tabby-update-backup/` and origin wins. Untracked `venv/`, `models/`, and `ComfyUI/` are ignored.

A leftover `tabbyapi-stack-archlinux` clone next to the install is optional after this.

## 6. If something fails

| Problem | What to do |
|---|---|
| `nvidia-smi` fails after a new driver | Standalone `install.sh` reboots once and resumes. `tsos-installer.sh` finishes `install.sh` in the ISO chroot and does not reboot on failure; after a successful reboot linger starts the API. If the driver still fails: `nvidia-smi` and `journalctl -k \| grep -i nvidia` |
| `TabbyAPI venv check failed` on the ISO | Expected if an old `install.sh` required `torch.cuda.is_available()`. Current `install.sh` only requires CUDA-built wheels in the chroot. Re-run with this tree (`--tabby-local-src` or the script next to `install.sh`). |
| `cannot open tty output` after Enter on the first install screen | `arch-chroot` + `su` has no `/dev/tty` for `dialog --stdout`. Current `install.sh` skips menus in a chroot. Copy this `install.sh` into `/mnt/home/USER/tabbyapi-stack/` and run `cd ~/tabbyapi-stack && ./install.sh`, or `runuser -u USER -- env TABBY_NONINTERACTIVE=1 TABBY_SKIP_NVIDIA_REBOOT=1 bash .../install.sh`. |
| USB NTFS read-only / dirty | `sudo ntfsfix /dev/sdXN` then remount |
| Missing model folder | Re-run `install.sh`. It downloads from Hugging Face and skips files that already exist. |
| Hugging Face 401/403 | Gated repo. `huggingface-cli login` or `export HF_TOKEN=...` then re-run. |
| SSH key missing | Optional. Only needed if you set up your own public reverse tunnel. |
| SSH rejects a cache-copied key | Windows CRLF. The installer runs `dos2unix`. Manual: `sudo pacman -S dos2unix && dos2unix ~/.ssh/id_ed25519` |
| `~/.ssh` empty after ISO install | There was no cache key to copy, and older `install.sh` did not create one. Current `install.sh` generates `id_ed25519`. On an already-installed box: `ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519` and put the `.pub` on `TABBY_SSH_REMOTE`. |
| `systemctl --user` fails | Log in graphically or `export XDG_RUNTIME_DIR=/run/user/$(id -u)` |
| Tabby dies when you log out of the shell | User units need linger. `sudo loginctl enable-linger $USER` then `loginctl show-user $USER -p Linger` should be `yes` |
| No sudo / not in wheel | Re-run as your user; enter the root password when asked. The installer then writes passwordless sudo. Or: `su -c 'pacman -S sudo && usermod -aG wheel USER'` |
| System Python is 3.13/3.14 | Expected. Re-run `install.sh` — it clones pyenv from GitHub and builds 3.12.5. Do not `pacman -S python312` (not in official repos). |
| `curl: (6) Could not resolve host: pyenv.run` | The short domain often fails DNS in the live-ISO chroot. Current `install.sh` clones `github.com/pyenv/pyenv`. Do not re-run `tsos-installer.sh` (that wipes the disk). Resume: `tsos-installer.sh --resume-tabby`, or copy this `install.sh` into `/mnt/home/USER/tabbyapi-stack/` and re-run `install.sh` in the chroot. |
| `mkfs.btrfs: Device or resource busy` on the root partition | A previous tsos run in this live session left the kernel holding btrfs after umount (lsblk looks clean). Current tsos (1.0.46) unloads that. Copy the new `tsos-installer.sh` onto the ISO and run it again, or reboot the live USB once then install. Do not use `--resume-tabby` here — that is only after Arch is already mounted at `/mnt`. |
| Interrupted download | Re-run `install.sh`; finished files are skipped |
| Chat `switch to …` returns 500 / `creationflags is only supported on Windows` | Re-run `install.sh` (it patches the spawn), then `systemctl --user restart tabbyapi` |
| Reply says `ComfyUI is not running` after a chat or `switch to qwen` | That was a missing LLM, not Flux. Re-run `install.sh` (it now defaults to qwen 9B), then `systemctl --user restart tabbyapi` and wait ~65s |
| First start hangs / no `:5000` | Model is loading before the port opens. qwen ~65s; qwen35 ~3 min. First Linux boot may compile Triton. |
| `tabby-gpu` fan stays on auto | Needs passwordless sudo and `/etc/systemd/system/tabby-gpu.service`. `tsctl gpu status` and `journalctl -u tabby-gpu -e`. GeForce fan writes are root NVML only. |

`update.sh` is the usual way to pull new code. Re-running `install.sh` is still safe for missing weights: it skips files that already exist. A healthy venv is rebuilt only when `update.sh` runs (pip -U).
