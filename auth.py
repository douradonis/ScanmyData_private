from flask import Blueprint, request, render_template, redirect, url_for, flash, current_app, jsonify, session
from werkzeug.utils import secure_filename
import os
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import firebase_config
from models import db, User, Group
import datetime
from firebase_auth_handlers import FirebaseAuthHandler
import email_utils
import secrets
import utils

auth_bp = Blueprint('auth', __name__)

login_manager = LoginManager()
login_manager.login_view = 'auth.login'


@login_manager.unauthorized_handler
def unauthorized_callback():
    """Return JSON for API/XHR requests when unauthenticated, otherwise redirect to login."""
    try:
        # If the request looks like an API or XHR call, return JSON/401 instead of redirecting
        accept = request.headers.get('Accept', '') or ''
        xrw = request.headers.get('X-Requested-With', '') or ''
        # Consider Authorization header as indicator of API call as well
        auth_hdr = request.headers.get('Authorization')
        if request.path.startswith('/api') or 'application/json' in accept or xrw == 'XMLHttpRequest' or auth_hdr:
            return jsonify({'error': 'Authentication required'}), 401
    except Exception:
        pass
    # Default behavior: redirect to login page with next
    return redirect(url_for('auth.login', next=request.path))


@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None


@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        # support two flows: join existing group (group) OR create new group (new_group_name + new_group_folder)
        group_name = (request.form.get('group') or '').strip()
        new_group_name = (request.form.get('new_group_name') or '').strip()
        new_group_folder = (request.form.get('new_group_folder') or '').strip()

        if not username or not password:
            flash('Απαιτείται όνομα χρήστη και κωδικός πρόσβασης', 'danger')
            return redirect(url_for('auth.signup'))

        if User.query.filter_by(username=username).first():
            flash('Το όνομα χρήστη υπάρχει ήδη', 'warning')
            return redirect(url_for('auth.signup'))

        # Try to register user in Firebase (if enabled). If successful, store firebase_uid.
        firebase_uid = None
        try:
            # Check if user exists locally (do not expose this to the end-user)
            try:
                from models import User
                from sqlalchemy import or_
                local_user = User.query.filter(or_(User.email == email, User.username == email)).first()
                user_exists = bool(local_user)
            except Exception:
                local_user = None
                user_exists = False
            # Log that a forgot-password request was received (will not reveal existence to user)
            try:
                from utils import log_user_activity
                log_user_activity(
                    user_id=email,
                    group_name='system',
                    action='forgot_password_request_received',
                    details={'email': email, 'user_exists': user_exists, 'description': 'Αίτημα επαναφοράς λησμονημένου κωδικού (λήφθηκε)'} ,
                    user_email=email,
                    user_username=email
                )
            except Exception:
                pass
            success, uid, err = FirebaseAuthHandler.register_user(username, password, display_name=username)
            if success and uid:
                firebase_uid = uid
        except Exception:
            # If Firebase fails or not enabled, continue with local-only user
            firebase_uid = None

        user = User(username=username)
        user.email = username
        user.set_password(password)
        if firebase_uid:
            user.firebase_uid = firebase_uid

        db.session.add(user)
        db.session.flush()

        # If creating a new group during signup, create it and make user admin
        if new_group_name and new_group_folder:
            existing = Group.query.filter_by(name=new_group_name).first()
            if existing:
                flash('Το όνομα ομάδας υπάρχει ήδη. Επιλέξτε άλλο ή εγγραφείτε σε αυτή.', 'warning')
                db.session.rollback()
                return redirect(url_for('auth.signup'))
            # sanitize folder name to avoid path traversal and ensure filesystem-safe name
            safe_folder = secure_filename(new_group_folder)
            if not safe_folder:
                flash('Μη έγκυρο όνομα φακέλου για ομάδα', 'danger')
                db.session.rollback()
                return redirect(url_for('auth.signup'))
            # ensure no other group uses the same data_folder
            if Group.query.filter_by(data_folder=safe_folder).first():
                flash('Το όνομα φακέλου χρησιμοποιείται ήδη. Επιλέξτε άλλο', 'warning')
                db.session.rollback()
                return redirect(url_for('auth.signup'))

            grp = Group(name=new_group_name, data_folder=safe_folder)
            db.session.add(grp)
            db.session.flush()
            
            # Create group in Firebase if enabled
            if user.firebase_uid:
                try:
                    from firebase_auth_handlers_new import firebase_create_group
                    success, error = firebase_create_group(new_group_name, user.firebase_uid, safe_folder)
                    if not success:
                        current_app.logger.warning(f"Firebase group creation failed: {error}")
                except Exception as e:
                    current_app.logger.error(f"Firebase group creation error: {e}")
            
            # attach user as admin
            user.add_to_group(grp, role='admin')
            # ensure folder exists under data/
            try:
                folder_path = os.path.join(current_app.root_path, 'data', safe_folder)
                os.makedirs(folder_path, exist_ok=True)
            except Exception:
                pass

        elif group_name:
            grp = Group.query.filter_by(name=group_name).first()
            if grp:
                user.add_to_group(grp, role='member')

        db.session.commit()
        # Log signup completion
        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=user.id,
                group_name='system',
                action='signup_complete',
                details={
                    'email': user.email,
                    'username': user.username,
                    'description': 'Ολοκλήρωση εγγραφής χρήστη'
                },
                user_email=user.email,
                user_username=user.username
            )
        except Exception:
            pass

        # If we registered in Firebase, generate a verification link via Firebase Admin SDK
        try:
            if firebase_uid:
                ok, link_or_err = FirebaseAuthHandler.generate_email_verification_link(user.email)
                if ok:
                    try:
                        from firebed_email_verification import FirebedEmailVerification
                        sent = FirebedEmailVerification.send_signup_verification_email(user.email, user.username)
                    except Exception:
                        sent = False
                    if sent:
                        flash('Ο λογαριασμός δημιουργήθηκε! Ένα email επαλήθευσης έχει σταλεί στα εισερχόμενά σας.', 'success')
                    else:
                        current_app.logger.info(f"Firebase verification link for {user.email}: {link_or_err}")
                        flash('Ο λογαριασμός δημιουργήθηκε! Ο σύνδεσμος επαλήθευσης έχει καταγραφεί (ανάπτυξη).', 'success')
                else:
                    current_app.logger.warning(f"Could not generate Firebase verification link: {link_or_err}")
                    flash('Ο λογαριασμός δημιουργήθηκε. Παρακαλώ επαληθεύστε το email σας μέσω Firebase (ελέγξτε τα εισερχόμενά σας).', 'success')
            else:
                flash('Ο λογαριασμός δημιουργήθηκε. Παρακαλώ συνδεθείτε.', 'success')
        except Exception as e:
            current_app.logger.exception('Failed to generate Firebase verification link')
            flash('Ο λογαριασμός δημιουργήθηκε. Παρακαλώ συνδεθείτε.', 'success')

        return redirect(url_for('auth.login'))

    # render signup form
    return render_template('auth/signup.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        # Support login by username or email
        user = User.query.filter_by(username=identifier).first()
        if not user:
            user = User.query.filter_by(email=identifier).first()
        if not user or not user.check_password(password):
            flash('Μη έγκυρο όνομα χρήστη/email ή κωδικός πρόσβασης', 'error')
            return redirect(url_for('auth.login'))

        # Successful login: clear any previous active credential selection
        session.pop('active_credential', None)
        session.pop('_remote_qr_owner', None)

        # Prevent concurrent login: if a session lock exists for this user, deny login
        try:
            # If the user already has a live session (recent heartbeat), block login.
            # Allow takeover if the existing session appears stale (> timeout).
            from models import db as _db
            SESSION_TIMEOUT = int(current_app.config.get('SESSION_TIMEOUT_SECONDS', 900))
            existing_sid = getattr(user, 'current_session_id', None)
            last_active = getattr(user, 'last_active_at', None)
            if existing_sid and last_active:
                try:
                    now = datetime.datetime.utcnow()
                    delta = now - last_active
                    if delta.total_seconds() <= SESSION_TIMEOUT:
                        current_app.logger.info(f"Blocked local auth login; live session exists for user id={user.id}")
                        flash('Ο λογαριασμός είναι ήδη ενεργός σε άλλη συσκευή/σύνδεση.', 'warning')
                        return redirect(url_for('auth.login'))
                    else:
                        # stale session -> allow takeover (fall through)
                        current_app.logger.info(f"Stale session for user id={user.id}, allowing takeover")
                except Exception:
                    pass
        except Exception:
            pass

        login_user(user)
        # create and store session id to prevent concurrent logins (DB-backed)
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
        # record last login
        try:
            user.last_login = datetime.datetime.utcnow()
            db.session.commit()
        except Exception:
            pass

        if not session.get('active_group'):
            user_groups = list(getattr(user, 'groups', []) or [])
            if len(user_groups) == 1:
                session['active_group'] = user_groups[0].name
                flash('Συνδεθήκατε επιτυχώς', 'success')
                # Optional pull sync on login (admin-toggleable policy)
                if utils.firebase_sync_login_logout_enabled():
                    try:
                        return redirect(url_for('firebase_auth.sync_start_pull', group=session.get('active_group')))
                    except Exception:
                        return redirect(url_for('home'))
                return redirect(url_for('home'))
            else:
                if user_groups:
                    flash('Επίλεξε ενεργή ομάδα για να συνεχίσεις.', 'info')
                else:
                    flash('Δεν έχεις ακόμη αντιστοιχιστεί σε ομάδα. Επίλεξε ή δημιούργησε μία.', 'warning')
                return redirect(url_for('auth.list_groups'))

        flash('Συνδεθήκατε επιτυχώς', 'success')
        # If there's an active group, optionally start pull sync page
        if session.get('active_group'):
            if utils.firebase_sync_login_logout_enabled():
                try:
                    return redirect(url_for('firebase_auth.sync_start_pull', group=session.get('active_group')))
                except Exception:
                    pass
        return redirect(request.args.get('next') or url_for('home'))

    # GET -> render login form
    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    # Optional push sync on logout (admin-toggleable policy)
    try:
        active_group_name = session.get('active_group')
        if active_group_name and utils.firebase_sync_login_logout_enabled():
            return redirect(url_for('firebase_auth.sync_start_push', group=active_group_name))
    except Exception:
        current_app.logger.exception('Failed to initiate sync-on-logout')

    # Log logout activity
    try:
        from utils import log_user_activity
        print(f"DEBUG: Logging logout for user {current_user.id}")
        result = log_user_activity(
            user_id=current_user.id,
            group_name='system',
            action='logout',
            details={
                'email': current_user.email,
                'username': current_user.username
            },
            user_email=current_user.email,
            user_username=current_user.username
        )
        print(f"DEBUG: Logout logging result: {result}")
    except Exception as e:
        try:
            current_app.logger.exception(f"Failed to log logout activity: {e}")
        except Exception:
            pass
        # Don't abort logout on logging failure; continue to clear session
        print(f"DEBUG: Exception in logout logging: {e}")

    # Clear session lock when user logs out
    try:
        sid = session.get('session_id')
        # end session and accumulate duration
        try:
            dur = current_user.end_session(sid)
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
    except Exception:
        try:
            current_app.logger.exception('Failed to clear session during logout')
        except Exception:
            pass

    logout_user()
    flash('Έχετε αποσυνδεθεί.', 'info')
    # clear any active client/credential selection and active group from session to avoid stale state
    session.pop('active_credential', None)
    session.pop('_remote_qr_owner', None)
    session.pop('active_group', None)
    # clear stored session id from flask session
    session.pop('session_id', None)
    return redirect(url_for('auth.login'))


@auth_bp.route('/account', methods=['GET', 'POST'])
@login_required
def account_settings():
    if request.method == 'POST':
        current_password = request.form.get('current_password') or ''
        new_password = request.form.get('new_password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not current_password:
            flash('Συμπλήρωσε τον τρέχοντα κωδικό.', 'danger')
            return redirect(url_for('auth.account_settings'))

        if not new_password or not confirm_password:
            flash('Συμπλήρωσε τον νέο κωδικό και την επιβεβαίωση.', 'danger')
            return redirect(url_for('auth.account_settings'))

        if new_password != confirm_password:
            flash('Ο νέος κωδικός και η επιβεβαίωση δεν ταιριάζουν.', 'danger')
            return redirect(url_for('auth.account_settings'))

        if not current_user.check_password(current_password):
            flash('Ο τρέχων κωδικός δεν είναι σωστός.', 'danger')
            return redirect(url_for('auth.account_settings'))

        if len(new_password) < 8:
            flash('Ο νέος κωδικός πρέπει να έχει τουλάχιστον 8 χαρακτήρες.', 'warning')
            return redirect(url_for('auth.account_settings'))

        if current_password == new_password:
            flash('Ο νέος κωδικός πρέπει να είναι διαφορετικός από τον τρέχοντα.', 'warning')
            return redirect(url_for('auth.account_settings'))

        current_user.set_password(new_password)
        db.session.commit()
        flash('Ο κωδικός ενημερώθηκε με επιτυχία.', 'success')
        return redirect(url_for('auth.account_settings'))

    return render_template('auth/account.html')


@auth_bp.route('/groups', methods=['GET'])
@login_required
def list_groups():
    # Only show groups the current user belongs to or has been granted access to
    try:
        groups = current_user.groups
        # Get groups where user is admin for the dropdown
        admin_groups = [g for g in groups if current_user.role_for_group(g) == 'admin']
    except Exception:
        groups = []
        admin_groups = []
    return render_template('auth/groups.html', groups=groups, admin_groups=admin_groups)


@auth_bp.route('/groups/delete', methods=['POST'])
@login_required
def delete_group():
    """Admin-only deletion of a group. Removes memberships, deletes folder, logs action.
    Expects 'group_name' param. Action code: group_delete.
    """
    group_name = (request.form.get('group_name') or '').strip()
    if not group_name and request.is_json:
        payload = request.get_json(silent=True) or {}
        group_name = (payload.get('group_name') or '').strip()
    if not group_name:
        resp = {'ok': False, 'error': 'group_name required'}
        return (jsonify(resp), 400) if request.is_json else (flash('Απαιτείται όνομα ομάδας.', 'danger'), redirect(url_for('auth.list_groups')))
    grp = Group.query.filter_by(name=group_name).first()
    if not grp:
        resp = {'ok': False, 'error': 'group not found'}
        return (jsonify(resp), 404) if request.is_json else (flash('Η ομάδα δεν βρέθηκε.', 'danger'), redirect(url_for('auth.list_groups')))
    # admin check
    try:
        from admin_panel import is_admin
        if not is_admin(current_user):
            resp = {'ok': False, 'error': 'not authorized'}
            return (jsonify(resp), 403) if request.is_json else (flash('Μόνο διαχειριστής μπορεί να διαγράψει ομάδα.', 'danger'), redirect(url_for('auth.list_groups')))
    except Exception:
        resp = {'ok': False, 'error': 'admin check failed'}
        return (jsonify(resp), 500) if request.is_json else (flash('Σφάλμα ελέγχου δικαιωμάτων.', 'danger'), redirect(url_for('auth.list_groups')))
    data_folder = getattr(grp, 'data_folder', None)
    try:
        # append deletion entry to group activity log BEFORE removing records/files
        try:
            # Structured log entry for admin-deleted group (for admin panel consumption)
            _append_group_log(grp, {
                'action': 'group_delete',
                'group': group_name,
                'description': 'Διαγραφή ομάδας',
                'details': {'reason': 'admin_deleted'}
            })
        except Exception:
            pass
        # Delete from Firebase if current user has firebase_uid
        if getattr(current_user, 'firebase_uid', None):
            try:
                from firebase_auth_handlers_new import firebase_delete_group
                firebase_success, firebase_error = firebase_delete_group(group_name, current_user.firebase_uid)
                if not firebase_success:
                    current_app.logger.warning(f"Firebase group deletion failed: {firebase_error}")
            except Exception as e:
                current_app.logger.error(f"Firebase group deletion error: {e}")
        
        # delete group (cascade removes memberships)
        db.session.delete(grp)
        db.session.commit()
        # remove folder under data/ if exists
        if data_folder:
            import shutil, os
            folder_path = os.path.join(current_app.root_path, 'data', data_folder)
            try:
                shutil.rmtree(folder_path, ignore_errors=True)
            except Exception:
                pass
        # log deletion
        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=current_user.id,
                group_name=group_name,
                action='group_delete',
                details={'group': group_name, 'description': 'Διαγραφή ομάδας'},
                user_email=getattr(current_user, 'email', None),
                user_username=current_user.username
            )
        except Exception:
            pass
        # clear session active_group if deleted
        if session.get('active_group') == group_name:
            session.pop('active_group', None)
        resp = {'ok': True, 'message': 'group deleted', 'group': group_name}
        return jsonify(resp) if request.is_json else (flash('Η ομάδα διαγράφηκε.', 'info'), redirect(url_for('auth.list_groups')))
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        resp = {'ok': False, 'error': str(e)}
        return (jsonify(resp), 500) if request.is_json else (flash('Σφάλμα διαγραφής ομάδας.', 'danger'), redirect(url_for('auth.list_groups')))


@auth_bp.route('/groups/assign-user', methods=['POST'])
@login_required
def assign_user_to_group():
    """Assign a user to a group by email or username (admin/group-admin only)"""
    try:
        group_name = (request.form.get('group') or '').strip()
        user_identifier = (request.form.get('username') or '').strip()  # email or username
        role = (request.form.get('role') or 'member').strip()
        
        if not group_name or not user_identifier:
            return jsonify({'ok': False, 'error': 'Όνομα ομάδας και χρήστης είναι απαραίτητα'}), 400
        
        grp = Group.query.filter_by(name=group_name).first()
        if not grp:
            return jsonify({'ok': False, 'error': 'group not found'}), 404
        
        # Check if current user is admin or group admin
        from admin_panel import is_admin
        if not (is_admin(current_user) or current_user.role_for_group(grp) == 'admin'):
            return jsonify({'ok': False, 'error': 'not authorized'}), 403
        
        # Find user by email or username
        user = User.query.filter_by(username=user_identifier).first()
        if not user:
            user = User.query.filter_by(email=user_identifier).first()
        
        if not user:
            return jsonify({'ok': False, 'error': f'user not found: {user_identifier}'}), 404
        
        # Check if assigning admin role and group already has an admin
        if role == 'admin':
            existing_admins = [ug for ug in grp.user_groups if ug.role == 'admin']
            if existing_admins:
                return jsonify({'ok': False, 'error': 'η ομάδα έχει ήδη διαχειριστή'}), 400
        
        # Add user to group with specified role
        user.add_to_group(grp, role=role)
        db.session.commit()
        
        # append to group activity log (structured)
        try:
            _append_group_log(grp, {
                'action': 'add_user',
                'target_user': user.username,
                'role': role,
                'description': f'Προσθήκη χρήστη {user.username} ως {role}',
            })
        except Exception:
            pass

        flash(f'Ο χρήστης {user.username} προστέθηκε στην ομάδα {group_name} ως {role}', 'success')
        return redirect(url_for('auth.list_groups'))
    except Exception as e:
        current_app.logger.exception('Error assigning user to group')
        flash('Σφάλμα κατά την ανάθεση χρήστη στην ομάδα', 'danger')
        return redirect(url_for('auth.list_groups'))


@auth_bp.route('/groups/create', methods=['POST'])
@login_required
def create_group():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Απαιτείται όνομα ομάδας', 'danger')
        return redirect(url_for('auth.list_groups'))

    if Group.query.filter_by(name=name).first():
        flash('Η ομάδα υπάρχει ήδη', 'warning')
        return redirect(url_for('auth.list_groups'))

    # Auto-generate data folder name from group name (convert to latin characters)
    def greek_to_latin(text):
        greek_chars = 'αβγδεζηθικλμνξοπρστυφχψωάέήίόύώϊϋ'
        latin_chars = 'abgdezhthiklmnxoprstyfxpswaehioyoiy'
        trans = str.maketrans(greek_chars + greek_chars.upper(), latin_chars + latin_chars.upper())
        return text.translate(trans).replace('ς', 's').replace('Σ', 'S')

    # Create safe folder name: convert greek to latin, keep only alphanumeric and underscores
    safe_folder = ''.join(c if c.isalnum() or c == '_' else '_' for c in greek_to_latin(name).lower())
    safe_folder = safe_folder.strip('_')  # remove leading/trailing underscores
    
    # Ensure uniqueness by adding number if needed
    base_folder = safe_folder
    counter = 1
    while Group.query.filter_by(data_folder=safe_folder).first():
        safe_folder = f"{base_folder}_{counter}"
        counter += 1

    # Allow users to be admin in multiple groups
    # Removed restriction: users can now be admin in multiple groups

    grp = Group(name=name, data_folder=safe_folder)
    db.session.add(grp)
    db.session.flush()
    
    # Create group in Firebase if current user has firebase_uid
    if getattr(current_user, 'firebase_uid', None):
        try:
            from firebase_auth_handlers_new import firebase_create_group
            firebase_success, firebase_error = firebase_create_group(name, current_user.firebase_uid, safe_folder)
            if not firebase_success:
                current_app.logger.warning(f"Firebase group creation failed: {firebase_error}")
        except Exception as e:
            current_app.logger.error(f"Firebase group creation error: {e}")
    
    # make current_user admin of the newly created group
    try:
        current_user.add_to_group(grp, role='admin')
    except Exception:
        pass

    # ensure folder exists
    try:
        folder_path = os.path.join(current_app.root_path, 'data', safe_folder)
        os.makedirs(folder_path, exist_ok=True)
    except Exception:
        pass

    db.session.commit()
    # append to group activity log (structured)
    try:
        _append_group_log(grp, {
            'action': 'create_group',
            'group': grp.name,
            'description': 'Δημιουργία ομάδας',
            'details': {'created_by': getattr(current_user, 'username', 'unknown')}
        })
    except Exception:
        pass
    flash('Η ομάδα δημιουργήθηκε επιτυχώς', 'success')
    return redirect(url_for('auth.list_groups'))


@auth_bp.route('/groups/assign', methods=['POST'])
@login_required
def assign_user_to_group_legacy():
    username = (request.form.get('username') or '').strip()
    group_name = (request.form.get('group') or '').strip()
    role = (request.form.get('role') or request.args.get('role') or 'member').strip()
    if role not in ('member', 'admin'):
        return jsonify({'ok': False, 'error': 'invalid role'}), 400

    if not username or not group_name:
        return jsonify({'ok': False, 'error': 'username and group required'}), 400

    # support username, user_id, or email
    user = None
    if username and username.isdigit():
        user = User.query.get(int(username))
    if not user and username:
        user = User.query.filter_by(username=username).first()
    if not user and username:
        user = User.query.filter_by(email=username).first()
    grp = Group.query.filter_by(name=group_name).first()
    if not user or not grp:
        return jsonify({'ok': False, 'error': 'user or group not found'}), 404

    # Only admins of the group can assign users / roles
    try:
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'authentication required'}), 403
        if current_user.role_for_group(grp) != 'admin':
            return jsonify({'ok': False, 'error': 'admin privileges required for this group'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'permission check failed'}), 500

    # Allow users to be admin in multiple groups
    # Removed restriction: users can now be admin in multiple groups

    # assign role
    db.session.commit()
    # assign role
    user.add_to_group(grp, role=role)
    db.session.commit()

    # log the assignment in the group's activity log (structured)
    try:
        _append_group_log(grp, {
            'action': 'add_user',
            'target_user': user.username,
            'role': role,
            'description': f'Προσθήκη χρήστη {user.username} ως {role}',
        })
    except Exception:
        pass

    # Return with refresh flag so frontend auto-refreshes
    return jsonify({'ok': True, 'refresh': True, 'message': f'User {user.username} assigned as {role}'})


