"""Telegram xHamster bot — Telethon/MTProto, GitHub-Actions friendly."""
import asyncio
import base64
import json
import os
import secrets
import subprocess
import traceback
from collections import OrderedDict

import requests
from telethon import Button, TelegramClient, events
from telethon.errors import (FloodWaitError, MessageIdInvalidError,
                             FileReferenceExpiredError)
from telethon.sessions import MemorySession
from telethon.tl.types import (InputDocument, KeyboardButton,
                             KeyboardButtonRow, ReplyKeyboardMarkup)
import time

import xhamster as xh

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

PER_PAGE = 10
MAX_BYTES = 400 * 1024 * 1024
MIN_VALID_BYTES = 200 * 1024  # anything smaller is an error page, not a video
RUN_HOURS = float(os.environ.get("RUN_HOURS", "5.8"))  # 0 = run forever
CACHE_FILE = "data/fileids.json"
STATS_FILE = "data/stats.json"
MAX_SESSIONS = 500
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # your Telegram user id

SESSIONS = OrderedDict()
DL_SEM = asyncio.Semaphore(2)
CACHE = {}
STATS = {"users": {}, "queries": []}   # users: {uid: {...}}, queries: capped list
STATS_DIRTY = False
LAST_FLUSH = 0.0

# persistent click-menu (no more typing /search all the time)
MENU = ReplyKeyboardMarkup(
    rows=[KeyboardButtonRow(buttons=[KeyboardButton("\U0001F50D Search")]),
          KeyboardButtonRow(buttons=[KeyboardButton("\u2139\uFE0F Help")])],
    resize=True)

client = TelegramClient(MemorySession(), API_ID, API_HASH)


# ---------- sessions (with ticket ID, so old buttons die safely) ----------

def get_session(uid):
    if uid in SESSIONS:
        SESSIONS.move_to_end(uid)
        return SESSIONS[uid]
    return None


def set_session(uid, value):
    SESSIONS[uid] = value
    SESSIONS.move_to_end(uid)
    while len(SESSIONS) > MAX_SESSIONS:
        SESSIONS.popitem(last=False)


# ---------- cache ----------

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            CACHE.update(json.load(f))


def save_cache():
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(CACHE, f)


# ---------- stats & query logging ----------

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE) as f:
                data = json.load(f)
            STATS["users"].update(data.get("users", {}))
            STATS["queries"].extend(data.get("queries", []))
        except Exception as e:
            print("[debug] stats load failed:", repr(e)[:150])


def flush_stats(force=False):
    global STATS_DIRTY, LAST_FLUSH
    now = time.time()
    if not STATS_DIRTY and not force:
        return
    if not force and now - LAST_FLUSH < 30:
        return
    os.makedirs("data", exist_ok=True)
    tmp = STATS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATS, f)
    os.replace(tmp, STATS_FILE)
    STATS_DIRTY = False
    LAST_FLUSH = now


async def track(event):
    """Record/refresh a user in stats (fire-and-forget)."""
    global STATS_DIRTY
    try:
        uid = str(event.sender_id)
        u = STATS["users"].setdefault(uid, {"name": "", "uses": 0,
                                            "first": 0, "last": 0})
        snd = await event.get_sender()
        if getattr(snd, "username", None):
            u["name"] = "@" + snd.username
        elif getattr(snd, "first_name", None):
            u["name"] = snd.first_name
        now = time.time()
        u["uses"] += 1
        if not u["first"]:
            u["first"] = now
        u["last"] = now
        STATS_DIRTY = True
    except Exception:
        pass


def log_query(query, sort, uid):
    global STATS_DIRTY
    STATS["queries"].append({
        "q": query[:120], "sort": sort, "uid": str(uid),
        "name": (STATS["users"].get(str(uid), {}) or {}).get("name", ""),
        "ts": time.time(),
    })
    if len(STATS["queries"]) > 500:
        del STATS["queries"][:-500]
    STATS_DIRTY = True


def _fmt_ts(ts):
    return time.strftime("%d/%m %H:%M", time.gmtime(ts))


def build_stats_text():
    users = STATS["users"]
    now = time.time()
    day = 86400
    total = len(users)
    today = sum(1 for u in users.values() if now - u["last"] < day)
    week = sum(1 for u in users.values() if now - u["last"] < 7 * day)
    return ("\U0001F465 Total users: {0}\n"
            "\u2705 Active today: {1}\n"
            "\U0001F4C5 Active this week: {2}\n"
            "\U0001F50E Total searches logged: {3}").format(
        total, today, week, len(STATS["queries"]))


