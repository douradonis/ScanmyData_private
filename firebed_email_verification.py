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
            subject = "Επιβεβαίωση Email - ScanmyData"

            from email_utils import make_email_html
            display = display_name or email.split('@')[0]
            body_html = f"""
            <p style='margin:0 0 14px; font-size: 15px;'>Γεια σου <strong>{display}</strong>! 👋</p>
            <p style='margin:0 0 14px;'>Σε ευχαριστούμε που εγγράφηκες στο <strong>ScanmyData</strong>! Για να ενεργοποιήσεις τον λογαριασμό σου και να έχεις πρόσβαση σε όλες τις δυνατότητες, χρειάζεται να επιβεβαιώσεις το email σου.</p>
            <p style='margin:0 0 10px;font-weight:600;'>✅ Επιβεβαίωση Email</p>
            <ul style='margin:0 0 14px;padding-left:20px;line-height:1.5;'>
              <li>Θα ενεργοποιηθεί ο λογαριασμός σου</li>
              <li>Θα μπορείς να κάνεις login</li>
              <li>Θα έχεις πρόσβαση στο dashboard</li>
              <li>Θα λαμβάνεις σημαντικές ενημερώσεις</li>
            </ul>
            <p style='margin:0 0 14px;color:#666;font-size:13px;'>🔒 Ασφάλεια: Αν δεν δημιούργησες εσύ αυτόν τον λογαριασμό, απλά αγνόησε αυτό το email. Ο λογαριασμός δεν θα ενεργοποιηθεί χωρίς επιβεβαίωση.</p>
            <p style='margin:0 0 10px;font-size:13px;'>Δεν μπορείς να κάνεις κλικ στο κουμπί; Αντίγραψε και επικόλλησε αυτό το link στον browser σου:</p>
            <p style='margin:0 0 14px;font-size:13px;word-break:break-all;'><a href='{verify_url}' style='color:#1a56db;text-decoration:none;'>{verify_url}</a></p>
            """

            html_body = make_email_html(
                greeting="Καλώς ήρθες στο ScanmyData!",
                body_html=body_html,
                cta_url=verify_url,
                cta_text="Επιβεβαίωση Email",
                expiry_note="Το link επιβεβαίωσης ισχύει για 24 ώρες.",
                security_note="Αν δεν δημιούργησες εσύ αυτόν τον λογαριασμό, αγνόησε αυτό το email.",
                header_subtitle='📧 Μήνυμα από Διαχειριστή',
            )
            
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
            
            subject = "Επαναφορά Κωδικού - ScanmyData"

            from email_utils import make_email_html
            logo_url = f"{base_url}/icons/scanmydata_logo_3000w.png"
            html_body = make_email_html(
                greeting="Γεια σας,",
                body_html=(
                    "<p style='margin:0 0 14px;'>Λάβαμε αίτημα για επαναφορά του κωδικού στο"
                    " <strong>ScanmyData</strong>. Κάντε κλικ στον παρακάτω σύνδεσμο για να ορίσετε νέο κωδικό:"
                    "</p>"
                ),
                cta_url=reset_url,
                cta_text="Επαναφορά Κωδικού",
                expiry_note="Ο σύνδεσμος λήγει σε 1 ώρα.",
                security_note="Εάν δεν ζήτησες εσύ επαναφορά, αγνόησε αυτό το email. Ο κωδικός σου παραμένει αμετάβλητος.",
                header_subtitle='🔐 Επαναφορά Κωδικού',
                logo_url=logo_url,
            )
            
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