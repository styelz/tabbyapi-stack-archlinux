#!/usr/bin/env python3
"""Live mixed coding+images e2e: fake Copilot client for Cosmos Tours.

Drives POST /v1/chat/completions with run_in_terminal + file tools, follows
the Shell wait/curl loop while Comfy renders, then lets the 9B write the site.

Each run lands in $HOME/tabby-mixed-runs/<stamp>-cosmos/ with transcript and
report. Pytest skips req_*.py; run manually:

    cd $HOME/tabbyapi-stack/tabbyAPI
    PYTHONPATH=. python tests/req_mixed_prompt.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

# Allow running from tabbyAPI/ or tabbyAPI/tests/
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _common import load_api_keys  # noqa: E402

BASE_URL = os.environ.get("TABBY_BASE_URL", "http://127.0.0.1:5000/v1")
MODEL = "gpt-4o"
MAX_TURNS = 120
PROMPT = (
    "create a website for Cosmos Tours with a nice logo image and "
    "transparent PNG images of Mars, Jupiter, Saturn and Neptune. "
    "Put files under llm-testing."
)
EXPECTED_PNGS = [
    "llm-testing/images/logo.png",
    "llm-testing/images/mars.png",
    "llm-testing/images/jupiter.png",
    "llm-testing/images/saturn.png",
    "llm-testing/images/neptune.png",
]

FORBIDDEN_SHELL = re.compile(
    r"generate_images\.py|from\s+PIL|import\s+PIL|pillow",
    re.I,
)
CURL_URL_RE = re.compile(r"https?://[^\s'\"]+/v1/images/[^\s'\"]+")
JOB_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_in_terminal",
            "description": "Run a shell command in the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_string_in_file",
            "description": "Replace text in an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "oldString": {"type": "string"},
                    "newString": {"type": "string"},
                },
                "required": ["filePath", "oldString", "newString"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Write a file (Cursor-style alias).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "contents": {"type": "string"},
                },
                "required": ["path", "contents"],
            },
        },
    },
]


class RunState:
  def __init__(self, run_dir: Path, api_key: str):
    self.run_dir = run_dir
    self.api_key = api_key
    self.messages: list[dict[str, Any]] = []
    self.transcript_path = run_dir / "transcript.jsonl"
    self.job_id: Optional[str] = None
    self.job_status: Optional[str] = None
    self.curl_while_running = False
    self.failures: list[str] = []
    self.notes: list[str] = []
    self.turn = 0

  def log(self, record: dict[str, Any]) -> None:
    with self.transcript_path.open("a", encoding="utf-8") as handle:
      handle.write(json.dumps(record, ensure_ascii=False) + "\n")

  def headers(self) -> dict[str, str]:
    if self.api_key:
      return {"Authorization": f"Bearer {self.api_key}"}
    return {}

  def fetch_job(self) -> Optional[dict[str, Any]]:
    if not self.job_id:
      url = f"{BASE_URL}/images/jobs"
    else:
      url = f"{BASE_URL}/images/jobs/{self.job_id}"
    try:
      response = httpx.get(url, headers=self.headers(), timeout=30)
      if response.status_code == 404:
        return None
      response.raise_for_status()
      data = response.json()
      self.job_id = data.get("id") or self.job_id
      self.job_status = data.get("status")
      return data
    except Exception as exc:
      self.notes.append(f"job poll error: {exc}")
      return None

  def pngs_present(self) -> bool:
    return all((self.run_dir / rel).is_file() for rel in EXPECTED_PNGS)

  def html_present(self) -> bool:
    site = self.run_dir / "llm-testing"
    if not site.is_dir():
      return False
    return any(site.rglob("*.html"))

  def done(self, choice: dict[str, Any]) -> bool:
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
      return False
    if not self.pngs_present():
      return False
    if not self.html_present():
      return False
    return True


def make_run_dir() -> Path:
  stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
  run_dir = Path.home() / "tabby-mixed-runs" / f"{stamp}-cosmos"
  run_dir.mkdir(parents=True, exist_ok=False)
  return run_dir


def health_ok() -> bool:
  root = BASE_URL.removesuffix("/v1")
  try:
    response = httpx.get(f"{root}/health", timeout=10)
    return response.status_code == 200
  except Exception:
    return False


def resolve_path(run_dir: Path, raw: str) -> Path:
  text = str(raw or "").strip().replace("\\", "/")
  if not text:
    raise ValueError("empty path")
  path = Path(text)
  if path.is_absolute():
    return path
  return (run_dir / path).resolve()


def allow_shell(command: str, state: RunState, images_done: bool) -> Optional[str]:
  cmd = command.strip()
  if FORBIDDEN_SHELL.search(cmd):
    return "blocked: forbidden Pillow/generate_images.py command"
  if not images_done:
    # During image job: only wait/download helpers.
    allowed = ("sleep", "echo", "ls", "mkdir", "curl", "pwd", "test", "true", "false")
    first = cmd.split()[0] if cmd.split() else ""
    base = Path(first).name
    if base not in allowed and not cmd.startswith("sleep "):
      return f"blocked during image job: {base}"
    if "curl" in cmd:
      if state.job_status == "running":
        state.curl_while_running = True
        return "blocked: curl while job still running"
      if "/v1/images/" not in cmd:
        return "blocked: curl must target /v1/images/ URLs"
  return None


def run_shell(command: str, state: RunState, images_done: bool) -> str:
  blocked = allow_shell(command, state, images_done)
  if blocked:
    state.failures.append(blocked)
    return blocked
  env = os.environ.copy()
  env["TABBY_API_KEY"] = state.api_key
  try:
    proc = subprocess.run(
      command,
      shell=True,
      cwd=state.run_dir,
      capture_output=True,
      text=True,
      timeout=180,
      env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
      out = f"exit {proc.returncode}\n{out}"
    return out.strip() or "(no output)"
  except subprocess.TimeoutExpired:
    return "error: command timed out after 180s"
  except Exception as exc:
    return f"error: {exc}"


def write_file(run_dir: Path, rel: str, content: str) -> str:
  path = resolve_path(run_dir, rel)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content, encoding="utf-8")
  return f"wrote {path.relative_to(run_dir)}"


def replace_in_file(run_dir: Path, rel: str, old: str, new: str) -> str:
  path = resolve_path(run_dir, rel)
  if not path.is_file():
    return f"error: file not found: {rel}"
  text = path.read_text(encoding="utf-8")
  if old not in text:
    return f"error: oldString not found in {rel}"
  path.write_text(text.replace(old, new, 1), encoding="utf-8")
  return f"patched {path.relative_to(run_dir)}"


def handle_tool(name: str, args: dict[str, Any], state: RunState, images_done: bool) -> str:
  if name in ("run_in_terminal", "Shell", "shell", "bash"):
    return run_shell(str(args.get("command") or ""), state, images_done)
  if name == "create_file":
    return write_file(
      state.run_dir,
      str(args.get("filePath") or args.get("path") or ""),
      str(args.get("content") or args.get("contents") or ""),
    )
  if name == "replace_string_in_file":
    return replace_in_file(
      state.run_dir,
      str(args.get("filePath") or ""),
      str(args.get("oldString") or ""),
      str(args.get("newString") or ""),
    )
  if name == "Write":
    return write_file(
      state.run_dir,
      str(args.get("path") or args.get("filePath") or ""),
      str(args.get("contents") or args.get("content") or ""),
    )
  return f"error: unknown tool {name}"


def chat_turn(state: RunState) -> dict[str, Any]:
  payload = {
    "model": MODEL,
    "messages": state.messages,
    "tools": TOOLS,
    "tool_choice": "auto",
    "stream": False,
  }
  response = httpx.post(
    f"{BASE_URL}/chat/completions",
    headers=state.headers(),
    json=payload,
    timeout=600,
  )
  response.raise_for_status()
  return response.json()


def extract_job_id(state: RunState, data: dict[str, Any]) -> None:
  blob = json.dumps(data)
  for match in JOB_ID_RE.finditer(blob):
    state.job_id = match.group(0)


def inspect_png(path: Path) -> dict[str, Any]:
  info: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
  if not path.is_file():
    return info
  raw = path.read_bytes()
  info["bytes"] = len(raw)
  info["png_magic"] = raw[:8] == b"\x89PNG\r\n\x1a\n"
  try:
    from PIL import Image

    with Image.open(path) as img:
      info["size"] = img.size
      info["mode"] = img.mode
  except Exception as exc:
    info["pil_error"] = str(exc)
  return info


def inspect_html(run_dir: Path) -> dict[str, Any]:
  site = run_dir / "llm-testing"
  result: dict[str, Any] = {
    "files": [],
    "img_srcs": [],
    "uses_svg": False,
    "missing_paths": [],
  }
  if not site.is_dir():
    return result
  for html in site.rglob("*.html"):
    rel = html.relative_to(run_dir).as_posix()
    result["files"].append(rel)
    text = html.read_text(encoding="utf-8", errors="replace")
    if "<svg" in text.lower():
      result["uses_svg"] = True
  expected = {Path(p).as_posix() for p in EXPECTED_PNGS}
  referenced: set[str] = set()
  for html in site.rglob("*.html"):
    html_dir = html.parent
    text = html.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"""<img[^>]+src=["']([^"']+)["']""", text, re.I):
      src = match.group(1)
      result["img_srcs"].append(src)
      if src.startswith(("http://", "https://", "data:")):
        continue
      resolved = (html_dir / src).resolve()
      try:
        referenced.add(resolved.relative_to(run_dir.resolve()).as_posix())
      except ValueError:
        referenced.add(src)
  result["missing_paths"] = sorted(expected - referenced)
  return result


def write_report(state: RunState) -> Path:
  report = state.run_dir / "report.md"
  png_rows = [inspect_png(state.run_dir / rel) for rel in EXPECTED_PNGS]
  html_info = inspect_html(state.run_dir)
  lines = [
    "# Cosmos Tours mixed e2e report",
    "",
    f"- Run folder: `{state.run_dir}`",
    f"- Job id: `{state.job_id or 'unknown'}`",
    f"- Job status: `{state.job_status or 'unknown'}`",
    f"- Turns: {state.turn}",
    f"- Curl while running: **{state.curl_while_running}**",
    "",
    "## PNG checks",
    "",
  ]
  for row in png_rows:
    rel = Path(row["path"]).name
    ok = row.get("exists") and row.get("png_magic") and row.get("bytes", 0) > 50_000
    lines.append(f"- `{rel}`: exists={row.get('exists')} magic={row.get('png_magic')} "
                 f"bytes={row.get('bytes')} size={row.get('size', '?')} "
                 f"{'PASS' if ok else 'FAIL'}")
  lines.extend(["", "## HTML checks", ""])
  if html_info["files"]:
    for name in html_info["files"]:
      lines.append(f"- `{name}`")
  else:
    lines.append("- No HTML files found (FAIL)")
  if html_info["img_srcs"]:
    lines.append("")
    lines.append("img src values:")
    for src in html_info["img_srcs"]:
      lines.append(f"- `{src}`")
  if html_info.get("missing_paths"):
    lines.append("")
    lines.append("Missing img src paths:")
    for path in html_info["missing_paths"]:
      lines.append(f"- `{path}`")
  if html_info["uses_svg"]:
    lines.append("")
    lines.append("- Note: inline SVG detected in HTML")
  if (state.run_dir / "generate_images.py").is_file():
    lines.append("")
    lines.append("- FAIL: generate_images.py present")
  if state.failures:
    lines.extend(["", "## Failures", ""])
    lines.extend(f"- {item}" for item in state.failures)
  if state.notes:
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {item}" for item in state.notes)
  png_ok = all(
    row.get("exists") and row.get("png_magic") and row.get("bytes", 0) > 50_000
    for row in png_rows
  )
  html_ok = bool(html_info["files"])
  refs_png = any(".png" in src.lower() for src in html_info["img_srcs"])
  paths_ok = not html_info.get("missing_paths")
  overall = (
    png_ok
    and html_ok
    and refs_png
    and paths_ok
    and not state.curl_while_running
    and not (state.run_dir / "generate_images.py").is_file()
  )
  lines.extend(["", f"## Overall: **{'PASS' if overall else 'FAIL'}**", ""])
  report.write_text("\n".join(lines), encoding="utf-8")
  return report


def load_api_key() -> str:
  token_path = os.environ.get("TABBY_API_TOKENS", "")
  if token_path and Path(token_path).is_file():
    key, _ = load_api_keys(path=token_path)
    if key:
      return key
  env_key = os.environ.get("TABBY_API_KEY", "")
  if env_key:
    return env_key
  default = Path.home() / "tabbyapi-stack" / "tabbyAPI" / "api_tokens.yml"
  if default.is_file():
    key, _ = load_api_keys(path=str(default))
    if key:
      return key
  # disable_auth: true on local installs — empty bearer is fine.
  return ""


def main() -> int:
  if not health_ok():
    print(f"health check failed for {BASE_URL}", file=sys.stderr)
    return 2

  api_key = load_api_key()

  run_dir = make_run_dir()
  state = RunState(run_dir, api_key)
  state.messages.append({"role": "user", "content": PROMPT})
  print(f"run folder: {run_dir}")

  for turn in range(1, MAX_TURNS + 1):
    state.turn = turn
    job = state.fetch_job()
    images_done = state.pngs_present() or state.job_status in ("done", "error")
    state.log({"turn": turn, "phase": "request", "job": job})

    try:
      data = chat_turn(state)
    except Exception as exc:
      state.failures.append(f"chat error turn {turn}: {exc}")
      break

    extract_job_id(state, data)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    state.log({"turn": turn, "phase": "response", "choice": choice})

    assistant_msg: dict[str, Any] = {
      "role": "assistant",
      "content": message.get("content"),
    }
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
      assistant_msg["tool_calls"] = tool_calls
    state.messages.append(assistant_msg)

    if not tool_calls:
      if state.done(choice):
        print(f"finished after {turn} turns")
        break
      if state.pngs_present() and not state.html_present():
        state.messages.append(
          {
            "role": "user",
            "content": (
              "The GPU PNGs are on disk under llm-testing/images/. "
              "Write index.html (and CSS if needed) that uses those .png files. "
              "Do not use SVG or Pillow."
            ),
          }
        )
        continue
      if turn >= MAX_TURNS - 1:
        break
      continue

    for call in tool_calls:
      fn = call.get("function") or {}
      name = fn.get("name") or ""
      raw_args = fn.get("arguments") or "{}"
      try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
      except json.JSONDecodeError:
        args = {}
      result = handle_tool(name, args, state, images_done)
      state.log(
        {
          "turn": turn,
          "phase": "tool_result",
          "tool": name,
          "args": args,
          "result": result[:4000],
        }
      )
      state.messages.append(
        {
          "role": "tool",
          "tool_call_id": call.get("id") or f"call_{turn}",
          "content": result,
        }
      )

    state.fetch_job()

  report = write_report(state)
  print(f"report: {report}")
  text = report.read_text(encoding="utf-8")
  print(text)
  return 0 if "## Overall: **PASS**" in text else 1


if __name__ == "__main__":
  raise SystemExit(main())
