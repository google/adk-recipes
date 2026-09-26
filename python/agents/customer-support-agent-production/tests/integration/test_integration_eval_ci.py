"""
CI/CD Integration Evaluation Tests using ADK AgentEvaluator

These tests evaluate multi-agent coordination and complex workflows:
- Multi-agent handoffs (product -> order -> billing)
- Refund agent handoffs
- Error handling
- End-to-end customer journeys
- Session context persistence

Eval profiles (EVAL_PROFILE env var):
    fast     — response_match only (free, ~30s)
    standard — + rubric_based_tool_use LLM judge (default)
    full     — + final_response_match_v2 LLM judge

Run with:
    pytest tests/integration/test_integration_eval_ci.py -v -s
    EVAL_PROFILE=fast pytest tests/integration/test_integration_eval_ci.py -v -s

Run single test:
    pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_product_agent_handoffs -v -s

Generate evalsets:
    python tests/generate_integration_evalset.py --dry-run
    python tests/generate_integration_evalset.py --delay 10
"""

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

from tests.eval_configs import load_eval_config, load_eval_set

AGENT_MODULE = "eval_wrappers.test_root_agent"


# =============================================================================
# MULTI-AGENT HANDOFF TESTS
# =============================================================================


class TestMultiAgentHandoffs:
    """Test multi-agent coordination and handoffs."""

    @pytest.mark.asyncio
    async def test_product_agent_handoffs(self):
        """Product browse flow: search -> details -> inventory -> reviews."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/product_agent_handoffs.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=False,
        )

    @pytest.mark.asyncio
    async def test_order_tracking_handoffs(self):
        """Order tracking: multiple orders -> order history."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/order_tracking_handoffs.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=True,
        )

    @pytest.mark.asyncio
    async def test_billing_agent_handoffs(self):
        """Billing overview: invoices -> invoice detail -> payment status."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/billing_handoffs.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=False,
        )

    @pytest.mark.asyncio
    async def test_refund_agent_handoffs(self):
        """Refund flows: eligible, denied, refund then context switch.

        num_runs=1: eval cases are intentionally stateful — refund_eligible_flow
        writes a refund record that refund_then_other expects to find already present.
        Running twice with a shared mock DB would cause refund_eligible_flow to fail
        on run 2 (item already refunded from run 1).
        """
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/refund_agent_handoffs.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=1,
            print_detailed_results=False,
        )

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Error recovery: invalid -> valid, unauthorized -> own, mixed requests."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/error_handling.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=False,
        )

    @pytest.mark.asyncio
    async def test_multi_agent_handoffs(self):
        """Cross-agent single prompts: product->order, order->billing, three-agent."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/multi_agent_handoffs.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=False,
        )


# =============================================================================
# END-TO-END CUSTOMER JOURNEY TESTS
# =============================================================================


class TestEndToEnd:
    """Test complete end-to-end customer journeys."""

    @pytest.mark.asyncio
    async def test_e2e_customer_journey(self):
        """Full journeys: purchase research, post-purchase support, multi-order."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/e2e_customer_journey.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=False,
        )


# =============================================================================
# SESSION CONTEXT PERSISTENCE TESTS
# =============================================================================


class TestSessionPersistence:
    """Test that session context is maintained across turns and agents."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not __import__("pathlib").Path("tests/integration/session_persistence.evalset.json").exists(),
        reason="session_persistence.evalset.json not generated yet — run: python tests/generate_integration_evalset.py",
    )
    async def test_session_persistence(self):
        """Context retention: product follow-ups, order->invoice, cross-agent refund."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module=AGENT_MODULE,
            eval_set=load_eval_set("tests/integration/session_persistence.evalset.json"),
            eval_config=load_eval_config("integration"),
            num_runs=2,
            print_detailed_results=False,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
