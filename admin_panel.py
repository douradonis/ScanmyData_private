"""
Admin Panel Backend Module - User & Group Management
"""
import os
import json
import shutil
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from pathlib import Path
from models import db, User, Group, UserGroup
import firebase_config
from firebase_config import firebase_log_activity

logger = logging.getLogger(__name__)

# ============================================================================
# Admin Authorization
# ============================================================================

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))  # Set in .env


def is_admin(user: Optional[User]) -> bool:
    """Check if user is an admin (either by is_admin flag or ADMIN_USER_ID env)"""
    if not user:
        return False
    
    # First check the is_admin flag on the user model
    if hasattr(user, 'is_admin') and user.is_admin:
        return True
    
    # Fallback to ADMIN_USER_ID env variable for backwards compatibility
    from flask import current_app
    try:
        admin_id = int(current_app.config.get('ADMIN_USER_ID', ADMIN_USER_ID))
    except Exception:
        admin_id = ADMIN_USER_ID
    
    return user.id == admin_id if admin_id > 0 else False


# ============================================================================
# User Management
# ============================================================================

def admin_list_all_users(include_deleted: bool = False) -> List[Dict[str, Any]]:
    """List all users in the system"""
    try:
        users = User.query.all()
        result = []
        
        for user in users:
            try:
                active = bool(user.is_online())
            except Exception:
                active = False
            started_at = getattr(user, 'session_started_at', None)

            result.append({
                'id': user.id,
                'username': user.username,
                'email': getattr(user, 'email', None),
                'firebase_uid': getattr(user, 'pw_hash', None),
                'created_at': str(getattr(user, 'created_at', None)),
                'groups': [{'id': g.id, 'name': g.name} for g in user.groups],
                'group_count': len(user.groups),
                'is_admin': getattr(user, 'is_admin', False),
                'is_admin_of': [g.name for g in user.groups if user.role_for_group(g) == 'admin'],
                'active': active,
                'session_started_at': str(started_at) if started_at else None,
                'total_active_minutes': int((getattr(user, 'total_active_seconds', 0) or 0) / 60)
            })
        
        return result
    
    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        return []


def admin_get_user_details(user_id: int) -> Optional[Dict[str, Any]]:
    """Get detailed info about a specific user"""
    try:
        user = User.query.get(user_id)
        if not user:
            return None
        
        groups_info = []
        for g in user.groups:
            role = user.role_for_group(g)
            groups_info.append({
                'id': g.id,
                'name': g.name,
                'data_folder': g.data_folder,
                'role': role
            })
        
        # Calculate total size for user's groups
        total_size = 0
        for g in user.groups:
            data_path = os.path.join(os.getcwd(), 'data', g.data_folder) if getattr(g, 'data_folder', None) else None
            # If the server has no local data for this group, attempt lazy pull from Firebase
            try:
                if getattr(g, 'data_folder', None):
                    firebase_config.ensure_group_data_local(g.data_folder)
                if data_path and os.path.exists(data_path):
                    total_size += _get_folder_size(data_path)
            except Exception:
                continue

        # Fetch recent activity entries related to this user (best-effort)
        try:
            logs = admin_get_activity_logs(limit=200)
            recent = []
            user_keys = {str(user.id), str(getattr(user, 'username', '') or ''), str(getattr(user, 'email', '') or ''), str(getattr(user, 'firebase_uid', '') or '' )}
            for entry in logs:
                uid = str(entry.get('user_id') or '')
                # include if any of the identifying keys match
                if uid in user_keys or any(k and k in uid for k in user_keys):
                    recent.append(entry)
            recent = recent[:50]
        except Exception:
            recent = []

        # compute total active time from recorded sessions + current live session
        try:
            total_active_seconds = int(getattr(user, 'total_active_seconds', 0) or 0)
        except Exception:
            total_active_seconds = 0

        try:
            current_minutes = None
            if getattr(user, 'current_session_id', None) and getattr(user, 'session_started_at', None):
                now = datetime.utcnow()
                started = user.session_started_at
                elapsed = now - started
                current_minutes = int(elapsed.total_seconds() / 60)
        except Exception:
            current_minutes = None

        total_active_minutes = int(total_active_seconds / 60)
        if current_minutes:
            total_active_minutes += current_minutes

        return {
            'id': user.id,
            'username': user.username,
            'email': getattr(user, 'email', None),
            'created_at': str(getattr(user, 'created_at', None)),
            'groups': groups_info,
            'last_login': str(getattr(user, 'last_login', None)),
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'recent_activity': recent,
            'total_active_minutes': total_active_minutes,
            'current_session_minutes': current_minutes,
        }
    
    except Exception as e:
        logger.error(f"Failed to get user details: {e}")
        return None


def admin_delete_user(user_id: int, current_admin: User) -> Dict[str, Any]:
    """
    Delete a user and handle associated data
    - Remove user from Firebase
    - Clean up local files
    - Log action
    """
    try:
        user = User.query.get(user_id)
        if not user:
            return {'ok': False, 'error': 'User not found'}
        
        if user.id == current_admin.id:
            return {'ok': False, 'error': 'Cannot delete yourself'}
        
        # Protect main admin from deletion
        user_email = getattr(user, 'email', '')
        if user_email == 'adonis.douramanis@gmail.com':
            return {'ok': False, 'error': 'Cannot delete the main admin user'}
        
        username = user.username
        
        # Remove from all groups
        for ug in user.user_groups:
            db.session.delete(ug)
        
        # Try to delete from Firebase
        try:
            fb_user = firebase_config.firebase_get_user_by_email(user.username + "@firebed.local")
            if fb_user:
                firebase_config.firebase_delete_user(fb_user['uid'])
        except Exception as e:
            logger.warning(f"Failed to delete Firebase user: {e}")
        
        # Delete user from DB
        db.session.delete(user)
        db.session.commit()
        
        firebase_log_activity(current_admin.id, "admin", "user_deleted", {
            'deleted_user_id': user_id,
            'deleted_username': username
        })
        
        logger.info(f"Admin deleted user: {username}")
        return {'ok': True, 'message': f'User {username} deleted'}
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete user: {e}")
        return {'ok': False, 'error': str(e)}


def admin_force_unlock(user_id: int, current_admin: User) -> Dict[str, Any]:
    """Force-clear the DB session claim for a user (support/admin tool).
    Returns dict {'ok': bool, 'cleared_previous': bool, 'error': str}
    """
    try:
        user = User.query.get(user_id)
        if not user:
            return {'ok': False, 'error': 'user_not_found'}

        prev_sid = getattr(user, 'current_session_id', None)
        user.current_session_id = None
        user.session_started_at = None
        user.last_active_at = None
        db.session.commit()

        try:
            firebase_log_activity(
                getattr(current_admin, 'pw_hash', current_admin.id),
                'admin',
                'force_unlock',
                {'target_user_id': user.id, 'previous_session_id': prev_sid}
            )
        except Exception:
            logger.debug('Failed to write firebase_log_activity for force_unlock')

        logger.info(f"Admin {getattr(current_admin, 'username', current_admin.id)} force-unlocked user {user.username} (cleared_previous={bool(prev_sid)})")
        return {'ok': True, 'cleared_previous': bool(prev_sid)}
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.error(f"Failed to force-unlock user {user_id}: {e}")
        return {'ok': False, 'error': str(e)}


# ============================================================================
# Group Management
# ============================================================================

def admin_list_all_groups() -> List[Dict[str, Any]]:
    """List all groups in the system"""
    try:
        groups = Group.query.all()
        result = []
        
        for group in groups:
            member_roles = {}
            for ug in group.user_groups:
                member_roles[ug.user.username] = ug.role
            
            # Attempt lazy-pull if group data missing locally
            data_folder = getattr(group, 'data_folder', None)
            if data_folder:
                try:
                    firebase_config.ensure_group_data_local(data_folder)
                except Exception as e:
                    logger.debug(f"Lazy-pull failed for group {group.name}: {e}")
            
            # Calculate folder size (now that lazy-pull has been attempted)
            folder_size = 0
            if data_folder:
                try:
                    data_path = os.path.join(os.getcwd(), 'data', data_folder)
                    if os.path.exists(data_path):
                        folder_size = _get_folder_size(data_path)
                except Exception as e:
                    logger.debug(f"Failed to calculate folder size for {group.name}: {e}")
            
            result.append({
                'id': group.id,
                'name': group.name,
                'group_name': group.name,  # keep alias for backward compatibility
                'data_folder': group.data_folder,
                'members_count': len(group.user_groups),
                'admins': [u.user.username for u in group.user_groups if u.role == 'admin'],
                'created_at': str(getattr(group, 'created_at', None)),
                'folder_size_mb': round(folder_size / (1024 * 1024), 2)
            })
        
        return result
    
    except Exception as e:
        logger.error(f"Failed to list groups: {e}")
        return []


