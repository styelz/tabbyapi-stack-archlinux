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
# No allow-same-origin on top-level previews: that would be the console origin
# and cookies. Same-origin iframe embeds set credentialless + allow-same-origin
# so native localStorage works without parent DOM access.
# script-src omits chrome-extension: so wallet injects cannot throw on Storage.
_SANDBOX_FLAGS = (
    "allow-scripts allow-forms allow-modals allow-popups "
    "allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation"
)
_SANDBOX_REST = (
    "default-src 'self' https: http: data: blob: ws: wss:; "
    "script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' 'self' https: http: blob: data:; "
    "style-src 'unsafe-inline' 'self' https: http: data:; "
    "img-src * data: blob:; media-src * data: blob:; font-src * data: blob:; "
    "connect-src 'self' https: http: ws: wss: blob:; "
    "frame-src https: http: blob: data:; worker-src 'self' blob:; object-src 'none'"
)


def sandbox_csp(*, allow_same_origin: bool = False) -> str:
    flags = _SANDBOX_FLAGS
    if allow_same_origin:
        flags = f"allow-same-origin {flags}"
    return f"sandbox {flags}; {_SANDBOX_REST}"


SANDBOX_CSP = sandbox_csp()


def preview_embed_allows_storage(headers: dict[str, str] | None) -> bool:
    """True when the console iframe is loading this document (not a top-level tab)."""
    raw = {str(key).lower(): str(value or "").lower() for key, value in (headers or {}).items()}
    return raw.get("sec-fetch-dest") == "iframe" and raw.get("sec-fetch-site") == "same-origin"