def build_queries_text(limit=15):
    if not STATS["queries"]:
        return "No searches logged yet."
    lines = []
    for r in STATS["queries"][-limit:][::-1]:
        lines.append("{0}  {1}: {2} [{3}]".format(
            _fmt_ts(r["ts"]), r.get("name") or r["uid"], r["q"], r["sort"]))
    return "\U0001F50D Recent searches:\n\n" + "\n".join(lines)


def build_top_text(limit=10):
    from collections import Counter
    c = Counter(q["q"].strip().lower() for q in STATS["queries"] if q["q"].strip())
    if not c:
        return "No searches logged yet."
    lines = ["{0}. {1} — {2}x".format(i + 1, q, n)
             for i, (q, n) in enumerate(c.most_common(limit))]
    return "\U0001F3C6 Top queries:\n\n" + "\n".join(lines)


def build_users_text(limit=20):
    users = STATS["users"]
    if not users:
        return "No users yet."
    rows = sorted(users.items(), key=lambda kv: kv[1]["last"], reverse=True)
    lines = ["{0}  {1}  ({2}x, last {3})".format(
        u["name"] or uid, uid, u["uses"], _fmt_ts(u["last"]))
        for uid, u in rows[:limit]]
    return "\U0001F465 Last {0} users (of {1}):\n\n{2}".format(
        min(limit, len(rows)), len(rows), "\n".join(lines))


# ---------- helpers ----------

def is_probably_video(path):
    """A real mp4/mkv starts with a magic header; an HTML error page doesn't."""
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) < MIN_VALID_BYTES:
        return False
    with open(path, "rb") as f:
        head = f.read(12)
    return (head[4:8] == b"ftyp"      # mp4/mov
            or head[:4] == b"\x1a\x45\xdf\xa3"  # matroska/webm
            or head[:1] == b"\x47")   # mpeg-ts


def download(url, path):
    """Download a direct mp4 URL, failing fast on expired/ciphered URLs."""
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("not a real URL (probably a ciphered token): {0!r}"
                         .format(url[:80]))
    with requests.get(url, headers=xh.HEADERS, stream=True,
                      timeout=(10, 60)) as r:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").lower()
        if "text/html" in ctype or "json" in ctype:
            raise RuntimeError(
                "CDN returned {0} ({1}) — URL is likely ciphered/expired"
                .format(r.status_code, ctype))
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def download_hls(url, path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-headers", "Referer: https://xhamster.com/\r\n",
         "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", path],
        check=True, timeout=1500)


def download_ytdlp(url, vid):
    """Last resort: let yt-dlp (kept up to date for xHamster) fetch it."""
    import glob
    import yt_dlp
    opts = {
        "outtmpl": "/tmp/{0}.%(ext)s".format(vid),
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "quiet": True, "no_warnings": True, "retries": 3,
        "fragment_retries": 3, "noplaylist": True,
        "socket_timeout": 30, "nocheckcertificate": True,
        "logger": None, "progress_hooks": [],
    }
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget("chrome")
    except Exception:
        pass
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if "impersonat" in str(e).lower() and "impersonate" in opts:
            opts.pop("impersonate")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        else:
            raise
    files = [f for f in glob.glob("/tmp/{0}.*".format(vid))
             if not f.endswith(".part")
             and os.path.getsize(f) > MIN_VALID_BYTES]
    return sorted(files)[0] if files else None


def item_buttons(s, idx):
    data = "dl:{sid}:{idx}".format(sid=s["sid"], idx=idx)
    return [[Button.inline("\u2b07 Download", data.encode())]]


def control_buttons(s, pages):
    row = []
    if s["page"] > 0:
        row.append(Button.inline("\u25c0 Back",
                                 "pg:{sid}:{p}".format(sid=s["sid"], p=s["page"] - 1).encode()))
    if s["page"] < pages - 1:
        row.append(Button.inline("Next \u25b6",
                                 "pg:{sid}:{p}".format(sid=s["sid"], p=s["page"] + 1).encode()))
    return [row] if row else None


# ---------- rendering ----------

