# Vanilla TabbyAPI (sidecar)

Goal: keep the full stack, stop forking TabbyAPI source. Treat [upstream TabbyAPI](https://github.com/theroyallab/tabbyAPI) as a backend.

Do not copy `ui/` or other stack files into their tree. Vanilla Tabby has no plugin API. Extra files next to their `main` will not run.

## Shape

- Vanilla TabbyAPI on localhost only (for example `:5001`), stock tree, stock API key.
- Stack sidecar on `:5000`: browser UI, accounts, Status, Models, Gallery, `tsctl`, GPU / Comfy.
- Editors and the UI talk only to the sidecar. It authenticates (Linux / UI users), then either handles the request or forwards it to Tabby with the internal key.

| Feature | Where it lives |
|---|---|
| `/v1/ui`, accounts, Settings | Sidecar |
| `switch to qwen` / `comfy` / `restart` | Sidecar reads the chat body, runs GPU orchestration, returns a chat reply |
| Chat → image, `/v1/images/generations` | Sidecar; Tabby is unloaded first via its own model API |
| Occupancy / queue | Sidecar lock before a forward |
| Skip LLM when Comfy owns the GPU | Sidecar never asks Tabby to load (or calls unload) |
| Screensaver / live HUD | Sidecar watches POSTs and logs |

Upstream already has several load settings we used to patch in (`vision_offload`, `cpu_moe_*`, `job_max_rq_tokens`). Those come for free on their tree.

Streaming tweaks (reasoning/content split, disconnect abort, noop-edit retries, agent-loop hints) move to the sidecar: change the request before Tabby runs, or rewrite the stream before the client sees it.

## What a sidecar cannot do

Tabby writes the reply one piece at a time, inside its own engine. A sidecar can change the request **before** that starts, and tidy the stream **after** pieces come out. It cannot reach in **while** a piece is being generated.

A few of our tweaks live in that inner step. Tabby already exposes some of them as settings; those we keep. Anything it does not expose, we cannot do from outside.

That leftover list is small today. Later, if we wanted a new inner-step tweak, we would either ask upstream Tabby to add a setting, or live with their behavior.

## Running it

The public process is `python -m sidecar` (port 5000). Tabby is started on `127.0.0.1:5001` by `watch_api.py`. `TABBY_SIDECAR=0` keeps the old single-process Tabby.

```bash
# Optional: clone pinned upstream as the LLM backend
python -m sidecar.fetch_upstream
```

`watch_api.py` uses `vendor/tabbyAPI` when that tree exists, otherwise the forked `tabbyAPI/` tree. Clients still only talk to the sidecar.
