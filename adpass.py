"""Watch an ad -> get time-limited access ("ad pass").

Turned on by setting ADSGRAM_BLOCK_ID (your Adsgram *Reward* block id, digits only) in .env. Then:
  * everyone who is not an admin and has no paid subscription (/grant) must hold an active ad pass to listen
  * the Mini App shows a "Watch ad & unlock" screen; when the ad finishes the server saves a pass that ends in
    AD_PASS_MINUTES minutes
  * paid users and admins never see ads gates

The reward is decided by the server, never by the page alone:
  1. the app asks /api/ad/start and gets a signed, single-use token
  2. the app shows the ad, then sends the token to /api/ad/claim
  3. the server rejects tokens that are forged, old, reused, or claimed faster than AD_MIN_WATCH_SECONDS,
     and limits how many passes one user can collect per day
Only the standard library is used here, so it is easy to test on its own.
"""
import base64
import hashlib
import hmac
import os
import time
from contextlib import closing

ZONE = (os.environ.get("ADSGRAM_BLOCK_ID") or os.environ.get("AD_REWARD_ZONE") or "").strip()  # Adsgram block id
ENABLED = ZONE.isdigit()                                      # no block id = feature off
SDK_URL = os.environ.get("AD_SDK_URL", "https://sad.adsgram.ai/js/sad.min.js").strip()
if SDK_URL.startswith("//"):
    SDK_URL = "https:" + SDK_URL


def _int_env(name, default, lo=0, hi=10**6):
    try:
        return max(lo, min(int(os.environ.get(name, str(default)).strip()), hi))
    except ValueError:
        return default


MINUTES = _int_env("AD_PASS_MINUTES", 60, 1, 7 * 24 * 60)     # access given per ad
MAX_PER_DAY = _int_env("AD_MAX_PER_DAY", 12, 1, 200)           # ads that count per user per 24 h
MIN_WATCH = _int_env("AD_MIN_WATCH_SECONDS", 5, 0, 120)        # shortest believable time between start and claim
GRACE_MINUTES = _int_env("AD_GRACE_MINUTES", 5, 0, 120)        # free minutes if the ad network has no ad / cannot load (0 = off)
GRACE_PER_DAY = _int_env("AD_GRACE_PER_DAY", 2, 0, 20)
TOKEN_MAX_AGE = 15 * 60
SECRET = os.environ.get("STREAM_SECRET", "").strip().encode()


INAPP = None   # Adsgram has no automatic in-app ads; kept so older code that reads adpass.INAPP still works

SCHEMA = """
CREATE TABLE IF NOT EXISTS adpass(
    user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL, last_claim INTEGER NOT NULL DEFAULT 0,
    claims INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS adlog(user_id INTEGER NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS adlog_user ON adlog(user_id, ts);
CREATE INDEX IF NOT EXISTS adlog_ts ON adlog(ts);
"""


# ------------------------------------------------------------ state
def expiry(db, uid):
    with closing(db()) as con:
        r = con.execute("SELECT expires_at FROM adpass WHERE user_id=?", (uid,)).fetchone()
    return r["expires_at"] if r else 0


def remaining(db, uid):
    """Seconds of ad-pass time left (0 if none)."""
    return max(0, expiry(db, uid) - int(time.time()))


# ------------------------------------------------------------ tokens
def _sig(payload):
    mac = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(uid, now=None):
    ts = int(now if now is not None else time.time())
    return f"{ts}.{_sig(f'ad{uid}.{ts}')}"


def _verify(uid, token, now):
    """Returns the time the token was issued, or None if forged / too old / from the future."""
    try:
        ts_s, sig = str(token).split(".", 1)
        ts = int(ts_s)
    except (ValueError, AttributeError):
        return None
    if not SECRET or not hmac.compare_digest(_sig(f"ad{uid}.{ts}"), sig):
        return None
    if ts > now + 5 or now - ts > TOKEN_MAX_AGE:
        return None
    return ts


# ------------------------------------------------------------ claim
def claim(db, uid, token, failed=False, now=None):
    """Called when the app says the ad finished (failed=False) or could not be shown (failed=True).
    Returns {"ok": True, "rem": seconds_left, "kind": "ad"|"grace", "mins": n} or {"ok": False, "msg": "..."}."""
    now = int(now if now is not None else time.time())
    ts = _verify(uid, token, now)
    if ts is None:
        return {"ok": False, "msg": "That took too long. Tap the button again."}
    if not failed and now - ts < MIN_WATCH:
        return {"ok": False, "msg": "Please watch the whole ad to unlock."}
    kind = "grace" if failed else "ad"
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")            # one claim at a time, so a token cannot be used twice in parallel
        row = con.execute("SELECT expires_at, last_claim FROM adpass WHERE user_id=?", (uid,)).fetchone()
        exp, last = (row["expires_at"], row["last_claim"]) if row else (0, 0)
        left = max(0, exp - now)
        if ts <= last:
            return {"ok": False, "msg": "That ad was already counted. Tap the button again."}
        used = con.execute("SELECT COUNT(*) FROM adlog WHERE user_id=? AND kind=? AND ts>?",
                           (uid, kind, now - 86400)).fetchone()[0]
        if failed:
            if GRACE_MINUTES <= 0 or GRACE_PER_DAY <= 0:
                return {"ok": False, "msg": "The ad could not load. Turn off any ad blocker or VPN and try again."}
            if left > 0:
                return {"ok": False, "msg": "You still have time left."}
            if used >= GRACE_PER_DAY:
                return {"ok": False, "msg": "No ad is available right now. Please try again in a little while."}
            new_exp = now + GRACE_MINUTES * 60
        else:
            if used >= MAX_PER_DAY:
                return {"ok": False, "msg": f"Daily limit of {MAX_PER_DAY} ads reached. Come back tomorrow."}
            if left >= MINUTES * 60:
                return {"ok": False, "msg": "You already have plenty of time left."}
            new_exp = max(exp, now) + MINUTES * 60
        con.execute("INSERT INTO adpass(user_id, expires_at, last_claim, claims) VALUES(?,?,?,1) "
                    "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, "
                    "last_claim=excluded.last_claim, claims=adpass.claims+1", (uid, new_exp, now))
        con.execute("INSERT INTO adlog(user_id, ts, kind) VALUES(?,?,?)", (uid, now, kind))
        con.commit()
    return {"ok": True, "rem": new_exp - now, "kind": kind, "mins": (new_exp - now) // 60}


# ------------------------------------------------------------ admin stats
def stats(db):
    now = int(time.time())
    with closing(db()) as con:
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
        return {
            "ads_1d": q("SELECT COUNT(*) FROM adlog WHERE kind='ad' AND ts>?", now - 86400),
            "ads_7d": q("SELECT COUNT(*) FROM adlog WHERE kind='ad' AND ts>?", now - 7 * 86400),
            "ads_all": q("SELECT COUNT(*) FROM adlog WHERE kind='ad'"),
            "grace_1d": q("SELECT COUNT(*) FROM adlog WHERE kind='grace' AND ts>?", now - 86400),
            "users_1d": q("SELECT COUNT(DISTINCT user_id) FROM adlog WHERE kind='ad' AND ts>?", now - 86400),
            "users_7d": q("SELECT COUNT(DISTINCT user_id) FROM adlog WHERE kind='ad' AND ts>?", now - 7 * 86400),
            "active": q("SELECT COUNT(*) FROM adpass WHERE expires_at>?", now),
        }
