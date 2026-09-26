"""
Database Client Configuration
===============================
Sets up Firestore database client for the customer support system.

Uses lazy initialization to ensure environment variables are loaded
before creating the Firestore client. This is critical for Agent Engine
deployments where env vars may not be available at module import time.
"""

import logging
import os

from dotenv import load_dotenv
from google.cloud import firestore

logger = logging.getLogger(__name__)

# Load environment variables from .env file (local development only)
load_dotenv()

# =============================================================================
# DATABASE CONFIGURATION (LAZY INITIALIZATION)
# =============================================================================

_db_client = None


def get_db_client() -> firestore.Client:
    """
    Get or create Firestore client (lazy initialization).

    This ensures the client is created AFTER environment variables are loaded,
    which is critical for Agent Engine deployments.
    """
    global _db_client
    if _db_client is None:
        database_id = os.getenv("FIRESTORE_DATABASE", "customer-support-db")
        _db_client = firestore.Client(database=database_id)
        logger.debug("Initialized Firestore client for database: %s", database_id)
    return _db_client


# For backward compatibility - creates a proxy that lazily initializes
class _LazyDbClient:
    """Proxy class for lazy Firestore client initialization."""

    def __getattr__(self, name):
        return getattr(get_db_client(), name)


db_client = _LazyDbClient()
