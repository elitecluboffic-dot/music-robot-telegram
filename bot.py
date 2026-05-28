import os
import asyncio
import subprocess
import yt_dlp
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserAlreadyParticipant, ChatAdminRequired

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

load_dotenv()

API_ID   = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH, atau BOT_TOKEN tidak ditemukan!")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── Pyrogram bot client ──────────────────────────────────────────
app = Client(
    "music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ─── PyTgCalls ───────────────────────────────────────────────────
calls = PyTgCalls(app)

# ─── Queue & state ───────────────────────────────────────────────
queues    = {}
now_playing = {}

def get_queue(chat_id):
    if chat_id not in queues:
        queues[chat_id] = []
    return queues[chat_id]

# ─── Cari JS runtime ─────────────────────────────────────────────
def find_runtime():
    for binary in ["deno", "node", "bun"]:
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                w = subprocess.run(["which", binary], capture_output=True, text=True)
                path = w.stdout.strip() if w.returncode == 0 else binary
                print(f"✅ {binary} ditemukan: {path}")
                return binary, path
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["find", "/nix/store", "-name", "node", "-type", "f"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            candidates = [p for p in r.stdout.strip().split("\n") if "/bin/node" in p]
            if candidates:
                print(f"✅ node ditemukan di nix store: {candidates[0]}")
                return "node", candidates[0]
    except Exception:
        pass
    print("⚠️ JS Runtime tidak ditemukan.")
    return None, None

JS_RUNTIME_KEY, JS_RUNTIME_PATH = find_runtime()

# ─── YDL opts ────────────────────────────────────────────────────
def get_ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android"],
            }
        },
    }
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE
    if JS_RUNTIME_KEY and JS_RUNTIME_PATH:
        opts["js_runtimes"] = {JS_RUNTIME_KEY: {"path": JS_RUNTIME_PATH}}
    if extra:
        opts.update(extra)
    return opts

# ─── Search info ─────────────────────────────────────────────────
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

# ─── Download audio ───────────────────────────────────────────────
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

# ─── Play next di voice chat ──────────────────────────────────────
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
        f"▶️ *Now Playing*\n\n"
        f"🎵 *{track['title']}*\n"
        f"👤 {track['uploader']}\n"
        f"⏱ {fmt_duration(track['duration'])}",
        parse_mode="markdown",
    )

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(
            None, download_audio, track["url"], filename
        )

        await calls.play(
            chat_id,
            MediaStream(file_path),
        )

        # Hapus file setelah selesai (pantau via stream_end callback)
        now_playing[chat_id]["file_path"] = file_path

    except Exception as e:
        await app.send_message(chat_id, f"❌ Error: {e}")
        # Coba lanjut ke lagu berikutnya
        await play_next(chat_id)

# ─── Callback saat lagu selesai ──────────────────────────────────
@calls.on_stream_end()
async def on_stream_end(_, update):
    chat_id = update.chat_id
    # Hapus file lama
    track = now_playing.get(chat_id, {})
    fp = track.get("file_path")
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass
    await play_next(chat_id)

# ─── /start ──────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def start(_, msg: Message):
    await msg.reply_text(
        "🎵 *Music Bot — Voice Chat*\n\n"
        "Perintah:\n"
        "/play `<judul lagu>` — putar lagu di voice chat\n"
        "/skip — skip lagu sekarang\n"
        "/queue — lihat antrian\n"
        "/clear — hapus antrian\n"
        "/nowplaying — info lagu sekarang\n"
        "/stop — stop & keluar voice chat\n\n"
        "⚠️ Bot harus sudah join ke grup dan ada Voice Chat aktif.",
        parse_mode="markdown",
    )

# ─── /play ───────────────────────────────────────────────────────
@app.on_message(filters.command("play") & filters.group)
async def play_command(_, msg: Message):
    query = " ".join(msg.command[1:])
    if not query:
        await msg.reply_text("❗ Contoh: `/play shape of you`", parse_mode="markdown")
        return

    chat_id = msg.chat.id
    status_msg = await msg.reply_text(f"🔍 Mencari: *{query}*...", parse_mode="markdown")

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, search_and_get_info, query)
    except Exception as e:
        await status_msg.edit_text(f"❌ Gagal mencari lagu: {e}")
        return

    queue = get_queue(chat_id)
    queue.append(info)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}"),
        InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
    ]])

    await status_msg.edit_text(
        f"✅ Ditambahkan ke antrian!\n\n"
        f"🎵 *{info['title']}*\n"
        f"👤 {info['uploader']}\n"
        f"⏱ {fmt_duration(info['duration'])}",
        parse_mode="markdown",
        reply_markup=keyboard,
    )

    # Kalau tidak ada lagu yang sedang main, langsung play
    if chat_id not in now_playing:
        await play_next(chat_id)

# ─── /skip ───────────────────────────────────────────────────────
@app.on_message(filters.command("skip") & filters.group)
async def skip_command(_, msg: Message):
    chat_id = msg.chat.id
    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return
    await msg.reply_text("⏭ Skip!")
    track = now_playing.pop(chat_id, {})
    fp = track.get("file_path")
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await play_next(chat_id)

# ─── /queue ──────────────────────────────────────────────────────
@app.on_message(filters.command("queue") & filters.group)
async def queue_command(_, msg: Message):
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
async def clear_command(_, msg: Message):
    chat_id = msg.chat.id
    queues[chat_id] = []
    now_playing.pop(chat_id, None)
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await msg.reply_text("🗑 Antrian dihapus.")

# ─── /stop ───────────────────────────────────────────────────────
@app.on_message(filters.command("stop") & filters.group)
async def stop_command(_, msg: Message):
    chat_id = msg.chat.id
    queues[chat_id] = []
    now_playing.pop(chat_id, None)
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await msg.reply_text("⏹ Stop & keluar dari voice chat.")

# ─── /nowplaying ─────────────────────────────────────────────────
@app.on_message(filters.command("nowplaying") & filters.group)
async def nowplaying_command(_, msg: Message):
    chat_id = msg.chat.id
    if chat_id not in now_playing:
        await msg.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return
    np = now_playing[chat_id]
    await msg.reply_text(
        f"▶️ *Now Playing*\n\n"
        f"🎵 *{np['title']}*\n"
        f"👤 {np['uploader']}\n"
        f"⏱ {fmt_duration(np['duration'])}",
        parse_mode="markdown",
    )

# ─── Callback buttons ─────────────────────────────────────────────
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
            try:
                os.remove(fp)
            except Exception:
                pass
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
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

# ─── Main ─────────────────────────────────────────────────────────
async def main():
    await app.start()
    await calls.start()
    print("✅ Bot jalan dengan voice chat support!")
    await asyncio.get_event_loop().run_forever()

if __name__ == "__main__":
    asyncio.run(main())
