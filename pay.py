"""Razorpay passes: ad-free listening. Two plans by default: Rs 15 for 7 days (weekly) and Rs 49 for 30 days (SUB_PLANS).

How it works
  * The user taps /subscribe (or the button in the app). The bot creates a one-time Razorpay Payment Link
    (UPI / cards / netbanking, amount in paise) and stores it in the `payments` table with the user's Telegram id.
  * When it is paid, the days are added to the same `subs` table that /grant uses, so a paid user already skips ads
    and time limits everywhere. Paying again before the pass ends adds the days on top of what is left.
  * Payment is confirmed in two independent ways, and each one is safe to run twice:
      1. Razorpay webhook (event payment_link.paid) -> POST <STREAM_BASE_URL>/razorpay/webhook   (needs RAZORPAY_WEBHOOK_SECRET)
      2. "I've paid" button / the app checks the link's status with the Razorpay API.
  * Each payment is one-time (not auto-renewing): the user picks a plan, pays, and pays again when it ends.

Turned on by RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET in .env. Without them the feature is off and nothing changes.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import closing

import aiohttp

log = logging.getLogger("pay")


def _int_env(name, default, lo, hi):
    try:
        return max(lo, min(int(os.environ.get(name, default)), hi))
    except (TypeError, ValueError):
        return default


KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()


def _parse_plans(raw):
    """SUB_PLANS="15:7,49:30" -> [{'price':15,'days':7,...}, {'price':49,'days':30,...}]  (rupees:days, comma separated)."""
    out = []
    for part in (raw or "").split(","):
        try:
            price, days = (int(x) for x in part.strip().split(":"))
        except ValueError:
            continue
        if 1 <= price <= 100000 and 1 <= days <= 366 and all(pl["days"] != days for pl in out):
            out.append({"price": price, "days": days, "id": str(days)})
    return out or [{"price": 15, "days": 7, "id": "7"}, {"price": 49, "days": 30, "id": "30"}]


PLANS = _parse_plans(os.environ.get("SUB_PLANS", "15:7,49:30"))
PRICE = PLANS[0]["price"]      # first plan, kept for old code
DAYS = PLANS[0]["days"]


def plan_label(pl):
    return f"{'Weekly' if pl['days'] == 7 else 'Monthly' if pl['days'] == 30 else str(pl['days']) + '-day'} - Rs {pl['price']} / {pl['days']} days"


def get_plan(plan_id):
    """Finds a plan by its id (the number of days). Returns None for an unknown id."""
    for pl in PLANS:
        if pl["id"] == str(plan_id):
            return pl
    return None


def plans_public():
    return [{"id": pl["id"], "price": pl["price"], "days": pl["days"]} for pl in PLANS]

ENABLED = bool(KEY_ID and KEY_SECRET)
API = "https://api.razorpay.com/v1"
LINK_TTL = 3600           # an unpaid payment link expires after 1 hour (Razorpay needs at least 15 minutes)
REUSE_WITHIN = 1200       # tapping Subscribe again within 20 minutes shows the same link instead of making a new one

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments(
    link_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL, url TEXT,
    status TEXT NOT NULL DEFAULT 'created', created INTEGER NOT NULL, paid_at INTEGER, days INTEGER);
CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id, created);
"""


