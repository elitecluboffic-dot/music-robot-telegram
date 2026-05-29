import os
import glob
import asyncio
import subprocess
import aiohttp
import yt_dlp
import random
from dotenv import load_dotenv
from aiohttp import web

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, StreamEnded

load_dotenv()

API_ID       = int(os.getenv("API_ID", 0))
API_HASH     = os.getenv("API_HASH", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
USER_SESSION = os.getenv("USER_SESSION", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH, BOT_TOKEN tidak ada di environment variable!")

if not USER_SESSION:
    raise ValueError("USER_SESSION tidak ada! Pastikan StringSession akun userbot sudah terisi.")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Bersihkan sisa session-file yang corrupt saat restart container
for f in glob.glob("*.session") + glob.glob("*.session-journal"):
    try:
        os.remove(f)
    except Exception:
        pass

bot   = None
user  = None
calls = None

queues: dict      = {}
now_playing: dict = {}
_play_locks: dict = {}

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

# ─── YT-DLP ENGINE OPTIMIZATION (ANTI-BLOCK & ROTATION SYSTEM) ───

_JS_KEY  = None
_JS_PATH = None

def _init_js_runtime():
    """Mendeteksi otomatis engine Javascript di container untuk bypass signature check"""
    global _JS_KEY, _JS_PATH
    if _JS_KEY is None:
        for binary in ["node", "deno", "bun"]:
            try:
                r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    _JS_KEY, _JS_PATH = binary, binary
                    break
            except Exception:
                pass

def get_active_cookie_file() -> str:
    """Mendeteksi cookie utama atau otomatis merotasi ke cookie cadangan jika tersedia"""
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        return COOKIES_FILE
    
    # Mencari file cadangan seperti cookies1.txt, cookies_backup.txt, dll.
    backup_cookies = [f for f in glob.glob("cookies*.txt") if os.path.getsize(f) > 0]
    if backup_cookies:
        selected_cookie = random.choice(backup_cookies)
        print(f"🔄 [COOKIE ENGINE] Menggunakan file cookie cadangan: {selected_cookie}")
        return selected_cookie
        
    return ""

def get_ydl_opts(extra=None):
    _init_js_runtime()
    
    opts = {
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 30,  
        "noplaylist":      True,
        "ignoreerrors":    True,
        "source_address": "0.0.0.0",  
        "format":         "bestaudio/best",
        "keepvideo":      False, 
        
        # ─── PROXY ENGINE INTEGRATION ───
        "proxy":          "https://139.59.105.64:8080",
        "noproxy":        "localhost,127.0.0.1",
        
        "extractor_args": {
            "youtube": {
                "player_client": ["tvhtml5", "web", "mweb"],
            }
        },
        "postprocessors": [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "128",
        }],
    }

    cookie_path = get_active_cookie_file()
    if cookie_path:
        opts["cookiefile"] = cookie_path

    if _JS_KEY:
        opts["js_runtimes"] = {_JS_KEY: {"path": _JS_PATH}}

    if extra:
        if "extractor_args" in extra and "youtube" in extra["extractor_args"]:
            opts["extractor_args"]["youtube"].update(extra["extractor_args"]["youtube"])
            extra.pop("extractor_args")
        opts.update(extra)

    return opts


def search_and_get_info(query: str) -> dict:
    print(f"🚀 [DIRECT-ENGINE] Mencari info musik untuk: {query}")
    _init_js_runtime()

    info_opts = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "noplaylist":      True,
        "default_search": "ytsearch1",
        "ignoreerrors":    False,
        "socket_timeout": 30,
        "source_address": "0.0.0.0",
        "format":         "bestaudio/best",
        
        # ─── PROXY ENGINE INTEGRATION ───
        "proxy":          "https://139.59.105.64:8080",
        "noproxy":        "localhost,127.0.0.1",
        
        "extractor_args": {
            "youtube": {
                "player_client": ["tvhtml5", "web", "mweb"],
            }
        },
    }

    cookie_path = get_active_cookie_file()
    if cookie_path:
        info_opts["cookiefile"] = cookie_path

    if _JS_KEY:
        info_opts["js_runtimes"] = {_JS_KEY: {"path": _JS_PATH}}

    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(query, download=False)

        if info is None:
            raise Exception("YouTube merespon dengan data kosong (Empty Response).")

        if "entries" in info:
            entries = list(info["entries"])
            if not entries:
                raise Exception("Lagu tidak ditemukan pada hasil pencarian.")
            info = entries[0]

        if not info:
            raise Exception("Gagal melakukan ekstraksi metadata video.")

        url = info.get("webpage_url") or info.get("url", "")
        if not url:
            raise Exception("URL streaming video YouTube tidak valid.")

        return {
            "title":    info.get("title", "Unknown"),
            "url":      url,
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
        }


