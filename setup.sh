#!/usr/bin/env bash
set -euo pipefail

# Setup script for running ScanmyData in a fresh environment (e.g. VPS).
# Installs Python dependencies, Playwright + Chromium, and required system libs.

# Ensure we have pip available.
if ! command -v pip >/dev/null 2>&1; then
  echo "❌ pip not found. Please install Python and pip before running this script."
  exit 1
fi

echo "➡️ Installing Python dependencies from requirements.txt..."
pip install -r requirements.txt

# Playwright needs browser runtimes, and on Debian/Ubuntu it also needs a few system libraries.
# See requirements.txt comment for the list.
if command -v apt-get >/dev/null 2>&1; then
  echo "➡️ Installing system dependencies for Playwright (Debian/Ubuntu)..."
  # apt-get update may fail in some hosted environments due to missing repo keys
  # (e.g. yarnpkg). We ignore that failure and proceed with installing available packages.
  sudo apt-get update || true
  sudo apt-get install -y \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libcups2t64 \
    libdrm2 \
    libxss1 \
    libgbm1 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgtk-3-0t64 \
    libasound2t64
fi

echo "➡️ Installing Playwright Chromium browser runtime..."
python -m playwright install chromium

echo "✅ Setup complete. You can now run the app (e.g. ./start.sh) or python app.py."
