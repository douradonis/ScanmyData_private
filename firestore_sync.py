"""
Firestore integration for group membership and role management.
Provides atomic operations and sync with SQLite cache.
"""

import os
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

from activity_monitor import monitor_resources

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from infisical_bootstrap import bootstrap_infisical_secrets
    bootstrap_infisical_secrets(logger=logging.getLogger(__name__))
except Exception:
    pass

# Set GOOGLE_APPLICATION_CREDENTIALS only when explicitly configured
if not os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
    creds_path = os.getenv('FIREBASE_CREDENTIALS_PATH') or ''
    if os.path.exists(creds_path):
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.abspath(creds_path)

try:
    from google.cloud import firestore
    from google.cloud.exceptions import GoogleCloudError, NotFound
    FIRESTORE_AVAILABLE = True
except ImportError:
    FIRESTORE_AVAILABLE = False
    firestore = None

logger = logging.getLogger(__name__)

# Feature flag
FIRESTORE_ENABLED = os.getenv('FIRESTORE_ENABLED', '0').lower() in ('1', 'true', 'yes')

# Firestore client (lazy-loaded)
_fs_client = None


def get_firestore_client():
    """Get or initialize Firestore client."""
    global _fs_client
    if _fs_client is None and FIRESTORE_AVAILABLE and FIRESTORE_ENABLED:
        try:
            _fs_client = firestore.Client()
            logger.info("Firestore client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Firestore client: {e}")
            _fs_client = None
    return _fs_client


@monitor_resources('fs_atomic_add_user_to_group')
def fs_atomic_add_user_to_group(uid: str, group_name: str, role: str = 'member') -> bool:
    """
    Atomically add user to group in Firestore and sync to SQLite.
    
    Args:
        uid: Firebase user ID
        group_name: Name of group
        role: Role in group ('admin' or 'member'), default 'member'
        
    Returns:
        True if successful, False otherwise
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return False
    
    client = get_firestore_client()
    if not client:
        return False
    
    try:
        # Use batch write for atomicity
        batch = client.batch()
        
        # Get user ref and prepare update
        user_ref = client.collection('users').document(uid)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            # Create user doc if not exists
            batch.set(user_ref, {
                'created_at': datetime.now(),
                'groups': {group_name: role},
                'group_roles': {group_name: role}
            })
        else:
            # Update existing user doc
            user_data = user_doc.to_dict()
            groups = user_data.get('groups', {})
            groups[group_name] = role
            
            batch.update(user_ref, {
                'groups': groups,
                'group_roles': groups,
                'updated_at': datetime.now()
            })
        
        # Get group ref and prepare update
        group_ref = client.collection('groups').document(group_name)
        group_doc = group_ref.get()
        
        if not group_doc.exists:
            # Create group doc if not exists
            batch.set(group_ref, {
                'created_at': datetime.now(),
                'members': [uid] if role == 'member' else [],
                'admins': [uid] if role == 'admin' else [],
            })
        else:
            # Update existing group doc
            group_data = group_doc.to_dict()
            members = group_data.get('members', [])
            admins = group_data.get('admins', [])
            
            # Remove from both lists first
            if uid in members:
                members.remove(uid)
            if uid in admins:
                admins.remove(uid)
            
            # Add to appropriate list
            if role == 'admin':
                if uid not in admins:
                    admins.append(uid)
            else:
                if uid not in members:
                    members.append(uid)
            
            batch.update(group_ref, {
                'members': members,
                'admins': admins,
                'updated_at': datetime.now()
            })
        
        # Commit batch atomically
        batch.commit()
        logger.info(f"Added user {uid} to group {group_name} in Firestore")
        return True
        
    except Exception as e:
        logger.error(f"Error adding user {uid} to group {group_name} in Firestore: {e}")
        return False


def fs_atomic_remove_user_from_group(uid: str, group_name: str) -> bool:
    """
    Atomically remove user from group in Firestore.
    
    Args:
        uid: Firebase user ID
        group_name: Name of group
        
    Returns:
        True if successful, False otherwise
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return False
    
    client = get_firestore_client()
    if not client:
        return False
    
    try:
        batch = client.batch()
        
        # Update user doc
        user_ref = client.collection('users').document(uid)
        user_doc = user_ref.get()
        
        if user_doc.exists:
            user_data = user_doc.to_dict()
            groups = user_data.get('groups', {})
            
            if group_name in groups:
                del groups[group_name]
                
            batch.update(user_ref, {
                'groups': groups,
                'group_roles': groups,
                'updated_at': datetime.now()
            })
        
        # Update group doc
        group_ref = client.collection('groups').document(group_name)
        group_doc = group_ref.get()
        
        if group_doc.exists:
            group_data = group_doc.to_dict()
            members = group_data.get('members', [])
            admins = group_data.get('admins', [])
            
            if uid in members:
                members.remove(uid)
            if uid in admins:
                admins.remove(uid)
            
            batch.update(group_ref, {
                'members': members,
                'admins': admins,
                'updated_at': datetime.now()
            })
        
        batch.commit()
        logger.info(f"Removed user {uid} from group {group_name} in Firestore")
        return True
        
    except Exception as e:
        logger.error(f"Error removing user {uid} from group {group_name} in Firestore: {e}")
        return False


