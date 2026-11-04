"""AUDIOS FM episodes bot - standalone (no website needed).

- Audio is read straight from your Cloudflare R2 bucket (keys look like audio/<story id>/<file>.mp3).
- Paid users are managed inside the bot by the admin: /grant <telegram id> <days>.
- Sent audio is protected (no forward/save) and auto-deleted.
"""
import asyncio
import io
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime

import boto3
from botocore.config import Config

import stream
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET = os.environ["R2_SECRET_ACCESS_KEY"]
DELETE_AFTER = int(os.environ.get("DELETE_AFTER_SECONDS", "5400"))
MAX_RANGE = int(os.environ.get("MAX_RANGE", "30"))
TG_MAX_BYTES = int(os.environ.get("TG_MAX_BYTES", str(49 * 1024 * 1024)))  # Bot API upload limit ~50 MB
CONTACT_TEXT = os.environ.get("CONTACT_TEXT", "Contact the admin to subscribe.")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
# If set (e.g. -1001234567890), new episodes added with /addto are stored in this private Telegram channel instead of R2.
STORAGE_CHANNEL = int(os.environ["STORAGE_CHANNEL_ID"]) if os.environ.get("STORAGE_CHANNEL_ID", "").strip() else None
# Paid-only mode is OFF by default. Set REQUIRE_SUBSCRIPTION=true in .env to turn it back on.
REQUIRE_SUB = os.environ.get("REQUIRE_SUBSCRIPTION", "false").strip().lower() in ("1", "true", "yes", "on")

# Force-join: users must be members of these channels before they can use the bot. Comma separated, e.g.
# FORCE_SUB_CHANNELS=@mychannel,-1001234567890|https://t.me/+InviteLink   (private channels need |invite link)
FORCE_SUB = []
for _raw in os.environ.get("FORCE_SUB_CHANNELS", "").split(","):
    _ref, _, _link = _raw.strip().partition("|")
    _ref, _link = _ref.strip(), _link.strip()
    if not _ref:
        continue
    _chat = int(_ref) if _ref.lstrip("-").isdigit() else (_ref if _ref.startswith("@") else "@" + _ref)
    if not _link and isinstance(_chat, str):
        _link = f"https://t.me/{_chat[1:]}"
    FORCE_SUB.append((_chat, _link))
FSUB_CACHE, FSUB_TTL = {}, 600  # a user who passed the check is trusted for 10 minutes

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("episodes-bot")

s3 = boto3.client(
    "s3", endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_KEY_ID, aws_secret_access_key=R2_SECRET,
    region_name="auto", config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                      retries={"max_attempts": 3}, max_pool_connections=200),
)
KEY_RE = re.compile(r"^audio/(\d+)/[^/]+\.(mp3|m4a|aac|wav|ogg|opus|webm|flac)$", re.I)


# ------------------------------------------------------------ database
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL, note TEXT);
        CREATE TABLE IF NOT EXISTS stories(story_id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE IF NOT EXISTS episodes(
            key TEXT PRIMARY KEY, story_id INTEGER NOT NULL, seq INTEGER NOT NULL,
            size INTEGER NOT NULL, modified INTEGER NOT NULL, file_id TEXT, kind TEXT,
            UNIQUE(story_id, seq));
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT,
            text TEXT NOT NULL, created INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS plays(user_id INTEGER NOT NULL, story_id INTEGER NOT NULL, seq INTEGER NOT NULL, ts INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts);
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
        for col in ("tg_chat", "tg_msg"):  # Telegram-stored episodes: channel id + message id
            if col not in cols:
                con.execute(f"ALTER TABLE episodes ADD COLUMN {col} INTEGER")
        con.commit()


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%d %b %Y")


def sub_expiry(uid):
    with closing(db()) as con:
        r = con.execute("SELECT expires_at FROM subs WHERE user_id=?", (uid,)).fetchone()
    return r["expires_at"] if r else 0


async def check_access(uid):
    if not REQUIRE_SUB or uid in ADMIN_IDS or sub_expiry(uid) > time.time():
        return True, ""
    exp = sub_expiry(uid)
    head = "⛔ Your subscription has expired." if exp else "🔒 This bot is only for paid members."
    return False, f"{head}\n{CONTACT_TEXT}\n\nYour Telegram ID: {uid}"


async def missing_channels(bot, uid):
    """Force-join channels this user has not joined yet (empty list = allowed)."""
    if not FORCE_SUB or uid in ADMIN_IDS or time.time() - FSUB_CACHE.get(uid, 0) < FSUB_TTL:
        return []
    missing = []
    for chat, link in FORCE_SUB:
        try:
            m = await bot.get_chat_member(chat, uid)
            joined = m.status in ("member", "administrator", "creator") or (m.status == "restricted" and getattr(m, "is_member", False))
        except Exception as e:  # bot not admin / wrong id: don't lock everyone out
            log.warning("force-join check failed for %s (is the bot an admin there?): %s", chat, e)
            continue
        if not joined:
            missing.append((chat, link))
    if not missing:
        FSUB_CACHE[uid] = time.time()
    return missing


async def force_prompt(message, missing):
    many = len(missing) > 1
    kb = [[InlineKeyboardButton(f"📢 Join channel {i}" if many else "📢 Join channel", url=link)]
          for i, (_, link) in enumerate(missing, 1) if link]
    kb.append([InlineKeyboardButton("✅ I've joined", callback_data="fsub:ok")])
    await message.reply_text("🔒 To use this bot, please join our channel" + ("s" if many else "") +
                             " first, then tap ✅ I've joined.", reply_markup=InlineKeyboardMarkup(kb))


async def guard(update):
    uid = update.effective_user.id
    ok, msg = await check_access(uid)
    if not ok:
        await update.effective_message.reply_text(msg)
        return False
    miss = await missing_channels(update.get_bot(), uid)
    if miss:
        await force_prompt(update.effective_message, miss)
        return False
    return True


# ------------------------------------------------------------ R2
def r2_scan():
    found = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix="audio/"):
        for o in page.get("Contents", []):
            m = KEY_RE.match(o["Key"])
            if m:
                found.append((o["Key"], int(m.group(1)), o["Size"], int(o["LastModified"].timestamp())))
    return found


def r2_get(key):
    return s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()


