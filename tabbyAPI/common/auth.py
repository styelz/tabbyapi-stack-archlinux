"""
This method of authorization is pretty insecure, but since TabbyAPI is a local
application, it should be fine.
"""

import asyncio
import hashlib
import io
import os
import secrets
import threading
import time
from typing import List, Optional, Union

import aiofiles
from fastapi import Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, PrivateAttr
from ruamel.yaml import YAML

from common.logger import xlogger

AUTH_FILE = "api_tokens.yml"

# Seconds between checks for changes to the auth file
AUTH_FILE_POLL_INTERVAL = 2.0


class AuthKeys(BaseModel):
    """
    This class represents the authentication keys for the application.
    It contains two types of keys: 'api_key' and 'admin_key'.
    The 'api_key' is used for general API calls, while the 'admin_key'
    is used for administrative tasks. The class also provides a method
    to verify if a given key matches the stored 'api_key' or 'admin_key'.

    api_key accepts either a single key or a list of keys, so access can
    be granted (and revoked) per user. There is always exactly one admin_key.
    """

    api_key: Union[str, List[str]]
    admin_key: str

    _api_key_set: set = PrivateAttr(default_factory=set)

    def model_post_init(self, __context):
        if isinstance(self.api_key, str):
            self._api_key_set = {self.api_key}
        else:
            self._api_key_set = set(self.api_key)

    def verify_key(self, test_key: str, key_type: str):
        """Verify if a given key matches the stored key."""
        if key_type == "admin_key":
            return _keys_equal(test_key, self.admin_key)
        if key_type == "api_key":
            # Admin keys are valid for all API calls
            return any(_keys_equal(test_key, key) for key in self._api_key_set) or _keys_equal(
                test_key, self.admin_key
            )
        return False


def _keys_equal(candidate: str, expected: str) -> bool:
    # Bytes: compare_digest rejects non-ASCII str.
    return secrets.compare_digest(
        str(candidate or "").encode("utf-8"), str(expected or "").encode("utf-8")
    )


# Global auth constants
AUTH_KEYS: Optional[AuthKeys] = None
DISABLE_AUTH: bool = False

# Login passwords are accepted as API keys (Linux admin password = admin key;
# each Tabby-only user's password = an API key). Cache successes so PAM and
# PBKDF2 are not run on every Chat Completions request.
PASSWORD_CACHE_TTL_S = 300.0
PASSWORD_FAIL_TTL_S = 2.0
_password_cache_lock = threading.Lock()
_password_cache: dict[str, tuple[Optional[str], float, str]] = {}

# Serializes reloads of the auth file. Reads don't need the lock: the working
# set of keys is swapped in as one immutable object, and every check function
# takes its own reference.
_reload_lock = asyncio.Lock()
_watch_task: Optional[asyncio.Task] = None


async def _read_auth_file(path: str = AUTH_FILE) -> AuthKeys:
    """Read and validate the auth keys file."""

    yaml = YAML(typ=["rt", "safe"])

    async with aiofiles.open(path, "r", encoding="utf8") as auth_file:
        contents = await auth_file.read()
        auth_keys_dict = yaml.load(contents)
        return AuthKeys.model_validate(auth_keys_dict)


async def _watch_auth_file():
    """
    Poll the auth file for changes and reload the working set of keys.

    A failed reload (partial write, invalid YAML, missing keys) keeps the
    previous keys. Ongoing requests are unaffected either way; a reload only
    changes which keys validate for future requests.
    """

    global AUTH_KEYS

    try:
        last_mtime = os.stat(AUTH_FILE).st_mtime
    except OSError:
        last_mtime = None

    while True:
        await asyncio.sleep(AUTH_FILE_POLL_INTERVAL)

        try:
            mtime = os.stat(AUTH_FILE).st_mtime
        except OSError:
            continue

        if mtime == last_mtime:
            continue
        last_mtime = mtime

        async with _reload_lock:
            try:
                AUTH_KEYS = await _read_auth_file()
            except Exception as exc:
                xlogger.warning(f"Failed to reload {AUTH_FILE}, keeping the previous keys: {exc}")
                continue

        xlogger.info(
            f"Reloaded auth keys from {AUTH_FILE} ({len(AUTH_KEYS._api_key_set)} API key(s))."
        )


def _format_api_keys(auth_keys: AuthKeys) -> str:
    if isinstance(auth_keys.api_key, str):
        count = 1 if auth_keys.api_key else 0
    else:
        count = len(auth_keys.api_key)
    return f"{count} extra API key(s)"