async def render(event, uid, first=False):
    s = get_session(uid)
    if not s or not s.get("results"):
        await event.respond("Type /search to start.")
        return
    chat = event.chat_id
    start = s["page"] * PER_PAGE
    batch = s["results"][start:start + PER_PAGE]
    pages = max(1, (len(s["results"]) + PER_PAGE - 1) // PER_PAGE)

    async def send_fresh():
        s["msg_ids"] = []
        for i, r in enumerate(batch):
            m = await client.send_file(chat, r["thumb"], caption=r["title"],
                                       buttons=item_buttons(s, start + i))
            s["msg_ids"].append(m.id)
        c = await client.send_message(
            chat, "Page {0}/{1}".format(s["page"] + 1, pages),
            buttons=control_buttons(s, pages))
        s["msg_ids"].append(c.id)

    stale = len(s.get("msg_ids", [])) != len(batch) + 1
    if first or stale:
        await send_fresh()
        return

    try:
        for i, r in enumerate(batch):
            await client.edit_message(chat, s["msg_ids"][i], file=r["thumb"],
                                      text=r["title"],
                                      buttons=item_buttons(s, start + i))
        await client.edit_message(
            chat, s["msg_ids"][-1],
            "Page {0}/{1}".format(s["page"] + 1, pages),
            buttons=control_buttons(s, pages))
    except (MessageIdInvalidError, ValueError):
        # a message was deleted (or bot restarted) — re-render fresh
        await send_fresh()


# ---------- download & deliver ----------

async def deliver(chat, r):
    vid = r["id"]
    tmp = "/tmp/{0}.mp4".format(vid)
    status = await client.send_message(chat, "Fetching...")
    async with DL_SEM:
        path = None
        try:
            # 1) cached Telegram file reference (fast re-send)
            cached = CACHE.get(vid)
            if cached:
                try:
                    doc = InputDocument(
                        id=cached["id"], access_hash=cached["ah"],
                        file_reference=base64.b64decode(cached["fr"]))
                    await client.send_file(chat, doc, caption=r["title"],
                                           supports_streaming=True)
                    await status.delete()
                    return
                except FileReferenceExpiredError:
                    CACHE.pop(vid, None)
                    save_cache()
                except Exception as e:
                    print("[debug] cached send failed:", repr(e)[:150])
                    CACHE.pop(vid, None)
                    save_cache()

            # 2) direct mp4 URL from window.initials (fast path)
            try:
                variants, hls, secs = await asyncio.to_thread(
                    xh.sources, r["url"])
            except Exception as e:
                print("[debug] sources failed:", repr(e)[:200])
                variants, hls, secs = [], None, None
            print("[debug] variants={0} hls={1}".format(len(variants), bool(hls)))
            pick = xh.pick_under(variants, MAX_BYTES, secs)
            if pick:
                try:
                    await asyncio.to_thread(download, pick["url"], tmp)
                    if is_probably_video(tmp):
                        path = tmp
                    else:
                        raise RuntimeError("not a valid video file")
                except Exception as e:
                    print("[debug] direct download failed:", repr(e)[:200])

            # 3) HLS master playlist via ffmpeg
            if path is None and hls:
                try:
                    await asyncio.to_thread(download_hls, hls, tmp)
                    if is_probably_video(tmp):
                        path = tmp
                except Exception as e:
                    print("[debug] hls failed:", repr(e)[:200])

            # 4) yt-dlp (kept current in CI — it is the maintained decipherer)
            if path is None:
                print("[debug] trying yt-dlp fallback")
                try:
                    found = await asyncio.to_thread(
                        download_ytdlp, r["url"], vid)
                    if found and is_probably_video(found):
                        path = found
                except Exception as e:
                    print("[debug] yt-dlp failed:", repr(e)[:300])

            if path is None:
                await status.edit(
                    "No downloadable source found.\n\nDEBUG: "
                    + (xh.LAST_DUMP or "(empty)")[:1200])
                return

            msg = await client.send_file(chat, path, caption=r["title"],
                                         supports_streaming=True)
            if msg.document:
                CACHE[vid] = {
                    "id": msg.document.id,
                    "ah": msg.document.access_hash,
                    "fr": base64.b64encode(
                        msg.document.file_reference).decode(),
                }
                save_cache()
            try:
                await status.delete()
            except Exception:
                pass
        except FloodWaitError as e:
            # Telegram told us to slow down — respect it
            await asyncio.sleep(e.seconds)
            try:
                await status.edit(
                    "Telegram asked for a pause. Try again in a minute.")
            except Exception:
                pass
        except Exception as e:
            try:
                await status.edit("Download failed: {0}: {1}".format(
                    type(e).__name__, str(e)[:300]))
            except Exception:
                try:
                    await client.send_message(
                        chat, "Download failed: {0}: {1}".format(
                            type(e).__name__, str(e)[:300]))
                except Exception:
                    pass
            print("[error] deliver failed for {0}".format(vid))
            traceback.print_exc()
        finally:
            for p in (tmp, "/tmp/{0}.mkv".format(vid), "/tmp/{0}.webm".format(vid)):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


# ---------- admin ----------

def admin_buttons():
    return [[Button.inline("\U0001F4CA Stats", b"adm:stats"),
             Button.inline("\U0001F50D Recent", b"adm:queries")],
            [Button.inline("\U0001F3C6 Top", b"adm:top"),
             Button.inline("\U0001F465 Users", b"adm:users")]]


@client.on(events.NewMessage(pattern=r"/(admin|panel|query|queries|stats)"))
async def on_admin(event):
    if not ADMIN_ID or event.sender_id != ADMIN_ID:
        return
    await track(event)
    await event.respond("\U0001F6E1 Admin panel:", buttons=admin_buttons())


# ---------- handlers ----------

@client.on(events.NewMessage(pattern=r"/(start|search)"))
async def on_start(event):
    await track(event)
    set_session(event.sender_id, {"state": "await_kw"})
    await event.respond("Type your keywords or tags:", buttons=MENU)


@client.on(events.NewMessage(
    func=lambda e: e.is_private and (e.text or "") and not e.text.startswith("/")))
async def on_text(event):
    await track(event)
    text = (event.text or "").strip()
    # persistent click-menu entries work from any state
    if text == "\U0001F50D Search" or text.lower() in ("/search", "/start"):
        set_session(event.sender_id, {"state": "await_kw"})
        await event.respond("Type your keywords or tags:", buttons=MENU)
        return
    if text == "\u2139\uFE0F Help":
        await event.respond(
            "Tap \U0001F50D Search, type keywords, pick a sort order, "
            "then hit \u2b07 Download on any result. Use /search anytime.",
            buttons=MENU)
        return
    s = get_session(event.sender_id)
    if not s or s.get("state") != "await_kw":
        return
    s["query"] = text
    s["state"] = "await_sort"
    await event.respond("Sort by?", buttons=[[
        Button.inline("Trending", b"sort:trending"),
        Button.inline("All time", b"sort:top"),
        Button.inline("Newest", b"sort:newest"),
    ]])


@client.on(events.CallbackQuery())
async def on_cb(event):
    uid = event.sender_id
    parts = event.data.decode().split(":")
    kind = parts[0]

    if kind == "adm":
        if event.sender_id != ADMIN_ID or not ADMIN_ID:
            await event.answer("Not authorized.", alert=True)
            return
        if len(parts) != 2:
            return
        which = parts[1]
        if which == "stats":
            text = build_stats_text()
        elif which == "queries":
            text = build_queries_text()
        elif which == "top":
            text = build_top_text()
        elif which == "users":
            text = build_users_text()
        else:
            await event.answer()
            return
        await event.answer()
        await event.edit(text, buttons=admin_buttons())
        return

    if kind == "sort":
        if len(parts) != 2:
            return
        s = get_session(uid)
        if not s or "query" not in s:
            await event.answer("Type /search first.", alert=True)
            return
        await event.answer("Searching...")
        await track(event)
        log_query(s["query"], parts[1], uid)
        flush_stats(force=True)
        results = await asyncio.to_thread(xh.search, s["query"], parts[1], 60)
        s["sid"] = secrets.token_hex(3)      # ticket for this search
        s["results"] = results
        s["page"] = 0
        s["msg_ids"] = []
        s["state"] = "browsing"
        if not results:
            await event.respond("No results.\n\nDEBUG: " + xh.LAST_SEARCH)
            return
        await render(event, uid, first=True)
        return

    # pg and dl carry the search ticket
    if len(parts) != 3:
        return
    _, sid, arg = parts
    s = get_session(uid)
    if not s or s.get("sid") != sid or s.get("state") != "browsing":
        await event.answer("Session expired — type /search again.", alert=True)
        return

    if kind == "pg":
        idx = int(arg)
        pages = max(1, (len(s["results"]) + PER_PAGE - 1) // PER_PAGE)
        if not 0 <= idx < pages:
            await event.answer()
            return
        s["page"] = idx
        await event.answer()
        await render(event, uid)

    elif kind == "dl":
        i = int(arg)
        if not 0 <= i < len(s["results"]):
            await event.answer("Result no longer available — search again.",
                               alert=True)
            return
        await event.answer("Fetching...")
        asyncio.create_task(deliver(event.chat_id, s["results"][i]))


# ---------- main ----------

async def _stats_flusher():
    while True:
        await asyncio.sleep(30)
        flush_stats()


async def run():
    load_cache()
    load_stats()
    await client.start(bot_token=BOT_TOKEN)
    print("Bot running.")
    flusher = asyncio.create_task(_stats_flusher())
    if RUN_HOURS <= 0:
        await client.run_until_disconnected()
    else:
        await asyncio.sleep(RUN_HOURS * 3600)
        await client.disconnect()
    flusher.cancel()
    flush_stats(force=True)
    save_cache()


if __name__ == "__main__":
    client.loop.run_until_complete(run())