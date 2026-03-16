"""Email utilities for sending verification emails, password resets, etc."""
import os
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone
import base64
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

logger = logging.getLogger(__name__)

# Email config from environment
EMAIL_PROVIDER = os.getenv('EMAIL_PROVIDER', 'smtp')  # 'smtp', 'oauth2_outlook', 'resend', or 'railway_proxy'
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SENDER_EMAIL = os.getenv('SENDER_EMAIL', SMTP_USER)
APP_URL = os.getenv('APP_URL', 'http://localhost:5001')
OAUTH2_CREDENTIALS_FILE = os.getenv('OAUTH2_CREDENTIALS_FILE', 'outlook_oauth2_credentials.json')
RESEND_API_KEY = os.getenv('RESEND_API_KEY', '')
RESEND_EMAIL_SENDER = os.getenv('RESEND_EMAIL_SENDER', '')
RESEND_REPLY_TO = os.getenv('RESEND_REPLY_TO', '')   # Reply-To address for Resend emails
RESEND_INBOUND_FORWARD_ENABLED = os.getenv('RESEND_INBOUND_FORWARD_ENABLED', 'true')
RESEND_INBOUND_FORWARD_TO = os.getenv('RESEND_INBOUND_FORWARD_TO', SMTP_USER)
RESEND_INBOUND_STORE_FILE = os.getenv('RESEND_INBOUND_STORE_FILE', 'data/system/resend_inbound_forwarded_ids.json')
RESEND_WEBHOOK_SIGNING_SECRET = os.getenv('RESEND_WEBHOOK_SIGNING_SECRET', '')  # whsec_... from Resend dashboard
RESEND_INBOUND_HOURLY_FALLBACK_ENABLED = os.getenv('RESEND_INBOUND_HOURLY_FALLBACK_ENABLED', 'false')
RAILWAY_PROXY_URL = os.getenv('RAILWAY_PROXY_URL', '')


def _inline_logo_data_uri() -> str:
    """Return a small inline logo as a data URI.

    This is used so that email clients (e.g. Outlook / Gmail) do not block the
    logo as an external image. Prefer a small favicon PNG for best compatibility.
    """
    # Prefer a small favicon to keep message size low.
    candidates = [
        os.path.join(os.getcwd(), 'icons', 'favicon-96x96.png'),
        os.path.join(os.getcwd(), 'icons', 'scanmydata_logo_3000w.png'),
    ]
    for p in candidates:
        try:
            if not os.path.exists(p):
                continue
            with open(p, 'rb') as fh:
                data = fh.read()
            b64 = base64.b64encode(data).decode('ascii')
            return f"data:image/png;base64,{b64}"
        except Exception:
            continue

    # Fallback to a very small SVG icon if no PNG is found.
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#1a56db"/><stop offset="100%" stop-color="#0b3a8b"/></linearGradient></defs>'
        '<rect width="64" height="64" rx="12" fill="url(#g)"/>'
        '<text x="32" y="38" font-family="Arial,Helvetica,sans-serif" font-size="28" fill="#fff" text-anchor="middle" font-weight="700">S</text>'
        '</svg>'
    )
    return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode('utf-8')).decode('ascii')}"


def _get_logo_data() -> tuple[bytes, str]:
    """Return raw logo bytes and mime type for embedding in email attachments."""
    # Prefer PNG as it is broadly supported, but fall back to SVG if PNG not found.
    candidates = [
        os.path.join(os.getcwd(), 'icons', 'favicon-96x96.png'),
        os.path.join(os.getcwd(), 'icons', 'scanmydata_logo_3000w.png'),
    ]
    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            with open(path, 'rb') as fh:
                data = fh.read()
            return data, 'image/png'
        except Exception:
            continue

    # Fallback to inline SVG content
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#1a56db"/><stop offset="100%" stop-color="#0b3a8b"/></linearGradient></defs>'
        '<rect width="64" height="64" rx="12" fill="url(#g)"/>'
        '<text x="32" y="38" font-family="Arial,Helvetica,sans-serif" font-size="28" fill="#fff" text-anchor="middle" font-weight="700">S</text>'
        '</svg>'
    )
    return svg.encode('utf-8'), 'image/svg+xml'


def _get_default_logo_url(logo_cid: str = 'scanmydata_logo') -> str:
    """Return the default logo URL to use in email HTML.

    For SMTP, we prefer `cid:` so the logo can be attached as an inline MIME part.
    For providers that don't support MIME attachments (e.g. Resend API), we fall
    back to an inline data URI via `_inline_logo_data_uri`.
    """
    return f"cid:{logo_cid}"