@auth_bp.route('/groups/remove_member', methods=['POST'])
@login_required
def remove_member():
    """Admin-only: remove a user from a group. Expects form/json: username OR user_id and group (name)."""
    username = (request.form.get('username') or request.json.get('username') if request.is_json else request.form.get('username')) or ''
    group_name = (request.form.get('group') or request.json.get('group') if request.is_json else request.form.get('group')) or ''
    if not username or not group_name:
        return jsonify({'ok': False, 'error': 'username and group required'}), 400

    grp = Group.query.filter_by(name=group_name).first()
    if not grp:
        return jsonify({'ok': False, 'error': 'group not found'}), 404

    # permission: only admins of the group may remove
    try:
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            return jsonify({'ok': False, 'error': 'admin privileges required for this group'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'permission check failed'}), 500

    # resolve user
    target = None
    if username.isdigit():
        target = User.query.get(int(username))
    if not target:
        target = User.query.filter_by(username=username).first()
    if not target:
        return jsonify({'ok': False, 'error': 'user not found'}), 404

    # cannot remove self via this endpoint; use leave
    if target.id == current_user.id:
        return jsonify({'ok': False, 'error': 'use leave endpoint to leave group'}), 400

    # remove membership
    try:
        ug = next((ug for ug in target.user_groups if ug.group_id == grp.id), None)
        if not ug:
            return jsonify({'ok': False, 'error': 'user is not a member of group'}), 400
        
        # Remove from Firebase if target user has firebase_uid
        if getattr(target, 'firebase_uid', None):
            try:
                from firebase_auth_handlers_new import firebase_remove_user_from_group
                firebase_success, firebase_error = firebase_remove_user_from_group(target.firebase_uid, group_name)
                if not firebase_success:
                    current_app.logger.warning(f"Firebase user removal failed: {firebase_error}")
            except Exception as e:
                current_app.logger.error(f"Firebase user removal error: {e}")
        
        db.session.delete(ug)
        db.session.commit()
        # structured log for removal
        try:
            _append_group_log(grp, {
                'action': 'remove_user',
                'target_user': target.username,
                'description': f'Αφαίρεση χρήστη {target.username} από ομάδα',
            })
        except Exception:
            pass
        flash(f'Ο χρήστης {target.username} αφαιρέθηκε από την ομάδα {group_name}', 'success')
        return redirect(url_for('auth.list_groups'))
    except Exception:
        db.session.rollback()
        flash('Σφάλμα κατά την αφαίρεση του μέλους', 'danger')
        return redirect(url_for('auth.list_groups'))


