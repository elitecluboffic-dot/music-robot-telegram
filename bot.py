import os
import glob
import json
import asyncio
import subprocess
import aiohttp
import requests
import random
import yt_dlp
import base64
import secrets
from dotenv import load_dotenv
from aiohttp import web

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, StreamEnded

load_dotenv()

API_ID       = int(os.getenv("API_ID", 0))
API_HASH     = os.getenv("API_HASH", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
USER_SESSION = os.getenv("USER_SESSION", "")

# ─── LOGIK BYPASS MANDIRI (FIX AUTO-GENERATE) ────────────────────
PO_TOKEN     = "web+dummy_po_token_bypass_v1"
VISITOR_DATA = ""

def generate_visitor_data_lokal():
    random_bytes = secrets.token_bytes(12)
    encoded = base64.b64encode(random_bytes).decode('utf-8')
    visitor = "Cg9leU" + encoded.replace('+', '-').replace('/', '_').replace('=', '')
    return visitor
# ─────────────────────────────────────────────────────────────────

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH, BOT_TOKEN tidak ada!")

if not USER_SESSION:
    raise ValueError("USER_SESSION tidak ada! Masukkan StringSession baru di Railway variables.")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 🔥 FIX INTERNAL SESSION: Hapus semua sisa session lama biar gak kena invalid nonce hash
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

# ─── SMART DYNAMIC PROXY SYSTEM (VALIDASI AUTO RETRY) ────────────

def get_dynamic_free_proxy():
    api_key = os.getenv("GEONODE_API_KEY")
    proxy_pool = []
    
    if api_key:
        try:
            url = f"https://api.geonode.com/gproxi/v1/proxies?apiKey={api_key}&limit=5&protocols=http&anonymityLevel=elite"
            response = requests.get(url, timeout=3)
            if response.status_code == 200:
                data = response.json()
                if data.get("data"):
                    for p in data["data"]:
                        proxy_pool.append(f"http://{p['ip']}:{p['port']}")
        except Exception as e:
            print(f"⚠️ Geonode API error/timeout: {e}")

    if len(proxy_pool) < 3:
        try:
            fallback_url = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"
            resp = requests.get(fallback_url, timeout=3)
            if resp.status_code == 200:
                proxies = resp.text.strip().split("\n")
                if proxies:
                    sampled = random.sample(proxies, min(25, len(proxies)))
                    for p in sampled:
                        proxy_pool.append(f"http://{p.strip()}")
        except Exception as e:
            print(f"❌ Gagal scrape proxy backup dari GitHub: {e}")

    if not proxy_pool:
        return None

    print(f"🔄 Memulai validasi dari {len(proxy_pool)} kandidat proxy...")
    random.shuffle(proxy_pool)
    
    for attempt, proxy_str in enumerate(proxy_pool[:8], 1):
        try:
            test_resp = requests.get("https://www.google.com", proxies={"http": proxy_str, "https": proxy_str}, timeout=2.5)
            if test_resp.status_code == 200:
                print(f"✅ Proxy OK (Percobaan {attempt}): {proxy_str}")
                return proxy_str
        except Exception:
            continue

    fallback_pick = random.choice(proxy_pool)
    print(f"⚠️ Proxy test semuanya lambat, pakai nekat: {fallback_pick}")
    return fallback_pick

# ─── YT-DLP CONFIGURATIONS ───────────────────────────────────────
_JS_KEY  = None
_JS_PATH = None

def get_ydl_opts(extra=None):
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

    opts = {
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 12,  
        "format":         "best/highest",  # 🔥 FIX NUKLIR: Ambil format standar apa saja agar proxy luar negeri tidak error format
        "noplaylist":      True,
        "ignoreerrors":    True,
        "proxy":          get_dynamic_free_proxy(),  
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"],
            }
        },
        "postprocessors": [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "128",
        }],
    }

    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE

    if PO_TOKEN:
        opts["extractor_args"]["youtube"]["po_token"] = [f"{PO_TOKEN}"]
        if VISITOR_DATA:
            opts["extractor_args"]["youtube"]["visitor_data"] = [VISITOR_DATA]

    if _JS_KEY:
        opts["js_runtimes"] = {_JS_KEY: {"path": _JS_PATH}}

    if extra:
        if "extractor_args" in extra:
            for k, v in extra.pop("extractor_args", {}).items():
                if k in opts["extractor_args"]:
                    opts["extractor_args"][k].update(v)
                else:
                    opts["extractor_args"][k] = v
        opts.update(extra)

    return opts