def _public_logo_url() -> str:
    """Return a public logo URL for providers that don't render CID reliably."""
    base = (
        os.getenv('PUBLIC_BASE_URL', '').strip()
        or os.getenv('RENDER_EXTERNAL_URL', '').strip()
        or APP_URL.strip()
    )
    if not base or 'localhost' in base or base.startswith('http://127.'):
        base = 'https://www.scanmydata.gr'
    return f"{base.rstrip('/')}/icons/favicon-96x96.png"


def _replace_cid_with_data_uri(html: str, cid: str = 'scanmydata_logo') -> str:
    """Replace a CID reference with an inline data URI (PNG)."""
    if not html or f"cid:{cid}" not in html:
        return html
    return html.replace(f"cid:{cid}", _inline_logo_data_uri())


def make_email_html(
    greeting: str,
    body_html: str,
    cta_url: str = None,
    cta_text: str = 'Κάντε κλικ εδώ',
    expiry_note: str = None,
    security_note: str = None,
    logo_url: str = None,
    header_subtitle: str = None,
    preheader: str = None,
) -> str:
    """Build a clean, transactional-style email (minimal, inbox-friendly).

    Modelled on plain service-notification emails (e.g. Papaki / bank alerts)
    to avoid Gmail/Hotmail classifying the message as "Promotions".
    """

    # Default preheader (Gmail/Outlook preview line)
    if preheader is None:
        preheader = 'Σας στέλνουμε αυτό το email από το ScanmyData.'

    if header_subtitle is None:
        header_subtitle = 'Ειδοποίηση Λογαριασμού'

    provider = get_email_provider()

    # For Resend/OAuth/Railway use a small public image directly.
    # Outlook web renders this more reliably than CID attachments.
    if not logo_url:
        if provider in ('resend', 'oauth2_outlook', 'railway_proxy'):
            logo_url = _public_logo_url()
        else:
            logo_url = 'cid:scanmydata_logo'

    cta_block = ''
    if cta_url:
        cta_block = f"""
                <table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" style=\"margin:18px 0 12px;\">
                    <tr>
                        <td style=\"border-radius:6px;background:#1a56db;\">
                            <a href=\"{cta_url}\" style=\"display:inline-block;padding:12px 22px;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;\">{cta_text}</a>
                        </td>
                    </tr>
                </table>
                <p style=\"margin:0 0 14px;font-size:12px;color:#666;word-break:break-all;\">{cta_url}</p>"""

    expiry_block = (
        f'<p style="margin:0 0 8px;font-size:12px;color:#666;">{expiry_note}</p>'
        if expiry_note else ''
    )
    security_block = (
        f'<p style="margin:0 0 8px;font-size:12px;color:#888;">{security_note}</p>'
        if security_note else ''
    )

    return f"""<!DOCTYPE html>
<html lang=\"el\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
</head>
<body style=\"margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;font-size:14px;color:#333;\">
  <!-- Preheader: hidden text shown in preview snippets -->
  <div style=\"display:none;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;\">{preheader}</div>
    <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" style=\"background:#f5f5f5;\">
        <tr>
            <td align=\"center\" style=\"padding:20px 10px 36px;\">
                <table role=\"presentation\" width=\"600\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" style=\"width:600px;max-width:600px;\">
                    <tr>
                        <td style=\"padding:0 0 16px;\">
                            <table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\">
                                <tr>
                                    <td style=\"border-right:2px solid #e3e3e3;padding-right:12px;\">
                                        <a href=\"https://www.scanmydata.gr\" target=\"_blank\" style=\"text-decoration:none;\">
                                            <img src=\"{logo_url}\" alt=\"ScanmyData\" width=\"48\" height=\"48\" style=\"display:block;border:0;width:48px;height:48px;\" />
                                        </a>
                                    </td>
                                    <td style=\"padding-left:16px;vertical-align:middle;\">
                                        <div style=\"font-family:Helvetica,Arial,sans-serif;font-size:20px;color:#424244;font-weight:bold;line-height:1.2;\">ScanmyData</div>
                                        <div style=\"font-size:12px;color:#666;line-height:1.3;\">{header_subtitle}</div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    <tr>
                        <td style=\"background:#ffffff;border:1px solid #e3e3e3;padding:24px 24px 30px;font-family:Arial,sans-serif;font-size:15px;color:#131212;line-height:1.6;\">
                            <p style=\"margin:0 0 16px;font-size:16px;font-weight:600;\">{greeting}</p>
                            {body_html}
                            {cta_block}
                            {expiry_block}
                            {security_block}
                            <p style=\"margin:16px 0 0;font-size:13px;color:#666;\">Αυτό το email στάλθηκε από το ScanmyData.</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


def get_email_provider() -> str:
    """Get the current email provider from settings or environment"""
    try:
        # Try to load from settings file first (admin panel preference)
        from pathlib import Path
        import json
        
        # Try to import app's load_settings function
        try:
            from app import load_settings
            settings = load_settings()
            provider = settings.get('email_provider', '').strip()
            logger.info(f"get_email_provider: loaded from settings: {provider}")
            if provider in ['smtp', 'oauth2_outlook', 'resend', 'railway_proxy']:
                return provider
        except (ImportError, Exception) as e:
            logger.warning(f"get_email_provider: failed to load from settings: {e}")
            pass
    except Exception as e:
        logger.warning(f"get_email_provider: outer exception: {e}")
        pass
    
    # Fallback to environment variable
    fallback = EMAIL_PROVIDER
    logger.info(f"get_email_provider: falling back to env: {fallback}")
    return fallback


def send_email(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an email via SMTP, OAuth2, Resend, or Railway Proxy based on configuration"""
    
    # Get current provider from settings or environment
    provider = get_email_provider()
    logger.info(f"send_email called: to={to_email}, subject={subject}, provider={provider}")
    
    # Optionally inline the logo image as a data URI (can be blocked by some
    # clients; enabled via environment variable when desired).
    try:
        inline_logo = os.getenv('EMAIL_INLINE_LOGO', 'false').strip().lower() in ('1', 'true', 'yes')
        if inline_logo:
            html_body = _inline_logo_into_html(html_body)
    except Exception:
        pass

    # Ensure we always have a plain-text fallback (helps mail clients and deliverability).
    if not text_body:
        text_body = _strip_html_to_text(html_body)

    # Route to appropriate sending function
    if provider == 'railway_proxy':
        logger.info(f"Routing to Railway Proxy for {to_email}")
        return send_railway_proxy_email(to_email, subject, html_body, text_body)
    elif provider == 'resend':
        logger.info(f"Routing to Resend for {to_email}")
        return send_resend_email(to_email, subject, html_body, text_body)
    elif provider == 'oauth2_outlook':
        logger.info(f"Routing to OAuth2 for {to_email}")
        return send_oauth2_email(to_email, subject, html_body, text_body)
    else:
        logger.info(f"Routing to SMTP for {to_email}")
        return send_smtp_email(to_email, subject, html_body, text_body)


