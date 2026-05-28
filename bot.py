import os
import glob
import asyncio
import subprocess
import aiohttp
import yt_dlp
from dotenv import load_dotenv
from aiohttp import web

from hydrogram import Client, filters
from hydrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from hydrogram.errors import FloodWait

from pytgcalls import PyTgCalls, idle
from pytgcalls import filters as tg_filters
from pytgcalls.types import MediaStream, StreamEnded
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

# ─── Load ENV ────────────────────────────────────────────────────
load_dotenv()

API_ID    = int(os.getenv("API_ID", 0))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("❌ API_ID, API_HASH, atau BOT_TOKEN tidak ditemukan di environment!")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

print("🚀 [1/5] ENV loaded & dirs created...")

# ─── Hapus session lama ───────────────────────────────────────────
for f in glob.glob("*.session") + glob.glob("*.session-journal"):
    try:
        os.remove(f)
        print(f"🗑 Hapus session lama: {f}")
    except Exception:
        pass

print("🚀 [2/5] Old sessions cleared...")

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
queues: dict[int, list] = {}
now_playing: dict[int, dict] = {}

def get_queue(chat_id: int) -> list:
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
def get_ydl_opts(extra: dict = None) -> dict:
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
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
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

# ─── Cleanup file ────────────────────────────────────────────────
def cleanup_file(fp: str):
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
            print(f"🗑 File dihapus: {fp}")
        except Exception as e:
            print(f"⚠️ Gagal hapus file: {e}")

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
        except Exception as e:
            print(f"⚠️ Gagal kirim pesan habis antrian: {e}")
        return

    track = queue.pop(0)
    now_playing[chat_id] = track

    safe_title = "".join(
        c for c in track["title"][:30] if c.isalnum() or c in " _-"
    ).replace(" ", "_")
    filename = f"{chat_id}_{safe_title}"

    try:
        await app.send_message(
            chat_id,
            f"▶️ **Now Playing**\n\n"
            f"🎵 **{track['title']}**\n"
            f"👤 {track['uploader']}\n"
            f"⏱ {fmt_duration(track['duration'])}\n"
            f"🔗 {track['url']}",
        )
    except Exception as e:
        print(f"⚠️ Gagal kirim now playing: {e}")

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(
            None, download_audio, track["url"], filename
        )
        await calls.play(chat_id, MediaStream(file_path))
        now_playing[chat_id]["file_path"] = file_path
        print(f"▶️ Playing: {track['title']} di chat {chat_id}")
    except Exception as e:
        print(f"❌ Error play: {e}")
        try:
            await app.send_message(chat_id, f"❌ Error memutar lagu: `{e}`\nSkip ke lagu berikutnya...")
        except Exception:
            pass
        await play_next(chat_id)

# ─── Stream selesai ──────────────────────────────────────────────
@calls.on_update(tg_filters.stream_end)
async def on_stream_end(client, update: StreamEnded):
    chat_id = update.chat_id
    print(f"🔔 Stream ended di chat {chat_id}")
    track = now_playing.pop(chat_id, {})
    cleanup_file(track.get("file_path"))
    await play_next(chat_id)

# ═══════════════════════════════════════════════════════════════════
# HANDLERS — SEMUA DIDAFTARKAN DI SINI, catch_all PALING BAWAH
# ═══════════════════════════════════════════════════════════════════

# ─── /start ──────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(_, msg: Message):
    print(f"📩 /start dari {msg.from_user.id if msg.from_user else '?'} di chat {msg.chat.id}")
    await msg.reply_text(
        "🎵 **Music Bot — Voice Chat**\n\n"
        "Kirim perintah berikut di **grup** dengan Voice Chat aktif:\n\n"
        "▶️ `/play <judul lagu>` — cari & putar lagu\n"
        "⏭ `/skip` — skip lagu sekarang\n"
        "📋 `/queue` — lihat antrian\n"
        "🗑 `/clear` — hapus antrian\n"
        "🎧 `/nowplaying` — info lagu sekarang\n"
        "⏹ `/stop` — stop & keluar voice chat\n\n"
        "Contoh: `/play despacito`\n\n"
        "⚠️ **Bot harus sudah join grup dan ada Voice Chat aktif.**",
    )

