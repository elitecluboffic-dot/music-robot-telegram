FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --pre \
    "ntgcalls==2.2.1b3" \
    "git+https://github.com/pytgcalls/pytgcalls.git" \
    "pyrofork" \
    TgCrypto \
    yt-dlp \
    python-dotenv

RUN python -c "from pytgcalls import PyTgCalls; print('OK')"

COPY . .
CMD ["python", "bot.py"]