def _convert_to_mp3(src: str, dest: str) -> str:
    subprocess.run(
        ["ffmpeg", "-y", "-i", src,
         "-vn", "-ar", "44100", "-ac", "2", "-b:a", "128k", dest],
        check=True, capture_output=True,
    )
    cleanup_file(src)
    return dest


def download_audio(url: str, filename: str) -> str:
    output_path = os.path.join(DOWNLOAD_DIR, filename)
    mp3_path    = output_path + ".mp3"

    cleanup_file(mp3_path)

    print(f"⬇️ Memulai proses download audio via Direct Cloud Connection...")
    opts = get_ydl_opts({"outtmpl": output_path + "._tmp.%(ext)s"})

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    if os.path.exists(mp3_path):
        return mp3_path

    for ext in ["mp3", "m4a", "webm", "opus", "mp4", "aac"]:
        fp = f"{output_path}._tmp.{ext}"
        if os.path.exists(fp):
            if ext == "mp3":
                os.rename(fp, mp3_path)
                return mp3_path
            return _convert_to_mp3(fp, mp3_path)

    raise Exception("File audio gagal diunduh atau diblokir oleh YouTube.")


# ─── VOICE CHAT PLAY LOGIC ──────────────────────────────────────

async def play_next(chat_id: int):
    if chat_id not in _play_locks:
        _play_locks[chat_id] = asyncio.Lock()

    async with _play_locks[chat_id]:
        queue = get_queue(chat_id)
        if not queue:
            now_playing.pop(chat_id, None)
            try:
                await calls.leave_call(chat_id)
            except Exception:
                pass
            try:
                await bot.send_message(chat_id, "✅ Antrian habis, bot keluar dari Voice Chat.")
            except Exception:
                pass
            return

        track = queue.pop(0)
        now_playing[chat_id] = track
        safe = "".join(c for c in track["title"][:30] if c.isalnum() or c in " _-").replace(" ", "_")

        msg = None
        try:
            msg = await bot.send_message(
                chat_id,
                f"▶️ **Now Playing**\n\n"
                f"🎵 **{track['title']}**\n"
                f"⏱ {fmt_duration(track['duration'])}\n"
                f"⏳ *Sedang memproses file audio...*",
            )
        except Exception as e:
            print(f"send_message error: {e}")

        try:
            file_path = await asyncio.wait_for(
                asyncio.to_thread(download_audio, track["url"], f"{chat_id}_{safe}"),
                timeout=120
            )

            await calls.play(chat_id, MediaStream(file_path))
            now_playing[chat_id]["file_path"] = file_path

            if msg:
                try:
                    await msg.edit(f"▶️ **Playing:** `{track['title']}`\n🎵 Jalur streaming berhasil dibuka!")
                except Exception:
                    pass

        except asyncio.TimeoutError:
            print(f"❌ Timeout download: {track['title']}")
            await bot.send_message(chat_id, f"❌ Timeout memproses `{track['title']}`. Skip otomatis ke antrian selanjutnya...")
            asyncio.create_task(play_next(chat_id))

        except Exception as e:
            print(f"❌ Play error: {e}")
            await bot.send_message(chat_id, f"❌ Gagal memutar `{track['title']}` akibat restriksi jaringan. Melanjutkan ke antrian berikutnya...")
            asyncio.create_task(play_next(chat_id))


# ─── TELEGRAM HANDLERS SYSTEM ───────────────────────────────────

