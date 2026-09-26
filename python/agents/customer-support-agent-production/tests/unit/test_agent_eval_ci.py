"""
CI/CD Agent Evaluation Tests using ADK AgentEvaluator

These tests:
- Use Vertex AI Gemini API for LLM calls (real LLM, not mocked)
- Run agents DIRECTLY via AgentEvaluator (NOT through orchestrator)
- Test actual tool trajectories (tool calls in sequence)

Eval profiles (EVAL_PROFILE env var):
    fast     — response_match only (free, ~30s)
    standard — + tool_trajectory (free, default)
    full     — + final_response_match_v2 LLM judge

Run with:
    pytest tests/unit/test_agent_eval_ci.py -v -s
    EVAL_PROFILE=fast pytest tests/unit/test_agent_eval_ci.py -v -s
"""

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

from tests.eval_configs import load_eval_config, load_eval_set

# =============================================================================
# UNIT TESTS - Direct Agent Tests (testing actual tools, not handoffs)
# =============================================================================


class TestProductAgentCI:
    """Product agent direct tests - tests search_products, get_product_info tools."""

    @pytest.mark.asyncio
    async def test_product_agent_direct(self):
        """Test product agent directly with its tools."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_product_agent",
            eval_set=load_eval_set("tests/unit/product_agent_direct.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )


class TestOrderAgentCI:
    """Order agent direct tests - tests track_order, get_my_order_history tools."""

    @pytest.mark.asyncio
    async def test_order_agent_direct(self):
        """Test order agent directly with its tools."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_order_agent",
            eval_set=load_eval_set("tests/unit/order_agent_direct.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )


class TestBillingAgentCI:
    """Billing agent direct tests - tests get_invoice, check_payment_status tools."""

    @pytest.mark.asyncio
    async def test_billing_agent_direct(self):
        """Test billing agent directly with its tools."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_billing_agent",
            eval_set=load_eval_set("tests/unit/billing_direct.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )


class TestRefundEligibilityCI:
    """Refund eligibility tests - tests check_if_refundable tool only."""

    @pytest.mark.asyncio
    async def test_refund_eligibility(self):
        """Test refund eligibility checks."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_refund_eligibility",
            eval_set=load_eval_set("tests/unit/refund_workflow_direct.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )

    # NOTE: Full refund flow (passing/denied) and SequentialAgent tests are NOT
    # tested via AgentEvaluator because the SequentialAgent's gate-stopping
    # behavior is non-deterministic. Instead, they are tested via direct tool
    # calls in test_tools.py and test_refund_standalone.py.


class TestErrorHandlingCI:
    """Error handling tests - out-of-scope queries (no tool calls expected)."""

    @pytest.mark.asyncio
    async def test_out_of_scope(self):
        """Test out-of-scope and ambiguous queries through the root orchestrator."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_root_agent",
            eval_set=load_eval_set("tests/unit/cases/out_of_scope.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )


class TestAuthorizationUser1CI:
    """Authorization tests - demo-user-001 accessing demo-user-002 resources."""

    @pytest.mark.asyncio
    async def test_cross_user_access_user1(self):
        """Test that demo-user-001 cannot access demo-user-002's orders/invoices."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_root_agent",
            eval_set=load_eval_set("tests/unit/cases/authorization_cross_user.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )


class TestAuthorizationUser2CI:
    """Authorization tests - demo-user-002 accessing demo-user-001 resources."""

    @pytest.mark.asyncio
    async def test_cross_user_access_user2(self):
        """Test that demo-user-002 cannot access demo-user-001's orders."""
        await AgentEvaluator.evaluate_eval_set(
            agent_module="eval_wrappers.test_root_agent",
            eval_set=load_eval_set("tests/unit/cases/demo_user_002.test.json"),
            eval_config=load_eval_config("unit"),
            num_runs=2,
            print_detailed_results=True,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