async def do_sync():
    found = await asyncio.to_thread(r2_scan)
    live = {k for k, *_ in found}
    added = removed = 0
    with closing(db()) as con:
        for (key,) in con.execute("SELECT key FROM episodes").fetchall():
            if key not in live and not key.startswith("tg/"):  # tg/ = stored in Telegram, not R2
                con.execute("DELETE FROM episodes WHERE key=?", (key,))
                removed += 1
        have = {r["key"] for r in con.execute("SELECT key FROM episodes")}
        by_story = {}
        for key, sid, size, mod in found:
            if key not in have:
                by_story.setdefault(sid, []).append((mod, key, size))
        for sid, items in by_story.items():
            con.execute("INSERT OR IGNORE INTO stories(story_id, title) VALUES(?, NULL)", (sid,))
            nxt = con.execute("SELECT COALESCE(MAX(seq),0) FROM episodes WHERE story_id=?", (sid,)).fetchone()[0]
            for mod, key, size in sorted(items):
                nxt += 1
                con.execute("INSERT INTO episodes(key, story_id, seq, size, modified) VALUES(?,?,?,?,?)",
                            (key, sid, nxt, size, mod))
                added += 1
        con.commit()
        stories = con.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    return added, removed, stories


# ------------------------------------------------------------ texts
def help_text(is_admin):
    t = (
        "🎧 How to use\n"
        "1. Type a series name to search.\n"
        "2. Tap the series - you'll see how many episodes it has.\n"
        "3. Send an episode number (350) or a range (350-370).\n\n"
        "/list - browse all available series\n"
        "/request <name> - ask for a series that isn't here\n\n"
        f"Episodes are protected and auto-delete after {max(DELETE_AFTER // 60, 1)} min - listen or save quickly.\n\n"
        "/start - welcome\n/help - this message\n/status - your subscription"
    )
    if is_admin:
        t += (
            "\n\n🛠 Admin\n"
            "/addto <story id> [title] - then just send audio files here; each becomes the next episode\n"
            "/done - stop adding\n"
            "/sync - scan Cloudflare R2 for new episodes\n"
            "/stories - list story ids (named / unnamed)\n"
            "/name <story id> <title> - give a story its name (needed for search)\n"
            "/swap <story id> <a> <b> - swap two episode numbers\n"
            "/delete <story id> <eps> - delete one, a range (10-25), several (3 7 10-15) or all\n"
            "/renumber <story id> - close gaps in episode numbers after deleting\n"
            "/grant <telegram id> <days> - give/extend access\n"
            "/revoke <telegram id> - remove access\n"
            "/subs - list paid users\n"
            "/clearcache - forget cached Telegram files\n"
            "/forcesub - check the force-join channel setup\n"
            "/setad <ad code> - show an ad on the player page (/setad off to remove)\n"
            "/adtest - check why the ad is not showing (gives a test link)\n"
            "/sethead <tag> - set the ad network's site-verification tag (/sethead off to remove)\n"
            "/requests - pending user requests\n"
            "/reqdone <request id> - mark a request as added (notifies the user)\n"
            "/stats\n\nAdmins don't need a subscription."
        )
    return t


async def delete_job(context):
    d = context.job.data
    try:
        await context.bot.delete_message(d["chat_id"], d["message_id"])
    except Exception as e:
        log.warning("auto-delete failed: %s", e)


async def reply_long(update, text):
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800])


# ------------------------------------------------------------ user commands
async def cmd_start(update, context):
    if not await guard(update):
        return
    await update.message.reply_text("Welcome to AUDIOS FM! Send a series name to search. /help for instructions.")


async def cmd_help(update, context):
    await update.message.reply_text(help_text(update.effective_user.id in ADMIN_IDS))


async def cmd_status(update, context):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await update.message.reply_text("👑 Admin - full access.")
        return
    if not REQUIRE_SUB:
        await update.message.reply_text("✅ Open access - no subscription needed right now.")
        return
    exp = sub_expiry(uid)
    if exp > time.time():
        await update.message.reply_text(f"✅ Active until {fmt(exp)}.")
    else:
        ok, msg = await check_access(uid)
        await update.message.reply_text(msg)


# ------------------------------------------------------------ admin commands
def admin_cmd(fn):
    async def wrapper(update, context):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("Admin only.")
            return
        await fn(update, context)
    return wrapper


@admin_cmd
async def cmd_sync(update, context):
    msg = await update.message.reply_text("⏳ Scanning R2...")
    try:
        added, removed, stories = await do_sync()
    except Exception as e:
        log.exception("sync failed")
        await msg.edit_text(f"❌ Sync failed: {e}")
        return
    await msg.edit_text(f"✅ Done. New episodes: {added}, removed: {removed}, stories: {stories}.\nUse /stories to name new ones.")


@admin_cmd
async def cmd_stories(update, context):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT s.story_id, s.title, COUNT(e.key) c, MIN(e.modified) first, MAX(e.modified) last "
            "FROM stories s LEFT JOIN episodes e ON e.story_id=s.story_id GROUP BY s.story_id ORDER BY s.story_id").fetchall()
    if not rows:
        await update.message.reply_text("No stories yet. Run /sync first.")
        return
    lines = [f"{r['story_id']}: {r['title'] or '(unnamed)'} - {r['c']} eps"
             + (f" ({fmt(r['first'])} to {fmt(r['last'])})" if r["c"] else "") for r in rows]
    await reply_long(update, "\n".join(lines))


@admin_cmd
async def cmd_name(update, context):
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /name <story id> <title>")
        return
    sid, title = int(context.args[0]), " ".join(context.args[1:]).strip()
    with closing(db()) as con:
        cur = con.execute("UPDATE stories SET title=? WHERE story_id=?", (title, sid))
        con.commit()
    await update.message.reply_text(f"✅ Story {sid} is now: {title}" if cur.rowcount else "Story id not found (run /sync).")