def register_handlers(tg_bot):
    @tg_bot.on(events.NewMessage(pattern=r"^/start"))
    async def cmd_start(event):
        await event.respond(
            "🎵 **Music Bot Cloud-Direct Ready**\n\n"
            "▶️ `/play <lagu>` — putar lagu\n"
            "⏭ `/skip` — skip lagu\n"
            "📋 `/queue` — lihat antrian\n"
            "⏹ `/stop` — stop & clear"
        )

    @tg_bot.on(events.NewMessage(pattern=r"^/play(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_group))
    async def cmd_play(event):
        query = event.pattern_match.group(1)
        if query:
            query = query.strip()

        if not query:
            await event.respond("❗ Contoh penggunaan: `/play sisa rasa`")
            return

        chat_id = event.chat_id
        status  = await event.respond(f"🔍 Memproses request **{query}** via cloud engine...")

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(search_and_get_info, query),
                timeout=45
            )
        except asyncio.TimeoutError:
            await status.edit("❌ Waktu pencarian habis (Timeout). Server YouTube lambat merespon.")
            return
        except Exception as e:
            await status.edit(f"❌ Pencarian gagal: `{e}`")
            return

        get_queue(chat_id).append(info)

        buttons = [
            [Button.inline("⏭ Skip", data=f"skip_{chat_id}"),
             Button.inline("📋 Queue", data=f"queue_{chat_id}")]
        ]
        await status.edit(
            f"✅ **Ditambahkan ke antrian!**\n\n"
            f"🎵 **{info['title']}**\n"
            f"⏱ {fmt_duration(info['duration'])}",
            buttons=buttons,
        )

        if chat_id not in now_playing:
            asyncio.create_task(play_next(chat_id))

    @tg_bot.on(events.NewMessage(pattern=r"^/skip(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_skip(event):
        chat_id = event.chat_id
        if chat_id not in now_playing:
            await event.respond("❗ Tidak ada musik yang sedang aktif diputar.")
            return

        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        await event.respond("⏭ Lagu berhasil dilewati!")
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        asyncio.create_task(play_next(chat_id))

    @tg_bot.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_queue(event):
        chat_id = event.chat_id
        queue   = get_queue(chat_id)

        if not queue and chat_id not in now_playing:
            await event.respond("📋 Daftar antrian musik kosong.")
            return

        text = "📋 **Daftar Antrian Musik**\n\n"
        if chat_id in now_playing:
            np = now_playing[chat_id]
            text += f"▶️ **Now Playing:** `{np['title']}` — {fmt_duration(np['duration'])}\n\n"
        for i, t in enumerate(queue, 1):
            text += f"{i}. `{t['title']}` — {fmt_duration(t['duration'])}\n"
        await event.respond(text)

    @tg_bot.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_stop(event):
        chat_id         = event.chat_id
        queues[chat_id] = []
        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await event.respond("⏹ Pemutaran dihentikan dan daftar antrian dibersihkan.")

    @tg_bot.on(events.CallbackQuery)
    async def cb_handler(event):
        data = event.data.decode()
        if data.startswith("skip_"):
            chat_id = int(data.split("_")[1])
            if chat_id not in now_playing:
                await event.answer("Tidak ada lagu yang diputar.", alert=True)
                return
            await event.answer("⏭ Skip!")
            cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
            try:
                await calls.leave_call(chat_id)
            except Exception:
                pass
            asyncio.create_task(play_next(chat_id))
        elif data.startswith("queue_"):
            chat_id = int(data.split("_")[1])
            queue   = get_queue(chat_id)
            if not queue and chat_id not in now_playing:
                await event.answer("Daftar antrian kosong.", alert=True)
                return
            text = "📋 **Antrian Saat Ini**\n\n"
            if chat_id in now_playing:
                text += f"▶️ `{now_playing[chat_id]['title']}`\n\n"
            for i, t in enumerate(queue, 1):
                text += f"{i}. `{t['title']}`\n"
            await event.respond(text)


async def on_stream_end_handler(client, update):
    if not isinstance(update, StreamEnded):
        return
    chat_id = update.chat_id
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    asyncio.create_task(play_next(chat_id))


# ─── CORE RUNNER SYSTEM ─────────────────────────────────────────

async def main():
    global bot, user, calls

    print("🚀 Inisialisasi container bot...")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            await session.get(url)
    except Exception:
        pass

    bot   = TelegramClient("bot_session", API_ID, API_HASH)
    user  = TelegramClient(StringSession(USER_SESSION), API_ID, API_HASH)
    calls = PyTgCalls(user)

    register_handlers(bot)
    calls.on_update()(on_stream_end_handler)

    await user.start()
    await bot.start(bot_token=BOT_TOKEN)
    await calls.start()

    async def health(request):
        return web.Response(text="OK")

    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
    print("✅ Bot siap! Menunggu request command...")

    while True:
        try:
            await bot.run_until_disconnected()
        except Exception as e:
            if "timestamp" in str(e).lower():
                await asyncio.sleep(2)
                continue
            break

if __name__ == "__main__":
    asyncio.run(main())