def fs_atomic_set_user_group_role(uid: str, group_name: str, role: str) -> bool:
    """
    Atomically update user role in group in Firestore.
    
    Args:
        uid: Firebase user ID
        group_name: Name of group
        role: New role ('admin' or 'member')
        
    Returns:
        True if successful, False otherwise
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return False
    
    client = get_firestore_client()
    if not client:
        return False
    
    try:
        batch = client.batch()
        
        # Update user doc
        user_ref = client.collection('users').document(uid)
        user_doc = user_ref.get()
        
        if user_doc.exists:
            user_data = user_doc.to_dict()
            groups = user_data.get('groups', {})
            
            if group_name in groups:
                groups[group_name] = role
                
                batch.update(user_ref, {
                    'groups': groups,
                    'group_roles': groups,
                    'updated_at': datetime.now()
                })
        
        # Update group doc
        group_ref = client.collection('groups').document(group_name)
        group_doc = group_ref.get()
        
        if group_doc.exists:
            group_data = group_doc.to_dict()
            members = group_data.get('members', [])
            admins = group_data.get('admins', [])
            
            # Remove from both lists
            if uid in members:
                members.remove(uid)
            if uid in admins:
                admins.remove(uid)
            
            # Add to appropriate list
            if role == 'admin':
                if uid not in admins:
                    admins.append(uid)
            else:
                if uid not in members:
                    members.append(uid)
            
            batch.update(group_ref, {
                'members': members,
                'admins': admins,
                'updated_at': datetime.now()
            })
        
        batch.commit()
        logger.info(f"Updated role for user {uid} in group {group_name} in Firestore")
        return True
        
    except Exception as e:
        logger.error(f"Error updating role for user {uid} in group {group_name} in Firestore: {e}")
        return False


@monitor_resources('fs_get_user_groups')
def fs_get_user_groups(uid: str) -> Dict[str, str]:
    """
    Get user's groups and roles from Firestore.
    
    Args:
        uid: Firebase user ID
        
    Returns:
        Dict of {group_name: role}
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return {}
    
    client = get_firestore_client()
    if not client:
        return {}
    
    try:
        user_ref = client.collection('users').document(uid)
        user_doc = user_ref.get()
        
        if user_doc.exists:
            user_data = user_doc.to_dict()
            return user_data.get('groups', {})
        
        return {}
        
    except Exception as e:
        logger.error(f"Error getting groups for user {uid} from Firestore: {e}")
        return {}


def fs_get_group_members(group_name: str) -> Dict[str, List[str]]:
    """
    Get group members and admins from Firestore.
    
    Args:
        group_name: Name of group
        
    Returns:
        Dict with 'members' and 'admins' lists
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return {'members': [], 'admins': []}
    
    client = get_firestore_client()
    if not client:
        return {'members': [], 'admins': []}
    
    try:
        group_ref = client.collection('groups').document(group_name)
        group_doc = group_ref.get()
        
        if group_doc.exists:
            group_data = group_doc.to_dict()
            return {
                'members': group_data.get('members', []),
                'admins': group_data.get('admins', [])
            }
        
        return {'members': [], 'admins': []}
        
    except Exception as e:
        logger.error(f"Error getting members for group {group_name} from Firestore: {e}")
        return {'members': [], 'admins': []}


def fs_get_user_role_in_group(uid: str, group_name: str) -> Optional[str]:
    """
    Get user's role in specific group from Firestore.
    
    Args:
        uid: Firebase user ID
        group_name: Name of group
        
    Returns:
        Role ('admin' or 'member') or None if not in group
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return None
    
    client = get_firestore_client()
    if not client:
        return None
    
    try:
        user_ref = client.collection('users').document(uid)
        user_doc = user_ref.get()
        
        if user_doc.exists:
            user_data = user_doc.to_dict()
            groups = user_data.get('groups', {})
            return groups.get(group_name)
        
        return None
        
    except Exception as e:
        logger.error(f"Error getting role for user {uid} in group {group_name} from Firestore: {e}")
        return None