@admin_cmd
async def cmd_swap(update, context):
    a = context.args
    if len(a) != 3 or not all(x.isdigit() for x in a):
        await update.message.reply_text("Usage: /swap <story id> <episode a> <episode b>")
        return
    sid, x, y = map(int, a)
    with closing(db()) as con:
        rx = con.execute("SELECT key FROM episodes WHERE story_id=? AND seq=?", (sid, x)).fetchone()
        ry = con.execute("SELECT key FROM episodes WHERE story_id=? AND seq=?", (sid, y)).fetchone()
        if not rx or not ry:
            await update.message.reply_text("One of those episode numbers doesn't exist.")
            return
        con.execute("UPDATE episodes SET seq=-1 WHERE key=?", (rx["key"],))
        con.execute("UPDATE episodes SET seq=? WHERE key=?", (x, ry["key"]))
        con.execute("UPDATE episodes SET seq=? WHERE key=?", (y, rx["key"]))
        con.commit()
    await update.message.reply_text(f"✅ Swapped Ep {x} and Ep {y}.")


# ------------------------------------------------------------ mass delete / renumber
PENDING_TTL = 300  # a delete confirmation stays valid for 5 minutes


def parse_eps(tokens, existing):
    """'3 5-9, 12' -> sorted list of episode numbers that exist. 'all' = every episode. None if unreadable."""
    text = " ".join(tokens).lower().replace(",", " ")
    if text.strip() == "all":
        return sorted(existing)
    wanted = set()
    for part in text.split():
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if hi < lo:
            lo, hi = hi, lo
        wanted.update(range(lo, hi + 1))
    return sorted(wanted & set(existing))


def compact(nums):
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'"""
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


@admin_cmd
async def cmd_delete(update, context):
    a = context.args
    if len(a) < 2 or not a[0].isdigit():
        await update.message.reply_text(
            "Usage: /delete <story id> <episodes>\n\n"
            "Examples:\n/delete 12 45 - one episode\n/delete 12 10-25 - a range\n"
            "/delete 12 3 7 10-15 - several\n/delete 12 all - every episode of the story (and the story itself)\n\n"
            "You will be asked to confirm before anything is removed.")
        return
    sid = int(a[0])
    with closing(db()) as con:
        s = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()
        rows = con.execute("SELECT seq, key FROM episodes WHERE story_id=? ORDER BY seq", (sid,)).fetchall()
    if not s and not rows:
        await update.message.reply_text("Story id not found (see /stories).")
        return
    whole = " ".join(a[1:]).strip().lower() == "all"
    seqs = parse_eps(a[1:], [r["seq"] for r in rows])
    if seqs is None:
        await update.message.reply_text("Couldn't read the episode list. Use numbers and ranges like: 5 8-12 20")
        return
    if not seqs and not whole:
        await update.message.reply_text("None of those episode numbers exist in that story.")
        return
    chosen = [r for r in rows if r["seq"] in set(seqs)]
    n_tg = sum(1 for r in chosen if r["key"].startswith("tg/"))
    context.user_data["pending_delete"] = {"sid": sid, "seqs": seqs, "whole": whole, "ts": time.time()}
    title = (s["title"] if s and s["title"] else f"Story {sid}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Yes, delete", callback_data="del:ok"),
                                InlineKeyboardButton("Cancel", callback_data="del:no")]])
    await update.message.reply_text(
        f"⚠️ Delete from {title} (story {sid})?\n\n"
        f"Episodes: {compact(seqs) if seqs else 'none'} ({len(seqs)} total)\n"
        f"Stored in R2: {len(chosen) - n_tg}   Stored in Telegram channel: {n_tg}\n"
        + ("The story name will be removed too.\n" if whole else "")
        + "\nThis permanently removes the audio files and cannot be undone.", reply_markup=kb)


def _r2_delete(keys):
    """Delete R2 objects. Returns the set of keys that failed."""
    failed = set()
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try:
            resp = s3.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            failed.update(e["Key"] for e in resp.get("Errors", []))
        except Exception:  # some boto3/R2 combinations reject bulk delete - fall back to one by one
            for k in batch:
                try:
                    s3.delete_object(Bucket=R2_BUCKET, Key=k)
                except Exception:
                    log.exception("R2 delete failed for %s", k)
                    failed.add(k)
    return failed


async def delete_callback(update, context):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        return
    pend = context.user_data.pop("pending_delete", None)
    if q.data == "del:no":
        await q.edit_message_text("Cancelled. Nothing was deleted.")
        return
    if not pend or time.time() - pend["ts"] > PENDING_TTL:
        await q.edit_message_text("That confirmation expired. Run /delete again.")
        return
    sid, seqs, whole = pend["sid"], pend["seqs"], pend["whole"]
    await q.edit_message_text("⏳ Deleting...")
    async with ADD_LOCK:  # don't renumber/insert while an upload is being saved
        with closing(db()) as con:
            rows = [r for r in con.execute("SELECT key, seq, tg_chat, tg_msg FROM episodes WHERE story_id=?", (sid,)).fetchall()
                    if r["seq"] in set(seqs)]
        r2_keys = [r["key"] for r in rows if not r["key"].startswith("tg/")]
        r2_failed = await asyncio.to_thread(_r2_delete, r2_keys) if r2_keys else set()
        tg_failed = 0
        for n, r in enumerate([r for r in rows if r["key"].startswith("tg/")], 1):
            if r["tg_chat"] and r["tg_msg"]:
                try:
                    await context.bot.delete_message(r["tg_chat"], r["tg_msg"])
                except Exception as e:
                    tg_failed += 1
                    log.warning("could not delete channel message %s: %s", r["tg_msg"], e)
                await asyncio.sleep(0.06)  # stay under Telegram's rate limit
            if n % 50 == 0:
                try:
                    await q.edit_message_text(f"⏳ Deleting... {n} channel messages done")
                except Exception:
                    pass
        done = [r["key"] for r in rows if r["key"] not in r2_failed]
        with closing(db()) as con:
            con.executemany("DELETE FROM episodes WHERE key=?", [(k,) for k in done])
            left = con.execute("SELECT seq FROM episodes WHERE story_id=? ORDER BY seq", (sid,)).fetchall()
            if whole and not left:
                con.execute("DELETE FROM stories WHERE story_id=?", (sid,))
            con.commit()
    left = [r["seq"] for r in left]
    msg = f"✅ Deleted {len(done)} episode(s) from story {sid}."
    if r2_failed:
        msg += f"\n⚠️ {len(r2_failed)} R2 file(s) could not be deleted and were kept (try again)."
    if tg_failed:
        msg += f"\n⚠️ {tg_failed} message(s) couldn't be removed from the storage channel (already gone, or the bot lacks delete rights) - the episodes are removed from the bot anyway."
    if left and left != list(range(1, len(left) + 1)):
        msg += f"\n\nEpisode numbers now have gaps ({compact(left)}). Run /renumber {sid} to make them 1..{len(left)} again, or leave as is."
    await q.edit_message_text(msg)


