# Setup Guide for ScanmyData

This guide walks you through setting up the ScanmyData application on a fresh environment (e.g., VPS or local machine).

## Prerequisites

- Python 3.8+
- Ubuntu/Debian-based system (for apt commands)
- Git

## Step 1: Clone the Repository

```bash
git clone https://github.com/douradonis/ScanmyData_private.git
cd ScanmyData_private
```

## Step 2: Install Python Dependencies

```bash
pip install -r requirements.txt
```

## Step 3: Install Playwright Browser Binaries

Playwright requires Chromium binaries for headless browsing (used for scraping dynamic sites like SimpleInvoicing and Megasoft).

```bash
python -m playwright install chromium
```

## Step 4: Install System Libraries

For Playwright to work properly, install the required shared libraries:

```bash
sudo apt-get update
sudo apt-get install -y libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libxss1 libgbm1 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgtk-3-0t64 libasound2t64
```

## Step 5: Configure Environment Variables

Copy and edit the `.env` file:

```bash
cp .env.example .env  # If .env.example exists, otherwise edit .env directly
```

Key variables to set:
- `FIREBASE_CREDENTIALS_PATH`: Path to your Firebase service account JSON.
- `FIREBASE_DATABASE_URL`: Your Firebase Realtime Database URL.
- `FIREBASE_API_KEY`: Firebase API key.
- `ADMIN_USER_ID`: Admin user ID.
- `SMTP_*`: Email settings for notifications.
- `MYDATA_USE_BROWSER=1`: Enable Playwright fallback for scraping.
- `LEGACY_FETCH_MODE=0`: Use new fetch mode (or 1 for legacy).

## Step 6: Initialize Database (if needed)

If starting fresh:

```bash
python scripts/reset_db.py
python scripts/create_test_users.py
```

## Step 7: Start the Application

Make the start script executable and run it:

```bash
chmod +x start.sh
./start.sh
```

The app will run on `http://localhost:10000` (or the port set in `PORT` env var).

## Testing

- Open `http://localhost:10000` in your browser.
- Test scraping with URLs like SimpleInvoicing or Megasoft.
- Check logs in `activity_monitor.log` for performance metrics.

## Troubleshooting

- If Playwright fails: Ensure system libraries are installed (Step 4).
- If app doesn't start: Check for missing env vars or port conflicts.
- For scraping issues: Set `MYDATA_USE_BROWSER=1` and ensure Chromium is installed.

## Notes

- The app uses Gunicorn for production serving.
- Playwright is only used as fallback for JS-heavy sites; static scraping is preferred for speed.
- All test scripts have been removed; use the main app for testing.

## Deploy with Coolify (Docker) and Custom Domain

For full Coolify UI deployment instructions, use:

- `docs/coolify_docker_setup.md`

Quick summary:

1. In Coolify, create a new Application from this repository.
2. Select Dockerfile mode with Dockerfile path set to `Dockerfile`.
3. Use internal port `5000`.
4. In Coolify Environment Variables, keep only Infisical bootstrap vars:
	- `INFISICAL_TOKEN`
	- `INFISICAL_PROJECT_ID`
	- `INFISICAL_ENVIRONMENT`
	- `INFISICAL_BASE_URL`
5. Add your custom domain from the app Domains section in Coolify.
6. Create DNS record at your registrar pointing to your Coolify host.
7. Enable SSL (Let's Encrypt) in Coolify and verify HTTPS.