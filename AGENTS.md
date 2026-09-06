# Agent / IDE notes (tabbyapi-stack)

Use **gpt-4o** as the model name in your editor, and leave it. That is not ChatGPT — it is only a name. Many editors sandbox or block tools unless they see a known OpenAI name. The GPU still runs the local model you switched to.

This file is for **any editor** that talks to the TabbyAPI server, and for anyone using the browser UI. There is one Chat Completions API. The GPU server is not the coding agent: the *client* sends `tools`, runs `tool_calls`, and POSTs `role: tool` results.

- **Editor** — project on the computer that runs the editor. Keep **your** tools. Do not send Tabby workspace tools.
- **Browser Code** — project on this GPU host. The page is the agent; tools hit the jailed workspace.

Treat the API like OpenAI: chat and HTTP. Some editors only accept `https://`; that is why a reverse SSH tunnel from an HTTPS host back to this API exists.

## API

- Base URL: the `/v1` URL you configured (LAN HTTP, Tailscale, or HTTPS via the reverse SSH tunnel)
- Model name: **`gpt-4o`** (leave it)
- API key: your UI login password — the Linux account password for the stack admin, or the password the administrator set for a Tabby-only user. Optional extra keys can still live in `tabbyAPI/api_tokens.yml`.
- Health: `GET /health` on the same origin
- Browser UI: `/v1/ui` on that same origin. Sign in with the Linux account that runs the stack (admin), or a Tabby-only account that admin created.
  - **Chat** — same Chat Completions pipeline as an editor, without file tools (searchable history, follow-up queue)
  - **Code** — a self-contained IDE on this host. The browser calls Chat Completions, then runs Grep/Glob/Read/Write/Shell against a jailed workspace (Monaco, preview, container terminal, zip). Nested chats under a workspace share the same files. **Agent** can write; **Ask** and **Plan** are read-only (Grep, Glob, Read, List)
  - **Status** — profile, GPU mode, occupancy, health, graphs, restart, and updates. **Update git** pulls `origin/main` into `$HOME/tabbyapi-stack` (that folder is the running checkout of `main`, not a second source tree). **Update all** also refreshes Python deps and restarts. Do not commit or push on the live install; its git hooks refuse those.
  - **Gallery** — generated images (the administrator can see every account)
  - **Logs** — live TabbyAPI and ComfyUI output
  - **Users** — administrator-only account creation. Extra users get Chat, Code, Status, Gallery, and Logs; they cannot create accounts
  - **Settings** — administrator-only Tabby `config.yml`, system `tabby.env`, screensaver, and GPU fan/power. Same keys from the shell: `tsctl`
  - **Account menu** — Download backup / Restore backup for this signed-in account (chats, Code files, prefs, gallery). After a fresh install, recreate extra Tabby users, then each person restores their own zip.
- The GPU is shared. Browser UI and editor `/v1` requests wait in one queue.

Do not SSH into the GPU host just to change models. Send a chat phrase, use Status in `/v1/ui`, or send `restart` to bounce the API.

## Switch models

Send a message that is **only** one of these. Times are warm switches on this RTX 4070 Ti 12 GB (first boot can compile Triton longer). Chat replies use `tabbyAPI/model_profiles/switch_times.json`.

| Phrase | Use | Context | Ready |
|---|---|---|---|
| `help` | Full usage guide | — | — |
| `list models` | Show installed profiles | — | — |
| `restart` | Bounce the API; last model reloads | — | ~65 seconds |
| `switch to qwen` | Daily coding, 9B | 262k | ~65 seconds |
| `switch to qwen35` | Long or hard agent work | 196k | ~3 minutes |
| `switch to qwen36` | Long or hard agent work | 98k | ~85 seconds |
| `switch to gemma` | General | 262k | ~65 seconds |
| `switch to gemma26` | General | 262k | ~2 minutes |
| `switch to glm` | Thinking chat only (no coding tools; vision off on RTX 4070 Ti 12 GB) | 65k (model max) | ~15 seconds |
| `switch to comfy` / `flux` | Unload the LLM; image gen | — | ~35 seconds (then Flux ~3 minutes / Qwen-Image ~4 minutes for the first picture) |
| `switch to llm` | Free Comfy; reload the last LLM | — | ~65 seconds |

The GPU is exclusive: **LLM or Comfy, not both**. `Qwen3-Embedding-0.6B` stays on CPU (`POST /v1/embeddings`). After `switch to comfy`, Flux Schnell is for drafts; Qwen-Image is for text / posters / UI, or a `qwen-image:` prefix.

## Images (works in every IDE)

The GPU server generates the PNG and returns a URL on **this same API host**. No special IDE plugin is required.

- In chat: `switch to comfy`, wait until Comfy is ready, then describe the image. Flux Schnell is the default draft. Prefix `qwen-image:` (or mention poster / button / logo) for readable text. Hero/header photos: a scene, not a website. Paste a photo in the same turn for Flux img2img. Then `switch to qwen`.
- Or one line while coding: `generate an image of a login form`. The API hands the GPU to Comfy, returns the URL, and reloads the last LLM.
- Or OpenAI-shaped: `POST $TABBY_V1/images/generations` with `{"prompt":"qwen-image: a logo that says Cafe"}`. Returns `b64_json` and `url`. Save the PNG with a shell command. Do not paste binary into chat. Never use a built-in cloud “generate image”.

### Coding plus images (same chat)

A line like “create a webpage and generate a header and logo” is a **coding task**. Write HTML/CSS/JS first, then generate PNGs on the GPU. Do not use React/Vite boilerplate, SVG/CSS art, or Pillow/`generate_images.py`.

- **Browser Code:** ask for the files and named PNGs together. The browser writes the project with workspace tools, then the API holds until PNGs exist and copies them into the Files pane.
- **Editor:** apply **your** file tools on your computer. Point `img src` at planned paths such as `images/logo.png`. Do not dump the page in chat or overwrite those PNGs. After the page is written, the next reply holds until every planned PNG exists, then returns **one** Shell `curl` of those real URLs. Run that `curl`. Do not `sleep`/`ls`, invent timestamps, or curl another chat’s leftovers. A 404 means the file is missing on the GPU host.

Prefix `qwen-image:` for logos and readable text. Hero/header photos: a scene, not a website.

Several PNGs share one Comfy batch (Flux ~3 min each, Qwen-Image ~4 min, then ~65 s to reload the coding model once).

## Long tasks

- Daily work: stay on `qwen`. For a long agent job, switch to `qwen35` or `qwen36` first, then continue in a new chat.
- Split big work: explore and stop, then a second chat that only makes the edit.
- Do not repeat the same search with the same arguments. After about 8 search/read rounds with no edit, stop and say what you found.
