FROM python:3.12-slim

WORKDIR /app

# system deps for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ffmpeg \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# skip heavy/local-only packages that won't work on server
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] websockets httpx python-dotenv \
    aiosqlite aiofiles edge-tts groq \
    pdfplumber reportlab python-multipart ddgs \
    yt-dlp youtube-transcript-api openai \
    faster-whisper playwright \
    "python-jose[cryptography]" "passlib[bcrypt]" "bcrypt<4.0.0" \
    "qrcode[pil]" markdown cryptography tiktoken
RUN pip install --no-cache-dir apscheduler sqlalchemy
RUN playwright install chromium --with-deps

COPY . .

RUN mkdir -p /data/workspace /data/workflows /data/vault
RUN ln -sfn /data/workspace workspace
RUN ln -sfn /data/workflows workflows

ENV HOST=0.0.0.0
ENV PORT=8080
ENV BUD_DATA=/data

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