def _inline_logo_into_html(html: str) -> str:
    """If a local logo file exists (icons/scanmydata_logo_3000w.png), embed it as a base64 data URI.

    This helps email clients show the logo even if they block external images or the
    APP_URL used in the template isn't reachable (e.g., localhost in production).
    """
    try:
        if not html or '<img' not in html:
            return html

        # Common logo file paths to try (project relative)
        candidates = [
            os.path.join(os.getcwd(), 'icons', 'favicon-96x96.png'),
            os.path.join(os.getcwd(), 'icons', 'scanmydata_logo_3000w.png'),
            os.path.join(os.getcwd(), 'static', 'icons', 'scanmydata_logo_3000w.png'),
            os.path.join(os.getcwd(), 'icons', 'scanmydata_logo.png'),
        ]
        logo_path = None
        for p in candidates:
            if os.path.exists(p):
                logo_path = p
                break
        if not logo_path:
            return html

        # Read and base64-encode
        import base64
        import re

        with open(logo_path, 'rb') as fh:
            raw = fh.read()
        mime = 'image/png'
        b64 = base64.b64encode(raw).decode('ascii')
        data_uri = f'data:{mime};base64,{b64}'

        # Replace any <img src=".../icons/..."> regardless of host (APP_URL may not match).
        # We do this rather than relying on the exact APP_URL value, so logos still render
        # even if the app sends emails with an APP_URL that isn't publicly reachable.
        pattern = r'(<img\b[^>]*\bsrc=["\"])([^"\"]*/icons/[^"\"]*)(["\"])'
        html = re.sub(pattern, lambda m: f"{m.group(1)}{data_uri}{m.group(3)}", html, flags=re.IGNORECASE)

        return html
    except Exception as e:
        logger.debug(f"_inline_logo_into_html failed: {e}")
        return html


