import os
import re
import json
import html
import asyncio

# Python 3.12+ no longer auto-creates an event loop; pyrogram needs one at import time.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from aiohttp import web
from pyrogram import Client, filters, enums, idle
from pyrogram.errors import FloodWait, MessageNotModified, MessageIdInvalid

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").replace(" ", "").split(",") if x]
MONGO_URI = os.environ.get("MONGO_URI", "")
PORT = int(os.environ.get("PORT", 10000))
DATA_FILE = "data.json"
BACKUP_FILE = "backups.json"
EDIT_DELAY = 1.2

app = Client(
    "caption_cleaner",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    parse_mode=enums.ParseMode.HTML,
)

admin = filters.private & filters.user(ADMINS)


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self):
        self.data = {
            "channels": [], "texts": [], "mode": "line", "autoclean": True,
            "backup": True, "peers": {},
        }
        self.col = None
        self.bcol = None   # MongoDB collection for original-caption backups
        self.bk = {}       # local-file backups: "chat:msgid" -> {chat, mid, kind, html}

    async def load(self):
        if MONGO_URI:
            from motor.motor_asyncio import AsyncIOMotorClient
            db = AsyncIOMotorClient(MONGO_URI)["caption_cleaner"]
            self.col = db["settings"]
            self.bcol = db["backups"]
            doc = await self.col.find_one({"_id": "cfg"})
            if doc:
                doc.pop("_id", None)
                self.data.update(doc)
        else:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            if os.path.exists(BACKUP_FILE):
                with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                    self.bk = json.load(f)

    # ---- original caption/text backups (used by /restore) ----
    async def bk_add(self, chat, mid, kind, html_text) -> bool:
        """Save the ORIGINAL text of a post. Keeps the first one only. True if newly saved."""
        key = f"{chat}:{mid}"
        row = {"chat": chat, "mid": mid, "kind": kind, "html": html_text}
        if self.bcol is not None:
            r = await self.bcol.update_one({"_id": key}, {"$setOnInsert": row}, upsert=True)
            return r.upserted_id is not None
        if key in self.bk:
            return False
        self.bk[key] = row
        return True

    async def bk_del(self, chat, mid):
        key = f"{chat}:{mid}"
        if self.bcol is not None:
            await self.bcol.delete_one({"_id": key})
        else:
            self.bk.pop(key, None)

    async def bk_list(self, chat, start=1, end=None):
        if self.bcol is not None:
            q = {"chat": chat, "mid": {"$gte": start}}
            if end is not None:
                q["mid"]["$lte"] = end
            return [d async for d in self.bcol.find(q).sort("mid", 1)]
        rows = [r for r in self.bk.values()
                if r["chat"] == chat and r["mid"] >= start and (end is None or r["mid"] <= end)]
        return sorted(rows, key=lambda r: r["mid"])

    async def bk_counts(self) -> dict:
        if self.bcol is not None:
            out = {}
            async for d in self.bcol.aggregate([{"$group": {"_id": "$chat", "n": {"$sum": 1}}}]):
                out[d["_id"]] = d["n"]
            return out
        out = {}
        for r in self.bk.values():
            out[r["chat"]] = out.get(r["chat"], 0) + 1
        return out

    async def bk_clear(self, chat=None) -> int:
        if self.bcol is not None:
            r = await self.bcol.delete_many({} if chat is None else {"chat": chat})
            return r.deleted_count
        keys = [k for k, r in self.bk.items() if chat is None or r["chat"] == chat]
        for k in keys:
            del self.bk[k]
        self.bk_flush()
        return len(keys)

    def bk_flush(self):
        """Local-file mode only: write backups to disk."""
        if self.bcol is None:
            with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(self.bk, f, ensure_ascii=False)

    def bk_flush_safe(self):
        try:
            self.bk_flush()
        except Exception as e:
            print(f"⚠️ backup flush: {e}")

    async def save(self):
        if self.col is not None:
            await self.col.replace_one({"_id": "cfg"}, {"_id": "cfg", **self.data}, upsert=True)
        else:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)


