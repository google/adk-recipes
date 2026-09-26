"""
Database layer for user management and session tracking.

Uses Firestore for storing:
- Users: User accounts and profiles
- Sessions: Conversation threads for each user
- Session state is managed by Agent Engine, but we track metadata here

Data Model:
-----------
1. Users can be:
   - Demo users: Pre-seeded accounts with order history (demo@example.com, jane@example.com)
   - New users: Fresh accounts that start with no order history

2. Demo users have known user_ids that link to pre-seeded order/billing data:
   - demo@example.com → user_id: "demo-user-001"
   - jane@example.com → user_id: "demo-user-002"

3. New users get random UUIDs and start with no orders, invoices, etc.
"""

import hashlib
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Dict, List, Optional

from google.api_core import exceptions as gcp_exceptions
from google.api_core import retry
from google.cloud import firestore

from .logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# RETRY CONFIGURATION
# =============================================================================

# Firestore retry policy for transient errors
FIRESTORE_RETRY = retry.Retry(
    initial=0.1,  # Initial delay: 100ms
    maximum=10.0,  # Maximum delay: 10 seconds
    multiplier=2.0,  # Exponential backoff multiplier
    deadline=30.0,  # Total deadline: 30 seconds
    predicate=retry.if_exception_type(
        gcp_exceptions.ServiceUnavailable,  # 503
        gcp_exceptions.DeadlineExceeded,  # 504
        gcp_exceptions.InternalServerError,  # 500
        gcp_exceptions.Aborted,  # 409 (transaction conflict)
    ),
)