def send_smtp_email(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an email via traditional SMTP"""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning(f"SMTP not configured; skipping email to {to_email}")
        return False

    try:
        # Use multipart/related to allow inline images (CID) plus alternative parts.
        msg = MIMEMultipart('related')
        alternative = MIMEMultipart('alternative')
        msg.attach(alternative)

        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email

        # Attach the plain text version first
        if text_body:
            alternative.attach(MIMEText(text_body, 'plain', 'utf-8'))
        alternative.attach(MIMEText(html_body, 'html', 'utf-8'))

        # Attach inline logo if the HTML references it via CID
        if 'cid:scanmydata_logo' in html_body:
            logo_paths = [
                os.path.join(os.getcwd(), 'icons', 'favicon-96x96.png'),
                os.path.join(os.getcwd(), 'icons', 'scanmydata_logo_3000w.png'),
            ]
            logo_path = next((p for p in logo_paths if os.path.exists(p)), None)
            if logo_path:
                try:
                    with open(logo_path, 'rb') as fh:
                        img_data = fh.read()
                    img = MIMEImage(img_data)
                    img.add_header('Content-ID', '<scanmydata_logo>')
                    img.add_header('Content-Disposition', 'inline', filename=os.path.basename(logo_path))
                    msg.attach(img)
                except Exception as e:
                    logger.warning(f"Failed to attach inline logo {logo_path}: {e}")

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, to_email, msg.as_string())

        logger.info(f"Email sent via SMTP to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send SMTP email to {to_email}: {e}")
        return False


def send_oauth2_email(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an email via Microsoft OAuth2"""
    try:
        from oauth2_email_handler import OutlookOAuth2EmailSender
        
        sender = OutlookOAuth2EmailSender(OAUTH2_CREDENTIALS_FILE)
        success = sender.send_email(to_email, subject, text_body or html_body, html_body)
        
        if success:
            logger.info(f"Email sent via OAuth2 to {to_email}: {subject}")
        else:
            logger.error(f"OAuth2 email send failed to {to_email}")
            
        return success
        
    except ImportError:
        logger.error("OAuth2 email handler not available")
        return False
    except Exception as e:
        logger.error(f"Failed to send OAuth2 email to {to_email}: {e}")
        return False


def send_resend_email(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an email via Resend API"""
    if not RESEND_API_KEY:
        logger.warning(f"Resend API key not configured; skipping email to {to_email}")
        return False
    
    # Check if using test domain - warn and suggest SMTP fallback
    sender = RESEND_EMAIL_SENDER or SENDER_EMAIL or "noreply@yourdomain.com"
    if sender == "onboarding@resend.dev":
        logger.warning(f"Using Resend test domain 'onboarding@resend.dev' - this only sends to verified emails!")
        logger.warning(f"For production, configure a verified domain in RESEND_EMAIL_SENDER")
        logger.warning(f"Falling back to SMTP for {to_email}")
        return send_smtp_email(to_email, subject, html_body, text_body)
    
    try:
        import resend
        
        # Set the API key
        resend.api_key = RESEND_API_KEY
        
        had_cid_logo = 'cid:scanmydata_logo' in html_body

        # Keep inline attachment as a secondary fallback for clients that do support CID.
        attachments = None
        if had_cid_logo:
            data, mime = _get_logo_data()
            b64 = base64.b64encode(data).decode('ascii')
            attachments = [
                {
                    "content": b64,
                    "filename": "scanmydata_logo.png" if mime == 'image/png' else "scanmydata_logo.svg",
                    "content_type": mime,
                    "content_id": "scanmydata_logo",
                    "inline_content_id": "scanmydata_logo",
                }
            ]

        # Use public URL in HTML for better Outlook web support.
        if had_cid_logo:
            html_body = html_body.replace('cid:scanmydata_logo', _public_logo_url())

        # Prepare email params - Resend requires 'from' to be a verified domain
        reply_to = RESEND_REPLY_TO or sender
        params = {
            "from": sender,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
            "reply_to": reply_to,
        }

        if attachments:
            params["attachments"] = attachments

        # Add text body if provided
        if text_body:
            params["text"] = text_body

        # Send email using Resend API
        logger.info(f"Attempting to send via Resend: from={sender}, to={to_email}")
        email = resend.Emails.send(params)

        logger.info(f"Email sent via Resend to {to_email}: {subject} (ID: {email.get('id', 'unknown')})")
        return True

    except ImportError:
        logger.error("Resend library not available. Install it with: pip install resend")
        return False
    except Exception as e:
        logger.error(f"Failed to send Resend email to {to_email}: {e}")
        logger.error(f"Resend error details - Type: {type(e).__name__}, Args: {e.args}")
        logger.error(f"Resend sender was: {sender}, API key present: {bool(RESEND_API_KEY)}")
        
        # Fallback to SMTP if Resend fails
        logger.warning(f"Falling back to SMTP for {to_email}")
        return send_smtp_email(to_email, subject, html_body, text_body)


def send_railway_proxy_email(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an email via Railway HTTP-to-SMTP proxy"""
    try:
        # Get Railway proxy URL from settings or environment
        from app import load_settings
        settings = load_settings()
        proxy_url = settings.get('railway_proxy_url', '').strip() or RAILWAY_PROXY_URL
    except Exception:
        proxy_url = RAILWAY_PROXY_URL
    
    if not proxy_url:
        logger.warning(f"Railway proxy URL not configured; skipping email to {to_email}")
        logger.warning(f"Falling back to SMTP for {to_email}")
        return send_smtp_email(to_email, subject, html_body, text_body)
    
    # Validate SMTP credentials are available
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error(f"SMTP credentials not configured for Railway proxy; cannot send email to {to_email}")
        return False
    
    try:
        import requests
        
        # Prepare the request payload
        payload = {
            "smtp": {
                "host": SMTP_SERVER,
                "port": SMTP_PORT,
                "secure": SMTP_PORT == 465,  # SSL for port 465, STARTTLS for others
                "user": SMTP_USER,
                "pass": SMTP_PASSWORD
            },
            "mail": {
                "from": SENDER_EMAIL,
                "to": to_email,
                "subject": subject,
                "html": html_body
            }
        }
        
        # Add text body if provided
        if text_body:
            payload["mail"]["text"] = text_body
        
        # Ensure proxy URL ends with /send-mail
        if not proxy_url.endswith('/send-mail'):
            proxy_url = proxy_url.rstrip('/') + '/send-mail'
        
        # Send request to Railway proxy
        logger.info(f"Sending email via Railway proxy: {proxy_url} to {to_email}")
        response = requests.post(
            proxy_url,
            json=payload,
            timeout=30,
            headers={'Content-Type': 'application/json'}
        )
        
        # Check response
        if response.status_code == 200:
            result = response.json()
            if result.get('success'):
                logger.info(f"Email sent via Railway proxy to {to_email}: {subject}")
                logger.info(f"Railway proxy response: messageId={result.get('messageId')}")
                return True
            else:
                logger.error(f"Railway proxy returned success=false: {result.get('error')}")
                return False
        else:
            logger.error(f"Railway proxy returned status {response.status_code}: {response.text}")
            return False
            
    except ImportError:
        logger.error("Requests library not available. Install it with: pip install requests")
        return False
    except requests.exceptions.Timeout:
        logger.error(f"Railway proxy request timeout for {to_email}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Railway proxy request failed for {to_email}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to send email via Railway proxy to {to_email}: {e}")
        return False


def create_verification_token(user_id: int, token_type: str = 'email_verify', expires_in_hours: int = 24) -> Optional[str]:
    """Create a verification/reset token and store it in DB"""
    try:
        from models import db, VerificationToken
        from datetime import datetime, timedelta, timezone
        
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
        
        vtoken = VerificationToken(
            user_id=user_id,
            token=token,
            token_type=token_type,
            expires_at=expires_at
        )
        db.session.add(vtoken)
        db.session.commit()
        return token
    except Exception as e:
        logger.error(f"Failed to create verification token: {e}")
        return None


def verify_token(token: str, token_type: str) -> Optional[int]:
    """Verify a token and return user_id if valid; mark token as used"""
    try:
        from models import db, VerificationToken
        
        vtoken = VerificationToken.query.filter_by(token=token, token_type=token_type).first()
        if not vtoken or not vtoken.is_valid():
            return None
        
        vtoken.used = True
        db.session.commit()
        return vtoken.user_id
    except Exception as e:
        logger.error(f"Failed to verify token: {e}")
        return None


def send_email_verification(user_email: str, user_id: int, user_username: str) -> bool:
    """Send email verification link"""
    token = create_verification_token(user_id, 'email_verify', 24)
    if not token:
        return False

    verify_url = f"{APP_URL}/auth/verify-email?token={token}"

    html_body = make_email_html(
        greeting=f"Γεια σου {user_username},",
        body_html=(
            "<p style='margin:0 0 14px;'>Σε ευχαριστούμε που εγγράφηκες στο <strong>ScanmyData</strong>!"
            " Για να ενεργοποιήσεις τον λογαριασμό σου, παρακαλώ επαλήθευσε τη διεύθυνση email σου"
            " πατώντας τον παρακάτω σύνδεσμο:</p>"
        ),
        cta_url=verify_url,
        cta_text="Επαλήθευση Email",
        expiry_note="Ο σύνδεσμος λήγει σε 24 ώρες.",
        security_note="Εάν δεν δημιούργησες αυτόν τον λογαριασμό, παρακαλώ αγνόησε αυτό το email.",
    )

    text_body = f"""ScanmyData - Επαλήθευση Email

Γεια σου {user_username},

Σε ευχαριστούμε που εγγράφηκες στο ScanmyData!
Για να ενεργοποιήσεις τον λογαριασμό σου, κάνε κλικ στο παρακάτω link:

{verify_url}

Ο σύνδεσμος λήγει σε 24 ώρες.
Αν δεν δημιούργησες εσύ αυτόν τον λογαριασμό, αγνόησε αυτό το email.

Τμήμα Εξυπηρέτησης Πελατών
ScanmyData"""

    return send_email(user_email, 'Επαλήθευση Email - ScanmyData', html_body, text_body)


def send_password_reset(user_email: str, user_id: int, user_username: str) -> bool:
    """Send password reset link"""
    token = create_verification_token(user_id, 'password_reset', 1)  # 1 hour expiry
    if not token:
        return False

    reset_url = f"{APP_URL}/auth/reset-password?token={token}"

    html_body = make_email_html(
        greeting=f"Γεια σου {user_username},",
        body_html=(
            "<p style='margin:0 0 14px;'>Λάβαμε αίτημα για επαναφορά του κωδικού σου στο"
            " <strong>ScanmyData</strong>. Κάντε κλικ στον παρακάτω σύνδεσμο για να ορίσετε νέο κωδικό:</p>"
        ),
        cta_url=reset_url,
        cta_text="Επαναφορά Κωδικού",
        expiry_note="Ο σύνδεσμος λήγει σε 1 ώρα.",
        security_note="Εάν δεν ζήτησες επαναφορά κωδικού, αγνόησε αυτό το email. Ο κωδικός σου παραμένει αμετάβλητος.",
    )

    text_body = f"""ScanmyData - Επαναφορά Κωδικού

Γεια σου {user_username},

Λάβαμε αίτημα για επαναφορά του κωδικού σου στο ScanmyData.

Για να ορίσεις νέο κωδικό, κάνε κλικ στο link:
{reset_url}

Ο σύνδεσμος λήγει σε 1 ώρα.
Αν δεν ζήτησες επαναφορά κωδικού, αγνόησε αυτό το email.

Τμήμα Εξυπηρέτησης Πελατών
ScanmyData"""

    return send_email(user_email, 'Επαναφορά Κωδικού - ScanmyData', html_body, text_body)


def _strip_html_to_text(html: str) -> str:
    """Very simple HTML -> text fallback for email plain-text bodies."""
    try:
        import re
        # Remove tags and unescape basic entities
        text = re.sub(r'<[^>]+>', '', html)
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        return text.strip()
    except Exception:
        return html


def send_bulk_email_to_users(user_ids: list, subject: str, html_body: str, text_body: Optional[str] = None) -> dict:
    """Send email to multiple users (admin function)"""
    from models import User
    
    results = {'sent': 0, 'failed': 0, 'errors': []}
    
    for uid in user_ids:
        try:
            user = User.query.get(uid)
            if not user or not user.email:
                results['failed'] += 1
                results['errors'].append(f'User {uid}: no email')
                continue
            
            # Provide a plain-text fallback for better deliverability
            text = text_body or _strip_html_to_text(html_body)
            if send_email(user.email, subject, html_body, text):
                results['sent'] += 1
            else:
                results['failed'] += 1
                results['errors'].append(f'User {uid}: send failed')
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f'User {uid}: {str(e)}')
    
    return results


def _resend_store_file_path() -> str:
    """Resolve persistent store for already-forwarded inbound email IDs."""
    path = RESEND_INBOUND_STORE_FILE.strip() if RESEND_INBOUND_STORE_FILE else ''
    if not path:
        path = 'data/system/resend_inbound_forwarded_ids.json'
    if os.path.isabs(path):
        return path
    return os.path.join(os.getcwd(), path)


def _load_forwarded_inbound_ids() -> set:
    """Load forwarded inbound email IDs from local JSON file."""
    try:
        import json

        path = _resend_store_file_path()
        if not os.path.exists(path):
            return set()
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        if isinstance(payload, list):
            return {str(x).strip() for x in payload if str(x).strip()}
        if isinstance(payload, dict):
            ids = payload.get('ids') or []
            return {str(x).strip() for x in ids if str(x).strip()}
        return set()
    except Exception as e:
        logger.warning(f"Failed to load resend inbound forward store: {e}")
        return set()


def _save_forwarded_inbound_ids(ids: set) -> None:
    """Persist forwarded inbound email IDs to local JSON file."""
    try:
        import json

        path = _resend_store_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'ids': sorted(list(ids))[-5000:],
        }
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save resend inbound forward store: {e}")


