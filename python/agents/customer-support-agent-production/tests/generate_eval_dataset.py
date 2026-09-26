#!/usr/bin/env python3
"""
Generate ADK evaluation datasets by running the actual agent.

Replaces the manual ADK web UI workflow. Queries the real agent, captures
tool call traces, and outputs .test.json files in the exact ADK eval
dashboard format (with invocation_events).

Usage:
    python tests/generate_eval_dataset.py                            # all agents
    python tests/generate_eval_dataset.py --agent product            # product only
    python tests/generate_eval_dataset.py --agent product --dry-run  # preview cases
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Load environment
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vertex AI init (required before importing agents)
# ---------------------------------------------------------------------------
import vertexai  # noqa: E402

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
if not PROJECT_ID:
    sys.exit("ERROR: GOOGLE_CLOUD_PROJECT env var not set")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
vertexai.init(project=PROJECT_ID, location=LOCATION)


# =============================================================================
# TEST CASE DEFINITIONS (from tests/GOLDEN_DATASETS.md)
# =============================================================================

PRODUCT_AGENT_CASES = [
    # --- Search Products ---
    {"eval_id": "search_laptops", "prompt": "Show me laptops"},
    {"eval_id": "search_keyboards", "prompt": "What gaming keyboards do you have?"},
    {"eval_id": "search_chairs", "prompt": "Show me office chairs"},
    {"eval_id": "search_headphones", "prompt": "I need wireless headphones"},
    # --- Search with Price Filter ---
    {"eval_id": "search_under_500", "prompt": "Show me products under $500"},
    {"eval_id": "search_under_200", "prompt": "What can I get for under $200?"},
    {"eval_id": "search_under_1000", "prompt": "Laptops under $1000"},
    {"eval_id": "search_no_results", "prompt": "Show me products under $100"},
    # --- Product Details ---
    {"eval_id": "details_prod001", "prompt": "Tell me about product PROD-001"},
    {"eval_id": "details_prod002", "prompt": "Details for PROD-002"},
    {"eval_id": "details_invalid", "prompt": "Tell me about product PROD-999"},
    # --- Inventory Check ---
    {"eval_id": "inventory_prod001", "prompt": "Is the ProBook laptop in stock?"},
    {"eval_id": "inventory_prod003", "prompt": "How many gaming keyboards are available?"},
    # --- Product Reviews ---
    {"eval_id": "reviews_prod001", "prompt": "Show me reviews for the ProBook laptop"},
]

ORDER_AGENT_CASES = [
    # --- Track Order (demo-user-001) ---
    {"eval_id": "track_in_transit", "prompt": "Where is my order ORD-12345?", "user_id": "demo-user-001"},
    {"eval_id": "track_delivered", "prompt": "What's the status of order ORD-67890?", "user_id": "demo-user-001"},
    {"eval_id": "track_old_order", "prompt": "Track order ORD-11111", "user_id": "demo-user-001"},
    # --- Track Order (demo-user-002) ---
    {"eval_id": "track_processing", "prompt": "Track order ORD-22222", "user_id": "demo-user-002"},
    # --- Invalid / Unauthorized ---
    {"eval_id": "track_not_found", "prompt": "Track order ORD-99999", "user_id": "demo-user-001"},
    {"eval_id": "track_unauthorized", "prompt": "Track order ORD-22222", "user_id": "demo-user-001"},
    {"eval_id": "track_unauthorized_user2", "prompt": "Track order ORD-12345", "user_id": "demo-user-002"},
    # --- Order History ---
    {"eval_id": "order_history", "prompt": "Show me my recent orders", "user_id": "demo-user-001"},
    {"eval_id": "order_history_alt", "prompt": "What have I ordered?", "user_id": "demo-user-001"},
    {"eval_id": "order_history_user2", "prompt": "Show me my orders", "user_id": "demo-user-002"},
]

BILLING_AGENT_CASES = [
    # --- Invoice by ID ---
    {"eval_id": "invoice_by_id_001", "prompt": "Show me invoice INV-2025-001", "user_id": "demo-user-001"},
    {"eval_id": "invoice_by_id_002", "prompt": "Get invoice INV-2025-002", "user_id": "demo-user-001"},
    {"eval_id": "invoice_not_found", "prompt": "Show me invoice INV-9999", "user_id": "demo-user-001"},
    {"eval_id": "invoice_unauthorized", "prompt": "Show me invoice INV-2025-004", "user_id": "demo-user-001"},
    # --- Invoice by Order ---
    {
        "eval_id": "invoice_by_order_12345",
        "prompt": "What's the invoice for order ORD-12345?",
        "user_id": "demo-user-001",
    },
    {"eval_id": "invoice_by_order_67890", "prompt": "Invoice for order ORD-67890", "user_id": "demo-user-001"},
    {"eval_id": "invoice_by_order_unauth", "prompt": "Invoice for order ORD-22222", "user_id": "demo-user-001"},
    # --- Payment Status ---
    {
        "eval_id": "payment_status_12345",
        "prompt": "Has my payment for order ORD-12345 been processed?",
        "user_id": "demo-user-001",
    },
    {"eval_id": "payment_status_67890", "prompt": "Payment status for ORD-67890", "user_id": "demo-user-001"},
    # --- My Invoices / Payments ---
    {"eval_id": "my_invoices", "prompt": "Show me all my invoices", "user_id": "demo-user-001"},
    {"eval_id": "my_payments", "prompt": "Show me my payment history", "user_id": "demo-user-001"},
    {"eval_id": "my_invoices_user2", "prompt": "Show me all my invoices", "user_id": "demo-user-002"},
]

# --- Section 1.4: Refund Eligibility (check_if_refundable only) ---
REFUND_ELIGIBILITY_CASES = [
    {"eval_id": "check_eligible", "prompt": "I want a refund for order ORD-67890", "user_id": "demo-user-001"},
    {"eval_id": "check_past_window", "prompt": "I want a refund for order ORD-11111", "user_id": "demo-user-001"},
    {"eval_id": "check_not_delivered", "prompt": "Refund order ORD-12345", "user_id": "demo-user-001"},
    {"eval_id": "check_not_found", "prompt": "Refund order ORD-99999", "user_id": "demo-user-001"},
    {"eval_id": "check_unauthorized", "prompt": "Refund order ORD-22222", "user_id": "demo-user-001"},
]

# --- Section 1.5: Error Handling (root agent, no tool calls expected) ---
ERROR_HANDLING_CASES = [
    # Out of scope
    {"eval_id": "out_of_scope_weather", "prompt": "What's the weather today?"},
    {"eval_id": "out_of_scope_joke", "prompt": "Tell me a joke"},
    {"eval_id": "out_of_scope_general", "prompt": "What's the capital of France?"},
    # Ambiguous requests
    {"eval_id": "ambiguous_details", "prompt": "Give me details"},
    {"eval_id": "ambiguous_refund", "prompt": "I want a refund"},
    {"eval_id": "ambiguous_track", "prompt": "Track it"},
]

# --- Section 1.6: Authorization (cross-user access) ---
AUTH_USER1_CASES = [
    # demo-user-001 accessing demo-user-002 resources
    {"eval_id": "auth_track_other", "prompt": "Track order ORD-22222", "user_id": "demo-user-001"},
    {"eval_id": "auth_refund_other", "prompt": "Refund order ORD-22222", "user_id": "demo-user-001"},
    {"eval_id": "auth_invoice_other", "prompt": "Invoice for ORD-22222", "user_id": "demo-user-001"},
]

AUTH_USER2_CASES = [
    # demo-user-002 accessing demo-user-001 resources
    {"eval_id": "auth_track_other_user2", "prompt": "Track order ORD-12345", "user_id": "demo-user-002"},
    {"eval_id": "auth_refund_other_user2", "prompt": "Refund order ORD-67890", "user_id": "demo-user-002"},
]

AGENT_REGISTRY = {
    "product": {
        "cases": PRODUCT_AGENT_CASES,
        "eval_set_id": "product_agent_direct_tests",
        "output_file": "tests/unit/product_agent_direct.test.json",
        "default_user_id": "demo-user-001",
        "agent_import": "customer_support_mas.agents.product.agent",
        "agent_attr": "product_agent",
    },
    "order": {
        "cases": ORDER_AGENT_CASES,
        "eval_set_id": "order_agent_direct_tests",
        "output_file": "tests/unit/order_agent_direct.test.json",
        "default_user_id": "demo-user-001",
        "agent_import": "customer_support_mas.agents.order.agent",
        "agent_attr": "order_agent",
    },
    "billing": {
        "cases": BILLING_AGENT_CASES,
        "eval_set_id": "billing_agent_direct_tests",
        "output_file": "tests/unit/billing_direct.test.json",
        "default_user_id": "demo-user-001",
        "agent_import": "customer_support_mas.agents.billing.agent",
        "agent_attr": "billing_agent",
    },
    "refund": {
        "cases": REFUND_ELIGIBILITY_CASES,
        "eval_set_id": "refund_eligibility_tests",
        "output_file": "tests/unit/refund_workflow_direct.test.json",
        "default_user_id": "demo-user-001",
        "agent_import": "eval_wrappers.test_refund_eligibility",
        "agent_attr": "agent",
    },
    "error": {
        "cases": ERROR_HANDLING_CASES,
        "eval_set_id": "error_handling_tests",
        "output_file": "tests/unit/cases/out_of_scope.test.json",
        "default_user_id": "demo-user-001",
        "agent_import": "eval_wrappers.test_root_agent",
        "agent_attr": "agent",
    },
    "auth_user1": {
        "cases": AUTH_USER1_CASES,
        "eval_set_id": "authorization_cross_user_tests",
        "output_file": "tests/unit/cases/authorization_cross_user.test.json",
        "default_user_id": "demo-user-001",
        "agent_import": "eval_wrappers.test_root_agent",
        "agent_attr": "agent",
    },
    "auth_user2": {
        "cases": AUTH_USER2_CASES,
        "eval_set_id": "demo_user_002_tests",
        "output_file": "tests/unit/cases/demo_user_002.test.json",
        "default_user_id": "demo-user-002",
        "agent_import": "eval_wrappers.test_root_agent",
        "agent_attr": "agent",
    },
}


# =============================================================================
# MOCK SETUP (reuses tests/mock_firestore.py and tests/mock_rag_search.py)
# =============================================================================


def apply_mocks():
    """Apply the same Firestore + RAG mocks used by the test suite.

    Returns a context manager that keeps the patches active.
    datetime.now() is frozen to FROZEN_DATE so generated golden responses
    contain the same dates that CI tests will see (via conftest._FrozenDatetime).
    """
    from datetime import datetime as real_datetime

    from tests.mock_firestore import MockFirestoreClient
    from tests.mock_rag_search import MockRAGProductSearch

    FROZEN_DATE = real_datetime(2026, 1, 15)

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_DATE

    # Freeze datetime BEFORE instantiating MockFirestoreClient so that
    # _days_ago() in seed.py computes from FROZEN_DATE, not real today.
    datetime_patches = [
        patch("customer_support_mas.database.fixtures.datetime", _FrozenDatetime),
        patch("customer_support_mas.agents.refund.tools.datetime", _FrozenDatetime),
    ]
    for p in datetime_patches:
        p.start()

    mock_db = MockFirestoreClient()
    mock_rag = MockRAGProductSearch()

    # Exact same patches as tests/conftest.py
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

    class MockContext:
        def __enter__(self):
            for p in patches:
                p.start()
            return self

        def __exit__(self, *args):
            for p in patches + datetime_patches:
                p.stop()

    return MockContext()


# =============================================================================
# CORE: Run a single eval case and capture events
# =============================================================================

MAX_RETRIES = 4
RETRY_BASE_DELAY = 15  # seconds


async def run_eval_case(agent, case: dict, default_user_id: str, app_name: str):
    """Run a single test case against the agent and capture the full trace.

    Retries on 429 rate-limit errors with exponential backoff.
    Returns an Invocation pydantic model matching the ADK eval format.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    prompt = case["prompt"]
    user_id = case.get("user_id", default_user_id)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            runner = InMemoryRunner(agent=agent, app_name=app_name)

            # Create a fresh session per test case
            session = await runner.session_service.create_session(
                app_name=app_name,
                user_id=user_id,
            )

            # Send the prompt and collect all events
            all_events = []
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=types.Content(
                    parts=[types.Part(text=prompt)],
                    role="user",
                ),
            ):
                all_events.append(event)

            break  # Success — exit retry loop

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "  ⚠ Rate limited (attempt %d/%d). Retrying in %ds...",
                        attempt,
                        MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("  ✗ Rate limited after %d attempts. Skipping.", MAX_RETRIES)
                    raise
            else:
                raise

    # ---- Extract trace components ----
    from google.adk.evaluation.eval_case import Invocation, InvocationEvent, InvocationEvents

    invocation_id = all_events[0].invocation_id if all_events else str(uuid.uuid4())

    # Final response: the last event flagged as final
    final_response_content = None
    for event in reversed(all_events):
        if event.is_final_response() and event.content:
            final_response_content = event.content
            break

    # Build InvocationEvent pydantic objects from captured events
    inv_events = []
    for event in all_events:
        if event.author == "user":
            continue
        if not event.content or not event.content.parts:
            continue
        if event.partial:
            continue
        if event.is_final_response():
            continue
        inv_events.append(
            InvocationEvent(
                author=event.author,
                content=event.content,
            )
        )

    # ---- Log to console ----
    tool_calls = []
    for event in all_events:
        for fc in event.get_function_calls():
            tool_calls.append(fc)

    for fc in tool_calls:
        logger.info("  \u2192 Tool: %s(%s)", fc.name, _format_args(fc.args))
    for event in all_events:
        for fr in event.get_function_responses():
            resp_str = str(fr.response)[:80] if fr.response else "None"
            logger.info("  \u2190 Response: %s", resp_str)

    final_text = _extract_text(final_response_content) if final_response_content else "(no response)"
    logger.info("  \u2713 Final: %s", final_text[:120])

    # ---- Build Invocation pydantic model ----
    invocation = Invocation(
        invocation_id=invocation_id,
        user_content=types.Content(parts=[types.Part(text=prompt)], role="user"),
        final_response=final_response_content,
        intermediate_data=InvocationEvents(invocation_events=inv_events),
        creation_timestamp=time.time(),
    )

    return invocation


