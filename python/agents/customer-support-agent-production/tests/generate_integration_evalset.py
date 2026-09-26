#!/usr/bin/env python3
"""
Generate ADK integration evaluation datasets by running the root agent.

Unlike the unit test script (generate_eval_dataset.py) which tests single agents
with single-turn conversations, this script generates multi-turn integration
evalsets that test orchestrator handoffs, cross-agent workflows, and session
context persistence through the root agent.

Output format matches the ADK web eval dashboard export (invocationEvents).

Usage:
    python tests/generate_integration_evalset.py                              # all
    python tests/generate_integration_evalset.py --suite product_handoffs     # one suite
    python tests/generate_integration_evalset.py --dry-run                    # preview
    python tests/generate_integration_evalset.py --delay 10                   # rate-limit safe
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
# INTEGRATION TEST CASE DEFINITIONS (from tests/GOLDEN_DATASETS.md Part 2)
#
# Each case has a list of "prompts" — the conversation turns.
# All cases use the root agent (customer_support orchestrator).
# =============================================================================

# --- 2.1 Product Agent Handoffs ---
PRODUCT_HANDOFFS_CASES = [
    {
        "eval_id": "product_browse_flow",
        "user_id": "demo-user-001",
        "prompts": [
            "Show me laptops",
            "Tell me more about the ProBook",
            "Is it in stock?",
            "What are the reviews?",
        ],
    },
]

# --- 2.2 Order Tracking Handoffs ---
ORDER_TRACKING_HANDOFFS_CASES = [
    {
        "eval_id": "track_multiple_orders",
        "user_id": "demo-user-001",
        "prompts": [
            "Where is order ORD-12345?",
            "What about ORD-67890?",
            "Show me all my orders",
        ],
    },
]

# --- 2.3 Billing Handoffs ---
BILLING_HANDOFFS_CASES = [
    {
        "eval_id": "billing_overview",
        "user_id": "demo-user-001",
        "prompts": [
            "Show me all my invoices",
            "What's the status of invoice INV-2025-001?",
            "Has the payment gone through?",
        ],
    },
]

# --- 2.4 Refund Agent Handoffs ---
REFUND_HANDOFFS_CASES = [
    {
        "eval_id": "refund_eligible_flow",
        "user_id": "demo-user-001",
        "prompts": [
            "I want a refund for order ORD-67890",
            "The item arrived damaged",
        ],
    },
    {
        "eval_id": "refund_denied_flow",
        "user_id": "demo-user-001",
        "prompts": [
            "Refund order ORD-11111",
        ],
    },
    {
        "eval_id": "refund_then_other",
        "user_id": "demo-user-001",
        "prompts": [
            "Refund ORD-67890, defective",
            "Track order ORD-12345",
        ],
    },
]

# --- 2.5 Multi-Agent Handoffs ---
MULTI_AGENT_HANDOFFS_CASES = [
    {
        "eval_id": "product_to_order",
        "user_id": "demo-user-001",
        "prompts": [
            "I ordered the ProBook laptop, where is it? Order ORD-12345",
        ],
    },
    {
        "eval_id": "order_to_billing",
        "user_id": "demo-user-001",
        "prompts": [
            "Track order ORD-12345 and show me its invoice",
        ],
    },
    {
        "eval_id": "order_payment",
        "user_id": "demo-user-001",
        "prompts": [
            "What's the status of ORD-12345 and has the payment gone through?",
        ],
    },
    {
        "eval_id": "three_agent_query",
        "user_id": "demo-user-001",
        "prompts": [
            "Tell me about the ProBook, track ORD-12345, and show the invoice",
        ],
    },
]

# --- 2.6 E2E Customer Journeys ---
E2E_JOURNEY_CASES = [
    {
        "eval_id": "journey_purchase",
        "user_id": "demo-user-001",
        "prompts": [
            "Show me laptops under $1500",
            "Tell me about the ProBook",
            "Is it in stock?",
            "What do customers say about it?",
        ],
    },
    {
        "eval_id": "journey_support",
        "user_id": "demo-user-001",
        "prompts": [
            "Track order ORD-67890",
            "Show me the invoice",
            "I want a refund, the item is defective",
        ],
    },
    {
        "eval_id": "journey_multi_order",
        "user_id": "demo-user-001",
        "prompts": [
            "Where is order ORD-12345?",
            "What about ORD-67890?",
            "Show me all my invoices",
            "What's my payment history?",
        ],
    },
]

# --- 2.7 Error Handling Integration ---
ERROR_HANDLING_CASES = [
    {
        "eval_id": "error_then_valid",
        "user_id": "demo-user-001",
        "prompts": [
            "Track order ORD-99999",
            "Try ORD-12345 instead",
        ],
    },
    {
        "eval_id": "unauthorized_then_own",
        "user_id": "demo-user-001",
        "prompts": [
            "Track order ORD-22222",
            "Show me my orders",
        ],
    },
    {
        "eval_id": "mixed_requests",
        "user_id": "demo-user-001",
        "prompts": [
            "What's the weather?",
            "Show me laptops",
            "Tell me a joke",
            "Track ORD-12345",
        ],
    },
]

# --- 2.8 Session Context Persistence ---
SESSION_PERSISTENCE_CASES = [
    {
        "eval_id": "context_product",
        "user_id": "demo-user-001",
        "prompts": [
            "Tell me about PROD-001",
            "Is it in stock?",
            "What are the reviews?",
        ],
    },
    {
        "eval_id": "context_order",
        "user_id": "demo-user-001",
        "prompts": [
            "Track ORD-12345",
            "Show me the invoice for it",
            "What's the payment status?",
        ],
    },
    {
        "eval_id": "context_cross_agent",
        "user_id": "demo-user-001",
        "prompts": [
            "Track ORD-67890",
            "I want a refund for it",
        ],
    },
]


# =============================================================================
# SUITE REGISTRY
# =============================================================================

SUITE_REGISTRY = {
    "product_handoffs": {
        "cases": PRODUCT_HANDOFFS_CASES,
        "eval_set_id": "product_agent_handoffs",
        "output_file": "tests/integration/product_agent_handoffs.evalset.json",
    },
    "order_handoffs": {
        "cases": ORDER_TRACKING_HANDOFFS_CASES,
        "eval_set_id": "order_tracking_handoffs",
        "output_file": "tests/integration/order_tracking_handoffs.evalset.json",
    },
    "billing_handoffs": {
        "cases": BILLING_HANDOFFS_CASES,
        "eval_set_id": "billing_handoffs",
        "output_file": "tests/integration/billing_handoffs.evalset.json",
    },
    "refund_handoffs": {
        "cases": REFUND_HANDOFFS_CASES,
        "eval_set_id": "refund_agent_handoffs",
        "output_file": "tests/integration/refund_agent_handoffs.evalset.json",
    },
    "multi_agent": {
        "cases": MULTI_AGENT_HANDOFFS_CASES,
        "eval_set_id": "multi_agent_handoffs",
        "output_file": "tests/integration/multi_agent_handoffs.evalset.json",
    },
    "e2e": {
        "cases": E2E_JOURNEY_CASES,
        "eval_set_id": "e2e_customer_journey",
        "output_file": "tests/integration/e2e_customer_journey.evalset.json",
    },
    "error_handling": {
        "cases": ERROR_HANDLING_CASES,
        "eval_set_id": "error_handling",
        "output_file": "tests/integration/error_handling.evalset.json",
    },
    "session_persistence": {
        "cases": SESSION_PERSISTENCE_CASES,
        "eval_set_id": "session_persistence",
        "output_file": "tests/integration/session_persistence.evalset.json",
    },
}


# =============================================================================
# MOCK SETUP (reuses tests/mock_firestore.py and tests/mock_rag_search.py)
# =============================================================================


def apply_mocks():
    """Apply the same Firestore + RAG mocks used by the test suite."""
    from tests.mock_firestore import MockFirestoreClient
    from tests.mock_rag_search import MockRAGProductSearch

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

    class MockContext:
        def __enter__(self):
            for p in patches:
                p.start()
            return self

        def __exit__(self, *args):
            for p in patches:
                p.stop()

    return MockContext()


# =============================================================================
# CORE: Run a multi-turn eval case and capture events per turn
# =============================================================================

MAX_RETRIES = 4
RETRY_BASE_DELAY = 15  # seconds


async def run_single_turn(runner, user_id: str, session_id: str, prompt: str, app_name: str):
    """Send one message and capture all events. Retries on 429.

    Returns (invocation_id, all_events) tuple.
    """
    from google.genai import types

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            all_events = []
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=types.Content(
                    parts=[types.Part(text=prompt)],
                    role="user",
                ),
            ):
                all_events.append(event)
            return all_events

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "    ⚠ Rate limited (attempt %d/%d). Retrying in %ds...",
                        attempt,
                        MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("    ✗ Rate limited after %d attempts.", MAX_RETRIES)
                    raise
            else:
                raise


def events_to_invocation(all_events, prompt: str):
    """Convert a list of Events from one turn into an Invocation object."""
    from google.adk.evaluation.eval_case import Invocation, InvocationEvent, InvocationEvents
    from google.genai import types

    invocation_id = all_events[0].invocation_id if all_events else str(uuid.uuid4())

    # Final response: the last event flagged as final
    final_response_content = None
    for event in reversed(all_events):
        if event.is_final_response() and event.content:
            final_response_content = event.content
            break

    # Build InvocationEvent objects (intermediate tool calls/responses)
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

    invocation = Invocation(
        invocation_id=invocation_id,
        user_content=types.Content(parts=[types.Part(text=prompt)], role="user"),
        final_response=final_response_content,
        intermediate_data=InvocationEvents(invocation_events=inv_events) if inv_events else None,
        creation_timestamp=time.time(),
    )

    return invocation, all_events


async def run_multi_turn_case(agent, case: dict, app_name: str, turn_delay: float):
    """Run a multi-turn conversation and return a list of Invocation objects.

    Each prompt in case["prompts"] is sent sequentially in the SAME session,
    preserving conversation context (exactly like the ADK web UI).
    """
    from google.adk.runners import InMemoryRunner

    user_id = case["user_id"]
    prompts = case["prompts"]

    runner = InMemoryRunner(agent=agent, app_name=app_name)

    # Create a single session for the entire conversation
    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
    )

    invocations = []
    for turn_idx, prompt in enumerate(prompts, 1):
        logger.info('    Turn %d/%d: "%s"', turn_idx, len(prompts), prompt)

        all_events = await run_single_turn(
            runner,
            user_id,
            session.id,
            prompt,
            app_name,
        )

        invocation, events = events_to_invocation(all_events, prompt)
        invocations.append(invocation)

        # Log tool calls
        for event in events:
            for fc in event.get_function_calls():
                logger.info("      → Agent: %s(%s)", fc.name, _format_args(fc.args))

        # Log final response
        final_text = _extract_text(invocation.final_response) if invocation.final_response else "(no response)"
        logger.info("      ✓ %s", final_text[:100])

        # Pace between turns
        if turn_idx < len(prompts):
            await asyncio.sleep(turn_delay)

    return invocations


# =============================================================================
# HELPERS
# =============================================================================


def _extract_text(content) -> str:
    if not content or not content.parts:
        return ""
    texts = [p.text for p in content.parts if p.text]
    return " ".join(texts)


def _format_args(args) -> str:
    if not args:
        return ""
    pairs = [f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}" for k, v in args.items()]
    return ", ".join(pairs)


# =============================================================================
# MAIN GENERATION LOGIC
# =============================================================================


async def generate_suite(suite_key: str, delay: float = 5.0, turn_delay: float = 3.0) -> str:
    """Generate the integration evalset for a given suite.

    Args:
        suite_key: Key into SUITE_REGISTRY
        delay: Seconds between eval cases (to avoid rate limits)
        turn_delay: Seconds between turns within a case
    """
    from google.adk.evaluation.eval_case import EvalCase, SessionInput
    from google.adk.evaluation.eval_set import EvalSet

    registry = SUITE_REGISTRY[suite_key]
    cases = registry["cases"]
    eval_set_id = registry["eval_set_id"]
    output_file = ROOT / registry["output_file"]
    app_name = "root_agent"  # Match ADK web UI (adk web uses folder/agent name)

    # Import root agent (via test_root_agent which disables memory callback)
    import importlib

    mod = importlib.import_module("eval_wrappers.test_root_agent")
    agent = getattr(mod, "agent")

    total_turns = sum(len(c["prompts"]) for c in cases)
    logger.info("=" * 60)
    logger.info("Generating: %s (%d cases, %d total turns)", eval_set_id, len(cases), total_turns)
    logger.info("Agent: %s (root orchestrator)", agent.name)
    logger.info("Output: %s", output_file)
    logger.info("=" * 60)

    eval_cases = []
    for i, case in enumerate(cases, 1):
        eval_id = case["eval_id"]
        user_id = case["user_id"]
        logger.info("\n  [%d/%d] %s (%d turns, user=%s)", i, len(cases), eval_id, len(case["prompts"]), user_id)

        try:
            invocations = await run_multi_turn_case(agent, case, app_name, turn_delay)
        except Exception as e:
            logger.error("    ✗ FAILED: %s — skipping", e)
            continue

        eval_case = EvalCase(
            eval_id=eval_id,
            conversation=invocations,
            session_input=SessionInput(app_name=app_name, user_id=user_id),
            creation_timestamp=time.time(),
        )
        eval_cases.append(eval_case)

        # Pace between cases
        if i < len(cases):
            await asyncio.sleep(delay)

    # Build and serialize
    eval_set = EvalSet(
        eval_set_id=eval_set_id,
        name=eval_set_id,
        eval_cases=eval_cases,
        creation_timestamp=time.time(),
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    data = eval_set.model_dump(mode="json", exclude_none=True)

    # Clean up empty defaults to match ADK Web UI format exactly:
    # - Remove empty "state" from session_input
    # - Remove empty "final_session_state" from eval_case
    for case_data in data.get("eval_cases", []):
        si = case_data.get("session_input")
        if si and si.get("state") == {}:
            del si["state"]
        if case_data.get("final_session_state") == {}:
            del case_data["final_session_state"]

    with open(output_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info("\n" + "=" * 60)
    logger.info(
        "✅ Generated %s (%d eval cases, %d total turns)", output_file.relative_to(ROOT), len(eval_cases), total_turns
    )
    logger.info("=" * 60)

    return str(output_file)


# =============================================================================
# CLI
# =============================================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate ADK integration eval datasets by running the root agent",
    )
    parser.add_argument(
        "--suite",
        choices=list(SUITE_REGISTRY.keys()),
        help="Which suite to generate (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print test cases without running the agent",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Seconds between eval cases (default: 5)",
    )
    parser.add_argument(
        "--turn-delay",
        type=float,
        default=3.0,
        help="Seconds between turns within a case (default: 3)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    suites_to_run = [args.suite] if args.suite else list(SUITE_REGISTRY.keys())

    if args.dry_run:
        total_cases = 0
        total_turns = 0
        for suite_key in suites_to_run:
            reg = SUITE_REGISTRY[suite_key]
            n_cases = len(reg["cases"])
            n_turns = sum(len(c["prompts"]) for c in reg["cases"])
            total_cases += n_cases
            total_turns += n_turns
            logger.info("\n%s (%d cases, %d turns) -> %s", reg["eval_set_id"], n_cases, n_turns, reg["output_file"])
            for i, case in enumerate(reg["cases"], 1):
                logger.info("  [%d] %s (user=%s, %d turns)", i, case["eval_id"], case["user_id"], len(case["prompts"]))
                for j, prompt in enumerate(case["prompts"], 1):
                    logger.info('      Turn %d: "%s"', j, prompt)
        logger.info("\nTotal: %d cases, %d turns across %d suites", total_cases, total_turns, len(suites_to_run))
        return

    # Apply mocks and run
    with apply_mocks():
        for suite_key in suites_to_run:
            asyncio.run(generate_suite(suite_key, delay=args.delay, turn_delay=args.turn_delay))


if __name__ == "__main__":
    main()