def _extract_inbound_field(data: dict, *keys: str) -> str:
    """Extract first non-empty inbound value from a dict using fallback keys."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            email = str(value.get('email') or '').strip()
            name = str(value.get('name') or '').strip()
            if email and name:
                return f"{name} <{email}>"
            if email:
                return email
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    email = str(item.get('email') or '').strip()
                    name = str(item.get('name') or '').strip()
                    if email and name:
                        parts.append(f"{name} <{email}>")
                    elif email:
                        parts.append(email)
            if parts:
                return ', '.join(parts)
    return ''


def _build_inbound_forward_bodies(email_id: str, details: dict) -> tuple[str, str, str]:
    """Build subject/html/text for SMTP forwarding of a received inbound email."""
    subject = _extract_inbound_field(details, 'subject') or '(χωρίς θέμα)'
    from_value = _extract_inbound_field(details, 'from', 'from_email', 'sender') or 'unknown'
    to_value = _extract_inbound_field(details, 'to') or 'unknown'
    received_at = _extract_inbound_field(details, 'created_at', 'received_at', 'date') or datetime.now(timezone.utc).isoformat()
    text_content = _extract_inbound_field(details, 'text', 'text_body', 'plain')
    html_content = _extract_inbound_field(details, 'html', 'html_body')

    if not text_content and html_content:
        text_content = _strip_html_to_text(html_content)

    forward_subject = f"[Inbound Reply] {subject}"

    html_body = f"""<!DOCTYPE html>
