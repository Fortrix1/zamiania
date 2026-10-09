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
from telethon.tl.types import InputDocument

import xhamster as xh

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

PER_PAGE = 10
MAX_BYTES = 400 * 1024 * 1024
RUN_HOURS = 5.8
CACHE_FILE = "data/fileids.json"
MAX_SESSIONS = 500

SESSIONS = OrderedDict()
DL_SEM = asyncio.Semaphore(2)
CACHE = {}

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


# ---------- helpers ----------

def download(url, path):
    with requests.get(url, headers=xh.HEADERS, stream=True, timeout=90) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def download_hls(url, path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-headers", "Referer: https://xhamster.com/\r\n",
         "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", path],
        check=True, timeout=1500)


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
    return [row] if row else [[]]


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
    path = "/tmp/{0}.mp4".format(vid)
    async with DL_SEM:
        try:
            cached = CACHE.get(vid)
            if cached:
                try:
                    doc = InputDocument(
                        id=cached["id"], access_hash=cached["ah"],
                        file_reference=base64.b64decode(cached["fr"]))
                    await client.send_file(chat, doc, caption=r["title"],
                                           supports_streaming=True)
                    return
                except FileReferenceExpiredError:
                    CACHE.pop(vid, None)
                    save_cache()

            variants, hls, secs = await asyncio.to_thread(xh.sources, r["url"])
            print("[debug] variants={0} hls={1}".format(len(variants), bool(hls)))
            pick = xh.pick_under(variants, MAX_BYTES, secs)
            if pick:
                await asyncio.to_thread(download, pick["url"], path)
            elif hls:
                await asyncio.to_thread(download_hls, hls, path)
            else:
                await client.send_message(chat, "No downloadable source found.")
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
        except FloodWaitError as e:
            # Telegram told us to slow down — respect it
            await asyncio.sleep(e.seconds)
            try:
                await client.send_message(
                    chat, "Telegram asked for a pause. Try again in a minute.")
            except Exception:
                pass
        except Exception:
            try:
                await client.send_message(chat, "Download failed, try again.")
            except Exception:
                pass
            print("[error] deliver failed for {0}".format(vid))
            traceback.print_exc()
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


# ---------- handlers ----------

@client.on(events.NewMessage(pattern=r"/(start|search)"))
async def on_start(event):
    set_session(event.sender_id, {"state": "await_kw"})
    await event.respond("Type your keywords or tags:")


@client.on(events.NewMessage(
    func=lambda e: e.is_private and (e.text or "") and not e.text.startswith("/")))
async def on_text(event):
    s = get_session(event.sender_id)
    if not s or s.get("state") != "await_kw":
        return
    s["query"] = event.text.strip()
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

    if kind == "sort":
        if len(parts) != 2:
            return
        s = get_session(uid)
        if not s or "query" not in s:
            await event.answer("Type /search first.", alert=True)
            return
        await event.answer("Searching...")
        results = await asyncio.to_thread(xh.search, s["query"], parts[1], 60)
        s["sid"] = secrets.token_hex(3)      # ticket for this search
        s["results"] = results
        s["page"] = 0
        s["msg_ids"] = []
        s["state"] = "browsing"
        if not results:
            await event.respond("No results.")
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

async def run():
    load_cache()
    await client.start(bot_token=BOT_TOKEN)
    print("Bot running.")
    await asyncio.sleep(RUN_HOURS * 3600)
    await client.disconnect()


if __name__ == "__main__":
    client.loop.run_until_complete(run())
