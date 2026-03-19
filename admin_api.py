"""
Admin API Endpoints for Dashboard
Provides JSON API for admin panel operations
"""

import logging
import os
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from datetime import datetime, timezone
import firebase_config
from firebase_auth_handlers import FirebaseAuthHandler
from models import db, User, Group
import admin_panel
from admin_panel import admin_list_all_users, admin_list_all_groups, admin_get_activity_logs, is_admin

logger = logging.getLogger(__name__)

admin_api_bp = Blueprint('admin_api', __name__, url_prefix='/admin/api')


def _require_admin(f):
    """Decorator to require admin access"""
    def decorated_function(*args, **kwargs):
        if not current_user or not current_user.is_authenticated:
            return jsonify({'error': 'Not authenticated'}), 401
        # Use centralized admin check (supports is_admin flag or ADMIN_USER_ID fallback)
        try:
            if not is_admin(current_user):
                return jsonify({'error': 'Admin access required'}), 403
        except Exception:
            return jsonify({'error': 'Admin access required'}), 403
        
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function


@admin_api_bp.route('/users', methods=['GET'])
@login_required
@_require_admin
def api_list_users():
    """Get all users"""
    try:
        users = admin_list_all_users()
        return jsonify({
            'success': True,
            'users': users,
            'count': len(users)
        })
    except Exception as e:
        logger.error(f"Error listing users: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/users/<uid>', methods=['GET'])
@login_required
@_require_admin
def api_get_user(uid):
    """Get user details"""
    try:
        user = FirebaseAuthHandler.get_user_by_uid(uid)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        return jsonify({
            'success': True,
            'user': user
        })
    except Exception as e:
        logger.error(f"Error getting user {uid}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/users/<uid>', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_user(uid):
    """Delete a user"""
    try:
        success, error = FirebaseAuthHandler.delete_user(uid)
        if not success:
            return jsonify({'success': False, 'error': error}), 400
        
        return jsonify({
            'success': True,
            'message': f'User {uid} deleted'
        })
    except Exception as e:
        logger.error(f"Error deleting user {uid}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups', methods=['GET'])
@login_required
@_require_admin
def api_list_groups():
    """Get all groups"""
    try:
        groups = admin_list_all_groups()
        return jsonify({
            'success': True,
            'groups': groups,
            'count': len(groups)
        })
    except Exception as e:
        logger.error(f"Error listing groups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<group_name>', methods=['GET'])
@login_required
@_require_admin
def api_get_group(group_name):
    """Get group details"""
    try:
        group_data = firebase_config.firebase_read_data(f'/groups/{group_name}')
        if not group_data:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        return jsonify({
            'success': True,
            'group': group_data
        })
    except Exception as e:
        logger.error(f"Error getting group {group_name}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<group_name>', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_group(group_name):
    """Delete a group"""
    try:
        # Log activity
        firebase_config.firebase_log_activity(
            current_user.pw_hash or 'admin',
            group_name,
            'admin_group_deleted',
            {'admin_user_id': current_user.id}
        )
        
        # Delete group data
        firebase_config.firebase_delete_data(f'/groups/{group_name}')
        firebase_config.firebase_delete_data(f'/group_encryption_keys/{group_name}')
        firebase_config.firebase_delete_data(f'/activity_logs/{group_name}')
        
        return jsonify({
            'success': True,
            'message': f'Group {group_name} deleted'
        })
    except Exception as e:
        logger.error(f"Error deleting group {group_name}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<int:group_id>', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_group_by_id(group_id):
    """Delete a group by ID using admin_panel function"""
    try:
        result = admin_panel.admin_delete_group(group_id, current_user, backup_first=True)
        if result.get('ok'):
            return jsonify({
                'success': True,
                'message': result.get('message', 'Group deleted successfully'),
                'backup_path': result.get('backup_path')
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            }), 400
    except Exception as e:
        logger.error(f"Error deleting group {group_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/users/<int:user_id>/force_unlock', methods=['POST'])
@login_required
@_require_admin
def api_force_unlock_user(user_id: int):
    """Admin API: force-clear a user's session claim."""
    try:
        res = admin_panel.admin_force_unlock(user_id, current_user)
        if res.get('ok'):
            return jsonify({'success': True, 'cleared_previous_session': res.get('cleared_previous', False)})
        return jsonify({'success': False, 'error': res.get('error', 'unknown')}), 400
    except Exception as e:
        logger.error(f"Error force-unlocking user {user_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/activity', methods=['GET'])
@login_required
@_require_admin
def api_get_activity():
    """Get activity logs"""
    try:
        group_filter = request.args.get('group', '')
        limit = int(request.args.get('limit', 100))
        
        if group_filter:
            logs = firebase_config.firebase_get_group_activity_logs(group_filter, limit=limit)
        else:
            # Get logs from all groups
            logs = []
            groups = admin_list_all_groups()
            for group in groups:
                group_name = group.get('name') or group.get('group_name')
                group_logs = firebase_config.firebase_get_group_activity_logs(group_name, limit=20)
                logs.extend(group_logs)
            
            # Sort by timestamp, most recent first (robust datetime parsing)
            from datetime import datetime
            def _parse_ts(ts: str):
                if not ts or ts == '-':
                    return datetime.min
                # Try ISO8601 variants
                for variant in (
                    lambda s: s.replace('Z','+00:00'),  # handle trailing Z
                    lambda s: s,
                ):
                    try:
                        return datetime.fromisoformat(variant(ts))
                    except Exception:
                        pass
                # Try numeric epoch
                try:
                    return datetime.fromtimestamp(float(ts))
                except Exception:
                    pass
                # Common fallback formats (including 24-hour and AM/PM)
                for fmt in (
                    "%Y-%m-%d %H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S",
                    "%d/%m/%Y, %H:%M:%S",  # 24-hour format
                    "%d/%m/%Y, %I:%M:%S %p",  # 12-hour with AM/PM
                    "%Y-%m-%d",
                ):
                    try:
                        return datetime.strptime(ts, fmt)
                    except Exception:
                        continue
                return datetime.min
            # Convert parsed times to Europe/Athens for consistent ordering when naive
            try:
                from zoneinfo import ZoneInfo
                athens = ZoneInfo("Europe/Athens")
            except Exception:
                from datetime import timezone, timedelta
                athens = timezone(timedelta(hours=2))

            def _sort_key(x):
                dt = _parse_ts(x.get('timestamp'))
                try:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=athens)
                    else:
                        dt = dt.astimezone(athens)
                    return dt.timestamp()
                except Exception:
                    return 0.0

            logs.sort(key=_sort_key, reverse=True)
            logs = logs[:limit]
        
        return jsonify({
            'success': True,
            'logs': logs,
            'count': len(logs)
        })
    except Exception as e:
        logger.error(f"Error getting activity: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/stats', methods=['GET'])
@login_required
@_require_admin
def api_get_stats():
    """Get system statistics"""
    try:
        users = admin_list_all_users()
        groups = admin_list_all_groups()
        
        # Count recent activity (last 24 hours)
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        
        recent_count = 0
        for group in groups:
            group_name = group.get('name') or group.get('group_name')
            logs = firebase_config.firebase_get_group_activity_logs(group_name, limit=100)
            for log in logs:
                try:
                    log_time = datetime.fromisoformat(log['timestamp'].replace('Z', '+00:00'))
                    if log_time > yesterday:
                        recent_count += 1
                except:
                    pass
        
        return jsonify({
            'success': True,
            'stats': {
                'total_users': len(users),
                'total_groups': len(groups),
                'recent_activity_24h': recent_count,
                'firebase_enabled': firebase_config.is_firebase_enabled(),
                'timestamp': now.isoformat()
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/activity/version', methods=['GET'])
@login_required
@_require_admin
def api_get_activity_version():
    """Return the global activity version for frontend polling (auto-reload)."""
    try:
        from datetime import datetime, timezone
        version = firebase_config.get_activity_version()
        return jsonify({'success': True, 'version': int(version), 'timestamp': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.error(f"Error getting activity version: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backup/group/<group_name>', methods=['POST'])
@login_required
@_require_admin
def api_backup_group(group_name):
    """Backup a specific group to local, remote, or both"""
    try:
        from models import Group
        
        # Find the group by name
        group = Group.query.filter_by(name=group_name).first()
        if not group:
            return jsonify({'success': False, 'error': f'Group "{group_name}" not found'}), 404
        
        data = request.get_json() or {}
        target = data.get('target', 'remote')  # Default to remote (Firebase)
        
        results = {}
        
        # Remote backup (Firebase)
        if target in ['remote', 'both']:
            backup_path = f'/backups/{group_name}/{datetime.now(timezone.utc).isoformat()}'
            group_data = firebase_config.firebase_read_data(f'/groups/{group_name}')
            if group_data:
                firebase_config.firebase_write_data(backup_path, group_data)
                results['remote'] = backup_path
                logger.info(f"Remote backup created for group {group_name} at {backup_path}")
                try:
                    # Log backup creation to activity logs so admin panel shows it
                    firebase_config.firebase_log_activity(
                        current_user.pw_hash or current_user.id,
                        group_name,
                        'backup_created',
                        {'backup_path': backup_path, 'groups_count': 1}
                    )
                except Exception:
                    logger.debug('Failed to write firebase_log_activity for backup_created')
        
        # Local backup (Server filesystem)
        if target in ['local', 'both']:
            from admin_panel import admin_backup_group
            local_path = admin_backup_group(group.id)
            if local_path:
                results['local'] = local_path
                logger.info(f"Local backup created for group {group_name} at {local_path}")
                try:
                    # log local backup creation (store friendly backup_name)
                    import os
                    backup_name = os.path.basename(local_path)
                    firebase_config.firebase_log_activity(
                        current_user.pw_hash or current_user.id,
                        group_name,
                        'backup_created',
                        {'backup_name': backup_name, 'local_path': local_path}
                    )
                except Exception:
                    logger.debug('Failed to write firebase_log_activity for local backup_created')
        
        return jsonify({
            'success': True,
            'ok': True,
            'results': results,
            'message': f'Backup created for {group_name}'
        })
    except Exception as e:
        logger.error(f"Error backing up group {group_name}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500



@admin_api_bp.route('/backup/list', methods=['GET'])
@login_required
@_require_admin
def api_list_remote_backups():
    """List backups stored in Firebase"""
    try:
        prefix = request.args.get('prefix')
        backups = admin_panel.admin_list_remote_backups(prefix)
        return jsonify({'success': True, 'backups': backups})
    except Exception as e:
        logger.error(f"Error listing remote backups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backup', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_remote_backup():
    """Delete a remote backup entry in Firebase. JSON body: {'backup_path': '/backups/...'}"""
    try:
        data = request.json or {}
        path = data.get('backup_path')
        if not path:
            return jsonify({'success': False, 'error': 'backup_path required'}), 400
        
        # If path doesn't start with /backups/, assume it's just the backup name and construct the full path
        if not path.startswith('/backups/'):
            # Try to construct a proper backup path
            if path.startswith('/backups'):
                full_path = path  # Already has /backups prefix
            else:
                full_path = f'/backups/{path}' if '/' not in path else path
        else:
            full_path = path
        
        logger.info(f"Attempting to delete remote backup: {full_path}")
        
        res = admin_panel.admin_delete_remote_backup(full_path, current_user)
        
        if res.get('ok'):
            return jsonify({'success': True, 'message': res.get('message', 'Remote backup deleted successfully')})
        else:
            logger.warning(f"Failed to delete remote backup: {res.get('error')}")
            return jsonify({'success': False, 'error': res.get('error', 'Unknown error')}), 400
    except Exception as e:
        logger.error(f"Error deleting remote backup: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backup/restore', methods=['POST'])
@login_required
@_require_admin
def api_restore_remote_backup():
    """Restore groups from a remote backup. JSON body: {'backup_path': '/backups/..', 'target_group_id': int, 'groups': [names]}"""
    try:
        # parse JSON safely; `request.json` may raise if malformed
        data = request.get_json(force=True, silent=True) or {}
        logger.debug("api_restore_remote_backup payload: %r", data)
        path = data.get('backup_path')
        # allow target_group_id to be omitted; treat empty/zero as None
        target_group_id = None
        if data.get('target_group_id') not in (None, '', 0):
            try:
                target_group_id = int(data.get('target_group_id'))
            except Exception:
                target_group_id = None
        groups = data.get('groups')
        if not path:
            logger.warning("remote restore called without path")
            return jsonify({'success': False, 'error': 'backup_path required'}), 400
        res = admin_panel.admin_restore_remote_backup(path, target_group_id, groups_to_restore=groups, current_admin=current_user)
        logger.info("remote restore result: %s", res)
        return jsonify({'success': res.get('ok', False), 'restored': res.get('restored', []), 'error': res.get('error')}), (200 if res.get('ok') else 400)
    except Exception as e:
        logger.error(f"Error restoring remote backup: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backup/all', methods=['POST'])
@login_required
@_require_admin
def api_backup_all():
    """Backup all system data"""
    try:
        data = request.json or {}
        target = data.get('target', 'remote')  # 'local' | 'remote' | 'both'

        backup_timestamp = datetime.now(timezone.utc).isoformat()
        backup_path = f'/backups/full_backup/{backup_timestamp}'

        # Backup all groups list
        groups = admin_list_all_groups()

        results = {'groups_processed': 0, 'local_paths': [], 'remote_path': None}

        # If remote requested, build payload and write to firebase (existing behavior)
        if target in ('remote', 'both'):
            backup_data = {
                'timestamp': backup_timestamp,
                'user_id': current_user.id,
                'groups_count': len(groups),
                'groups': {}
            }

            for group in groups:
                group_name = group.get('name') or group.get('group_name')
                group_data = firebase_config.firebase_read_data(f'/groups/{group_name}')
                if group_data:
                    backup_data['groups'][group['group_name']] = group_data

            firebase_config.firebase_write_data(backup_path, backup_data)
            results['remote_path'] = backup_path
            try:
                # Log full backup creation as admin-level event
                firebase_config.firebase_log_activity(
                    current_user.pw_hash or current_user.id,
                    '__admin__',
                    'backup_created',
                    {'backup_path': backup_path, 'groups_count': len(groups)}
                )
            except Exception:
                logger.debug('Failed to write firebase_log_activity for full backup_created')

        # If local requested, create per-group backups into data/_backups
        if target in ('local', 'both'):
            local_paths = []
            for grp in groups:
                gid = grp.get('id')
                try:
                    p = admin_panel.admin_backup_group(gid)
                    if p:
                        local_paths.append(p)
                except Exception as e:
                    logger.warning(f"Failed local backup for group {gid}: {e}")
            results['local_paths'] = local_paths
            try:
                if results.get('local_paths'):
                    # Log each local backup created
                    for lp in results.get('local_paths'):
                        import os
                        bn = os.path.basename(lp)
                        try:
                            firebase_config.firebase_log_activity(
                                current_user.pw_hash or current_user.id,
                                '__admin__',
                                'backup_created',
                                {'backup_name': bn, 'local_path': lp}
                            )
                        except Exception:
                            pass
            except Exception:
                logger.debug('Failed to log local backup entries')

        results['groups_processed'] = len(groups)
        logger.info(f"Full system backup completed: target={target} groups={len(groups)}")

        return jsonify({
            'success': True,
            'backup_target': target,
            'remote_path': results.get('remote_path'),
            'local_paths': results.get('local_paths'),
            'groups_backed_up': results.get('groups_processed'),
            'message': 'Full system backup created'
        })
    except Exception as e:
        logger.error(f"Error backing up system: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/activity/clear', methods=['POST'])
@login_required
@_require_admin
def api_clear_activity():
    """Clear activity logs (dangerous!)"""
    try:
        # Get confirmation from client
        if not request.json or not request.json.get('confirm'):
            return jsonify({'success': False, 'error': 'Confirmation required'}), 400
        
        groups = admin_list_all_groups()
        for group in groups:
            group_name = group.get('name') or group.get('group_name')
            firebase_config.firebase_delete_data(f'/activity_logs/{group_name}')
        
        logger.warning(f"Activity logs cleared by admin {current_user.id}")
        
        return jsonify({
            'success': True,
            'message': 'Activity logs cleared'
        })
    except Exception as e:
        logger.error(f"Error clearing activity: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/group/<group_id>/files', methods=['GET'])
@login_required
@_require_admin
def api_list_group_files(group_id):
    """Get list of files in a group folder (for frontend file browser)"""
    try:
        import os
        from pathlib import Path
        
        # Get group
        group = Group.query.get(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        group_folder = group.data_folder
        if not group_folder:
            return jsonify({'success': False, 'error': 'Group has no data folder'}), 400
        
        # Ensure data is pulled locally
        try:
            firebase_config.ensure_group_data_local(group_folder)
        except Exception as e:
            logger.warning(f"Could not lazy-pull group data: {e}")
        
        # Build file list
        data_path = os.path.join(current_app.root_path, 'data', group_folder)
        files = []
        
        if os.path.isdir(data_path):
            for root, dirs, filenames in os.walk(data_path):
                rel_root = os.path.relpath(root, data_path)
                if rel_root == '.':
                    rel_root = ''
                
                # Add directories
                for dirname in sorted(dirs):
                    dir_path = os.path.join(root, dirname)
                    rel_path = os.path.join(rel_root, dirname) if rel_root else dirname
                    
                    # Skip hidden and system dirs
                    if dirname.startswith('.') or dirname == '__pycache__':
                        continue
                    
                    files.append({
                        'type': 'folder',
                        'name': dirname,
                        'path': rel_path.replace(os.sep, '/'),
                        'size': None
                    })
                
                # Add files
                for filename in sorted(filenames):
                    if filename.startswith('.'):
                        continue
                    
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.join(rel_root, filename) if rel_root else filename
                    
                    try:
                        size = os.path.getsize(file_path)
                    except:
                        size = 0
                    
                    files.append({
                        'type': 'file',
                        'name': filename,
                        'path': rel_path.replace(os.sep, '/'),
                        'size': size
                    })
        
        return jsonify({
            'success': True,
            'group': {
                'id': group.id,
                'name': group.name,
                'data_folder': group_folder
            },
            'files': files,
            'count': len(files)
        })
    except Exception as e:
        logger.error(f"Error listing group files: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/group/<group_id>/file/<path:file_path>', methods=['GET'])
@login_required
@_require_admin
def api_get_group_file(group_id, file_path):
    """Download or view a file from a group folder"""
    try:
        import os
        from flask import send_file
        
        # Get group
        group = Group.query.get(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        group_folder = group.data_folder
        if not group_folder:
            return jsonify({'success': False, 'error': 'Group has no data folder'}), 400
        
        # Security: prevent path traversal
        if '..' in file_path or file_path.startswith('/'):
            return jsonify({'success': False, 'error': 'Invalid file path'}), 400
        
        full_path = os.path.join(current_app.root_path, 'data', group_folder, file_path)
        
        # Verify path is within group folder
        real_path = os.path.realpath(full_path)
        real_group_path = os.path.realpath(os.path.join(current_app.root_path, 'data', group_folder))
        
        if not real_path.startswith(real_group_path):
            return jsonify({'success': False, 'error': 'Path traversal not allowed'}), 403
        
        if not os.path.isfile(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # For download
        if request.args.get('download'):
            return send_file(full_path, as_attachment=True, download_name=os.path.basename(file_path))
        
        # For preview/read
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return jsonify({
                'success': True,
                'content': content,
                'type': 'text'
            })
        except:
            # If not text, return as binary for download
            return send_file(full_path, as_attachment=True, download_name=os.path.basename(file_path))
    
    except Exception as e:
        logger.error(f"Error getting group file: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Enhanced CRUD and Admin Operations
# ============================================================================

def _log_admin_action(action_type: str, target_type: str, target_id: str, details: dict = None):
    """Log an admin action for audit trail"""
    try:
        log_entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'admin_user_id': current_user.id,
            'admin_username': current_user.username,
            'action': action_type,
            'target_type': target_type,
            'target_id': target_id,
            'details': details or {},
            'ip_address': request.remote_addr
        }
        firebase_config.firebase_log_activity(
            current_user.pw_hash or 'admin',
            '__admin__',
            f'admin_{action_type}',
            log_entry
        )
        logger.info(f'[ADMIN ACTION] {action_type} on {target_type} {target_id} by {current_user.username}')
    except Exception as e:
        logger.error(f'Failed to log admin action: {e}')


@admin_api_bp.route('/users', methods=['POST'])
@login_required
@_require_admin
def api_create_user():
    """Create a new user"""
    try:
        data = request.get_json()
        email = data.get('email', '').strip()
        password = data.get('password', '').strip()
        username = data.get('username', '').strip()
        
        if not email or not password or not username:
            return jsonify({'success': False, 'error': 'Email, password, and username required'}), 400
        
        # Check if user already exists
        existing = User.query.filter_by(email=email).first()
        if existing:
            return jsonify({'success': False, 'error': 'User already exists'}), 400
        
        # Create user in database
        new_user = User(email=email, username=username)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        
        # Log action
        _log_admin_action('create_user', 'user', str(new_user.id), {
            'email': email,
            'username': username
        })
        
        return jsonify({
            'success': True,
            'message': f'User {username} created',
            'user': {
                'id': new_user.id,
                'email': new_user.email,
                'username': new_user.username
            }
        })
    except Exception as e:
        logger.error(f'Error creating user: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/users/<int:user_id>', methods=['PUT'])
@login_required
@_require_admin
def api_update_user(user_id):
    """Update user details"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        data = request.get_json()
        changes = {}
        
        # Update email
        if 'email' in data:
            new_email = data['email'].strip()
            if new_email and new_email != user.email:
                existing = User.query.filter_by(email=new_email).first()
                if existing:
                    return jsonify({'success': False, 'error': 'Email already in use'}), 400
                changes['email'] = {'from': user.email, 'to': new_email}
                user.email = new_email
        
        # Update username
        if 'username' in data:
            new_username = data['username'].strip()
            if new_username and new_username != user.username:
                changes['username'] = {'from': user.username, 'to': new_username}
                user.username = new_username
        
        # Update password
        if 'password' in data:
            new_password = data['password'].strip()
            if new_password:
                user.set_password(new_password)
                changes['password'] = 'updated'
        
        # Update is_admin flag
        if 'is_admin' in data:
            old_is_admin = user.is_admin
            user.is_admin = bool(data['is_admin'])
            if old_is_admin != user.is_admin:
                changes['is_admin'] = {'from': old_is_admin, 'to': user.is_admin}
        
        db.session.commit()
        
        # Log action
        _log_admin_action('update_user', 'user', str(user_id), changes)
        
        return jsonify({
            'success': True,
            'message': f'User {user.username} updated',
            'changes': changes,
            'user': {
                'id': user.id,
                'email': user.email,
                'username': user.username,
                'is_admin': user.is_admin
            }
        })
    except Exception as e:
        logger.error(f'Error updating user {user_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/users/<int:user_id>', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_user_by_id(user_id):
    """Delete a user by ID"""
    try:
        if user_id == current_user.id:
            return jsonify({'success': False, 'error': 'Cannot delete yourself'}), 400
        
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Protect main admin from deletion
        user_email = getattr(user, 'email', '')
        if user_email == 'adonis.douramanis@gmail.com':
            return jsonify({'success': False, 'error': 'Cannot delete the main admin user'}), 400
        
        username = user.username
        email = user.email
        firebase_uid = getattr(user, 'pw_hash', None)  # Firebase UID is stored in pw_hash
        
        # Delete from Firebase Authentication if UID exists
        if firebase_uid:
            try:
                import firebase_config
                firebase_config.firebase_delete_user(firebase_uid)
                logger.info(f"Deleted Firebase user: {firebase_uid}")
            except Exception as e:
                logger.warning(f"Failed to delete Firebase user {firebase_uid}: {e}")
        
        # Delete from Firebase Database (user data)
        try:
            import firebase_config
            firebase_config.firebase_delete_data(f'/users/{firebase_uid}')
            firebase_config.firebase_delete_data(f'/user_profiles/{firebase_uid}')
            logger.info(f"Deleted Firebase user data for: {firebase_uid}")
        except Exception as e:
            logger.warning(f"Failed to delete Firebase user data: {e}")
        
        # Remove from all groups in the database
        for group in user.groups:
            group.users.remove(user)
        
        # Delete from database
        db.session.delete(user)
        db.session.commit()
        
        # Log action
        _log_admin_action('delete_user', 'user', str(user_id), {
            'username': username,
            'email': email
        })
        
        return jsonify({
            'success': True,
            'message': f'User {username} deleted'
        })
    except Exception as e:
        logger.error(f'Error deleting user {user_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<int:group_id>/members', methods=['GET'])
@login_required
@_require_admin
def api_get_group_members(group_id):
    """Get members of a group"""
    try:
        group = Group.query.get(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        members = []
        for user in group.users:
            members.append({
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'is_admin': user.is_admin
            })
        
        return jsonify({
            'success': True,
            'group': {
                'id': group.id,
                'name': group.name,
                'data_folder': group.data_folder
            },
            'members': members,
            'count': len(members)
        })
    except Exception as e:
        logger.error(f'Error getting group members: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<int:group_id>/members', methods=['POST'])
@login_required
@_require_admin
def api_add_group_member(group_id):
    """Add a member to a group"""
    try:
        group = Group.query.get(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        data = request.get_json()
        user_id = data.get('user_id')
        
        if not user_id:
            return jsonify({'success': False, 'error': 'user_id required'}), 400
        
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        if user in group.users:
            return jsonify({'success': False, 'error': 'User already in group'}), 400
        
        group.users.append(user)
        db.session.commit()
        
        # Log action
        _log_admin_action('add_group_member', 'group', str(group_id), {
            'user_id': user_id,
            'username': user.username,
            'group_name': group.name
        })
        
        return jsonify({
            'success': True,
            'message': f'User {user.username} added to group {group.name}'
        })
    except Exception as e:
        logger.error(f'Error adding group member: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<int:group_id>/members/<int:user_id>', methods=['DELETE'])
@login_required
@_require_admin
def api_remove_group_member(group_id, user_id):
    """Remove a member from a group"""
    try:
        group = Group.query.get(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        if user not in group.users:
            return jsonify({'success': False, 'error': 'User not in group'}), 400
        
        group.users.remove(user)
        db.session.commit()
        
        # Log action
        _log_admin_action('remove_group_member', 'group', str(group_id), {
            'user_id': user_id,
            'username': user.username,
            'group_name': group.name
        })
        
        return jsonify({
            'success': True,
            'message': f'User {user.username} removed from group {group.name}'
        })
    except Exception as e:
        logger.error(f'Error removing group member: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/audit-log', methods=['GET'])
@login_required
@_require_admin
def api_get_audit_log():
    """Get detailed audit log of admin actions"""
    try:
        limit = int(request.args.get('limit', 200))
        admin_user_id = request.args.get('admin_user_id')
        action_type = request.args.get('action')
        
        # Get audit logs from Firebase
        logs = firebase_config.firebase_get_group_activity_logs('__admin__', limit=limit)
        
        # Filter if needed
        if admin_user_id:
            logs = [log for log in logs if log.get('details', {}).get('admin_user_id') == int(admin_user_id)]
        
        if action_type:
            logs = [log for log in logs if action_type in log.get('event_type', '')]
        
        return jsonify({
            'success': True,
            'logs': logs,
            'count': len(logs)
        })
    except Exception as e:
        logger.error(f'Error getting audit log: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/user-actions/<int:user_id>', methods=['GET'])
@login_required
@_require_admin
def api_get_user_actions(user_id):
    """Get all actions performed by a specific user"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        limit = int(request.args.get('limit', 100))
        
        # Get all activity logs for this user across groups
        all_logs = []
        groups = Group.query.filter(Group.users.any(id=user_id)).all()
        
        for group in groups:
            group_logs = firebase_config.firebase_get_group_activity_logs(group.name, limit=limit)
            all_logs.extend(group_logs)
        
        # Sort by timestamp, most recent first (robust datetime parsing)
        from datetime import datetime
        def _parse_ts(ts: str):
            if not ts or ts == '-':
                return datetime.min
            # Try ISO8601 variants
            for variant in (
                lambda s: s.replace('Z','+00:00'),  # handle trailing Z
                lambda s: s,
            ):
                try:
                    return datetime.fromisoformat(variant(ts))
                except Exception:
                    pass
            # Try numeric epoch
            try:
                return datetime.fromtimestamp(float(ts))
            except Exception:
                pass
            # Common fallback formats (including 24-hour and AM/PM)
            for fmt in (
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y, %H:%M:%S",  # 24-hour format
                "%d/%m/%Y, %I:%M:%S %p",  # 12-hour with AM/PM
                "%Y-%m-%d",
            ):
                try:
                    return datetime.strptime(ts, fmt)
                except Exception:
                    continue
            return datetime.min
        
        all_logs.sort(key=lambda x: _parse_ts(x.get('timestamp')), reverse=True)
        all_logs = all_logs[:limit]
        
        return jsonify({
            'success': True,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email
            },
            'actions': all_logs,
            'count': len(all_logs)
        })
    except Exception as e:
        logger.error(f'Error getting user actions: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Additional Endpoints for Unified Dashboard
# ============================================================================

@admin_api_bp.route('/activity-logs', methods=['GET'])
@login_required
@_require_admin
def api_activity_logs():
    """Get activity logs with filtering and enhanced details"""
    try:
        group_filter = request.args.get('group', '')
        action_filter = request.args.get('action', '')
        search_filter = request.args.get('search', '')
        limit = int(request.args.get('limit', 100))
        
        logs = admin_panel.admin_get_activity_logs(group_filter if group_filter else None, limit)
        
        # Enhance logs with human-readable descriptions
        enhanced_logs = []
        for log in logs:
            enhanced_log = log.copy()
            
            # Extract nested details if they exist
            details = log.get('details', {})
            if isinstance(details, dict):
                # Check if details has nested 'details' (from log_user_activity)
                if 'details' in details and isinstance(details['details'], dict):
                    # The actual action details are nested
                    action_details = details['details']
                else:
                    # Old format or direct details
                    action_details = details
                
                # Add user info from top-level details
                if 'user_email' in details and not enhanced_log.get('user_email'):
                    enhanced_log['user_email'] = details.get('user_email')
                if 'user_username' in details and not enhanced_log.get('user_username'):
                    enhanced_log['user_username'] = details.get('user_username')
                
                # Add readable summary based on action
                action = log.get('action', '')
                summary_parts = []
                
                if action == 'login':
                    summary_parts.append(f"Σύνδεση χρήστη {details.get('user_email', 'N/A')}")
                elif action == 'logout':
                    duration = action_details.get('duration_minutes')
                    if duration:
                        summary_parts.append(f"Αποσύνδεση (διάρκεια: {duration} λεπτά)")
                    else:
                        summary_parts.append("Αποσύνδεση")
                elif action == 'backup_download':
                    summary_parts.append(f"Λήψη backup: {action_details.get('file_name', 'N/A')} ({action_details.get('file_size_mb', 0):.2f} MB)")
                elif action == 'backup_upload':
                    summary_parts.append(f"Φόρτωση backup: {action_details.get('file_name', 'N/A')} ({action_details.get('file_size_mb', 0):.2f} MB, {action_details.get('files_count', 0)} αρχεία)")
                elif action == 'delete_rows':
                    count = action_details.get('count', 0)
                    from_excel = action_details.get('from_excel', 0)
                    from_epsilon = action_details.get('from_epsilon', 0)
                    summary_parts.append(f"Διαγραφή {count} γραμμών (Excel: {from_excel}, Epsilon: {from_epsilon})")
                elif action == 'export_bridge':
                    cat = action_details.get('book_category', 'N/A')
                    rows = action_details.get('rows_count', 0)
                    size = action_details.get('file_size_mb', 0)
                    summary_parts.append(f"Λήψη γέφυρας κατηγορίας {cat} ({rows} γραμμές, {size:.2f} MB)")
                elif action == 'export_expenses':
                    cat = action_details.get('book_category', 'N/A')
                    rows = action_details.get('rows_count', 0)
                    size = action_details.get('file_size_mb', 0)
                    file_name = action_details.get('file_name', '')
                    summary_parts.append(f"Λήψη εξοδολογίου κατηγορίας {cat} ({rows} γραμμές, {size:.2f} MB) - {file_name}")
                elif action == 'fetch_data':
                    records = action_details.get('records_count', 0)
                    date_range = f"{action_details.get('date_from', 'N/A')} - {action_details.get('date_to', 'N/A')}"
                    summary_parts.append(f"Ανάκτηση {records} εγγραφών ({date_range})")
                else:
                    # Use description if available
                    desc = details.get('description') or log.get('description')
                    if desc:
                        summary_parts.append(desc)
                
                if summary_parts:
                    enhanced_log['summary'] = ' | '.join(summary_parts)
                
                # Add IP address if available
                if 'ip_address' in details:
                    enhanced_log['ip_address'] = details.get('ip_address')
            
            enhanced_logs.append(enhanced_log)
        
        # Filter by action if provided
        if action_filter:
            enhanced_logs = [log for log in enhanced_logs if action_filter.lower() in (log.get('event_type') or log.get('action') or '').lower()]
        
        # Filter by search term (searches in multiple fields)
        if search_filter:
            search_lower = search_filter.lower()
            filtered_logs = []
            for log in enhanced_logs:
                # Search in user_id, action, event_type, details, summary
                searchable_text = ' '.join([
                    str(log.get('user_id') or ''),
                    str(log.get('user_email') or ''),
                    str(log.get('user_username') or ''),
                    str(log.get('action') or ''),
                    str(log.get('event_type') or ''),
                    str(log.get('summary') or ''),
                    str(log.get('details') or ''),
                    str(log.get('group') or '')
                ]).lower()
                
                if search_lower in searchable_text:
                    filtered_logs.append(log)
            enhanced_logs = filtered_logs
        
        # Keep the order provided by admin_panel (already newest-first in Athens time)
        # Re-limit to respect requested limit
        enhanced_logs = enhanced_logs[:limit]

        return jsonify({'success': True, 'logs': enhanced_logs, 'count': len(enhanced_logs)})
    except Exception as e:
        logger.error(f"Error getting activity logs: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/users/<int:user_id>', methods=['GET'])
@login_required
@_require_admin
def api_get_user_detail(user_id):
    """Get detailed info about a specific user"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        user_detail = admin_panel.admin_get_user_details(user_id)
        if not user_detail:
            return jsonify({'success': False, 'error': 'Could not load user details'}), 404
        
        return jsonify({'success': True, 'user': user_detail})
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/groups/<int:group_id>', methods=['GET'])
@login_required
@_require_admin
def api_get_group_detail(group_id):
    """Get detailed info about a specific group"""
    try:
        group = Group.query.get(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        
        group_detail = admin_panel.admin_get_group_details(group_id)
        if not group_detail:
            return jsonify({'success': False, 'error': 'Could not load group details'}), 404
        
        return jsonify({'success': True, 'group': group_detail})
    except Exception as e:
        logger.error(f"Error getting group {group_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/send-email', methods=['POST'])
@login_required
@_require_admin
def api_send_email():
        """Send email to selected users"""
        try:
                subject = request.form.get('subject', '').strip()
                message = request.form.get('message', '').strip()
                user_ids_str = request.form.get('user_ids', '')

                if not subject or not message or not user_ids_str:
                        return jsonify({'success': False, 'error': 'Subject, message, and users are required'}), 400

                # Parse user IDs
                try:
                        user_ids = [int(uid.strip()) for uid in user_ids_str.split(',') if uid.strip()]
                except ValueError:
                        return jsonify({'success': False, 'error': 'Invalid user IDs format'}), 400

                if not user_ids:
                        return jsonify({'success': False, 'error': 'No valid user IDs provided'}), 400

                from email_utils import send_bulk_email_to_users

                # Build HTML body using the admin template requested by the user
                import os
                app_url = os.getenv('APP_URL', 'http://localhost:5001')
                logo_filename = 'scanmydata_logo_email.png'
                logo_url = f"{app_url}/icons/{logo_filename}"
                # Preserve newlines from message by replacing with <br>
                safe_message = (message or '').replace('\n', '<br>')
                html_body = f"""
        <!DOCTYPE html>
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <img src="{logo_url}" alt="ScanmyData" style="height: 60px; width: auto;">
                    </div>
                    <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                        <h2 style="color: #333; margin: 0;">📧 Μήνυμα από Διαχειριστή</h2>
                    </div>
                    <div style="background-color: white; padding: 20px; border: 1px solid #dee2e6; border-radius: 8px;">
                        <h3 style="color: #495057; border-bottom: 2px solid #e9ecef; padding-bottom: 10px;">{subject}</h3>
                        <div style="margin: 20px 0; line-height: 1.6; color: #495057;">
                            {safe_message}
                        </div>
                    </div>
                    <div style="margin-top: 20px; text-align: center;">
                        <img src="{logo_url}" alt="ScanmyData" style="height: 40px; width: auto; opacity: 0.6;">
                        <p style="color: #6c757d; font-size: 0.9em; margin-top: 10px;">Αυτό το email στάλθηκε από το ScanmyData</p>
                    </div>
                </div>
            </body>
        </html>
        """

                # Resolve recipient emails for logging, then send
                from models import User
                recipients = []
                for uid in user_ids:
                        try:
                                u = User.query.get(uid)
                                if u and u.email:
                                        recipients.append(u.email)
                        except Exception:
                                continue

                results = send_bulk_email_to_users(user_ids, subject, html_body)

                # Log the admin action (include recipient list and sent/failed counts)
                try:
                        _log_admin_action('send_email', 'users', f'{len(user_ids)}_users', {
                                'subject': subject,
                                'user_count': len(user_ids),
                                'recipient_count': len(recipients),
                                'recipients': recipients,
                                'sent': results.get('sent', 0),
                                'failed': results.get('failed', 0),
                                'errors': results.get('errors', [])
                        })
                except Exception:
                        logger.debug('Failed to log admin send_email action')

                return jsonify({
                        'success': True,
                        'message': f'Email sent to {results["sent"]} users, {results["failed"]} failed',
                        'results': results
                })
        except Exception as e:
                logger.error(f"Error sending admin email: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/email-config', methods=['GET', 'POST'])
@login_required
@_require_admin
def api_email_config():
    """Get or update email configuration"""
    if request.method == 'GET':
        try:
            import email_utils
            from app import load_settings
            
            settings = load_settings()
            current_provider = email_utils.get_email_provider()
            
            # Check configuration status for different providers
            smtp_configured = bool(email_utils.SMTP_USER and email_utils.SMTP_PASSWORD)
            resend_configured = bool(email_utils.RESEND_API_KEY)
            railway_proxy_configured = bool(settings.get('railway_proxy_url') or email_utils.RAILWAY_PROXY_URL)
            
            config_status = {
                'smtp': smtp_configured,
                'resend': resend_configured,
                'railway_proxy': railway_proxy_configured and smtp_configured,
                'oauth2_outlook': False  # TODO: Add OAuth2 check
            }
            
            provider_info = {
                'smtp': {
                    'server': email_utils.SMTP_SERVER,
                    'port': email_utils.SMTP_PORT,
                    'user': email_utils.SMTP_USER,
                    'sender': email_utils.SENDER_EMAIL
                } if smtp_configured else None,
                'resend': {
                    'api_key_set': bool(email_utils.RESEND_API_KEY),
                    'sender': email_utils.RESEND_EMAIL_SENDER or email_utils.SENDER_EMAIL
                } if resend_configured else None,
                'railway_proxy': {
                    'proxy_url': settings.get('railway_proxy_url') or email_utils.RAILWAY_PROXY_URL,
                    'smtp_configured': smtp_configured
                } if railway_proxy_configured else None
            }
            
            return jsonify({
                'success': True,
                'current_provider': current_provider,
                'config_status': config_status,
                'provider_info': provider_info,
                'configured': config_status.get(current_provider, False)
            })
        except Exception as e:
            logger.error(f"Error getting email config: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500
    
    # POST method would update config, but we use environment variables
    return jsonify({
        'success': False,
        'error': 'Email configuration is managed via environment variables (.env file)'
    })


@admin_api_bp.route('/test-email', methods=['POST'])
@login_required
@_require_admin
def api_test_email():
    """Send a test email to verify SMTP configuration"""
    try:
        test_email = request.json.get('email') or current_user.email
        if not test_email:
            return jsonify({'success': False, 'error': 'No email address provided'}), 400
        
        import email_utils
        import os
        app_url = os.getenv('APP_URL', 'http://localhost:5001')
        logo_url = f"{app_url}/icons/scanmydata_logo_3000w.png"
        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <img src="{logo_url}" alt="ScanmyData" style="height: 60px; width: auto;">
                    </div>
                    <h2 style="color: #28a745;">✅ SMTP Configuration Test</h2>
                    <p>If you received this email, your SMTP configuration is working correctly!</p>
                    <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 30px 0;">
                    <div style="text-align: center; margin-top: 30px;">
                        <img src="{logo_url}" alt="ScanmyData" style="height: 40px; width: auto; opacity: 0.6;">
                        <p style="color: #6c757d; font-size: 0.9em; margin-top: 10px;">Sent from ScanmyData Admin Panel</p>
                    </div>
                </div>
            </body>
        </html>
        """
        
        success = email_utils.send_email(test_email, '✅ SMTP Test - ScanmyData', html_body)
        
        if success:
            return jsonify({'success': True, 'message': f'Test email sent to {test_email}'})
        else:
            return jsonify({'success': False, 'error': 'Failed to send test email. Check SMTP configuration.'}), 400
    except Exception as e:
        logger.error(f"Error sending test email: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/resend/inbound/forward-sync', methods=['POST'])
@login_required
@_require_admin
def api_resend_inbound_forward_sync():
    """Fetch inbound emails from Resend Receiving API and forward new ones via SMTP."""
    try:
        payload = request.get_json(silent=True) or {}
        limit_raw = payload.get('limit', 25)
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 25

        import email_utils
        result = email_utils.forward_resend_inbound_to_smtp_user(limit=limit)
        ok = bool(result.get('errors') == [] and result.get('failed', 0) == 0)

        return jsonify({
            'success': ok,
            'result': result,
        }), (200 if ok else 400)
    except Exception as e:
        logger.error(f"Error forwarding Resend inbound emails: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/resend/inbound/webhook', methods=['POST'])
def api_resend_inbound_webhook():
    """Webhook receiver for inbound Resend events (event-driven forwarding).

    Verifies the Resend/Svix HMAC-SHA256 signature using the signing secret
    (whsec_... from Resend webhook dashboard → RESEND_WEBHOOK_SIGNING_SECRET).
    """
    import hmac
    import hashlib
    import base64

    try:
        import email_utils

        signing_secret = (getattr(email_utils, 'RESEND_WEBHOOK_SIGNING_SECRET', None) or os.getenv('RESEND_WEBHOOK_SIGNING_SECRET') or '').strip()
        if not signing_secret:
            return jsonify({'success': False, 'error': 'RESEND_WEBHOOK_SIGNING_SECRET is not configured'}), 503

        # --- Svix signature verification ---
        svix_id        = request.headers.get('svix-id', '')
        svix_timestamp = request.headers.get('svix-timestamp', '')
        svix_signature = request.headers.get('svix-signature', '')  # e.g. "v1,<base64>"

        raw_body = request.get_data()  # bytes, must be read before get_json()

        if not svix_id or not svix_timestamp or not svix_signature:
            return jsonify({'success': False, 'error': 'Missing Svix signature headers'}), 401

        # The signed payload is: "{svix-id}.{svix-timestamp}.{body}"
        signed_content = f"{svix_id}.{svix_timestamp}.".encode() + raw_body

        # The secret is whsec_<base64> — strip the prefix and decode
        secret_bytes = base64.b64decode(
            signing_secret[6:] if signing_secret.startswith('whsec_') else signing_secret
        )

        computed = base64.b64encode(
            hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
        ).decode()

        # svix-signature may contain multiple sigs: "v1,<b64> v1,<b64>"
        valid = any(
            hmac.compare_digest(computed, part.split(',', 1)[1])
            for part in svix_signature.split()
            if ',' in part
        )
        if not valid:
            return jsonify({'success': False, 'error': 'Invalid webhook signature'}), 401

        payload = request.get_json(silent=True) or {}
        event_type = str(payload.get('type') or payload.get('event') or '').strip().lower()
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}

        email_id = str(
            data.get('id')
            or data.get('email_id')
            or payload.get('email_id')
            or payload.get('id')
            or ''
        ).strip()

        if email_id:
            result = email_utils.forward_specific_resend_inbound_email(email_id)
        else:
            result = email_utils.forward_resend_inbound_to_smtp_user(limit=10)

        return jsonify({
            'success': result.get('failed', 0) == 0,
            'event': event_type,
            'email_id': email_id,
            'result': result,
        }), (200 if result.get('failed', 0) == 0 else 400)
    except Exception as e:
        logger.error(f"Error handling resend inbound webhook: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups', methods=['GET'])
@login_required
@_require_admin
def api_list_backups():
    """List all local backups"""
    try:
        backups = admin_panel.admin_list_backups()
        return jsonify({'success': True, 'backups': backups})
    except Exception as e:
        logger.error(f"Error listing backups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups/restore/<path:backup_path>', methods=['POST'])
@login_required
@_require_admin
def api_restore_backup_by_path(backup_path):
    # ensure we strip any accidental leading slash – the JS client sometimes
    # passes "/backups/..." strings, which would confuse downstream code.
    backup_path = backup_path.lstrip('/')
    """Restore a backup by its path (handles both local and remote)"""
    try:
        data = request.json or {}
        backup_type = data.get('type', 'remote')
        
        if backup_type == 'remote':
            # For remote backups, backup_path is the Firebase path
            result = admin_panel.admin_restore_remote_backup(
                backup_path, 
                target_group_id=1,  # Will be determined by backend
                current_admin=current_user
            )
        else:
            # For local backups, use the specified target group or find first available
            target_group_id = data.get('target_group_id')
            if not target_group_id:
                groups = admin_panel.admin_list_all_groups()
                if not groups:
                    return jsonify({'success': False, 'error': 'No groups available for restore'}), 400
                target_group_id = groups[0]['id']
            
            result = admin_panel.admin_restore_backup(
                backup_path, 
                target_group_id,
                current_user
            )
        
        # log outcome so admin actions are recorded in server log too
        if result.get('ok'):
            logger.info("backup restore succeeded: %s -> group %s", backup_path, target_group_id)
        else:
            logger.error("backup restore failed: %s -> %s", backup_path, result.get('error'))
        
        return jsonify({
            'success': result.get('ok', False),
            'message': result.get('message', ''),
            'error': result.get('error', ''),
            'restored': result.get('restored', [])
        })
    except Exception as e:
        logger.error(f"Error restoring backup {backup_path}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups/local', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_local_backup_legacy():
    """Delete a local backup. JSON body: {'backup_name': 'name'} - LEGACY endpoint"""
    try:
        data = request.json or {}
        backup_name = data.get('backup_name')
        if not backup_name:
            return jsonify({'success': False, 'error': 'backup_name required'}), 400
        
        # Delete local backup
        import os
        import shutil
        backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
        backup_path = os.path.join(backups_dir, backup_name)
        
        if not os.path.exists(backup_path):
            return jsonify({'success': False, 'error': 'Backup not found'}), 404
        
        # Security check - make sure path is within backups directory
        real_backup_path = os.path.realpath(backup_path)
        real_backups_dir = os.path.realpath(backups_dir)
        
        if not real_backup_path.startswith(real_backups_dir):
            return jsonify({'success': False, 'error': 'Invalid backup path'}), 400
        
        # Delete the backup
        if os.path.isdir(backup_path):
            shutil.rmtree(backup_path)
        elif os.path.isfile(backup_path):
            os.remove(backup_path)
        
        # Log the action
        _log_admin_action('delete_backup', 'local_backup', backup_name)
        
        return jsonify({'success': True, 'message': f'Backup {backup_name} deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting local backup: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups/local', methods=['GET'])
@login_required
@_require_admin
def api_list_local_backups():
    """List only local server backups"""
    try:
        backups = admin_panel.admin_list_backups()
        return jsonify({'success': True, 'backups': backups, 'type': 'local'})
    except Exception as e:
        logger.error(f"Error listing local backups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups/remote', methods=['GET'])
@login_required
@_require_admin
def api_list_remote_backups_only():
    """List only Firebase remote backups"""
    try:
        prefix = request.args.get('prefix')
        backups = admin_panel.admin_list_remote_backups(prefix)
        return jsonify({'success': True, 'backups': backups, 'type': 'remote'})
    except Exception as e:
        logger.error(f"Error listing remote backups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backup/compare', methods=['POST'])
@login_required
@_require_admin
def api_compare_backup():
    """Compare current state with backup for preview before restore"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        logger.debug("api_compare_backup payload: %r", data)
        backup_path = data.get('backup_path')
        # strip possible leading slash
        if isinstance(backup_path, str):
            backup_path = backup_path.lstrip('/')
        backup_type = data.get('type', 'remote')  # 'local' or 'remote'
        
        if not backup_path:
            return jsonify({'success': False, 'error': 'backup_path required'}), 400
        
        comparison = admin_panel.admin_compare_backup_with_current(backup_path, backup_type)
        return jsonify({'success': True, 'comparison': comparison})
    except Exception as e:
        logger.error(f"Error comparing backup: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups/local/<path:backup_name>', methods=['DELETE'])
@login_required
@_require_admin
def api_delete_local_backup(backup_name):
    """Delete a local backup"""
    try:
        import os
        import shutil
        import urllib.parse
        
        # Decode URL-encoded backup name
        decoded_backup_name = urllib.parse.unquote(backup_name)
        
        backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
        backup_path = os.path.join(backups_dir, decoded_backup_name)
        
        logger.info(f"Attempting to delete local backup: {backup_path}")
        
        if not os.path.exists(backup_path):
            # Also try without decoding in case the name is already correct
            backup_path_alt = os.path.join(backups_dir, backup_name)
            if not os.path.exists(backup_path_alt):
                logger.warning(f"Backup not found: {backup_path} or {backup_path_alt}")
                return jsonify({'success': False, 'error': f'Backup not found: {decoded_backup_name}'}), 404
            backup_path = backup_path_alt
        
        # Safety check - ensure it's within backups directory
        real_backup_path = os.path.realpath(backup_path)
        real_backups_dir = os.path.realpath(backups_dir)
        
        if not real_backup_path.startswith(real_backups_dir):
            return jsonify({'success': False, 'error': 'Invalid backup path - security violation'}), 400
        
        # Delete the backup
        try:
            if os.path.isdir(backup_path):
                shutil.rmtree(backup_path)
                logger.info(f"Deleted backup directory: {backup_path}")
            else:
                os.remove(backup_path)
                logger.info(f"Deleted backup file: {backup_path}")
        except Exception as delete_error:
            logger.error(f"Failed to delete backup file/directory: {delete_error}")
            return jsonify({'success': False, 'error': f'Failed to delete backup: {str(delete_error)}'}), 500
        
        # Log action
        _log_admin_action('delete_local_backup', 'backup', decoded_backup_name)
        
        return jsonify({
            'success': True,
            'message': f'Local backup {decoded_backup_name} deleted successfully'
        })
    except Exception as e:
        logger.error(f'Error deleting local backup {backup_name}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backups/download/<path:backup_name>', methods=['GET'])
@login_required
@_require_admin
def api_download_local_backup(backup_name):
    """Download a local backup as ZIP file"""
    import os
    import shutil
    import urllib.parse
    import tempfile
    from flask import send_file
    
    try:
        # Decode URL-encoded backup name
        decoded_backup_name = urllib.parse.unquote(backup_name)
        
        backups_dir = os.path.join(os.getcwd(), 'data', '_backups')
        backup_path = os.path.join(backups_dir, decoded_backup_name)
        
        logger.info(f"Download request for backup: {decoded_backup_name}")
        logger.info(f"Looking for backup at: {backup_path}")
        
        if not os.path.exists(backup_path):
            backup_path_alt = os.path.join(backups_dir, backup_name)
            if not os.path.exists(backup_path_alt):
                logger.error(f"Backup not found: {backup_path}")
                return jsonify({'success': False, 'error': f'Backup not found: {decoded_backup_name}'}), 404
            backup_path = backup_path_alt
        
        # Safety check - ensure it's within backups directory
        real_backup_path = os.path.realpath(backup_path)
        real_backups_dir = os.path.realpath(backups_dir)
        
        if not real_backup_path.startswith(real_backups_dir):
            logger.error(f"Security violation: path outside backups dir")
            return jsonify({'success': False, 'error': 'Invalid backup path - security violation'}), 400
        
        # Create ZIP in a temporary location
        temp_dir = tempfile.gettempdir()
        zip_base_name = os.path.join(temp_dir, f"backup_{decoded_backup_name}")
        
        # Remove old zip if exists
        old_zip = f"{zip_base_name}.zip"
        if os.path.exists(old_zip):
            try:
                os.remove(old_zip)
            except:
                pass
        
        logger.info(f"Creating ZIP archive: {zip_base_name}.zip")
        
        # Create ZIP archive - shutil.make_archive returns the full path
        zip_path = shutil.make_archive(
            zip_base_name,  # Base name without extension
            'zip',          # Format
            backup_path     # Directory to archive
        )
        
        logger.info(f"ZIP created successfully at: {zip_path}")
        
        if not os.path.exists(zip_path):
            logger.error(f"ZIP file was not created: {zip_path}")
            return jsonify({'success': False, 'error': 'Failed to create ZIP file'}), 500
        
        # Log the download action to activity so admin panel shows who downloaded which backup
        try:
            firebase_config.firebase_log_activity(
                current_user.pw_hash or current_user.id,
                '__admin__',
                'backup_download',
                {
                    'backup_name': decoded_backup_name,
                    'backup_path': backup_path,
                    'zip_path': zip_path,
                }
            )
        except Exception:
            logger.debug('Failed to write firebase_log_activity for backup_download')

        # Send the file
        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"{decoded_backup_name}.zip"
        )
    except Exception as e:
        logger.error(f'Error downloading backup {backup_name}: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_api_bp.route('/backup/restore/local', methods=['POST'])
@login_required
@_require_admin
def api_restore_local_backup():
    """Restore from a local backup"""
    try:
        data = request.json or {}
        backup_name = data.get('backup_name')
        target_group_id = data.get('target_group_id', 1)  # Default to first available group
        
        if not backup_name:
            return jsonify({'success': False, 'error': 'backup_name required'}), 400
        
        # Find a suitable group or create one
        from models import Group
        group = Group.query.first()
        if not group:
            return jsonify({'success': False, 'error': 'No groups available for restore'}), 400
        
        result = admin_panel.admin_restore_backup(backup_name, group.id, current_user)
        
        return jsonify({
            'success': result['ok'],
            'message': result.get('message'),
            'error': result.get('error')
        })
    except Exception as e:
        logger.error(f'Error restoring local backup: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500