def fs_delete_group(group_name: str) -> bool:
    """
    Delete an entire group document and remove the group membership/roles
    from all user documents in Firestore.

    Args:
        group_name: Name of group to delete

    Returns:
        True if successful, False otherwise
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        return False

    client = get_firestore_client()
    if not client:
        return False

    try:
        group_ref = client.collection('groups').document(group_name)
        group_doc = group_ref.get()

        if not group_doc.exists:
            # Nothing to delete
            logger.info(f"Group {group_name} does not exist in Firestore; nothing to delete")
            return True

        group_data = group_doc.to_dict() or {}
        member_uids = set(group_data.get('members', []) or [])
        admin_uids = set(group_data.get('admins', []) or [])
        all_uids = list(member_uids.union(admin_uids))

        # Batch operations; respect 500 ops limit per batch
        def commit_batch(batch_ops):
            if batch_ops:
                batch_ops.commit()

        batch = client.batch()
        ops_in_batch = 0

        # For each user, remove group from 'groups' and 'group_roles'
        for uid in all_uids:
            user_ref = client.collection('users').document(uid)
            user_doc = user_ref.get()
            if user_doc.exists:
                user_data = user_doc.to_dict() or {}
                groups = user_data.get('groups', {})
                # groups expected as dict {group: role}; support list just in case
                if isinstance(groups, dict):
                    if group_name in groups:
                        del groups[group_name]
                elif isinstance(groups, list):
                    if group_name in groups:
                        groups.remove(group_name)

                group_roles = user_data.get('group_roles', {})
                if isinstance(group_roles, dict) and group_name in group_roles:
                    del group_roles[group_name]

                batch.update(user_ref, {
                    'groups': groups,
                    'group_roles': group_roles,
                    'updated_at': datetime.now()
                })
                ops_in_batch += 1

                if ops_in_batch >= 450:
                    commit_batch(batch)
                    batch = client.batch()
                    ops_in_batch = 0

        # Finally, delete the group document
        batch.delete(group_ref)
        ops_in_batch += 1

        commit_batch(batch)

        logger.info(f"Deleted group {group_name} and cleaned users in Firestore")
        return True

    except Exception as e:
        logger.error(f"Error deleting group {group_name} in Firestore: {e}")
        return False


def init_firestore_sync(app=None):
    """
    Initialize Firestore sync on app startup.
    Reconciles SQLite cache with Firestore.
    
    Args:
        app: Flask app instance
    """
    if not FIRESTORE_ENABLED or not FIRESTORE_AVAILABLE:
        logger.info("Firestore sync disabled or unavailable")
        return
    
    try:
        from models import db, User, UserGroup
        
        client = get_firestore_client()
        if not client:
            logger.warning("Firestore client not available for sync")
            return
        
        logger.info("Starting Firestore sync...")
        
        # Get all UserGroup records from SQLite
        user_groups = db.session.query(UserGroup).all()
        
        for ug in user_groups:
            user = db.session.query(User).filter_by(id=ug.user_id).first()
            group = db.session.query(db.Model).filter_by(id=ug.group_id).first()
            
            if user and group and user.firebase_uid:
                try:
                    # Sync to Firestore
                    group_name = getattr(group, 'name', None)
                    if group_name:
                        role = ug.role or 'member'
                        fs_atomic_add_user_to_group(user.firebase_uid, group_name, role)
                except Exception as e:
                    logger.error(f"Error syncing user group {ug.id}: {e}")
        
        logger.info("Firestore sync completed")
        
    except Exception as e:
        logger.error(f"Error during Firestore sync initialization: {e}")
