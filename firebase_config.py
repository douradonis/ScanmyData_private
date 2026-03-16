"""
Firebase configuration and initialization module
"""
import os
import json
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import base64

# Load environment variables from .env file
load_dotenv()

try:
    from infisical_bootstrap import bootstrap_infisical_secrets
    bootstrap_infisical_secrets(logger=logging.getLogger(__name__))
except Exception:
    # Keep module import-safe if Infisical bootstrap is unavailable.
    pass

import firebase_admin
from firebase_admin import credentials
from firebase_admin import db
from firebase_admin import auth as fb_auth
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
import time
import threading
from typing import List, Dict
import encryption
import math

# ============================================================================
# Firebase Initialization
# ============================================================================

FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH")
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

_firebase_app = None
_firebase_initialized = False


def _firebase_credential_input() -> tuple[Optional[Any], Optional[str]]:
    """Return Firebase credentials in a format accepted by Certificate()."""
    raw_json = (os.getenv("FIREBASE_CREDENTIALS_JSON") or "").strip()
    raw_json_b64 = (os.getenv("FIREBASE_CREDENTIALS_JSON_B64") or "").strip()
    path = (os.getenv("FIREBASE_CREDENTIALS_PATH") or "").strip()

    if raw_json:
        try:
            return json.loads(raw_json), "FIREBASE_CREDENTIALS_JSON"
        except Exception as exc:
            logger.warning("Invalid FIREBASE_CREDENTIALS_JSON payload: %s", exc)

    if raw_json_b64:
        try:
            decoded = base64.b64decode(raw_json_b64).decode("utf-8")
            return json.loads(decoded), "FIREBASE_CREDENTIALS_JSON_B64"
        except Exception as exc:
            logger.warning("Invalid FIREBASE_CREDENTIALS_JSON_B64 payload: %s", exc)

    # Fallback: build service-account JSON from individual secret fields.
    # Useful when Infisical stores firebase-key fields separately.
    field_names = [
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "client_id",
        "auth_uri",
        "token_uri",
        "auth_provider_x509_cert_url",
        "client_x509_cert_url",
        "universe_domain",
    ]
    service_account = {}
    for field in field_names:
        val = (os.getenv(field) or "").strip()
        if val:
            if field == "private_key":
                val = val.replace("\\n", "\n")
            service_account[field] = val

    required = ["type", "project_id", "private_key", "client_email", "token_uri"]
    if all(service_account.get(k) for k in required):
        return service_account, "SERVICE_ACCOUNT_FIELDS"

    if path:
        if os.path.exists(path):
            return path, "FIREBASE_CREDENTIALS_PATH"
        logger.warning("Firebase credentials file not found: %s", path)

    return None, None


def init_firebase():
    """Initialize Firebase Admin SDK and (optionally) enable DB I/O logging.

    This wraps `db.reference` with a small proxy that logs every read/write/push
    including an approximate payload size and elapsed time. Enable/disable via
    environment variable `FIREBASE_LOG_IO` (default: enabled).
    """
    global _firebase_app, _firebase_initialized
    
    if _firebase_initialized:
        return True
    
    try:
        cred_input, cred_source = _firebase_credential_input()
        database_url = (os.getenv("FIREBASE_DATABASE_URL") or "").strip()

        if not cred_input:
            logger.warning(
                "Firebase disabled: missing credentials. Set FIREBASE_CREDENTIALS_JSON or FIREBASE_CREDENTIALS_JSON_B64 in Infisical"
            )
            return False

        if not database_url:
            logger.warning("Firebase disabled: FIREBASE_DATABASE_URL not set")
            return False

        # Initialize Firebase Admin SDK
        cred = credentials.Certificate(cred_input)
        _firebase_app = firebase_admin.initialize_app(
            cred,
            {
                'databaseURL': database_url
            }
        )
        _firebase_initialized = True

        # Optionally monkey-patch db.reference to log every DB I/O
        try:
            if os.getenv('FIREBASE_LOG_IO', '1') == '1':
                _patch_db_reference_logging()
                logger.info('Firebase DB I/O logging enabled (FIREBASE_LOG_IO=1)')
        except Exception:
            logger.exception('Failed to enable Firebase DB I/O logging')

        logger.info("Firebase Admin SDK initialized successfully via %s", cred_source or "unknown source")
        return True
    
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")
        _firebase_initialized = False
        return False


def is_firebase_enabled() -> bool:
    """Check if Firebase is properly initialized"""
    return _firebase_initialized


# -------------------------
# Optional DB reference I/O logging
# -------------------------
def _patch_db_reference_logging() -> None:
    """Wrap `db.reference` so every get/set/update/delete/push is logged.

    The wrapper proxies the original Reference and intercepts common I/O
    methods to emit: action, path, approximate payload/result size (bytes)
    and elapsed_ms. This gives an application-level record of RTDB traffic
    that can be compared to Firebase Usage charts.
    """
    try:
        orig_ref = db.reference
    except Exception:
        logger.debug('firebase_admin.db.reference not available for patching')
        return

    class _LoggingRef:
        def __init__(self, ref, path=None):
            self._ref = ref
            self._path = path or getattr(ref, 'path', '<unknown>')

        def _log(self, action: str, payload=None, result=None, start_ts=None):
            try:
                now = datetime.now(timezone.utc)
                start = start_ts or now
                elapsed_ms = int((now - start).total_seconds() * 1000)
                size = 0
                try:
                    if payload is not None:
                        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                    elif result is not None:
                        size = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                except Exception:
                    size = 0
                logger.info('firebase.%s path=%s size=%d elapsed_ms=%d', action, self._path, size, elapsed_ms)
            except Exception:
                logger.exception('firebase logging failed')

        def get(self, *args, **kwargs):
            start = datetime.now(timezone.utc)
            res = self._ref.get(*args, **kwargs)
            self._log('read', result=res, start_ts=start)
            return res

        def set(self, value, *args, **kwargs):
            start = datetime.now(timezone.utc)
            res = self._ref.set(value, *args, **kwargs)
            self._log('write', payload=value, start_ts=start)
            return res

        def update(self, value, *args, **kwargs):
            start = datetime.now(timezone.utc)
            res = self._ref.update(value, *args, **kwargs)
            self._log('update', payload=value, start_ts=start)
            return res

        def delete(self, *args, **kwargs):
            start = datetime.now(timezone.utc)
            res = self._ref.delete(*args, **kwargs)
            self._log('delete', start_ts=start)
            return res

        def push(self, *args, **kwargs):
            start = datetime.now(timezone.utc)
            res = self._ref.push(*args, **kwargs)
            self._log('push', start_ts=start)
            return res

        def __getattr__(self, name):
            return getattr(self._ref, name)

    def _wrapped_reference(path=None, app=None):
        ref = orig_ref(path, app=app or _firebase_app)
        return _LoggingRef(ref, path=path)

    try:
        db.reference = _wrapped_reference
    except Exception:
        logger.exception('Failed to install db.reference logging wrapper')



# ============================================================================
# Firebase Auth Helpers
# ============================================================================

def firebase_create_user(email: str, password: str, display_name: str = "") -> Optional[Dict[str, Any]]:
    """Create a new user in Firebase Authentication"""
    try:
        if not is_firebase_enabled():
            logger.warning("Firebase not initialized - cannot create user")
            return None
        
        user = fb_auth.create_user(
            email=email,
            password=password,
            display_name=display_name,
            email_verified=False
        )
        
        logger.info(f"Firebase user created: {email} (UID: {user.uid})")
        return {
            'uid': user.uid,
            'email': user.email,
            'display_name': user.display_name
        }
    except Exception as e:
        logger.error(f"Failed to create Firebase user {email}: {e}")
        return None