@auth_bp.route('/groups/leave', methods=['POST'])
@login_required
def leave_group():
    """Allow the current user to leave a group. Expects 'group' param (name) or uses session active_group."""
    group_name = (request.form.get('group') or request.json.get('group') if request.is_json else request.form.get('group')) or session.get('active_group')
    if not group_name:
        return jsonify({'ok': False, 'error': 'group required'}), 400
    grp = Group.query.filter_by(name=group_name).first()
    if not grp:
        return jsonify({'ok': False, 'error': 'group not found'}), 404

    # find membership
    try:
        ug = next((ug for ug in current_user.user_groups if ug.group_id == grp.id), None)
        if not ug:
            return jsonify({'ok': False, 'error': 'not a member'}), 400

        # If admin clicked, delete the entire group after confirmation (regardless of other admins)
        if ug.role == 'admin':
            # Get data folder before deleting group
            data_folder = getattr(grp, 'data_folder', None)

            # append deletion entry to group activity log BEFORE removing records/files
            try:
                _append_group_log(grp, {
                    'action': 'group_delete',
                    'group': grp.name,
                    'description': 'Διαγραφή ομάδας',
                    'details': {'reason': 'admin_requested_delete'}
                })
            except Exception:
                pass

            # Delete all user memberships for this group
            for user_group in list(grp.user_groups):
                db.session.delete(user_group)

            # Delete from Firebase/Firestore
            if getattr(current_user, 'firebase_uid', None):
                try:
                    from firebase_auth_handlers_new import firebase_delete_group
                    firebase_success, firebase_error = firebase_delete_group(grp.name, current_user.firebase_uid)
                    if not firebase_success:
                        current_app.logger.warning(f"Firebase group deletion failed: {firebase_error}")
                except Exception as e:
                    current_app.logger.error(f"Firebase group deletion error: {e}")
            
            # Delete the group itself
            db.session.delete(grp)
            
            # Clear active group from session if it was this group
            if session.get('active_group') == grp.name:
                session.pop('active_group', None)
            
            db.session.commit()
            
            # Remove folder under data/ if exists
            if data_folder:
                import shutil, os
                folder_path = os.path.join(current_app.root_path, 'data', data_folder)
                try:
                    shutil.rmtree(folder_path, ignore_errors=True)
                except Exception:
                    pass
            
            return jsonify({'ok': True, 'message': 'Group deleted successfully'})

        # Remove from Firebase if current user has firebase_uid
        if getattr(current_user, 'firebase_uid', None):
            try:
                from firebase_auth_handlers_new import firebase_remove_user_from_group
                firebase_success, firebase_error = firebase_remove_user_from_group(current_user.firebase_uid, grp.name)
                if not firebase_success:
                    current_app.logger.warning(f"Firebase user leave failed: {firebase_error}")
            except Exception as e:
                current_app.logger.error(f"Firebase user leave error: {e}")
        
        db.session.delete(ug)
        db.session.commit()
        # if active_group matches, clear it
        if session.get('active_group') == grp.name:
            session.pop('active_group', None)
        try:
            _append_group_log(grp, {
                'action': 'leave_group',
                'user': current_user.username,
                'description': 'Αποχώρηση χρήστη από ομάδα'
            })
        except Exception:
            pass
        return jsonify({'ok': True})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': 'failed to leave group'}), 500


