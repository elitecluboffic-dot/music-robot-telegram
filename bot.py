import os
import glob
import asyncio
import subprocess
import sys
import aiohttp
import yt_dlp
import random
from dotenv import load_dotenv
from aiohttp import web

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError  # 🔥 FIX ERROR COOLDOWN

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, StreamEnded
from pytgcalls.types import AudioQuality

load_dotenv()

API_ID           = int(os.getenv("API_ID", 0))
API_HASH         = os.getenv("API_HASH", "")
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
USER_SESSION     = os.getenv("USER_SESSION", "")
TWO_FA_PASSWORD  = os.getenv("TWO_FA_PASSWORD", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH, BOT_TOKEN tidak ada di environment variable!")

if not USER_SESSION:
    raise ValueError("USER_SESSION tidak ada! Pastikan StringSession akun userbot sudah terisi.")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

for f in glob.glob("*.session") + glob.glob("*.session-journal"):
    try: os.remove(f)
    except: pass

bot   = None
user  = None
calls = None
queues: dict      = {}
now_playing: dict = {}
_play_locks: dict = {}

# 🔥 JALUR PENYELAMATAN YOUTUBE: Kosongkan ("") jika proxy lu dirasa bikin buntu
PROXY_URL = "http://hhumibam:9h6wl3nko9hd@84.247.60.125:6095"

def get_queue(chat_id):
    if chat_id not in queues: queues[chat_id] = []
    return queues[chat_id]

def fmt_duration(seconds) -> str:
    if not seconds: return "0:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def cleanup_file(fp):
    if fp and os.path.exists(fp):
        try: os.remove(fp)
        except: pass

_JS_KEY  = None
_JS_PATH = None

def _init_js_runtime():
    global _JS_KEY, _JS_PATH
    if _JS_KEY is None:
        for binary in ["node", "deno", "bun"]:
            try:
                r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    _JS_KEY, _JS_PATH = binary, binary
                    break
            except: pass

def get_active_cookie_file() -> str:
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0: return COOKIES_FILE
    backup_cookies = [f for f in glob.glob("cookies*.txt") if os.path.getsize(f) > 0]
    if backup_cookies: return random.choice(backup_cookies)
    return ""

def get_ydl_opts(extra=None):
    _init_js_runtime()
    opts = {
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 30,
        "noplaylist":      True,
        "ignoreerrors":   True,
        "format":         "ba/b", 
        "keepvideo":      False,
    }
    
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
        opts["noproxy"] = "localhost,127.0.0.1"
        
    opts["http_headers"] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    opts["extractor_args"] = {
        "youtube": {
            "player_client": ["mweb", "ios"],
            "skip": ["dash", "hls"]
        }
    }
    
    cookie_path = get_active_cookie_file()
    if cookie_path: opts["cookiefile"] = cookie_path
    if _JS_KEY: opts["js_runtimes"] = {_JS_KEY: {"path": _JS_PATH}}
    if extra:
        if "extractor_args" in extra and "youtube" in extra["extractor_args"]:
            opts["extractor_args"]["youtube"].update(extra["extractor_args"]["youtube"])
            extra.pop("extractor_args")
        opts.update(extra)
    return opts

def search_and_get_info(query: str) -> dict:
    _init_js_runtime()
    opts = get_ydl_opts({
        "skip_download":  True,
        "default_search": "ytsearch1"
    })
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if info is None: 
            raise Exception("YouTube memblokir koneksi IP/Proxy ini (Respon Kosong).")
        if "entries" in info:
            entries = list(info["entries"])
            if not entries or entries[0] is None: 
                raise Exception("Lagu tidak ditemukan atau IP diblokir Captcha.")
            info = entries[0]
        url = info.get("webpage_url") or info.get("url", "")
        return {"title": info.get("title", "Unknown"), "url": url, "duration": info.get("duration", 0), "uploader": info.get("uploader", "Unknown")}

def _convert_to_opus(src: str, dest: str) -> str:
    subprocess.run([
        "ffmpeg", "-y", "-i", src, 
        "-vn", "-acodec", "libopus", 
        "-ar", "48000", "-ac", "2", 
        "-b:a", "192k", dest
    ], check=True, capture_output=True)
    cleanup_file(src)
    return dest

def download_audio(url: str, filename: str) -> str:
    output_path = os.path.join(DOWNLOAD_DIR, filename)
    opus_path    = output_path + ".opus"
    cleanup_file(opus_path)
    
    opts = get_ydl_opts({"outtmpl": output_path + "._tmp.%(ext)s"})
    with yt_dlp.YoutubeDL(opts) as ydl: 
        ydl.download([url])
        
    if os.path.exists(opus_path): return opus_path
    
    for ext in ["opus", "webm", "m4a", "mp3", "mp4", "aac", "3gp"]:
        fp = f"{output_path}._tmp.{ext}"
        if os.path.exists(fp):
            if ext == "opus":
                os.rename(fp, opus_path)
                return opus_path
            return _convert_to_opus(fp, opus_path)
            
    raise Exception("YouTube memblokir download stream di IP ini.")

async def play_next(chat_id: int):
    if chat_id not in _play_locks: _play_locks[chat_id] = asyncio.Lock()
    async with _play_locks[chat_id]:
        queue = get_queue(chat_id)
        if not queue:
            now_playing.pop(chat_id, None)
            try: await calls.leave_call(chat_id)
            except: pass
            try: await bot.send_message(chat_id, "✅ Antrian habis, bot keluar dari Voice Chat.")
            except: pass
            return
        track = queue.pop(0)
        now_playing[chat_id] = track
        safe = "".join(c for c in track["title"][:30] if c.isalnum() or c in " _-").replace(" ", "_")
        msg = None
        try: msg = await bot.send_message(chat_id, f"▶️ **Now Playing**\n\n🎵 **{track['title']}**\n⏱ {fmt_duration(track['duration'])}\n⏳ *Sedang memproses file audio...*")
        except: pass
        try:
            file_path = await asyncio.wait_for(asyncio.to_thread(download_audio, track["url"], f"{chat_id}_{safe}"), timeout=120)
            
            await calls.play(
                chat_id, 
                MediaStream(
                    file_path,
                    audio_parameters=AudioQuality.STUDIO
                )
            )
            
            now_playing[chat_id]["file_path"] = file_path
            if msg:
                try: await msg.edit(f"▶️ **Playing:** `{track['title']}`\n🎵 Jalur streaming berhasil dibuka!")
                except: pass
        except Exception as e:
            print(f"❌ Play error: {e}")
            await bot.send_message(chat_id, f"❌ Gagal memutar `{track['title']}`. Lanjut ke antrian berikutnya...")
            asyncio.create_task(play_next(chat_id))

def register_handlers(tg_bot):
    @tg_bot.on(events.NewMessage(pattern=r"^/start"))
    async def cmd_start(event): await event.respond("🎵 **Music Bot Cloud-Direct Ready**\n\n▶️ `/play <lagu>`\n⏭ `/skip`\n📋 `/queue`\n⏹ `/stop`")

    @tg_bot.on(events.NewMessage(pattern=r"^/play(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_group))
    async def cmd_play(event):
        query = event.pattern_match.group(1)
        if query: query = query.strip()
        if not query:
            await event.respond("❗ Contoh penggunaan: `/play sisa rasa`")
            return
        chat_id = event.chat_id
        status  = await event.respond(f"🔍 Memproses request **{query}** via cloud engine...")
        try: info = await asyncio.wait_for(asyncio.to_thread(search_and_get_info, query), timeout=90)
        except Exception as e:
            await status.edit(f"❌ Pencarian gagal: `{e}`")
            return
        get_queue(chat_id).append(info)
        buttons = [[Button.inline("⏭ Skip", data=f"skip_{chat_id}"), Button.inline("📋 Queue", data=f"queue_{chat_id}")]]
        await status.edit(f"✅ **Ditambahkan ke antrian!**\n\n🎵 **{info['title']}**\n⏱ {fmt_duration(info['duration'])}", buttons=buttons)
        if chat_id not in now_playing: asyncio.create_task(play_next(chat_id))

    @tg_bot.on(events.NewMessage(pattern=r"^/skip(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_skip(event):
        chat_id = event.chat_id
        if chat_id not in now_playing: return
        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        await event.respond("⏭ Lagu berhasil dilewati!")
        try: await calls.leave_call(chat_id)
        except: pass
        asyncio.create_task(play_next(chat_id))

    @tg_bot.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_queue(event):
        chat_id = event.chat_id
        queue   = get_queue(chat_id)
        if not queue and chat_id not in now_playing:
            await event.respond("📋 Daftar antrian musik kosong.")
            return
        text = "📋 **Daftar Antrian Musik**\n\n"
        if chat_id in now_playing: text += f"▶️ **Now Playing:** `{now_playing[chat_id]['title']}`\n\n"
        for i, t in enumerate(queue, 1): text += f"{i}. `{t['title']}`\n"
        await event.respond(text)

    @tg_bot.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_stop(event):
        chat_id         = event.chat_id
        queues[chat_id] = []
        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        try: await calls.leave_call(chat_id)
        except: pass
        await event.respond("⏹ Pemutaran dihentikan.")

    @tg_bot.on(events.CallbackQuery)
    async def cb_handler(event):
        data = event.data.decode()
        chat_id = int(data.split("_")[1])
        if data.startswith("skip_"):
            if chat_id not in now_playing: return
            cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
            try: await calls.leave_call(chat_id)
            except: pass
            asyncio.create_task(play_next(chat_id))

async def on_stream_end_handler(client, update):
    if isinstance(update, StreamEnded):
        cleanup_file(now_playing.pop(update.chat_id, {}).get("file_path"))
        asyncio.create_task(play_next(update.chat_id))


# ─── CORE RUNNER SYSTEM ─────────────────────────────────────────

def generate_otp_or_password(secret: str) -> str:
    if not secret: return ""
    try:
        import pyotp
        totp = pyotp.TOTP(secret.strip().replace(" ", ""))
        return totp.now()
    except Exception:
        return secret

async def main():
    global bot, user, calls
    print("🚀 Inisialisasi container bot...")
    
    print("📦 [SYSTEM] Memaksa upgrade yt-dlp ke versi terbaru...")
    up_proc = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "pyotp"], capture_output=True, text=True)
    
    global yt_dlp
    import importlib
    importlib.reload(yt_dlp)

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            await session.get(url)
    except: pass

    bot   = TelegramClient("bot_session", API_ID, API_HASH, catch_up=False)
    user  = TelegramClient(StringSession(USER_SESSION), API_ID, API_HASH, catch_up=False)
    calls = PyTgCalls(user)

    register_handlers(bot)
    calls.on_update()(on_stream_end_handler)

    if TWO_FA_PASSWORD:
        credential = generate_otp_or_password(TWO_FA_PASSWORD)
        print("🔐 [2FA ENGINE] Membuka proteksi login session menggunakan password teks...")
        try:
            await user.start(password=lambda: credential)
        except FloodWaitError as e:
            print(f"⏳ [FLOODWAIT USERBOT] Telegram meminta tunggu {e.seconds} detik sebelum login userbot...")
            await asyncio.sleep(e.seconds)
            await user.start(password=lambda: credential)
    else:
        try:
            await user.start()
        except FloodWaitError as e:
            print(f"⏳ [FLOODWAIT USERBOT] Telegram meminta tunggu {e.seconds} detik sebelum login userbot...")
            await asyncio.sleep(e.seconds)
            await user.start()

    # 🔥 FIX COOLDOWN: Bungkus start bot dengan logic penahan FloodWaitError
    try:
        await bot.start(bot_token=BOT_TOKEN)
    except FloodWaitError as e:
        print(f"⏳ [FLOODWAIT BOT] Telegram membatasi login! Script otomatis tidur selama {e.seconds} detik...")
        await asyncio.sleep(e.seconds)
        print("🚀 Bangun! Mencoba login kembali ke Telegram...")
        await bot.start(bot_token=BOT_TOKEN)

    await calls.start()

    async def health(request): return web.Response(text="OK")
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
            err_msg = str(e).lower()
            if "timestamp" in err_msg or "outdated" in err_msg:
                print("⚠️ [TIMESTAMP WARNING] Menangkap error desinkronisasi, merefresh koneksi dalam 3 detik...")
                await asyncio.sleep(3)
                continue
            break

if __name__ == "__main__":
    asyncio.run(main())
