# tabbyapi-stack

A self-hosted coding assistant, web workspace, and image generator for an Arch Linux machine with an NVIDIA GPU.

Use it from Cursor, VS Code, Continue, Cline, another OpenAI-compatible client, or the built-in browser IDE. Editors keep their own tools and files. Prompts, project files, and generated images stay on hardware you control.

## Install

**New machine:** boot the [TSOS installer ISO](iso/README.md). The installer starts on its own.

**Official Arch live USB:**

```bash
curl -fsSL https://raw.githubusercontent.com/styelz/tabbyapi-stack-archlinux/main/tsos-installer.sh | bash
```

**Already running Arch** (NVIDIA GPU, internet). Run as your user, not root:

```bash
sudo pacman -S --needed git
git clone https://github.com/styelz/tabbyapi-stack-archlinux.git "$HOME/tabbyapi-stack"
cd "$HOME/tabbyapi-stack"
bash install.sh
```

The menu defaults to **Simple** (this PC vs LAN, core models from Hugging Face). Choose **Advanced** for encryption, extra models, bind address, public URL, reverse SSH, Omarchy, or to turn the screensaver off.

Re-run is safe: existing weights are skipped. USB caches, unattended install, and recovery: [Arch install guide](tabbyAPI/deploy/arch/README.md).

## What you get

- An OpenAI-compatible API for local chat, tool use, vision, and embeddings
- Switchable language-model profiles tuned for a 12 GB NVIDIA card
- Flux Schnell and Qwen-Image through ComfyUI
- A browser UI with Chat, Code, Status, Gallery, Logs, and user accounts

The language model and ComfyUI share one GPU. The stack unloads one before starting the other. The CPU embedding model can stay loaded.

## Start and sign in

The API starts at boot, even before login.

```bash
curl -sS http://127.0.0.1:5000/health
systemctl --user status tabbyapi
```

Open `http://127.0.0.1:5000/v1/ui` on the GPU host (or `/v1/ui` under a public URL you configured). Sign in with the Linux account that installed the stack.

That first account is the administrator. **Users** creates extra Tabby-only accounts (not Linux users). Chats, Code projects, and images are per account. Each account can **Download backup** / **Restore backup** from the account menu.

## Browser UI

**Chat** is the same Chat Completions pipeline as an editor, without file tools. **Code** is a project folder on this host (Monaco, file tools, preview, container terminal). **Agent** can write; **Ask** and **Plan** only inspect.

| Page | What it is for |
|---|---|
| **Status** | Profile, GPU mode, queue, health, restart, updates |
| **Gallery** | Generated images (administrators see every account) |
| **Logs** | TabbyAPI and ComfyUI output |
| **Users** | Administrator: create, reset, or delete Tabby accounts |
| **Settings** | Administrator: `config.yml`, `tabby.env`, screensaver, GPU. Shell: `tsctl` |

<img width="1919" height="1122" alt="Chat" src="https://github.com/user-attachments/assets/03456b83-b6a5-46e9-a96f-9d752ed34fdb" />

<img width="1919" height="1123" alt="Code" src="https://github.com/user-attachments/assets/11dca1dd-7036-4a11-9c12-043e417014e7" />

<img width="1919" height="1123" alt="Status" src="https://github.com/user-attachments/assets/1b7ab6fe-6a12-4947-b1bd-23bae0175447" />

## Connect an editor

| Setting | Value |
|---|---|
| Base URL | `http://<gpu-host>:5000/v1` on a trusted LAN/Tailscale network, or your HTTPS `/v1` URL |
| Model name | `gpt-4o` |
| API key | Your UI login password |

Leave the model name as **`gpt-4o`**. It is only a label so editors keep tool support; inference still uses the local profile from `list models` or Status. Do not send Tabby workspace tools from an editor.

Some clients require HTTPS. Advanced install can set a reverse SSH tunnel to a host that already has a certificate. Details: [Arch install guide](tabbyAPI/deploy/arch/README.md).

![Using help, switching models, generating an image, and building a page](docs/ide-chat.gif)

## Commands

Send the whole message as the command (`switch to qwen`, or `please switch to qwen`).

| Message | Result |
|---|---|
| `help` | In-chat user guide |
| `list models` | Installed profiles; marks the loaded one |
| `restart` | Restart the API; reload the last language model |
| `switch to qwen` | Everyday coding profile |
| `switch to qwen35` / `switch to qwen36` | Larger profile for long or difficult work |
| `switch to gemma` / `switch to gemma26` | General-purpose profile |
| `switch to glm` | Thinking chat (no coding tools) |
| `switch to comfy` / `switch to flux` | Unload the language model; start image generation |
| `switch to llm` | Stop ComfyUI; restore the last language model |

Only installed profiles appear in `list models`. On an RTX 4070 Ti 12 GB, a warm switch is about 15 seconds to 3 minutes. First boot can take longer while Triton compiles.

## Images

```text
generate an image of a neon diner on a rainy street at night
```

The GPU moves to ComfyUI, the image URL comes from this same server, then the previous language model reloads. For several images in a row: `switch to comfy`, send prompts, then `switch to qwen`.

- Flux Schnell is the default for photos and drafts.
- Prefix `qwen-image:` for logos, posters, or readable text.
- Attach a photo in the same message for Flux img2img.
- Files also appear in **Gallery**.

`POST /v1/images/generations` returns `b64_json` and a URL. Editor agents that mix pages and images: [AGENTS.md](AGENTS.md).

## Update

On the GPU host:

```bash
bash "$HOME/tabbyapi-stack/update.sh"
```

**Update git** pulls code. **Update all** also refreshes dependencies and restarts. Status can do the same. Config, `tabby.env`, weights, and the venv stay. This is not a full Arch upgrade.

## More

- [Arch install and troubleshooting](tabbyAPI/deploy/arch/README.md)
- [Editor and agent notes](AGENTS.md)
- [Upstream TabbyAPI](tabbyAPI/README.md)

## License

TabbyAPI is [AGPL-3.0](LICENSE), matching [upstream TabbyAPI](https://github.com/theroyallab/tabbyAPI). ComfyUI, custom nodes, and model weights keep their own licenses.
