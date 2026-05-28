import os
import glob
import asyncio
import subprocess
import aiohttp
import yt_dlp
from dotenv import load_dotenv
from aiohttp import web

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError

from pytgcalls import PyTgCalls
from pytgcalls import filters as tg_filters
from pytgcalls.types import MediaStream, StreamEnded

load_dotenv()

API_ID    = int(os.getenv("API_ID", 0))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH, BOT_TOKEN tidak ada!")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

for f in glob.glob("*.session") + glob.glob("*.session-journal"):
    try:
        os.remove(f)
    except Exception:
        pass

# Telethon bot client
bot = TelegramClient("bot_session", API_ID, API_HASH)

calls = PyTgCalls(bot)

queues: dict = {}
now_playing: dict = {}

def get_queue(chat_id):
    if chat_id not in queues:
        queues[chat_id] = []
    return queues[chat_id]

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

_JS_KEY = None
_JS_PATH = None

def get_ydl_opts(extra=None):
    global _JS_KEY, _JS_PATH
    if _JS_KEY is None:
        for binary in ["node", "deno", "bun"]:
            try:
                r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3)
                if r.returncode == 0:
                    _JS_KEY, _JS_PATH = binary, binary
                    break
            except Exception:
                pass
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
            raise Exception("Tidak ditemukan")
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

async def play_next(chat_id: int):
    queue = get_queue(chat_id)
    if not queue:
        now_playing.pop(chat_id, None)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        try:
            await bot.send_message(chat_id, "✅ Antrian habis.")
        except Exception:
            pass
        return

    track = queue.pop(0)
    now_playing[chat_id] = track
    safe = "".join(c for c in track["title"][:30] if c.isalnum() or c in " _-").replace(" ", "_")

    try:
        await bot.send_message(
            chat_id,
            f"▶️ **Now Playing**\n\n🎵 **{track['title']}**\n"
            f"👤 {track['uploader']}\n"
            f"⏱ {fmt_duration(track['duration'])}\n"
            f"🔗 {track['url']}",
        )
    except Exception as e:
        print(f"send_message error: {e}")

    try:
        file_path = await asyncio.get_event_loop().run_in_executor(
            None, download_audio, track["url"], f"{chat_id}_{safe}"
        )
        await calls.play(chat_id, MediaStream(file_path))
        now_playing[chat_id]["file_path"] = file_path
        print(f"▶️ Playing {track['title']} @ {chat_id}")
    except Exception as e:
        print(f"play error: {e}")
        try:
            await bot.send_message(chat_id, f"❌ Error: {e}")
        except Exception:
            pass
        await play_next(chat_id)

@calls.on_update(tg_filters.stream_end)
async def on_stream_end(_, update: StreamEnded):
    chat_id = update.chat_id
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    await play_next(chat_id)

# ─── HANDLERS ────────────────────────────────────────────────────

@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    print(f"✅ /start dari {event.sender_id}")
    await event.respond(
        "🎵 **Music Bot**\n\n"
        "Commands di grup dengan Voice Chat aktif:\n"
        "▶️ `/play <lagu>` — putar\n"
        "⏭ `/skip` — skip\n"
        "📋 `/queue` — antrian\n"
        "⏹ `/stop` — stop\n"
        "🎧 `/nowplaying` — lagu sekarang\n\n"
        "Contoh: `/play despacito`"
    )