# =============================================================================
# HELPERS
# =============================================================================


def _extract_text(content) -> str:
    """Extract the text from a Content object."""
    if not content or not content.parts:
        return ""
    texts = [p.text for p in content.parts if p.text]
    return " ".join(texts)


def _format_args(args) -> str:
    """Format function call args for logging."""
    if not args:
        return ""
    pairs = [f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}" for k, v in args.items()]
    return ", ".join(pairs)


# =============================================================================
# MAIN GENERATION LOGIC
# =============================================================================


async def generate_dataset(agent_key: str, delay: float = 2.0) -> str:
    """Generate the eval dataset for a given agent.

    Returns the output file path.
    """
    from google.adk.evaluation.eval_case import EvalCase, SessionInput
    from google.adk.evaluation.eval_set import EvalSet

    registry = AGENT_REGISTRY[agent_key]
    cases = registry["cases"]
    eval_set_id = registry["eval_set_id"]
    output_file = ROOT / registry["output_file"]
    default_user_id = registry["default_user_id"]
    app_name = f"eval_{agent_key}"

    # Import the agent
    import importlib

    mod = importlib.import_module(registry["agent_import"])
    agent = getattr(mod, registry["agent_attr"])

    logger.info("=" * 60)
    logger.info("Generating: %s (%d cases)", eval_set_id, len(cases))
    logger.info("Agent: %s", agent.name)
    logger.info("Output: %s", output_file)
    logger.info("=" * 60)

    eval_cases = []
    for i, case in enumerate(cases, 1):
        eval_id = case["eval_id"]
        prompt = case["prompt"]
        user_id = case.get("user_id", default_user_id)
        logger.info('\n[%d/%d] %s: "%s" (user=%s)', i, len(cases), eval_id, prompt, user_id)

        try:
            invocation = await run_eval_case(agent, case, default_user_id, app_name)
        except Exception as e:
            logger.error("  ✗ FAILED: %s — skipping", e)
            continue

        eval_case = EvalCase(
            eval_id=eval_id,
            conversation=[invocation],
            session_input=SessionInput(app_name=app_name, user_id=user_id),
        )
        eval_cases.append(eval_case)

        # Pace requests to avoid 429 rate limits
        if i < len(cases):
            await asyncio.sleep(delay)

    # Build the final EvalSet using ADK pydantic model
    eval_set = EvalSet(
        eval_set_id=eval_set_id,
        name=eval_set_id,
        eval_cases=eval_cases,
        creation_timestamp=time.time(),
    )

    # Serialize using pydantic (mode="json" handles bytes → base64 automatically)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    data = eval_set.model_dump(mode="json", exclude_none=True)

    # Clean up empty defaults to match ADK Web UI format exactly
    for case_data in data.get("eval_cases", []):
        si = case_data.get("session_input")
        if si and si.get("state") == {}:
            del si["state"]
        if case_data.get("final_session_state") == {}:
            del case_data["final_session_state"]

    with open(output_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info("\n" + "=" * 60)
    logger.info("\u2705 Generated %s (%d eval cases)", output_file.relative_to(ROOT), len(eval_cases))
    logger.info("=" * 60)

    return str(output_file)


# =============================================================================
# CLI
# =============================================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate ADK eval datasets by running the actual agent",
    )
    parser.add_argument(
        "--agent",
        choices=list(AGENT_REGISTRY.keys()),
        help="Which agent to generate for (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print test cases without running the agent",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Override output file path",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait between cases to avoid rate limits (default: 2)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    agents_to_run = [args.agent] if args.agent else list(AGENT_REGISTRY.keys())

    if args.dry_run:
        for agent_key in agents_to_run:
            reg = AGENT_REGISTRY[agent_key]
            logger.info("\n%s (%d cases) -> %s", reg["eval_set_id"], len(reg["cases"]), reg["output_file"])
            for i, case in enumerate(reg["cases"], 1):
                uid = case.get("user_id", reg["default_user_id"])
                logger.info('  [%d] %s: "%s" (user=%s)', i, case["eval_id"], case["prompt"], uid)
        return

    # Apply mocks and run
    with apply_mocks():
        for agent_key in agents_to_run:
            if args.output and len(agents_to_run) == 1:
                AGENT_REGISTRY[agent_key]["output_file"] = args.output
            asyncio.run(generate_dataset(agent_key, delay=args.delay))


if __name__ == "__main__":
    main()