STORAGE_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
_STORAGE_MARK = "data-tabby-preview-storage"
_BROWSER_MARK = "data-tabby-preview-browser"
_SCREENSHOT_MARK = "data-tabby-preview-screenshot"

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
    head = (
        f'<script {_BROWSER_MARK}="1" {_SCREENSHOT_MARK}="1">'
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
    )
    tail = (
        "window.addEventListener('message',function(ev){"
        "var d=ev.data;"
        "if(!d||d.source!=='tabby-preview-host')return;"
        "if(d.kind==='screenshot'){captureScreenshot(d);return;}"
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
    return head + _screenshot_js() + tail


def _screenshot_js() -> str:
    """Paint the preview DOM onto a canvas. SVG foreignObject taints Chrome."""
    return (
        "function isTransparent(c){"
        "return !c||c==='transparent'||c==='rgba(0, 0, 0, 0)'||c==='rgba(0,0,0,0)';"
        "}"
        "function parsePx(v){"
        "var n=parseFloat(v);"
        "return isFinite(n)?n:0;"
        "}"
        "function inView(r,w,h){"
        "return r.width>=1&&r.height>=1&&r.bottom>0&&r.right>0&&r.top<h&&r.left<w;"
        "}"
        "function drawBg(ctx,el,r,cs){"
        "if(isTransparent(cs.backgroundColor))return;"
        "ctx.fillStyle=cs.backgroundColor;"
        "var rad=parsePx(cs.borderTopLeftRadius);"
        "if(rad>0&&ctx.roundRect){"
        "ctx.beginPath();"
        "ctx.roundRect(r.left,r.top,r.width,r.height,rad);"
        "ctx.fill();"
        "}else ctx.fillRect(r.left,r.top,r.width,r.height);"
        "}"
        "function drawBorder(ctx,r,cs){"
        "var tw=parsePx(cs.borderTopWidth),rw=parsePx(cs.borderRightWidth);"
        "var bw=parsePx(cs.borderBottomWidth),lw=parsePx(cs.borderLeftWidth);"
        "if(tw<0.5&&rw<0.5&&bw<0.5&&lw<0.5)return;"
        "if(tw>=0.5&&cs.borderTopStyle!=='none'){"
        "ctx.fillStyle=cs.borderTopColor;ctx.fillRect(r.left,r.top,r.width,tw);"
        "}"
        "if(rw>=0.5&&cs.borderRightStyle!=='none'){"
        "ctx.fillStyle=cs.borderRightColor;ctx.fillRect(r.right-rw,r.top,rw,r.height);"
        "}"
        "if(bw>=0.5&&cs.borderBottomStyle!=='none'){"
        "ctx.fillStyle=cs.borderBottomColor;ctx.fillRect(r.left,r.bottom-bw,r.width,bw);"
        "}"
        "if(lw>=0.5&&cs.borderLeftStyle!=='none'){"
        "ctx.fillStyle=cs.borderLeftColor;ctx.fillRect(r.left,r.top,lw,r.height);"
        "}"
        "}"
        "function drawText(ctx,el,cs){"
        "if(cs.visibility==='hidden'||parseFloat(cs.opacity)===0)return;"
        "ctx.fillStyle=cs.color||'#e8ecf4';"
        "ctx.font=cs.font||'16px sans-serif';"
        "ctx.textBaseline='top';"
        "ctx.textAlign='left';"
        "for(var i=0;i<el.childNodes.length;i++){"
        "var n=el.childNodes[i];"
        "if(!n||n.nodeType!==3)continue;"
        "var text=n.textContent;"
        "if(!text||!String(text).trim())continue;"
        "try{"
        "var range=document.createRange();"
        "range.selectNodeContents(n);"
        "var rects=range.getClientRects();"
        "var parts=String(text).replace(/\\s+/g,' ').trim();"
        "if(!rects.length)continue;"
        "if(rects.length===1){"
        "ctx.fillText(parts,rects[0].left,rects[0].top,Math.max(1,rects[0].width));"
        "}else{"
        "var words=parts.split(' ');"
        "var wi=0;"
        "for(var ri=0;ri<rects.length;ri++){"
        "var box=rects[ri];"
        "var chunk='';"
        "while(wi<words.length){"
        "var next=chunk?chunk+' '+words[wi]:words[wi];"
        "if(chunk&&ctx.measureText(next).width>box.width+1)break;"
        "chunk=next;wi++;"
        "}"
        "if(chunk)ctx.fillText(chunk,box.left,box.top,Math.max(1,box.width));"
        "}"
        "}"
        "}catch(err){}"
        "}"
        "}"
        "function tryDrawImage(ctx,el,r){"
        "try{"
        "var scratch=document.createElement('canvas');"
        "scratch.width=Math.max(1,Math.round(r.width));"
        "scratch.height=Math.max(1,Math.round(r.height));"
        "scratch.getContext('2d').drawImage(el,0,0,scratch.width,scratch.height);"
        "scratch.toDataURL('image/png');"
        "ctx.drawImage(scratch,r.left,r.top,r.width,r.height);"
        "return true;"
        "}catch(err){return false;}"
        "}"
        "function paintPreview(width,height){"
        "var canvas=document.createElement('canvas');"
        "canvas.width=width;"
        "canvas.height=height;"
        "var ctx=canvas.getContext('2d');"
        "var root=document.documentElement;"
        "var body=document.body;"
        "ctx.fillStyle='#0b0d12';"
        "ctx.fillRect(0,0,width,height);"
        "try{"
        "var htmlCs=getComputedStyle(root);"
        "if(!isTransparent(htmlCs.backgroundColor)){"
        "ctx.fillStyle=htmlCs.backgroundColor;ctx.fillRect(0,0,width,height);"
        "}"
        "if(body){"
        "var bodyCs=getComputedStyle(body);"
        "if(!isTransparent(bodyCs.backgroundColor)){"
        "ctx.fillStyle=bodyCs.backgroundColor;ctx.fillRect(0,0,width,height);"
        "}"
        "}"
        "}catch(err){}"
        "var nodes=root.querySelectorAll('*');"
        "var limit=Math.min(nodes.length,2500);"
        "for(var i=0;i<limit;i++){"
        "var el=nodes[i];"
        "var tag=el.tagName;"
        "if(tag==='SCRIPT'||tag==='STYLE'||tag==='LINK'||tag==='NOSCRIPT'||tag==='HEAD'||tag==='META'||tag==='TITLE')continue;"
        "var cs=getComputedStyle(el);"
        "if(cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity)===0)continue;"
        "var r=el.getBoundingClientRect();"
        "if(!inView(r,width,height))continue;"
        "ctx.save();"
        "try{"
        "var op=parseFloat(cs.opacity);"
        "if(isFinite(op)&&op<1)ctx.globalAlpha=Math.max(0,op);"
        "drawBg(ctx,el,r,cs);"
        "if(tag==='IMG'||tag==='VIDEO')tryDrawImage(ctx,el,r);"
        "else if(tag==='CANVAS')tryDrawImage(ctx,el,r);"
        "drawBorder(ctx,r,cs);"
        "drawText(ctx,el,cs);"
        "}catch(err){}"
        "ctx.restore();"
        "}"
        "return canvas;"
        "}"
        "function captureScreenshot(req){"
        "var id=req&&req.id||'';"
        "var full=!!(req&&(req.full||req.full_page));"
        "function fail(err){"
        "report('screenshot',{id:id,ok:false,error:String((err&&err.message)||err||'capture failed')});"
        "}"
        "try{"
        "var vw=Math.max(1,Math.round(window.innerWidth||document.documentElement.clientWidth||800));"
        "var vh=Math.max(1,Math.round(window.innerHeight||document.documentElement.clientHeight||600));"
        "var width=vw;"
        "var height=full?Math.min(4096,Math.max(vh,Math.round(document.documentElement.scrollHeight||vh))):vh;"
        "function finish(){"
        "try{"
        "var canvas=paintPreview(width,height);"
        "var edge=Math.max(canvas.width,canvas.height)||1;"
        "var scale=edge>1280?1280/edge:1;"
        "var out=canvas;"
        "if(scale!==1){"
        "out=document.createElement('canvas');"
        "out.width=Math.max(1,Math.round(canvas.width*scale));"
        "out.height=Math.max(1,Math.round(canvas.height*scale));"
        "var ctx=out.getContext('2d');"
        "ctx.fillStyle='#0b0d12';"
        "ctx.fillRect(0,0,out.width,out.height);"
        "ctx.drawImage(canvas,0,0,out.width,out.height);"
        "}"
        "report('screenshot',{"
        "id:id,ok:true,dataUrl:out.toDataURL('image/jpeg',0.82),"
        "width:out.width,height:out.height,full:full"
        "});"
        "}catch(err){fail(err);}"
        "}"
        "if(document.fonts&&document.fonts.ready){"
        "var timer=setTimeout(finish,400);"
        "document.fonts.ready.then(function(){clearTimeout(timer);finish();}).catch(function(){clearTimeout(timer);finish();});"
        "}else finish();"
        "}catch(err){fail(err);}"
        "}"
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
