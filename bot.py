import os
import glob
import asyncio
import subprocess
import aiohttp
import yt_dlp
from dotenv import load_dotenv
from aiohttp import web

from hydrogram import Client, filters
from hydrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from hydrogram.errors import FloodWait

from pytgcalls import PyTgCalls, idle
from pytgcalls import filters as tg_filters
from pytgcalls.types import MediaStream, StreamEnded
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

load_dotenv()

print("🚀 [1/5] ENV loaded...")

API_ID    = int(os.getenv("API_ID", 0))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("❌ API_ID, API_HASH, atau BOT_TOKEN tidak ditemukan di environment!")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

print("🚀 [2/5] Dirs created...")

# ─── Hapus session lama ───────────────────────────────────────────
for f in glob.glob("*.session") + glob.glob("*.session-journal"):
    try:
        os.remove(f)
        print(f"🗑 Hapus session lama: {f}")
    except Exception:
        pass

# ─── Hydrogram bot client ─────────────────────────────────────────
app = Client(
    "bimrobot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ─── PyTgCalls ───────────────────────────────────────────────────
calls = PyTgCalls(app)

print("🚀 [3/5] Clients initialized...")

# ─── Queue & state ───────────────────────────────────────────────
queues      = {}
now_playing = {}

def get_queue(chat_id):
    if chat_id not in queues:
        queues[chat_id] = []
    return queues[chat_id]

# ─── Cari JS runtime ─────────────────────────────────────────────
def find_runtime():
    for binary in ["node", "deno", "bun"]:
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                print(f"✅ JS Runtime: {binary}")
                return binary, binary
        except Exception:
            pass
    print("⚠️  JS Runtime tidak ditemukan.")
    return None, None

_JS_RUNTIME_KEY  = None
_JS_RUNTIME_PATH = None

def _init_runtime():
    global _JS_RUNTIME_KEY, _JS_RUNTIME_PATH
    if _JS_RUNTIME_KEY is None:
        _JS_RUNTIME_KEY, _JS_RUNTIME_PATH = find_runtime()
    return _JS_RUNTIME_KEY, _JS_RUNTIME_PATH

print("🚀 [4/5] Helpers ready...")

# ─── YDL opts ────────────────────────────────────────────────────
def get_ydl_opts(extra=None):
    js_key, js_path = _init_runtime()
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
    if js_key and js_path:
        opts["js_runtimes"] = {js_key: {"path": js_path}}
    if extra:
        opts.update(extra)
    return opts

# ─── Search ──────────────────────────────────────────────────────
def search_and_get_info(query: str) -> dict:
    ydl_opts = get_ydl_opts({"default_search": "ytsearch1"})
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        if not info:
            raise Exception("Lagu tidak ditemukan")
        return {
            "title":    info.get("title", "Unknown"),
            "url":      info.get("webpage_url"),
            "thumbnail":info.get("thumbnail"),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
        }

# ─── Download ────────────────────────────────────────────────────
def download_audio(url: str, filename: str) -> str:
    output_path = os.path.join(DOWNLOAD_DIR, filename)
    ydl_opts = get_ydl_opts({
        "outtmpl": output_path + ".%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path + ".mp3"

# ─── Format durasi ────────────────────────────────────────────────
def fmt_duration(seconds) -> str:
    if not seconds:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ─── Play next ───────────────────────────────────────────────────
async def play_next(chat_id: int):
    queue = get_queue(chat_id)
    if not queue:
        now_playing.pop(chat_id, None)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await app.send_message(chat_id, "✅ Antrian habis, keluar dari voice chat.")
        return

    track = queue.pop(0)
    now_playing[chat_id] = track

    safe_title = "".join(c for c in track["title"][:30] if c.isalnum() or c in " _-").replace(" ", "_")
    filename = f"{chat_id}_{safe_title}"

    await app.send_message(
        chat_id,
        f"▶️ *Now Playing*\n\n🎵 *{track['title']}*\n👤 {track['uploader']}\n⏱ {fmt_duration(track['duration'])}\n🔗 {track['url']}",
        parse_mode="markdown",
    )

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(None, download_audio, track["url"], filename)
        await calls.play(chat_id, MediaStream(file_path))
        now_playing[chat_id]["file_path"] = file_path
        print(f"▶️ Playing: {track['title']} di chat {chat_id}")
    except Exception as e:
        print(f"❌ Error play: {e}")
        await app.send_message(chat_id, f"❌ Error memutar lagu: {e}")
        await play_next(chat_id)

# ─── Stream selesai ──────────────────────────────────────────────
@calls.on_update(tg_filters.stream_end)
async def on_stream_end(client, update: StreamEnded):
    chat_id = update.chat_id
    track = now_playing.get(chat_id, {})
    fp = track.get("file_path")
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass
    await play_next(chat_id)

# ─── CATCH ALL — debug semua pesan masuk ─────────────────────────
@app.on_message()
async def catch_all(_, msg: Message):
    print(f"📨 PESAN MASUK | chat={msg.chat.id} | type={msg.chat.type} | text={msg.text}")

# ─── /start ──────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(_, msg: Message):
    print(f"📩 /start dari {msg.from_user.id if msg.from_user else '?'}")
    await msg.reply_text(
        "🎵 *Music Bot — Voice Chat*\n\n"
        "Kirim perintah berikut di *grup* dengan Voice Chat aktif:\n\n"
        "▶️ `/play <judul lagu>` — cari & putar lagu\n"
        "⏭ `/skip` — skip lagu sekarang\n"
        "📋 `/queue` — lihat antrian\n"
        "🗑 `/clear` — hapus antrian\n"
        "🎧 `/nowplaying` — info lagu sekarang\n"
        "⏹ `/stop` — stop & keluar voice chat\n\n"
        "Contoh: `/play despacito`\n\n"
        "⚠️ *Bot harus sudah join grup dan ada Voice Chat aktif.*",
        parse_mode="markdown",
    )

# ─── /play ───────────────────────────────────────────────────────
@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, msg: Message):
    query = " ".join(msg.command[1:])
    print(f"📩 /play '{query}' dari chat {msg.chat.id}")
    if not query:
        await msg.reply_text("❗ Gunakan: `/play <judul lagu>`\nContoh: `/play despacito`", parse_mode="markdown")
        return
    chat_id = msg.chat.id
    status_msg = await msg.reply_text(f"🔍 Mencari: *{query}*...", parse_mode="markdown")
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, search_and_get_info, query)
        print(f"✅ Ditemukan: {info['title']}")
    except Exception as e:
        print(f"❌ Gagal cari: {e}")
        await status_msg.edit_text(f"❌ Gagal mencari lagu: {e}")
        return
    queue = get_queue(chat_id)
    queue.append(info)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}"),
        InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
    ]])
    await status_msg.edit_text(
        f"✅ *Ditambahkan ke antrian!*\n\n🎵 *{info['title']}*\n👤 {info['uploader']}\n⏱ {fmt_duration(info['duration'])}\n🔗 {info['url']}",
        parse_mode="markdown",
        reply_markup=keyboard,
    )
    if chat_id not in now_playing:
        await play_next(chat_id)

