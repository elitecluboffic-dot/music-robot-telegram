FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

RUN pip install --no-cache-dir --pre \
    "pytgcalls==3.0.0.dev24" \
    ntgcalls \
    "pyrogram==2.0.106" \
    TgCrypto \
    yt-dlp \
    python-dotenv

RUN python -c "from pytgcalls import PyTgCalls; print('pytgcalls OK')"

COPY . .
CMD ["python", "bot.py"]
