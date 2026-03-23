# Dockerfile (βελτιωμένη εκδοχή για Render Free)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Athens \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Runtime deps:
# - existing libs for pyzbar/pdf2image/Pillow
# - extra libs required by Playwright Chromium fallback (Megasoft -> MyData button click)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 \
    poppler-utils \
    libjpeg-dev \
    zlib1g-dev \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxrender1 \
    libxshmfence1 \
    libxtst6 \
    xdg-utils \
 && rm -rf /var/lib/apt/lists/*

# Πρώτα τα requirements για caching
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /app/requirements.txt \
 && python -m playwright install chromium \
 && python - <<'PY'
from pathlib import Path
p = Path('/ms-playwright')
ok = any(x.is_file() and ('chrome' in x.name.lower()) for x in p.rglob('*'))
if not ok:
    raise SystemExit('Playwright Chromium binary not found under /ms-playwright')
print('Playwright Chromium binary detected')
PY

# Μετά όλος ο κώδικας
COPY . /app

# Δημιουργία φακέλων runtime (ephemeral fs στο Render)
RUN mkdir -p /app/uploads /app/data && chmod -R 777 /app/uploads /app/data

EXPOSE 5000

# Free tier: 1 worker, 8 threads, logs στο STDOUT
CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-5000} \
    --workers 1 --threads 8 --timeout 180 \
    --access-logfile - --error-logfile -
