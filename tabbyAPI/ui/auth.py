"""Linux-user login for the management UI.

Validates the stack account via PAM (in a subprocess) and issues an
HTTP-only session cookie. Never call libpam inside the API process —
a bad ctypes conversation used to abort TabbyAPI with free(): invalid size.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Cookie, HTTPException, Request, Response

COOKIE_NAME = "tabby_ui"
SESSION_TTL_S = 24 * 60 * 60
LOGIN_WINDOW_S = 60
LOGIN_MAX_ATTEMPTS = 5
PAM_CHECK_TIMEOUT_S = 15
SESSION_SAVE_INTERVAL_S = 5 * 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_sessions_loaded = False
_login_hits: dict[str, list[float]] = {}
_login_lock = threading.Lock()
_AUTHENTICATE = None

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_PATH = ROOT / "ui_sessions.json"


def _load_sessions_locked() -> None:
    global _sessions_loaded
    if _sessions_loaded:
        return
    _sessions_loaded = True
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    rows = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return
    now = time.time()
    for token, session in rows.items():
        if not isinstance(token, str) or not isinstance(session, dict):
            continue
        username = str(session.get("username") or "")
        try:
            created_at = float(session.get("created_at"))
            last_activity = float(session.get("last_activity"))
        except (TypeError, ValueError):
            continue
        if not username or now - last_activity > SESSION_TTL_S:
            continue
        _sessions[token] = {
            "username": username,
            "is_admin": bool(session.get("is_admin")),
            "created_at": created_at,
            "last_activity": last_activity,
            "saved_activity": last_activity,
        }


def _save_sessions_locked() -> None:
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS_PATH.with_suffix(".json.tmp")
    rows = {
        token: {
            "username": session["username"],
            "is_admin": bool(session.get("is_admin")),
            "created_at": session["created_at"],
            "last_activity": session["last_activity"],
        }
        for token, session in _sessions.items()
    }
    payload = json.dumps({"sessions": rows}, separators=(",", ":")) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, SESSIONS_PATH)
    try:
        os.chmod(SESSIONS_PATH, 0o600)
    except OSError:
        pass


def stack_username() -> str:
    env = (os.environ.get("TABBY_UI_USER") or "").strip()
    if env:
        return env
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _pam_helper_env() -> dict[str, str]:
    """Sidecar cwd is the stack root; `-m ui.pam_check` needs tabbyAPI on PYTHONPATH."""
    env = os.environ.copy()
    tabby = str(ROOT)
    existing = env.get("PYTHONPATH", "")
    parts = [tabby]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _pam_authenticate(username: str, password: str) -> bool:
    """Ask a throwaway helper process. Crash there must not kill TabbyAPI."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ui.pam_check", username],
            input=password.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PAM_CHECK_TIMEOUT_S,
            check=False,
            env=_pam_helper_env(),
            cwd=str(ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def is_admin_username(username: str) -> bool:
    expected = stack_username()
    return bool(expected) and username == expected


def authenticate_user(username: str, password: str) -> bool:
    if not username or not password:
        return False
    expected = stack_username()
    if expected and username == expected:
        if _AUTHENTICATE is not None:
            return bool(_AUTHENTICATE(username, password))
        return _pam_authenticate(username, password)
    from ui.users import verify_extra_user

    return verify_extra_user(username, password)


def set_authenticator(fn) -> None:
    global _AUTHENTICATE
    _AUTHENTICATE = fn


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        _load_sessions_locked()
        _sessions[token] = {
            "username": username,
            "is_admin": is_admin_username(username),
            "created_at": now,
            "last_activity": now,
            "saved_activity": now,
        }
        _save_sessions_locked()
    return token


def validate_session(token: str, max_age: int = SESSION_TTL_S) -> Optional[str]:
    if not token:
        return None
    now = time.time()
    with _sessions_lock:
        _load_sessions_locked()
        session = _sessions.get(token)
        if not session:
            return None
        if now - session["last_activity"] > max_age:
            _sessions.pop(token, None)
            _save_sessions_locked()
            return None
        session["last_activity"] = now
        if now - session.get("saved_activity", 0) >= SESSION_SAVE_INTERVAL_S:
            session["saved_activity"] = now
            _save_sessions_locked()
        return session["username"]


def destroy_session(token: str) -> None:
    with _sessions_lock:
        _load_sessions_locked()
        if _sessions.pop(token, None) is not None:
            _save_sessions_locked()


def destroy_sessions_for_user(username: str) -> None:
    if not username:
        return
    with _sessions_lock:
        _load_sessions_locked()
        dead = [token for token, session in _sessions.items() if session.get("username") == username]
        for token in dead:
            _sessions.pop(token, None)
        if dead:
            _save_sessions_locked()


def clear_sessions() -> None:
    global _sessions_loaded
    with _sessions_lock:
        _sessions.clear()
        _sessions_loaded = True
        try:
            SESSIONS_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    with _login_lock:
        _login_hits.clear()


def login_allowed(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        hits = [stamp for stamp in _login_hits.get(ip, []) if now - stamp < LOGIN_WINDOW_S]
        _login_hits[ip] = hits
        return len(hits) < LOGIN_MAX_ATTEMPTS


def record_login_attempt(ip: str) -> None:
    now = time.time()
    with _login_lock:
        hits = [stamp for stamp in _login_hits.get(ip, []) if now - stamp < LOGIN_WINDOW_S]
        hits.append(now)
        _login_hits[ip] = hits


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    # Trust X-Forwarded-For only from a local reverse proxy / SSH tunnel.
    if peer in ("127.0.0.1", "::1", "localhost") and forwarded:
        return forwarded
    return peer or forwarded or "unknown"


def set_session_cookie(response: Response, token: str, request: Request | None = None) -> None:
    secure = False
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
        secure = proto == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
        max_age=SESSION_TTL_S,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def csrf_origin_ok(request: Request) -> bool:
    """Reject cross-site mutating calls that still send the session cookie."""
    method = str(getattr(request, "method", "") or "").upper()
    if method in _SAFE_METHODS:
        return True
    return _origin_matches_host(request)


def websocket_origin_ok(websocket) -> bool:
    return _origin_matches_host(websocket)


def _origin_matches_host(source) -> bool:
    headers = getattr(source, "headers", None)
    if headers is None:
        return True
    origin = str(headers.get("origin") or "").strip()
    if not origin:
        return True
    origin_host = (urlparse(origin).netloc or "").strip().lower()
    if not origin_host:
        return False
    host = str(headers.get("host") or "").split(",")[0].strip().lower()
    forwarded = ""
    client = getattr(source, "client", None)
    peer = getattr(client, "host", "") if client is not None else ""
    if peer in ("127.0.0.1", "::1", "localhost"):
        forwarded = str(headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()
    allowed = {item for item in (host, forwarded) if item}
    return origin_host in allowed


async def require_ui_user(
    request: Request,
    tabby_ui: Optional[str] = Cookie(None, alias=COOKIE_NAME),
) -> str:
    username = validate_session(tabby_ui or "")
    if not username:
        raise HTTPException(401, "Not authenticated")
    if not csrf_origin_ok(request):
        raise HTTPException(403, "Invalid origin")
    return username


async def require_ui_admin(
    request: Request,
    tabby_ui: Optional[str] = Cookie(None, alias=COOKIE_NAME),
) -> str:
    username = await require_ui_user(request, tabby_ui)
    if not is_admin_username(username):
        raise HTTPException(403, "Admin only.")
    return username
