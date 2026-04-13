"""
Firebase Authentication Routes for Firebed Private
Routes for signup, login, logout, password reset via Firebase
"""

import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, current_app
from flask_login import login_user, logout_user, current_user, login_required
from datetime import datetime, timezone
import firebase_config
from firebase_auth_handlers import FirebaseAuthHandler
from firebed_email_verification import FirebedEmailVerification
from models import db, User, Group, UserGroup
from sqlalchemy.exc import IntegrityError
import secrets
import utils

logger = logging.getLogger(__name__)

firebase_auth_bp = Blueprint('firebase_auth', __name__, url_prefix='/firebase-auth')


@firebase_auth_bp.route('/verify-email')
def verify_email():
    """Handle email verification from link"""
    token = request.args.get('token')
    if not token:
        flash('❌ Μη έγκυρος σύνδεσμος επιβεβαίωσης.', 'danger')
        return redirect(url_for('firebase_auth.firebase_login'))
    
    success, email, error = FirebedEmailVerification.verify_email_token(token)
    
    if success:
        flash(f'✅ Το email {email} επιβεβαιώθηκε επιτυχώς! Μπορείτε τώρα να συνδεθείτε.', 'success')
        return redirect(url_for('firebase_auth.firebase_login'))
    else:
        flash(f'❌ {error or "Αποτυχία επιβεβαίωσης email"}', 'danger')
        return redirect(url_for('firebase_auth.firebase_login'))