@admin_cmd
async def cmd_renumber(update, context):
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /renumber <story id>\nCloses gaps so episodes are numbered 1, 2, 3... in their current order.")
        return
    sid = int(context.args[0])
    async with ADD_LOCK:
        with closing(db()) as con:
            seqs = [r["seq"] for r in con.execute("SELECT seq FROM episodes WHERE story_id=? ORDER BY seq", (sid,))]
            if not seqs:
                await update.message.reply_text("That story has no episodes.")
                return
            if seqs == list(range(1, len(seqs) + 1)):
                await update.message.reply_text("Already numbered 1..%d, nothing to do." % len(seqs))
                return
            con.execute("UPDATE episodes SET seq=-seq WHERE story_id=?", (sid,))  # park out of the way (UNIQUE story_id+seq)
            for new, old in enumerate(seqs, 1):
                con.execute("UPDATE episodes SET seq=? WHERE story_id=? AND seq=?", (new, sid, -old))
            con.commit()
    await update.message.reply_text(f"✅ Story {sid} renumbered: {len(seqs)} episode(s), now Ep 1-{len(seqs)}.")



@admin_cmd
async def cmd_grant(update, context):
    if len(context.args) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /grant <telegram id> <days>")
        return
    uid, days = int(context.args[0]), int(context.args[1])
    new_exp = max(int(time.time()), sub_expiry(uid)) + days * 86400
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO subs(user_id, expires_at) VALUES(?,?)", (uid, new_exp))
        con.commit()
    await update.message.reply_text(f"✅ {uid} has access until {fmt(new_exp)}.")
    try:
        await context.bot.send_message(uid, f"✅ Your subscription is active until {fmt(new_exp)}. Send a series name to start.")
    except Exception:
        pass


