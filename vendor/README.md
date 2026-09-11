# Vanilla TabbyAPI backend

This folder holds a pinned clone of [theroyallab/tabbyAPI](https://github.com/theroyallab/tabbyAPI) used only as the localhost LLM process.

```bash
python -m sidecar.fetch_upstream
```

`watch_api.py` starts `vendor/tabbyAPI/main.py` on `127.0.0.1:5001` when that tree exists. The public sidecar on port 5000 stays in this repo. Do not copy `ui/` into the vendor tree.

Pin: see `vendor/UPSTREAM` after a fetch.