# ─── /skip ───────────────────────────────────────────────────────
@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(_, msg: Message):
    chat_id = msg.chat.id
    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return
    await msg.reply_text("⏭ Diskip!")
    track = now_playing.pop(chat_id, {})
    fp = track.get("file_path")
    if fp and os.path.exists(fp):
        try: os.remove(fp)
        except Exception: pass
    try: await calls.leave_call(chat_id)
    except Exception: pass
    await play_next(chat_id)

# ─── /queue ──────────────────────────────────────────────────────
@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, msg: Message):
    chat_id = msg.chat.id
    queue = get_queue(chat_id)
    if not queue and chat_id not in now_playing:
        await msg.reply_text("📋 Antrian kosong.")
        return
    text = "📋 *Antrian Lagu*\n\n"
    if chat_id in now_playing:
        np = now_playing[chat_id]
        text += f"▶️ *{np['title']}* — {fmt_duration(np['duration'])}\n\n"
    for i, track in enumerate(queue, 1):
        text += f"{i}. {track['title']} — {fmt_duration(track['duration'])}\n"
    await msg.reply_text(text, parse_mode="markdown")

# ─── /clear ──────────────────────────────────────────────────────
@app.on_message(filters.command("clear") & filters.group)
async def cmd_clear(_, msg: Message):
    chat_id = msg.chat.id
    queues[chat_id] = []
    now_playing.pop(chat_id, None)
    try: await calls.leave_call(chat_id)
    except Exception: pass
    await msg.reply_text("🗑 Antrian dihapus.")