store = Store()
jobs = {}  # chat_id -> {"stop": bool}


# ---------------------------------------------------------------- cleaning
INVISIBLE = "\u2060\u200b\u200c\u200d\ufeff"
INVIS_TABLE = dict.fromkeys(map(ord, INVISIBLE), None)
GAP = r"(?:<[^>]*>|[" + INVISIBLE + r"])*"


def norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).translate(INVIS_TABLE).casefold().strip()


def build_pattern(text: str):
    tokens = text.split()
    parts = []
    for tok in tokens:
        tok = html.escape(tok, quote=False)
        parts.append(GAP.join(re.escape(c) for c in tok))
    if not parts:
        return None
    return re.compile((GAP + r"\s+" + GAP).join(parts), re.IGNORECASE)


def clean_html(h: str) -> str:
    texts = store.data["texts"]
    if not texts:
        return h.strip()

    if store.data["mode"] == "line":
        keys = []
        for t in texts:
            for ln in t.split("\n"):
                k = norm(ln)
                if k:
                    keys.append(k)
        out = [ln for ln in h.split("\n") if not any(k in norm(ln) for k in keys)]
        res = "\n".join(out)
    else:
        res = h
        for t in texts:
            p = build_pattern(t)
            if p:
                res = p.sub("", res)

    res = re.sub(r"\n{3,}", "\n\n", res).strip()
    return res


async def process(msg) -> bool:
    """Edit one message if it contains any removal text. Returns True if edited."""
    if not store.data["texts"]:
        return False

    if msg.caption:
        orig = msg.caption.html
        new = clean_html(orig)
        if new != orig.strip():
            saved = await backup_original(msg, "caption", orig)
            try:
                await app.edit_message_caption(
                    msg.chat.id, msg.id, new,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=msg.reply_markup,
                )
            except Exception:
                if saved:
                    await store.bk_del(msg.chat.id, msg.id)
                raise
            return True
    elif msg.text:
        orig = msg.text.html
        new = clean_html(orig)
        if new != orig.strip() and new:
            saved = await backup_original(msg, "text", orig)
            try:
                await app.edit_message_text(
                    msg.chat.id, msg.id, new,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=msg.reply_markup,
                )
            except Exception:
                if saved:
                    await store.bk_del(msg.chat.id, msg.id)
                raise
            return True
    return False


async def backup_original(msg, kind, orig_html) -> bool:
    """Save original text before editing so /restore can bring it back."""
    if not store.data.get("backup", True):
        return False
    try:
        return await store.bk_add(msg.chat.id, msg.id, kind, orig_html)
    except Exception as e:
        print(f"⚠️ backup {msg.chat.id}/{msg.id}: {e}")
        return False


async def restore_one(msg, row) -> bool:
    """Put the original caption/text back. Returns True if restored (or already original)."""
    for _ in range(3):
        try:
            if row["kind"] == "caption":
                await app.edit_message_caption(
                    msg.chat.id, msg.id, row["html"],
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=msg.reply_markup,
                )
            else:
                await app.edit_message_text(
                    msg.chat.id, msg.id, row["html"],
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=msg.reply_markup,
                )
            return True
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except MessageNotModified:
            return True
        except MessageIdInvalid:
            return False
        except Exception as e:
            print(f"⚠️ restore {msg.chat.id}/{msg.id}: {e}")
            return False
    return False


async def safe_process(msg) -> bool:
    for _ in range(3):
        try:
            return await process(msg)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except (MessageNotModified, MessageIdInvalid):
            return False
        except Exception as e:
            print(f"⚠️ {msg.chat.id}/{msg.id}: {e}")
            return False
    return False