def admin_get_group_details(group_id: int) -> Optional[Dict[str, Any]]:
    """Get detailed info about a group"""
    try:
        group = Group.query.get(group_id)
        if not group:
            return None
        
        members = []
        for ug in group.user_groups:
            members.append({
                'user_id': ug.user_id,
                'username': ug.user.username,
                'role': ug.role
            })
        
        # Get data size. If local data folder missing, try to pull from Firebase lazily
        data_path = os.path.join(os.getcwd(), 'data', group.data_folder)
        try:
            firebase_config.ensure_group_data_local(group.data_folder)
        except Exception as e:
            logger.debug(f"Lazy-pull failed for group {group.name}: {e}")
        folder_size = _get_folder_size(data_path) if os.path.exists(data_path) else 0
        
        return {
            'id': group.id,
            'name': group.name,
            'data_folder': group.data_folder,
            'members': members,
            'folder_size_mb': round(folder_size / (1024 * 1024), 2),
            'created_at': str(getattr(group, 'created_at', None))
        }
    
    except Exception as e:
        logger.error(f"Failed to get group details: {e}")
        return None


def admin_delete_group(group_id: int, current_admin: User, backup_first: bool = True, active_client_name: str = None) -> Dict[str, Any]:
    """
    Delete a group and its data
    - Backup data if requested
    - Remove from Firebase
    - Delete local files
    """
    try:
        group = Group.query.get(group_id)
        if not group:
            return {'ok': False, 'error': 'Group not found'}
        
        group_name = group.name
        data_folder = group.data_folder
        
        # Create backup if requested and if data folder exists
        backup_path = None
        data_path = os.path.join(os.getcwd(), 'data', data_folder) if data_folder else None
        if backup_first and data_path and os.path.exists(data_path):
            backup_path = admin_backup_group(group_id)
            if not backup_path:
                return {'ok': False, 'error': 'Failed to create backup before deletion'}
        
        # Remove all members
        for ug in group.user_groups:
            db.session.delete(ug)
        
        # Delete from Firebase - all related data
        try:
            firebase_config.firebase_delete_data(f'/groups/{group_name}')
            firebase_config.firebase_delete_data(f'/group_encryption_keys/{group_name}')
            firebase_config.firebase_delete_data(f'/activity_logs/{group_name}')
            firebase_config.firebase_delete_data(f'/receipts/{group_name}')
            firebase_config.firebase_delete_data(f'/group_settings/{group_name}')
            logger.info(f"Deleted all Firebase data for group: {group_name}")
        except Exception as e:
            logger.warning(f"Failed to delete Firebase group data: {e}")
        
        # Delete local folder
        data_path = os.path.join(os.getcwd(), 'data', data_folder)
        if os.path.exists(data_path):
            shutil.rmtree(data_path)
        
        # Delete group from DB
        db.session.delete(group)
        db.session.commit()
        
        group_display = group_name
        if active_client_name:
            group_display = f"{group_name} | {active_client_name}"
        firebase_log_activity(current_admin.id, "admin", "group_deleted", {
            'group_id': group_id,
            'group_name': group_display,
            'backup_path': backup_path
        })
        logger.info(f"Admin deleted group: {group_display}")
        return {
            'ok': True,
            'message': f'Group {group_name} deleted',
            'backup_path': backup_path
        }
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to delete group: {e}")
        return {'ok': False, 'error': str(e)}


# ============================================================================
# Backup & Restore
# ============================================================================

def admin_backup_group(group_id: int, active_client_name: str = None, auto: bool = False) -> Optional[str]:
    """
    Create a backup of a group's data
    Returns path to backup file
    """
    try:
        group = Group.query.get(group_id)
        if not group:
            return None
        
        # Ensure group data exists locally.  ``ensure_group_data_local``
        # may spawn a background thread; if we return immediately the
        # directory could still be empty when we copy it, so we attempt a
        # synchronous pull if the folder is too small.
        try:
            firebase_config.ensure_group_data_local(group.data_folder)
        except Exception as e:
            logger.debug(f"Lazy-pull failed for backup of {group.name}: {e}")
        
        data_path = os.path.join(os.getcwd(), 'data', group.data_folder)
        if not os.path.exists(data_path):
            logger.warning(f"Group data folder not found after lazy-pull attempt: {data_path}")
            return None
        # if folder only contains few files, try to pull synchronously as well
        try:
            if len(list(os.scandir(data_path))) < 3:
                logger.info("Performing synchronous pull for group %s before backup", group.name)
                firebase_config.firebase_pull_group_to_local(group.data_folder, os.path.join(os.getcwd(), 'data'))
        except Exception as e:
            logger.debug("Synchronous pull attempt failed: %s", e)
        
        # Create backups folder
        backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
        os.makedirs(backups_dir, exist_ok=True)
        
        # Create timestamped backup
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        backup_name = f"{group.data_folder}_backup_{timestamp}"
        backup_path = os.path.join(backups_dir, backup_name)
        
        # Copy directory
        shutil.copytree(data_path, backup_path)
        
        group_display = group.name
        if active_client_name:
            group_display = f"{group.name} | {active_client_name}"
        logger.info(f"Group backup created: {backup_path} for {group_display}")
        if auto:
            logger.info(f"[AUTO BACKUP] Group: {group_display}, Path: {backup_path}")
        return backup_path
    
    except Exception as e:
        logger.error(f"Failed to backup group {group_id}: {e}")
        return None


