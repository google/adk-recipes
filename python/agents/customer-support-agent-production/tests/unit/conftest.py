"""
Pytest configuration for CI/CD unit tests.

Uses Vertex AI Gemini API for LLM calls (NOT Agent Engine).
Runs agent locally via AgentEvaluator.
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

# Add project root to path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="session", autouse=True)
def register_custom_eval_metrics():
    """Register custom eval metrics with ADK's DEFAULT_METRIC_EVALUATOR_REGISTRY.

    EvalConfig.custom_metrics sets custom_function_path on EvalMetric, but
    get_evaluator() still requires the metric name to exist in the registry
    (mapped to _CustomMetricEvaluator). This fixture registers our custom
    metrics once per session before any test runs.
    """
    from google.adk.evaluation.custom_metric_evaluator import _CustomMetricEvaluator
    from google.adk.evaluation.eval_metrics import Interval, MetricInfo, MetricValueInfo
    from google.adk.evaluation.metric_evaluator_registry import DEFAULT_METRIC_EVALUATOR_REGISTRY

    DEFAULT_METRIC_EVALUATOR_REGISTRY.register_evaluator(
        metric_info=MetricInfo(
            metric_name="tool_name_f1",
            description=(
                "F1 score on tool names only (ignores argument values). "
                "precision = |actual ∩ expected| / |actual|, "
                "recall = |actual ∩ expected| / |expected|."
            ),
            metric_value_info=MetricValueInfo(interval=Interval(min_value=0.0, max_value=1.0)),
        ),
        evaluator=_CustomMetricEvaluator,
    )
    logger.info("[EVAL] Registered custom metric: tool_name_f1")


@pytest.fixture(scope="session", autouse=True)
def ci_environment_setup():
    """
    Setup for CI environment - Uses Vertex AI Gemini API (NOT Agent Engine).

    This fixture:
    1. Loads .env for GCP credentials
    2. Initializes Vertex AI for Gemini API calls
    3. Does NOT use Agent Engine deployed resource
    """
    logger.info("[CI SETUP] Loading environment for CI/CD testing...")

    # Load .env from project root
    dotenv_path = os.path.join(ROOT, ".env")
    if os.path.exists(dotenv_path):
        dotenv.load_dotenv(dotenv_path)
        logger.info("[CI SETUP] Loaded .env from %s", dotenv_path)

    # Configuration
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

    if not PROJECT_ID:
        pytest.skip("GOOGLE_CLOUD_PROJECT not set - skipping CI tests")

    logger.info("[CI SETUP] Initializing Vertex AI Gemini API...")
    logger.info("  Project: %s", PROJECT_ID)
    logger.info("  Location: %s", LOCATION)
    logger.info("  Mode: Local agent execution (NOT Agent Engine)")

    # Initialize Vertex AI for Gemini API calls only
    vertexai.init(
        project=PROJECT_ID,
        location=LOCATION,
    )

    # Ensure we're using Vertex AI for Gemini
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

    logger.info("[CI SETUP] Vertex AI Gemini API ready")

    yield

    logger.info("[CI TEARDOWN] CI test session complete")


@pytest.fixture(autouse=True)
def mock_backends():
    """Apply mock Firestore + RAG backends for agent evaluation re-runs.

    The eval datasets were generated with mocked backends (MockFirestoreClient
    and MockRAGProductSearch). The AgentEvaluator re-runs the agent during
    evaluation, so the same mocks must be active for tool trajectories to match.

    datetime.now() is frozen to _FROZEN_DATE in both seed.py and workflow_tools.py
    so that:
    - MockFirestoreClient dates (_days_ago calls) are deterministic across CI runs
    - Refund eligibility calculations see the same "today" as the mock data
    This ensures the mock and real Firestore agree on dates when seeded on the
    same day, while keeping CI results stable regardless of when tests run.
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

    backend_patches = [
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
    for p in backend_patches:
        p.start()

    yield

    for p in backend_patches + datetime_patches:
        p.stop()
