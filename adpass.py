"""Free daily listening + watch an ad for bonus time ("ad pass").

Model (freemium): everyone can browse the catalog without any ad. Playing needs "time":
  * every user gets AD_FREE_MINUTES of free listening once per AD_FREE_EVERY_HOURS, started by their first play - no ad
  * watching a rewarded ad adds AD_PASS_MINUTES of bonus time on top
  * paid users (/grant) and admins never need either
Set AD_FREE_MINUTES=0 for the strict version instead: no free time, an ad is required for any playback and browsing.

Turned on by choosing an ad provider in .env (AD_PROVIDER=adsgram with AD_ADSGRAM_BLOCK, or AD_PROVIDER=monetag with
AD_REWARD_ZONE; `bash setup-rewarded.sh` does it for you). Then:
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

ZONE = os.environ.get("AD_REWARD_ZONE", "").strip()                 # Monetag Rewarded Interstitial zone id
SDK_URL = os.environ.get("AD_SDK_URL", "https://libtl.com/sdk.js").strip()   # Monetag SDK
if SDK_URL.startswith("//"):
    SDK_URL = "https:" + SDK_URL
BLOCK = os.environ.get("AD_ADSGRAM_BLOCK", "").strip()              # Adsgram Reward block id (digits)
ADSGRAM_SDK = "https://sad.adsgram.ai/js/sad.min.js"
ADSGRAM_DEBUG = os.environ.get("AD_ADSGRAM_DEBUG", "off").strip().lower() in ("1", "on", "true", "yes")  # test ads: never in production

# Which network shows the unlock ad: adsgram | monetag | off. Not set = Adsgram if a block id exists, else Monetag.
PROVIDER = os.environ.get("AD_PROVIDER", "").strip().lower()
if PROVIDER not in ("adsgram", "monetag", "off"):
    PROVIDER = "adsgram" if BLOCK.isdigit() else "monetag"
ENABLED = (PROVIDER == "adsgram" and BLOCK.isdigit()) or (PROVIDER == "monetag" and ZONE.isdigit())  # no id = feature off


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
FREE_MINUTES = _int_env("AD_FREE_MINUTES", 30, 0, 24 * 60)     # free listening per window, no ad (0 = strict ad gate)
FREE_EVERY = _int_env("AD_FREE_EVERY_HOURS", 24, 1, 24 * 14) * 3600
TOKEN_MAX_AGE = 15 * 60
SECRET = os.environ.get("STREAM_SECRET", "").strip().encode()


def _inapp():
    """Optional automatic In-App Interstitial for free users. AD_INAPP=off (default) | on | frequency,capping,interval,timeout"""
    raw = os.environ.get("AD_INAPP", "off").strip().lower()
    if raw in ("", "0", "off", "false", "no"):
        return None
    freq, cap, gap, wait = 2, 0.1, 30, 5                        # Monetag's sample: 2 ads per 6 min, 30 s apart, first after 5 s
    if raw not in ("1", "on", "true", "yes"):
        try:
            a = [float(x) for x in raw.split(",")]
            if len(a) == 4:
                freq, cap, gap, wait = int(a[0]), a[1], int(a[2]), int(a[3])
        except ValueError:
            pass
    return {"frequency": freq, "capping": cap, "interval": gap, "timeout": wait, "everyPage": False}


INAPP = _inapp() if ZONE.isdigit() else None   # In-App Interstitial is a Monetag format: needs a Monetag zone (works with either provider)

SCHEMA = """
CREATE TABLE IF NOT EXISTS adpass(
    user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL, last_claim INTEGER NOT NULL DEFAULT 0,
    claims INTEGER NOT NULL DEFAULT 0, free_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS adlog(user_id INTEGER NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS adlog_user ON adlog(user_id, ts);
CREATE INDEX IF NOT EXISTS adlog_ts ON adlog(ts);
"""


def migrate(con):
    """Adds columns that older installs of the adpass table do not have yet."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(adpass)")]
    if "free_at" not in cols:
        con.execute("ALTER TABLE adpass ADD COLUMN free_at INTEGER NOT NULL DEFAULT 0")


# ------------------------------------------------------------ state
def expiry(db, uid):
    with closing(db()) as con:
        r = con.execute("SELECT expires_at FROM adpass WHERE user_id=?", (uid,)).fetchone()
    return r["expires_at"] if r else 0


def remaining(db, uid):
    """Seconds of ad-pass time left (0 if none)."""
    return max(0, expiry(db, uid) - int(time.time()))


def info(db, uid, now=None):
    """What the app needs to show: seconds left, and whether / when the next free window is available."""
    now = int(now if now is not None else time.time())
    with closing(db()) as con:
        r = con.execute("SELECT expires_at, free_at FROM adpass WHERE user_id=?", (uid,)).fetchone()
    exp, free_at = (r["expires_at"], r["free_at"]) if r else (0, 0)
    wait = max(0, free_at + FREE_EVERY - now) if FREE_MINUTES > 0 else 0
    return {"rem": max(0, exp - now), "free_ready": FREE_MINUTES > 0 and wait == 0, "next_free": wait}


def start_free(db, uid, now=None):
    """Called when the user actually tries to listen. If they have no time left and their free window is available, starts it
    (AD_FREE_MINUTES, no ad). Returns the seconds of time the user has now (0 = none, they need an ad or to wait)."""
    now = int(now if now is not None else time.time())
    if FREE_MINUTES <= 0:
        return 0
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT expires_at, free_at FROM adpass WHERE user_id=?", (uid,)).fetchone()
        exp, free_at = (row["expires_at"], row["free_at"]) if row else (0, 0)
        if exp > now:                                  # already has time (free or bonus): keep the free window for later
            return exp - now
        if now - free_at < FREE_EVERY:
            return 0
        new_exp = now + FREE_MINUTES * 60
        con.execute("INSERT INTO adpass(user_id, expires_at, last_claim, claims, free_at) VALUES(?,?,0,0,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, free_at=excluded.free_at",
                    (uid, new_exp, now))
        con.execute("INSERT INTO adlog(user_id, ts, kind) VALUES(?,?,'free')", (uid, now))
        con.commit()
    return new_exp - now


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
            "free_1d": q("SELECT COUNT(*) FROM adlog WHERE kind='free' AND ts>?", now - 86400),
            "users_1d": q("SELECT COUNT(DISTINCT user_id) FROM adlog WHERE kind='ad' AND ts>?", now - 86400),
            "users_7d": q("SELECT COUNT(DISTINCT user_id) FROM adlog WHERE kind='ad' AND ts>?", now - 7 * 86400),
            "active": q("SELECT COUNT(*) FROM adpass WHERE expires_at>?", now),
        }
