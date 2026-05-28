FROM python:3.10-slim
RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

# Install PO Token generator sekali di build time
RUN npm install -g youtube-po-token-generator

WORKDIR /app

# 🔥 FIX TOTAL: Install library dasar + tambahkan "requests" di sini!
RUN pip install --no-cache-dir \
    "telethon" \
    "aiohttp" \
    "yt-dlp" \
    "python-dotenv" \
    "requests"

RUN pip install --no-cache-dir --pre \
    "ntgcalls==2.2.1b3" \
    "git+https://github.com/pytgcalls/pytgcalls.git"

RUN python -c "from telethon import TelegramClient; print('Telethon OK')"
RUN python -c "from pytgcalls import PyTgCalls; print('PyTgCalls OK')"

COPY . .
CMD ["python", "-u", "bot.py"]