@firebase_auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Forgot password page and handler"""
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        if not email or '@' not in email:
            flash('❌ Παρακαλώ εισάγετε έγκυρο email.', 'danger')
            return redirect(url_for('firebase_auth.forgot_password'))
        
        # Send password reset email
        success = FirebedEmailVerification.send_password_reset_email(email)
        
        if success:
            flash('📧 Οδηγίες επαναφοράς κωδικού στάλθηκαν στο email σας!', 'success')
        else:
            flash('❌ Το email δεν βρέθηκε ή υπήρξε σφάλμα.', 'danger')
        # Log forgot password request
        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=email,  # use email as identifier if user id not resolved
                group_name='system',
                action='forgot_password_request',
                details={
                    'email': email,
                    'success': success,
                    'description': 'Αίτημα επαναφοράς κωδικού'
                },
                user_email=email,
                user_username=email
            )
        except Exception:
            pass
        
        return redirect(url_for('firebase_auth.firebase_login'))
    
    return render_template('firebase_auth/forgot_password.html')


@firebase_auth_bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    """Password reset page and handler"""
    token = request.args.get('token') or request.form.get('token')
    
    if not token:
        flash('❌ Μη έγκυρος σύνδεσμος επαναφοράς κωδικού.', 'danger')
        return redirect(url_for('firebase_auth.firebase_login'))
    
    if request.method == 'POST':
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        
        if not new_password or len(new_password) < 6:
            flash('❌ Ο κωδικός πρέπει να έχει τουλάχιστον 6 χαρακτήρες.', 'danger')
            return render_template('firebase_auth/reset_password.html', token=token)
        
        if new_password != confirm_password:
            flash('❌ Οι κωδικοί δεν ταιριάζουν.', 'danger')
            return render_template('firebase_auth/reset_password.html', token=token)
        
        # Verify reset token
        try:
            email, token_type = FirebedEmailVerification.verify_token(token)
            if not email or token_type != 'password_reset':
                flash('❌ Μη έγκυρος ή ληγμένος σύνδεσμος επαναφοράς.', 'danger')
                return redirect(url_for('firebase_auth.firebase_login'))
            
            # Update password in Firebase
            from firebase_admin import auth as firebase_auth
            user = firebase_auth.get_user_by_email(email)
            firebase_auth.update_user(user.uid, password=new_password)
            
            logger.info(f"Password reset successful for {email}")
            
            # Log activity
            firebase_config.firebase_log_activity(
                user.uid,
                'user',
                'password_reset_completed',
                {'email': email, 'timestamp': datetime.now(timezone.utc).isoformat()}
            )
            try:
                from utils import log_user_activity
                log_user_activity(
                    user_id=user.uid,
                    group_name='system',
                    action='password_reset_completed',
                    details={'email': email, 'description': 'Ολοκλήρωση επαναφοράς κωδικού'},
                    user_email=email,
                    user_username=email
                )
            except Exception:
                pass
            
            flash('✅ Ο κωδικός επαναφέρθηκε επιτυχώς! Μπορείτε τώρα να συνδεθείτε.', 'success')
            return redirect(url_for('firebase_auth.firebase_login'))
            
        except Exception as e:
            logger.error(f"Password reset error: {e}")
            try:
                from utils import log_user_activity
                log_user_activity(
                    user_id=email if 'email' in locals() else 'unknown',
                    group_name='system',
                    action='password_reset_error',
                    details={'email': email if 'email' in locals() else None, 'error': str(e), 'description': 'Σφάλμα επαναφοράς κωδικού'},
                    user_email=email if 'email' in locals() else None,
                    user_username=email if 'email' in locals() else None
                )
            except Exception:
                pass
            flash('❌ Σφάλμα επαναφοράς κωδικού. Δοκιμάστε ξανά.', 'danger')
            return render_template('firebase_auth/reset_password.html', token=token)
    
    return render_template('firebase_auth/reset_password.html', token=token)


@firebase_auth_bp.route('/resend-verification', methods=['POST'])
def resend_verification():
    """Resend verification email"""
    email = request.form.get('email', '').strip()
    
    if not email or '@' not in email:
        flash('❌ Παρακαλώ εισάγετε έγκυρο email.', 'danger')
        return redirect(url_for('firebase_auth.firebase_login'))
    
    # Check if user exists and is not verified
    if FirebedEmailVerification.is_email_verified(email):
        flash('✅ Το email είναι ήδη επιβεβαιωμένο!', 'info')
        return redirect(url_for('firebase_auth.firebase_login'))
    
    # Send verification email
    success = FirebedEmailVerification.send_signup_verification_email(email)
    
    if success:
        flash('📧 Νέο email επιβεβαίωσης στάλθηκε!', 'success')
    else:
        flash('❌ Αποτυχία αποστολής email επιβεβαίωσης.', 'danger')
    
    return redirect(url_for('firebase_auth.firebase_login'))


@firebase_auth_bp.route('/signup', methods=['GET', 'POST'])
def firebase_signup():
    """Firebase signup page and handler"""
    if current_user.is_authenticated:
            return redirect(url_for('home'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        password_confirm = request.form.get('password_confirm', '').strip()
        display_name = request.form.get('display_name', '').strip()
        
        # Validate inputs
        if not email or not password:
            flash('Email and password are required', 'danger')
            return redirect(url_for('firebase_auth.firebase_signup'))
        
        if password != password_confirm:
            flash('Passwords do not match', 'danger')
            return redirect(url_for('firebase_auth.firebase_signup'))
        
        if len(password) < 6:
            flash('Password must be at least 6 characters', 'danger')
            return redirect(url_for('firebase_auth.firebase_signup'))
        
        # Register with Firebase
        success, uid, error = FirebaseAuthHandler.register_user(email, password, display_name)
        # Log signup attempt
        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=email,
                group_name='system',
                action='signup_request',
                details={'email': email, 'display_name': display_name, 'success': success, 'description': 'Αίτημα εγγραφής χρήστη'},
                user_email=email,
                user_username=email
            )
        except Exception:
            pass
        
        if not success:
            flash(f'Registration failed: {error}', 'danger')
            return redirect(url_for('firebase_auth.firebase_signup'))
        
        # Check if admin email - skip verification
        is_admin = FirebedEmailVerification.is_admin_email(email)
        verification_sent = False
        
        if not is_admin:
            # Send verification email for non-admin users
            verification_sent = FirebedEmailVerification.send_signup_verification_email(email, display_name)
            
            if not verification_sent:
                logger.warning(f"Failed to send verification email to {email}")
                flash('Η εγγραφή ολοκληρώθηκε, αλλά το email επιβεβαίωσης δεν στάλθηκε. Επικοινωνήστε με τον διαχειριστή.', 'warning')
        else:
            logger.info(f"Admin email {email} registered - skipping email verification")
        
        # Create local user entry. Store username as the full email to allow email-login.
        try:
            user = User.query.filter_by(username=email).first()
            if not user:
                user = User(
                    username=email,
                    pw_hash=uid,  # Store Firebase UID
                    email=email,
                    firebase_uid=uid
                )
                db.session.add(user)
                try:
                    db.session.commit()
                except IntegrityError:
                    # If another process created the user concurrently, roll back and fetch it.
                    db.session.rollback()
                    user = User.query.filter_by(username=email).first()
            
            # Log the registration
            firebase_config.firebase_log_activity(
                uid,
                'system',
                'user_signup_complete',
                {'email': email, 'verification_email_sent': verification_sent}
            )
            # Local structured log for unified admin panel
            try:
                from utils import log_user_activity
                log_user_activity(
                    user_id=user.id,
                    group_name='system',
                    action='signup_complete',
                    details={'email': email, 'verification_email_sent': verification_sent, 'description': 'Ολοκλήρωση εγγραφής χρήστη'},
                    user_email=email,
                    user_username=email
                )
            except Exception:
                pass
            
            if is_admin:
                flash('🎉 Η εγγραφή admin ολοκληρώθηκε! Μπορείτε να συνδεθείτε απευθείας.', 'success')
            elif verification_sent:
                flash('🎉 Η εγγραφή ολοκληρώθηκε! Ελέγξτε το email σας για επιβεβαίωση πριν συνδεθείτε.', 'success')
            else:
                flash('Η εγγραφή ολοκληρώθηκε! Μπορείτε να συνδεθείτε.', 'success')
            
            return redirect(url_for('firebase_auth.firebase_login'))
            
        except Exception as e:
            logger.error(f"Error creating local user: {e}")
            flash('Ο λογαριασμός δημιουργήθηκε στο Firebase, αλλά απέτυχε η τοπική ρύθμιση. Επικοινωνήστε με υποστήριξη.', 'warning')
            return redirect(url_for('firebase_auth.firebase_login'))
    
    return render_template('auth/signup.html')


@firebase_auth_bp.route('/login', methods=['GET', 'POST'])
def firebase_login():
    """Firebase login page and handler"""
    if current_user.is_authenticated:
            return redirect(url_for('home'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        
        if not email or not password:
            flash('Απαιτούνται email και κωδικός', 'danger')
            return redirect(url_for('firebase_auth.firebase_login'))
        
        # Support login by either email or username. If user provided a username (no @),
        # try to look up the local user and resolve the Firebase email.
        identifier = email
        firebase_email = None
        if '@' in identifier:
            firebase_email = identifier
        else:
            # treat as username: try to find local user and resolve their firebase email
            local = User.query.filter_by(username=identifier).first()
            if local:
                if getattr(local, 'email', None):
                    firebase_email = local.email
                elif getattr(local, 'pw_hash', None):
                    try:
                        fb_user = FirebaseAuthHandler.get_user_by_uid(local.pw_hash)
                        if fb_user and fb_user.get('email'):
                            firebase_email = fb_user.get('email')
                    except Exception:
                        pass

        # fallback: if we couldn't resolve an email, use the raw identifier (will likely fail)
        if not firebase_email:
            firebase_email = identifier

        # If we have a local user for this email/username, check session lock before calling Firebase
        try:
            from sqlalchemy import or_
            local_check_user = User.query.filter(or_(User.username == firebase_email, User.email == firebase_email)).first()
            if local_check_user:
                try:
                    if utils.is_session_locked(local_check_user.id):
                        logger.info(f"Blocked login attempt for user id={local_check_user.id} (locked)")
                        flash('Ο λογαριασμός είναι ήδη ενεργός σε άλλη συσκευή/σύνδεση.', 'warning')
                        return redirect(url_for('firebase_auth.firebase_login'))
                except Exception:
                    # on any failure, continue to auth (fail-open)
                    pass
        except Exception:
            local_check_user = None

        # Verify with Firebase
        success, uid, error = FirebaseAuthHandler.login_user(firebase_email, password)
        
        if not success:
            flash(f'Login failed: {error}', 'danger')
            return redirect(url_for('firebase_auth.firebase_login'))
        
        # Check email verification status (skip for admin emails)
        is_admin = FirebedEmailVerification.is_admin_email(firebase_email)
        
        if not is_admin and not FirebedEmailVerification.is_email_verified(firebase_email):
            flash('📧 Πρέπει να επιβεβαιώσετε το email σας πριν συνδεθείτε. Ελέγξτε το email σας για τον σύνδεσμο επιβεβαίωσης.', 'warning')
            
            # Option to resend verification email
            resend = request.form.get('resend_verification')
            if resend:
                verification_sent = FirebedEmailVerification.send_signup_verification_email(firebase_email)
                if verification_sent:
                    flash('📧 Νέο email επιβεβαίωσης στάλθηκε!', 'success')
                else:
                    flash('❌ Αποτυχία αποστολής email επιβεβαίωσης.', 'danger')
            
            return redirect(url_for('firebase_auth.firebase_login'))
        elif is_admin:
            logger.info(f"Admin login bypass for {firebase_email} - no email verification required")
        
        # Get or create local user. Prefer finding by username OR email to avoid creating duplicates
        from sqlalchemy import or_
        user = User.query.filter(or_(User.username == firebase_email, User.email == firebase_email)).first()
        if not user:
            try:
                user = User(
                    username=firebase_email,
                    pw_hash=uid
                )
                user.email = firebase_email
                user.firebase_uid = uid
                db.session.add(user)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    user = User.query.filter_by(username=firebase_email).first()
                    if not user:
                        logger.error('Failed to create or fetch user after IntegrityError')
                        flash('Αποτυχία σύνδεσης — σφάλμα τοπικής ρύθμισης', 'danger')
                        return redirect(url_for('firebase_auth.firebase_login'))
            except Exception as e:
                logger.error(f"Error creating local user: {e}")
                flash('Αποτυχία σύνδεσης — σφάλμα τοπικής ρύθμισης', 'danger')
                return redirect(url_for('firebase_auth.firebase_login'))
        
        # Update local user's Firebase UID
        user.pw_hash = uid
        # update explicit firebase uid and email if missing
        if not getattr(user, 'firebase_uid', None):
            user.firebase_uid = uid
        if getattr(user, 'email', None) != firebase_email:
            user.email = firebase_email
        db.session.commit()
        
        # Prevent concurrent login: check DB-backed session and allow takeover if stale
        try:
            SESSION_TIMEOUT = int(current_app.config.get('SESSION_TIMEOUT_SECONDS', 300))
            existing_sid = getattr(user, 'current_session_id', None)
            last_active = getattr(user, 'last_active_at', None)
            if existing_sid and last_active:
                try:
                    now = datetime.utcnow()
                    delta = now - last_active
                    if delta.total_seconds() <= SESSION_TIMEOUT:
                        logger.info(f"Blocked login; live session exists for user id={user.id}")
                        flash('Ο λογαριασμός είναι ήδη ενεργός σε άλλη συσκευή/σύνδεση.', 'warning')
                        return redirect(url_for('firebase_auth.firebase_login'))
                except Exception:
                    pass
        except Exception:
            pass

        # Login user and create DB-backed session id
        login_user(user, remember=True)
        try:
            session_id = secrets.token_urlsafe(32)
            user.start_session(session_id)
            db.session.commit()
            session['session_id'] = session_id
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
        # Update last_login timestamp
        try:
            user.last_login = datetime.utcnow()
            db.session.commit()
        except Exception:
            pass
        
        # Sync user's groups from Firestore to local DB
        # This ensures the user's group memberships are up-to-date
        try:
            firebase_config.sync_user_groups_from_firestore(user.id, uid)
            logger.info('User %s groups synced from Firestore on login', uid)
        except Exception as e:
            logger.error('Failed to sync groups from Firestore for user %s: %s', uid, e)
            # Continue anyway - syncing is not critical
        
        # Log the login with enhanced details
        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=uid,
                group_name='system',
                action='login',
                details={
                    'email': firebase_email,
                    'username': user.username,
                    'is_admin': getattr(user, 'is_admin', False),
                    'groups_count': len(list(getattr(user, 'groups', []) or []))
                },
                user_email=firebase_email,
                user_username=user.username
            )
        except Exception as e:
            logger.error(f"Failed to log login activity: {e}")
        
        # Fallback to original logging
        firebase_config.firebase_log_activity(
            uid,
            'system',
            'user_logged_in',
            {'email': firebase_email}
        )
        
        # Handle active group selection after login
        # Always check user's groups and set/clear active_group appropriately
        user_groups = list(getattr(user, 'groups', []) or [])
        
        if len(user_groups) == 0:
            # No groups: clear active_group and redirect to list
            session.pop('active_group', None)
            flash('Δεν έχεις ακόμη αντιστοιχιστεί σε ομάδα.', 'warning')
            return redirect(url_for('auth.list_groups'))
        elif len(user_groups) == 1:
            # Exactly one group: auto-set as active and continue
            session['active_group'] = user_groups[0].name
            flash(f'Καλώς ήρθατε!', 'success')
            # Optional lazy-pull via sync page (admin-toggleable policy)
            if utils.firebase_sync_login_logout_enabled():
                return redirect(url_for('firebase_auth.sync_start_pull', group=session['active_group']))
            return redirect(url_for('home'))
        else:
            # Multiple groups: redirect to list to select one
            session.pop('active_group', None)  # Clear any stale active_group
            flash('Επίλεξε ενεργή ομάδα για να συνεχίσεις.', 'info')
            return redirect(url_for('auth.list_groups'))
    
    return render_template('auth/login.html')


@firebase_auth_bp.route('/logout')
@login_required
def firebase_logout():
    """Logout user"""
    uid = current_user.pw_hash
    email = current_user.email
    username = current_user.username
    
    # Calculate session duration if possible
    session_duration_minutes = None
    try:
        if hasattr(current_user, 'last_login') and current_user.last_login:
            from datetime import datetime
            now = datetime.utcnow()
            duration = now - current_user.last_login
            session_duration_minutes = int(duration.total_seconds() / 60)
    except Exception:
        pass
    
    # Enhanced logout logging
    try:
        from utils import log_user_activity
        log_user_activity(
            user_id=uid,
            group_name='system',
            action='logout',
            details={
                'email': email,
                'username': username,
                'duration_minutes': session_duration_minutes
            },
            user_email=email,
            user_username=username
        )
    except Exception as e:
        logger.exception(f"Failed to log logout activity: {e}")
    
    # Fallback to original logging
    firebase_config.firebase_log_activity(
        uid,
        'system',
        'user_logged_out',
        {'email': email}
    )
    
    # Optional push-on-logout flow (admin-toggleable policy).
    try:
        active_group_name = session.get('active_group')
        if active_group_name and utils.firebase_sync_login_logout_enabled():
            # redirect to the sync page which will call the push API and then logout
            return redirect(url_for('firebase_auth.sync_start_push', group=active_group_name))
    except Exception:
        logger.exception('Failed to initiate sync-on-logout')

    # Fallback: if no active group, proceed to immediate logout
    # End DB-backed session for this user
    try:
        sid = session.get('session_id')
        try:
            dur = current_user.end_session(sid)
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
    except Exception:
        pass
    logout_user()
    try:
        session.pop('active_group', None)
    except Exception:
        pass
    flash('Έχετε αποσυνδεθεί.', 'info')
    return redirect(url_for('firebase_auth.firebase_login'))


@firebase_auth_bp.route('/profile')
@login_required
def firebase_profile():
    """User profile page"""
    uid = current_user.pw_hash
    user_profile = FirebaseAuthHandler.get_user_by_uid(uid)
    user_groups = FirebaseAuthHandler.get_user_groups(uid)
    
    return render_template('auth/account.html', 
                         user_profile=user_profile,
                         user_groups=user_groups)


@firebase_auth_bp.route('/profile/update', methods=['POST'])
@login_required
def firebase_profile_update():
    """Update user profile"""
    uid = current_user.pw_hash
    display_name = request.form.get('display_name', '').strip()
    
    if not display_name:
        flash('Το όνομα χρήστη δεν μπορεί να είναι κενό', 'danger')
        return redirect(url_for('firebase_auth.firebase_profile'))
    
    success, error = FirebaseAuthHandler.update_user_profile(uid, {
        'display_name': display_name,
        'updated_at': datetime.now(timezone.utc).isoformat()
    })
    
    if success:
        current_user.username = display_name
        db.session.commit()
        flash('Το προφίλ ενημερώθηκε επιτυχώς', 'success')
    else:
        flash(f'Αποτυχία ενημέρωσης προφίλ: {error}', 'danger')
    
    return redirect(url_for('firebase_auth.firebase_profile'))


@firebase_auth_bp.route('/password/change', methods=['POST'])
@login_required
def firebase_change_password():
    """Change user password"""
    uid = current_user.pw_hash
    current_password = request.form.get('current_password', '').strip()
    new_password = request.form.get('new_password', '').strip()
    password_confirm = request.form.get('password_confirm', '').strip()
    
    if not current_password or not new_password:
        flash('Απαιτούνται όλα τα πεδία', 'danger')
        return redirect(url_for('firebase_auth.firebase_profile'))
    
    if new_password != password_confirm:
        flash('Οι νέοι κωδικοί δεν ταιριάζουν', 'danger')
        return redirect(url_for('firebase_auth.firebase_profile'))
    
    if len(new_password) < 6:
        flash('Ο νέος κωδικός πρέπει να έχει τουλάχιστον 6 χαρακτήρες', 'danger')
        return redirect(url_for('firebase_auth.firebase_profile'))
    
    # Verify current password via Firebase REST API in the handler
    success, error = FirebaseAuthHandler.change_password(uid, current_password, new_password)
    
    if success:
        flash('Ο κωδικός άλλαξε επιτυχώς', 'success')
        firebase_config.firebase_log_activity(uid, 'system', 'password_changed', {})
    else:
        flash(f'Αποτυχία αλλαγής κωδικού: {error}', 'danger')
    
    return redirect(url_for('firebase_auth.firebase_profile'))


@firebase_auth_bp.route('/password/reset', methods=['GET', 'POST'])
def firebase_password_reset():
    """Password reset request"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        if not email:
            flash('Το email απαιτείται', 'danger')
            return redirect(url_for('firebase_auth.firebase_password_reset'))
        
        success, error = FirebaseAuthHandler.reset_password(email)
        
        if success:
            flash('Αποστάλθηκε email επαναφοράς. Ελέγξτε τα εισερχόμενα.', 'success')
        else:
            # Don't reveal if email exists
            flash('Εάν υπάρχει λογαριασμός με αυτό το email, αποστάλθηκε σύνδεσμος επαναφοράς.', 'info')
        
        return redirect(url_for('firebase_auth.firebase_login'))
    
    return render_template('firebase_auth/password_reset.html')