def search_and_get_info(query: str) -> dict:
    last_error = None
    
    # 🔥 FIX AUTO-RETRY LOOP: Jika proxy timeout pas nyari info lagu, otomatis ganti proxy baru sampai 5 kali
    for run in range(1, 6):
        proxy_current = get_dynamic_free_proxy()
        print(f"🔍 Mencari info lagu (Percobaan {run}/5) menggunakan proxy: {proxy_current}")
        
        info_opts = {
            "quiet":          True,
            "no_warnings":    True,
            "skip_download":  True,
            "noplaylist":      True,
            "default_search": "ytsearch1",
            "ignoreerrors":    False,
            "socket_timeout": 12, 
            "proxy":          proxy_current,  
            "format":         "best/highest",  # 🔥 SAMA: Paksa format universal
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "mweb", "web"],
                }
            },
        }

        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            info_opts["cookiefile"] = COOKIES_FILE

        if PO_TOKEN:
            info_opts["extractor_args"]["youtube"]["po_token"] = [f"{PO_TOKEN}"]
            if VISITOR_DATA:
                info_opts["extractor_args"]["youtube"]["visitor_data"] = [VISITOR_DATA]

        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(query, download=False)

                if info is None:
                    continue

                if "entries" in info:
                    entries = list(info["entries"])
                    if not entries:
                        continue
                    info = entries[0]

                if not info:
                    continue

                url = info.get("webpage_url") or info.get("url", "")
                if not url:
                    continue

                return {
                    "title":    info.get("title", "Unknown"),
                    "url":      url,
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", "Unknown"),
                }
        except Exception as e:
            print(f"⚠️ Percobaan {run}/5 gagal akibat error proxy: {e}")
            last_error = e
            continue

    raise Exception(f"Gagal setelah 5x ganti proxy. Error terakhir: {last_error}")


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

    print(f"⬇️ Memproses download via yt-dlp...")
    try:
        opts = get_ydl_opts({
            "outtmpl": output_path + "._tmp.%(ext)s",
        })
        
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

    except Exception as e:
        print(f"⚠️ Proses download gagal/timeout: {e}")

    raise Exception("Koneksi proxy macet atau diblokir YouTube! Mencoba ulang otomatis...")


# ─── PLAY LOGIC ──────────────────────────────────────────────────

async def play_next(chat_id: int):
    queue = get_queue(chat_id)
    if not queue:
        now_playing.pop(chat_id, None)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        try:
            await bot.send_message(chat_id, "✅ Antrian habis, userbot keluar dari Voice Chat.")
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
            f"⏳ *Sedang memproses audio (Anti-Stuck Aktif)...*",
        )
    except Exception as e:
        print(f"send_message error: {e}")

    try:
        file_path = await asyncio.to_thread(
            download_audio, track["url"], f"{chat_id}_{safe}"
        )
        
        await calls.play(chat_id, MediaStream(file_path))
        now_playing[chat_id]["file_path"] = file_path
        
        if msg:
            try:
                await msg.edit(f"▶️ **Playing:** `{track['title']}`\n🎵 Jalur streaming berhasil dibuka!")
            except Exception:
                pass
    except Exception as e:
        print(f"❌ Play error (Otomatis ganti proxy baru): {e}")
        queue.insert(0, track)
        await asyncio.sleep(2)
        await play_next(chat_id) 


async def on_stream_end_handler(client, update):
    if not isinstance(update, StreamEnded):
        return
    chat_id = update.chat_id
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    await play_next(chat_id)


# ─── REGISTER HANDLERS SYSTEM ───────────────────────────────────

def register_handlers(tg_bot):
    @tg_bot.on(events.NewMessage(pattern=r"^/start"))
    async def cmd_start(event):
        await event.respond(
            "🎵 **Music Bot Aktif**\n\n"
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
            await event.respond("❗ Contoh: `/play despacito`")
            return

        chat_id = event.chat_id
        status  = await event.respond(f"🔍 Mencari info & mengetes proxy untuk **{query}**...")

        try:
            info = await asyncio.to_thread(search_and_get_info, query)
        except Exception as e:
            await status.edit(f"❌ Gagal mencari info (Semua proxy mati): {e}")
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
            await play_next(chat_id)

    @tg_bot.on(events.NewMessage(pattern=r"^/skip(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_skip(event):
        chat_id = event.chat_id
        if chat_id not in now_playing:
            await event.respond("❗ Tidak ada lagu yang sedang diputar.")
            return

        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        await event.respond("⏭ Di-skip!")
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await play_next(chat_id)

    @tg_bot.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_queue(event):
        chat_id = event.chat_id
        queue   = get_queue(chat_id)

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

    @tg_bot.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$", func=lambda e: e.is_group))
    async def cmd_stop(event):
        chat_id         = event.chat_id
        queues[chat_id] = []
        cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await event.respond("⏹ Stop. Antrian dikosongkan.")

    @tg_bot.on(events.CallbackQuery)
    async def cb_handler(event):
        data = event.data.decode()
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
            await play_next(chat_id)


# ─── MAIN ────────────────────────────────────────────────────────

async def main():
    global PO_TOKEN, VISITOR_DATA, bot, user, calls

    print("🚀 Starting bot...")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            await session.get(url)
    except Exception:
        pass

    VISITOR_DATA = generate_visitor_data_lokal()
    bot   = TelegramClient("bot_session", API_ID, API_HASH)
    
    # Membuka sesi baru secara bersih
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
    print("✅ Bot siap!")

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