@auth_bp.route('/groups/leave/confirm', methods=['POST'])
@login_required
def leave_group_confirm():
    """Force leave group and delete all associated data (admin only, last admin case)."""
    group_name = (request.form.get('group') or request.json.get('group') if request.is_json else request.form.get('group')) or session.get('active_group')
    if not group_name:
        return jsonify({'ok': False, 'error': 'group required'}), 400
    grp = Group.query.filter_by(name=group_name).first()
    if not grp:
        return jsonify({'ok': False, 'error': 'group not found'}), 404

    try:
        ug = next((ug for ug in current_user.user_groups if ug.group_id == grp.id), None)
        if not ug:
            return jsonify({'ok': False, 'error': 'not a member'}), 400

        # Only allow if admin and is the only admin
        if ug.role == 'admin':
            other_admins = [u for u in grp.user_groups if u.role == 'admin' and u.user_id != current_user.id]
            if other_admins:
                return jsonify({'ok': False, 'error': 'other admins exist; use regular leave'}), 400

        # append deletion entry to group activity log BEFORE removing records/files
        try:
            _append_group_log(grp, {
                'action': 'group_delete',
                'group': grp.name,
                'description': 'Διαγραφή ομάδας',
                'details': {'reason': 'force_delete_confirmed'}
            })
        except Exception:
            pass

        # Delete all user memberships for this group
        for user_group in grp.user_groups:
            db.session.delete(user_group)

        # Delete the group itself
        data_folder = getattr(grp, 'data_folder', None)
        db.session.delete(grp)

        # Clear active group from session if it was this group
        if session.get('active_group') == grp.name:
            session.pop('active_group', None)

        db.session.commit()

        # Remove folder under data/ if exists
        if data_folder:
            try:
                import shutil, os
                folder_path = os.path.join(current_app.root_path, 'data', data_folder)
                shutil.rmtree(folder_path, ignore_errors=True)
            except Exception:
                pass

        return jsonify({'ok': True, 'message': 'Group deleted successfully - all data removed'})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False, 'error': 'failed to delete group'}), 500