@firebase_auth_bp.route('/group/<group_name>/join', methods=['POST'])
@login_required
def firebase_join_group(group_name: str):
    """Join a group"""
    uid = current_user.pw_hash
    
    success, error = FirebaseAuthHandler.add_user_to_group(uid, group_name)
    
    if success:
        flash(f'Ενταχθήκατε στην ομάδα: {group_name}', 'success')
        firebase_config.firebase_log_activity(uid, group_name, 'user_joined_group', {})
    else:
        flash(f'Αποτυχία εισόδου στην ομάδα: {error}', 'danger')
    
    return redirect(url_for('index'))


@firebase_auth_bp.route('/group/<group_name>/leave', methods=['POST'])
@login_required
def firebase_leave_group(group_name: str):
    """Leave a group"""
    uid = current_user.pw_hash
    
    success, error = FirebaseAuthHandler.remove_user_from_group(uid, group_name)
    
    if success:
        flash(f'Απεχώρησα από την ομάδα: {group_name}', 'success')
        firebase_config.firebase_log_activity(uid, group_name, 'user_left_group', {})
    else:
        flash(f'Αποτυχία εξόδου από την ομάδα: {error}', 'danger')
    
    return redirect(url_for('index'))


@firebase_auth_bp.route('/sync/start_pull')
@login_required
def sync_start_pull():
    """Render a small page that opens a modal and triggers the pull via AJAX."""
    group = request.args.get('group') or session.get('active_group')
    if not group:
        flash('Δεν υπάρχει ενεργή ομάδα για συγχρονισμό.', 'warning')
        return redirect(url_for('home'))
    return render_template('sync_progress.html', action='pull', group=group)


