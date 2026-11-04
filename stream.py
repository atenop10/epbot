"""Temporary streaming player for the episodes bot.

The bot sends a "Play" button instead of an audio file. The button opens a signed, expiring link to a small web
player served from this process (behind Caddy/nginx for HTTPS). The R2 bucket stays private: this server fetches
the audio from R2 and passes it to the browser, throttled so a whole episode can't be grabbed instantly.

Enabled only when STREAM_BASE_URL and STREAM_SECRET are set in .env.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from aiohttp import web

log = logging.getLogger("episodes-bot.stream")

BASE_URL = os.environ.get("STREAM_BASE_URL", "").strip().rstrip("/")
SECRET = os.environ.get("STREAM_SECRET", "").strip().encode()
ENABLED = bool(BASE_URL and SECRET)
PORT = int(os.environ.get("STREAM_PORT", "8080"))
RATE = max(int(os.environ.get("STREAM_RATE_KBPS", "80")), 16) * 1024   # bytes/second after the initial burst
MAX_DEVICES = max(int(os.environ.get("STREAM_MAX_DEVICES", "2")), 1)    # browsers that may use one link
SESSION_SECONDS = int(os.environ.get("STREAM_SESSION_MIN", "90")) * 60  # lifetime of prev/next links
AD_FILE = os.environ.get("STREAM_AD_FILE", "ad.html")        # ad network code (set with /setad in the bot)
AD_HEIGHT = int(os.environ.get("STREAM_AD_HEIGHT", "280"))    # 280 fits a 300x250 banner, use 60 for 320x50
BURST = 1_000_000   # bytes sent at full speed so playback starts instantly
CHUNK = 64 * 1024

CTX = {}  # filled by configure(): db, s3, bucket, check_access
CLAIMS = {}  # token -> list of cookie values (browsers that opened it)
CTYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
          ".opus": "audio/ogg", ".webm": "audio/webm", ".flac": "audio/flac"}
RANGE_RE = re.compile(r"^bytes=\d*-\d*$")


_ad_cache = {"mtime": None, "html": ""}


def ad_html():
    """Ad code pasted by the admin (empty string = no ad). Re-read only when the file changes."""
    try:
        mtime = os.stat(AD_FILE).st_mtime
        if _ad_cache["mtime"] != mtime:
            with open(AD_FILE, encoding="utf-8") as f:
                _ad_cache.update(mtime=mtime, html=f.read().strip())
        return _ad_cache["html"]
    except OSError:
        return ""


def configure(db, s3, bucket, check_access):
    CTX.update(db=db, s3=s3, bucket=bucket, check_access=check_access)


# ------------------------------------------------------------ signed tokens
def _sig(payload):
    mac = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(uid, sid, seq, exp):
    payload = f"{uid}.{sid}.{seq}.{exp}"
    return f"{payload}.{_sig(payload)}"


def parse_token(tok):
    """Returns (uid, sid, seq, exp) or None if forged. Expiry is checked separately."""
    parts = tok.split(".")
    if len(parts) != 5:
        return None
    payload = ".".join(parts[:4])
    if not hmac.compare_digest(_sig(payload), parts[4]):
        return None
    try:
        uid, sid, seq, exp = (int(x) for x in parts[:4])
    except ValueError:
        return None
    return uid, sid, seq, exp


def url_for(uid, sid, seq, exp):
    return f"{BASE_URL}/p/{make_token(uid, sid, seq, exp)}"


# ------------------------------------------------------------ helpers
def _page(msg, status=403):
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Link unavailable</title><body style=\"font-family:system-ui;background:#0f1115;color:#e8e8ea;"
            "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;"
            f"padding:24px\"><div><h2>🎧</h2><p>{html.escape(msg)}</p></div>")
    return web.Response(text=body, status=status, content_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def _cookie_name(tok):
    return "c" + tok.rsplit(".", 1)[1][:10]


def _prune():
    if len(CLAIMS) < 2000:
        return
    now = time.time()
    for t in [t for t in CLAIMS if int(t.split(".")[3]) < now]:
        CLAIMS.pop(t, None)


def _episode(sid, seq):
    with closing(CTX["db"]()) as con:
        return con.execute("SELECT key, size FROM episodes WHERE story_id=? AND seq=?", (sid, seq)).fetchone()


def _neighbors(sid, seq):
    with closing(CTX["db"]()) as con:
        rows = con.execute("SELECT seq FROM episodes WHERE story_id=? AND seq IN (?,?) AND key NOT LIKE 'tg/%'",
                           (sid, seq - 1, seq + 1)).fetchall()
        title = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()
    have = {r["seq"] for r in rows}
    return seq - 1 in have, seq + 1 in have, (title["title"] if title and title["title"] else f"Story {sid}")


async def _authorize(tok):
    """Common checks for the page and the audio: signature, expiry, subscription. Returns (info, error_response)."""
    info = parse_token(tok)
    if not info:
        return None, _page("This link is not valid.")
    uid, sid, seq, exp = info
    if exp < time.time():
        return None, _page("This link has expired. Go back to the bot and request the episode again.", 410)
    ok, _ = await CTX["check_access"](uid)
    if not ok:
        return None, _page("Your subscription has ended. Go back to the bot for details.")
    return info, None


# ------------------------------------------------------------ player page
PLAYER = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<meta name=referrer content=no-referrer><title>@@TITLE@@ - Ep @@SEQ@@</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e8e8ea;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-user-select:none;user-select:none}
main{width:100%;max-width:420px;padding:28px 22px;text-align:center}
.t{font-size:20px;font-weight:600;margin-bottom:4px}.e{color:#9aa0aa;margin-bottom:28px}
input[type=range]{width:100%;accent-color:#7c9cff;height:28px}.tm{display:flex;justify-content:space-between;color:#9aa0aa;font-size:13px}
.row{display:flex;align-items:center;justify-content:center;gap:14px;margin:22px 0}
button{background:#1b1f29;color:#e8e8ea;border:0;border-radius:50%;width:48px;height:48px;font-size:14px;cursor:pointer}
#b{width:68px;height:68px;font-size:24px;background:#7c9cff;color:#0f1115}
.nav{display:flex;justify-content:space-between;margin-top:8px}.nav a{color:#7c9cff;text-decoration:none;padding:10px 4px}
.nav span{color:#444b59;padding:10px 4px}.n{color:#6b7280;font-size:12px;margin-top:26px}
.ad{margin-top:22px}.ad small{display:block;color:#6b7280;font-size:11px;margin-bottom:4px}
.ad iframe{border:0;width:100%;max-width:340px;height:@@ADH@@px;background:transparent}
</style></head><body data-next="@@NEXT@@"><main>
<div class=t>@@TITLE@@</div><div class=e>Episode @@SEQ@@</div>
<audio id=a src="/a/@@TOK@@" preload=auto controlsList="nodownload"></audio>
<input type=range id=s min=0 max=1000 value=0><div class=tm><span id=c>0:00</span><span id=d>0:00</span></div>
<div class=row><button id=bk>-15</button><button id=b>▶</button><button id=fw>+15</button><button id=sp>1x</button></div>
<div class=nav>@@PREV@@@@NEXTLINK@@</div>@@AD@@<div class=n>Streaming link - expires @@EXP@@ min after you open it</div></main>
<script>
const a=document.getElementById('a'),b=document.getElementById('b'),s=document.getElementById('s'),
c=document.getElementById('c'),d=document.getElementById('d'),sp=document.getElementById('sp'),KEY='pos:@@SID@@:@@SEQ@@';
const f=t=>{t=Math.floor(t||0);const m=Math.floor(t/60),x=t%60;return m+':'+(x<10?'0':'')+x};let seeking=false;
b.onclick=()=>a.paused?a.play():a.pause();a.onplay=()=>b.textContent='❚❚';a.onpause=()=>b.textContent='▶';
a.onloadedmetadata=()=>{d.textContent=f(a.duration);try{const p=parseFloat(localStorage.getItem(KEY));if(p>5&&p<a.duration-5)a.currentTime=p}catch(e){}};
a.ontimeupdate=()=>{if(!seeking&&a.duration)s.value=a.currentTime/a.duration*1000;c.textContent=f(a.currentTime)};
s.oninput=()=>{seeking=true;c.textContent=f(s.value/1000*a.duration)};
s.onchange=()=>{a.currentTime=s.value/1000*a.duration;seeking=false};
document.getElementById('bk').onclick=()=>{a.currentTime=Math.max(0,a.currentTime-15)};
document.getElementById('fw').onclick=()=>{a.currentTime=Math.min(a.duration||1e9,a.currentTime+15)};
const R=[1,1.25,1.5,2];let ri=0;sp.onclick=()=>{ri=(ri+1)%R.length;a.playbackRate=R[ri];sp.textContent=R[ri]+'x'};
setInterval(()=>{try{if(!a.paused)localStorage.setItem(KEY,a.currentTime)}catch(e){}},5000);
a.onended=()=>{try{localStorage.removeItem(KEY)}catch(e){}const n=document.body.dataset.next;if(n)location.href=n};
document.addEventListener('contextmenu',e=>e.preventDefault());
</script></body></html>"""