def with_retry(func):
    """
    Decorator to add retry logic to database operations.

    Retries on transient Firestore errors with exponential backoff.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        last_exception = None
        for attempt in range(3):  # Max 3 attempts
            try:
                return func(*args, **kwargs)
            except (
                gcp_exceptions.ServiceUnavailable,
                gcp_exceptions.DeadlineExceeded,
                gcp_exceptions.InternalServerError,
                gcp_exceptions.Aborted,
            ) as e:
                last_exception = e
                wait_time = (2**attempt) * 0.1  # 0.1s, 0.2s, 0.4s
                logger.warning(
                    "Firestore operation failed, retrying", attempt=attempt + 1, error=str(e), wait_seconds=wait_time
                )
                time.sleep(wait_time)
            except Exception:
                # Non-retryable error, raise immediately
                raise

        # All retries exhausted
        logger.error("Firestore operation failed after all retries", error=str(last_exception))
        raise last_exception

    return wrapper


# =============================================================================
# DEMO USER CONFIGURATION
# =============================================================================
# These must match the fixture data in customer_support_mas/database/fixtures.py

DEMO_USERS = {
    "demo@example.com": {
        "user_id": "demo-user-001",
        "name": "Demo User",
        "tier": "Gold",
        "password_hash": hashlib.sha256("demo123".encode()).hexdigest(),
    },
    "jane@example.com": {
        "user_id": "demo-user-002",
        "name": "Jane Smith",
        "tier": "Silver",
        "password_hash": hashlib.sha256("jane123".encode()).hexdigest(),
    },
}


def is_demo_email(email: str) -> bool:
    """Check if an email belongs to a demo account."""
    return email.lower() in DEMO_USERS


def get_demo_user_id(email: str) -> Optional[str]:
    """Get the pre-seeded user_id for a demo email."""
    demo = DEMO_USERS.get(email.lower())
    return demo["user_id"] if demo else None


class Database:
    def __init__(self, project_id: str, database_id: str):
        """Initialize Firestore database client."""
        self.db = firestore.Client(project=project_id, database=database_id)
        logger.info(f"Connected to Firestore: {project_id}/{database_id}")

    # =========================================================================
    # USER MANAGEMENT
    # =========================================================================

    @with_retry
    def create_user(self, email: str, name: str, password_hash: str) -> str:
        """
        Create a new user account.

        IMPORTANT: Demo user emails (demo@example.com, jane@example.com) are
        pre-seeded and cannot be registered again. Users should log in with
        these accounts instead.

        Args:
            email: User email (unique identifier)
            name: User display name
            password_hash: Hashed password (use bcrypt in production)

        Returns:
            user_id: Generated user ID

        Raises:
            ValueError: If email belongs to a demo account
        """
        # Check if this is a demo email - demo users are pre-seeded
        if is_demo_email(email):
            logger.warning(f"Attempted to register demo email: {email}")
            raise ValueError(
                "This email is reserved for demo purposes. "
                "Please log in with password 'demo123' or 'jane123' instead."
            )

        # Check if user already exists
        existing = self.get_user_by_email(email)
        if existing:
            logger.warning(f"User already exists: {email}")
            raise ValueError("An account with this email already exists.")

        # Generate new user ID for non-demo users
        user_id = str(uuid.uuid4())

        user_data = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "password_hash": password_hash,
            "created_at": datetime.now(timezone.utc),
            "last_login": None,
            "is_demo": False,  # Mark as non-demo user
        }

        self.db.collection("users").document(user_id).set(user_data)
        logger.info(f"Created user: {user_id} ({email})")

        return user_id

    @with_retry
    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """
        Get user by email address.

        This method checks Firestore for the user. Demo users should be
        pre-seeded in the database with known user_ids that link to
        their order/billing data.

        Args:
            email: User email

        Returns:
            User data dict or None if not found
        """
        email_lower = email.lower()

        # First, try to find in Firestore (works for both demo and regular users)
        query = self.db.collection("users").where("email", "==", email_lower).limit(1)
        results = list(query.stream())

        if results:
            user_data = results[0].to_dict()
            logger.info(f"Found user: {user_data['user_id']} ({email})")
            return user_data

        # Also check original case (for backwards compatibility)
        if email != email_lower:
            query = self.db.collection("users").where("email", "==", email).limit(1)
            results = list(query.stream())
            if results:
                user_data = results[0].to_dict()
                logger.info(f"Found user: {user_data['user_id']} ({email})")
                return user_data

        # If this is a demo email but not found in DB, the seed hasn't run
        if is_demo_email(email):
            logger.warning(
                f"Demo user {email} not found in database. "
                f"Run the seed script: python -m customer_support_mas.database.fixtures --project YOUR_PROJECT"
            )

        logger.info(f"User not found: {email}")
        return None

    @with_retry
    def get_user(self, user_id: str) -> Optional[Dict]:
        """
        Get user by user_id.

        Args:
            user_id: User ID

        Returns:
            User data dict or None if not found
        """
        doc = self.db.collection("users").document(user_id).get()

        if doc.exists:
            return doc.to_dict()

        return None

    @with_retry
    def update_last_login(self, user_id: str):
        """Update user's last login timestamp."""
        self.db.collection("users").document(user_id).update({"last_login": datetime.now(timezone.utc)})

    @with_retry
    def create_anonymous_user(self) -> str:
        """
        Create an anonymous user (for users who don't register).

        Returns:
            user_id: Generated anonymous user ID
        """
        user_id = f"anon-{uuid.uuid4()}"

        user_data = {
            "user_id": user_id,
            "is_anonymous": True,
            "created_at": datetime.now(timezone.utc),
        }

        self.db.collection("users").document(user_id).set(user_data)
        logger.info(f"Created anonymous user: {user_id}")

        return user_id

    # =========================================================================
    # SESSION MANAGEMENT
    # =========================================================================

    @with_retry
    def create_session(self, user_id: str, agent_engine_session_id: str, session_name: Optional[str] = None) -> str:
        """
        Create a new conversation session for a user.

        Args:
            user_id: User ID who owns this session
            agent_engine_session_id: The session ID from Agent Engine
            session_name: Optional name for the session

        Returns:
            session_id: Our internal session ID
        """
        session_id = str(uuid.uuid4())

        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "agent_engine_session_id": agent_engine_session_id,
            "session_name": session_name or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "message_count": 0,
            "is_active": True,
        }

        self.db.collection("sessions").document(session_id).set(session_data)
        logger.info(f"Created session: {session_id} for user: {user_id}")

        return session_id

    @with_retry
    def get_session(self, session_id: str) -> Optional[Dict]:
        """
        Get session by session_id.

        Args:
            session_id: Session ID

        Returns:
            Session data dict or None if not found
        """
        doc = self.db.collection("sessions").document(session_id).get()

        if doc.exists:
            return doc.to_dict()

        return None

    @with_retry
    def get_user_sessions(self, user_id: str, limit: int = 20) -> List[Dict]:
        """
        Get all sessions for a user.

        Args:
            user_id: User ID
            limit: Maximum number of sessions to return

        Returns:
            List of session data dicts, ordered by updated_at desc
        """
        # Simplified query - only filter by user_id to avoid composite index requirement
        # We'll sort and filter active sessions in Python
        query = (
            self.db.collection("sessions")
            .where("user_id", "==", user_id)
            .limit(limit * 2)  # Get more to account for inactive sessions
        )

        sessions = [doc.to_dict() for doc in query.stream()]

        # Filter for active sessions and sort by updated_at
        active_sessions = [s for s in sessions if s.get("is_active", True)]
        active_sessions.sort(key=lambda x: x.get("updated_at", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

        # Limit to requested number
        result = active_sessions[:limit]

        logger.info(f"Found {len(result)} active sessions for user: {user_id}")

        return result

    @with_retry
    def update_session(self, session_id: str):
        """
        Update session's updated_at timestamp and increment message count.

        Args:
            session_id: Session ID
        """
        self.db.collection("sessions").document(session_id).update(
            {
                "updated_at": datetime.now(timezone.utc),
                "message_count": firestore.Increment(1),
            }
        )

    @with_retry
    def rename_session(self, session_id: str, new_name: str):
        """
        Rename a session.

        Args:
            session_id: Session ID
            new_name: New session name
        """
        self.db.collection("sessions").document(session_id).update(
            {
                "session_name": new_name,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        logger.info(f"Renamed session {session_id} to: {new_name}")

    @with_retry
    def delete_session(self, session_id: str):
        """
        Mark session as inactive (soft delete).

        Args:
            session_id: Session ID
        """
        self.db.collection("sessions").document(session_id).update(
            {
                "is_active": False,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        logger.info(f"Deleted session: {session_id}")

    # =========================================================================
    # TOKEN MANAGEMENT
    # =========================================================================

    @with_retry
    def create_token(self, user_id: str) -> str:
        """Generate and persist an auth token for user_id. Returns the token."""
        import secrets
        from datetime import timedelta

        token = secrets.token_urlsafe(32)
        self.db.collection("tokens").document(token).set(
            {
                "user_id": user_id,
                "created_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            }
        )
        logger.info("Token created", user_id=user_id)
        return token

    @with_retry
    def verify_token(self, token: str) -> Optional[str]:
        """Return user_id for a valid, non-expired token, or None."""
        doc = self.db.collection("tokens").document(token).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if datetime.now(timezone.utc) > data["expires_at"]:
            self.db.collection("tokens").document(token).delete()
            logger.info("Token expired and deleted")
            return None
        return data["user_id"]

    @with_retry
    def revoke_token(self, token: str):
        """Delete a token (logout)."""
        self.db.collection("tokens").document(token).delete()
        logger.info("Token revoked")

    # =========================================================================
    # MESSAGE MANAGEMENT
    # =========================================================================

    @with_retry
    def save_message(self, session_id: str, role: str, content: str, message_id: Optional[str] = None) -> str:
        """
        Save a message to a session.

        Args:
            session_id: Session ID
            role: Message role ('user' or 'assistant')
            content: Message content
            message_id: Optional custom message ID

        Returns:
            message_id: The message ID
        """
        if not message_id:
            message_id = str(uuid.uuid4())

        message_data = {
            "message_id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc),
        }

        # Store in subcollection: sessions/{session_id}/messages/{message_id}
        self.db.collection("sessions").document(session_id).collection("messages").document(message_id).set(
            message_data
        )

        logger.info(f"Saved {role} message to session {session_id}")

        return message_id

    @with_retry
    def get_session_messages(self, session_id: str, limit: int = 100) -> List[Dict]:
        """
        Get all messages for a session.

        Args:
            session_id: Session ID
            limit: Maximum number of messages to return

        Returns:
            List of message dicts, ordered by timestamp asc
        """
        query = (
            self.db.collection("sessions")
            .document(session_id)
            .collection("messages")
            .order_by("timestamp")
            .limit(limit)
        )

        messages = [doc.to_dict() for doc in query.stream()]

        logger.info(f"Retrieved {len(messages)} messages for session {session_id}")

        return messages


# Global database instance
_db_instance: Optional[Database] = None


def get_database(project_id: str, database_id: str) -> Database:
    """Get or create global database instance."""
    global _db_instance

    if _db_instance is None:
        _db_instance = Database(project_id, database_id)

    return _db_instance