def firebase_get_user(uid: str) -> Optional[Dict[str, Any]]:
    """Get user info from Firebase"""
    try:
        if not is_firebase_enabled():
            return None
        
        user = fb_auth.get_user(uid)
        return {
            'uid': user.uid,
            'email': user.email,
            'display_name': user.display_name,
            'email_verified': user.email_verified,
            'disabled': user.disabled
        }
    except Exception as e:
        logger.error(f"Failed to get Firebase user {uid}: {e}")
        return None


def firebase_get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Get user info from Firebase by email"""
    try:
        if not is_firebase_enabled():
            return None
        
        user = fb_auth.get_user_by_email(email)
        return {
            'uid': user.uid,
            'email': user.email,
            'display_name': user.display_name,
            'email_verified': user.email_verified,
            'disabled': user.disabled
        }
    except Exception as e:
        logger.error(f"Failed to get Firebase user by email {email}: {e}")
        return None


def firebase_delete_user(uid: str) -> bool:
    """Delete a user from Firebase"""
    try:
        if not is_firebase_enabled():
            return False
        
        fb_auth.delete_user(uid)
        logger.info(f"Firebase user deleted: {uid}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete Firebase user {uid}: {e}")
        return False


def firebase_update_user_password(uid: str, password: str) -> bool:
    """Update user password in Firebase"""
    try:
        if not is_firebase_enabled():
            return False
        
        fb_auth.update_user(uid, password=password)
        logger.info(f"Firebase user password updated: {uid}")
        return True
    except Exception as e:
        logger.error(f"Failed to update Firebase user password {uid}: {e}")
        return False


def firebase_set_custom_claims(uid: str, claims: Dict[str, Any]) -> bool:
    """Set custom claims for a user (useful for role management)"""
    try:
        if not is_firebase_enabled():
            return False
        
        fb_auth.set_custom_user_claims(uid, claims)
        logger.info(f"Custom claims set for user {uid}: {claims}")
        return True
    except Exception as e:
        logger.error(f"Failed to set custom claims for {uid}: {e}")
        return False


# ============================================================================
# Firebase Realtime Database Helpers
# ============================================================================

def firebase_write_data(path: str, data: Dict[str, Any]) -> bool:
    """Write data to Firebase Realtime Database"""
    try:
        if not is_firebase_enabled():
            logger.warning("Firebase not initialized - cannot write data")
            return False
        # sanitize path: remove leading slash and replace illegal characters in each segment
        def _sanitize_path(p: str) -> str:
            if not p:
                return ''
            # strip leading/trailing slashes
            p = p.lstrip('/').rstrip('/')
            parts = [seg for seg in p.split('/') if seg != '']
            safe_parts = []
            for seg in parts:
                # Firebase keys cannot contain . # $ [ ]
                for ch in ['.', '#', '$', '[', ']']:
                    seg = seg.replace(ch, '_')
                safe_parts.append(seg)
            return '/'.join(safe_parts)

        safe_path = _sanitize_path(path)
        ref = db.reference(safe_path)
        # If caller passed None, treat as a delete request
        if data is None:
            try:
                ref.delete()
                logger.debug(f"Data deleted from Firebase: {path}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete data from Firebase at {path}: {e}")
                return False

        ref.set(data)
        logger.debug(f"Data written to Firebase: {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write data to Firebase at {path}: {e}")
        return False


def firebase_read_data(path: str) -> Optional[Dict[str, Any]]:
    """Read data from Firebase Realtime Database"""
    try:
        if not is_firebase_enabled():
            return None
        
        ref = db.reference(path)
        data = ref.get()
        return data
    except Exception as e:
        logger.error(f"Failed to read data from Firebase at {path}: {e}")
        return None


def firebase_update_data(path: str, data: Dict[str, Any]) -> bool:
    """Update (partial) data in Firebase Realtime Database"""
    try:
        if not is_firebase_enabled():
            return False
        
        ref = db.reference(path)
        ref.update(data)
        logger.debug(f"Data updated in Firebase: {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to update data in Firebase at {path}: {e}")
        return False


def firebase_delete_data(path: str) -> bool:
    """Delete data from Firebase Realtime Database"""
    try:
        if not is_firebase_enabled():
            return False
        
        ref = db.reference(path)
        ref.delete()
        logger.debug(f"Data deleted from Firebase: {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete data from Firebase at {path}: {e}")
        return False


def firebase_log_activity(user_id: str, group_name: str, action: str, details: Optional[Dict] = None) -> bool:
    """Log user activity to Firebase for traffic tracking"""
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        log_entry = {
            'user_id': user_id,
            'group': group_name,
            'action': action,
            'timestamp': timestamp,
            'details': details or {}
        }

        # Determine the folder to use for logs
        # Try to get data_folder from Group model if group_name matches a group
        folder_name = group_name
        try:
            # Lazy import to avoid circular dependencies
            from models import Group
            grp = Group.query.filter_by(name=group_name).first()
            if grp and getattr(grp, 'data_folder', None):
                folder_name = grp.data_folder
        except Exception:
            # If we can't query, fall back to group_name
            pass

        wrote_any = False

        # Attempt to write to Firebase if enabled
        try:
            if is_firebase_enabled():
                # Write to activity log under /activity_logs/{group}/{timestamp}
                # Replace special characters in timestamp for Firebase path compatibility
                safe_timestamp = timestamp.replace(":", "-").replace("+", "_").replace(".", "-")
                path = f'/activity_logs/{folder_name}/{safe_timestamp}'
                if firebase_write_data(path, log_entry):
                    wrote_any = True
        except Exception:
            # keep going; we'll still write a local log
            pass

        # Also append to a local activity.log per-group for offline inspection and admin panel fallback
        try:
            data_dir = os.path.join(os.getcwd(), 'data')
            os.makedirs(data_dir, exist_ok=True)
            
            group_dir = os.path.join(data_dir, str(folder_name) if folder_name else 'global')
            os.makedirs(group_dir, exist_ok=True)
            activity_path = os.path.join(group_dir, 'activity.log')
            with open(activity_path, 'a', encoding='utf-8') as fh:
                # store a compact JSON line for easier parsing
                fh.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            wrote_any = True
        except Exception:
            pass

        # Bump global activity version for auto-reload mechanisms
        try:
            _increment_activity_version()
        except Exception:
            pass

        return wrote_any
    except Exception as e:
        logger.error(f"Failed to log activity to Firebase: {e}")
        return False


# ---------------------------------------------------------------------------
# Activity Version Counter (for frontend auto-reload)
# ---------------------------------------------------------------------------
_activity_version_lock = threading.Lock()

def _activity_version_path() -> str:
    data_dir = os.path.join(os.getcwd(), 'data', 'system')
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, 'activity_version.txt')

def _increment_activity_version() -> int:
    path = _activity_version_path()
    with _activity_version_lock:
        try:
            current = 0
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        current = int((f.read() or '0').strip() or 0)
                except Exception:
                    current = 0
            new_val = current + 1
            with open(path, 'w', encoding='utf-8') as f:
                f.write(str(new_val))
            return new_val
        except Exception:
            # best-effort only
            return 0

def get_activity_version() -> int:
    path = _activity_version_path()
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return int((f.read() or '0').strip() or 0)
        return 0
    except Exception:
        return 0


def firebase_get_group_activity_logs(group_name: str, limit: int = 100) -> list:
    """Retrieve activity logs for a group"""
    try:
        if not is_firebase_enabled():
            return []
        
        path = f'/activity_logs/{group_name}'
        data = firebase_read_data(path)
        
        if not data:
            return []
        
        # Convert dict to list and sort by timestamp
        logs = []
        for key, value in data.items():
            logs.append(value)
        
        # Sort by timestamp descending (newest first)
        logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        return logs[:limit]
    except Exception as e:
        logger.error(f"Failed to retrieve activity logs for {group_name}: {e}")
        return []


# ============================================================================
# Backup & Export Helpers
# ============================================================================

def firebase_export_group_data(group_name: str) -> Optional[Dict[str, Any]]:
    """Export all data for a group from Firebase"""
    try:
        if not is_firebase_enabled():
            return None
        
        path = f'/groups/{group_name}'
        data = firebase_read_data(path)
        return data
    except Exception as e:
        logger.error(f"Failed to export group data for {group_name}: {e}")
        return None


def _maybe_decompress_blob(obj: Any) -> Any:
    """If object is a compressed payload created by firebase_write_compressed, decompress it."""
    try:
        if isinstance(obj, dict) and obj.get('_compressed'):
            import gzip
            b64 = obj.get('content') or ''
            raw = base64.urlsafe_b64decode(b64.encode('utf-8'))
            data = gzip.decompress(raw)
            return json.loads(data.decode('utf-8'))
    except Exception:
        pass
    return obj


def firebase_read_data_compressed(path: str) -> Optional[Dict[str, Any]]:
    """Read data and automatically decompress if stored as compressed blob."""
    try:
        data = firebase_read_data(path)
        if data is None:
            return None
        # If top-level object is compressed blob
        decompressed = _maybe_decompress_blob(data)
        if decompressed is not data:
            return decompressed

        # Walk dict and decompress any nested compressed blobs (shallow)
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                out[k] = _maybe_decompress_blob(v)
            return out
        return data
    except Exception as e:
        logger.error(f"Failed to read compressed data from Firebase at {path}: {e}")
        return None


def firebase_write_compressed(path: str, data: Dict[str, Any], compress_threshold: int = 5 * 1024) -> bool:
    """Write data to Firebase; if serialized size > threshold, gzip-compress and base64 encode it.
    Stores compressed payload under the same path as {'_compressed': True, 'content': '<b64>'}.
    """
    try:
        text = json.dumps(data, ensure_ascii=False)
        raw = text.encode('utf-8')
        if len(raw) <= compress_threshold:
            return firebase_write_data(path, data)

        import gzip
        compressed = gzip.compress(raw)
        b64 = base64.urlsafe_b64encode(compressed).decode('utf-8')
        payload = {'_compressed': True, 'content': b64}
        return firebase_write_data(path, payload)
    except Exception as e:
        logger.error(f"Failed to write compressed data to Firebase at {path}: {e}")
        return False


def firebase_push_group_files(group_name: str, local_data_root: str = None, dry_run: bool = False, verbose: bool = False):
    """Upload group files from local data/ folder to Firebase /groups/{group_name}/files.
    
    Also detects and removes files from Firebase that have been deleted locally.
    This is called on logout to sync any changes made to files back to Firebase.
    Files are read from data/{group_name}/ (and subdirectories), encrypted, and uploaded.
    
    Returns True if push succeeded or no files found.
    """
    try:
        if not is_firebase_enabled():
            logger.warning('[PUSH] Firebase not enabled; cannot push group files')
            return False

        if local_data_root is None:
            local_data_root = os.path.join(os.getcwd(), 'data')

        source_dir = os.path.join(local_data_root, group_name)
        if not os.path.isdir(source_dir):
            logger.warning('[PUSH] Group directory not found: %s', source_dir)
            return True  # No error, just nothing to push

        # Get encryption key
        fernet_key = encryption._ensure_key()
        if not fernet_key:
            logger.error('[PUSH] No Fernet key available; cannot encrypt files')
            return False

        from cryptography.fernet import Fernet
        cipher = Fernet(fernet_key)
        
        files_uploaded = 0
        files_failed = 0
        files_deleted = 0
        # When dry_run is requested, collect candidate lists instead of performing writes
        upload_candidates = []
        delete_candidates = []
        
        # Build set of local file keys (what should exist in Firebase)
        local_file_keys = set()
        
        # prepare smart sync remote metadata map
        # smart_sync will skip uploading files whose mtime is unchanged; however
        # the epsilon/ and excel/ folders are exempt (they are always uploaded on
        # logout because they change constantly).
        smart_sync = os.getenv('FIREBASE_SMART_SYNC', '1') == '1'
        remote_meta = {}
        if smart_sync:
            # read existing remote file tree once
            try:
                firebase_path = f'/groups/{group_name}/files'
                remote_tree = firebase_read_data_compressed(firebase_path) or {}
                def _collect_meta(obj, prefix=''):
                    if not isinstance(obj, dict):
                        return
                    for k, v in obj.items():
                        keystr = str(k).lstrip('/')
                        full = f"{prefix}/{keystr}".lstrip('/')
                        if isinstance(v, dict) and 'content' in v and '_meta' in v:
                            try:
                                mtime = float(v.get('_meta', {}).get('mtime', 0) or 0)
                            except Exception:
                                mtime = 0
                            remote_meta[full] = mtime
                        elif isinstance(v, dict):
                            _collect_meta(v, full)
                _collect_meta(remote_tree, '')
                logger.debug('[PUSH] Collected %d remote metadata entries for smart sync', len(remote_meta))
            except Exception as e:
                logger.warning('[PUSH] Could not collect remote metadata for smart sync: %s', e)
        
        # Scan all files in the source directory (including subdirectories)
        for root, dirs, files in os.walk(source_dir):
            for fname in files:
                try:
                    # Skip certain files
                    # Allow uploading log files (activity.log / error.log) so
                    # the server copy can be the source of truth in Firebase.
                    # Keep skipping hidden files and internal state files.
                    if fname.startswith('.') or fname in ['files_json', 'fiscal_meta_json']:
                        continue
                    
                    file_path = os.path.join(root, fname)
                    
                    # Determine the key name for Firebase
                    # If file is in a subdirectory (excel/, epsilon/), include it in the key
                    # Otherwise, convert extension to suffix (json -> _json, xlsx -> _xlsx)
                    rel_path = os.path.relpath(file_path, source_dir)
                    rel_path = rel_path.replace('\\', '/')  # Normalize paths
                    
                    # If file is in root directory, convert extension to suffix
                    if '/' not in rel_path:
                        # This is a root-level file
                        # Convert: credentials_settings.json -> credentials_settings_json
                        name_no_ext = os.path.splitext(fname)[0]
                        ext = os.path.splitext(fname)[1]
                        
                        # Map extensions to suffixes
                        ext_map = {
                            '.json': '_json',
                            '.xlsx': '_xlsx',
                            '.xls': '_xls',
                            '.pdf': '_pdf',
                            '.csv': '_csv',
                            '.txt': '_txt',
                            '.xml': '_xml',
                        }
                        
                        # Special-case: for root-level Excel files, store them under 'imports/'
                        if ext.lower() in ('.xls', '.xlsx'):
                            firebase_key = '/'.join(['imports', fname])
                        else:
                            suffix = ext_map.get(ext.lower(), f'_{ext.lower().lstrip(".")}')
                            firebase_key = f"{name_no_ext}{suffix}"
                    else:
                        # File is in a subdirectory (excel/, epsilon/)
                        # Keep the path as-is
                        firebase_key = rel_path
                    
                    # Normalize/sanitize the firebase key the same way firebase_write_data does
                    # so that local_file_keys matches the actual remote keys stored.
                    safe_parts = []
                    for seg in firebase_key.lstrip('/').split('/'):
                        seg_safe = seg
                        for ch in ['.', '#', '$', '[', ']']:
                            seg_safe = seg_safe.replace(ch, '_')
                        safe_parts.append(seg_safe)
                    safe_firebase_key = '/'.join(safe_parts)
                    local_file_keys.add(safe_firebase_key)
                    
                    # smart sync: skip if unchanged and not in epsilon/excel
                    if smart_sync:
                        rel = os.path.relpath(file_path, source_dir).replace('\\', '/')
                        parts = rel.split('/')
                        if not any(p in ('epsilon', 'excel') for p in parts):
                            # compare with remote metadata
                            remote_mtime = remote_meta.get(safe_firebase_key)
                            try:
                                local_mtime = os.path.getmtime(file_path)
                            except Exception:
                                local_mtime = None
                            if remote_mtime and local_mtime and local_mtime <= remote_mtime:
                                logger.debug('[PUSH] Skipping unchanged file %s', file_path)
                                # still record key so deletion logic knows it exists
                                local_file_keys.add(safe_firebase_key)
                                continue
                    # Read file
                    with open(file_path, 'rb') as f:
                        file_content = f.read()
                    
                    # Encrypt
                    encrypted_content = cipher.encrypt(file_content)
                    logger.debug('[PUSH] Encrypted file %s (size: %d -> %d)', firebase_key, len(file_content), len(encrypted_content))
                    
                    # Base64 encode
                    content_b64 = base64.urlsafe_b64encode(encrypted_content).decode('utf-8')
                    
                    # Get mtime
                    mtime = os.path.getmtime(file_path)
                    
                    # Prepare file payload
                    file_payload = {
                        'content': content_b64,
                        '_meta': {
                            'mtime': mtime,
                            'size': len(file_content)
                        }
                    }
                    
                    # Upload to Firebase (or simulate if dry_run)
                    firebase_path = f'/groups/{group_name}/files/{firebase_key}'
                    if verbose:
                        logger.info('[PUSH] Preparing upload: local=%s -> firebase=%s', file_path, firebase_path)
                    else:
                        logger.debug('[PUSH] Preparing upload: local=%s -> firebase=%s', file_path, firebase_path)

                    if dry_run:
                        upload_candidates.append(firebase_key)
                        files_uploaded += 0
                    else:
                        if firebase_write_data(firebase_path, file_payload):
                            logger.info('[PUSH] Uploaded file to Firebase: %s', firebase_key)
                            files_uploaded += 1
                        else:
                            files_failed += 1
                            logger.error('[PUSH] Failed to upload file to Firebase: %s', firebase_key)
                
                except Exception as e:
                    files_failed += 1
                    logger.error('[PUSH] Error processing file %s: %s', fname, e)
        
        # Now detect and remove deleted files from Firebase
        try:
            firebase_path = f'/groups/{group_name}/files'
            remote_files = firebase_read_data_compressed(firebase_path) or {}
            
            if isinstance(remote_files, dict):
                def _find_all_keys(obj, prefix=''):
                    """Recursively find all keys that look like files (have content + _meta)"""
                    result = set()
                    if not isinstance(obj, dict):
                        return result
                    for key, val in obj.items():
                        key_str = str(key).lstrip('/')
                        current_path = f"{prefix}/{key_str}".lstrip('/')
                        if isinstance(val, dict) and 'content' in val and '_meta' in val:
                            result.add(current_path)
                        elif isinstance(val, dict):
                            result.update(_find_all_keys(val, current_path))
                    return result
                
                remote_file_keys = _find_all_keys(remote_files)
                
                # Find keys that exist in Firebase but not locally
                deleted_keys = remote_file_keys - local_file_keys
                
                for deleted_key in deleted_keys:
                    try:
                        delete_path = f'/groups/{group_name}/files/{deleted_key}'
                        if dry_run:
                            delete_candidates.append(deleted_key)
                        else:
                            if firebase_write_data(delete_path, None):  # Writing None deletes the key
                                logger.info('[PUSH] Deleted file from Firebase: %s', deleted_key)
                                files_deleted += 1
                            else:
                                logger.warning('[PUSH] Failed to delete file from Firebase: %s', deleted_key)
                    except Exception as e:
                        logger.error('[PUSH] Error deleting file from Firebase %s: %s', deleted_key, e)
        
        except Exception as e:
            logger.warning('[PUSH] Could not detect deleted files: %s', e)
        
        # Log summary
        logger.info('[PUSH] Pushed files for group %s: uploaded %d files, deleted %d files, %d failed', 
                    group_name, files_uploaded, files_deleted, files_failed)

        if dry_run:
            return {
                'success': True,
                'upload_candidates': upload_candidates,
                'delete_candidates': delete_candidates,
                'uploaded_count': len(upload_candidates),
                'deleted_count': len(delete_candidates),
                'failed_count': files_failed
            }

        return True
    
    except Exception as e:
        logger.error('[PUSH] Failed to push files for group %s: %s', group_name, e)
        return False


def firebase_pull_group_to_local(group_name: str, local_data_root: str = None) -> bool:
    """Download group data from Firebase and populate `data/<group_name>` locally.

    This is a best-effort lazy-sync used when server is missing a group's data.
    Only files from the 'files' folder in Firebase are pulled and stored locally.
    All files are flattened directly into the group's folder (no nested structure).
    It will:
    1. Read only data from /groups/{group_name}/files
    2. Recursively find all files (content + _meta pairs)
    3. Decrypt encrypted files using Fernet key
    4. Store all files directly in data/<group_name>/ (flattened)
    5. Log all actions for admin visibility
    
    Returns True if pull succeeded (or no data found to pull).
    """
    try:
        if not is_firebase_enabled():
            logger.warning('Firebase not enabled; cannot pull group data')
            return False

        if local_data_root is None:
            local_data_root = os.path.join(os.getcwd(), 'data')

        # Read only from the 'files' subfolder in Firebase
        path = f'/groups/{group_name}/files'
        exported = firebase_read_data_compressed(path) or {}
        if not isinstance(exported, dict):
            logger.warning('No files found in Firebase for %s at path %s', group_name, path)
            return True

        target_dir = os.path.join(local_data_root, group_name)
        os.makedirs(target_dir, exist_ok=True)

        files_created = 0
        files_failed = 0
        # Progress tracking helpers
        def _progress_path():
            try:
                grp_dir = os.path.join(os.getcwd(), 'data', group_name)
                os.makedirs(grp_dir, exist_ok=True)
                return os.path.join(grp_dir, '.sync_progress.json')
            except Exception:
                return os.path.join(os.getcwd(), 'data', f'.sync_progress_{group_name}.json')

        def _set_progress(status: str, percent: int = 0, message: str = ''):
            try:
                p = _progress_path()
                with open(p, 'w', encoding='utf-8') as fh:
                    json.dump({'status': status, 'percent': int(percent or 0), 'message': message}, fh)
            except Exception:
                pass

        def _clear_progress():
            try:
                p = _progress_path()
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        _set_progress('running', 0, 'Starting pull')
        
        # Get encryption key once
        fernet_key = encryption._ensure_key()
        if not fernet_key:
            logger.warning('[PULL] No Fernet key available; encrypted files will not be decrypted')
        
        # decide whether smart sync (skip unchanged files) is enabled
        # note: epsilon/ and excel/ subdirectories are *always* pulled regardless
        # of smart_sync, because those folders change frequently and we want to
        # sync them only on login/logout rather than incrementally.
        smart_sync = os.getenv('FIREBASE_SMART_SYNC', '1') == '1'
        if smart_sync:
            logger.debug('[PULL] Smart sync is enabled (unchanged files may be skipped); epsilon/excel will be ignored for skipping')
        else:
            logger.debug('[PULL] Smart sync is disabled; all files will be pulled')
        
        def _get_file_name_with_extension(key_name):
            """
            Convert Firebase key names back to local file names.
            Handles both unsanitized keys (foo.xlsx.meta_json) and sanitized ones
            produced by RTDB key rules (foo_xlsx_meta_json).
            """
            key_str = str(key_name).lstrip('/')

            # Handle sanitized metadata keys first (critical for CoA/client_db metadata).
            special_suffixes = {
                '_xlsx_meta_json': '.xlsx.meta.json',
                '_xls_meta_json': '.xls.meta.json',
                '_csv_meta_json': '.csv.meta.json',
            }
            for suffix, ext in special_suffixes.items():
                if key_str.endswith(suffix):
                    return f"{key_str[:-len(suffix)]}{ext}"

            # Also handle unsanitized metadata keys.
            unsanitized_suffixes = {
                '.xlsx.meta_json': '.xlsx.meta.json',
                '.xls.meta_json': '.xls.meta.json',
                '.csv.meta_json': '.csv.meta.json',
            }
            for suffix, ext in unsanitized_suffixes.items():
                if key_str.endswith(suffix):
                    return f"{key_str[:-len(suffix)]}{ext}"

            extension_map = {
                '_json': '.json',
                '_xlsx': '.xlsx',
                '_xls': '.xls',
                '_pdf': '.pdf',
                '_csv': '.csv',
                '_txt': '.txt',
                '_xml': '.xml',
                '_log': '.log',
            }

            for suffix, ext in extension_map.items():
                if key_str.endswith(suffix):
                    return f"{key_str[:-len(suffix)]}{ext}"

            return key_str
        
        # compute total files to process for progress estimation
        def _count_files(obj):
            if not isinstance(obj, dict):
                return 0
            c = 0
            for key, val in obj.items():
                if isinstance(val, dict) and 'content' in val and '_meta' in val:
                    c += 1
                elif isinstance(val, dict):
                    c += _count_files(val)
            return c

        total_files = _count_files(exported) or 0
        processed_files = 0
        bytes_downloaded = 0

        def _recursive_process(obj, current_path=""):
            """Recursively find all files (flattened) and materialize them"""
            nonlocal files_created, files_failed, bytes_downloaded
            
            if not isinstance(obj, dict):
                return
            
            for key, val in obj.items():
                try:
                    # Check if this is a binary file (has content + _meta)
                    # compute the full key path for routing decisions
                    full_key = (current_path + '/' + key).lstrip('/').rstrip('/') if current_path else key
                    if isinstance(val, dict) and 'content' in val and '_meta' in val:
                        # This is a file - materialize it according to the remote key structure
                        try:
                            # File name is derived from the current key segment
                            file_name = _get_file_name_with_extension(key)
                            content_b64 = val.get('content')
                            logger.debug('[PULL] Processing file: %s (original key: %s, full_key: %s)', file_name, key, full_key)

                            # Base64 decode
                            blob = base64.urlsafe_b64decode(content_b64.encode('utf-8'))
                            logger.debug('[PULL] Decoded %d bytes for %s', len(blob), file_name)

                            # Try to decrypt if key is available
                            decrypted = False
                            if fernet_key:
                                from cryptography.fernet import Fernet
                                try:
                                    cipher = Fernet(fernet_key)
                                    blob = cipher.decrypt(blob)
                                    decrypted = True
                                    logger.info('[PULL] Decrypted file: %s (size: %d bytes)', file_name, len(blob))
                                except Exception as de:
                                    logger.warning('[PULL] Decrypt failed for %s: %s (using as-is)', file_name, de)
                            else:
                                logger.debug('[PULL] No key available; using raw blob for %s', file_name)

                            # account bytes downloaded for this pull (after optional decryption)
                            try:
                                bytes_downloaded += len(blob)
                            except Exception:
                                pass

                            # Determine where to write the file.
                            # Prefer preserving remote subdirectory structure (except imports/)
                            file_dir = target_dir
                            # If the file was stored under 'imports/' in Firebase, materialize it directly in group root
                            if full_key.startswith('imports/'):
                                file_dir = target_dir
                                logger.debug('[PULL] Routing imports file to group root: %s', file_name)
                            # If the remote key contains subdirectories, use them as local subdirs
                            elif '/' in full_key:
                                subdir = os.path.dirname(full_key)
                                file_dir = os.path.join(target_dir, subdir)
                                os.makedirs(file_dir, exist_ok=True)
                                logger.debug('[PULL] Preserving remote subdir %s for file %s', subdir, file_name)
                            # Special-case Chart of Accounts files: always materialize in group root
                            elif any(name_part in file_name for name_part in ('chart_of_accounts_b', 'chart_of_accounts_g')):
                                file_dir = target_dir
                                logger.debug('[PULL] Routing chart_of_accounts file to group root: %s', file_name)
                            # Otherwise, route .xlsx files to excel/ subdirectory
                            elif file_name.endswith('.xlsx'):
                                file_dir = os.path.join(target_dir, 'excel')
                                os.makedirs(file_dir, exist_ok=True)
                                logger.debug('[PULL] Routing .xlsx file to excel/ subdirectory: %s', file_name)
                            # Route epsilon_invoices files to epsilon/ subdirectory
                            elif 'epsilon_invoices' in file_name:
                                file_dir = os.path.join(target_dir, 'epsilon')
                                os.makedirs(file_dir, exist_ok=True)
                                logger.debug('[PULL] Routing epsilon_invoices file to epsilon/ subdirectory: %s', file_name)

                            # Build local path before potentially skipping
                            target_file_path = os.path.join(file_dir, file_name)

                            # smart sync: skip if file unchanged and not in epsilon/excel
                            if smart_sync:
                                rel = os.path.relpath(target_file_path, target_dir)
                                parts = rel.replace('\\', '/').split('/')
                                if not any(p in ('epsilon', 'excel') for p in parts):
                                    try:
                                        if os.path.exists(target_file_path):
                                            local_mtime = os.path.getmtime(target_file_path)
                                            remote_mtime = float(val.get('_meta', {}).get('mtime', 0) or 0)
                                            if remote_mtime and local_mtime >= remote_mtime:
                                                logger.debug('[PULL] Skipping unchanged file %s', target_file_path)
                                                continue
                                    except Exception:
                                        pass
                            # If this is a log file, append the content to the existing server log (paste behavior).
                            # For other files, overwrite with the Firebase copy (login-triggered pull should replace locals).
                            if file_name.endswith('.log'):
                                # Ensure directory exists
                                os.makedirs(file_dir, exist_ok=True)
                                with open(target_file_path, 'ab') as fh:
                                    fh.write(blob)
                                logger.info('[PULL] Appended log %s to %s (size: %d bytes, decrypted: %s)',
                                            file_name, file_dir, len(blob), decrypted)
                            else:
                                os.makedirs(file_dir, exist_ok=True)
                                with open(target_file_path, 'wb') as fh:
                                    fh.write(blob)
                                logger.info('[PULL] Wrote (overwrite) file %s to %s (size: %d bytes, decrypted: %s)', 
                                           file_name, file_dir, len(blob), decrypted)
                            
                            # Set mtime if available
                            try:
                                mtime = float(val.get('_meta', {}).get('mtime', 0))
                                if mtime:
                                    os.utime(target_file_path, (mtime, mtime))
                            except Exception:
                                pass
                            
                            files_created += 1
                            # update progress after materializing a file
                            try:
                                processed_files_local = files_created
                                pct = 100 if total_files == 0 else int(min(100, math.floor((processed_files_local / float(total_files)) * 100)))
                                _set_progress('running', pct, f'Pulled {processed_files_local}/{total_files} files')
                            except Exception:
                                pass
                        except Exception as e:
                            files_failed += 1
                            logger.error('[PULL] Failed to materialize file %s: %s', key, e)
                    
                    elif isinstance(val, dict):
                        # This is a nested dict - recurse into it to find files
                        logger.debug('[PULL] Recursing into nested dict at key: %s', key)
                        # Recurse with accumulated path
                        _recursive_process(val, current_path=(current_path + '/' + key).lstrip('/'))
                    
                    else:
                        # Other types - skip
                        logger.debug('[PULL] Skipping non-dict value at key %s', key)
                
                except Exception as e:
                    files_failed += 1
                    logger.error('[PULL] Error processing key %s: %s', key, e)
        
        # Start recursive processing
        _recursive_process(exported, "")

        # After materializing files locally, prune remote duplicated activity/error logs
        try:
            if is_firebase_enabled():
                remote_files = firebase_read_data_compressed(path) or {}
                # Collect candidates for pruning: map filename -> list of (full_key, mtime)
                candidates = {'activity.log': [], 'error.log': []}

                def _collect_log_candidates(obj, current_path=''):
                    if not isinstance(obj, dict):
                        return
                    for key, val in obj.items():
                        full_key = (current_path + '/' + key).lstrip('/').rstrip('/') if current_path else key
                        if isinstance(val, dict) and 'content' in val and '_meta' in val:
                            # Determine the materialized filename
                            fname = _get_file_name_with_extension(key)
                            if fname in candidates:
                                try:
                                    mtime = float(val.get('_meta', {}).get('mtime', 0)) or 0
                                except Exception:
                                    mtime = 0
                                candidates[fname].append((full_key, mtime))
                        elif isinstance(val, dict):
                            _collect_log_candidates(val, current_path=(current_path + '/' + key).lstrip('/'))

                _collect_log_candidates(remote_files, '')

                # For each log type, keep the newest and delete the rest
                for fname, items in candidates.items():
                    if len(items) <= 1:
                        continue
                    # Sort by mtime desc; keep first
                    items.sort(key=lambda x: x[1], reverse=True)
                    to_delete = items[1:]
                    for full_key, _ in to_delete:
                        try:
                            delete_path = f'/groups/{group_name}/files/{full_key}'
                            if firebase_write_data(delete_path, None):
                                logger.info('[PULL] Pruned remote %s (deleted %s)', fname, full_key)
                            else:
                                logger.warning('[PULL] Failed to prune remote %s (keep %s)', fname, full_key)
                        except Exception as e:
                            logger.error('[PULL] Error pruning remote log %s: %s', full_key, e)
        except Exception as e:
            logger.warning('[PULL] Could not prune remote log variants: %s', e)

        # Log summary
        try:
            logger.info('[PULL] Pulled files for group %s: created %d files, %d failed, bytes_downloaded=%d. Stored in: %s', 
                        group_name, files_created, files_failed, bytes_downloaded, target_dir)
            # Record per-pull bytes in activity logs for auditing
            try:
                firebase_log_activity('system', group_name, 'firebase_pull', {'bytes_downloaded': int(bytes_downloaded), 'files_created': int(files_created), 'files_failed': int(files_failed)})
            except Exception:
                logger.debug('Could not write firebase_pull activity entry')
        except Exception:
            pass

        # mark progress as complete (include MB in message)
        try:
            _set_progress('done', 100, f'Completed: {files_created} files (downloaded {round(bytes_downloaded/1024.0/1024.0,2)} MB)')
        except Exception:
            pass
        return True
    
    except Exception as e:
        logger.error('Failed to pull files for group %s: %s', group_name, e)
        return False


def ensure_group_data_local(group_folder: str, create_empty_dirs: bool = True) -> bool:
    """
    Ensure a group's data folder exists locally.
    
    Strategy:
    1. If folder already exists locally, return True immediately (fast path)
    2. If missing, attempt lazy-pull from Firebase
    3. If Firebase pull fails/empty, optionally create empty folder structure
    
    This is the primary entry point for lazy-loading group data.
    Used by routes to ensure data is available before processing.
    
    Args:
        group_folder: The data_folder name (e.g., 'client_xyz')
        create_empty_dirs: If True, create empty folder even if Firebase has no data
    
    Returns:
        True if folder now exists and is accessible (or will be created)
        False only if there's a critical error
    """
    def _start_background_pull(folder):
        try:
            # mark running
            try:
                grp_dir = os.path.join(os.getcwd(), 'data', folder)
                os.makedirs(grp_dir, exist_ok=True)
                prog_file = os.path.join(grp_dir, '.sync_progress.json')
                with open(prog_file, 'w', encoding='utf-8') as fh:
                    json.dump({'status': 'running', 'percent': 0, 'message': 'background pull started'}, fh)
            except Exception:
                pass
            # run pull worker
            firebase_pull_group_to_local(folder, os.path.join(os.getcwd(), 'data'))
        except Exception as e:
            logger.error('Background pull failed for %s: %s', folder, e)

    try:
        if not group_folder:
            logger.warning('ensure_group_data_local: No group_folder provided')
            return False
        
        data_root = os.path.join(os.getcwd(), 'data')
        target_dir = os.path.join(data_root, group_folder)
        
        # Fast path: folder already exists
        if os.path.isdir(target_dir):
            logger.debug('Group data already exists locally: %s', group_folder)
            return True
        
        # Attempt to pull from Firebase (run in background to avoid blocking login)
        logger.info('Group data missing locally, attempting lazy-pull: %s', group_folder)
        # Start background pull to avoid blocking requests (non-blocking behaviour)
        try:
            t = threading.Thread(target=_start_background_pull, args=(group_folder,), daemon=True)
            t.start()
            return True
        except Exception:
            logger.debug('Background pull spawn failed, falling back to synchronous pull for %s', group_folder)

        if firebase_pull_group_to_local(group_folder, data_root):
            # Pull succeeded (either found data or returned without error)
            # Ensure the folder exists (might be empty if Firebase had no data)
            if create_empty_dirs:
                os.makedirs(target_dir, exist_ok=True)
                # Also create common subdirectories proactively
                for subdir in ['epsilon', 'excel', '__pycache__']:
                    try:
                        os.makedirs(os.path.join(target_dir, subdir), exist_ok=True)
                    except Exception:
                        pass
            logger.info('Successfully ensured group data local: %s', group_folder)
            return True
        
        # Pull failed, but if create_empty_dirs is True, create folder anyway
        if create_empty_dirs:
            try:
                os.makedirs(target_dir, exist_ok=True)
                # Create common subdirectories
                for subdir in ['epsilon', 'excel']:
                    os.makedirs(os.path.join(target_dir, subdir), exist_ok=True)
                logger.warning('Created empty group folder (Firebase pull failed): %s', group_folder)
                return True
            except Exception as e:
                logger.error('Failed to create empty group folder: %s (error: %s)', group_folder, e)
                return False
        
        logger.warning('Could not ensure group data local and create_empty_dirs=False: %s', group_folder)
        return False
    
    except Exception as e:
        logger.error('Unexpected error in ensure_group_data_local for %s: %s', group_folder, e)
        return False


def firebase_import_group_data(group_name: str, data: Dict[str, Any]) -> bool:
    """Import data for a group to Firebase"""
    try:
        if not is_firebase_enabled():
            return False
        
        path = f'/groups/{group_name}'
        # Use compressed write to reduce payload size when appropriate
        return firebase_write_compressed(path, data)
    except Exception as e:
        logger.error(f"Failed to import group data for {group_name}: {e}")
        return False


# ============================================================================
# File sync helpers
# ============================================================================

_sync_thread = None
_sync_stop = False
_sync_state_path = os.path.join(os.getcwd(), 'data', '.firebase_sync_state.json')

# Map user_id -> last db write timestamp (epoch seconds)
_user_last_db_activity = {}
# Map user_id -> threading.Timer so we can cancel / reschedule idle syncs
_user_idle_timers = {}

# Idle timeout in seconds (10 minutes default). Can be adjusted by tests/env.
IDLE_SYNC_TIMEOUT = int(os.getenv('FIREBASE_IDLE_SYNC_TIMEOUT', '600'))


def _load_sync_state() -> Dict[str, float]:
    try:
        if os.path.exists(_sync_state_path):
            with open(_sync_state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


# -----------------
# Group sync progress helpers
# -----------------
def _group_progress_path(group_name: str) -> str:
    try:
        grp_dir = os.path.join(os.getcwd(), 'data', group_name)
        os.makedirs(grp_dir, exist_ok=True)
        return os.path.join(grp_dir, '.sync_progress.json')
    except Exception:
        return os.path.join(os.getcwd(), 'data', f'.sync_progress_{group_name}.json')


def set_group_sync_progress(group_name: str, status: str, percent: int = 0, message: str = '') -> None:
    try:
        p = _group_progress_path(group_name)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump({'status': status, 'percent': int(percent or 0), 'message': message}, fh)
    except Exception:
        pass


def get_group_sync_progress(group_name: str) -> Dict[str, Any]:
    try:
        p = _group_progress_path(group_name)
        if not os.path.exists(p):
            return {'status': 'not_started', 'percent': 0, 'message': ''}
        with open(p, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return {
            'status': data.get('status', 'unknown'),
            'percent': int(data.get('percent', 0) or 0),
            'message': data.get('message', '')
        }
    except Exception:
        return {'status': 'error', 'percent': 0, 'message': 'Could not read progress'}


def clear_group_sync_progress(group_name: str) -> None:
    try:
        p = _group_progress_path(group_name)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _save_sync_state(state: Dict[str, float]) -> None:
    try:
        with open(_sync_state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except Exception:
        pass


def firebase_upload_encrypted_file(group_name: str, rel_path: str, file_bytes: bytes, mtime: float) -> bool:
    """Encrypt file bytes and upload to Firebase under /groups/{group_name}/files/{rel_path}

    Stored payload is base64-encoded ciphertext + metadata.
    """
    try:
        if not is_firebase_enabled():
            return False

        key = encryption._ensure_key()
        if not key:
            logger.error('No master encryption key available for file upload')
            return False

        from cryptography.fernet import Fernet
        cipher = Fernet(key)
        encrypted = cipher.encrypt(file_bytes)
        b64 = base64.urlsafe_b64encode(encrypted).decode('utf-8')

        path = f'/groups/{group_name}/files/{rel_path}'
        data = {
            '_meta': {
                'mtime': mtime,
                'size': len(file_bytes)
            },
            'content': b64
        }
        return firebase_write_data(path, data)
    except Exception as e:
        logger.error(f'Failed to upload encrypted file to Firebase: {e}')
        return False


def _scan_and_sync_data_dir(data_dir: str, group_names: List[str] = None) -> None:
    """Scan data_dir and sync changed files to Firebase. Top-level folders are treated as group names.
    If group_names is provided, restrict to those subfolders.
    """
    state = _load_sync_state()
    new_state: Dict[str, float] = {}

    for root, dirs, files in os.walk(data_dir):
        # determine group name: use first path component under data_dir
        rel_root = os.path.relpath(root, data_dir)
        parts = rel_root.split(os.sep)
        if rel_root == '.' or parts[0] == '.':
            group = '__global__'
        else:
            group = parts[0]

        if group_names and group not in group_names:
            continue

        for fname in files:
            # skip dotfiles and internal state files
            if fname.startswith('.') or fname == '.firebase_sync_state.json':
                continue
            full = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(full)
            except Exception:
                continue

            key = os.path.relpath(full, data_dir)
            new_state[key] = mtime

            if state.get(key) == mtime:
                continue

            # file changed -> upload
            try:
                with open(full, 'rb') as f:
                    fb = f.read()
                # Build rel_path relative to the group folder (do not include the group name twice)
                # key is like 'group_name/...' or maybe other; strip the leading group segment if present
                parts = key.split(os.sep)
                rel_parts = parts[1:] if len(parts) > 1 and parts[0] == group else parts
                # If file is directly under group root and is an .xls/.xlsx, place it under 'imports/' in Firebase
                fname = parts[-1]
                _, fext = os.path.splitext(fname)
                if len(parts) == 2 and fext.lower() in ('.xls', '.xlsx'):
                    rel_parts = ['imports', fname]

                rel_path = '/'.join([p for p in rel_parts if p not in ('', '.')])
                rel_path = rel_path.replace('..', '')
                ok = firebase_upload_encrypted_file(group, rel_path, fb, mtime)
                if ok:
                    logger.info(f'Uploaded file to Firebase: {rel_path} (group={group})')
            except Exception as e:
                logger.error(f'Failed to read/upload file {full}: {e}')

    _save_sync_state(new_state)


def _sync_loop(data_dir: str, interval: int = 60):
    """DEPRECATED: Background sync loop disabled to reduce RTDB traffic.
    Sync now happens only on: login (pull), logout (push), and server startup (cleanup).
    """
    global _sync_stop
    logger.info('Ο βρόχος συγχρονισμού είναι απενεργοποιημένος (SYNC_ENABLED=0 από προεπιλογή). Ο συγχρονισμός γίνεται μόνο σύνδεση/αποσύνδεση.')
    return


def start_firebase_data_sync(data_dir: str = None, interval: int = 60) -> None:
    """Start background thread to sync data/ to Firebase periodically.
    Call after Firebase initialization.
    """
    global _sync_thread, _sync_stop
    if not is_firebase_enabled():
        return

    if data_dir is None:
        data_dir = os.path.join(os.getcwd(), 'data')

    # Cleanup any stale per-group .sync_progress.json left by previous runs/crashes
    try:
        if os.path.isdir(data_dir):
            for entry in os.listdir(data_dir):
                grp_dir = os.path.join(data_dir, entry)
                if os.path.isdir(grp_dir):
                    try:
                        clear_group_sync_progress(entry)
                        logger.info('Cleared stale sync progress for group %s', entry)
                    except Exception as e:
                        logger.debug('Failed to clear stale sync progress for %s: %s', entry, e)
    except Exception as e:
        logger.debug('Failed to cleanup stale group sync progress files: %s', e)

    # Background sync thread is now disabled by default.
    # Sync happens only on login (pull), logout (push), and server startup (cleanup).
    # To re-enable: set FIREBASE_SYNC_ENABLED=1 in .env
    sync_enabled = os.getenv('FIREBASE_SYNC_ENABLED', '0') == '1'
    if not sync_enabled:
        logger.info('Ο συγχρονισμός με Firebase απενεργοποιήθηκε (FIREBASE_SYNC_ENABLED δεν ορίστηκε). Συγχρονισμός μόνο σύνδεση/αποσύνδεση.')
        return

    if _sync_thread and _sync_thread.is_alive():
        return

    _sync_stop = False
    _sync_thread = threading.Thread(target=_sync_loop, args=(data_dir, interval), daemon=True)
    _sync_thread.start()
    logger.info('Εκκινήθηκε νήμα συγχρονισμού δεδομένων Firebase (FIREBASE_SYNC_ENABLED=1)')


def firebase_sync_group_folder(group_folder: str, data_dir: str = None) -> bool:
    """Perform immediate sync of a single group folder under `data/`.

    `group_folder` must be the filesystem folder name under `data/` (this is the
    Group.data_folder value). Returns True if the sync ran without fatal errors.
    """
    try:
        if not is_firebase_enabled():
            logger.warning('Firebase not enabled; skipping group sync')
            return False

        if not group_folder:
            logger.warning('No group folder provided for firebase_sync_group_folder')
            return False

        if data_dir is None:
            data_dir = os.path.join(os.getcwd(), 'data')

        # run a single scan for the specified folder
        _scan_and_sync_data_dir(data_dir, group_names=[group_folder])
        logger.info('Completed firebase_sync_group_folder for %s', group_folder)
        return True
    except Exception as e:
        logger.error('Error syncing group folder to Firebase: %s', e)
        return False


def _user_idle_sync_handler(user_id: int, group_folder: str) -> None:
    """Called by timer when a user has been idle long enough to trigger sync."""
    try:
        last_ts = _user_last_db_activity.get(str(user_id) if isinstance(user_id, str) else user_id)
        if not last_ts:
            logger.debug('Idle sync: no last activity record for user %s', user_id)
            return

        # If current time now is at least IDLE_SYNC_TIMEOUT seconds after last activity, proceed
        if time.time() - float(last_ts) >= IDLE_SYNC_TIMEOUT:
            logger.info('User %s idle for >= %s seconds. Syncing group %s', user_id, IDLE_SYNC_TIMEOUT, group_folder)
            # Perform a targeted group sync
            firebase_sync_group_folder(group_folder)
        else:
            logger.debug('Idle sync: user %s not idle anymore (last %s)', user_id, last_ts)
    except Exception as e:
        logger.error('Error during user idle sync handler for %s: %s', user_id, e)
    finally:
        # cleanup timer reference
        try:
            _user_idle_timers.pop(user_id, None)
        except Exception:
            pass


def firebase_record_db_activity(user_id: int, group_folder: str) -> None:
    """Record that a user performed a DB-write action and (re)schedule idle sync.

    When called, this stores a last-activity timestamp and schedules a Timer
    to call `_user_idle_sync_handler` after `IDLE_SYNC_TIMEOUT` seconds. If the
    user performs more DB writes, the timer is reset.
    """
    try:
        if not user_id:
            return

        # Normalize to str key to be safe across sessions
        key = str(user_id)
        _user_last_db_activity[key] = time.time()

        # Cancel previous timer if exists
        prev = _user_idle_timers.get(key)
        if prev and isinstance(prev, threading.Timer):
            try:
                prev.cancel()
            except Exception:
                pass

        # Only schedule if group_folder is provided
        if not group_folder:
            return

        t = threading.Timer(IDLE_SYNC_TIMEOUT, _user_idle_sync_handler, args=(key, group_folder))
        t.daemon = True
        t.start()
        _user_idle_timers[key] = t
        logger.debug('Scheduled idle sync for user %s (group=%s) in %s seconds', user_id, group_folder, IDLE_SYNC_TIMEOUT)
    except Exception as e:
        logger.error('Failed to schedule idle sync for user %s: %s', user_id, e)


def firebase_cancel_idle_sync_for_user(user_id: int) -> None:
    """Cancel a pending idle sync for a user (used on logout)."""
    key = str(user_id)
    t = _user_idle_timers.pop(key, None)
    try:
        if t and isinstance(t, threading.Timer):
            t.cancel()
    except Exception:
        pass
    _user_last_db_activity.pop(key, None)


def stop_firebase_data_sync() -> None:
    global _sync_stop, _sync_thread
    _sync_stop = True
    if _sync_thread:
        _sync_thread.join(timeout=2)


def sync_user_groups_from_firestore(user_id: int, firebase_uid: str = None) -> bool:
    """Sync user's group memberships from Firestore to local SQLite database.
    
    Call this after user login to ensure local DB is in sync with Firestore.
    - Queries Firestore for `/users/{firebase_uid}/groups`
    - Updates local UserGroup table to match
    - Returns True if sync succeeded or Firebase disabled, False on error
    """
    try:
        if not is_firebase_enabled():
            logger.debug('Firebase not enabled; skipping group sync')
            return True
        
        if not firebase_uid:
            logger.warning('No firebase_uid provided for user %s', user_id)
            return False
        
        # Import locally to avoid circular dependency
        from models import db, User, Group, UserGroup
        from flask import current_app
        
        # Get user record
        user = User.query.get(user_id)
        if not user:
            logger.warning('User %s not found in DB', user_id)
            return False
        
        # Query Firestore for user's groups
        try:
            user_profile = firebase_read_data(f'/users/{firebase_uid}')
            firestore_groups = []
            if user_profile and isinstance(user_profile, dict) and 'groups' in user_profile:
                firestore_groups = user_profile.get('groups', [])
            
            logger.info('Χρήστης %s: Βρέθηκαν %d ομάδες στο Firestore: %s', 
                       firebase_uid, len(firestore_groups), firestore_groups)
        except Exception as e:
            logger.error('Failed to query Firestore groups for %s: %s', firebase_uid, e)
            return False
        
        # Sync groups to local DB
        try:
            # Get local Group records by name
            local_groups = {}
            for grp in Group.query.all():
                local_groups[grp.name] = grp
            
            # Add user to groups from Firestore if not already there
            for group_name in firestore_groups:
                if group_name not in local_groups:
                    logger.debug('Group %s not found in local DB (creating)', group_name)
                    # Create group if it doesn't exist (default data_folder = group_name)
                    grp = Group(name=group_name, data_folder=group_name)
                    db.session.add(grp)
                    db.session.flush()
                    local_groups[group_name] = grp
                else:
                    grp = local_groups[group_name]
                
                # Add user to this group if not already a member
                existing_ug = UserGroup.query.filter_by(user_id=user_id, group_id=grp.id).first()
                if not existing_ug:
                    ug = UserGroup(user_id=user_id, group_id=grp.id, role='member')
                    db.session.add(ug)
                    logger.info('Added user %s to group %s', user_id, group_name)
            
            # Optionally: remove user from groups not in Firestore
            # (be careful here - only do if Firestore is source of truth)
            # For now, we'll keep existing local groups to avoid breaking things
            
            db.session.commit()
            logger.info('User %s groups synced successfully', firebase_uid)
            return True
            
        except Exception as e:
            logger.error('Failed to sync groups to local DB for user %s: %s', firebase_uid, e)
            db.session.rollback()
            return False
            
    except Exception as e:
        logger.error('Unexpected error in sync_user_groups_from_firestore: %s', e)
        return False
