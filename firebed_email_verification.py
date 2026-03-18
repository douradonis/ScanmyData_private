"""
Enhanced Firebase Email Verification Integration για Firebed
Ενσωματώνει Firebase Authentication με Firebed email system
Υποστηρίζει email verification και password reset με custom templates
"""

import logging
import os
import secrets
import hashlib
import json
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timezone, timedelta
from firebase_admin import auth as firebase_auth
import firebase_config
from email_utils import send_email

logger = logging.getLogger(__name__)


class FirebedEmailVerification:
    """Enhanced email verification system για Firebed με Firebase integration"""
    
    @staticmethod
    def is_admin_email(email: str) -> bool:
        """
        Ελέγχει αν το email είναι admin και δεν χρειάζεται επιβεβαίωση
        """
        admin_emails = [
            'adonis.douramanis@gmail.com',
            os.getenv('ADMIN_EMAIL', '').strip(),
            os.getenv('SENDER_EMAIL', '').strip()  # SMTP sender email
        ]
        # Remove empty strings
        admin_emails = [email.lower() for email in admin_emails if email]
        return email.lower() in admin_emails
    
    @staticmethod
    def get_base_url() -> str:
        """
        Δυναμικός προσδιορισμός του base URL για το application
        Υποστηρίζει Render deployment, Flask request context, και fallbacks
        """
        try:
            # 1. Προσπάθησε να πάρεις από Flask request context
            try:
                from flask import request
                if request and hasattr(request, 'url_root'):
                    base_url = request.url_root.rstrip('/')
                    logger.info(f"Using Flask request base URL: {base_url}")
                    return base_url
            except (ImportError, RuntimeError):
                # Εκτός Flask context ή δεν είναι διαθέσιμη
                pass
            
            # 2. GitHub Codespaces detection
            codespace_name = os.getenv('CODESPACE_NAME')
            github_codespaces_port_forwarding_domain = os.getenv('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN')
            if codespace_name and github_codespaces_port_forwarding_domain:
                base_url = f"https://{codespace_name}-5000.{github_codespaces_port_forwarding_domain}"
                logger.info(f"Using GitHub Codespaces URL: {base_url}")
                return base_url
            
            # 3. Render deployment - χρησιμοποιεί RENDER_EXTERNAL_URL
            render_url = os.getenv('RENDER_EXTERNAL_URL')
            if render_url:
                base_url = render_url.rstrip('/')
                logger.info(f"Using Render external URL: {base_url}")
                return base_url
            
            # 4. Custom APP_URL από environment
            app_url = os.getenv('APP_URL')
            if app_url and app_url != 'http://localhost:5000':
                base_url = app_url.rstrip('/')
                logger.info(f"Using custom APP_URL: {base_url}")
                return base_url
            
            # 4. Fallback για development
            fallback_url = 'http://localhost:5000'
            logger.warning(f"Using fallback URL: {fallback_url}")
            return fallback_url
            
        except Exception as e:
            logger.error(f"Error determining base URL: {e}")
            return 'http://localhost:5000'
    
    @staticmethod
    def create_verification_token(email: str, token_type: str = 'email_verify', expires_hours: int = 24) -> Optional[str]:
        """
        Δημιουργεί verification token για email
        """
        try:
            # Create token data
            token_data = {
                'email': email,
                'type': token_type,
                'created': datetime.now(timezone.utc).isoformat(),
                'expires': (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat(),
                'random': secrets.token_hex(16)
            }
            
            # Encode token
            token_json = json.dumps(token_data, sort_keys=True)
            token_bytes = token_json.encode('utf-8')
            
            # Create hash for verification
            secret_key = os.getenv('FLASK_SECRET', 'dev-secret-key')
            token_hash = hashlib.pbkdf2_hmac('sha256', token_bytes, secret_key.encode(), 100000)
            
            # Combine data and hash
            import base64
            combined = base64.b64encode(token_bytes + token_hash).decode('ascii')
            
            return combined
            
        except Exception as e:
            logger.error(f"Error creating verification token: {e}")
            return None
    
    @staticmethod
    def verify_token(token: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Επιβεβαιώνει verification token
        Returns: (email, token_type) or (None, None) if invalid
        """
        try:
            import base64
            
            # Decode token
            combined = base64.b64decode(token.encode('ascii'))
            
            # Split data and hash (hash is last 32 bytes)
            token_bytes = combined[:-32]
            provided_hash = combined[-32:]
            
            # Verify hash
            secret_key = os.getenv('FLASK_SECRET', 'dev-secret-key')
            expected_hash = hashlib.pbkdf2_hmac('sha256', token_bytes, secret_key.encode(), 100000)
            
            if provided_hash != expected_hash:
                logger.warning("Token verification failed: Invalid hash")
                return None, None
            
            # Parse token data
            token_json = token_bytes.decode('utf-8')
            token_data = json.loads(token_json)
            
            # Check expiration
            expires = datetime.fromisoformat(token_data['expires'])
            if datetime.now(timezone.utc) > expires:
                logger.warning("Token verification failed: Expired")
                return None, None
            
            return token_data['email'], token_data['type']
            
        except Exception as e:
            logger.error(f"Error verifying token: {e}")
            return None, None
    
    @staticmethod
    def send_signup_verification_email(email: str, display_name: str = "") -> bool:
        """
        Στέλνει verification email για νέο χρήστη
        Χρησιμοποιεί custom Firebed templates αντί για Firebase defaults
        """
        try:
            # Δημιουργία verification token
            token = FirebedEmailVerification.create_verification_token(email, 'email_verify')
            if not token:
                logger.error(f"Failed to create verification token for {email}")
                return False
            
            # Verification URL
            base_url = FirebedEmailVerification.get_base_url()
            verify_url = f"{base_url}/firebase-auth/verify-email?token={token}"
            
            # Greek subject and body
            subject = "✅ Επιβεβαίωση Email - ScanmyData Account"
            
            # Logo URL - prefer the email-specific logo for transactional emails
            logo_url = f"{base_url}/icons/scanmydata_logo_email.png"
            
            # HTML Email Template
            html_body = f"""
            <!DOCTYPE html>
            <html lang="el">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Επιβεβαίωση Email - ScanmyData</title>
                <style>
                    body {{ 
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                        line-height: 1.6;
                        color: #0f172a;
                        max-width: 600px;
                        margin: 0 auto;
                        padding: 20px;
                        background-color: #f8f9fa;
                    }}
                    .container {{
                        background: white;
                        padding: 40px;
                        border-radius: 12px;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                    }}
                    .header {{
                        text-align: center;
                        margin-bottom: 30px;
                        border-bottom: 3px solid #e74c3c;
                        padding-bottom: 20px;
                    }}
                    .logo {{
                        font-size: 28px;
                        font-weight: bold;
                        color: #e74c3c;
                        margin-bottom: 10px;
                    }}
                    .welcome {{
                        font-size: 18px;
                        color: #2c3e50;
                        margin-bottom: 20px;
                    }}
                    .verify-btn {{
                        display: inline-block;
                        background-color: #e74c3c;
                        background-image: linear-gradient(135deg, #e74c3c, #c0392b);
                        color: #ffffff !important;
                        padding: 15px 30px;
                        text-decoration: none !important;
                        border-radius: 8px;
                        font-weight: bold;
                        text-align: center;
                        margin: 20px 0;
                        transition: all 0.3s ease;
                    }}
                    .verify-btn:hover {{
                        background-color: #c0392b;
                        background-image: linear-gradient(135deg, #c0392b, #a93226);
                        transform: translateY(-2px);
                    }}
                    .info-box {{
                        background: #f8f9fa;
                        border-left: 4px solid #3498db;
                        padding: 15px;
                        margin: 20px 0;
                        border-radius: 4px;
                    }}
                    .footer {{
                        margin-top: 30px;
                        padding-top: 20px;
                        border-top: 1px solid #eee;
                        text-align: center;
                        color: #666;
                        font-size: 14px;
                    }}
                    .security-note {{
                        background: #fff3cd;
                        border: 1px solid #ffeaa7;
                        padding: 15px;
                        border-radius: 6px;
                        margin: 20px 0;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <div style="text-align: center; margin-bottom: 20px;">
                            <img src="{logo_url}" alt="ScanmyData" style="height: 80px; width: auto;">
                        </div>
                        <h2 style="color: #0ea5e9; margin: 0; text-align: center;">Καλώς ήρθες στο ScanmyData!</h2>
                    </div>
                    
                    <div class="welcome">
                        Γεια σου {display_name or email.split('@')[0]}! 👋
                    </div>
                    
                    <p>
                        Σε ευχαριστούμε που εγγράφηκες στο <strong>ScanmyData</strong>! 
                        Για να ενεργοποιήσεις τον λογαριασμό σου και να έχεις πρόσβαση 
                        σε όλες τις δυνατότητες, χρειάζεται να επιβεβαιώσεις το email σου.
                    </p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{verify_url}" class="verify-btn" style="background-color:#e74c3c;color:#ffffff;text-decoration:none;display:inline-block;padding:15px 30px;border-radius:8px;">
                            ✅ Επιβεβαίωση Email
                        </a>
                    </div>
                    
                    <div class="info-box">
                        <strong>📧 Τι θα συμβεί μετά:</strong><br>
                        • Θα ενεργοποιηθεί ο λογαριασμός σου<br>
                        • Θα μπορείς να κάνεις login<br>
                        • Θα έχεις πρόσβαση στο dashboard<br>
                        • Θα λαμβάνεις σημαντικές ενημερώσεις
                    </div>
                    
                    <div class="security-note">
                        <strong>🔒 Ασφάλεια:</strong> Αν δεν δημιούργησες εσύ αυτόν τον λογαριασμό, 
                        απλά αγνόησε αυτό το email. Ο λογαριασμός δεν θα ενεργοποιηθεί χωρίς επιβεβαίωση.
                    </div>
                    
                    <p style="color: #666; font-size: 14px;">
                        <strong>Δεν μπορείς να κάνεις κλικ στο κουμπί;</strong><br>
                        Αντίγραψε και επικόλλησε αυτό το link στον browser σου:<br>
                        <a href="{verify_url}" style="color: #e74c3c; word-break: break-all;">{verify_url}</a>
                    </p>
                    
                    <div class="footer">
                        <div style="text-align: center; margin-bottom: 15px;">
                            <img src="{logo_url}" alt="ScanmyData" style="height: 50px; width: auto; opacity: 0.6;">
                        </div>
                        <p>
                            <strong>ScanmyData Team</strong><br>
                            Αυτό το email στάλθηκε στις {datetime.now().strftime('%d/%m/%Y %H:%M')} ΕΕΤ
                        </p>
                        <p style="font-size: 12px; color: #999;">
                            Το link επιβεβαίωσης ισχύει για 24 ώρες
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Plain text fallback
            text_body = f"""
ScanmyData - Επιβεβαίωση Email

Γεια σου {display_name or email.split('@')[0]}!

Σε ευχαριστούμε που εγγράφηκες στο ScanmyData!
Για να ενεργοποιήσεις τον λογαριασμό σου, κάνε κλικ στο παρακάτω link:

{verify_url}

Τι θα συμβεί μετά:
✅ Θα ενεργοποιηθεί ο λογαριασμός σου
✅ Θα μπορείς να κάνεις login  
✅ Θα έχεις πρόσβαση στο dashboard

🔒 Ασφάλεια: Αν δεν δημιούργησες εσύ αυτόν τον λογαριασμό, αγνόησε αυτό το email.

ScanmyData Team
Αποστολή: {datetime.now().strftime('%d/%m/%Y %H:%M')} ΕΕΤ
Το link ισχύει για 24 ώρες.
            """
            
            # Send email
            success = send_email(email, subject, html_body, text_body)
            
            if success:
                logger.info(f"Verification email sent successfully to {email}")
                
                # Log στο Firebase
                try:
                    firebase_config.firebase_log_activity(
                        email,
                        'system',
                        'verification_email_sent',
                        {'email': email, 'timestamp': datetime.now(timezone.utc).isoformat()}
                    )
                except Exception as e:
                    logger.warning(f"Failed to log verification email activity: {e}")
                
                return True
            else:
                logger.error(f"Failed to send verification email to {email}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending verification email to {email}: {e}")
            return False
    
    @staticmethod  
    def send_password_reset_email(email: str) -> bool:
        """
        Στέλνει password reset email με custom Firebed template
        """
        try:
            # Ελέγχουμε αν υπάρχει ο χρήστης στο Firebase
            try:
                user = firebase_auth.get_user_by_email(email)
                if not user:
                    logger.warning(f"Password reset requested for non-existent user: {email}")
                    return False
            except firebase_auth.UserNotFoundError:
                logger.warning(f"Password reset requested for non-existent user: {email}")
                return False
            
            # Δημιουργία reset token
            token = FirebedEmailVerification.create_verification_token(email, 'password_reset', expires_hours=1)
            if not token:
                logger.error(f"Failed to create password reset token for {email}")
                return False
            
            # Reset URL
            base_url = FirebedEmailVerification.get_base_url()
            reset_url = f"{base_url}/firebase-auth/reset-password?token={token}"
            
            subject = "🔐 Επαναφορά Κωδικού - ScanmyData Account"
            
            # HTML Email Template
            html_body = f"""
            <!DOCTYPE html>
            <html lang="el">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Επαναφορά Κωδικού - ScanmyData</title>
                <style>
                    body {{ 
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                        line-height: 1.6;
                        color: #333;
                        max-width: 600px;
                        margin: 0 auto;
                        padding: 20px;
                        background-color: #f8f9fa;
                    }}
                    .container {{
                        background: white;
                        padding: 40px;
                        border-radius: 12px;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                    }}
                    .header {{
                        text-align: center;
                        margin-bottom: 30px;
                        border-bottom: 3px solid #f1f5f9;
                        padding-bottom: 20px;
                    }}
                    .logo {{
                        font-size: 28px;
                        font-weight: bold;
                        color: #0f172a;
                        margin-bottom: 10px;
                    }}
                    .reset-btn {{
                        display: inline-block;
                        background-color: #ff6b6b;
                        background-image: linear-gradient(135deg, #ff6b6b, #ee5a24);
                        color: #ffffff !important;
                        padding: 14px 28px;
                        text-decoration: none !important;
                        border-radius: 8px;
                        font-weight: bold;
                        text-align: center;
                        margin: 20px 0;
                        border: 2px solid #ee5a24;
                    }}
                    .warning-box {{
                        background: #fff3cd;
                        border: 1px solid #ffeaa7;
                        padding: 15px;
                        border-radius: 6px;
                        margin: 20px 0;
                    }}
                    .info-box {{
                        background: #e8f4fd;
                        border-left: 4px solid #3498db;
                        padding: 15px;
                        margin: 20px 0;
                        border-radius: 4px;
                    }}
                    .footer {{
                        margin-top: 30px;
                        padding-top: 20px;
                        border-top: 1px solid #eee;
                        text-align: center;
                        color: #666;
                        font-size: 14px;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <div style="text-align: center; margin-bottom: 20px;">
                            <img src="{logo_url}" alt="ScanmyData" style="height: 80px; width: auto;">
                        </div>
                        <h2 style="color: #f39c12; margin: 0; text-align: center;">Επαναφορά Κωδικού - ScanmyData</h2>
                    </div>
                    
                    <p>
                        Λάβαμε αίτημα για επαναφορά του κωδικού για τον λογαριασμό σου στο <strong>ScanmyData</strong>.
                    </p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{reset_url}" class="reset-btn" style="background-color:#ff6b6b;color:#ffffff;text-decoration:none;display:inline-block;padding:14px 28px;border-radius:8px;">
                            🔐 Επαναφορά Κωδικού
                        </a>
                    </div>
                    
                    <div class="info-box">
                        <strong>📋 Διαδικασία Επαναφοράς:</strong><br>
                        1. Κάνε κλικ στο κουμπί παραπάνω<br>
                        2. Εισάγαγε νέο κωδικό (τουλάχιστον 6 χαρακτήρες)<br>
                        3. Επιβεβαίωσε τον νέο κωδικό<br>
                        4. Κάνε login με τα νέα στοιχεία
                    </div>
                    
                    <div class="warning-box">
                        <strong>⚠️ Σημαντικό:</strong><br>
                        • Το link ισχύει για 1 ώρα από την αποστολή<br>
                        • Αν δεν ζήτησες εσύ επαναφορά, αγνόησε αυτό το email<br>
                        • Ο κωδικός σου δεν θα αλλάξει χωρίς την επιβεβαίωσή σου
                    </div>
                    
                    <p style="color: #666; font-size: 14px;">
                        <strong>Δεν μπορείς να κάνεις κλικ στο κουμπί;</strong><br>
                        Αντίγραψε και επικόλλησε αυτό το link:<br>
                        <a href="{reset_url}" style="color: #f39c12; word-break: break-all;">{reset_url}</a>
                    </p>
                    
                    <div class="footer">
                        <div style="text-align: center; margin-bottom: 15px;">
                            <img src="{logo_url}" alt="ScanmyData" style="height: 50px; width: auto; opacity: 0.6;">
                        </div>
                        <p>
                            <strong>ScanmyData Security Team</strong><br>
                            Αποστολή: {datetime.now().strftime('%d/%m/%Y %H:%M')} ΕΕΤ
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Logo URL for password reset
            logo_url = f"{base_url}/icons/scanmydata_logo_email.png"
            
            # Plain text version
            text_body = f"""
ScanmyData - Επαναφορά Κωδικού

Λάβαμε αίτημα για επαναφορά του κωδικού σου.

Για να ορίσεις νέο κωδικό, κάνε κλικ στο link:
{reset_url}

Διαδικασία:
1. Κάνε κλικ στο link
2. Εισάγαγε νέο κωδικό  
3. Επιβεβαίωσε τον κωδικό
4. Login με τα νέα στοιχεία

⚠️ Σημαντικό:
• Το link ισχύει για 1 ώρα
• Αν δεν ζήτησες επαναφορά, αγνόησε το email

ScanmyData Security Team
{datetime.now().strftime('%d/%m/%Y %H:%M')} ΕΕΤ
            """
            
            # Send email
            success = send_email(email, subject, html_body, text_body)
            
            if success:
                logger.info(f"Password reset email sent to {email}")
                
                # Log activity
                try:
                    firebase_config.firebase_log_activity(
                        email,
                        'system', 
                        'password_reset_email_sent',
                        {'email': email, 'timestamp': datetime.now(timezone.utc).isoformat()}
                    )
                except Exception as e:
                    logger.warning(f"Failed to log password reset activity: {e}")
                
                return True
            else:
                logger.error(f"Failed to send password reset email to {email}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending password reset email to {email}: {e}")
            return False
    
    @staticmethod
    def verify_email_token(token: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Επιβεβαιώνει email verification token και ενεργοποιεί Firebase user
        Returns: (success, email, error_message)
        """
        try:
            # Verify token
            email, token_type = FirebedEmailVerification.verify_token(token)
            if not email or token_type != 'email_verify':
                return False, None, "Μη έγκυρος ή ληγμένος σύνδεσμος επιβεβαίωσης"
            
            # Get Firebase user
            try:
                user = firebase_auth.get_user_by_email(email)
                
                # Update email verification status
                firebase_auth.update_user(user.uid, email_verified=True)
                
                # Update user data in Realtime Database
                firebase_config.firebase_write_data(
                    f'/users/{user.uid}/email_verified', 
                    True
                )
                firebase_config.firebase_write_data(
                    f'/users/{user.uid}/verified_at', 
                    datetime.now(timezone.utc).isoformat()
                )
                
                logger.info(f"Email verified successfully for {email}")
                
                # Log activity
                firebase_config.firebase_log_activity(
                    user.uid,
                    'user',
                    'email_verified', 
                    {'email': email, 'verified_at': datetime.now(timezone.utc).isoformat()}
                )
                
                return True, email, None
                
            except firebase_auth.UserNotFoundError:
                logger.error(f"User not found for email verification: {email}")
                return False, None, "Ο χρήστης δεν βρέθηκε"
                
        except Exception as e:
            logger.error(f"Error verifying email token: {e}")
            return False, None, f"Σφάλμα επιβεβαίωσης: {str(e)}"
    
    @staticmethod
    def is_email_verified(email: str) -> bool:
        """Ελέγχει αν το email έχει επιβεβαιωθεί"""
        try:
            user = firebase_auth.get_user_by_email(email)
            return user.email_verified
        except firebase_auth.UserNotFoundError:
            return False
        except Exception as e:
            logger.error(f"Error checking email verification status: {e}")
            return False