@admin_cmd
async def cmd_revoke(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke <telegram id>")
        return
    with closing(db()) as con:
        cur = con.execute("DELETE FROM subs WHERE user_id=?", (int(context.args[0]),))
        con.commit()
    await update.message.reply_text("✅ Removed." if cur.rowcount else "Not found.")


@admin_cmd
async def cmd_subs(update, context):
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM subs ORDER BY expires_at DESC").fetchall()
    now = time.time()
    await reply_long(update, "\n".join(f"{r['user_id']} - {'active' if r['expires_at'] > now else 'expired'} until {fmt(r['expires_at'])}"
                                       for r in rows) or "No paid users yet.")


@admin_cmd
async def cmd_clearcache(update, context):
    with closing(db()) as con:
        con.execute("UPDATE episodes SET file_id=NULL, kind=NULL")
        con.commit()
    await update.message.reply_text("🧹 Cache cleared.")


@admin_cmd
async def cmd_setad(update, context):
    parts = (update.message.text or "").split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not stream.ENABLED:
        await update.message.reply_text("The streaming player is off (STREAM_BASE_URL / STREAM_SECRET not set), so there is nowhere to show an ad.")
        return
    if not arg:
        cur = stream.ad_html()
        await update.message.reply_text(
            ("📢 Player ad: ON (" + str(len(cur)) + " characters of code)" if cur else "📢 Player ad: OFF") +
            "\n\nSet it: /setad <paste the full ad code here>\nRemove it: /setad off")
        return
    try:
        if arg.lower() == "off":
            if os.path.exists(stream.AD_FILE):
                os.remove(stream.AD_FILE)
            await update.message.reply_text("📢 Player ad removed.")
            return
        if len(arg) > 4000:
            await update.message.reply_text("That code is too long for one message. Put it in the file " + stream.AD_FILE + " on the server instead.")
            return
        tmp = stream.AD_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(arg)
        os.replace(tmp, stream.AD_FILE)
        await update.message.reply_text("📢 Player ad saved. It shows on the player page from the next time a link is opened.")
    except Exception as e:
        log.exception("setad failed")
        await update.message.reply_text(f"❌ Could not save: {e}")


@admin_cmd
async def cmd_sethead(update, context):
    """Site-verification tags (Monetag etc.) for the player's home page and <head> - no file upload needed."""
    parts = (update.message.text or "").split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not stream.ENABLED:
        await update.message.reply_text("The streaming player is off (STREAM_BASE_URL / STREAM_SECRET not set), so there is no home page to verify.")
        return
    if not arg:
        cur = stream.head_html()
        await update.message.reply_text(
            ("🏷 Verification tags: ON\n" + cur[:1500] if cur else "🏷 Verification tags: none") +
            f"\n\nSet: /sethead <meta name=\"monetag\" content=\"...\">\nRemove: /sethead off\nCheck: open {stream.BASE_URL}/ and view the page source.")
        return
    try:
        if arg.lower() == "off":
            if os.path.exists(stream.HEAD_FILE):
                os.remove(stream.HEAD_FILE)
            await update.message.reply_text("🏷 Verification tags removed.")
            return
        if not re.match(r"^<(meta|link|script)\b", arg, re.I) or len(arg) > 2000:
            await update.message.reply_text("Send the full tag exactly as the ad network gave it, starting with <meta ...> (max 2000 characters).")
            return
        tmp = stream.HEAD_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(arg + "\n")
        os.replace(tmp, stream.HEAD_FILE)
        await update.message.reply_text(f"🏷 Saved. It is now on {stream.BASE_URL}/ - open that address and check the page source, then click Verify in the ad network.")
    except Exception as e:
        log.exception("sethead failed")
        await update.message.reply_text(f"❌ Could not save: {e}")


@admin_cmd
async def cmd_adtest(update, context):
    code = stream.ad_html()
    lines = ["📢 Ad check",
             "Player: " + ("ON" if stream.ENABLED else "OFF - STREAM_BASE_URL / STREAM_SECRET are missing in .env, so there is no player and no ad"),
             "Ad file: " + os.path.abspath(stream.AD_FILE) + (f" - found, {len(code)} characters" if code else " - MISSING or EMPTY (use /setad or put the code in this file)"),
             f"Ad box height: {stream.AD_HEIGHT}px (60 for 320x50, 280 for 300x250)",
             "Verification tags (" + os.path.abspath(stream.HEAD_FILE) + "): " + ("found - " + str(len(stream.head_html())) + " characters, shown on the home page and the player" if stream.head_html() else "none"),
             "Storage for new uploads: " + ("Telegram channel - those episodes are sent as files and NEVER show ads (ads only appear on the R2 player)" if STORAGE_CHANNEL else "R2 - good")]
    if not stream.ENABLED or not code:
        await update.message.reply_text("\n".join(lines))
        return
    with closing(db()) as con:
        ep = con.execute("SELECT story_id, seq FROM episodes WHERE key NOT LIKE 'tg/%' ORDER BY story_id, seq LIMIT 1").fetchone()
    if not ep:
        lines.append("\nNo R2 episode in the database, so there is nothing the player can open. Run /sync.")
        await update.message.reply_text("\n".join(lines))
        return
    url = stream.url_for(update.effective_user.id, ep["story_id"], ep["seq"], int(time.time()) + 900) + "?adtest=1"
    lines.append("\nTEST 1 (normal, sandboxed) - open in your phone browser (valid 15 min). The text under the ad box tells what the ad network did:\n" + url)
    lines.append("\nTEST 2 (no sandbox, for testing only - never use for real listeners) - if the ad shows here but not in TEST 1, the ad network refuses sandboxed frames:\n" + url + "&nosb=1")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


@admin_cmd
async def cmd_forcesub(update, context):
    if not FORCE_SUB:
        await update.message.reply_text("Force-join is OFF. Set FORCE_SUB_CHANNELS in .env (for example @mychannel) and restart the bot.")
        return
    me = await context.bot.get_me()
    lines = ["🔒 Force-join channels:"]
    for chat, link in FORCE_SUB:
        try:
            c = await context.bot.get_chat(chat)
            m = await context.bot.get_chat_member(chat, me.id)
            good = m.status in ("administrator", "creator")
            lines.append(f"{'✅' if good else '⚠️'} {c.title} ({chat}) - bot is {m.status}"
                         + ("" if good else " - make the bot an admin so it can check members"))
        except Exception as e:
            lines.append(f"❌ {chat} - cannot access: {e}")
        if not link:
            lines.append("   ⚠️ no join link - add |https://t.me/+yourInviteLink after the id in .env")
    await reply_long(update, "\n".join(lines))


async def track_user(update, context):
    """Runs before every handler: remember each private-chat user and when they were last active."""
    u = update.effective_user
    if not u or u.is_bot or not update.effective_chat or update.effective_chat.type != "private":
        return
    try:
        now = int(time.time())
        with closing(db()) as con:
            con.execute("INSERT INTO users(user_id, username, name, first_seen, last_seen) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name, last_seen=excluded.last_seen",
                        (u.id, u.username, u.full_name, now, now))
            con.commit()
    except Exception as e:
        log.warning("user tracking failed: %s", e)


@admin_cmd
async def cmd_stats(update, context):
    now = int(time.time())
    d1, d7, d30 = now - 86400, now - 7 * 86400, now - 30 * 86400
    with closing(db()) as con:
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
        total = q("SELECT COUNT(*) FROM users")
        new1, new7 = q("SELECT COUNT(*) FROM users WHERE first_seen>?", d1), q("SELECT COUNT(*) FROM users WHERE first_seen>?", d7)
        act1, act7, act30 = (q("SELECT COUNT(*) FROM users WHERE last_seen>?", d) for d in (d1, d7, d30))
        paid = q("SELECT COUNT(*) FROM subs WHERE expires_at>?", now)
        s, n = q("SELECT COUNT(*) FROM stories"), q("SELECT COUNT(*) FROM stories WHERE title IS NOT NULL")
        e = q("SELECT COUNT(*) FROM episodes")
        p1, p7, pall = q("SELECT COUNT(*) FROM plays WHERE ts>?", d1), q("SELECT COUNT(*) FROM plays WHERE ts>?", d7), q("SELECT COUNT(*) FROM plays")
        first = q("SELECT MIN(first_seen) FROM users")
        top = con.execute("SELECT COALESCE(s.title, 'Story ' || p.story_id) t, COUNT(*) c FROM plays p LEFT JOIN stories s "
                          "ON s.story_id=p.story_id WHERE p.ts>? GROUP BY p.story_id ORDER BY c DESC LIMIT 5", (d7,)).fetchall()
    lines = [
        "📊 Bot stats",
        "",
        f"👥 Users: {total} total" + (f" (counted since {fmt(first)})" if first else ""),
        f"   New: {new1} today, {new7} this week",
        f"   Active: {act1} in 24h, {act7} in 7 days, {act30} in 30 days",
        f"   Paid (active): {paid}",
        "",
        f"🎧 Episodes sent: {p1} in 24h, {p7} in 7 days, {pall} total",
        f"📚 Library: {s} stories ({n} named), {e} episodes",
    ]
    if top:
        lines += ["", "🔥 Top this week:"] + [f"   {r['t']} - {r['c']}" for r in top]
    await update.message.reply_text("\n".join(lines))


# ------------------------------------------------------------ add episodes from the bot
ADD_LOCK = asyncio.Lock()
TG_DOWNLOAD_MAX = 20 * 1024 * 1024  # standard Bot API: bots can only download files up to 20 MB
MIME_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
            "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/opus": ".opus",
            "audio/webm": ".webm", "audio/flac": ".flac", "audio/x-flac": ".flac"}
OK_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".webm", ".flac"}