@auth_bp.route('/lookup_user', methods=['GET'])
@login_required
def lookup_user():
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'ok': False, 'error': 'missing query'}), 400
    # allow lookup by username, id, or email
    user = None
    if q.isdigit():
            # Compatibility: some versions of FirebaseAuthHandler expose
            # `generate_password_reset_link`, others expose `reset_password`.
            # Try the most specific method first, fall back gracefully.
            ok = False
            link_or_err = None
            try:
                if hasattr(FirebaseAuthHandler, 'generate_password_reset_link'):
                    ok, link_or_err = FirebaseAuthHandler.generate_password_reset_link(email)
                    used_method = 'generate_password_reset_link'
                elif hasattr(FirebaseAuthHandler, 'generate_password_reset_link_local'):
                    ok, link_or_err = FirebaseAuthHandler.generate_password_reset_link_local(email)
                    used_method = 'generate_password_reset_link_local'
                elif hasattr(FirebaseAuthHandler, 'reset_password'):
                    # `reset_password` may only return (True, None) and not a link.
                    ok, link_or_err = FirebaseAuthHandler.reset_password(email)
                    used_method = 'reset_password'
                else:
                    raise AttributeError('No compatible password-reset method on FirebaseAuthHandler')
            except Exception as _method_err:
                # Re-raise to be handled by outer except so we log activity and flash user-friendly message
                raise
    if not user:
        user = User.query.filter_by(username=q).first()
    if not user:
        user = User.query.filter_by(email=q).first()
    if not user:
        return jsonify({'ok': False, 'found': False}), 200
    return jsonify({'ok': True, 'found': True, 'username': user.username, 'id': user.id})