def admin_list_backups(group_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """List available backups"""
    try:
        backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
        if not os.path.exists(backups_dir):
            return []
        
        backups = []
        for item in os.listdir(backups_dir):
            item_path = os.path.join(backups_dir, item)
            if os.path.isdir(item_path):
                size = _get_folder_size(item_path)
                stat = os.stat(item_path)
                
                # format size: round to two decimals, but never show 0 for non-empty
                size_mb = size / (1024 * 1024)
                if size_mb < 0.01 and size > 0:
                    size_human = '<0.01'
                else:
                    size_human = f"{round(size_mb,2)}"

                backups.append({
                    'name': item,
                    'path': item_path,
                    'size_mb': size_mb,
                    'size_human': size_human,
                    'created_at': datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        
        # Sort by creation date descending
        backups.sort(key=lambda x: x['created_at'], reverse=True)
        return backups
    
    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
        return []


def admin_list_remote_backups(prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """List backups stored in Firebase under /backups. Optionally filter by prefix (e.g. group name)."""
    try:
        if not firebase_config.is_firebase_enabled():
            return []

        data = firebase_config.firebase_read_data('/backups') or {}
        results = []

        # data structure is expected to be { category: { timestamp: payload } }
        for cat, entries in data.items():
            if not isinstance(entries, dict):
                continue
            for ts_key, payload in entries.items():
                # build a simple name/path
                name = f"/backups/{cat}/{ts_key}"
                if prefix and prefix not in name:
                    continue
                # attempt to compute an approximate size
                try:
                    size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                except Exception:
                    size = 0

                created_at = None
                try:
                    # ts_key may be ISO timestamp
                    created_at = ts_key
                except Exception:
                    created_at = ''

                results.append({
                    'name': name,
                    'category': cat,
                    'key': ts_key,
                    'size_bytes': size,
                    'size_mb': round(size / (1024 * 1024), 2),
                    'created_at': created_at
                })

        # sort newest first by created_at (string compare OK for ISO timestamps)
        results.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return results
    except Exception as e:
        logger.error(f"Failed to list remote backups: {e}")
        return []


def admin_delete_remote_backup(backup_path: str, current_admin: User) -> Dict[str, Any]:
    """Delete a backup entry stored in Firebase. `backup_path` should be like '/backups/<cat>/<key>'"""
    try:
        if not firebase_config.is_firebase_enabled():
            return {'ok': False, 'error': 'Firebase not enabled'}

        # sanitize path: ensure it starts with /backups
        if not backup_path.startswith('/backups'):
            return {'ok': False, 'error': 'Invalid backup path'}

        ok = firebase_config.firebase_delete_data(backup_path)
        if ok:
            # include human-readable message so activity log isn't vague
            firebase_log_activity(current_admin.id, '__admin__', 'backup_deleted', {
                'backup_path': backup_path,
                'message': f'Διαγραφή backup {backup_path}'
            })
            return {'ok': True, 'message': f'Deleted backup {backup_path}'}
        return {'ok': False, 'error': 'Failed to delete backup in Firebase'}
    except Exception as e:
        logger.error(f"Failed to delete remote backup {backup_path}: {e}")
        return {'ok': False, 'error': str(e)}


def admin_restore_remote_backup(backup_path: str, target_group_id: int, groups_to_restore: Optional[List[str]] = None, current_admin: Optional[User] = None) -> Dict[str, Any]:
    """Restore one or more groups from a Firebase backup entry.

    - `backup_path` : Firebase path like '/backups/<cat>/<key>'
    - `target_group_id` : DB Group id to map single-group restores; ignored when groups_to_restore provided
    - `groups_to_restore` : list of group folder names to restore from the backup; if None and backup contains multiple groups, will try to restore the single group matched by target_group_id
    """
    try:
        if not firebase_config.is_firebase_enabled():
            return {'ok': False, 'error': 'Firebase not enabled'}

        # Read backup data
        data = firebase_config.firebase_read_data(backup_path)
        if not data:
            return {'ok': False, 'error': 'Backup not found or empty'}

        # If groups_to_restore not provided, infer from either target_group_id or backup_path
        restore_groups = []
        if groups_to_restore:
            restore_groups = groups_to_restore
        else:
            # if caller supplied a target_group_id, try to map it
            if target_group_id:
                group = Group.query.get(target_group_id)
                if group:
                    gf = group.data_folder
                    if isinstance(data.get('groups'), dict) and gf in data.get('groups'):
                        restore_groups = [gf]
            # if still nothing, attempt to infer from the path itself
            if not restore_groups:
                parts = backup_path.strip('/').split('/')
                if len(parts) >= 2:
                    restore_groups = [parts[1]]

        if not restore_groups:
            return {'ok': False, 'error': 'No groups determined to restore'}

        restored = []
        for gname in restore_groups:
            # If full-backup structure (has 'groups' key)
            if isinstance(data.get('groups'), dict) and gname in data['groups']:
                group_blob = data['groups'][gname]
            else:
                # fallback: top-level payload for single group backups
                # backup path like /backups/<group>/<key>
                group_blob = data if isinstance(data, dict) else None

            if not group_blob:
                continue

            # Write to Firebase groups path
            ok = firebase_config.firebase_import_group_data(gname, group_blob)
            # Also write to local folder
            try:
                local_dir = os.path.join(os.getcwd(), 'data', gname)
                # remove existing local data (safety: keep a pre-restore backup)
                if os.path.exists(local_dir):
                    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
                    pre = os.path.join(os.getcwd(), 'data', '_backups', f"{gname}_pre_remote_restore_{timestamp}")
                    shutil.copytree(local_dir, pre)
                    shutil.rmtree(local_dir)
                os.makedirs(local_dir, exist_ok=True)
                # materialize keys as JSON files where appropriate
                if isinstance(group_blob, dict):
                    for k, v in group_blob.items():
                        safe = str(k).lstrip('/').replace('/', '_')
                        target_path = os.path.join(local_dir, f"{safe}.json")
                        try:
                            with open(target_path, 'w', encoding='utf-8') as fh:
                                json.dump(v, fh, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"Failed to write local data for restored group {gname}: {e}")

            restored.append(gname)

        if current_admin:
            # provide a Greek message and list of restored groups for clarity
            msg = f"Επαναφορά από απομακρυσμένο backup {backup_path} → ομάδες {', '.join(restored)}"
            firebase_log_activity(current_admin.id, '__admin__', 'remote_backup_restored', {
                'backup_path': backup_path,
                'restored_groups': restored,
                'message': msg
            })
            # also log to server log in Greek for easier grep
            try:
                from flask import current_app
                current_app.logger.info(msg)
            except Exception:
                logger.info(msg)

        if not restored:
            return {'ok': False, 'error': 'No groups restored'}
        return {'ok': True, 'restored': restored}

    except Exception as e:
        logger.error(f"Failed to restore remote backup {backup_path}: {e}")
        return {'ok': False, 'error': str(e)}


def admin_get_backup_zip(backup_name: str) -> Optional[str]:
    """
    Create a zip archive for a named backup folder and return the zip path.
    The zip will be created alongside the backup folder in the _backups directory.
    """
    try:
        backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
        backup_path = os.path.join(backups_dir, backup_name)
        if not os.path.exists(backup_path):
            logger.warning(f"Backup not found for zipping: {backup_path}")
            return None

        zip_base = os.path.join(backups_dir, f"{backup_name}")
        zip_path = f"{zip_base}.zip"

        # If zip already exists, reuse it
        if os.path.exists(zip_path):
            return zip_path

        # Create an archive (zip)
        shutil.make_archive(zip_base, 'zip', backup_path)
        if os.path.exists(zip_path):
            logger.info(f"Created backup zip: {zip_path}")
            return zip_path
        return None

    except Exception as e:
        logger.error(f"Failed to create zip for backup {backup_name}: {e}")
        return None


def admin_restore_backup(backup_name: str, target_group_id: int, current_admin: User) -> Dict[str, Any]:
    """Restore a group from backup

    This function is called from both the admin API (AJAX endpoint) and
    various internal scripts.  Historically it used the module logger,
    which was never configured and therefore messages did not appear in
    ``firebed.log``.  We now log via ``current_app.logger`` when running
    inside a Flask context, and we emit extra debug details so failures
    are easier to diagnose.
    """
    # normalize backup_name by removing any leading slash; callers may pass
    # "/backups/…" when they inadvertently include the firebase path.
    if isinstance(backup_name, str):
        backup_name = backup_name.lstrip('/')

    # prefer the flask logger if available so we end up in the same log
    try:
        from flask import current_app
        log = current_app.logger
    except Exception:
        log = logger  # fallback to module-level logger

    log.debug("admin_restore_backup called: backup_name=%s, target_group_id=%s", backup_name, target_group_id)

    try:
        group = Group.query.get(target_group_id)
        if not group:
            log.warning("restore target group not found: %s", target_group_id)
            return {'ok': False, 'error': 'Target group not found'}
        
        backup_path = os.path.join(os.getcwd(), 'data', '_backups', backup_name)
        if not os.path.exists(backup_path):
            log.warning("backup path does not exist: %s", backup_path)
            return {'ok': False, 'error': 'Backup not found'}
        
        data_path = os.path.join(os.getcwd(), 'data', group.data_folder)
        
        # Create safety backup of current data
        if os.path.exists(data_path):
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            safety_backup = os.path.join(os.getcwd(), 'data', '_backups', f"{group.data_folder}_pre_restore_{timestamp}")
            shutil.copytree(data_path, safety_backup)
            log.info(f"Safety backup created: {safety_backup}")
        
        # Remove current data
        if os.path.exists(data_path):
            shutil.rmtree(data_path)
        
        # Restore from backup
        shutil.copytree(backup_path, data_path)
        
        firebase_log_activity(current_admin.id, "admin", "group_restored", {
            'group_id': target_group_id,
            'group_name': group.name,
            'backup_name': backup_name
        })
        
        log.info(f"Group restored from backup: {group.name}")
        return {'ok': True, 'message': f'Group {group.name} restored from {backup_name}'}
    
    except Exception as e:
        log.exception("Failed to restore backup")
        return {'ok': False, 'error': str(e)}


# ============================================================================
# Activity & Traffic Logs
# ============================================================================

def _create_detailed_description(action: str, details: Dict[str, Any], entry: Optional[Dict[str, Any]] = None) -> str:
    """Create detailed description in Greek for activity logs
    
    Args:
        action: The action type
        details: The details dict (may be empty)
        entry: The full entry object (for accessing entry-level fields when details is empty)
    """
    try:
        # Helper to get a field from details first, then fall back to entry level
        def get_field(key: str, default=None):
            # Check top-level details
            try:
                if details and isinstance(details, dict):
                    if key in details and details.get(key):
                        return details.get(key)
                    # Check nested details inside details (common wrapper)
                    nested = details.get('details')
                    if isinstance(nested, dict) and key in nested and nested.get(key):
                        return nested.get(key)
                # Check entry-level keys
                if entry and isinstance(entry, dict):
                    if key in entry and entry.get(key):
                        return entry.get(key)
                    nested_e = entry.get('details')
                    if isinstance(nested_e, dict) and key in nested_e and nested_e.get(key):
                        return nested_e.get(key)
            except Exception:
                pass
            return default
        
        # Normalize legacy action aliases to canonical actions
        if action in ['remove_member']:
            action = 'remove_user'
        if action in ['add_member', 'invite_member']:
            action = 'add_user'
        if action in ['group_deleted']:
            action = 'group_delete'

        if action == 'export_bridge':
            # Handle nested details structure for bridge exports
            actual_details = details.get('details', details)
            book_category = actual_details.get('book_category', 'Άγνωστο')
            rows_count = actual_details.get('rows_count', 0)
            file_size_mb = actual_details.get('file_size_mb', 0)
            file_name = actual_details.get('file_name', 'Άγνωστο')
            includes_b_kat = actual_details.get('includes_b_kat', False)
            
            category_name = {
                'A': 'Αρχείο Εσόδων',
                'B': 'Βιβλίο Εσόδων',
                'C': 'Βιβλίο Εξόδων',
                'D': 'Αρχείο Εξόδων',
                'E': 'Βιβλίο Παγίων',
                'Β': 'Β Κατηγορία',
                'Γ': 'Γ Κατηγορία',
                'G': 'Γ Κατηγορία'
            }.get(book_category, f'Κατηγορία {book_category}')
            
            includes_g_kat = actual_details.get('includes_g_kat', False)
            b_kat_text = " (συμπεριλαμβάνει Β' Κατηγορία)" if includes_b_kat else ""
            g_kat_text = " (συμπεριλαμβάνει Γ.ect)" if includes_g_kat else ""
            kat_text = b_kat_text or g_kat_text
            
            # Safely format file_size_mb
            try:
                size_text = f"{float(file_size_mb):.2f}"
            except (ValueError, TypeError):
                size_text = str(file_size_mb)
            
            return f"Λήψη γέφυρας {category_name}{kat_text}. {rows_count} γραμμές, κατηγορία βιβλίων {book_category}, μέγεθος {size_text} MB, όνομα αρχείου: {file_name}"
        
        elif action == 'export_expenses':
            # Handle export_expenses action with enhanced details
            actual_details = details.get('details', details)
            rows_count = actual_details.get('rows_count', 0)
            file_size_mb = actual_details.get('file_size_mb', 0)
            file_name = actual_details.get('file_name', 'Άγνωστο')
            book_category = actual_details.get('book_category', 'mixed')
            
            # Translate book_category to Greek
            category_translations = {
                'invoices': 'Τιμολόγια',
                'expenses': 'Έξοδα',
                'receipts': 'Αποδείξεις',
                'mixed': 'Μικτό'
            }
            category_name = category_translations.get(book_category, book_category)
            
            # Safely format file_size_mb
            try:
                size_text = f"{float(file_size_mb):.2f}"
            except (ValueError, TypeError):
                size_text = str(file_size_mb)
            
            return f"Λήψη εξοδολογίου κατηγορίας {category_name} ({rows_count} γραμμές, {size_text} MB) - {file_name}"
        
        elif action == 'delete_rows':
            # Handle delete rows action
            actual_details = details.get('details', details)
            count = actual_details.get('count', 0)
            excel_path = actual_details.get('excel_path', 'Άγνωστο αρχείο')
            
            return f"Διαγραφή {count} γραμμών από αρχείο: {excel_path}"

        elif action in ['add_user', 'assign_user']:
            # details: target_user, role, group, actor/user_email
            target = get_field('target_user') or get_field('user') or get_field('username') or 'Άγνωστος'
            role = get_field('role') or ''
            group = get_field('group') or get_field('group_name') or '-'
            actor = get_field('user_email') or get_field('actor') or get_field('user_username') or '-'
            role_text = f" ως {role}" if role else ''
            return f"Προσθήκη χρήστη {target}{role_text} από {actor} στην ομάδα {group}"

        elif action == 'remove_user':
            target = get_field('target_user') or get_field('removed_user') or get_field('user') or get_field('username')
            actor = get_field('actor') or get_field('user_email') or get_field('user_username')
            group = get_field('group') or get_field('group_name')
            
            # If missing fields, try to parse legacy message format: "X removed member Y"
            if not target or not actor:
                msg = details.get('message', '')
                if ' removed member ' in msg:
                    try:
                        parts = msg.split(' removed member ', 1)
                        if len(parts) == 2:
                            if not actor:
                                actor = parts[0].strip()
                            if not target:
                                target = parts[1].strip()
                    except Exception:
                        pass
            
            target = target or 'Άγνωστος'
            actor = actor or '-'
            group = group or '-'
            return f"Αφαίρεση χρήστη {target} από ομάδα {group} (από {actor})"

        elif action == 'create_group':
            group = get_field('group') or get_field('group_name') or '-'
            # Try nested details.details.created_by, then details.created_by, then entry.created_by, etc.
            creator = (details.get('details', {}).get('created_by') if isinstance(details.get('details'), dict) else None) or get_field('created_by') or get_field('user') or '-'
            return f"Δημιουργία ομάδας {group} από {creator}"

        elif action == 'group_delete':
            group = get_field('group') or get_field('group_name') or '-'
            actor = get_field('user_email') or get_field('actor') or get_field('user_username') or '-'
            reason = (details.get('details', {}).get('reason') if isinstance(details.get('details'), dict) else None) or get_field('reason') or ''
            reason_text = f" (αιτία: {reason})" if reason else ''
            return f"Διαγραφή ομάδας: {group}{reason_text} από {actor}"

        elif action == 'leave_group':
            who = get_field('user') or get_field('user_username') or get_field('actor') or '-'
            group = get_field('group') or get_field('group_name') or '-'
            return f"Αποχώρηση χρήστη {who} από ομάδα {group}"
        
        elif action in ['fetch_data', 'ληψη παραστατικων']:
            # details may be nested under 'details' or present directly
            actual = details.get('details', details) if isinstance(details, dict) else details
            date_from = actual.get('date_from') or actual.get('από') or ''
            date_to = actual.get('date_to') or actual.get('έως') or ''
            vat = actual.get('client_vat') or actual.get('vat') or actual.get('πελατης') or ''
            added_docs = actual.get('added_docs') or 0
            added_summaries = actual.get('added_summaries') or actual.get('summaries') or 0
            return f"Λήψη παραστατικών για πελάτη {vat} από {date_from} έως {date_to}. Προστέθηκαν {added_docs} έγγραφα, {added_summaries} συνοψίσεις."

        elif action in ['user_logged_in', 'login']:
            ip_address = details.get('ip_address', 'Άγνωστο')
            return f"Σύνδεση από IP: {ip_address}"
        
        elif action == 'logout':
            ip_address = details.get('ip_address', 'Άγνωστο')
            return f"Αποσύνδεση από IP: {ip_address}"
        
        elif action in ['user_signup_complete', 'user_registered']:
            ip_address = details.get('ip_address', 'Άγνωστο')
            return f"Εγγραφή νέου χρήστη από IP: {ip_address}"
        
        elif action == 'password_changed':
            return "Αλλαγή κωδικού πρόσβασης"

        elif action == 'password_reset_requested' or action == 'forgot_password_request':
            # details may contain 'email' or 'user'
            email = get_field('email') or get_field('user_email') or get_field('user') or get_field('actor') or '-'
            return f"Αίτηση επαναφοράς κωδικού για {email} — αποστολή email επαναφοράς"

        elif action in ['password_reset_email_sent']:
            email = get_field('email') or get_field('user_email') or get_field('user') or get_field('actor') or '-'
            return f"Αποστολή email επαναφοράς κωδικού σε {email}"

        elif action in ['forgot_password_request_error', 'password_reset_error']:
            reason = get_field('reason') or get_field('message') or get_field('error') or ''
            reason_text = f": {reason}" if reason else ''
            email = get_field('email') or get_field('user_email') or get_field('user') or '-'
            return f"Σφάλμα κατά την αίτηση επαναφοράς κωδικού για {email}{reason_text}"

        elif action == 'password_reset_completed':
            return "Ολοκλήρωση επαναφοράς κωδικού μέσω email"
        
        elif action == 'verification_email_sent':
            return "Αποστολή email επαλήθευσης λογαριασμού"
        
        elif action in ['delete_user', 'admin_delete_user']:
            target_user = None
            if isinstance(details, dict):
                # details may wrap real details under 'details'
                inner = details.get('details', details)
            else:
                inner = details or {}
            target_user = inner.get('target_user_email') or inner.get('deleted_user') or inner.get('user_email') or 'Άγνωστος'
            admin_text = " (από admin)" if action.startswith('admin_') else ""
            return f"Διαγραφή χρήστη: {target_user}{admin_text}"

        elif action in ['delete_backup', 'admin_delete_backup', 'backup_deleted']:
            # message field is sometimes included for clarity
            if isinstance(details, dict) and details.get('message'):
                return details.get('message')

            # Use get_field helper so we check both details and entry-level keys
            backup_name = get_field('backup_name') or get_field('backup') or get_field('name')
            if not backup_name:
                bp = get_field('backup_path') or get_field('path')
                if bp:
                    try:
                        parts = str(bp).strip('/').split('/')
                        backup_name = '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
                    except Exception:
                        backup_name = None
            # Last-resort: search any textual fields for a '/backups/...' pattern
            if not backup_name:
                try:
                    import re, json
                    hay = ''
                    try:
                        hay = json.dumps(entry or {}, ensure_ascii=False)
                    except Exception:
                        hay = str(entry or '')
                    try:
                        if details:
                            hay += ' ' + json.dumps(details, ensure_ascii=False)
                    except Exception:
                        hay += ' ' + str(details or '')
                    m = re.search(r'/backups/([^\s"\']+/?[^\s"\']*)', hay)
                    if m:
                        bp2 = m.group(0)
                        parts = str(bp2).strip('/').split('/')
                        backup_name = '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
                except Exception:
                    pass
            if not backup_name:
                backup_name = 'Άγνωστο'
            admin_text = " (από admin)" if action.startswith('admin_') else ""
            return f"Διαγραφή backup: {backup_name}{admin_text}"

        elif action == 'remote_backup_restored':
            # details may contain backup_path and restored_groups list
            if isinstance(details, dict):
                bp = details.get('backup_path') or details.get('path')
                groups = details.get('restored_groups') or []
                if bp:
                    if groups:
                        return f"Επαναφορά από απομακρυσμένο backup {bp} σε ομάδες {', '.join(groups)}"
                    return f"Επαναφορά από απομακρυσμένο backup {bp}"
            return "Επαναφορά απομακρυσμένου backup"

        elif action in ['backup_created', 'backup_upload', 'backup_saved']:
            # Creation/upload of a backup
            actual_details = details.get('details', details) if isinstance(details, dict) else details
            backup_name = None
            try:
                if isinstance(actual_details, dict):
                    backup_name = actual_details.get('backup_name') or actual_details.get('backup_path') or actual_details.get('name')
            except Exception:
                backup_name = None
            if not backup_name:
                try:
                    bp = actual_details.get('backup_path') or actual_details.get('path')
                    if bp:
                        parts = str(bp).strip('/').split('/')
                        backup_name = '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
                except Exception:
                    backup_name = None
            if not backup_name:
                backup_name = 'Άγνωστο'
            admin_text = " (από admin)" if action.startswith('admin_') else ""
            return f"Δημιουργία backup: {backup_name}{admin_text}"

        elif action in ['backup_download', 'backup_downloaded', 'backup_retrieved']:
            # Backup download event (admin or user)
            actual_details = details.get('details', details) if isinstance(details, dict) else details
            backup_name = get_field('backup_name') or get_field('backup') or None
            if not backup_name:
                try:
                    if isinstance(actual_details, dict):
                        backup_name = actual_details.get('backup_name') or actual_details.get('backup')
                except Exception:
                    backup_name = None
            if not backup_name:
                bp = get_field('backup_path') or (actual_details.get('backup_path') if isinstance(actual_details, dict) else None)
                if bp:
                    try:
                        parts = str(bp).strip('/').split('/')
                        backup_name = '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
                    except Exception:
                        backup_name = None
            # If backup_name still missing, try common fallback keys like file_name or zip_path
            if not backup_name:
                try:
                    fn = get_field('file_name') or (actual_details.get('file_name') if isinstance(actual_details, dict) else None)
                    if fn:
                        backup_name = fn
                    else:
                        zp = get_field('zip_path') or (actual_details.get('zip_path') if isinstance(actual_details, dict) else None)
                        if zp:
                            import os
                            backup_name = os.path.basename(str(zp))
                except Exception:
                    backup_name = None
            if not backup_name:
                backup_name = 'Άγνωστο'
            groups_count = None
            try:
                if isinstance(actual_details, dict):
                    groups_count = actual_details.get('groups_count') or actual_details.get('groups') or None
            except Exception:
                groups_count = None
            gc_text = f" ({groups_count} ομάδες)" if groups_count else ''
            admin_text = " (από admin)" if action.startswith('admin_') else ""
            return f"Λήψη αντιγράφου ασφαλείας: {backup_name}{gc_text}{admin_text}"

        elif action in ['send_email', 'admin_send_email']:
            # Accept several shapes: details may already be the outer log entry (which wraps
            # the real details under a 'details' key) or may be the inner details dict.
            actual_details = details.get('details', details) if isinstance(details, dict) else details

            # Determine recipient count from multiple possible shapes used in logging
            recipient_count = 0
            try:
                if isinstance(actual_details.get('recipients'), list):
                    recipient_count = len(actual_details.get('recipients'))
                elif isinstance(actual_details.get('recipient_count'), int):
                    recipient_count = int(actual_details.get('recipient_count'))
                elif isinstance(actual_details.get('user_count'), int):
                    recipient_count = int(actual_details.get('user_count'))
                elif isinstance(actual_details.get('sent'), int):
                    recipient_count = int(actual_details.get('sent'))
                else:
                    # Fallback: if recipients is present but not a list, try to compute length
                    recs = actual_details.get('recipients')
                    if recs and isinstance(recs, str):
                        # comma separated
                        recipient_count = len([r for r in recs.split(',') if r.strip()])
            except Exception:
                recipient_count = 0
            admin_text = " (από admin)" if action.startswith('admin_') else ""
            return f"Αποστολή email σε {recipient_count} παραλήπτες{admin_text}"
        
        elif action == 'group_deleted':
            group_name = details.get('group_name') or 'Άγνωστο'
            return f"Διαγραφή ομάδας: {group_name}"
        
        elif action == 'group_restored':
            group_name = details.get('group_name') or 'Άγνωστο'
            return f"Επαναφορά ομάδας: {group_name}"
        
        elif action == 'backup_deleted':
            # duplicate of previous case kept for compatibility; fall through above
            if isinstance(details, dict) and details.get('message'):
                return details.get('message')
            backup_name = get_field('backup_name') or get_field('backup') or get_field('name')
            if not backup_name:
                bp = get_field('backup_path') or get_field('path')
                if bp:
                    try:
                        parts = str(bp).strip('/').split('/')
                        backup_name = '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
                    except Exception:
                        backup_name = None
            if not backup_name:
                backup_name = 'Άγνωστο'
            return f"Διαγραφή backup: {backup_name}"
        
        elif action == 'user_deleted':
            return "Διαγραφή λογαριασμού χρήστη"
        
        elif action == 'delete_rows':
            # Handle delete rows action
            actual_details = details.get('details', details)
            count = actual_details.get('count', 0)
            excel_path = actual_details.get('excel_path', 'Άγνωστο αρχείο')
            
            return f"Διαγραφή {count} γραμμών από αρχείο: {excel_path}"
        
        # Default fallback - only use existing description if no custom handling
        description = details.get('description', '')
        if description and action not in ['export_bridge']:  # Don't use existing description for actions we handle
            return description
        
        # If no specific handling, return a generic description
        return f"Ενέργεια: {action}"
        
    except Exception as e:
        logger.error(f"Error creating detailed description for {action}: {e}")
        return f"Ενέργεια: {action}"


def admin_get_activity_logs(group_name: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Get activity logs from Firebase"""
    try:
        if group_name:
            logs = firebase_config.firebase_get_group_activity_logs(group_name, limit) or []

            # If Firebase returned no logs (or fewer than requested), try the local
            # per-group activity.log fallback so admin panel still shows recent activity
            try:
                import os, json
                data_dir = os.path.join(os.getcwd(), 'data')
                group_dir = os.path.join(data_dir, str(group_name))
                # Prefer normalized JSONL if present (created by migration), else fallback to legacy activity.log
                activity_jsonl = os.path.join(group_dir, 'activity.log.jsonl')
                activity_path = os.path.join(group_dir, 'activity.log')
                local_lines = []
                # If either local JSONL or legacy activity.log exists, read it and normalize into local_lines
                if os.path.exists(activity_jsonl) or os.path.exists(activity_path):
                    # Prefer JSONL when available
                    chosen_path = activity_jsonl if os.path.exists(activity_jsonl) else activity_path
                    is_jsonl = chosen_path.endswith('.jsonl')
                    with open(chosen_path, 'r', encoding='utf-8') as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            # If JSONL, try to parse each line as JSON first
                            parsed = None
                            if is_jsonl:
                                try:
                                    parsed = json.loads(line)
                                except Exception:
                                    parsed = None
                                if parsed and isinstance(parsed, dict):
                                    local_lines.append(parsed)
                                    continue
                                # If JSON parsing fails for a JSONL file, fall through and treat as legacy line below

                            # For non-JSONL or fallback, try JSON first as well
                            try:
                                parsed = json.loads(line)
                            except Exception:
                                parsed = None

                            if parsed and isinstance(parsed, dict):
                                local_lines.append(parsed)
                                continue

                            # Fallback: try to parse 'TIMESTAMP - message' plain lines
                            try:
                                if ' - ' in line:
                                    ts_part, msg_part = line.split(' - ', 1)
                                    ts = ts_part.strip()
                                    msg = msg_part.strip()
                                    # Detect bulk fetch pattern and convert to structured entry
                                    import re
                                    m = re.search(r'Bulk fetch performed:\s*(?P<d1>\d{2}/\d{2}/\d{4})\s*to\s*(?P<d2>\d{2}/\d{2}/\d{4}),\s*VAT\s*(?P<vat>\d+),\s*(?P<docs>\d+)\s*docs\s*\+\s*(?P<summaries>\d+)\s*summaries\s*by\s*(?P<by>.+)$', msg)
                                    if m:
                                        try:
                                            d1 = m.group('d1')
                                            d2 = m.group('d2')
                                            vat = m.group('vat')
                                            docs = int(m.group('docs') or 0)
                                            summaries = int(m.group('summaries') or 0)
                                            by = m.group('by').strip()
                                        except Exception:
                                            d1 = d2 = vat = by = ''
                                            docs = summaries = 0

                                        entry = {
                                            'timestamp': ts,
                                            'group': str(group_name),
                                            'action': 'ληψη παραστατικων',
                                            'details': {
                                                'date_from': d1,
                                                'date_to': d2,
                                                'client_vat': vat,
                                                'client_label': vat,
                                                'added_docs': docs,
                                                'added_summaries': summaries,
                                                'by': by
                                            }
                                        }
                                        local_lines.append(entry)
                                        continue

                                    # If not bulk fetch, store as generic log_message
                                    entry = {
                                        'timestamp': ts,
                                        'group': str(group_name),
                                        'action': 'log_message',
                                        'details': {
                                            'message': msg
                                        }
                                    }
                                    local_lines.append(entry)
                                    continue
                            except Exception:
                                pass

                            # Last resort: store raw line as a message with no timestamp
                            try:
                                entry = {
                                    'timestamp': '',
                                    'group': str(group_name),
                                    'action': 'log_message',
                                    'details': {
                                        'message': line
                                    }
                                }
                                local_lines.append(entry)
                            except Exception:
                                continue

                # Prepend local logs (most recent at file bottom) - convert to newest-first
                if local_lines:
                    # Keep only up to 'limit' entries and avoid duplicates by timestamp+user+action
                    existing_keys = set()
                    for e in logs:
                        k = (str(e.get('timestamp','')) + '|' + str(e.get('user_id','')) + '|' + str(e.get('action','')))
                        existing_keys.add(k)
                    # Add local entries reversed (newest first) and only if not present
                    for entry in reversed(local_lines):
                        k = (str(entry.get('timestamp','')) + '|' + str(entry.get('user_id','')) + '|' + str(entry.get('action','')))
                        if k in existing_keys:
                            continue
                        logs.append(entry)
                        existing_keys.add(k)
                        if len(logs) >= limit:
                            break
            except Exception:
                # Non-fatal fallback; continue with whatever logs we have
                pass
        else:
            # Get all activity logs from all groups
            all_logs = []
            scanned_local_folders = set()

            def _append_local_folder_logs(folder_name: str):
                try:
                    import os, json
                    if not folder_name:
                        return
                    folder_name = str(folder_name).strip()
                    if not folder_name or folder_name in scanned_local_folders:
                        return
                    scanned_local_folders.add(folder_name)

                    group_dir = os.path.join(os.getcwd(), 'data', folder_name)
                    activity_jsonl = os.path.join(group_dir, 'activity.log.jsonl')
                    activity_path = os.path.join(group_dir, 'activity.log')
                    if os.path.exists(activity_jsonl):
                        with open(activity_jsonl, 'r', encoding='utf-8') as fh:
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    parsed = json.loads(line)
                                except Exception:
                                    parsed = None
                                if parsed and isinstance(parsed, dict):
                                    if not parsed.get('group'):
                                        parsed['group'] = folder_name
                                    all_logs.append(parsed)
                                    continue
                    elif os.path.exists(activity_path):
                        with open(activity_path, 'r', encoding='utf-8') as fh:
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                parsed = None
                                try:
                                    parsed = json.loads(line)
                                except Exception:
                                    parsed = None

                                if parsed and isinstance(parsed, dict):
                                    if not parsed.get('group'):
                                        parsed['group'] = folder_name
                                    all_logs.append(parsed)
                                    continue

                                try:
                                    if ' - ' in line:
                                        ts_part, msg_part = line.split(' - ', 1)
                                        entry = {
                                            'timestamp': ts_part.strip(),
                                            'group': folder_name,
                                            'action': 'log_message',
                                            'details': {'message': msg_part.strip()}
                                        }
                                        all_logs.append(entry)
                                        continue
                                except Exception:
                                    pass

                                try:
                                    entry = {
                                        'timestamp': '',
                                        'group': folder_name,
                                        'action': 'log_message',
                                        'details': {'message': line}
                                    }
                                    all_logs.append(entry)
                                except Exception:
                                    continue
                except Exception:
                    pass

            try:
                # First try to get the old format (flat structure)
                old_logs = firebase_config.firebase_read_data('/activity_logs') or {}
                if isinstance(old_logs, dict):
                    for group, entries in old_logs.items():
                        if isinstance(entries, dict):
                            for _, entry in entries.items():
                                if isinstance(entry, dict):
                                    all_logs.append(entry)
            except Exception as e:
                logger.debug(f"Could not read old format logs: {e}")

            # Now get logs from new format (nested by group)
            try:
                # Get all groups to know which folders to check
                from models import Group
                groups = Group.query.all()
                for group in groups:
                    folder_name = getattr(group, 'data_folder', None) or group.name
                    if folder_name:
                        # First try remote logs
                        try:
                            group_logs = firebase_config.firebase_get_group_activity_logs(folder_name, limit) or []
                            all_logs.extend(group_logs)
                        except Exception:
                            group_logs = []

                        # Also include local `data/<folder>/activity.log.jsonl` (preferred) or legacy `activity.log` as a fallback
                        try:
                            _append_local_folder_logs(folder_name)
                        except Exception:
                            # non-fatal: continue
                            pass

                # Also include local folders that are not registered in Group DB
                # (e.g. __admin__, system, legacy/adhoc group folders).
                try:
                    import os
                    data_root = os.path.join(os.getcwd(), 'data')
                    if os.path.isdir(data_root):
                        for entry_name in os.listdir(data_root):
                            folder_path = os.path.join(data_root, entry_name)
                            if not os.path.isdir(folder_path):
                                continue
                            if entry_name.startswith('.'):
                                continue
                            _append_local_folder_logs(entry_name)
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"Could not read new format logs: {e}")

            # Do not pre-limit using string sorting; we'll sort robustly after parsing timestamps
            logs = all_logs

        # Format timestamps for all logs and compute Athens-based sort key
        def _athens_tz():
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo("Europe/Athens")
            except Exception:
                from datetime import timezone, timedelta
                return timezone(timedelta(hours=2))

        athens = _athens_tz()

        for entry in logs:
            ts = entry.get('timestamp', '')
            dt = None
            entry['timestamp_fmt'] = ''
            if ts:
                # Try ISO8601
                try:
                    dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
                except Exception:
                    pass
                # Try unix timestamp (int/float)
                if not dt:
                    try:
                        dt = datetime.fromtimestamp(float(ts))
                    except Exception:
                        pass
                # Try common string formats (including timezone offset like +0200)
                if not dt:
                    for fmt in (
                        "%Y-%m-%d %H:%M:%S%z",         # 2025-11-20 13:00:58+0200
                        "%Y-%m-%d %H:%M:%S",           # 2025-11-20 13:00:58
                        "%d/%m/%Y, %H:%M:%S",          # 01/12/2025, 14:05:33 (24-hour, Greek UI)
                        "%d/%m/%Y, %I:%M:%S %p",       # 20/11/2025, 01:00:58 PM
                        "%Y-%m-%d"                     # 2025-11-20
                    ):
                        try:
                            dt = datetime.strptime(ts, fmt)
                            break
                        except Exception:
                            continue
                if dt:
                    # Attach Athens tz if naive, then convert to Athens
                    if not getattr(dt, 'tzinfo', None):
                        dt = dt.replace(tzinfo=athens)
                    dt = dt.astimezone(athens)
                    # Format in 24-hour format for proper sorting
                    time_str = dt.strftime('%d/%m/%Y, %H:%M:%S')
                    entry['timestamp_fmt'] = time_str
                    try:
                        entry['__ts_sort'] = dt.timestamp()
                    except Exception:
                        entry['__ts_sort'] = 0
                elif ts and 'Invalid' not in ts:
                    entry['timestamp_fmt'] = ts
                    entry['__ts_sort'] = 0
                else:
                    entry['timestamp_fmt'] = '-'
                    entry['__ts_sort'] = 0

        # Robust numeric sort (Europe/Athens), newest first, then deduplicate and limit
        try:
            logs.sort(key=lambda e: e.get('__ts_sort', 0), reverse=True)
        except Exception:
            pass

        # Deduplicate based on (timestamp prefix + user + action + group), keeping newest first
        seen_keys = set()
        deduped_logs = []
        for log in logs:
            user_id = log.get('user_id') or log.get('details', {}).get('user_id', '')
            action = log.get('action') or log.get('event_type', '')
            group = log.get('group') or log.get('details', {}).get('group', '')
            timestamp = log.get('timestamp', '')
            key = f"{str(timestamp)[:19]}_{user_id}_{action}_{group}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped_logs.append(log)
            if len(deduped_logs) >= limit:
                break
        logs = deduped_logs

        # Create formatted logs with Greek details
        formatted = []

        def _resolve_actor_email(entry_obj: Dict[str, Any], details_obj: Dict[str, Any]) -> str:
            """Try to return a sensible actor email or identifier for display.
            If only a username or id is present, attempt DB lookup for email.
            """
            # Candidate fields in order (include legacy keys like 'by' and admin fields)
            candidates = []
            if isinstance(details_obj, dict):
                candidates.extend([
                    details_obj.get('user_email'),
                    details_obj.get('actor_email'),
                    details_obj.get('performed_by_email'),
                    details_obj.get('actor'),
                    details_obj.get('by'),
                    details_obj.get('performed_by'),
                    details_obj.get('admin_username'),
                    details_obj.get('admin_user_id'),
                    details_obj.get('user'),
                    details_obj.get('username'),
                    details_obj.get('user_username')
                ])
            candidates.extend([
                entry_obj.get('user_email'),
                entry_obj.get('user_id'),
                entry_obj.get('username'),
                entry_obj.get('admin_username'),
                entry_obj.get('admin_user_id')
            ])

            # Normalize and pick first useful, non-placeholder value
            placeholders = {'', None, '-', 'Άγνωστος', 'Unknown'}
            for c in candidates:
                if not c:
                    continue
                c_str = str(c).strip()
                if not c_str or c_str in placeholders:
                    continue
                # If looks like email, return directly
                if '@' in c_str:
                    return c_str
                # If numeric, try lookup by id
                try:
                    uid = int(c_str)
                    try:
                        user = User.query.get(uid)
                        if user and getattr(user, 'email', None):
                            return user.email
                        if user and getattr(user, 'username', None):
                            return user.username
                    except Exception:
                        pass
                except Exception:
                    # Not an int; try username lookup
                    try:
                        user = User.query.filter_by(username=c_str).first()
                        if user and getattr(user, 'email', None):
                            return user.email
                        if user and getattr(user, 'username', None):
                            return user.username
                    except Exception:
                        pass
                # Return this candidate as last resort
                return c_str

            return '-'

        for entry in logs:
            try:
                # Extract user and group info
                details = entry.get('details') if isinstance(entry.get('details'), dict) else {}
                # Resolve a displayable user_email (may be username if email missing)
                user_email = _resolve_actor_email(entry, details)
                group = details.get('group') or entry.get('group') or entry.get('group_name') or '-'
                action = details.get('action') or entry.get('action') or entry.get('event') or '-'
                # Create detailed description in Greek (include actor/target/group where possible)
                details_text = _create_detailed_description(action, details, entry)
            except Exception:
                # Fallback safe handling
                try:
                    user_email = entry.get('user_email') or entry.get('user_id') or entry.get('username') or '-'
                except Exception:
                    user_email = '-'
                action = entry.get('action') or entry.get('message') or entry.get('event') or '-'
                group = entry.get('group') or entry.get('group_name') or '-'
                details_text = str(entry.get('details') or '')

            # Translate actions to Greek (keep simple for display)
            action_descriptions = {
                'user_login_attempt': 'Προσπάθεια σύνδεσης',
                'user_logged_in': 'Σύνδεση χρήστη',
                'user_signup_complete': 'Εγγραφή χρήστη',
                'password_changed': 'Αλλαγή κωδικού',
                'password_reset_completed': 'Επαναφορά κωδικού',
                'user_joined_group': 'Συμμετοχή σε ομάδα',
                'user_left_group': 'Αποχώρηση από ομάδα',
                'login': 'Σύνδεση',
                'logout': 'Αποσύνδεση',
                'user_registered': 'Εγγραφή χρήστη',
                'verification_email_sent': 'Αποστολή email επαλήθευσης',
                'password_reset_requested': 'Αίτηση επαναφοράς κωδικού',
                'password_reset_email_sent': 'Αποστολή email επαναφοράς',
                'forgot_password_request': 'Αίτηση επαναφοράς κωδικού',
                'forgot_password_request_error': 'Σφάλμα επαναφοράς κωδικού',
                'password_reset_error': 'Σφάλμα επαναφοράς κωδικού',
                'delete_user': 'Διαγραφή χρήστη',
                'delete_backup': 'Διαγραφή backup',
                'admin_delete_user': 'Διαγραφή χρήστη (admin)',
                'admin_delete_backup': 'Διαγραφή backup (admin)',
                'send_email': 'Αποστολή email',
                'admin_send_email': 'Αποστολή email (admin)',
                'group_deleted': 'Διαγραφή ομάδας',
                'backup_deleted': 'Διαγραφή backup',
                'group_restored': 'Επαναφορά ομάδας',
                'export_bridge': 'Λήψη γέφυρας',
                'fetch_data': 'Λήψη Παραστατικών',
                'export_expenses': 'Λήψη εξοδολογίου',
                'ληψη παραστατικων': 'Λήψη Παραστατικών',
                'user_deleted': 'Διαγραφή χρήστη',
                'delete_rows': 'Διαγραφή γραμμών',
                'remove_member': 'Αφαίρεση χρήστη',
                'add_user': 'Προσθήκη χρήστη ομάδας',
                'assign_user': 'Ανάθεση χρήστη',
                'remove_user': 'Αφαίρεση χρήστη',
                'create_group': 'Δημιουργία ομάδας',
                'group_delete': 'Διαγραφή ομάδας',
                'leave_group': 'Αποχώρηση από ομάδα',
            }
            action_display = action_descriptions.get(action, action)
            
            formatted.append({
                'timestamp': entry.get('timestamp_fmt', entry.get('timestamp','')),
                'user_email': user_email,
                'group': group,
                'action': action_display,  # This is the translated Greek action
                'summary': action_display,  # Add summary field for template compatibility
                'details': details_text,  # This is the detailed Greek description
                '_ts_sort': entry.get('__ts_sort', 0),
                'ts_sort': entry.get('__ts_sort', 0)  # expose for frontend sorting
            })
        # Sort newest first based on Athens time
        formatted.sort(key=lambda e: e.get('_ts_sort', 0), reverse=True)
        # Keep ts_sort for frontend; drop only the private key
        for e in formatted:
            e.pop('_ts_sort', None)
        return formatted

    except Exception as e:
        logger.error(f"Failed to get activity logs: {e}")
        return []


# ============================================================================
# User Impersonation (for testing/support)
# ============================================================================

def admin_create_impersonation_token(target_user_id: int, current_admin: User, expires_in_minutes: int = 30) -> Optional[str]:
    """
    Create a token that allows admin to impersonate a user
    (useful for debugging issues)
    """
    try:
        target_user = User.query.get(target_user_id)
        if not target_user:
            return None
        
        # Store in cache with expiration
        import secrets
        token = secrets.token_urlsafe(32)
        
        # Store token info (in production, use Redis or similar)
        cache_key = f"impersonate_{token}"
        impersonation_info = {
            'target_user_id': target_user_id,
            'admin_id': current_admin.id,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'expires_at': datetime.now(timezone.utc).isoformat()  # TODO: implement expiration check
        }
        
        firebase_log_activity(current_admin.id, "admin", "impersonation_created", {
            'target_user_id': target_user_id,
            'target_username': target_user.username
        })
        
        return token
    
    except Exception as e:
        logger.error(f"Failed to create impersonation token: {e}")
        return None


# ============================================================================
# Helper Functions
# ============================================================================

def _get_folder_size(folder_path: str) -> int:
    """Get total size of folder in bytes"""
    total = 0
    try:
        for entry in os.scandir(folder_path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _get_folder_size(entry.path)
    except Exception as e:
        logger.error(f"Error calculating folder size: {e}")
    
    return total


def admin_compare_backup_with_current(backup_path: str, backup_type: str = 'remote') -> Dict[str, Any]:
    """Compare backup with current state to show differences before restore"""
    try:
        comparison = {
            'backup_info': {
                'path': backup_path,
                'type': backup_type
            },
            'differences': [],
            'summary': {
                'groups_in_backup': 0,
                'groups_in_current': 0,
                'groups_to_add': [],
                'groups_to_update': [],
                'groups_to_remove': []
            }
        }
        
        # Get backup data
        backup_data = None
        if backup_type == 'remote':
            backup_data = firebase_config.firebase_read_data(backup_path)
        elif backup_type == 'local':
            # For local backups, we need to read the backup folder structure
            backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
            backup_full_path = os.path.join(backups_dir, backup_path)
            if os.path.exists(backup_full_path):
                backup_data = {'groups': {}}
                # Read backup folder as a single group.  we used to strip the
                # "_backup_" prefix by simple replace, which would leave the
                # timestamp attached (e.g. "tony20260225"), causing the
                # comparison logic to think a new group should be added.  instead
                # split at the first "_backup_" occurrence.
                bn = os.path.basename(backup_path)
                if '_backup_' in bn:
                    group_name = bn.split('_backup_')[0]
                else:
                    group_name = bn
                backup_data['groups'][group_name] = {'folder_exists': True}
        
        if not backup_data:
            return {'error': 'Could not read backup data'}
        
        # Get current groups
        current_groups = admin_list_all_groups()
        current_group_names = {g['name'] for g in current_groups}
        
        # Analyze backup structure
        backup_groups = set()
        if 'groups' in backup_data and isinstance(backup_data['groups'], dict):
            backup_groups = set(backup_data['groups'].keys())
        elif backup_type == 'remote':
            # Single group backup - extract group name from path
            parts = backup_path.strip('/').split('/')
            if len(parts) >= 2:
                backup_groups.add(parts[1])
        
        comparison['summary']['groups_in_backup'] = len(backup_groups)
        comparison['summary']['groups_in_current'] = len(current_group_names)
        
        # Find differences
        comparison['summary']['groups_to_add'] = list(backup_groups - current_group_names)
        comparison['summary']['groups_to_update'] = list(backup_groups & current_group_names)
        comparison['summary']['groups_to_remove'] = list(current_group_names - backup_groups)
        
        # Create detailed differences
        for group_name in backup_groups:
            diff = {
                'group_name': group_name,
                'action': 'add' if group_name not in current_group_names else 'update',
                'current_exists': group_name in current_group_names
            }
            
            if group_name in current_group_names:
                # Find current group info
                current_group = next((g for g in current_groups if g['name'] == group_name), None)
                if current_group:
                    diff['current_size_mb'] = current_group.get('folder_size_mb', 0)
                    diff['current_members'] = current_group.get('members_count', 0)
            
            comparison['differences'].append(diff)
        
        return comparison
        
    except Exception as e:
        logger.error(f"Failed to compare backup: {e}")
        return {'error': str(e)}


def admin_get_system_stats() -> Dict[str, Any]:
    """Get overall system statistics"""
    try:
        users = User.query.all()
        groups = Group.query.all()
        
        # Calculate total data size
        data_dir = os.path.join(os.getcwd(), 'data')
        total_size = _get_folder_size(data_dir) if os.path.exists(data_dir) else 0
        
        # Calculate activity in last 24h
        now = datetime.now(timezone.utc)
        activity_24h = 0
        try:
            logs = admin_get_activity_logs(limit=1000)
            for entry in logs:
                ts = entry.get('timestamp') or entry.get('timestamp_fmt')
                dt = None
                if ts and ts != '-':
                    # Try ISO format
                    try:
                        dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
                    except Exception:
                        pass
                    # Try formatted datetime with timezone
                    if not dt:
                        try:
                            # Handle Greek locale AM/PM (legacy support)
                            ts_normalized = ts.replace('π.μ.', 'AM').replace('μ.μ.', 'PM')
                            dt = datetime.strptime(ts_normalized, '%d/%m/%Y, %I:%M:%S %p')
                        except Exception:
                            pass
                    # Try simple datetime
                    if not dt:
                        try:
                            dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                        except Exception:
                            pass
                    # Try 24-hour Greek formatted timestamp (current display format)
                    if not dt:
                        try:
                            dt = datetime.strptime(ts, '%d/%m/%Y, %H:%M:%S')
                            # This string is in local Greek time (EET/EEST). Assume EET (UTC+2) for parsing, then convert to UTC.
                            from datetime import timedelta
                            eet = timezone(timedelta(hours=2))
                            dt = dt.replace(tzinfo=eet).astimezone(timezone.utc)
                        except Exception:
                            pass
                if dt:
                    # Make timezone-aware if naive
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (now - dt).total_seconds() <= 86400:
                        activity_24h += 1
        except Exception as e:
            logger.error(f"Failed to calculate 24h activity: {e}")
            pass
        return {
            'total_users': len(users),
            'total_groups': len(groups),
            'total_data_size_mb': round(total_size / (1024 * 1024), 2),
            'activity_24h': activity_24h,
            'timestamp': now.isoformat()
        }
    
    except Exception as e:
        logger.error(f"Failed to get system stats: {e}")
        return {}