async def player(request):
    tok = request.match_info["tok"]
    info, err = await _authorize(tok)
    if err:
        return err
    uid, sid, seq, exp = info
    ep = await asyncio.to_thread(_episode, sid, seq)
    if not ep or ep["key"].startswith("tg/"):
        return _page("This episode is not available for streaming.", 404)
    # claim the link for this browser (a few browsers allowed, so Telegram's in-app browser -> Chrome still works)
    _prune()
    vals = CLAIMS.setdefault(tok, [])
    cookie = request.cookies.get(_cookie_name(tok))
    new_cookie = None
    if not (cookie and cookie in vals):
        if len(vals) >= MAX_DEVICES:
            return _page("This link was already opened on another device. Request the episode again from the bot.")
        new_cookie = secrets.token_urlsafe(9)
        vals.append(new_cookie)
    has_prev, has_next, title = await asyncio.to_thread(_neighbors, sid, seq)
    new_exp = int(time.time()) + SESSION_SECONDS
    prev_url = f"/p/{make_token(uid, sid, seq - 1, new_exp)}" if has_prev else ""
    next_url = f"/p/{make_token(uid, sid, seq + 1, new_exp)}" if has_next else ""
    page = (PLAYER.replace("@@TITLE@@", html.escape(title)).replace("@@SEQ@@", str(seq)).replace("@@SID@@", str(sid))
            .replace("@@TOK@@", tok).replace("@@EXP@@", str(SESSION_SECONDS // 60)).replace("@@NEXT@@", next_url)
            .replace("@@PREV@@", f'<a href="{prev_url}">◀ Prev</a>' if prev_url else "<span>◀ Prev</span>")
            .replace("@@ADH@@", str(AD_HEIGHT))
            .replace("@@AD@@", (f'<div class=ad><small>Advertisement</small><iframe src="/ad/{tok}" loading=lazy '
                                'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
                                'referrerpolicy="origin"></iframe></div>') if ad_html() else "")
            .replace("@@NEXTLINK@@", f'<a href="{next_url}">Next ▶</a>' if next_url else "<span>Next ▶</span>"))
    resp = web.Response(text=page, content_type="text/html", headers={
        "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; media-src 'self'"})
    if new_cookie:
        resp.set_cookie(_cookie_name(tok), new_cookie, max_age=max(exp - int(time.time()), 60), httponly=True,
                        secure=True, samesite="Lax", path="/")
    return resp


# ------------------------------------------------------------ ad frame (sandboxed: ad scripts can't touch the player)
async def ad_frame(request):
    tok = request.match_info["tok"]
    info, err = await _authorize(tok)
    if err:
        return err
    code = ad_html()
    if not code:
        return web.Response(status=204)
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>html,body{margin:0;background:transparent;display:flex;justify-content:center}</style>" + code)
    return web.Response(text=page, content_type="text/html", headers={
        "Cache-Control": "no-store", "X-Robots-Tag": "noindex",
        # opaque origin: no cookies, no access to the parent page or the audio, popups allowed for ad clicks
        "Content-Security-Policy": "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
                                   "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'"})


# ------------------------------------------------------------ audio (throttled, range-capable)
async def audio(request):
    tok = request.match_info["tok"]
    info, err = await _authorize(tok)
    if err:
        return err
    uid, sid, seq, exp = info
    cookie = request.cookies.get(_cookie_name(tok))
    if not (cookie and cookie in CLAIMS.get(tok, [])):
        return _page("Open this episode from its player link.")
    ep = await asyncio.to_thread(_episode, sid, seq)
    if not ep or ep["key"].startswith("tg/"):
        return web.Response(status=404)
    rng = request.headers.get("Range", "")
    kw = {"Range": rng} if RANGE_RE.match(rng) else {}
    s3 = CTX["s3"]
    try:
        obj = await asyncio.to_thread(lambda: s3.get_object(Bucket=CTX["bucket"], Key=ep["key"], **kw))
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("InvalidRange", "416"):
            return web.Response(status=416)
        log.warning("R2 get failed for %s: %s", ep["key"], e)
        return web.Response(status=404)
    ctype = CTYPES.get(os.path.splitext(ep["key"])[1].lower(), "audio/mpeg")
    headers = {"Content-Type": ctype, "Accept-Ranges": "bytes", "Content-Length": str(obj["ContentLength"]),
               "Cache-Control": "private, no-store", "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"}
    status = 200
    if kw and obj.get("ContentRange"):
        status = 206
        headers["Content-Range"] = obj["ContentRange"]
    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)
    body, sent, t0 = obj["Body"], 0, time.monotonic()
    try:
        while True:
            chunk = await asyncio.to_thread(body.read, CHUNK)
            if not chunk:
                break
            await resp.write(chunk)
            sent += len(chunk)
            if sent > BURST:  # throttle after the burst so a full download can't be grabbed instantly
                delay = (sent - BURST) / RATE - (time.monotonic() - t0)
                if delay > 0:
                    await asyncio.sleep(delay)
        await resp.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # listener closed the page or seeked
    finally:
        try:
            body.close()
        except Exception:
            pass
    return resp


# ------------------------------------------------------------ lifecycle (run inside the bot's event loop)
async def start(_application=None):
    if not ENABLED:
        return
    # many listeners at once: give blocking R2 reads plenty of worker threads (the default is only a handful)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))
    app = web.Application()
    app.router.add_get("/p/{tok}", player)
    app.router.add_get("/a/{tok}", audio)
    app.router.add_get("/ad/{tok}", ad_frame)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    CTX["runner"] = runner
    log.info("Streaming player listening on 127.0.0.1:%s (public: %s)", PORT, BASE_URL)


async def stop(_application=None):
    runner = CTX.get("runner")
    if runner:
        await runner.cleanup()