<html lang="el">
<body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;padding:18px;">
  <h3 style="margin:0 0 12px;">Νέο εισερχόμενο email (Resend Receiving)</h3>
  <p style="margin:0 0 10px;"><strong>From:</strong> {from_value}<br>
     <strong>To:</strong> {to_value}<br>
     <strong>Subject:</strong> {subject}<br>
     <strong>Received:</strong> {received_at}<br>
     <strong>Email ID:</strong> {email_id}
  </p>
  <hr style="border:none;border-top:1px solid #ddd;margin:14px 0;">
  <div style="white-space:pre-wrap;">{(text_content or '(χωρίς σώμα κειμένου)').replace('<', '&lt;').replace('>', '&gt;')}</div>
</body>
</html>"""

    text_body = (
        "Νέο εισερχόμενο email (Resend Receiving)\n\n"
        f"From: {from_value}\n"
        f"To: {to_value}\n"
        f"Subject: {subject}\n"
        f"Received: {received_at}\n"
        f"Email ID: {email_id}\n\n"
        f"{text_content or '(χωρίς σώμα κειμένου)'}"
    )

    return forward_subject, html_body, text_body


def _validate_inbound_forward_prerequisites() -> tuple[bool, str, str]:
    """Validate required configuration for inbound -> SMTP forwarding."""
    enabled = str(RESEND_INBOUND_FORWARD_ENABLED or 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    if not enabled:
        return False, '', 'Forwarding is disabled by RESEND_INBOUND_FORWARD_ENABLED.'

    target = (RESEND_INBOUND_FORWARD_TO or SMTP_USER or '').strip()
    if not target:
        return False, '', 'No forwarding target configured (RESEND_INBOUND_FORWARD_TO or SMTP_USER).'

    if not SMTP_USER or not SMTP_PASSWORD:
        return False, target, 'SMTP credentials are missing; cannot forward inbound emails.'

    if not RESEND_API_KEY:
        return False, target, 'RESEND_API_KEY missing; cannot fetch inbound emails.'

    return True, target, ''


def forward_specific_resend_inbound_email(email_id: str) -> dict:
    """Forward a specific inbound email from Resend Receiving API to SMTP target."""
    result = {
        'enabled': True,
        'checked': 0,
        'forwarded': 0,
        'skipped_existing': 0,
        'failed': 0,
        'errors': [],
        'target': RESEND_INBOUND_FORWARD_TO or SMTP_USER,
        'email_id': str(email_id or '').strip(),
    }

    ok, target, err = _validate_inbound_forward_prerequisites()
    result['target'] = target or result['target']
    if not ok:
        if err.startswith('Forwarding is disabled'):
            result['enabled'] = False
            return result
        result['failed'] = 1
        result['errors'].append(err)
        return result

    email_id = str(email_id or '').strip()
    if not email_id:
        result['failed'] = 1
        result['errors'].append('Missing email_id for specific inbound forward.')
        return result

    forwarded_ids = _load_forwarded_inbound_ids()
    result['checked'] = 1
    if email_id in forwarded_ids:
        result['skipped_existing'] = 1
        return result

    try:
        import resend

        resend.api_key = RESEND_API_KEY
        details = resend.Emails.Receiving.get(email_id=email_id)
        if not isinstance(details, dict):
            details = dict(details) if details is not None else {}

        forward_subject, forward_html, forward_text = _build_inbound_forward_bodies(email_id, details)
        sent = send_smtp_email(target, forward_subject, forward_html, forward_text)
        if not sent:
            result['failed'] = 1
            result['errors'].append(f'Failed to forward inbound email {email_id} via SMTP.')
            return result

        forwarded_ids.add(email_id)
        _save_forwarded_inbound_ids(forwarded_ids)
        result['forwarded'] = 1
        return result
    except ImportError:
        result['failed'] = 1
        result['errors'].append('Resend SDK not installed. Run: pip install resend')
        return result
    except Exception as e:
        result['failed'] = 1
        result['errors'].append(f'Unhandled error during specific inbound forwarding: {e}')
        return result


def forward_resend_inbound_to_smtp_user(limit: int = 25) -> dict:
    """Fetch inbound emails from Resend Receiving API and forward new ones to SMTP_USER.

    Uses a local dedup store so each received email ID is forwarded once.
    """
    result = {
        'enabled': True,
        'checked': 0,
        'forwarded': 0,
        'skipped_existing': 0,
        'failed': 0,
        'errors': [],
        'target': RESEND_INBOUND_FORWARD_TO or SMTP_USER,
    }

    ok, target, err = _validate_inbound_forward_prerequisites()
    result['target'] = target or result['target']
    if not ok:
        if err.startswith('Forwarding is disabled'):
            result['enabled'] = False
            return result
        result['failed'] = 1
        result['errors'].append(err)
        return result

    try:
        import resend

        resend.api_key = RESEND_API_KEY

        # SDK versions differ; try with limit first, then fallback.
        try:
            listing = resend.Emails.Receiving.list(params={'limit': max(1, min(int(limit), 100))})
        except Exception:
            listing = resend.Emails.Receiving.list()

        data = []
        if isinstance(listing, dict):
            data = listing.get('data') or []
        else:
            data = getattr(listing, 'data', []) or []

        forwarded_ids = _load_forwarded_inbound_ids()

        for item in data:
            email_id = ''
            if isinstance(item, dict):
                email_id = str(item.get('id') or '').strip()
            else:
                email_id = str(getattr(item, 'id', '') or '').strip()
            if not email_id:
                continue

            result['checked'] += 1
            if email_id in forwarded_ids:
                result['skipped_existing'] += 1
                continue

            try:
                details = resend.Emails.Receiving.get(email_id=email_id)
                if not isinstance(details, dict):
                    details = dict(details) if details is not None else {}
            except Exception as e:
                result['failed'] += 1
                result['errors'].append(f'Failed to fetch inbound email {email_id}: {e}')
                continue

            forward_subject, forward_html, forward_text = _build_inbound_forward_bodies(email_id, details)
            ok = send_smtp_email(target, forward_subject, forward_html, forward_text)
            if ok:
                forwarded_ids.add(email_id)
                result['forwarded'] += 1
            else:
                result['failed'] += 1
                result['errors'].append(f'Failed to forward inbound email {email_id} via SMTP.')

        _save_forwarded_inbound_ids(forwarded_ids)
        return result

    except ImportError:
        result['failed'] = 1
        result['errors'].append('Resend SDK not installed. Run: pip install resend')
        return result
    except Exception as e:
        result['failed'] += 1
        result['errors'].append(f'Unhandled error during inbound forwarding: {e}')
        return result
