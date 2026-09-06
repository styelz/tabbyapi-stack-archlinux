"""Per-chat Docker sandbox for UI Code mode.

No host useradd. Each chat container has a Unix account named after the Tabby
login. Project files stay bind-mounted at /work.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from ui.workspace import safe_name, user_dir, workspace_root

IMAGE = "tabbyapi-stack-code:local"
LABEL = "tabby.stack=code"
DOCKER_SOCK = "/var/run/docker.sock"
WORK_DIR = "/work"
SHELL_TIMEOUT_S = 30
SHELL_MAX_BYTES = 64 * 1024
MEMORY = "512m"
PIDS = "256"
CPUS = "1"

_ensure_guard = threading.Lock()
_name_locks: dict[str, threading.Lock] = {}


class CodeboxError(RuntimeError):
    pass


def docker_bin() -> str:
    path = shutil.which("docker")
    if not path:
        raise CodeboxError("install docker")
    return path


def unix_name(username: str) -> str:
    return safe_name(username)


def container_name(username: str, chat_id: str) -> str:
    return f"tabby-code-{safe_name(username)}-{safe_name(chat_id)}"


def identity_dir(username: str, chat_id: str) -> Path:
    path = user_dir(username) / f"{safe_name(chat_id)}.codebox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _uid_gid() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def _git_env_pairs() -> list[str]:
    from ui.git import GIT_HELPER

    return [
        "GIT_CONFIG_COUNT=1",
        "GIT_CONFIG_KEY_0=credential.helper",
        f"GIT_CONFIG_VALUE_0={GIT_HELPER}",
        "GIT_TERMINAL_PROMPT=0",
    ]


def write_identity(username: str, chat_id: str) -> tuple[Path, Path]:
    name = unix_name(username)
    uid, gid = _uid_gid()
    folder = identity_dir(username, chat_id)
    passwd = folder / "passwd"
    group = folder / "group"
    passwd.write_text(
        f"root:x:0:0:root:/root:/bin/false\n{name}:x:{uid}:{gid}:{name}:{WORK_DIR}:/bin/bash\n",
        encoding="ascii",
    )
    group.write_text(f"root:x:0:\n{name}:x:{gid}:\n", encoding="ascii")
    os.chmod(passwd, 0o644)
    os.chmod(group, 0o644)
    return passwd, group


def run_args(username: str, chat_id: str, workspace: Path) -> list[str]:
    """docker run argv for this chat's sandbox (no host bash)."""
    root = workspace.resolve()
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
    passwd, group = write_identity(username, chat_id)
    from ui.git import GIT_CREDS_MOUNT, ensure_creds_file

    creds = ensure_creds_file(username, chat_id)
    name = unix_name(username)
    uid, gid = _uid_gid()
    argv = [
        docker_bin(),
        "run",
        "-d",
        "--name",
        container_name(username, chat_id),
        "--label",
        LABEL,
        "--label",
        f"tabby.user={safe_name(username)}",
        "--label",
        f"tabby.chat={safe_name(chat_id)}",
        "--hostname",
        "tabby",
        "--user",
        f"{uid}:{gid}",
        "--workdir",
        WORK_DIR,
        "--env",
        f"HOME={WORK_DIR}",
        "--env",
        f"USER={name}",
        "--env",
        f"LOGNAME={name}",
        "--env",
        "TERM=xterm-256color",
        "--env",
        "PS1=\\W $ ",
    ]
    for pair in _git_env_pairs():
        argv.extend(["--env", pair])
    argv.extend(
        [
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            MEMORY,
            "--pids-limit",
            PIDS,
            "--cpus",
            CPUS,
            "--tmpfs",
            "/tmp",
            "-v",
            f"{root}:{WORK_DIR}",
            "-v",
            f"{passwd}:/etc/passwd:ro",
            "-v",
            f"{group}:/etc/group:ro",
            "-v",
            f"{creds.resolve()}:{GIT_CREDS_MOUNT}",
            "--restart",
            "no",
            IMAGE,
            "sleep",
            "infinity",
        ]
    )
    return argv


def exec_args(username: str, chat_id: str, argv: list[str]) -> list[str]:
    name = unix_name(username)
    uid, gid = _uid_gid()
    out = [
        docker_bin(),
        "exec",
        "-i",
        "-u",
        f"{uid}:{gid}",
        "-w",
        WORK_DIR,
        "-e",
        f"HOME={WORK_DIR}",
        "-e",
        f"USER={name}",
        "-e",
        f"LOGNAME={name}",
    ]
    for pair in _git_env_pairs():
        out.extend(["-e", pair])
    out.extend([container_name(username, chat_id), *argv])
    return out


