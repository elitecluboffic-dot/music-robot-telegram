FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    "pyrogram==2.0.106" \
    "TgCrypto" \
    "ntgcalls==2.2.1b3" \
    --pre "git+https://github.com/pytgcalls/pytgcalls.git" \
    yt-dlp \
    python-dotenv \
    aiohttp

RUN python -c "from pyrogram import Client; print('Pyrogram OK')"
RUN python -c "from pytgcalls import PyTgCalls; print('PyTgCalls OK')"

COPY . .

CMD ["python", "-u", "bot.py"]