@firebase_auth_bp.route('/sync/start_push')
@login_required
def sync_start_push():
    """Render a small page that opens a modal and triggers the push via AJAX (used on logout)."""
    group = request.args.get('group') or session.get('active_group')
    if not group:
        # proceed to logout immediately
        # End DB-backed session before logging out to avoid leaving stale locks
        try:
            sid = session.get('session_id')
            try:
                dur = current_user.end_session(sid)
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            session.pop('session_id', None)
        except Exception:
            pass
        try:
            session.pop('active_group', None)
        except Exception:
            pass
        logout_user()
        flash('Έχετε αποσυνδεθεί.', 'info')
        return redirect(url_for('firebase_auth.firebase_login'))
    return render_template('sync_progress.html', action='push', group=group)


@firebase_auth_bp.route('/api/sync/pull', methods=['POST'])
@login_required
def api_sync_pull():
    payload = request.get_json() or {}
    group = payload.get('group') or session.get('active_group')
    if not group:
        return jsonify({'success': False, 'error': 'no_group_provided'}), 400
    try:
        ok = firebase_config.firebase_pull_group_to_local(group)
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.exception('api_sync_pull failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@firebase_auth_bp.route('/api/sync/push', methods=['POST'])
@login_required
def api_sync_push():
    payload = request.get_json() or {}
    group = payload.get('group') or session.get('active_group')
    if not group:
        return jsonify({'success': False, 'error': 'no_group_provided'}), 400
    try:
        dry_run = bool(payload.get('dry_run'))
        verbose = bool(payload.get('verbose'))
        ok = firebase_config.firebase_push_group_files(group, dry_run=dry_run, verbose=verbose)
        # If this push was invoked as part of logout, finish logout on success
        if payload.get('logout_after') and ok:
            try:
                # End DB-backed session before logging out to avoid leaving stale locks
                try:
                    sid = session.get('session_id')
                    try:
                        dur = current_user.end_session(sid)
                        db.session.commit()
                    except Exception:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass
                except Exception:
                    pass
                # clear flask session id and active group, then logout
                try:
                    session.pop('session_id', None)
                except Exception:
                    pass
                try:
                    session.pop('active_group', None)
                except Exception:
                    pass
                logout_user()
            except Exception:
                pass
        # If dry_run requested, firebase_push_group_files returns a dict with candidates
        if isinstance(ok, dict):
            return jsonify(ok)
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.exception('api_sync_push failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@firebase_auth_bp.route('/api/user/groups')
@login_required
def api_user_groups():
    """API endpoint to get user's groups"""
    uid = current_user.pw_hash
    groups = FirebaseAuthHandler.get_user_groups(uid)
    return jsonify({
        'success': True,
        'groups': groups,
        'count': len(groups)
    })


@firebase_auth_bp.route('/api/group/<group_name>/members')
@login_required
def api_group_members(group_name: str):
    """API endpoint to get group members"""
    uid = current_user.pw_hash
    user_groups = FirebaseAuthHandler.get_user_groups(uid)
    
    # Check if user is in this group
    if group_name not in user_groups:
        return jsonify({'success': False, 'error': 'Μη εξουσιοδοτημένο'}), 403
    
    members = FirebaseAuthHandler.get_group_members(group_name)
    return jsonify({
        'success': True,
        'group': group_name,
        'members': members,
        'count': len(members)
    })
