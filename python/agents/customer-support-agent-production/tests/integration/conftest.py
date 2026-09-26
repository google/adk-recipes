"""
Pytest configuration for integration tests.
Uses Vertex AI Gemini API (NOT Agent Engine).
Mock backends are applied so evaluation re-runs see the same data
that was used to generate the eval datasets.
"""

import logging
import os
import sys
from datetime import datetime as real_datetime
from unittest.mock import patch

import dotenv
import pytest
import vertexai

# =============================================================================
# FROZEN REFERENCE DATE
# =============================================================================
# All test code uses this as "today". Chosen so that:
#   ORD-67890 delivered _days_ago(5)  → 5 days ago  → ELIGIBLE  (within 30-day window)
#   ORD-11111 delivered _days_ago(45) → 45 days ago → NOT ELIGIBLE (past 30-day window)
#
# Real Firestore seeding (seed_firestore.py) still uses actual datetime.now()
# so the demo always shows a recently delivered order. Tests use the frozen date
# to keep eval golden responses stable across CI runs.
_FROZEN_DATE = real_datetime(2026, 1, 15)


class _FrozenDatetime(real_datetime):
    """datetime subclass that returns _FROZEN_DATE from now()."""

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_DATE


logger = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="session", autouse=True)
def integration_environment_setup():
    """Setup for integration tests - Uses Vertex AI Gemini API."""
    logger.info("[INTEGRATION SETUP] Loading environment...")

    dotenv_path = os.path.join(ROOT, ".env")
    if os.path.exists(dotenv_path):
        dotenv.load_dotenv(dotenv_path)

    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

    if not PROJECT_ID:
        pytest.skip("GOOGLE_CLOUD_PROJECT not set")

    vertexai.init(project=PROJECT_ID, location=LOCATION)
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

    logger.info("[INTEGRATION SETUP] Project: %s, Location: %s", PROJECT_ID, LOCATION)

    yield

    logger.info("[INTEGRATION TEARDOWN] Complete")


@pytest.fixture(autouse=True)
def mock_backends():
    """Apply mock Firestore + RAG backends for agent evaluation re-runs.

    The eval datasets were generated with mocked backends (MockFirestoreClient
    and MockRAGProductSearch). The AgentEvaluator re-runs the agent during
    evaluation, so the same mocks must be active for tool trajectories to match.
    """
    from tests.mock_firestore import MockFirestoreClient
    from tests.mock_rag_search import MockRAGProductSearch

    # Freeze datetime BEFORE instantiating MockFirestoreClient so that
    # _days_ago() in seed.py computes from _FROZEN_DATE, not real today.
    datetime_patches = [
        patch("customer_support_mas.database.fixtures.datetime", _FrozenDatetime),
        patch("customer_support_mas.agents.refund.tools.datetime", _FrozenDatetime),
    ]
    for p in datetime_patches:
        p.start()

    mock_db = MockFirestoreClient()
    mock_rag = MockRAGProductSearch()

    patches = [
        patch("customer_support_mas.database.db_client", mock_db),
        patch("customer_support_mas.database.get_db_client", return_value=mock_db),
        patch("customer_support_mas.database.client.get_db_client", return_value=mock_db),
        patch("customer_support_mas.database.client.db_client", mock_db),
        patch("customer_support_mas.agents.product.tools.db_client", mock_db),
        patch("customer_support_mas.agents.order.tools.db_client", mock_db),
        patch("customer_support_mas.agents.billing.tools.db_client", mock_db),
        patch("customer_support_mas.agents.refund.tools.db_client", mock_db),
        patch("customer_support_mas.services.rag_search.RAGProductSearch", MockRAGProductSearch),
        patch("customer_support_mas.services.rag_search._rag_search", mock_rag),
        patch("customer_support_mas.services.rag_search.get_rag_search", return_value=mock_rag),
        patch("customer_support_mas.services.get_rag_search", return_value=mock_rag),
        patch("customer_support_mas.agents.product.tools.get_rag_search", return_value=mock_rag),
        patch("customer_support_mas.agents.product.tools.USE_RAG", True),
    ]

    for p in patches:
        p.start()

    yield

    for p in patches + datetime_patches:
        p.stop()