# ─── /stop ───────────────────────────────────────────────────────
@app.on_message(filters.command("stop") & filters.group)
async def cmd_stop(_, msg: Message):
    chat_id = msg.chat.id
    queues[chat_id] = []
    now_playing.pop(chat_id, None)
    try: await calls.leave_call(chat_id)
    except Exception: pass
    await msg.reply_text("⏹ Stop & keluar dari voice chat.")

# ─── /nowplaying ─────────────────────────────────────────────────
@app.on_message(filters.command("nowplaying") & filters.group)
async def cmd_nowplaying(_, msg: Message):
    chat_id = msg.chat.id
    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return
    np = now_playing[chat_id]
    await msg.reply_text(
        f"▶️ *Now Playing*\n\n🎵 *{np['title']}*\n👤 {np['uploader']}\n⏱ {fmt_duration(np['duration'])}\n🔗 {np.get('url', '-')}",
        parse_mode="markdown",
    )

# ─── Callback buttons ────────────────────────────────────────────
@app.on_callback_query()
async def callback_handler(_, cq):
    data = cq.data
    await cq.answer()
    if data.startswith("skip_"):
        chat_id = int(data.split("_")[1])
        if chat_id not in now_playing:
            await cq.answer("Tidak ada lagu.", show_alert=True)
            return
        track = now_playing.pop(chat_id, {})
        fp = track.get("file_path")
        if fp and os.path.exists(fp):
            try: os.remove(fp)
            except Exception: pass
        try: await calls.leave_call(chat_id)
        except Exception: pass
        await cq.edit_message_text("⏭ Diskip!")
        await play_next(chat_id)
    elif data.startswith("queue_"):
        chat_id = int(data.split("_")[1])
        queue = get_queue(chat_id)
        if not queue:
            await cq.answer("📋 Antrian kosong.", show_alert=True)
        else:
            text = "\n".join([f"{i+1}. {t['title']}" for i, t in enumerate(queue)])
            await cq.answer(f"📋 Antrian:\n{text}", show_alert=True)

# ─── Start dengan FloodWait retry ────────────────────────────────
async def start_bot_with_retry():
    for attempt in range(5):
        try:
            await app.start()
            me = await app.get_me()
            print(f"✅ Bot login sebagai @{me.username} (id={me.id})")
            return
        except FloodWait as e:
            wait = e.value + 5
            print(f"⏳ FloodWait: nunggu {wait} detik... (attempt {attempt+1}/5)")
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"❌ Gagal start: {e}")
            raise
    raise Exception("❌ Gagal login setelah 5x retry")

# ─── Main ────────────────────────────────────────────────────────
async def main():
    print("🚀 [5/5] Starting bot...")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            async with session.get(url) as resp:
                data = await resp.json()
                print(f"🔧 deleteWebhook: {data.get('description', data)}")
    except Exception as e:
        print(f"⚠️  Gagal hapus webhook: {e}")

    await start_bot_with_retry()

    try:
        await calls.start()
        print("✅ PyTgCalls started")
    except Exception as e:
        print(f"❌ Gagal start PyTgCalls: {e}")
        raise

    async def health(request):
        return web.Response(text="OK")

    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Health check running on port {port}")
    print("✅ Bot siap! Kirim /start ke bot kamu di Telegram.")

    await idle()

if __name__ == "__main__":
    asyncio.run(main())