# ─── /play ───────────────────────────────────────────────────────
@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, msg: Message):
    query = " ".join(msg.command[1:]).strip()
    print(f"📩 /play '{query}' dari {msg.from_user.id if msg.from_user else '?'} di chat {msg.chat.id}")

    if not query:
        await msg.reply_text(
            "❗ Gunakan: `/play <judul lagu>`\nContoh: `/play despacito`"
        )
        return

    chat_id = msg.chat.id
    status_msg = await msg.reply_text(f"🔍 Mencari: **{query}**...")

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, search_and_get_info, query)
        print(f"✅ Ditemukan: {info['title']}")
    except Exception as e:
        print(f"❌ Gagal cari: {e}")
        await status_msg.edit_text(f"❌ Gagal mencari lagu: `{e}`")
        return

    queue = get_queue(chat_id)
    queue.append(info)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}"),
        InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
    ]])

    await status_msg.edit_text(
        f"✅ **Ditambahkan ke antrian!**\n\n"
        f"🎵 **{info['title']}**\n"
        f"👤 {info['uploader']}\n"
        f"⏱ {fmt_duration(info['duration'])}\n"
        f"🔗 {info['url']}",
        reply_markup=keyboard,
    )

    # Mulai main kalau belum ada yang playing
    if chat_id not in now_playing:
        await play_next(chat_id)

# ─── /skip ───────────────────────────────────────────────────────
@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(_, msg: Message):
    chat_id = msg.chat.id
    print(f"📩 /skip di chat {chat_id}")

    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return

    track = now_playing.pop(chat_id, {})
    cleanup_file(track.get("file_path"))

    await msg.reply_text("⏭ Diskip!")

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

    text = "📋 **Antrian Lagu**\n\n"
    if chat_id in now_playing:
        np = now_playing[chat_id]
        text += f"▶️ **{np['title']}** — {fmt_duration(np['duration'])}\n\n"
    for i, track in enumerate(queue, 1):
        text += f"{i}. {track['title']} — {fmt_duration(track['duration'])}\n"

    await msg.reply_text(text)

# ─── /clear ──────────────────────────────────────────────────────
@app.on_message(filters.command("clear") & filters.group)
async def cmd_clear(_, msg: Message):
    chat_id = msg.chat.id
    print(f"📩 /clear di chat {chat_id}")
    queues[chat_id] = []
    track = now_playing.pop(chat_id, {})
    cleanup_file(track.get("file_path"))
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await msg.reply_text("🗑 Antrian dihapus.")

# ─── /stop ───────────────────────────────────────────────────────
@app.on_message(filters.command("stop") & filters.group)
async def cmd_stop(_, msg: Message):
    chat_id = msg.chat.id
    print(f"📩 /stop di chat {chat_id}")
    queues[chat_id] = []
    track = now_playing.pop(chat_id, {})
    cleanup_file(track.get("file_path"))
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
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
        f"▶️ **Now Playing**\n\n"
        f"🎵 **{np['title']}**\n"
        f"👤 {np['uploader']}\n"
        f"⏱ {fmt_duration(np['duration'])}\n"
        f"🔗 {np.get('url', '-')}",
    )

# ─── Callback buttons ────────────────────────────────────────────
@app.on_callback_query()
async def callback_handler(_, cq: CallbackQuery):
    data = cq.data
    print(f"📩 Callback: {data}")

    if data.startswith("skip_"):
        chat_id = int(data.split("_")[1])
        if chat_id not in now_playing:
            await cq.answer("Tidak ada lagu yang sedang diputar.", show_alert=True)
            return
        await cq.answer("⏭ Diskip!")
        track = now_playing.pop(chat_id, {})
        cleanup_file(track.get("file_path"))
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
            await cq.answer("📋 Antrian kosong.", show_alert=True)
        else:
            text = "\n".join([f"{i+1}. {t['title']}" for i, t in enumerate(queue)])
            await cq.answer(f"📋 Antrian:\n{text[:200]}", show_alert=True)

    else:
        await cq.answer()

# ─── DEBUG — catch all pesan (HARUS PALING BAWAH) ────────────────
@app.on_message(group=999)
async def catch_all(_, msg: Message):
    print(
        f"📨 MSG | chat={msg.chat.id} ({msg.chat.type}) "
        f"| from={msg.from_user.id if msg.from_user else 'N/A'} "
        f"| text={msg.text!r}"
    )

# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════

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


async def main():
    print("🚀 [5/5] Starting bot...")

    # Hapus webhook biar polling bisa jalan
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            async with session.get(url) as resp:
                data = await resp.json()
                print(f"🔧 deleteWebhook: {data.get('description', data)}")
    except Exception as e:
        print(f"⚠️ Gagal hapus webhook: {e}")

    await start_bot_with_retry()

    try:
        await calls.start()
        print("✅ PyTgCalls started")
    except Exception as e:
        print(f"❌ Gagal start PyTgCalls: {e}")
        raise

    # Health check endpoint (untuk Render/Railway/dll)
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