@admin_cmd
async def cmd_addto(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /addto <story id> [title]\nThen send the audio files. /done when finished.")
        return
    sid = int(context.args[0])
    title = " ".join(context.args[1:]).strip()
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO stories(story_id, title) VALUES(?, NULL)", (sid,))
        if title:
            con.execute("UPDATE stories SET title=? WHERE story_id=?", (title, sid))
        con.commit()
        s = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()
        n = con.execute("SELECT COALESCE(MAX(seq),0) FROM episodes WHERE story_id=?", (sid,)).fetchone()[0]
    context.user_data["add_story"] = sid
    await update.message.reply_text(
        f"📥 Adding to story {sid}: {s['title'] or '(unnamed - use /name or /addto again with a title)'}\n"
        f"Currently {n} episode(s). Send audio files now - next will be Ep {n + 1}.\n"
        f"Storage: {'Telegram channel' if STORAGE_CHANNEL else 'Cloudflare R2'}. /done to stop.")


@admin_cmd
async def cmd_done(update, context):
    sid = context.user_data.pop("add_story", None)
    await update.message.reply_text("✅ Stopped adding." if sid else "Nothing was being added.")


async def add_via_telegram(update, context, media, sid):
    """Store the episode in the private storage channel (no download, so no 20 MB limit)."""
    m = update.message
    status = await m.reply_text("⏳ Saving to Telegram storage...")
    key = f"tg/{sid}/{media.file_unique_id}"
    try:
        async with ADD_LOCK:
            with closing(db()) as con:
                if con.execute("SELECT 1 FROM episodes WHERE key=?", (key,)).fetchone():
                    await status.edit_text("⚠️ This exact file is already in that story - skipped.")
                    return
                con.execute("INSERT OR IGNORE INTO stories(story_id, title) VALUES(?, NULL)", (sid,))
                con.commit()
                seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM episodes WHERE story_id=?", (sid,)).fetchone()[0]
                title = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()["title"]
            cm = await context.bot.copy_message(chat_id=STORAGE_CHANNEL, from_chat_id=m.chat_id, message_id=m.message_id,
                                                caption=f"{title or 'Story ' + str(sid)} - Ep {seq}")
            kind = "audio" if m.audio else "document"
            with closing(db()) as con:
                con.execute("INSERT INTO episodes(key, story_id, seq, size, modified, file_id, kind, tg_chat, tg_msg) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (key, sid, seq, media.file_size or 0, int(time.time()), media.file_id, kind,
                             STORAGE_CHANNEL, cm.message_id))
                con.commit()
    except Exception as e:
        log.exception("add via telegram failed")
        await status.edit_text(f"❌ Failed: {e}\nCheck STORAGE_CHANNEL_ID and that the bot is an admin of the channel with permission to post.")
        return
    note = f"✅ Added to story {sid} ({title or 'unnamed'}) as Ep {seq} - saved in Telegram storage"
    if not title:
        note += f"\n⚠️ Story has no name yet - /name {sid} <title> so users can search it."
    await status.edit_text(note)


# ------------------------------------------------------------ sequential upload queue
# Files are processed ONE AT A TIME in the order they were sent (Telegram message id), so a mass upload
# gets Ep n, n+1, n+2... exactly in sending order, whatever the download/upload speed of each file.
UPLOAD_Q = []          # heap of (message_id, counter, job)
_upload_counter = [0]
_upload_task = [None]


def enqueue_upload(message_id, job):
    import heapq
    _upload_counter[0] += 1
    heapq.heappush(UPLOAD_Q, (message_id, _upload_counter[0], job))
    if _upload_task[0] is None or _upload_task[0].done():
        _upload_task[0] = asyncio.get_running_loop().create_task(upload_worker())


async def upload_worker():
    import heapq
    await asyncio.sleep(0.8)  # let files that arrived in the same instant line up in message-id order
    while UPLOAD_Q:
        _, _, job = heapq.heappop(UPLOAD_Q)
        try:
            await job()
        except Exception:
            log.exception("upload job crashed")
        if not UPLOAD_Q:
            await asyncio.sleep(0.8)  # next file of a mass upload may be a moment behind


async def on_file(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return
    m = update.message
    media = m.audio or m.document
    if not media:
        return
    name = getattr(media, "file_name", None) or ""
    ext = os.path.splitext(name)[1].lower() or MIME_EXT.get((media.mime_type or "").lower(), "")
    if ext not in OK_EXT:
        if m.audio:
            ext = ".mp3"
        else:
            return  # some other document, ignore
    # story: caption "<story id>" overrides the active /addto story
    cap = (m.caption or "").strip()
    sid = int(cap.split()[0]) if cap.split() and cap.split()[0].isdigit() else context.user_data.get("add_story")
    if not sid:
        await m.reply_text("Which story? Use /addto <story id> first, or put the story id as the file caption.")
        return
    if not STORAGE_CHANNEL and (media.file_size or 0) > TG_DOWNLOAD_MAX:
        await m.reply_text(f"❌ {name or 'File'} is {media.file_size / 1048576:.1f} MB. Bots can only download up to 20 MB - "
                           f"upload this one straight to R2 as audio/{sid}/<file>{ext} and run /sync.")
        return
    # Queue it. NOTHING may be awaited before this line: updates run concurrently, so the queue (sorted by Telegram
    # message id = the order you sent the files) is what guarantees Ep numbers follow your sending order.
    if STORAGE_CHANNEL:
        job = lambda: add_via_telegram(update, context, media, sid)
    else:
        job = lambda: add_via_r2(update, context, media, sid, ext)
    enqueue_upload(m.message_id, job)


async def add_via_r2(update, context, media, sid, ext):
    m = update.message
    status = await m.reply_text("⏳ Uploading to R2...")
    try:
        f = await context.bot.get_file(media.file_id)
        data = bytes(await f.download_as_bytearray())
        key = f"audio/{sid}/{int(time.time())}_{media.file_unique_id}{ext}"
        ctype = media.mime_type if (media.mime_type or "").startswith("audio/") else "audio/mpeg"
        await asyncio.to_thread(lambda: s3.put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType=ctype))
        kind = "audio" if m.audio else "document"
        async with ADD_LOCK:
            with closing(db()) as con:
                con.execute("INSERT OR IGNORE INTO stories(story_id, title) VALUES(?, NULL)", (sid,))
                seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM episodes WHERE story_id=?", (sid,)).fetchone()[0]
                con.execute("INSERT INTO episodes(key, story_id, seq, size, modified, file_id, kind) VALUES(?,?,?,?,?,?,?)",
                            (key, sid, seq, len(data), int(time.time()), media.file_id, kind))
                con.commit()
                title = con.execute("SELECT title FROM stories WHERE story_id=?", (sid,)).fetchone()["title"]
    except Exception as e:
        log.exception("add episode failed")
        await status.edit_text(f"❌ Failed: {e}")
        return
    note = f"✅ Added to story {sid} ({title or 'unnamed'}) as Ep {seq}"
    if not title:
        note += f"\n⚠️ Story has no name yet - /name {sid} <title> so users can search it."
    await status.edit_text(note)


