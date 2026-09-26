"""
Authentication — password hashing only.

Token storage is handled by Database.create_token / verify_token / revoke_token
(Firestore-backed, safe for multi-instance Cloud Run deployments).
"""

import logging

import bcrypt

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt with automatic salt generation."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError) as e:
        logger.warning(f"Password verification failed: {e}")
        return False
