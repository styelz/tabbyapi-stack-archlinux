"""Jailed git porcelain for Code-mode workspaces.

Commands are assembled here and run in the chat container. The browser never
sends a git argv. Credentials live in `{chat}.codebox`, not under /work.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

from ui.workspace import (
    CLONE_DEPTH,
    CLONE_TIMEOUT_S,
    MAX_TEXT_BYTES,
    _https_clone_url,
    resolve_rel,
    safe_name,
    workspace_root,
)

GIT_CREDS_NAME = "git-credentials"
GIT_CREDS_MOUNT = "/etc/tabby-git-credentials"
GIT_HELPER = f"store --file={GIT_CREDS_MOUNT}"
GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")
_USERINFO = re.compile(r"(https://)([^/@\s]+)@", re.I)
_AUTH_HINTS = (
    "authentication failed",
    "could not read username",
    "invalid username or password",
    "terminal prompts disabled",
    "http basic: access denied",
    "401 unauthorized",
    "403 forbidden",
    "access denied",
    "repository not found",
)
_STATUS_HEAD = re.compile(
    r"^## (?P<head>\S+?)(?:\.\.\.(?P<up>\S+))?(?: \[(?P<meta>[^\]]*)\])?\s*$"
)


class GitError(ValueError):
    def __init__(self, message: str, *, needs_auth: bool = False):
        super().__init__(message)
        self.needs_auth = bool(needs_auth)


def redact_git_output(text: str) -> str:
    return _USERINFO.sub(r"\1***@", str(text or ""))


def looks_like_auth_failure(text: str) -> bool:
    lower = str(text or "").lower()
    return any(hint in lower for hint in _AUTH_HINTS)


def git_branch_name(raw: str) -> str:
    text = str(raw or "").strip()
    if (
        not text
        or text.startswith("-")
        or text.startswith("/")
        or text.endswith("/")
        or text.endswith(".lock")
        or ".." in text
        or "@{" in text
        or text in {".", "HEAD"}
    ):
        raise GitError("Invalid branch name.")
    if not GIT_BRANCH_RE.fullmatch(text):
        raise GitError("Invalid branch name.")
    return text


def parse_status_porcelain(text: str) -> dict[str, Any]:
    """Parse `git status --porcelain=v1 -b` into a JSON-friendly dict."""
    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    detached = False
    files: list[dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        line = raw.rstrip("\n")
        if line.startswith("## "):
            body = line[3:]
            if body.startswith("HEAD (no branch)") or body.startswith("HEAD (detached"):
                detached = True
                branch = "HEAD"
                continue
            match = _STATUS_HEAD.match(line)
            if match:
                branch = str(match.group("head") or "").strip()
                upstream = str(match.group("up") or "").strip()
                meta = str(match.group("meta") or "")
                ahead_m = re.search(r"ahead (\d+)", meta)
                behind_m = re.search(r"behind (\d+)", meta)
                if ahead_m:
                    ahead = int(ahead_m.group(1))
                if behind_m:
                    behind = int(behind_m.group(1))
            else:
                branch = body.split("...", 1)[0].strip()
            continue
        if len(line) < 4:
            continue
        index, work = line[0], line[1]
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[-1]
        rest = rest.strip()
        if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            rest = rest[1:-1]
        rest = rest.strip()
        if not rest:
            continue
        files.append(
            {
                "path": rest,
                "index": index,
                "work": work,
                "staged": index not in (" ", "?"),
                "unstaged": work not in (" ",),
            }
        )
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "detached": detached,
        "files": files,
    }


def parse_log(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        try:
            ts = int(parts[3] or 0)
        except ValueError:
            ts = 0
        rows.append(
            {
                "hash": parts[0],
                "short": parts[1],
                "author": parts[2],
                "ts": ts,
                "subject": parts[4],
            }
        )
    return rows


def creds_path(username: str, chat_id: str) -> Path:
    from ui.codebox import identity_dir

    return identity_dir(username, chat_id) / GIT_CREDS_NAME


def ensure_creds_file(username: str, chat_id: str) -> Path:
    path = creds_path(username, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def has_creds(username: str, chat_id: str) -> bool:
    path = creds_path(username, chat_id)
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def save_creds(username: str, chat_id: str, token: str, host: str) -> None:
    token = str(token or "").strip()
    if not token or any(ch in token for ch in "\n\r\0"):
        raise GitError("A personal access token is required.")
    if len(token) > 512:
        raise GitError("Token is too long.")
    host_name = str(host or "").strip().lower()
    if host_name.count(":") > 1 or "/" in host_name or "@" in host_name:
        raise GitError("Invalid remote host.")
    name = host_name.split(":", 1)[0]
    if not name or name in {"localhost", "127.0.0.1", "::1"}:
        raise GitError("Clone or add an HTTPS remote first.")
    line = f"https://git:{quote(token, safe='')}@{host_name}\n"
    path = ensure_creds_file(username, chat_id)
    path.write_text(line, encoding="ascii")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_creds(username: str, chat_id: str) -> None:
    path = creds_path(username, chat_id)
    try:
        if path.is_file():
            path.write_text("", encoding="utf-8")
            os.chmod(path, 0o600)
    except OSError:
        pass


def _git_dir(path: Path) -> bool:
    git = path / ".git"
    try:
        return git.is_dir() or git.is_file()
    except OSError:
        return False


_SKIP_REPO_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
}


def find_repo_on_disk(root: Path, *, max_depth: int = 3) -> Optional[str]:
    """Relative path to a repo under root ('' for root). None if none."""
    if not root.is_dir():
        return None
    if _git_dir(root):
        return ""
    found: list[str] = []

    def walk(path: Path, rel: str, depth: int) -> None:
        if depth > max_depth or len(found) > 1:
            return
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for entry in entries:
            name = str(entry.name or "")
            if not name or name.startswith(".") or name in _SKIP_REPO_DIRS:
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
            except OSError:
                continue
            child_rel = f"{rel}/{name}" if rel else name
            if _git_dir(Path(entry.path)):
                found.append(child_rel)
                if len(found) > 1:
                    return
                continue
            walk(Path(entry.path), child_rel, depth + 1)

    walk(root, "", 1)
    if len(found) == 1:
        return found[0]
    return None


def find_repo_rel(username: str, chat_id: str) -> Optional[str]:
    """Relative workspace path to the git repo ('' for /work). None if none.

    Disk only. A docker `git rev-parse` fallback used to run on every Code
    poll, including empty workspaces, and blocked the API event loop (~2s)
    so streamed tokens paused.
    """
    root = workspace_root(username, chat_id, create=False)
    return find_repo_on_disk(root)


def _workspace_file_text(root: Path, rel: str) -> str:
    try:
        path = resolve_rel(root, rel)
    except ValueError:
        return ""
    if not path.is_file() or path.is_symlink():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_TEXT_BYTES:
        return f"[binary {len(data)} bytes]"
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"[binary {len(data)} bytes]"


def _git_cmd(repo_rel: str, args: list[str]) -> str:
    parts = [
        "git",
        "--no-pager",
        "-c",
        "safe.directory=*",
        "-C",
        repo_rel or ".",
        *args,
    ]
    return " ".join(shlex.quote(part) for part in parts)


def _run_git(
    username: str,
    chat_id: str,
    repo_rel: str,
    args: list[str],
    *,
    timeout: float | None = None,
    max_bytes: int | None = None,
    network: bool = False,
) -> tuple[int, str]:
    from ui import codebox

    try:
        code, output = codebox.run_shell(
            username,
            chat_id,
            _git_cmd(repo_rel, args),
            timeout=timeout,
            max_bytes=max_bytes,
            network=network,
        )
    except codebox.CodeboxError as exc:
        raise GitError(str(exc)) from exc
    return code, redact_git_output(output)


def _require_repo(username: str, chat_id: str) -> str:
    rel = find_repo_rel(username, chat_id)
    if rel is None:
        raise GitError("This workspace is not a git repository.")
    return rel


def _host_path(root: Path, repo_rel: str, repo_path: str) -> str:
    combined = "/".join(part for part in (repo_rel, repo_path) if part)
    resolve_rel(root, combined)
    return combined


def _repo_path(root: Path, repo_rel: str, workspace_path: str) -> str:
    rel = str(workspace_path or "").replace("\\", "/").strip("/")
    if not rel:
        raise GitError("A file path is required.")
    resolve_rel(root, rel)
    prefix = f"{repo_rel}/" if repo_rel else ""
    if repo_rel and rel == repo_rel:
        raise GitError("A file path is required.")
    if prefix:
        if not rel.startswith(prefix):
            raise GitError("That file is outside the repository.")
        return rel[len(prefix) :]
    return rel


def _origin_host(username: str, chat_id: str, repo_rel: str) -> str:
    code, output = _run_git(username, chat_id, repo_rel, ["remote", "get-url", "origin"])
    url = (output or "").strip().splitlines()[0] if output else ""
    if code or not url:
        raise GitError("Clone or add an HTTPS remote first.")
    try:
        clean, _dest = _https_clone_url(url)
    except ValueError as exc:
        raise GitError(str(exc)) from exc
    host = str(urlparse(clean).hostname or "").strip().lower()
    port = urlparse(clean).port
    if not host:
        raise GitError("Remote URL host is not allowed.")
    if port and port != 443:
        return f"{host}:{port}"
    return host


def _raise_git(output: str, fallback: str, *, auth: bool = False) -> None:
    detail = redact_git_output(output).strip() or fallback
    needs = auth or looks_like_auth_failure(detail)
    raise GitError(detail[:1500], needs_auth=needs)


def git_status(username: str, chat_id: str) -> dict[str, Any]:
    repo_rel = find_repo_rel(username, chat_id)
    if repo_rel is None:
        return {"ok": True, "repo": False, "has_creds": has_creds(username, chat_id)}
    empty = {
        "ok": False,
        "repo": True,
        "root": repo_rel,
        "branch": "",
        "upstream": "",
        "ahead": 0,
        "behind": 0,
        "detached": False,
        "shallow": False,
        "files": [],
        "has_creds": has_creds(username, chat_id),
    }
    try:
        code, output = _run_git(
            username, chat_id, repo_rel, ["status", "--porcelain=v1", "-b"]
        )
    except GitError as exc:
        empty["error"] = str(exc)
        return empty
    if code:
        empty["error"] = (redact_git_output(output).strip() or "git status failed")[:1500]
        return empty
    parsed = parse_status_porcelain(output)
    root = workspace_root(username, chat_id, create=False)
    files = []
    for row in parsed["files"]:
        repo_path = str(row["path"])
        try:
            path = _host_path(root, repo_rel, repo_path)
        except ValueError:
            continue
        files.append({**row, "path": path, "repo_path": repo_path})
    shallow_code, shallow_out = _run_git(
        username, chat_id, repo_rel, ["rev-parse", "--is-shallow-repository"]
    )
    shallow = shallow_code == 0 and str(shallow_out or "").strip() == "true"
    return {
        "ok": True,
        "repo": True,
        "root": repo_rel,
        "branch": parsed["branch"],
        "upstream": parsed["upstream"],
        "ahead": parsed["ahead"],
        "behind": parsed["behind"],
        "detached": parsed["detached"],
        "shallow": shallow,
        "files": files,
        "has_creds": has_creds(username, chat_id),
    }


def git_diff(
    username: str, chat_id: str, path: str, *, staged: bool = False
) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    root = workspace_root(username, chat_id, create=False)
    repo_path = _repo_path(root, repo_rel, path)
    host_rel = _host_path(root, repo_rel, repo_path)
    modified = _workspace_file_text(root, host_rel)
    spec = f":{repo_path}" if staged else f"HEAD:{repo_path}"
    code, output = _run_git(
        username,
        chat_id,
        repo_rel,
        ["show", spec],
        max_bytes=MAX_TEXT_BYTES + 64,
    )
    original = "" if code else redact_git_output(output)
    if original.startswith("[truncated]") or original.endswith("[truncated]"):
        original = original.replace("\n[truncated]", "")
    return {
        "ok": True,
        "path": host_rel,
        "repo_path": repo_path,
        "staged": bool(staged),
        "original": original,
        "modified": modified,
    }


def git_log(username: str, chat_id: str) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    code, output = _run_git(
        username,
        chat_id,
        repo_rel,
        ["log", "-n", str(CLONE_DEPTH), "--pretty=format:%H%x09%h%x09%an%x09%ct%x09%s"],
    )
    if code and "does not have any commits" not in (output or "").lower():
        if "bad default revision" in (output or "").lower() or "needed a single revision" in (
            output or ""
        ).lower():
            return {"ok": True, "commits": []}
        _raise_git(output, "git log failed")
    return {"ok": True, "commits": parse_log(output)}


def git_init(username: str, chat_id: str) -> dict[str, Any]:
    if find_repo_rel(username, chat_id) is not None:
        raise GitError("This workspace already has a git repository.")
    workspace_root(username, chat_id, create=True)
    code, output = _run_git(username, chat_id, ".", ["init"])
    if code:
        _raise_git(output, "git init failed")
    return git_status(username, chat_id)


def _paths_arg(username: str, chat_id: str, paths: Any) -> tuple[str, list[str]]:
    repo_rel = _require_repo(username, chat_id)
    root = workspace_root(username, chat_id, create=False)
    raw = paths if isinstance(paths, list) else [paths]
    out: list[str] = []
    for item in raw:
        repo_path = _repo_path(root, repo_rel, str(item or ""))
        out.append(repo_path)
    if not out:
        raise GitError("A file path is required.")
    return repo_rel, out


def git_stage(username: str, chat_id: str, paths: Any) -> dict[str, Any]:
    repo_rel, rels = _paths_arg(username, chat_id, paths)
    code, output = _run_git(username, chat_id, repo_rel, ["add", "--", *rels])
    if code:
        _raise_git(output, "git add failed")
    return git_status(username, chat_id)


def git_unstage(username: str, chat_id: str, paths: Any) -> dict[str, Any]:
    repo_rel, rels = _paths_arg(username, chat_id, paths)
    code, output = _run_git(
        username, chat_id, repo_rel, ["restore", "--staged", "--", *rels]
    )
    if code:
        _raise_git(output, "git unstage failed")
    return git_status(username, chat_id)


def git_commit(username: str, chat_id: str, message: str) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    text = str(message or "").strip()
    if not text:
        raise GitError("A commit message is required.")
    if len(text) > 4000:
        raise GitError("Commit message is too long.")
    user = safe_name(username)
    code, output = _run_git(
        username,
        chat_id,
        repo_rel,
        [
            "-c",
            f"user.name={user}",
            "-c",
            f"user.email={user}@tabby.local",
            "commit",
            "-m",
            text,
        ],
    )
    if code:
        _raise_git(output, "git commit failed")
    return git_status(username, chat_id)


def git_checkout(
    username: str, chat_id: str, name: str, *, create: bool = False
) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    branch = git_branch_name(name)
    args = ["checkout", "-b", branch] if create else ["checkout", branch]
    code, output = _run_git(username, chat_id, repo_rel, args)
    if code:
        _raise_git(output, "git checkout failed")
    return git_status(username, chat_id)


def git_branches(username: str, chat_id: str) -> list[str]:
    repo_rel = _require_repo(username, chat_id)
    code, output = _run_git(
        username, chat_id, repo_rel, ["branch", "--list", "--format=%(refname:short)"]
    )
    if code:
        _raise_git(output, "git branch failed")
    names = []
    for line in str(output or "").splitlines():
        item = line.strip()
        if item:
            names.append(item)
    return names


def _ensure_https_origin(username: str, chat_id: str, repo_rel: str) -> str:
    return _origin_host(username, chat_id, repo_rel)


def git_fetch(username: str, chat_id: str) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    _ensure_https_origin(username, chat_id, repo_rel)
    args = ["fetch", "--prune"]
    code, shallow = _run_git(
        username, chat_id, repo_rel, ["rev-parse", "--is-shallow-repository"]
    )
    if code == 0 and str(shallow or "").strip() == "true":
        args = ["fetch", "--unshallow", "--prune"]
    code, output = _run_git(
        username, chat_id, repo_rel, args, timeout=CLONE_TIMEOUT_S, network=True
    )
    if code:
        _raise_git(output, "git fetch failed", auth=not has_creds(username, chat_id))
    return git_status(username, chat_id)


def git_pull(username: str, chat_id: str) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    _ensure_https_origin(username, chat_id, repo_rel)
    args = ["pull", "--ff-only"]
    code, shallow = _run_git(
        username, chat_id, repo_rel, ["rev-parse", "--is-shallow-repository"]
    )
    if code == 0 and str(shallow or "").strip() == "true":
        fetch_code, fetch_out = _run_git(
            username,
            chat_id,
            repo_rel,
            ["fetch", "--unshallow", "--prune"],
            timeout=CLONE_TIMEOUT_S,
            network=True,
        )
        if fetch_code:
            _raise_git(
                fetch_out,
                "git fetch failed",
                auth=not has_creds(username, chat_id),
            )
    code, output = _run_git(
        username, chat_id, repo_rel, args, timeout=CLONE_TIMEOUT_S, network=True
    )
    if code:
        _raise_git(output, "git pull failed", auth=not has_creds(username, chat_id))
    return git_status(username, chat_id)


def git_push(username: str, chat_id: str) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    _ensure_https_origin(username, chat_id, repo_rel)
    args = ["push", "-u", "origin", "HEAD"]
    code, output = _run_git(
        username, chat_id, repo_rel, args, timeout=CLONE_TIMEOUT_S, network=True
    )
    if code:
        _raise_git(output, "git push failed", auth=not has_creds(username, chat_id))
    return git_status(username, chat_id)


def git_save_token(username: str, chat_id: str, token: str) -> dict[str, Any]:
    repo_rel = _require_repo(username, chat_id)
    host = _origin_host(username, chat_id, repo_rel)
    save_creds(username, chat_id, token, host)
    return {"ok": True, "has_creds": True}


def git_clear_token(username: str, chat_id: str) -> dict[str, Any]:
    clear_creds(username, chat_id)
    status = git_status(username, chat_id)
    status["has_creds"] = False
    return status


def git_action(username: str, chat_id: str, body: dict[str, Any]) -> dict[str, Any]:
    action = str((body or {}).get("action") or "").strip().lower()
    if action == "init":
        return git_init(username, chat_id)
    if action == "stage":
        return git_stage(username, chat_id, (body or {}).get("paths"))
    if action == "unstage":
        return git_unstage(username, chat_id, (body or {}).get("paths"))
    if action == "commit":
        return git_commit(username, chat_id, str((body or {}).get("message") or ""))
    if action == "checkout":
        return git_checkout(
            username,
            chat_id,
            str((body or {}).get("name") or ""),
            create=bool((body or {}).get("create")),
        )
    if action == "branch":
        names = git_branches(username, chat_id)
        status = git_status(username, chat_id)
        status["branches"] = names
        return status
    if action == "fetch":
        return git_fetch(username, chat_id)
    if action == "pull":
        return git_pull(username, chat_id)
    if action == "push":
        return git_push(username, chat_id)
    if action == "creds":
        return git_save_token(username, chat_id, str((body or {}).get("token") or ""))
    if action in {"clear-creds", "clear_creds"}:
        return git_clear_token(username, chat_id)
    raise GitError("Unknown git action.")
