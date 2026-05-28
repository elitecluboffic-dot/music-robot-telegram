import os
import glob
import json
import asyncio
import subprocess
import aiohttp
import yt_dlp
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
PO_TOKEN     = os.getenv("PO_TOKEN", "")
VISITOR_DATA = os.getenv("VISITOR_DATA", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("API_ID, API_HASH, BOT_TOKEN tidak ada!")

if not USER_SESSION:
    raise ValueError("USER_SESSION tidak ada! Jalankan gen_session.py dulu di lokal.")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Hapus session bot lama
for f in glob.glob("bot_session.session") + glob.glob("bot_session.session-journal"):
    try:
        os.remove(f)
    except Exception:
        pass

# ─── CLIENTS ─────────────────────────────────────────────────────

bot   = TelegramClient("bot_session", API_ID, API_HASH)
user  = TelegramClient(StringSession(USER_SESSION), API_ID, API_HASH)
calls = PyTgCalls(user)

# ─── STATE ───────────────────────────────────────────────────────

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

# ─── YT-DLP ──────────────────────────────────────────────────────

_JS_KEY  = None
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
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 30,
        "format":         "bestaudio/best",  # paling relaxed, ambil apapun yg ada
        "noplaylist":     True,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "mweb", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "com.google.android.youtube/19.29.37 (Linux; U; Android 11) gzip"
            ),
        },
    }

    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE

    if PO_TOKEN:
        opts["extractor_args"]["youtube"]["po_token"]      = [f"web+{PO_TOKEN}"]
        opts["extractor_args"]["youtube"]["player_client"] = ["web", "android"]
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
    """
    Ambil info lagu tanpa download.
    Format tidak di-validasi saat info fetch — biar tidak OOM/format error.
    """
    info_opts = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "noplaylist":     True,
        "default_search": "ytsearch1",
        "ignoreerrors":   False,
        "socket_timeout": 30,
        # Jangan set "format" di sini — biar tidak trigger format check saat info
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "mweb", "web"],
            }
        },
    }

    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        info_opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(query, download=False)

        if info is None:
            raise Exception("Tidak ditemukan hasil apapun")

        if "entries" in info:
            entries = list(info["entries"])  # flatten generator dulu!
            if not entries:
                raise Exception("Hasil pencarian kosong")
            info = entries[0]

        if not info:
            raise Exception("Info lagu tidak valid")

        url = info.get("webpage_url") or info.get("url", "")
        if not url:
            raise Exception("URL lagu tidak ditemukan")

        return {
            "title":    info.get("title", "Unknown"),
            "url":      url,
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
        }


def _find_downloaded_file(base_path: str, suffixes: list) -> str | None:
    """Cari file hasil download dengan berbagai kemungkinan ekstensi."""
    for suffix in suffixes:
        for ext in ["mp3", "m4a", "webm", "opus", "ogg", "mp4", "aac"]:
            p = f"{base_path}{suffix}.{ext}"
            if os.path.exists(p):
                return p
    return None


