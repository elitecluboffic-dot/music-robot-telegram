FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install pyrofork dulu (fork pyrogram yg support GroupcallForbidden)
# Lalu pytgcalls dev dari git
RUN pip install --no-cache-dir \
    "pyrofork==2.3.42" \
    "TgCrypto" \
    "aiohttp" \
    "yt-dlp" \
    "python-dotenv" \
    && pip install --no-cache-dir --pre \
    "ntgcalls==2.2.1b3" \
    "git+https://github.com/pytgcalls/pytgcalls.git"

# Verify
RUN python -c "from pyrogram import Client, idle; print('Pyrogram OK')"
RUN python -c "from pytgcalls import PyTgCalls; print('PyTgCalls OK')"

COPY . .

CMD ["python", "-u", "bot.py"]