# ------------------------------------------------------------ browse & request
LIST_PAGE = 10
REQ_PER_DAY = 5


def list_page(page):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT s.story_id, s.title, COUNT(e.key) c FROM stories s JOIN episodes e ON e.story_id=s.story_id "
            "WHERE s.title IS NOT NULL AND s.title<>'' GROUP BY s.story_id ORDER BY s.title COLLATE NOCASE").fetchall()
    if not rows:
        return None
    pages = (len(rows) + LIST_PAGE - 1) // LIST_PAGE
    page = max(0, min(page, pages - 1))
    chunk = rows[page * LIST_PAGE:(page + 1) * LIST_PAGE]
    kb = [[InlineKeyboardButton(f"{r['title']} ({r['c']} eps)", callback_data=f"pick:{r['story_id']}")] for r in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"list:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"list:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"list:{page + 1}"))
    kb.append(nav)
    return f"📚 Available series ({len(rows)}) - tap one:\nCan't find yours? /request <name>", InlineKeyboardMarkup(kb)


async def cmd_list(update, context):
    if not await guard(update):
        return
    data = list_page(0)
    if not data:
        await update.message.reply_text("No series available yet.")
        return
    await update.message.reply_text(data[0], reply_markup=data[1])


async def cmd_request(update, context):
    if not await guard(update):
        return
    text = " ".join(context.args).strip()[:200]
    if not text:
        await update.message.reply_text("Usage: /request <series name>\nExample: /request Mera Naam Joker")
        return
    u = update.effective_user
    now = int(time.time())
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND created>?", (u.id, now - 86400)).fetchone()[0]
        if n >= REQ_PER_DAY:
            await update.message.reply_text(f"You can send up to {REQ_PER_DAY} requests per day. Try again tomorrow.")
            return
        cur = con.execute("INSERT INTO requests(user_id, username, text, created) VALUES(?,?,?,?)",
                          (u.id, u.username, text, now))
        con.commit()
        rid = cur.lastrowid
    await update.message.reply_text(f"✅ Request #{rid} sent: {text}\nWe'll notify you here when it's added.")
    who = f"{u.full_name} (@{u.username})" if u.username else u.full_name
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"📩 Request #{rid} from {who}, id {u.id}:\n{text}\n\n/reqdone {rid} once added")
        except Exception as e:
            log.warning("could not notify admin %s: %s", aid, e)


@admin_cmd
async def cmd_requests(update, context):
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM requests WHERE status='pending' ORDER BY id DESC LIMIT 40").fetchall()
    if not rows:
        await update.message.reply_text("No pending requests.")
        return
    await reply_long(update, "\n".join(f"#{r['id']} - {r['text']} ({fmt(r['created'])}, user {r['user_id']})" for r in rows)
                     + "\n\n/reqdone <id> when added")


@admin_cmd
async def cmd_reqdone(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /reqdone <request id>")
        return
    rid = int(context.args[0])
    with closing(db()) as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            await update.message.reply_text("Request not found.")
            return
        con.execute("UPDATE requests SET status='done' WHERE id=?", (rid,))
        con.commit()
    try:
        await context.bot.send_message(r["user_id"], f"✅ Your requested series \"{r['text']}\" has been added! Search for it or use /list.")
        extra = " User notified."
    except Exception:
        extra = " (Could not notify the user - they may have blocked the bot.)"
    await update.message.reply_text(f"✅ Request #{rid} marked done.{extra}")


# ------------------------------------------------------------ search & pick
async def on_text(update, context):
    if not await guard(update):
        return
    text = update.message.text.strip()
    m = re.fullmatch(r"(\d+)\s*(?:-|–|to)\s*(\d+)|(\d+)", text)
    if m:
        sid = context.user_data.get("story_id")
        if not sid:
            await update.message.reply_text("First search for a series and tap it.")
            return
        lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)),) * 2
        await send_episodes(update, context, sid, min(lo, hi), max(lo, hi))
        return
    like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with closing(db()) as con:
        rows = con.execute(
            "SELECT s.story_id, s.title, COUNT(e.key) c FROM stories s JOIN episodes e ON e.story_id=s.story_id "
            "WHERE s.title LIKE ? ESCAPE '\\' GROUP BY s.story_id ORDER BY s.title LIMIT 15", (like,)).fetchall()
    if not rows:
        await update.message.reply_text("❌ No series found. Try another name, browse with /list, or ask for it with /request <name>.")
        return
    kb = [[InlineKeyboardButton(f"{r['title']} ({r['c']} eps)", callback_data=f"pick:{r['story_id']}")] for r in rows]
    await update.message.reply_text("Select a series:", reply_markup=InlineKeyboardMarkup(kb))


async def on_callback(update, context):
    q = update.callback_query
    action, _, val = q.data.partition(":")
    if action != "fsub":
        await q.answer()
    if action == "del":
        await delete_callback(update, context)
        return
    ok, msg = await check_access(q.from_user.id)
    if not ok:
        if action == "fsub":
            await q.answer()
        await q.message.reply_text(msg)
        return
    if action == "fsub":  # "I've joined" button: re-check now
        FSUB_CACHE.pop(q.from_user.id, None)
        if await missing_channels(context.bot, q.from_user.id):
            await q.answer("You haven't joined yet. Join the channel, then tap again.", show_alert=True)
            return
        await q.answer()
        try:
            await q.edit_message_text("✅ Thanks for joining! Send a series name to search, or use /list.")
        except Exception:
            pass
        return
    miss = await missing_channels(context.bot, q.from_user.id)
    if miss:
        await force_prompt(q.message, miss)
        return
    if action == "list":
        data = list_page(int(val))
        if data:
            try:
                await q.edit_message_text(data[0], reply_markup=data[1])
            except Exception:
                pass  # same page tapped again
        return
    if action != "pick":
        return
    with closing(db()) as con:
        s = con.execute("SELECT * FROM stories WHERE story_id=?", (int(val),)).fetchone()
        c = con.execute("SELECT COUNT(*) n, MIN(seq) lo, MAX(seq) hi FROM episodes WHERE story_id=?", (int(val),)).fetchone()
    if not s or not c["n"]:
        await q.edit_message_text("Series not found.")
        return
    context.user_data["story_id"] = int(val)
    lo, hi = c["lo"], c["hi"]
    await q.edit_message_text(
        f"📺 {s['title']}\nTotal episodes: {c['n']} (Ep {lo} - {hi})\n\n"
        f"Send an episode number (e.g. {hi}) or a range (e.g. {lo}-{min(lo + 19, hi)}).")