# helper: get data folders current user has access to (list)
def get_user_data_folders(user):
    if not user:
        return []
    return [g.data_folder for g in user.groups if g.data_folder]


def get_active_group():
    """Return the Group object for the currently-selected active group stored in session.
    If none is selected and the current user belongs to exactly one group, return that group.
    Returns None if no applicable group is found.
    """
    name = session.get('active_group')
    if name:
        try:
            return Group.query.filter_by(name=name).first()
        except Exception:
            return None

    # fallback: if user is authenticated and belongs to exactly one group, use it
    try:
        if getattr(current_user, 'is_authenticated', False):
            groups = current_user.groups
            if groups and len(groups) == 1:
                return groups[0]
    except Exception:
        pass
    return None


def _append_group_log(group: Group, message: str) -> None:
    """Append a timestamped message to the group's activity.log inside data/<data_folder>/activity.log
    Fall back to current_app.root_path/data/<folder>.
    """
    try:
        folder = (group.data_folder if getattr(group, 'data_folder', None) else None)
        if not folder:
            return
        base = os.path.join(current_app.root_path, 'data', folder)
        os.makedirs(base, exist_ok=True)
        p = os.path.join(base, 'activity.log')

        # Build JSON object for log entry. Accept either a dict-like message or a string.
        try:
            import json
            ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
            if isinstance(message, (dict, list)):
                obj = dict(message) if isinstance(message, dict) else {"value": message}
            else:
                # If message is a plain string, preserve it under 'message'
                obj = {"message": str(message)}

            # Add actor/username and email and user_id if not provided
            try:
                actor = getattr(current_user, 'username', None)
                if actor and 'actor' not in obj:
                    obj['actor'] = actor
            except Exception:
                actor = None
            try:
                uemail = getattr(current_user, 'email', None)
                if uemail and 'user_email' not in obj:
                    obj['user_email'] = uemail
            except Exception:
                pass
            try:
                uid = getattr(current_user, 'id', None)
                if uid and 'user_id' not in obj:
                    obj['user_id'] = uid
            except Exception:
                pass

            # Use standardized timestamp key 'timestamp' for admin panel; keep 'ts' for backward compat
            obj['timestamp'] = ts
            obj['ts'] = ts

            # Write one JSON object per line (JSON Lines format), UTF-8, do not escape non-ascii
            with open(p, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            # On JSON or write failure, fall back to plain text entry for resilience
            ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
            entry = f"{ts} - {message}\n"
            with open(p, 'a', encoding='utf-8') as fh:
                fh.write(entry)
    except Exception:
        try:
            current_app.logger.exception('Failed to append group log')
        except Exception:
            pass


@auth_bp.route('/groups/select', methods=['POST'])
@login_required
def select_group():
    """Set the active group for the session. Expects form param 'group' (group name).
    Only allows selecting groups the current user belongs to.
    Also automatically downloads group data from Firebase (lazy-pull).
    Returns JSON for AJAX requests, redirect for form submissions.
    """
    group_name = (request.form.get('group') or request.json.get('group') if request.is_json else request.form.get('group')) or ''
    group_name = (group_name or '').strip()
    if not group_name:
        if request.is_json:
            return jsonify({'ok': False, 'error': 'group required'}), 400
        flash('Η ομάδα είναι υποχρεωτική.', 'error')
        return redirect(url_for('auth.list_groups')), 400

    grp = Group.query.filter_by(name=group_name).first()
    if not grp:
        if request.is_json:
            return jsonify({'ok': False, 'error': 'group not found'}), 404
        flash('Η ομάδα δεν βρέθηκε.', 'error')
        return redirect(url_for('auth.list_groups')), 404

    # verify membership
    if grp not in current_user.groups:
        if request.is_json:
            return jsonify({'ok': False, 'error': 'not a member of group'}), 403
        flash('Δεν έχετε πρόσβαση στην ομάδα.', 'error')
        return redirect(url_for('auth.list_groups')), 403

    # Set active group and force explicit customer selection for this group.
    session['active_group'] = grp.name
    session.pop('active_credential', None)
    session.pop('_remote_qr_owner', None)
    current_app.logger.info(f'Ο χρήστης {current_user.username} επέλεξε την ομάδα {group_name}')
    
    # Ensure local group folder exists (server-authoritative mode; no automatic pull)
    try:
        import firebase_config
        if getattr(grp, 'data_folder', None):
            current_app.logger.info(f'Έλεγχος τοπικού φακέλου για την ομάδα {group_name}...')
            success = firebase_config.ensure_group_data_local(grp.data_folder)
            if success:
                current_app.logger.info(f'Ο τοπικός φάκελος της ομάδας {group_name} είναι έτοιμος')
            else:
                current_app.logger.warning(f'Αποτυχία προετοιμασίας τοπικού φακέλου για την ομάδα {group_name}')
    except Exception as e:
        # Log but don't fail - folder prep is non-critical
        current_app.logger.warning(f"Local folder prep failed when selecting group {group_name}: {e}")
    
    # Return JSON for AJAX requests, redirect for form submissions
    if request.is_json:
        return jsonify({
            'ok': True,
            'message': f'Επιλέχθηκε η ομάδα: {grp.name}. Επίλεξε πελάτη για να συνεχίσεις.',
            'data_folder': getattr(grp, 'data_folder', None),
            'redirect_url': url_for('credentials')
        }), 200
    else:
        flash(f'Επιλέχθηκε η ομάδα: {grp.name}. Επίλεξε πελάτη για να συνεχίσεις.', 'info')
        return redirect(url_for('credentials'))


# --- JSON API endpoints for frontend-driven login/logout/status ---
@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or request.form or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'ok': False, 'error': 'username and password required'}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'ok': False, 'error': 'invalid credentials'}), 401

    # clear previous active credential for the session
    session.pop('active_credential', None)
    session.pop('_remote_qr_owner', None)

    login_user(user)
    # create and store session id to prevent concurrent logins (DB-backed)
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
    
    # Sync user's groups from Firestore (if Firebase is enabled)
    # This ensures the user's group memberships are up-to-date
    try:
        firebase_uid = getattr(user, 'firebase_uid', None) or getattr(user, 'pw_hash', None)
        if firebase_uid:
            firebase_config.sync_user_groups_from_firestore(user.id, firebase_uid)
            logger.debug('User %s groups synced from Firestore on login', user.username)
    except Exception as e:
        logger.debug('Failed to sync groups from Firestore for user %s: %s', user.username, e)
        # Continue anyway - syncing is not critical
    
    return jsonify({'ok': True, 'username': user.username})