def migrate(con):
    """Older databases have no `days` column on payments: add it (old rows then use the first plan's days)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(payments)")}
    if "days" not in cols:
        con.execute("ALTER TABLE payments ADD COLUMN days INTEGER")
        con.commit()


async def _call(method, path, payload=None):
    auth = aiohttp.BasicAuth(KEY_ID, KEY_SECRET)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.request(method, API + path, json=payload, auth=auth) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                err = (data.get("error") or {}) if isinstance(data, dict) else {}
                log.error("razorpay %s %s -> %s %s", method, path, r.status, err)
                raise RuntimeError(err.get("description") or f"Razorpay error {r.status}")
            return data


async def create_link(db, uid, plan_id=None):
    """Returns (link_id, pay_url) for the chosen plan (default: the first one). Reuses a recent unpaid link of the same user and plan."""
    pl = get_plan(plan_id) if plan_id is not None else PLANS[0]
    if not pl:
        raise RuntimeError("Unknown plan")
    now = int(time.time())
    with closing(db()) as con:
        r = con.execute("SELECT link_id, url FROM payments WHERE user_id=? AND status='created' AND created>? AND url IS NOT NULL "
                        "AND amount=? AND days=? ORDER BY created DESC LIMIT 1",
                        (uid, now - REUSE_WITHIN, pl["price"] * 100, pl["days"])).fetchone()
    if r:
        return r["link_id"], r["url"]
    data = await _call("POST", "/payment_links", {
        "amount": pl["price"] * 100, "currency": "INR", "accept_partial": False,
        "description": f"AUDIOS FM - {pl['days']} days ad-free listening",
        "reference_id": f"tg{uid}-{now}"[:40],
        "expire_by": now + LINK_TTL,
        "notify": {"sms": False, "email": False}, "reminder_enable": False,
        "notes": {"telegram_id": str(uid), "days": str(pl["days"])},
    })
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO payments(link_id, user_id, amount, url, created, days) VALUES(?,?,?,?,?,?)",
                    (data["id"], uid, pl["price"] * 100, data.get("short_url"), now, pl["days"]))
        con.commit()
    return data["id"], data.get("short_url")


def settle(db, link_id, amount_paid):
    """Marks the link paid and adds the days. Safe to call many times: only the first call returns (user_id, new_expiry, rupees),
    every later call returns None."""
    now = int(time.time())
    with closing(db()) as con:
        row = con.execute("SELECT user_id, amount, days FROM payments WHERE link_id=?", (link_id,)).fetchone()
        if not row or int(amount_paid or 0) < row["amount"]:
            return None
        cur = con.execute("UPDATE payments SET status='paid', paid_at=? WHERE link_id=? AND status<>'paid'", (now, link_id))
        if cur.rowcount != 1:
            con.commit()
            return None
        uid = row["user_id"]
        r = con.execute("SELECT expires_at FROM subs WHERE user_id=?", (uid,)).fetchone()
        days = row["days"] or DAYS   # days of the plan that was bought
        new_exp = max(now, r["expires_at"] if r else 0) + days * 86400
        con.execute("INSERT OR REPLACE INTO subs(user_id, expires_at) VALUES(?,?)", (uid, new_exp))
        con.commit()
    return uid, new_exp, row["amount"] // 100


async def check(db, uid, link_id):
    """Asks Razorpay about a link that belongs to this user. Returns ('paid', (uid, new_exp) or None) | ('open', None) | ('closed', None)."""
    with closing(db()) as con:
        row = con.execute("SELECT user_id, status FROM payments WHERE link_id=?", (link_id,)).fetchone()
    if not row or row["user_id"] != uid:
        return "closed", None
    if row["status"] == "paid":
        return "paid", None
    data = await _call("GET", f"/payment_links/{link_id}")
    st = data.get("status")
    if st == "paid":
        return "paid", settle(db, link_id, data.get("amount_paid"))
    if st in ("expired", "cancelled"):
        return "closed", None
    return "open", None


def verify(body, signature):
    if not WEBHOOK_SECRET or not signature:
        return False
    want = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature)


def handle_event(db, body):
    """Razorpay webhook body (already signature-checked). Returns (user_id, new_expiry, rupees) for a newly confirmed payment, else None."""
    ev = json.loads(body)
    if ev.get("event") != "payment_link.paid":
        return None
    ent = ((ev.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
    if ent.get("status") != "paid" or not ent.get("id"):
        return None
    return settle(db, ent["id"], ent.get("amount_paid"))