# ------------------------------------------------------------ sending
async def send_one(context, chat_id, ep, title):
    seq = ep["seq"]
    caption = f"{title} - Ep {seq}"
    if stream.ENABLED and not ep["key"].startswith("tg/"):  # R2 episode -> temporary streaming link, not a file
        exp = int(time.time()) + (DELETE_AFTER if DELETE_AFTER > 0 else stream.SESSION_SECONDS)
        url = stream.url_for(chat_id, ep["story_id"], seq, exp)
        msg = await context.bot.send_message(chat_id, f"🎧 {caption}", protect_content=True,
                                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶ Play", url=url)]]))
        return msg, None
    if ep["file_id"]:
        try:
            if ep["kind"] == "audio":
                return await context.bot.send_audio(chat_id, audio=ep["file_id"], title=caption, protect_content=True), None
            return await context.bot.send_document(chat_id, document=ep["file_id"], caption=caption, protect_content=True), None
        except Exception as e:
            log.warning("cached file_id failed for %s: %s", ep["key"], e)
    if ep.get("tg_msg"):
        try:
            return await context.bot.copy_message(chat_id, from_chat_id=ep["tg_chat"], message_id=ep["tg_msg"],
                                                  caption=caption, protect_content=True), None
        except Exception as e:
            log.error("copy from storage channel failed for %s: %s", ep["key"], e)
            return None, f"Ep {seq}: could not send"
    if ep["size"] > TG_MAX_BYTES:
        return None, f"Ep {seq}: file is too large for Telegram bots"
    data = await asyncio.to_thread(r2_get, ep["key"])
    ext = os.path.splitext(ep["key"])[1] or ".mp3"
    fname = re.sub(r'[\\/:*?"<>|]', "", f"{caption}{ext}")
    try:
        msg = await context.bot.send_audio(chat_id, audio=InputFile(io.BytesIO(data), filename=fname),
                                           title=caption, protect_content=True)
        kind, fid = "audio", msg.audio.file_id
    except Exception as e:
        log.warning("send_audio failed (%s), trying document", e)
        msg = await context.bot.send_document(chat_id, document=InputFile(io.BytesIO(data), filename=fname),
                                              caption=caption, protect_content=True)
        kind, fid = "document", msg.document.file_id
    with closing(db()) as con:
        con.execute("UPDATE episodes SET file_id=?, kind=? WHERE key=?", (fid, kind, ep["key"]))
        con.commit()
    return msg, None


async def send_episodes(update, context, story_id, lo, hi):
    chat_id = update.effective_chat.id
    if hi - lo + 1 > MAX_RANGE:
        await update.message.reply_text(f"Please request at most {MAX_RANGE} episodes at a time.")
        return
    with closing(db()) as con:
        s = con.execute("SELECT title FROM stories WHERE story_id=?", (story_id,)).fetchone()
        eps = [dict(r) for r in con.execute(
            "SELECT * FROM episodes WHERE story_id=? AND seq BETWEEN ? AND ? ORDER BY seq", (story_id, lo, hi))]
    if not eps:
        await update.message.reply_text("No episodes found in that range.")
        return
    status = await update.message.reply_text(f"⏳ Preparing {len(eps)} episode(s)...")
    sent, errors = 0, []
    for ep in eps:
        ok, _ = await check_access(update.effective_user.id)  # access can't outlive the subscription mid-request
        if not ok:
            errors.append("Subscription ended")
            break
        try:
            msg, err = await send_one(context, chat_id, ep, s["title"])
        except Exception as e:
            log.error("send failed %s: %s", ep["key"], e)
            msg, err = None, f"Ep {ep['seq']}: could not send"
        if err:
            errors.append(err)
            continue
        sent += 1
        try:
            with closing(db()) as con:
                con.execute("INSERT INTO plays(user_id, story_id, seq, ts) VALUES(?,?,?,?)",
                            (update.effective_user.id, story_id, ep["seq"], int(time.time())))
                con.commit()
        except Exception as e:
            log.warning("play log failed: %s", e)
        if DELETE_AFTER > 0:
            context.job_queue.run_once(delete_job, DELETE_AFTER, data={"chat_id": chat_id, "message_id": msg.message_id})
    try:
        await status.delete()
    except Exception:
        pass
    note = f"✅ Sent {sent} episode(s)."
    if sent and DELETE_AFTER > 0:
        note += f"\n🗑 They will be deleted in {max(DELETE_AFTER // 60, 1)} min."
    if errors:
        note += "\n⚠️ " + "\n⚠️ ".join(errors[:10])
    await update.message.reply_text(note)


# ------------------------------------------------------------ main
def main():
    if not ADMIN_IDS:
        raise SystemExit("Set ADMIN_IDS in .env (your Telegram user id).")
    init_db()
    stream.configure(db, s3, R2_BUCKET, check_access)
    builder = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    if stream.ENABLED:
        builder = builder.post_init(stream.start).post_shutdown(stream.stop)
    app = builder.build()
    priv = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, track_user), group=-1)
    for name, fn in [("start", cmd_start), ("help", cmd_help), ("status", cmd_status), ("sync", cmd_sync),
                     ("stories", cmd_stories), ("name", cmd_name), ("swap", cmd_swap), ("grant", cmd_grant),
                     ("revoke", cmd_revoke), ("subs", cmd_subs), ("clearcache", cmd_clearcache), ("stats", cmd_stats),
                     ("addto", cmd_addto), ("done", cmd_done), ("delete", cmd_delete), ("renumber", cmd_renumber),
                     ("forcesub", cmd_forcesub), ("setad", cmd_setad), ("adtest", cmd_adtest), ("sethead", cmd_sethead), ("list", cmd_list), ("request", cmd_request), ("requests", cmd_requests), ("reqdone", cmd_reqdone)]:
        app.add_handler(CommandHandler(name, fn, filters=priv))
    app.add_handler(MessageHandler((filters.AUDIO | filters.Document.ALL) & priv, on_file))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & priv, on_text))
    log.info("Bot started (streaming %s)", "ON" if stream.ENABLED else "off")
    app.run_polling()


if __name__ == "__main__":
    main()