def _convert_to_mp3(src: str, dest: str) -> str:
    """Convert file audio ke mp3 128k."""
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

    # ── Attempt 1: android client + langsung ke mp3 ──
    print(f"⬇️ Attempt 1: android client...")
    try:
        opts = get_ydl_opts({
            "outtmpl": output_path + ".%(ext)s",
            "format":  "bestaudio/best",
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "128",
            }],
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        if os.path.exists(mp3_path):
            print("✅ Attempt 1 berhasil")
            return mp3_path

        found = _find_downloaded_file(output_path, [""])
        if found:
            return _convert_to_mp3(found, mp3_path)

    except Exception as e:
        print(f"⚠️ Attempt 1 gagal: {e}")

    # ── Attempt 2: ios client ──
    print("🔄 Attempt 2: ios client...")
    try:
        opts = get_ydl_opts({
            "outtmpl": output_path + "_a2.%(ext)s",
            "format":  "bestaudio/best",
            "extractor_args": {
                "youtube": {"player_client": ["ios"]}
            },
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        found = _find_downloaded_file(output_path, ["_a2"])
        if found:
            if found.endswith(".mp3"):
                return found
            return _convert_to_mp3(found, mp3_path)

    except Exception as e:
        print(f"⚠️ Attempt 2 gagal: {e}")

    # ── Attempt 3: web client tanpa format restriction ──
    print("🔄 Attempt 3: web client fallback...")
    try:
        opts = get_ydl_opts({
            "outtmpl": output_path + "_a3.%(ext)s",
            "format":  "bestaudio/best",
            "extractor_args": {
                "youtube": {"player_client": ["web", "mweb"]}
            },
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        found = _find_downloaded_file(output_path, ["_a3"])
        if found:
            if found.endswith(".mp3"):
                return found
            return _convert_to_mp3(found, mp3_path)

    except Exception as e:
        print(f"⚠️ Attempt 3 gagal: {e}")

    # ── Attempt 4: last resort ──
    print("🔄 Attempt 4: last resort...")
    try:
        opts = get_ydl_opts({
            "outtmpl": output_path + "_a4.%(ext)s",
            "format":  "worstaudio/worst",
            "extractor_args": {
                "youtube": {"player_client": ["android", "ios", "web", "mweb"]}
            },
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        found = _find_downloaded_file(output_path, ["_a4"])
        if found:
            if found.endswith(".mp3"):
                return found
            return _convert_to_mp3(found, mp3_path)

    except Exception as e:
        print(f"⚠️ Attempt 4 gagal: {e}")

    raise Exception("Semua metode download gagal. Cek koneksi atau update yt-dlp.")


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

    try:
        await bot.send_message(
            chat_id,
            f"▶️ **Now Playing**\n\n"
            f"🎵 **{track['title']}**\n"
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
            await bot.send_message(chat_id, f"❌ Error memutar lagu: {e}")
        except Exception:
            pass
        await play_next(chat_id)


@calls.on_update()
async def on_stream_end(client, update):
    if not isinstance(update, StreamEnded):
        return
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
        "▶️ `/play <lagu>` — putar lagu\n"
        "⏭ `/skip` — skip lagu\n"
        "📋 `/queue` — lihat antrian\n"
        "⏹ `/stop` — stop & kosongkan antrian\n"
        "🎧 `/nowplaying` — lagu yang sedang diputar\n\n"
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
    status  = await event.respond(f"🔍 Mencari **{query}**...")

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, search_and_get_info, query)
        print(f"✅ Found: {info['title']}")
    except Exception as e:
        await status.edit(f"❌ Gagal mencari: {e}")
        return

    get_queue(chat_id).append(info)

    buttons = [
        [Button.inline("⏭ Skip", data=f"skip_{chat_id}"),
         Button.inline("📋 Queue", data=f"queue_{chat_id}")]
    ]
    await status.edit(
        f"✅ **Ditambahkan ke antrian!**\n\n"
        f"🎵 **{info['title']}**\n"
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
        await event.respond("❗ Tidak ada lagu yang sedang diputar.")
        return

    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))
    await event.respond("⏭ Di-skip!")

    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass

    await play_next(chat_id)


@bot.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_queue(event):
    chat_id = event.chat_id
    queue   = get_queue(chat_id)

    if not queue and chat_id not in now_playing:
        await event.respond("📋 Antrian kosong.")
        return

    text = "📋 **Antrian**\n\n"
    if chat_id in now_playing:
        np    = now_playing[chat_id]
        text += f"▶️ **{np['title']}** — {fmt_duration(np['duration'])}\n\n"
    for i, t in enumerate(queue, 1):
        text += f"{i}. {t['title']} — {fmt_duration(t['duration'])}\n"

    await event.respond(text)


@bot.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_stop(event):
    chat_id         = event.chat_id
    queues[chat_id] = []
    cleanup_file(now_playing.pop(chat_id, {}).get("file_path"))

    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass

    await event.respond("⏹ Stop. Antrian dikosongkan.")


@bot.on(events.NewMessage(pattern=r"^/nowplaying(?:@\w+)?$", func=lambda e: e.is_group))
async def cmd_nowplaying(event):
    chat_id = event.chat_id
    if chat_id not in now_playing:
        await event.respond("❗ Tidak ada lagu yang sedang diputar.")
        return

    np = now_playing[chat_id]
    await event.respond(
        f"▶️ **Now Playing**\n\n"
        f"🎵 **{np['title']}**\n"
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
            await event.edit("⏭ Di-skip!")
        except Exception:
            pass
        await play_next(chat_id)

    elif data.startswith("queue_"):
        chat_id = int(data.split("_")[1])
        queue   = get_queue(chat_id)
        if not queue:
            await event.answer("Antrian kosong.", alert=True)
        else:
            text = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(queue))
            await event.answer(f"📋 Antrian:\n{text[:200]}", alert=True)


@bot.on(events.NewMessage)
async def catch_all(event):
    print(f"📨 chat={event.chat_id} text={event.text!r}")


# ─── MAIN ────────────────────────────────────────────────────────

async def main():
    global PO_TOKEN, VISITOR_DATA

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

    # Skip auto-generate PO Token (OOM di server RAM kecil)
    if PO_TOKEN:
        print(f"✅ PO Token dari env: {PO_TOKEN[:20]}...")
    else:
        print("⚠️ PO_TOKEN tidak ada di env, lanjut tanpa PO Token")

    # Login userbot
    print("👤 Login userbot via StringSession...")
    try:
        await user.start()
        me = await user.get_me()
        print(f"✅ Userbot login: @{me.username or me.first_name} (id={me.id})")
    except Exception as e:
        print(f"❌ Userbot login error: {e}")
        raise

    # Login bot
    for attempt in range(10):
        try:
            await bot.start(bot_token=BOT_TOKEN)
            me = await bot.get_me()
            print(f"✅ Bot login: @{me.username} (id={me.id})")
            break
        except FloodWaitError as e:
            wait = e.seconds + 5
            print(f"⏳ FloodWait {wait}s... attempt {attempt+1}/10")
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"❌ Bot login error: {e}")
            raise
    else:
        raise Exception("Gagal login bot 10x")

    # Start PyTgCalls
    try:
        await calls.start()
        print("✅ PyTgCalls started (userbot)")
    except Exception as e:
        print(f"❌ PyTgCalls error: {e}")
        raise

    # Health check server
    async def health(request):
        return web.Response(text="OK")

    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Health check port {port}")
    print("✅ Bot siap!")

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