def _run(argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodeboxError("install docker") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeboxError("docker timed out") from exc


def _inspect_field(name: str, fmt: str) -> Optional[str]:
    proc = _run([docker_bin(), "inspect", "-f", fmt, name], timeout=15)
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"").decode("utf-8", "replace").strip() or None


def _inspect_status(name: str) -> Optional[str]:
    return _inspect_field(name, "{{.State.Status}}")


def _stale_container(name: str) -> bool:
    """True when this container was created from an older image tag."""
    wanted = _inspect_field(IMAGE, "{{.Id}}")
    current = _inspect_field(name, "{{.Image}}")
    if not wanted or not current:
        return False
    return current != wanted


def _missing_git_creds_mount(name: str) -> bool:
    from ui.git import GIT_CREDS_MOUNT

    mounts = _inspect_field(name, "{{range .Mounts}}{{.Destination}}\n{{end}}")
    if mounts is None:
        return False
    return GIT_CREDS_MOUNT not in mounts.split()


def _docker_error(proc: subprocess.CompletedProcess, fallback: str) -> str:
    err = ((proc.stderr or b"") + (proc.stdout or b"")).decode("utf-8", "replace").strip()
    lower = err.lower()
    if "no such image" in lower or "unable to find image" in lower:
        return f"build {IMAGE}"
    if "cannot connect" in lower or "is the docker daemon running" in lower:
        return "Docker is not running"
    if "permission denied" in lower and "docker" in lower:
        return "this account cannot use docker"
    return err or fallback


def _name_lock(name: str) -> threading.Lock:
    with _ensure_guard:
        lock = _name_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _name_locks[name] = lock
        return lock


def ensure_container(username: str, chat_id: str) -> str:
    """Create or start this chat's container. Returns the container name."""
    docker_bin()
    name = container_name(username, chat_id)
    root = workspace_root(username, chat_id, create=True, box=False)
    with _name_lock(name):
        status = _inspect_status(name)
        if status and (_stale_container(name) or _missing_git_creds_mount(name)):
            _run([docker_bin(), "rm", "-f", name], timeout=30)
            status = None
        if status == "running":
            return name
        if status:
            proc = _run([docker_bin(), "start", name], timeout=30)
            if proc.returncode != 0:
                _run([docker_bin(), "rm", "-f", name], timeout=30)
            else:
                return name
        argv = run_args(username, chat_id, root)
        proc = _run(argv, timeout=60)
        if proc.returncode != 0:
            raise CodeboxError(_docker_error(proc, "could not start the project container"))
        return name


def try_ensure_container(username: str, chat_id: str) -> None:
    try:
        ensure_container(username, chat_id)
    except CodeboxError:
        return


def drop_container(username: str, chat_id: str) -> None:
    name = container_name(username, chat_id)
    try:
        _run([docker_bin(), "rm", "-f", name], timeout=30)
    except CodeboxError:
        pass
    folder = user_dir(username) / f"{safe_name(chat_id)}.codebox"
    shutil.rmtree(folder, ignore_errors=True)


def drop_user_containers(username: str) -> None:
    label = f"tabby.user={safe_name(username)}"
    try:
        proc = _run(
            [docker_bin(), "ps", "-aq", "--filter", f"label={LABEL}", "--filter", f"label={label}"],
            timeout=30,
        )
    except CodeboxError:
        return
    ids = (proc.stdout or b"").decode("utf-8", "replace").split()
    if ids:
        _run([docker_bin(), "rm", "-f", *ids], timeout=60)
    folder = user_dir(username)
    if not folder.is_dir():
        return
    for path in folder.glob("*.codebox"):
        shutil.rmtree(path, ignore_errors=True)


def drop_all_code_containers() -> None:
    try:
        proc = _run(
            [docker_bin(), "ps", "-aq", "--filter", f"label={LABEL}"],
            timeout=30,
        )
    except CodeboxError:
        return
    ids = (proc.stdout or b"").decode("utf-8", "replace").split()
    if ids:
        _run([docker_bin(), "rm", "-f", *ids], timeout=60)


