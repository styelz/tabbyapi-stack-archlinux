"""Short-lived tokens that let a browser load a Code-mode project as a site.

The session cookie cannot be used here. Preview pages are served under a CSP
sandbox so LLM-written HTML gets an opaque origin instead of the console's
origin, and a sandboxed document does not send SameSite=Lax cookies with its
own subresource requests. The token sits in the path instead, so relative
``src`` and ``href`` values resolve back onto an authorized URL.

That opaque origin also makes ``window.localStorage`` throw. Generated pages
often persist into it, so HTML responses get a Storage shim. Writes go to a
sidecar file next to the workspace (not the project tree), and the next HTML
response embeds those keys. Do not add ``allow-same-origin``: that would give
the page the console origin.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

TOKEN_TTL_S = 2 * 60 * 60
MAX_TOKENS = 200
STORAGE_MAX_BYTES = 256 * 1024
STORAGE_ROUTE = "__tabby_storage"
STORAGE_FILE_SUFFIX = ".preview-storage.json"
# No allow-same-origin: the page must not reach the console DOM or its cookies.
SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-modals allow-popups "
    "allow-top-navigation-by-user-activation"
)
STORAGE_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
_STORAGE_MARK = "data-tabby-preview-storage"
_BROWSER_MARK = "data-tabby-preview-browser"

_tokens: dict[str, dict] = {}
_lock = threading.Lock()
_storage_lock = threading.Lock()


def _prune(now: float) -> None:
    dead = [key for key, row in _tokens.items() if now - row["created_at"] > TOKEN_TTL_S]
    for key in dead:
        _tokens.pop(key, None)
    if len(_tokens) <= MAX_TOKENS:
        return
    oldest = sorted(_tokens.items(), key=lambda item: item[1]["created_at"])
    for key, _row in oldest[: len(_tokens) - MAX_TOKENS]:
        _tokens.pop(key, None)


def mint(username: str, chat_id: str) -> str:
    """Reuse a live token for this chat so a reload keeps the same URL."""
    now = time.time()
    with _lock:
        _prune(now)
        for key, row in _tokens.items():
            if row["username"] == username and row["chat_id"] == chat_id:
                row["created_at"] = now
                return key
        token = secrets.token_urlsafe(24)
        _tokens[token] = {"username": username, "chat_id": chat_id, "created_at": now}
        return token


def resolve(token: str) -> Optional[tuple[str, str]]:
    if not token:
        return None
    now = time.time()
    with _lock:
        _prune(now)
        row = _tokens.get(token)
        if not row:
            return None
        # Sliding window: an open preview tab keeps working while it is in use.
        row["created_at"] = now
        return row["username"], row["chat_id"]


def drop_chat(username: str, chat_id: str) -> None:
    with _lock:
        dead = [
            key
            for key, row in _tokens.items()
            if row["username"] == username and row["chat_id"] == chat_id
        ]
        for key in dead:
            _tokens.pop(key, None)


def drop_user(username: str) -> None:
    with _lock:
        dead = [key for key, row in _tokens.items() if row["username"] == username]
        for key in dead:
            _tokens.pop(key, None)


def is_html_name(name: str) -> bool:
    return Path(name).suffix.lower() in {".html", ".htm"}


def spa_fallback_rels(rel: str) -> list[str]:
    """Extra preview paths to try when a hash/history route has no file."""
    posix = str(rel or "").replace("\\", "/").lstrip("/")
    if not posix or posix.endswith("/"):
        return []
    name = posix.rsplit("/", 1)[-1]
    if "." in name:
        return []
    out: list[str] = [f"{posix}/index.html", "index.html"]
    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def persist_url_for(rel: str) -> str:
    """Relative URL from this preview page to the token storage route."""
    parts = [part for part in str(rel or "").replace("\\", "/").split("/") if part]
    if len(parts) <= 1:
        return STORAGE_ROUTE
    return "../" * (len(parts) - 1) + STORAGE_ROUTE


def storage_path(username: str, chat_id: str) -> Path:
    from ui.workspace import safe_name, user_dir

    return user_dir(username) / f"{safe_name(chat_id)}{STORAGE_FILE_SUFFIX}"


def load_storage(username: str, chat_id: str) -> dict[str, str]:
    path = storage_path(username, chat_id)
    with _storage_lock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def save_storage(username: str, chat_id: str, raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("Storage must be an object.")
    store = {str(key): "" if value is None else str(value) for key, value in raw.items()}
    payload = json.dumps(store, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = payload.encode("utf-8")
    if len(data) > STORAGE_MAX_BYTES:
        raise ValueError("Preview storage is too large.")
    path = storage_path(username, chat_id)
    with _storage_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o600)


def drop_storage(username: str, chat_id: str) -> None:
    try:
        storage_path(username, chat_id).unlink()
    except OSError:
        pass


def _json_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _storage_shim(storage: dict[str, str], persist_url: str) -> str:
    return (
        f'<script {_STORAGE_MARK}="1">'
        "(function(){"
        f"var SEED={_json_script(storage)};"
        f"var ENDPOINT={_json_script(persist_url)};"
        "function memoryStorage(seed,persist){"
        "var data=Object.create(null),keys=[];"
        "if(seed){"
        "Object.keys(seed).forEach(function(k){"
        "data[k]=String(seed[k]);"
        "keys.push(k);"
        "});"
        "}"
        "function flush(){"
        "if(!persist||!ENDPOINT)return;"
        "try{"
        "var body=JSON.stringify(data);"
        "var blob=new Blob([body],{type:'text/plain'});"
        "if(navigator.sendBeacon&&navigator.sendBeacon(ENDPOINT,blob))return;"
        "fetch(ENDPOINT,{method:'POST',body:body,credentials:'omit',keepalive:true,mode:'no-cors'});"
        "}catch(err){}"
        "}"
        "if(persist){"
        "try{window.addEventListener('pagehide',flush);}catch(err){}"
        "}"
        "return{"
        "getItem:function(key){"
        "key=String(key);"
        "return Object.prototype.hasOwnProperty.call(data,key)?data[key]:null;"
        "},"
        "setItem:function(key,value){"
        "key=String(key);"
        "if(!Object.prototype.hasOwnProperty.call(data,key))keys.push(key);"
        "data[key]=String(value);"
        "flush();"
        "},"
        "removeItem:function(key){"
        "key=String(key);"
        "if(Object.prototype.hasOwnProperty.call(data,key)){"
        "delete data[key];keys=Object.keys(data);"
        "flush();"
        "}"
        "},"
        "clear:function(){data=Object.create(null);keys=[];flush();},"
        "key:function(i){return keys[i]==null?null:keys[i];},"
        "get length(){return keys.length;}"
        "};"
        "}"
        "function nativeWorks(name){"
        "try{"
        "var store=window[name];"
        "if(!store||typeof store.getItem!=='function')return false;"
        "store.getItem('__tabby_probe');"
        "return true;"
        "}catch(err){return false;}"
        "}"
        "function install(name,seed,persist){"
        "if(nativeWorks(name))return;"
        "var mem=memoryStorage(seed,persist);"
        "function overlay(obj){"
        "try{"
        "Object.defineProperty(obj,name,{"
        "configurable:true,enumerable:true,"
        "get:function(){return mem;},"
        "set:function(){}"
        "});"
        "return true;"
        "}catch(err){return false;}"
        "}"
        "try{delete window[name];}catch(err){}"
        "overlay(window);"
        "try{overlay(Window.prototype);}catch(err){}"
        "try{window[name]=mem;}catch(err){}"
        "overlay(window);"
        "}"
        'install("localStorage",SEED,true);'
        'install("sessionStorage",null,false);'
        "})();"
        "</script>"
    )


def _inject_after_open(html: str, mark: str, snippet: str) -> str:
    if mark in html:
        return html
    lower = html.lower()
    for tag in ("<head", "<html"):
        start = lower.find(tag)
        if start == -1:
            continue
        end = html.find(">", start)
        if end == -1:
            continue
        return html[: end + 1] + snippet + html[end + 1 :]
    return snippet + html


def inject_storage_shim(html: str, storage: dict[str, str], persist_url: str) -> str:
    """Put Storage on sandboxed previews before page scripts run."""
    return _inject_after_open(html, _STORAGE_MARK, _storage_shim(storage, persist_url))


def _browser_shim() -> str:
    """Tell the console about title, URL, and window.open so the preview can tab."""
    return (
        f'<script {_BROWSER_MARK}="1">'
        "(function(){"
        "function pageTitle(){"
        "if(document.title)return String(document.title);"
        "try{"
        "var parts=String(location.pathname||'').split('/');"
        "return decodeURIComponent(parts[parts.length-1]||'')||String(location.href||'');"
        "}catch(err){return String(location.href||'');}"
        "}"
        "function report(kind,extra){"
        "var msg={source:'tabby-preview',kind:kind,href:String(location.href||''),title:pageTitle()};"
        "if(extra){for(var k in extra){if(Object.prototype.hasOwnProperty.call(extra,k))msg[k]=extra[k];}}"
        "try{parent.postMessage(msg,'*');}catch(err){}"
        "}"
        "window.open=function(url,target){"
        "var href=url==null||url===''?'':String(url);"
        "try{href=new URL(href,location.href).href;}catch(err){}"
        "report('open',{href:href,target:String(target||'')});"
        "return {closed:false,close:function(){this.closed=true;},focus:function(){},blur:function(){},"
        "opener:window,location:{href:href,assign:function(){},replace:function(){},reload:function(){}},"
        "postMessage:function(){}};"
        "};"
        "function onClick(ev){"
        "var a=ev.target&&ev.target.closest?ev.target.closest('a[href]'):null;"
        "if(!a)return;"
        "var t=String(a.getAttribute('target')||'').toLowerCase();"
        "if(t==='_top'||t==='_parent'){"
        "ev.preventDefault();"
        "try{location.href=a.href;}catch(err){}"
        "return;"
        "}"
        "if(!(t==='_blank'||t==='_new'||ev.ctrlKey||ev.metaKey||ev.shiftKey||ev.button===1))return;"
        "ev.preventDefault();"
        "ev.stopPropagation();"
        "var href='';"
        "try{href=a.href;}catch(err){href=a.getAttribute('href')||'';}"
        "report('open',{href:href});"
        "}"
        "document.addEventListener('click',onClick,true);"
        "document.addEventListener('auxclick',onClick,true);"
        "window.addEventListener('message',function(ev){"
        "var d=ev.data;"
        "if(!d||d.source!=='tabby-preview-host')return;"
        "if(d.kind==='back')history.back();"
        "if(d.kind==='forward')history.forward();"
        "if(d.kind==='reload')location.reload();"
        "});"
        "var push=history.pushState;"
        "var replace=history.replaceState;"
        "if(push)history.pushState=function(){var r=push.apply(this,arguments);report('nav');return r;};"
        "if(replace)history.replaceState=function(){var r=replace.apply(this,arguments);report('nav');return r;};"
        "window.addEventListener('hashchange',function(){report('nav');});"
        "window.addEventListener('popstate',function(){report('nav');});"
        "window.addEventListener('load',function(){report('nav');});"
        "try{"
        "var head=document.head||document.documentElement;"
        "var last=document.title;"
        "new MutationObserver(function(){"
        "if(document.title===last)return;"
        "last=document.title;"
        "report('title');"
        "}).observe(head,{subtree:true,childList:true,characterData:true});"
        "}catch(err){}"
        "report('ready');"
        "})();"
        "</script>"
    )


def inject_browser_shim(html: str) -> str:
    """Open target=_blank and window.open as tabs in the console preview."""
    return _inject_after_open(html, _BROWSER_MARK, _browser_shim())


def html_preview_bytes(
    path: Path, *, username: str, chat_id: str, persist_url: str
) -> bytes:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Browser shim last so it sits first in <head> and patches window.open
    # before page scripts (storage injects first, then this prepends).
    text = inject_storage_shim(text, load_storage(username, chat_id), persist_url)
    text = inject_browser_shim(text)
    return text.encode("utf-8")