def _chmod_private(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


async def load_auth_keys(disable_from_config: bool):
    """Load the authentication keys from api_tokens.yml. If the file does not
    exist, generate new keys and save them to api_tokens.yml."""
    global AUTH_KEYS
    global DISABLE_AUTH
    global _watch_task

    DISABLE_AUTH = disable_from_config
    if disable_from_config:
        xlogger.warning(
            "Disabling authentication makes your instance vulnerable. "
            "Set the `disable_auth` flag to False in config.yml if you "
            "want to share this instance with others."
        )

        return

    try:
        AUTH_KEYS = await _read_auth_file()
    except FileNotFoundError:
        new_auth_keys = AuthKeys(api_key=secrets.token_hex(16), admin_key=secrets.token_hex(16))
        AUTH_KEYS = new_auth_keys

        yaml = YAML(typ=["rt", "safe"])
        async with aiofiles.open(AUTH_FILE, "w", encoding="utf8") as auth_file:
            string_stream = io.StringIO()
            yaml.dump(AUTH_KEYS.model_dump(), string_stream)

            await auth_file.write(string_stream.getvalue())
        _chmod_private(AUTH_FILE)
    else:
        _chmod_private(AUTH_FILE)

    # Reload the keys whenever the file changes, so keys can be added or
    # revoked without a server restart
    if _watch_task is None:
        _watch_task = asyncio.create_task(_watch_auth_file())

    logger.info(
        f"Login passwords are also API keys: the Linux account password for "
        f"admin endpoints, and each Tabby-only user's password for API calls.\n"
        f"Optional extra keys from {AUTH_FILE}: {_format_api_keys(AUTH_KEYS)}. "
        f"Extra admin key: {'present' if AUTH_KEYS.admin_key else 'none'}.\n"
        "If yaml keys get compromised, delete api_tokens.yml and restart. "
        "Have fun!"
    )


def clear_password_cache() -> None:
    with _password_cache_lock:
        _password_cache.clear()


def _token_digest(test_key: str) -> str:
    return hashlib.sha256(test_key.encode("utf-8")).hexdigest()


def _extra_users_stamp() -> str:
    from ui.users import password_hashes_stamp

    return password_hashes_stamp()


def _linux_auth_stamp() -> str:
    try:
        return str(os.stat("/etc/shadow").st_mtime)
    except OSError:
        return ""


def _password_cache_stamp() -> str:
    return f"{_extra_users_stamp()}\0{_linux_auth_stamp()}"


def _password_cache_ttl(perm: Optional[str]) -> float:
    if not perm:
        return PASSWORD_FAIL_TTL_S
    if perm == "admin" and not _linux_auth_stamp():
        return 15.0
    return PASSWORD_CACHE_TTL_S


def _presented_token(*candidates: Optional[str]) -> Optional[str]:
    for raw in candidates:
        if not isinstance(raw, str) or not raw:
            continue
        if raw.lower().startswith("bearer"):
            parts = raw.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                continue
            return parts[1]
        return raw
    return None


def _yaml_permission(test_key: str) -> Optional[str]:
    auth_keys = AUTH_KEYS
    if auth_keys is None:
        return None
    if auth_keys.verify_key(test_key, "admin_key"):
        return "admin"
    if auth_keys.verify_key(test_key, "api_key"):
        return "api"
    return None


def _login_permission(test_key: str) -> Optional[str]:
    from ui.auth import authenticate_user, stack_username
    from ui.users import match_password

    if match_password(test_key):
        return "api"
    admin = stack_username()
    if admin and authenticate_user(admin, test_key):
        return "admin"
    return None


def permission_for_token(test_key: str) -> Optional[str]:
    """Return 'admin', 'api', or None for a presented API/admin token."""
    if not test_key:
        return None
    yaml_perm = _yaml_permission(test_key)
    if yaml_perm:
        return yaml_perm
    digest = _token_digest(test_key)
    stamp = _password_cache_stamp()
    now = time.time()
    with _password_cache_lock:
        hit = _password_cache.get(digest)
        if hit is not None:
            perm, expires, cached_stamp = hit
            if now < expires and cached_stamp == stamp:
                return perm
    perm = _login_permission(test_key)
    ttl = _password_cache_ttl(perm)
    with _password_cache_lock:
        _password_cache[digest] = (perm, now + ttl, stamp)
    return perm


def get_key_permission(request: Request):
    """
    Gets the key permission from a request.

    Internal only! Use the depends functions for incoming requests.
    """

    # Give full admin permissions if auth is disabled
    if DISABLE_AUTH:
        return "admin"

    test_key = _presented_token(
        request.headers.get("x-admin-key"),
        request.headers.get("x-api-key"),
        request.headers.get("authorization"),
    )

    if test_key is None:
        raise ValueError("The provided authentication key is missing.")

    perm = permission_for_token(test_key)
    if perm:
        return perm
    raise ValueError("The provided authentication key is invalid.")


async def check_api_key(x_api_key: str = Header(None), authorization: str = Header(None)):
    """Check if the API key is valid."""

    # Allow request if auth is disabled
    if DISABLE_AUTH:
        return

    token = _presented_token(x_api_key, authorization)
    if not token:
        raise HTTPException(401, "Please provide an API key")
    perm = await asyncio.to_thread(permission_for_token, token)
    if perm not in ("admin", "api"):
        raise HTTPException(401, "Invalid API key")
    return x_api_key if isinstance(x_api_key, str) else authorization


async def check_admin_key(x_admin_key: str = Header(None), authorization: str = Header(None)):
    """Check if the admin key is valid."""

    # Allow request if auth is disabled
    if DISABLE_AUTH:
        return

    token = _presented_token(x_admin_key, authorization)
    if not token:
        raise HTTPException(401, "Please provide an admin key")
    perm = await asyncio.to_thread(permission_for_token, token)
    if perm != "admin":
        raise HTTPException(401, "Invalid admin key")
    return x_admin_key if isinstance(x_admin_key, str) else authorization