async def scan(chat_id: int, start: int, end, notify: int):
    job = jobs[chat_id] = {"stop": False}
    done = scanned = 0
    try:
        if end is None:
            tmp = await app.send_message(chat_id, ".")
            end = tmp.id - 1
            await tmp.delete()

        await app.send_message(notify, f"🚀 Clean start: <code>{chat_id}</code>\nRange: {start} → {end}")

        a = start
        while a <= end:
            if job["stop"]:
                await app.send_message(notify, f"🛑 Stopped at ID {a}. Edited: {done}")
                return
            ids = list(range(a, min(a + 100, end + 1)))
            try:
                msgs = await app.get_messages(chat_id, ids)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue
            except Exception as e:
                print(f"⚠️ get_messages {a}: {e}")
                a += 100
                continue

            for msg in msgs:
                if not msg or msg.empty:
                    continue
                scanned += 1
                if await safe_process(msg):
                    done += 1
                    print(f"✅ Edited {chat_id}/{msg.id}")
                    await asyncio.sleep(EDIT_DELAY)
            a += 100

            if (a // 100) % 10 == 0:
                try:
                    await app.send_message(notify, f"⏳ {min(a, end)}/{end} scanned, edited: {done}")
                except Exception:
                    pass

        await app.send_message(
            notify, f"🏁 Done <code>{chat_id}</code>\nScanned: {scanned}\nEdited: {done}"
        )
    except Exception as e:
        await app.send_message(notify, f"❌ Error <code>{chat_id}</code>: {html.escape(str(e))}")
    finally:
        jobs.pop(chat_id, None)
        store.bk_flush_safe()


async def restore_scan(chat_id: int, start: int, end, notify: int):
    """Bring back original captions/texts for posts that were cleaned earlier."""
    job = jobs[chat_id] = {"stop": False}
    done = failed = gone = 0
    try:
        rows = await store.bk_list(chat_id, start, end)
        if not rows:
            await app.send_message(
                notify, f"ℹ️ <code>{chat_id}</code>: is range me koi backup nahi mila (restore karne layak kuch nahi)."
            )
            return
        await app.send_message(
            notify, f"♻️ Restore start: <code>{chat_id}</code>\nPosts: {len(rows)}"
        )
        for i in range(0, len(rows), 100):
            if job["stop"]:
                await app.send_message(notify, f"🛑 Restore stopped. Restored: {done}")
                return
            chunk = rows[i:i + 100]
            ids = [r["mid"] for r in chunk]
            msgs = None
            for _ in range(3):
                try:
                    msgs = await app.get_messages(chat_id, ids)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    print(f"⚠️ restore get_messages {ids[0]}: {e}")
                    break
            if msgs is None:
                failed += len(chunk)
                continue
            by_id = {m.id: m for m in msgs if m and not m.empty}
            for r in chunk:
                msg = by_id.get(r["mid"])
                if msg is None:  # post delete ho chuka hai
                    await store.bk_del(chat_id, r["mid"])
                    gone += 1
                    continue
                if await restore_one(msg, r):
                    await store.bk_del(chat_id, r["mid"])
                    done += 1
                    print(f"♻️ Restored {chat_id}/{r['mid']}")
                    await asyncio.sleep(EDIT_DELAY)
                else:
                    failed += 1
            if ((i // 100) + 1) % 10 == 0:
                try:
                    await app.send_message(notify, f"⏳ Restored {done}/{len(rows)}")
                except Exception:
                    pass
        await app.send_message(
            notify,
            f"🏁 Restore done <code>{chat_id}</code>\nRestored: {done}\n"
            f"Deleted posts (skip): {gone}\nFailed: {failed}",
        )
    except Exception as e:
        await app.send_message(notify, f"❌ Restore error <code>{chat_id}</code>: {html.escape(str(e))}")
    finally:
        jobs.pop(chat_id, None)
        store.bk_flush_safe()


# ---------------------------------------------------------------- helpers
def arg(m) -> str:
    parts = m.text.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


HELP = (
    "<b>🧹 Caption Cleaner Bot</b>\n\n"
    "<b>Text manage</b>\n"
    "/addtext &lt;text&gt; - hatane wala text add karo (multi-line ok, ya kisi msg ko reply karke)\n"
    "/listtext - saved texts\n"
    "/deltext &lt;no&gt; - text delete\n"
    "/cleartext - sab texts delete\n"
    "/mode line|text - line: poori line hatao | text: sirf wahi text hatao\n\n"
    "<b>Channels</b>\n"
    "/addchannel &lt;id/@username&gt;\n"
    "/delchannel &lt;id&gt;\n"
    "/channels\n\n"
    "<b>Clean</b>\n"
    "/clean [channel_id] [start_id] [end_id] - purane posts clean\n"
    "/stop - cleaning/restore roko\n"
    "/autoclean on|off - naye posts auto clean\n"
    "/status - settings dekho\n\n"
    "<b>Repair (hataye hue caption wapas)</b>\n"
    "/restore [channel_id] [start_id] [end_id] - original caption wapas lagao\n"
    "/backup on|off - edit se pehle original save karna\n"
    "/backups - kitne posts restore ho sakte hain\n"
    "/clearbackup [channel_id] - backups delete karo"
)


# ---------------------------------------------------------------- admin commands
@app.on_message(admin & filters.command(["start", "help"]))
async def cmd_start(_, m):
    await m.reply(HELP)


@app.on_message(filters.private & ~filters.user(ADMINS) & filters.command("start"))
async def cmd_start_noadmin(_, m):
    await m.reply("🔒 Ye private bot hai.")


@app.on_message(admin & filters.command("addtext"))
async def cmd_addtext(_, m):
    t = arg(m)
    if not t and m.reply_to_message:
        t = str(m.reply_to_message.text or m.reply_to_message.caption or "")
    t = t.strip()
    if not t:
        return await m.reply("Use: <code>/addtext text</code> ya kisi message ko reply karke /addtext")
    if t in store.data["texts"]:
        return await m.reply("⚠️ Ye text pehle se added hai.")
    store.data["texts"].append(t)
    await store.save()
    await m.reply(f"✅ Added #{len(store.data['texts'])}:\n<code>{html.escape(t)}</code>")


@app.on_message(admin & filters.command("listtext"))
async def cmd_listtext(_, m):
    texts = store.data["texts"]
    if not texts:
        return await m.reply("Koi text add nahi hai. /addtext use karo.")
    out = "\n\n".join(f"<b>{i}.</b> <code>{html.escape(t)}</code>" for i, t in enumerate(texts, 1))
    await m.reply(out)


@app.on_message(admin & filters.command("deltext"))
async def cmd_deltext(_, m):
    a = arg(m).strip()
    if not a.isdigit() or not (1 <= int(a) <= len(store.data["texts"])):
        return await m.reply("Use: <code>/deltext 1</code> (number /listtext se dekho)")
    t = store.data["texts"].pop(int(a) - 1)
    await store.save()
    await m.reply(f"🗑 Deleted:\n<code>{html.escape(t)}</code>")


@app.on_message(admin & filters.command("cleartext"))
async def cmd_cleartext(_, m):
    store.data["texts"] = []
    await store.save()
    await m.reply("🗑 Saare texts delete ho gaye.")


@app.on_message(admin & filters.command("mode"))
async def cmd_mode(_, m):
    a = arg(m).strip().lower()
    if a not in ("line", "text"):
        return await m.reply(
            f"Current mode: <b>{store.data['mode']}</b>\n"
            "<code>/mode line</code> - jis line me text mile poori line hatao\n"
            "<code>/mode text</code> - sirf wahi text hatao"
        )
    store.data["mode"] = a
    await store.save()
    await m.reply(f"✅ Mode: <b>{a}</b>")


async def remember_peer(chat_id):
    """Persist channel access_hash so it survives restarts (session is in-memory)."""
    try:
        peer = await app.resolve_peer(chat_id)
        h = getattr(peer, "access_hash", None)
        if h is not None and store.data["peers"].get(str(chat_id)) != h:
            store.data["peers"][str(chat_id)] = h
            await store.save()
    except Exception as e:
        print(f"⚠️ remember_peer {chat_id}: {e}")


async def restore_peers():
    rows = [(int(cid), h, "channel", None, None) for cid, h in store.data.get("peers", {}).items()]
    if rows:
        await app.storage.update_peers(rows)


async def add_channel(m, chat):
    if chat.id in store.data["channels"]:
        return await m.reply("⚠️ Channel pehle se added hai.")
    store.data["channels"].append(chat.id)
    await store.save()
    await remember_peer(chat.id)
    await m.reply(f"✅ Added: <b>{html.escape(chat.title or '')}</b> (<code>{chat.id}</code>)")


@app.on_message(admin & filters.command("addchannel"))
async def cmd_addchannel(_, m):
    a = arg(m).strip()
    if not a:
        return await m.reply(
            "Use: <code>/addchannel -1001234567890</code> ya <code>@username</code>\n"
            "Ya channel ka koi post yahan <b>forward</b> kar do."
        )
    try:
        chat = await app.get_chat(int(a) if a.lstrip("-").isdigit() else a)
        member = await app.get_chat_member(chat.id, "me")
        if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
            return await m.reply("❌ Bot is channel me admin nahi hai. Pehle admin banao.")
    except Exception as e:
        return await m.reply(
            f"❌ Channel nahi mila / bot admin nahi: {html.escape(str(e))}\n\n"
            "Fix: bot ko channel me admin banao, phir us channel ka <b>koi post yahan forward kar do</b> "
            "(bot ko channel ki pehchaan wahin se milti hai)."
        )
    await add_channel(m, chat)


@app.on_message(admin & filters.forwarded)
async def fwd_addchannel(_, m):
    chat = m.forward_from_chat
    if not chat or chat.type != enums.ChatType.CHANNEL:
        return
    try:
        member = await app.get_chat_member(chat.id, "me")
        if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
            return await m.reply("❌ Bot is channel me admin nahi hai. Pehle admin banao.")
    except Exception as e:
        return await m.reply(f"❌ Bot admin nahi / channel access nahi: {html.escape(str(e))}")
    await add_channel(m, chat)


@app.on_message(admin & filters.command("delchannel"))
async def cmd_delchannel(_, m):
    a = arg(m).strip()
    if not a.lstrip("-").isdigit() or int(a) not in store.data["channels"]:
        return await m.reply("Use: <code>/delchannel -1001234567890</code>")
    store.data["channels"].remove(int(a))
    await store.save()
    await m.reply("🗑 Channel hata diya.")


@app.on_message(admin & filters.command("channels"))
async def cmd_channels(_, m):
    ch = store.data["channels"]
    if not ch:
        return await m.reply("Koi channel add nahi hai. /addchannel use karo.")
    await m.reply("\n".join(f"• <code>{c}</code>" for c in ch))


@app.on_message(admin & filters.command("autoclean"))
async def cmd_autoclean(_, m):
    a = arg(m).strip().lower()
    if a not in ("on", "off"):
        return await m.reply(f"Auto clean: <b>{'ON' if store.data['autoclean'] else 'OFF'}</b>\nUse: /autoclean on|off")
    store.data["autoclean"] = a == "on"
    await store.save()
    await m.reply(f"✅ Auto clean: <b>{a.upper()}</b>")


@app.on_message(admin & filters.command("clean"))
async def cmd_clean(_, m):
    if not store.data["texts"]:
        return await m.reply("❌ Pehle /addtext se hatane wala text add karo.")
    args = m.command[1:]
    targets = []
    if args and args[0].lstrip("-").isdigit() and len(args[0]) > 6:
        targets = [int(args[0])]
        args = args[1:]
    else:
        targets = list(store.data["channels"])
    if not targets:
        return await m.reply("❌ Koi channel nahi. /addchannel use karo ya <code>/clean -100xxxx</code>")

    start = int(args[0]) if len(args) > 0 and args[0].isdigit() else 1
    end = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    for t in targets:
        if t in jobs:
            await m.reply(f"⚠️ <code>{t}</code> par cleaning pehle se chal rahi hai.")
            continue

    async def runner():
        for t in targets:
            if t in jobs:
                continue
            await scan(t, start, end, m.chat.id)

    asyncio.create_task(runner())
    await m.reply("▶️ Cleaning shuru. Progress yahin aayega. Rokne ke liye /stop")


@app.on_message(admin & filters.command("restore"))
async def cmd_restore(_, m):
    args = m.command[1:]
    if args and args[0].lstrip("-").isdigit() and len(args[0]) > 6:
        targets = [int(args[0])]
        args = args[1:]
    else:
        targets = list(store.data["channels"])
    if not targets:
        return await m.reply("❌ Koi channel nahi. /addchannel use karo ya <code>/restore -100xxxx</code>")

    start = int(args[0]) if len(args) > 0 and args[0].isdigit() else 1
    end = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    async def runner():
        for t in targets:
            if t in jobs:
                await m.reply(f"⚠️ <code>{t}</code> par kaam pehle se chal raha hai.")
                continue
            await restore_scan(t, start, end, m.chat.id)

    asyncio.create_task(runner())
    await m.reply(
        "♻️ Restore shuru. Progress yahin aayega. Rokne ke liye /stop\n"
        "⚠️ Restore ke baad /clean ya autoclean dubara text hata dega, "
        "to zarurat ho to pehle /autoclean off karo."
    )


@app.on_message(admin & filters.command("backup"))
async def cmd_backup(_, m):
    a = arg(m).strip().lower()
    if a not in ("on", "off"):
        return await m.reply(
            f"Backup: <b>{'ON' if store.data.get('backup', True) else 'OFF'}</b>\nUse: /backup on|off"
        )
    store.data["backup"] = a == "on"
    await store.save()
    await m.reply(f"✅ Backup: <b>{a.upper()}</b>")


@app.on_message(admin & filters.command("backups"))
async def cmd_backups(_, m):
    counts = await store.bk_counts()
    if not counts:
        return await m.reply("Koi backup nahi hai (abhi restore karne layak kuch nahi).")
    await m.reply(
        "<b>♻️ Restore ho sakne wale posts</b>\n"
        + "\n".join(f"• <code>{c}</code>: {n}" for c, n in counts.items())
    )


@app.on_message(admin & filters.command("clearbackup"))
async def cmd_clearbackup(_, m):
    a = arg(m).strip()
    chat = int(a) if a.lstrip("-").isdigit() else None
    n = await store.bk_clear(chat)
    await m.reply(f"🗑 {n} backup delete hue." + ("" if chat else " (saare channels)"))


@app.on_message(admin & filters.command("stop"))
async def cmd_stop(_, m):
    if not jobs:
        return await m.reply("Koi cleaning nahi chal rahi.")
    for j in jobs.values():
        j["stop"] = True
    await m.reply("🛑 Stop kar raha hoon...")


@app.on_message(admin & filters.command("status"))
async def cmd_status(_, m):
    d = store.data
    await m.reply(
        "<b>📊 Status</b>\n"
        f"Texts: {len(d['texts'])}\n"
        f"Channels: {len(d['channels'])}\n"
        f"Mode: {d['mode']}\n"
        f"Auto clean: {'ON' if d['autoclean'] else 'OFF'}\n"
        f"Backup (restore ke liye): {'ON' if d.get('backup', True) else 'OFF'}\n"
        f"Running jobs: {len(jobs)}\n"
        f"Storage: {'MongoDB' if store.col is not None else 'local file (restart par reset ho sakta hai)'}"
    )


# ---------------------------------------------------------------- new posts auto clean
@app.on_message(filters.channel, group=5)
async def on_new_post(_, msg):
    if not store.data["autoclean"] or msg.chat.id not in store.data["channels"]:
        return
    await safe_process(msg)
    store.bk_flush_safe()


# ---------------------------------------------------------------- web server (Render)
async def health(_):
    return web.Response(text="Caption Cleaner Bot is running ✅")


async def main():
    await store.load()
    await app.start()
    await restore_peers()
    web_app = web.Application()
    web_app.add_routes([web.get("/", health)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print("Bot started ✅")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