def run_shell(
    username: str,
    chat_id: str,
    command: str,
    *,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> tuple[int, str]:
    """Run one command in the chat container. Returns (exit_code, output)."""
    text = str(command or "").strip()
    if not text:
        return 0, ""
    ensure_container(username, chat_id)
    argv = exec_args(username, chat_id, ["bash", "-lc", text])
    limit = SHELL_TIMEOUT_S if timeout is None else max(1.0, float(timeout))
    cap = SHELL_MAX_BYTES if max_bytes is None else max(1024, int(max_bytes))
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=limit,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodeboxError("install docker") from exc
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    out = (proc.stdout or b"") + (proc.stderr or b"")
    if len(out) > cap:
        out = out[:cap] + b"\n[truncated]"
    return int(proc.returncode), out.decode("utf-8", "replace")


def _docker_http(method: str, path: str, body: Any = None, timeout: float = 15) -> tuple[int, bytes]:
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(DOCKER_SOCK)
    except OSError as exc:
        sock.close()
        raise CodeboxError("Docker is not running") from exc
    req = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + payload
    try:
        sock.sendall(req)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise CodeboxError("Docker is not running") from exc
    finally:
        sock.close()
    raw = b"".join(chunks)
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        raise CodeboxError("docker returned an empty reply")
    header = raw[:header_end].decode("iso-8859-1", "replace")
    rest = raw[header_end + 4 :]
    status = 0
    first = header.split("\r\n", 1)[0]
    parts = first.split()
    if len(parts) >= 2:
        try:
            status = int(parts[1])
        except ValueError:
            status = 0
    return status, rest


def create_exec(username: str, chat_id: str, argv: list[str], tty: bool = True) -> str:
    ensure_container(username, chat_id)
    name = unix_name(username)
    uid, gid = _uid_gid()
    status, raw = _docker_http(
        "POST",
        f"/containers/{container_name(username, chat_id)}/exec",
        {
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": bool(tty),
            "User": f"{uid}:{gid}",
            "WorkingDir": WORK_DIR,
            "Env": [
                f"HOME={WORK_DIR}",
                f"USER={name}",
                f"LOGNAME={name}",
                "TERM=xterm-256color",
                "PS1=\\W $ ",
                *_git_env_pairs(),
            ],
            "Cmd": argv,
        },
    )
    if status not in (200, 201):
        text = raw.decode("utf-8", "replace").strip() or f"docker exec create failed ({status})"
        raise CodeboxError(text)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodeboxError("docker exec create failed") from exc
    exec_id = str((data or {}).get("Id") or "").strip()
    if not exec_id:
        raise CodeboxError("docker exec create failed")
    return exec_id


def start_exec_tty(exec_id: str) -> tuple[socket.socket, bytes]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(30)
    try:
        sock.connect(DOCKER_SOCK)
    except OSError as exc:
        sock.close()
        raise CodeboxError("Docker is not running") from exc
    payload = json.dumps({"Detach": False, "Tty": True}).encode("utf-8")
    req = (
        f"POST /exec/{exec_id}/start HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: tcp\r\n"
        "\r\n"
    ).encode("ascii") + payload
    try:
        sock.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1_000_000:
                break
    except OSError as exc:
        sock.close()
        raise CodeboxError("could not attach to the project shell") from exc
    header_end = buf.find(b"\r\n\r\n")
    if header_end < 0:
        sock.close()
        raise CodeboxError("could not attach to the project shell")
    header = buf[:header_end].decode("iso-8859-1", "replace")
    rest = buf[header_end + 4 :]
    first = header.split("\r\n", 1)[0]
    parts = first.split()
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    if status not in (101, 200):
        sock.close()
        raise CodeboxError(rest.decode("utf-8", "replace").strip() or "could not attach to the project shell")
    sock.settimeout(None)
    sock.setblocking(False)
    return sock, rest


def resize_exec(exec_id: str, cols: int, rows: int) -> None:
    cols = max(20, min(int(cols or 80), 400))
    rows = max(4, min(int(rows or 24), 120))
    try:
        _docker_http("POST", f"/exec/{exec_id}/resize?h={rows}&w={cols}", {})
    except CodeboxError:
        return


def lsp_command(language: str) -> Optional[list[str]]:
    commands = {
        "python": ["pylsp"],
        "javascript": ["typescript-language-server", "--stdio"],
        "typescript": ["typescript-language-server", "--stdio"],
        "html": ["vscode-html-language-server", "--stdio"],
        "css": ["vscode-css-language-server", "--stdio"],
        "json": ["vscode-json-language-server", "--stdio"],
    }
    argv = commands.get(language)
    return list(argv) if argv else None