@bot.on(events.NewMessage(pattern=r"^/play(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_group))
async def cmd_play(event):
    query = event.pattern_match.group(1)
    if query:
        query = query.strip()
    print(f"✅ /play '{query}' @ {event.chat_id}")
    if not query:
        await event.respond("❗ Contoh: `/play despacito`")
        return
    chat_id = event.chat_id
    status = await event.respond(f"🔍 Mencari **{query}**...")
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, search_and_get_info, query)
        print(f"✅ Found: {info['title']}")
    except Exception as e:
        await status.edit(f"❌ Gagal: {e}")
        return
    get_queue(chat_id).append(info)
    buttons = [
        [Button.inline("⏭ Skip", data=f"skip_{chat_id}"),
         Button.inline("📋 Queue", data=f"queue_{chat_id}")]
    ]
    await status.edit(
        f"✅ **Ditambahkan!**\n\n🎵 **{info['title']}**\n"
        f"👤 {info['uploader']}\n"
        f"⏱ {fmt_duration(info['duration'])}",
        buttons=buttons,
    )
    if chat_id not in now_playing:
        await play_next(chat_id)

@bot.on(events.NewMessage(pattern=r"^/skip(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_skip(event):
    chat_id = event.chat_id
    print(f"✅ /skip @ {chat_id}")
    if chat_id not in now_playing:
        await event.respond("❗ Tidak ada lagu.")
        return
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    await event.respond("⏭ Skip!")
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await play_next(chat_id)

@bot.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_queue(event):
    chat_id = event.chat_id
    queue = get_queue(chat_id)
    if not queue and chat_id not in now_playing:
        await event.respond("📋 Antrian kosong.")
        return
    text = "📋 **Antrian**\n\n"
    if chat_id in now_playing:
        np = now_playing[chat_id]
        text += f"▶️ **{np['title']}** — {fmt_duration(np['duration'])}\n\n"
    for i, t in enumerate(queue, 1):
        text += f"{i}. {t['title']} — {fmt_duration(t['duration'])}\n"
    await event.respond(text)

@bot.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_stop(event):
    chat_id = event.chat_id
    print(f"✅ /stop @ {chat_id}")
    queues[chat_id] = []
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await event.respond("⏹ Stop.")

@bot.on(events.NewMessage(pattern=r"^/nowplaying(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_nowplaying(event):
    chat_id = event.chat_id
    if chat_id not in now_playing:
        await event.respond("❗ Tidak ada lagu.")
        return
    np = now_playing[chat_id]
    await event.respond(
        f"▶️ **Now Playing**\n\n🎵 **{np['title']}**\n"
        f"👤 {np['uploader']}\n"
        f"⏱ {fmt_duration(np['duration'])}\n"
        f"🔗 {np.get('url', '-')}"
    )

@bot.on(events.CallbackQuery)
async def cb_handler(event):
    data = event.data.decode()
    print(f"✅ Callback: {data}")
    if data.startswith("skip_"):
        chat_id = int(data.split("_")[1])
        if chat_id not in now_playing:
            await event.answer("Tidak ada lagu.", alert=True)
            return
        await event.answer("⏭ Skip!")
        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        try:
            await event.edit("⏭ Diskip!")
        except Exception:
            pass
        await play_next(chat_id)
    elif data.startswith("queue_"):
        chat_id = int(data.split("_")[1])
        queue = get_queue(chat_id)
        if not queue:
            await event.answer("Antrian kosong.", alert=True)
        else:
            text = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(queue))
            await event.answer(f"📋 Antrian:\n{text[:200]}", alert=True)

# catch all debug
@bot.on(events.NewMessage)
async def catch_all(event):
    print(f"📨 chat={event.chat_id} text={event.text!r}")


async def main():
    print("🚀 Starting bot...")

    # Delete webhook
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            async with session.get(url) as r:
                d = await r.json()
                print(f"🔧 deleteWebhook: {d.get('description', d)}")
    except Exception as e:
        print(f"⚠️ deleteWebhook error: {e}")

    # Start Telethon bot dengan FloodWait handling
    for attempt in range(10):
        try:
            await bot.start(bot_token=BOT_TOKEN)
            me = await bot.get_me()
            print(f"✅ Login @{me.username} (id={me.id})")
            break
        except FloodWaitError as e:
            wait = e.seconds + 5
            print(f"⏳ FloodWait {wait}s... attempt {attempt+1}/10")
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"❌ Login error: {e}")
            raise
    else:
        raise Exception("Gagal login 10x")

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
    print("✅ Bot ready!")

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
