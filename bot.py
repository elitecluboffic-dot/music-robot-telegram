import os
import glob
import asyncio
import subprocess
import aiohttp
import yt_dlp
from dotenv import load_dotenv
from aiohttp import web

from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait

from pytgcalls import PyTgCalls
from pytgcalls import filters as tg_filters
from pytgcalls.types import MediaStream, StreamEnded
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

# ─── ENV ─────────────────────────────────────────────────────────
load_dotenv()

API_ID    = int(os.getenv("API_ID", 0))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("❌ API_ID, API_HASH, atau BOT_TOKEN tidak ditemukan!")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── Hapus session lama ──────────────────────────────────────────
for f in glob.glob("*.session") + glob.glob("*.session-journal"):
    try:
        os.remove(f)
        print(f"🗑 Hapus session: {f}")
    except Exception:
        pass

# ─── Client ──────────────────────────────────────────────────────
app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

calls = PyTgCalls(app)

# ─── State ───────────────────────────────────────────────────────
queues: dict     = {}
now_playing: dict = {}

def get_queue(chat_id):
    if chat_id not in queues:
        queues[chat_id] = []
    return queues[chat_id]

# ─── Helpers ─────────────────────────────────────────────────────
def fmt_duration(seconds) -> str:
    if not seconds:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def cleanup_file(fp):
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass

def find_runtime():
    for binary in ["node", "deno", "bun"]:
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                return binary, binary
        except Exception:
            pass
    return None, None

_JS_KEY = None
_JS_PATH = None

def get_ydl_opts(extra=None):
    global _JS_KEY, _JS_PATH
    if _JS_KEY is None:
        _JS_KEY, _JS_PATH = find_runtime()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
    }
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE
    if _JS_KEY:
        opts["js_runtimes"] = {_JS_KEY: {"path": _JS_PATH}}
    if extra:
        opts.update(extra)
    return opts

def search_and_get_info(query: str) -> dict:
    with yt_dlp.YoutubeDL(get_ydl_opts({"default_search": "ytsearch1"})) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        if not info:
            raise Exception("Lagu tidak ditemukan")
        return {
            "title":    info.get("title", "Unknown"),
            "url":      info.get("webpage_url"),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
        }

def download_audio(url: str, filename: str) -> str:
    output_path = os.path.join(DOWNLOAD_DIR, filename)
    with yt_dlp.YoutubeDL(get_ydl_opts({
        "outtmpl": output_path + ".%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
    })) as ydl:
        ydl.download([url])
    return output_path + ".mp3"

# ─── Play next ───────────────────────────────────────────────────
async def play_next(chat_id: int):
    queue = get_queue(chat_id)
    if not queue:
        now_playing.pop(chat_id, None)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        try:
            await app.send_message(chat_id, "✅ Antrian habis, keluar dari voice chat.")
        except Exception:
            pass
        return

    track = queue.pop(0)
    now_playing[chat_id] = track

    safe = "".join(c for c in track["title"][:30] if c.isalnum() or c in " _-").replace(" ", "_")
    filename = f"{chat_id}_{safe}"

    try:
        await app.send_message(
            chat_id,
            f"▶️ **Now Playing**\n\n🎵 **{track['title']}**\n👤 {track['uploader']}\n⏱ {fmt_duration(track['duration'])}\n🔗 {track['url']}",
        )
    except Exception as e:
        print(f"⚠️ send_message error: {e}")

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(
            None, download_audio, track["url"], filename
        )
        await calls.play(chat_id, MediaStream(file_path))
        now_playing[chat_id]["file_path"] = file_path
        print(f"▶️ Playing: {track['title']} @ {chat_id}")
    except Exception as e:
        print(f"❌ play error: {e}")
        try:
            await app.send_message(chat_id, f"❌ Error: {e}\nSkip...")
        except Exception:
            pass
        await play_next(chat_id)

# ─── Stream end ──────────────────────────────────────────────────
@calls.on_update(tg_filters.stream_end)
async def on_stream_end(_, update: StreamEnded):
    chat_id = update.chat_id
    print(f"🔔 Stream ended @ {chat_id}")
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    await play_next(chat_id)

# ─── /start ──────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(_, msg: Message):
    print(f"✅ /start dari {msg.from_user.id if msg.from_user else '?'}")
    await msg.reply_text(
        "🎵 **Music Bot**\n\n"
        "Commands di grup dengan Voice Chat aktif:\n"
        "▶️ `/play <lagu>` — putar lagu\n"
        "⏭ `/skip` — skip\n"
        "📋 `/queue` — antrian\n"
        "🗑 `/clear` — hapus antrian\n"
        "🎧 `/nowplaying` — lagu sekarang\n"
        "⏹ `/stop` — stop & keluar\n\n"
        "Contoh: `/play despacito`"
    )

