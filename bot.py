import os
import asyncio
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan! Cek file .env atau environment variable.")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── Queue per chat ───────────────────────────────────────────────
queues = {}
now_playing = {}

def get_queue(chat_id):
    if chat_id not in queues:
        queues[chat_id] = []
    return queues[chat_id]

# ─── YDL base opts ────────────────────────────────────────────────
def get_ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "noplaylist": True,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts

# ─── Download audio ───────────────────────────────────────────────
def search_and_get_info(query: str) -> dict:
    ydl_opts = get_ydl_opts({
        "default_search": "ytsearch1",
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        if not info:
            raise Exception("Lagu tidak ditemukan")
        return {
            "title": info.get("title", "Unknown"),
            "url": info.get("webpage_url"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
        }

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
def fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

# ─── Commands ─────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Music Bot*\n\n"
        "Perintah yang tersedia:\n"
        "/play `<judul lagu>` — putar lagu\n"
        "/queue — lihat antrian\n"
        "/skip — skip lagu sekarang\n"
        "/clear — hapus antrian\n"
        "/nowplaying — info lagu sekarang\n\n"
        "Di grup gunakan /play untuk memutar lagu.",
        parse_mode="Markdown"
    )

async def play_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❗ Contoh: `/play shape of you`", parse_mode="Markdown")
        return

    query = " ".join(ctx.args)
    chat_id = update.effective_chat.id

    msg = await update.message.reply_text(f"🔍 Mencari: *{query}*...", parse_mode="Markdown")

    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, search_and_get_info, query
        )
    except Exception as e:
        await msg.edit_text(f"❌ Gagal mencari lagu: {e}")
        return

    queue = get_queue(chat_id)
    queue.append(info)

    keyboard = [[
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}"),
        InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
    ]]

    await msg.edit_text(
        f"✅ Ditambahkan ke antrian!\n\n"
        f"🎵 *{info['title']}*\n"
        f"👤 {info['uploader']}\n"
        f"⏱ {fmt_duration(info['duration'])}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    if len(queue) == 1 and chat_id not in now_playing:
        await play_next(chat_id, update, ctx)

async def play_next(chat_id: int, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    queue = get_queue(chat_id)
    if not queue:
        now_playing.pop(chat_id, None)
        await ctx.bot.send_message(chat_id, "✅ Antrian habis.")
        return

    track = queue.pop(0)
    now_playing[chat_id] = track

    filename = f"{chat_id}_{track['title'][:30].replace(' ', '_')}"

    await ctx.bot.send_message(
        chat_id,
        f"▶️ *Now Playing*\n\n"
        f"🎵 *{track['title']}*\n"
        f"👤 {track['uploader']}\n"
        f"⏱ {fmt_duration(track['duration'])}",
        parse_mode="Markdown"
    )

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(
            None, download_audio, track["url"], filename
        )

        with open(file_path, "rb") as audio:
            await ctx.bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=track["title"],
                performer=track["uploader"],
                duration=track["duration"],
            )

        os.remove(file_path)

    except Exception as e:
        await ctx.bot.send_message(chat_id, f"❌ Error saat memutar: {e}")

    await play_next(chat_id, update, ctx)

async def skip_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in now_playing:
        await update.message.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return
    await update.message.reply_text("⏭ Skip!")
    now_playing.pop(chat_id, None)
    await play_next(chat_id, update, ctx)

async def queue_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    queue = get_queue(chat_id)

    if not queue and chat_id not in now_playing:
        await update.message.reply_text("📋 Antrian kosong.")
        return

    text = "📋 *Antrian Lagu*\n\n"

    if chat_id in now_playing:
        np = now_playing[chat_id]
        text += f"▶️ *{np['title']}* — {fmt_duration(np['duration'])}\n\n"

    for i, track in enumerate(queue, 1):
        text += f"{i}. {track['title']} — {fmt_duration(track['duration'])}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def clear_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    queues[chat_id] = []
    now_playing.pop(chat_id, None)
    await update.message.reply_text("🗑 Antrian dihapus.")

async def nowplaying_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in now_playing:
        await update.message.reply_text("❗ Tidak ada lagu yang sedang diputar.")
        return
    np = now_playing[chat_id]
    await update.message.reply_text(
        f"▶️ *Now Playing*\n\n"
        f"🎵 *{np['title']}*\n"
        f"👤 {np['uploader']}\n"
        f"⏱ {fmt_duration(np['duration'])}",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_type = update.message.chat.type
    if chat_type in ["group", "supergroup"]:
        return
    ctx.args = update.message.text.split()
    await play_command(update, ctx)

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("skip_"):
        chat_id = int(data.split("_")[1])
        now_playing.pop(chat_id, None)
        await query.edit_message_text("⏭ Diskip!")
        await play_next(chat_id, update, ctx)

    elif data.startswith("queue_"):
        chat_id = int(data.split("_")[1])
        queue = get_queue(chat_id)
        if not queue:
            await query.answer("📋 Antrian kosong.", show_alert=True)
        else:
            text = "\n".join([f"{i+1}. {t['title']}" for i, t in enumerate(queue)])
            await query.answer(f"📋 Antrian:\n{text}", show_alert=True)

# ─── Main ─────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("nowplaying", nowplaying_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    PORT = int(os.environ.get("PORT", 8080))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    print("Bot jalan...")

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
        )
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