@auth_bp.route('/api/logout', methods=['POST'])
def api_logout():
    """API endpoint for logout. Handles both JSON and FormData requests.
    Optional 'reason' parameter can be 'manual', 'inactivity', or 'tab_close'.
    """
    # Extract reason if provided
    reason = None
    try:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            reason = data.get('reason', '').strip()
        else:
            reason = (request.form.get('reason') or '').strip()
    except Exception:
        pass

    reason = reason or 'manual'

    # Log logout activity if user is authenticated
    try:
        from utils import log_user_activity
        if getattr(current_user, 'is_authenticated', False):
            log_user_activity(
                user_id=current_user.id,
                group_name='system',
                action='logout',
                details={'reason': reason},
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
    except Exception:
        pass

    # Attempt to end DB-backed session if present
    try:
        from models import db as _db
        sid = session.get('session_id') or getattr(current_user, 'current_session_id', None)
        try:
            if getattr(current_user, 'is_authenticated', False):
                current_user.end_session(sid)
                _db.session.commit()
        except Exception:
            try:
                _db.session.rollback()
            except Exception:
                pass
    except Exception:
        pass

    try:
        logout_user()
    except Exception:
        pass
    # clear session state related to active credential
    session.pop('active_credential', None)
    session.pop('_remote_qr_owner', None)
    # also clear session_id from flask session
    session.pop('session_id', None)
    
    return jsonify({'ok': True, 'reason': reason})


@auth_bp.route('/api/user', methods=['GET'])
def api_user():
    if getattr(current_user, 'is_authenticated', False):
        return jsonify({'authenticated': True, 'username': current_user.username, 'groups': [g.name for g in current_user.groups]})
    return jsonify({'authenticated': False})


@auth_bp.route('/api/user_groups', methods=['GET'])
def api_user_groups():
    """Get list of groups for the authenticated user, including active group."""
    if not getattr(current_user, 'is_authenticated', False):
        return jsonify({'groups': [], 'active_group': None}), 401
    
    try:
        groups = []
        active_group = None
        
        for g in current_user.groups:
            groups.append({
                'name': g.name,
                'data_folder': g.data_folder,
            })
        
        # Get active group from session
        try:
            active_group_name = session.get('active_group')
            if active_group_name:
                # Verify user has access to this group
                if any(g.name == active_group_name for g in current_user.groups):
                    active_group = active_group_name
        except Exception:
            pass
        
        return jsonify({'groups': groups, 'active_group': active_group})
    except Exception as e:
        current_app.logger.exception("api_user_groups failed")
        return jsonify({'error': str(e)}), 500


# =====================================================================
# Email Verification & Password Reset
# =====================================================================

@auth_bp.route('/verify-email', methods=['GET'])
def verify_email():
    """Verify user email via token link"""
    # When using Firebase Authentication, email verification is handled by Firebase.
    # The generated Firebase verification link will verify the user's email in Firebase.
    # Here we simply inform the user and, if possible, sync local user record.
    email = request.args.get('email')
    if email:
        try:
            # Try to sync verification status from Firebase
            from firebase_admin import auth as firebase_auth
            fb_user = firebase_auth.get_user_by_email(email)
            if fb_user and getattr(fb_user, 'email_verified', False):
                # Update local user record if present
                user = User.query.filter_by(email=email).first()
                if user:
                    user.email_verified = True
                    user.email_verified_at = datetime.datetime.utcnow()
                    db.session.commit()
                    flash('Το email επαληθεύτηκε επιτυχώς! Μπορείτε τώρα να συνδεθείτε.', 'success')
                    return redirect(url_for('auth.login'))
        except Exception:
            pass

    flash('Η επαλήθευση email διαχειρίζεται από το Firebase. Παρακαλώ ελέγξτε τα εισερχόμενά σας και ακολουθήστε τον σύνδεσμο που στάλθηκε από το Firebase για να επαληθεύσετε το email σας.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Request password reset link"""
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        if not email:
            flash('Απαιτείται το email', 'danger')
            return redirect(url_for('auth.forgot_password'))
        
        try:
            # Use Firebase to generate password reset link and let Firebase handle sending.
            ok, link_or_err = FirebaseAuthHandler.generate_password_reset_link(email)
            if ok:
                try:
                    from firebed_email_verification import FirebedEmailVerification
                    sent = FirebedEmailVerification.send_password_reset_email(email)
                except Exception:
                    sent = False
                if sent:
                    flash('Εάν το email υπάρχει στο σύστημά μας, θα λάβετε σύνδεσμο επαναφοράς κωδικού (ελέγξτε τα εισερχόμενά σας).', 'info')
                    try:
                        from utils import log_user_activity
                        log_user_activity(
                            user_id=email,
                            group_name='system',
                            action='forgot_password_request',
                            details={'email': email, 'sent': True, 'description': 'Αποστολή συνδέσμου επαναφοράς'},
                            user_email=email,
                            user_username=email
                        )
                    except Exception:
                        pass
                else:
                    current_app.logger.info(f"Firebase password reset link for {email}: {link_or_err}")
                    flash('Εάν το email υπάρχει στο σύστημά μας, θα λάβετε σύνδεσμο επαναφοράς κωδικού (ελέγξτε τα εισερχόμενά σας).', 'info')
                    try:
                        from utils import log_user_activity
                        log_user_activity(
                            user_id=email,
                            group_name='system',
                            action='forgot_password_request',
                            details={'email': email, 'sent': False, 'link': reset_link, 'description': 'Καταγραφή συνδέσμου επαναφοράς (fallback)'},
                            user_email=email,
                            user_username=email
                        )
                    except Exception:
                        pass
            else:
                current_app.logger.warning(f"Could not generate Firebase password reset link: {link_or_err}")
                flash('Εάν το email υπάρχει στο σύστημά μας, θα λάβετε σύνδεσμο επαναφοράς κωδικού (ελέγξτε τα εισερχόμενά σας).', 'info')
            return redirect(url_for('auth.login'))
        except Exception as e:
            current_app.logger.exception('Forgot password failed')
            try:
                from utils import log_user_activity
                log_user_activity(
                    user_id=email if 'email' in locals() else 'unknown',
                    group_name='system',
                    action='forgot_password_request_error',
                    details={'email': email if 'email' in locals() else None, 'error': str(e), 'description': 'Σφάλμα κατά επεξεργασία αιτήματος επαναφοράς'},
                    user_email=email if 'email' in locals() else None,
                    user_username=email if 'email' in locals() else None
                )
            except Exception:
                pass
            flash('Σφάλμα κατά την επεξεργασία του αιτήματος επαναφοράς κωδικού', 'danger')
            return redirect(url_for('auth.forgot_password'))
    
    return render_template('auth/forgot_password.html')


@auth_bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    """Reset password via token link"""
    # With Firebase-based flows, password reset is handled by Firebase links.
    # Users should follow the link sent by Firebase to reset their password.
    flash('Η επαναφορά κωδικού διαχειρίζεται από το Firebase. Παρακαλώ χρησιμοποιήστε τον σύνδεσμο που στάλθηκε στο email σας για να επαναφέρετε τον κωδικό σας.', 'info')
    return redirect(url_for('auth.login'))