# ─── /play ───────────────────────────────────────────────────────
@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, msg: Message):
    query = " ".join(msg.command[1:]).strip()
    print(f"✅ /play '{query}' @ {msg.chat.id}")
    if not query:
        await msg.reply_text("❗ Contoh: `/play despacito`")
        return

    chat_id = msg.chat.id
    status = await msg.reply_text(f"🔍 Mencari **{query}**...")
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, search_and_get_info, query)
        print(f"✅ Found: {info['title']}")
    except Exception as e:
        print(f"❌ search error: {e}")
        await status.edit_text(f"❌ Gagal cari: {e}")
        return

    get_queue(chat_id).append(info)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}"),
        InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
    ]])
    await status.edit_text(
        f"✅ **Ditambahkan!**\n\n🎵 **{info['title']}**\n👤 {info['uploader']}\n⏱ {fmt_duration(info['duration'])}",
        reply_markup=kb,
    )

    if chat_id not in now_playing:
        await play_next(chat_id)

# ─── /skip ───────────────────────────────────────────────────────
@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(_, msg: Message):
    chat_id = msg.chat.id
    print(f"✅ /skip @ {chat_id}")
    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu.")
        return
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    await msg.reply_text("⏭ Skip!")
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await play_next(chat_id)

# ─── /queue ──────────────────────────────────────────────────────
@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, msg: Message):
    chat_id = msg.chat.id
    queue = get_queue(chat_id)
    if not queue and chat_id not in now_playing:
        await msg.reply_text("📋 Antrian kosong.")
        return
    text = "📋 **Antrian**\n\n"
    if chat_id in now_playing:
        np = now_playing[chat_id]
        text += f"▶️ **{np['title']}** — {fmt_duration(np['duration'])}\n\n"
    for i, t in enumerate(queue, 1):
        text += f"{i}. {t['title']} — {fmt_duration(t['duration'])}\n"
    await msg.reply_text(text)

# ─── /clear ──────────────────────────────────────────────────────
@app.on_message(filters.command("clear") & filters.group)
async def cmd_clear(_, msg: Message):
    chat_id = msg.chat.id
    queues[chat_id] = []
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await msg.reply_text("🗑 Antrian dihapus.")

# ─── /stop ───────────────────────────────────────────────────────
@app.on_message(filters.command("stop") & filters.group)
async def cmd_stop(_, msg: Message):
    chat_id = msg.chat.id
    queues[chat_id] = []
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await msg.reply_text("⏹ Stop.")

# ─── /nowplaying ─────────────────────────────────────────────────
@app.on_message(filters.command("nowplaying") & filters.group)
async def cmd_nowplaying(_, msg: Message):
    chat_id = msg.chat.id
    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu.")
        return
    np = now_playing[chat_id]
    await msg.reply_text(
        f"▶️ **Now Playing**\n\n🎵 **{np['title']}**\n👤 {np['uploader']}\n⏱ {fmt_duration(np['duration'])}\n🔗 {np.get('url', '-')}"
    )

# ─── Callbacks ───────────────────────────────────────────────────
@app.on_callback_query()
async def cb_handler(_, cq: CallbackQuery):
    data = cq.data
    print(f"✅ Callback: {data}")
    if data.startswith("skip_"):
        chat_id = int(data.split("_")[1])
        if chat_id not in now_playing:
            await cq.answer("Tidak ada lagu.", show_alert=True)
            return
        await cq.answer("⏭ Skip!")
        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        try:
            await cq.edit_message_text("⏭ Diskip!")
        except Exception:
            pass
        await play_next(chat_id)
    elif data.startswith("queue_"):
        chat_id = int(data.split("_")[1])
        queue = get_queue(chat_id)
        if not queue:
            await cq.answer("Antrian kosong.", show_alert=True)
        else:
            text = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(queue))
            await cq.answer(f"📋 Antrian:\n{text[:200]}", show_alert=True)
    else:
        await cq.answer()

# ─── catch_all DEBUG ─────────────────────────────────────────────
@app.on_message(group=999)
async def catch_all(_, msg: Message):
    print(f"📨 MSG chat={msg.chat.id} type={msg.chat.type} text={msg.text!r}")

# ─── MAIN ────────────────────────────────────────────────────────
async def main():
    print("🚀 Starting...")

    # Delete webhook
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            async with session.get(url) as r:
                d = await r.json()
                print(f"🔧 deleteWebhook: {d.get('description', d)}")
    except Exception as e:
        print(f"⚠️ deleteWebhook error: {e}")

    # Start Pyrogram
    for attempt in range(5):
        try:
            await app.start()
            me = await app.get_me()
            print(f"✅ Login sebagai @{me.username} (id={me.id})")
            break
        except FloodWait as e:
            wait = e.value + 5
            print(f"⏳ FloodWait {wait}s... attempt {attempt+1}/5")
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"❌ start error: {e}")
            raise
    else:
        raise Exception("❌ Gagal login 5x")

    # Start PyTgCalls
    try:
        await calls.start()
        print("✅ PyTgCalls started")
    except Exception as e:
        print(f"❌ PyTgCalls error: {e}")
        raise

    # Health check
    async def health(request):
        return web.Response(text="OK")
    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Health check port {port}")

    print("✅ Bot ready! Semua handler aktif.")

    # Pyrogram idle — ini yang bener, bukan pytgcalls idle
    await idle()

    print("🛑 Shutting down...")
    await calls.stop()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
