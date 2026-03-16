# ScanmyData Deployment on Coolify (Docker UI Guide)

This document explains how to deploy this application on Coolify using the existing Dockerfile, with full step-by-step instructions and custom domain setup.

## 1. Prerequisites

Before starting in Coolify UI, verify the following:

1. A running Coolify instance (self-hosted or cloud).
2. Access to this repository from Coolify (GitHub connected).
3. A valid domain name that you control.
4. DNS management access for your domain.
5. Infisical project already populated with app secrets.

Important: The app is configured to use Infisical at runtime. In the server .env for this app, keep only:

- INFISICAL_TOKEN
- INFISICAL_PROJECT_ID
- INFISICAL_ENVIRONMENT
- INFISICAL_BASE_URL

All other secrets should come from Infisical.

## 2. Create Application in Coolify UI

1. Open Coolify Dashboard.
2. Go to Projects.
3. Create a new project or open an existing one.
4. Click New Resource.
5. Choose Application.
6. Select Public Repository or Private Repository (depending on your repo visibility).
7. Pick repository: douradonis/ScanmyData_private.
8. Pick branch: backup-good (or your production branch).
9. Build Pack / Type: Dockerfile.
10. Dockerfile Path: Dockerfile.
11. Base Directory: leave empty (repo root).
12. Port: 5001 (recommended internal app port for consistency with app.py default).

Then click Continue.

## 3. Configure Build and Runtime in Coolify

In the application settings:

1. Build Command: leave empty (Dockerfile handles build).
2. Start Command: leave empty (Dockerfile CMD runs gunicorn).
3. Exposed Port / Internal Port: 5000.
4. Set health check path to a lightweight endpoint if available (example: /).
5. Keep auto-redeploy enabled if you want deployment on every push.
6. Add env var `PORT=5001` so gunicorn binds to the same app port.

## 4. Add Environment Variables in Coolify

In Environment Variables for this app, add only these four values:

1. INFISICAL_TOKEN
2. INFISICAL_PROJECT_ID
3. INFISICAL_ENVIRONMENT
4. INFISICAL_BASE_URL

Do not add Firebase, SMTP, FLASK_SECRET, or other application secrets in Coolify env if they already exist in Infisical.

## 5. First Deploy

1. Click Deploy.
2. Wait for build phase to complete (dependency install and Playwright Chromium install can take time).
3. Open Logs and confirm startup line from gunicorn.
4. Open the generated Coolify application URL and test login.

Expected behavior:

- App starts on port 5001 inside container.
- Runtime pulls secrets from Infisical.
- Firebase initializes without requiring firebase-key.json on disk.

## 5.1 Port Matrix (Important)

The repository has different default ports depending on how you start it:

1. `python app.py` -> default `5001` (from `app.py`).
2. Dockerfile `gunicorn` -> `PORT` env var, fallback `5000`.
3. `start.sh` -> default `10000`.

For Coolify, set `PORT=5001` to keep one consistent runtime port.

## 6. Connect Custom Domain (Cloudflare Tunnel)

If your domain is routed through Cloudflare Tunnel and the tunnel forwards to `localhost:8000`, use this model:

1. Domain -> Cloudflare Tunnel hostname
2. Cloudflare Tunnel service -> `http://localhost:8000`
3. Local reverse proxy on `8000` -> Coolify app upstream
4. Coolify app container -> `PORT=5001`

In this setup, `8000` is tunnel entrypoint and `5001` is app runtime port.

### 6.1 Add Domain in Coolify

1. Open your app in Coolify.
2. Go to Domains.
3. Click Add Domain.
4. Enter your domain (example: app.example.com).
5. Enable HTTPS / SSL (Let\'s Encrypt) in Coolify.
6. Save.

### 6.2 Configure DNS at your registrar

Use one of the following patterns depending on your Coolify installation:

Option A: Coolify on your own server

1. Create an A record:
- Host: app (or @)
- Value: your Coolify server public IP
- TTL: Auto

Option B: Coolify behind another proxy/load balancer

1. Create a CNAME record:
- Host: app
- Value: target provided by your infrastructure/proxy setup
- TTL: Auto

If you are unsure, start with the A record to the Coolify host public IP for self-hosted setups.

For Cloudflare Tunnel deployments, DNS is typically managed in Cloudflare with a proxied CNAME created by the tunnel route command, instead of direct A/CNAME to server IP.

Example cloudflared config snippet:

```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
	- hostname: app.yourdomain.com
		service: http://localhost:8000
	- service: http_status:404
```

### 6.3 Verify certificate and routing

1. Wait for DNS propagation (usually a few minutes, sometimes longer).
2. In Coolify, check domain status.
3. Confirm SSL is issued.
4. Visit https://your-domain and verify app loads.

### 6.4 Verify that port 8000 is correct (live checks)

Run these checks directly on the server that runs cloudflared.

1. Check cloudflared ingress target (must show localhost:8000):

```bash
grep -n "service: http://localhost:" /etc/cloudflared/config.yml
```

Expected output (example):

```text
12:    - hostname: app.yourdomain.com
13:      service: http://localhost:8000
```

2. Check that something is listening on 8000:

```bash
ss -ltnp | grep ':8000'
```

Expected output (example):

```text
tcp    LISTEN 0      128    127.0.0.1:8000      0.0.0.0:*   users:("python3",pid=1234,fd=12)
```

3. Check that upstream app is healthy behind the local endpoint:

```bash
curl -I http://127.0.0.1:8000/
```

Expected output (any HTTP 2xx/3xx):

```text
HTTP/1.1 200 OK
Date: ...
Server: ...
```

If #1 shows `localhost:8000`, #2 shows LISTEN on `:8000`, and #3 returns HTTP 200/302, then port 8000 is correct for your tunnel entrypoint.

Optional deeper check (confirm app port behind the reverse proxy):

```bash
ss -ltnp | grep -E ':5001|:5000|:10000'
```

## 7. Post-Deploy Validation Checklist

1. Login works.
2. Firebase-dependent features work.
3. Scraping endpoints work.
4. File uploads and data persistence paths are writable.
5. No missing-secret warnings in logs.

## 8. Common Issues and Fixes

### Issue: App starts but login says Firebase not enabled

Cause: Firebase credentials are missing in Infisical or incomplete.

Fix:

1. Ensure Infisical contains either:
- FIREBASE_CREDENTIALS_JSON (full JSON), or
- split service-account fields (type, project_id, private_key, client_email, token_uri, and related fields).
2. Ensure FIREBASE_DATABASE_URL exists in Infisical.
3. Redeploy.

### Issue: Domain added but site not reachable

Cause: DNS record points to wrong IP/target or has not propagated yet.

Fix:

1. Re-check A/CNAME target.
2. Wait for propagation.
3. Confirm Coolify host is reachable on 80/443.

### Issue: Build is slow or fails during browser install

Cause: Playwright Chromium installation timeouts or resource limits.

Fix:

1. Re-run deployment.
2. Ensure host has enough disk and memory.
3. Keep Dockerfile unchanged for dependency compatibility.

## 9. Recommended Production Notes

1. Use a dedicated branch for production deployments.
2. Enable automatic backups of persistent volumes and database.
3. Restrict Coolify admin access with strong auth.
4. Rotate INFISICAL_TOKEN periodically.
5. Keep application secrets only in Infisical.
