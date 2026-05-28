FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    "hydrogram[fast]" \
    "TgCrypto" \
    "aiohttp" \
    "yt-dlp" \
    "python-dotenv"

RUN pip install --no-cache-dir --pre \
    "ntgcalls==2.2.1b3"

RUN pip install --no-cache-dir --pre \
    "git+https://github.com/pytgcalls/pytgcalls.git"

RUN python -c "from hydrogram import Client; print('Hydrogram OK')"
RUN python -c "from pytgcalls import PyTgCalls; print('PyTgCalls OK')"

COPY . .

CMD ["python", "-u", "bot.py"]
