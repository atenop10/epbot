"""Telegram Mini App for the episodes bot.

Served by the same aiohttp server as the streaming player (see stream.py), so it needs the same
STREAM_BASE_URL / STREAM_SECRET settings and the same HTTPS domain. Nothing new to install.

  GET  /app                the Mini App page (single HTML file, miniapp.html)
  GET  /api/config         small settings for the page
  GET  /api/list           series list / search            (?q=&sort=new|az&offset=)
  GET  /api/story/<id>     one series with its episode numbers
  POST /api/play           gives an audio URL for one episode
  POST /api/send           sends a Telegram-stored episode to the user's chat
  POST /api/request        "please add this series" request
  GET  /m/<token>          the audio stream for the Mini App
  GET  /adframe            the /setad banner (sandboxed), shown in the app if an ad is set
  POST /api/ad/start       rewarded ads: gives a signed one-time token before the ad is shown
  POST /api/ad/claim       rewarded ads: after the ad, turns the token into time-limited access (see adpass.py)

Every /api call must carry Telegram's signed "initData" (header X-Init-Data). It is checked with the bot token,
so nobody can pretend to be another user. The bot's own rules (subscription, force-join) are applied on every call.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import closing
from urllib.parse import parse_qsl

from aiohttp import web

import adpass
import stream

log = logging.getLogger("episodes-bot.miniapp")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
INIT_MAX_AGE = 2 * 86400          # how long a Telegram login stays valid while the app is open
PAGE_SIZE = 30
# The audio link only works in the browser that logged in (cookie), so a copied link is useless elsewhere.
# Set MINIAPP_REQUIRE_COOKIE=false only if audio does not play in Telegram Web (browser version).
REQUIRE_COOKIE = os.environ.get("MINIAPP_REQUIRE_COOKIE", "true").strip().lower() not in ("0", "false", "no", "off")
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")
COOKIE = "mt"

_rate = {}
_html_cache = {"mtime": None, "html": ""}


# ------------------------------------------------------------ helpers
def _json(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _limited(uid, bucket, n, per):
    """True if this user did `bucket` more than n times in the last `per` seconds."""
    now = time.time()
    if len(_rate) > 5000:
        _rate.clear()
    key = (uid, bucket)
    hits = [t for t in _rate.get(key, []) if now - t < per]
    if len(hits) >= n:
        _rate[key] = hits
        return True
    hits.append(now)
    _rate[key] = hits
    return False


def check_init_data(init_data, bot_token=None, max_age=INIT_MAX_AGE):
    """Validate Telegram WebApp initData. Returns the user dict, or None if forged / expired."""
    token = (bot_token if bot_token is not None else BOT_TOKEN)
    if not init_data or len(init_data) > 4096 or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    got = pairs.pop("hash", "")
    if not got:
        return None
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()

    def calc(d):
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        return hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()

    ok = hmac.compare_digest(calc(pairs), got)
    if not ok and "signature" in pairs:  # newer Telegram versions add a 'signature' field; some checks leave it out
        d = dict(pairs)
        d.pop("signature")
        ok = hmac.compare_digest(calc(d), got)
    if not ok:
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", ""))
        user["id"] = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    return user


def _like_clauses(q):
    """Every word of the search must appear in the title."""
    clauses, params = [], []
    for w in q.split()[:6]:
        clauses.append("s.title LIKE ? ESCAPE '\\'")
        params.append("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return clauses, params


async def _gate(uid, allow_adwall=False):
    """Subscription + force-join rules of the bot. Returns None if allowed, else an error response.
    With allow_adwall, a user who only lacks time (ad-unlock mode) is let through to the few endpoints that unlock it."""
    cb = stream.CTX.get("gate")
    if cb:
        res = await cb(uid)
    else:
        ok, msg = await stream.CTX["check_access"](uid)
        res = {"ok": ok, "msg": msg, "links": []}
    if res.get("ok") or (allow_adwall and res.get("adwall")):
        return None
    return _json({"error": "gate", "adwall": bool(res.get("adwall")), "msg": res.get("msg", "Access denied."),
                  "links": res.get("links", [])}, 403)


def _api(allow_adwall):
    def deco(fn):
        async def wrapper(request):
            user = check_init_data(request.headers.get("X-Init-Data", ""))
            if not user:
                return _json({"error": "auth"}, 401)
            denied = await _gate(user["id"], allow_adwall)
            if denied:
                return denied
            try:
                return await fn(request, user)
            except web.HTTPException:
                raise
            except Exception:
                log.exception("miniapp api error in %s", fn.__name__)
                return _json({"error": "server"}, 500)
        return wrapper
    return deco


api = _api(False)      # normal endpoints: need access (paid, admin, or an active ad pass)
api_ad = _api(True)    # config + ad endpoints: also open to a user who just needs to watch an ad


async def _body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _int(v, default=0, lo=0, hi=10**9):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ page
def _page_html():
    try:
        mtime = os.stat(HTML_FILE).st_mtime
        if _html_cache["mtime"] != mtime:
            with open(HTML_FILE, encoding="utf-8") as f:
                _html_cache.update(mtime=mtime, html=f.read())
        return _html_cache["html"]
    except OSError:
        return ""


def app_csp():
    if stream.head_html() or adpass.ENABLED:  # ad network tag (/sethead) or rewarded-ad SDK: let its https scripts/frames load
        return stream.player_csp(force=True)
    return ("default-src 'self'; script-src 'unsafe-inline' https://telegram.org; style-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; media-src 'self'; frame-src 'self'")


async def page(request):
    html_ = _page_html()
    if not html_:
        return web.Response(text="miniapp.html is missing next to miniapp.py", status=500)
    html_ = html_.replace("@@HEAD@@", stream.head_html())
    return web.Response(text=html_, content_type="text/html", headers={
        "Cache-Control": "no-cache", "X-Robots-Tag": "noindex", "Content-Security-Policy": app_csp()})


async def adframe(request):
    code = stream.ad_html()
    if not code:
        return web.Response(status=204)
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>html,body{margin:0;background:transparent;display:flex;justify-content:center}</style>" + code)
    return web.Response(text=body, content_type="text/html", headers={
        "Cache-Control": "no-store", "X-Robots-Tag": "noindex",
        "Content-Security-Policy": "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
                                   "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'"})


# ------------------------------------------------------------ API
@api_ad
async def config(request, user):
    cb = stream.CTX.get("adstatus")
    reward = await cb(user["id"]) if cb else {"on": False}
    return _json({"ad": bool(stream.ad_html()), "adh": stream.AD_HEIGHT, "uid": user["id"],
                  "name": user.get("first_name", ""), "reward": reward})


@api_ad
async def ad_start(request, user):
    if not adpass.ENABLED:
        return _json({"error": "off", "msg": "Ad unlock is not turned on."}, 404)
    if _limited(user["id"], "adstart", 10, 60):
        return _json({"error": "slow", "msg": "Too many requests - wait a moment."}, 429)
    return _json({"token": adpass.make_token(user["id"])})


@api_ad
async def ad_claim(request, user):
    if not adpass.ENABLED:
        return _json({"error": "off", "msg": "Ad unlock is not turned on."}, 404)
    if _limited(user["id"], "adclaim", 10, 60):
        return _json({"error": "slow", "msg": "Too many requests - wait a moment."}, 429)
    data = await _body(request)
    res = await asyncio.to_thread(adpass.claim, stream.CTX["db"], user["id"], data.get("token", ""), bool(data.get("failed")))
    if res.get("ok"):
        log.info("ad pass: user %s +%s min (%s)", user["id"], res.get("mins"), res.get("kind"))
    return _json(res)


def _list_sql(q, sort, limit, offset):
    clauses, params = _like_clauses(q)
    where = "s.title IS NOT NULL AND s.title<>''" + "".join(" AND " + c for c in clauses)
    order = "m DESC, s.title COLLATE NOCASE" if sort == "new" else "s.title COLLATE NOCASE"
    sql = ("SELECT s.story_id id, s.title title, COUNT(e.key) c, "
           "SUM(CASE WHEN e.key LIKE 'tg/%' THEN 0 ELSE 1 END) p, MAX(e.modified) m "
           "FROM stories s JOIN episodes e ON e.story_id=s.story_id "
           f"WHERE {where} GROUP BY s.story_id ORDER BY {order} LIMIT ? OFFSET ?")
    return sql, params + [limit + 1, offset]


def _list_rows(q, sort, offset):
    sql, params = _list_sql(q, sort, PAGE_SIZE, offset)
    with closing(stream.CTX["db"]()) as con:
        return con.execute(sql, params).fetchall()


@api
async def list_series(request, user):
    q = request.query.get("q", "").strip()[:60]
    sort = "az" if request.query.get("sort") == "az" else "new"
    offset = _int(request.query.get("offset"), 0, 0, 100000)
    rows = await asyncio.to_thread(_list_rows, q, sort, offset)
    more = len(rows) > PAGE_SIZE
    items = [{"id": r["id"], "title": r["title"], "c": r["c"], "p": r["p"] or 0} for r in rows[:PAGE_SIZE]]
    return _json({"items": items, "more": more})


def _story(sid):
    with closing(stream.CTX["db"]()) as con:
        s = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()
        rows = con.execute("SELECT seq, key FROM episodes WHERE story_id=? ORDER BY seq", (sid,)).fetchall()
    return s, rows


@api
async def story(request, user):
    sid = _int(request.match_info.get("sid"), -1, -1)
    s, rows = await asyncio.to_thread(_story, sid)
    if not rows:
        return _json({"error": "notfound"}, 404)
    title = s["title"] if s and s["title"] else f"Story {sid}"
    return _json({"id": sid, "title": title, "eps": [[r["seq"], 0 if r["key"].startswith("tg/") else 1] for r in rows]})


def _play_info(sid, seq):
    with closing(stream.CTX["db"]()) as con:
        ep = con.execute("SELECT key FROM episodes WHERE story_id=? AND seq=?", (sid, seq)).fetchone()
        t = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()
        near = con.execute("SELECT seq FROM episodes WHERE story_id=? AND seq IN (?,?) AND key NOT LIKE 'tg/%'",
                           (sid, seq - 1, seq + 1)).fetchall()
    return ep, t, {r["seq"] for r in near}


def _log_play(uid, sid, seq):
    try:
        with closing(stream.CTX["db"]()) as con:
            con.execute("INSERT INTO plays(user_id, story_id, seq, ts) VALUES(?,?,?,?)", (uid, sid, seq, int(time.time())))
            con.commit()
    except Exception as e:
        log.warning("play log failed: %s", e)


def _cookie_value(uid, exp):
    mt = f"{uid}.{exp}"
    return f"{mt}.{stream._sig('m' + mt)}"


def _cookie_ok(request, uid):
    raw = request.cookies.get(COOKIE, "")
    parts = raw.split(".")
    if len(parts) != 3:
        return False
    mt = f"{parts[0]}.{parts[1]}"
    try:
        return (hmac.compare_digest(stream._sig("m" + mt), parts[2]) and int(parts[0]) == uid and int(parts[1]) > time.time())
    except ValueError:
        return False


@api
async def play(request, user):
    if _limited(user["id"], "play", 60, 60):
        return _json({"error": "slow", "msg": "Too many requests - wait a moment."}, 429)
    data = await _body(request)
    sid, seq = _int(data.get("sid"), -1, -1), _int(data.get("seq"), -1, -1)
    ep, t, near = await asyncio.to_thread(_play_info, sid, seq)
    if not ep:
        return _json({"error": "notfound"}, 404)
    if ep["key"].startswith("tg/"):
        return _json({"error": "notstream", "msg": "This episode can't be played here. Use the send button to get it in your chat."}, 409)
    uid = user["id"]
    exp = int(time.time()) + stream.SESSION_SECONDS
    tok = stream.make_token(uid, sid, seq, exp)
    asyncio.get_running_loop().run_in_executor(None, _log_play, uid, sid, seq)
    resp = _json({"url": f"/m/{tok}", "title": (t["title"] if t and t["title"] else f"Story {sid}"), "seq": seq,
                  "prev": (seq - 1) in near, "next": (seq + 1) in near})
    resp.set_cookie(COOKIE, _cookie_value(uid, exp), max_age=stream.SESSION_SECONDS, httponly=True,
                    secure=True, samesite="None", path="/")
    return resp


@api
async def send(request, user):
    if _limited(user["id"], "send", 20, 60):
        return _json({"error": "slow", "msg": "Too many requests - wait a moment."}, 429)
    data = await _body(request)
    sid, seq = _int(data.get("sid"), -1, -1), _int(data.get("seq"), -1, -1)
    cb = stream.CTX.get("send")
    if not cb:
        return _json({"error": "server"}, 500)
    ok, msg = await cb(user["id"], sid, seq)
    return _json({"ok": ok, "msg": msg})


@api
async def request_series(request, user):
    data = await _body(request)
    text = str(data.get("text", "")).strip()[:200]
    cb = stream.CTX.get("request")
    if not text or not cb:
        return _json({"ok": False, "msg": "Type the series name first."})
    ok, msg = await cb(user, text)
    return _json({"ok": ok, "msg": msg})


# ------------------------------------------------------------ audio
async def audio(request):
    tok = request.match_info["tok"]
    info = stream.parse_token(tok)
    if not info:
        return web.Response(status=403)
    uid, sid, seq, exp = info
    if exp < time.time():
        return web.Response(status=410)
    if REQUIRE_COOKIE and not _cookie_ok(request, uid):
        return web.Response(status=403)
    ok, _ = await stream.CTX["check_access"](uid)
    if not ok:
        return web.Response(status=403)
    ep = await asyncio.to_thread(stream._episode, sid, seq)
    if not ep or ep["key"].startswith("tg/"):
        return web.Response(status=404)
    return await stream.serve_episode(request, ep)


def add_routes(app):
    app.router.add_get("/app", page)
    app.router.add_get("/adframe", adframe)
    app.router.add_get("/api/config", config)
    app.router.add_post("/api/ad/start", ad_start)
    app.router.add_post("/api/ad/claim", ad_claim)
    app.router.add_get("/api/list", list_series)
    app.router.add_get("/api/story/{sid}", story)
    app.router.add_post("/api/play", play)
    app.router.add_post("/api/send", send)
    app.router.add_post("/api/request", request_series)
    app.router.add_get("/m/{tok}", audio)
