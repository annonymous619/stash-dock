FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOAD_ROOT=/downloads \
    CONFIG_ROOT=/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade gallery-dl yt-dlp

COPY backend/ /app/
RUN mkdir -p /config /downloads \
    && cp /app/gallery-dl.conf /config/gallery-dl.conf

EXPOSE 9091
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9091/api/health || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9091"]
