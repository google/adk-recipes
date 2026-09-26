"""
Unit Tests for Customer Support Tools - CI/CD Compatible

These tests run WITHOUT Vertex AI / LLM calls.
They test the tool functions directly against Firestore.

Run with:
    pytest tests/unit/test_tools.py -v -s
"""

from unittest.mock import MagicMock

import pytest

# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def firestore_client(mock_db):
    """Return the mock Firestore client from conftest.py."""
    return mock_db


@pytest.fixture(autouse=True)
def clear_refunds_before_test(mock_db):
    """Clear all refunds before each test to ensure isolation."""
    # Clear any existing refunds from previous tests
    if "refunds" in mock_db._collections:
        mock_db._collections["refunds"].clear()
    yield
    # Also clear after test
    if "refunds" in mock_db._collections:
        mock_db._collections["refunds"].clear()


@pytest.fixture
def mock_tool_context():
    """Create a mock ToolContext for tools that need it.

    Uses demo-user-001 which owns orders: ORD-12345, ORD-67890, ORD-11111
    """
    mock_ctx = MagicMock()
    mock_ctx.state = {}
    mock_ctx.user_id = "demo-user-001"  # Must match customer_id in seed data
    mock_ctx.actions = MagicMock()  # For escalate action
    return mock_ctx


@pytest.fixture
def mock_tool_context_user2():
    """Create a mock ToolContext for demo-user-002.

    Owns orders: ORD-22222
    """
    mock_ctx = MagicMock()
    mock_ctx.state = {}
    mock_ctx.user_id = "demo-user-002"
    mock_ctx.actions = MagicMock()
    return mock_ctx


# =============================================================================
# PRODUCT TOOLS TESTS
# =============================================================================


class TestProductTools:
    """Test product-related tools."""

    def test_search_products_laptops(self, mock_tool_context):
        """Test searching for laptops returns results."""
        from customer_support_mas.agents.product.tools import search_products

        result = search_products(query="laptops", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert result["count"] > 0
        assert "products" in result

        # Should find laptop products
        product_names = [p["name"].lower() for p in result["products"]]
        assert any("laptop" in name for name in product_names)

    def test_search_products_with_price_filter(self, mock_tool_context):
        """Test search filters by price when mentioned."""
        from customer_support_mas.agents.product.tools import search_products

        result = search_products(query="laptops under $600", tool_context=mock_tool_context)

        # No laptops under $600 in seed data, so RAG returns empty → falls back to keyword
        assert result["status"] in ["success", "no_results"]

    def test_search_products_no_results(self, mock_tool_context):
        """Test search with no matching products."""
        from customer_support_mas.agents.product.tools import search_products

        result = search_products(query="xyz123nonexistent", tool_context=mock_tool_context)

        assert result["status"] == "no_results"

    def test_get_product_details_valid(self):
        """Test getting details for valid product."""
        from customer_support_mas.agents.product.tools import get_product_details

        result = get_product_details(product_id="PROD-001")

        assert result["status"] == "success"
        assert "product" in result
        assert result["product"]["id"] == "PROD-001"
        assert "name" in result["product"]
        assert "price" in result["product"]

    def test_get_product_details_invalid(self):
        """Test getting details for non-existent product."""
        from customer_support_mas.agents.product.tools import get_product_details

        result = get_product_details(product_id="PROD-99999")

        assert result["status"] in ["error", "not_found"]

    def test_check_inventory_valid(self):
        """Test checking inventory for valid product."""
        from customer_support_mas.agents.product.tools import check_inventory

        result = check_inventory(product_id="PROD-001")

        assert result["status"] == "success"

    def test_get_product_reviews(self):
        """Test getting reviews for a product."""
        from customer_support_mas.agents.product.tools import get_product_reviews

        result = get_product_reviews(product_id="PROD-001")

        # Reviews may or may not exist
        assert result["status"] in ["success", "error", "not_found"]


# =============================================================================
# ORDER TOOLS TESTS
# =============================================================================


class TestOrderTools:
    """Test order-related tools."""

    def test_track_order_valid(self, mock_tool_context):
        """Test tracking a valid order."""
        from customer_support_mas.agents.order.tools import track_order

        result = track_order(order_id="ORD-12345", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert "order" in result
        assert result["order"]["order_id"] == "ORD-12345"
        assert "status" in result["order"]

    def test_track_order_invalid(self, mock_tool_context):
        """Test tracking a non-existent order."""
        from customer_support_mas.agents.order.tools import track_order

        result = track_order(order_id="ORD-99999", tool_context=mock_tool_context)

        assert result["status"] in ["error", "not_found"]

    def test_track_order_in_transit(self, mock_tool_context):
        """Test tracking an in-transit order."""
        from customer_support_mas.agents.order.tools import track_order

        result = track_order(order_id="ORD-12345", tool_context=mock_tool_context)

        assert result["status"] == "success"
        # ORD-12345 should be in transit based on seed data
        order_status = result["order"]["status"].lower().replace("_", " ")
        assert order_status in ["in transit", "shipped", "processing"]

    def test_track_order_delivered(self, mock_tool_context):
        """Test tracking a delivered order."""
        from customer_support_mas.agents.order.tools import track_order

        result = track_order(order_id="ORD-67890", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert result["order"]["status"].lower() == "delivered"


# =============================================================================
# BILLING TOOLS TESTS
# =============================================================================


class TestBillingTools:
    """Test billing-related tools."""

    def test_get_invoice_valid(self, mock_tool_context):
        """Test getting a valid invoice."""
        from customer_support_mas.agents.billing.tools import get_invoice

        result = get_invoice(invoice_id="INV-2025-001", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert "invoice" in result
        assert result["invoice"]["invoice_id"] == "INV-2025-001"

    def test_get_invoice_invalid(self, mock_tool_context):
        """Test getting a non-existent invoice."""
        from customer_support_mas.agents.billing.tools import get_invoice

        result = get_invoice(invoice_id="INV-9999-999", tool_context=mock_tool_context)

        assert result["status"] in ["error", "not_found"]

    def test_get_invoice_by_order_id(self, mock_tool_context):
        """Test getting invoice by order ID."""
        from customer_support_mas.agents.billing.tools import get_invoice_by_order_id

        result = get_invoice_by_order_id(order_id="ORD-12345", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert "invoice" in result

    def test_check_payment_status(self, mock_tool_context):
        """Test checking payment status."""
        from customer_support_mas.agents.billing.tools import check_payment_status

        result = check_payment_status(order_id="ORD-12345", tool_context=mock_tool_context)

        assert result["status"] == "success"


# =============================================================================
# WORKFLOW TOOLS TESTS (Refund)
# =============================================================================


class TestWorkflowTools:
    """Test workflow-related tools (refund process)."""

    def test_validate_refund_request_valid_delivered(self, mock_tool_context):
        """Test validating a delivered order (eligible for refund)."""
        from customer_support_mas.agents.refund.tools import validate_refund_request

        # ORD-67890 is delivered and owned by demo-user-001
        result = validate_refund_request(order_id="ORD-67890", tool_context=mock_tool_context)

        assert result["status"] == "valid"
        assert "items_to_refund" in result

    def test_validate_refund_request_not_delivered(self, mock_tool_context):
        """Test validating an in-transit order (not yet delivered)."""
        from customer_support_mas.agents.refund.tools import validate_refund_request

        # ORD-12345 is in transit
        result = validate_refund_request(order_id="ORD-12345", tool_context=mock_tool_context)

        assert result["status"] == "not_eligible"
        assert "in transit" in result.get("message", "").lower() or "not delivered" in result.get("message", "").lower()

    def test_validate_refund_request_invalid_order(self, mock_tool_context):
        """Test validating a non-existent order ID."""
        from customer_support_mas.agents.refund.tools import validate_refund_request

        result = validate_refund_request(order_id="ORD-99999", tool_context=mock_tool_context)

        assert result["status"] in ["invalid", "error"]

    def test_check_refund_eligibility_eligible(self, mock_tool_context):
        """Test refund eligibility for eligible order (within 30-day window)."""
        from customer_support_mas.agents.refund.tools import check_refund_eligibility, validate_refund_request

        # First validate to populate tool_context.state with items
        validate_refund_request(order_id="ORD-67890", tool_context=mock_tool_context)

        # ORD-67890 should be within 30-day window (delivered 5 days ago)
        result = check_refund_eligibility(order_id="ORD-67890", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert result.get("eligible")

    def test_check_refund_eligibility_not_eligible(self, mock_tool_context):
        """Test refund eligibility for order past return window."""
        from customer_support_mas.agents.refund.tools import check_refund_eligibility

        # ORD-11111 should be past 30-day window (delivered 45 days ago)
        result = check_refund_eligibility(order_id="ORD-11111", tool_context=mock_tool_context)

        # Can be "not_eligible" or "success" with eligible=False depending on implementation
        assert not result.get("eligible")

    def test_process_refund_success(self, mock_tool_context, firestore_client):
        """Test processing a refund successfully."""
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund(order_id="ORD-67890", reason="Product is defective", tool_context=mock_tool_context)

        assert result["status"] == "success"
        assert "refund_id" in result

        # Cleanup - delete the test refund
        refund_id = result["refund_id"]
        firestore_client.collection("refunds").document(refund_id).delete()

    def test_process_refund_invalid_order(self, mock_tool_context):
        """Test processing refund for invalid order."""
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund(order_id="ORD-99999", reason="Test", tool_context=mock_tool_context)

        assert result["status"] == "error"

    def test_process_refund_invalid_reason(self, mock_tool_context):
        """Test processing refund with unacceptable reason."""
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund(order_id="ORD-67890", reason="I changed my mind", tool_context=mock_tool_context)

        assert result["status"] == "reason_not_acceptable"


# =============================================================================
# CHECK IF REFUNDABLE TOOL TESTS (NEW)
# =============================================================================


class TestCheckIfRefundable:
    """Test the check_if_refundable pre-check tool."""

    def test_check_if_refundable_eligible(self, mock_tool_context):
        """Test order that IS eligible for refund."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        # ORD-67890: delivered 5 days ago, within 30-day window
        result = check_if_refundable(order_id="ORD-67890", tool_context=mock_tool_context)

        assert result["status"] == "eligible"
        assert result["eligible"]
        assert "refundable_items" in result
        assert result["estimated_refund_amount"] > 0
        assert result["days_remaining_in_window"] > 0

    def test_check_if_refundable_past_window(self, mock_tool_context):
        """Test order past 30-day return window."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        # ORD-11111: delivered 45 days ago, past 30-day window
        result = check_if_refundable(order_id="ORD-11111", tool_context=mock_tool_context)

        assert result["status"] == "not_eligible"
        assert not result["eligible"]
        assert "30" in result.get("reason", "") or "window" in result.get("reason", "").lower()

    def test_check_if_refundable_not_delivered(self, mock_tool_context):
        """Test order not yet delivered (in transit)."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        # ORD-12345: in transit, not delivered yet
        result = check_if_refundable(order_id="ORD-12345", tool_context=mock_tool_context)

        assert result["status"] == "not_eligible"
        assert not result["eligible"]
        assert "transit" in result.get("reason", "").lower() or "delivered" in result.get("reason", "").lower()

    def test_check_if_refundable_invalid_order(self, mock_tool_context):
        """Test non-existent order."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        result = check_if_refundable(order_id="ORD-99999", tool_context=mock_tool_context)

        assert result["status"] == "not_eligible"
        assert not result["eligible"]
        assert "not found" in result.get("reason", "").lower()

    def test_check_if_refundable_wrong_user(self, mock_tool_context_user2):
        """Test order belonging to different user (authorization check)."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        # ORD-67890 belongs to demo-user-001, not demo-user-002
        result = check_if_refundable(order_id="ORD-67890", tool_context=mock_tool_context_user2)

        assert result["status"] == "not_eligible"
        assert not result["eligible"]
        assert "permission" in result.get("reason", "").lower()

    def test_check_if_refundable_processing_order(self, mock_tool_context_user2):
        """Test order still being processed."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        # ORD-22222 belongs to demo-user-002 and is in Processing status
        result = check_if_refundable(order_id="ORD-22222", tool_context=mock_tool_context_user2)

        assert result["status"] == "not_eligible"
        assert not result["eligible"]
        assert "processing" in result.get("reason", "").lower() or "cancel" in result.get("reason", "").lower()


# =============================================================================
# REFUND WORKFLOW INTEGRATION
# =============================================================================


class TestRefundWorkflowIntegration:
    """Test the complete refund workflow sequence."""

    def test_full_refund_workflow_success(self, mock_tool_context, firestore_client):
        """Test complete refund workflow: pre-check -> validate -> check -> process."""
        from customer_support_mas.agents.refund.tools import (
            check_if_refundable,
            check_refund_eligibility,
            process_refund,
            validate_refund_request,
        )

        order_id = "ORD-67890"
        reason = "Integration test - defective item"

        # Step 0: Pre-check (new conversational flow)
        precheck_result = check_if_refundable(order_id, tool_context=mock_tool_context)
        assert precheck_result["status"] == "eligible", f"Pre-check failed: {precheck_result}"
        assert precheck_result["eligible"]

        # Step 1: Validate order
        validate_result = validate_refund_request(order_id, tool_context=mock_tool_context)
        assert validate_result["status"] == "valid", f"Validation failed: {validate_result}"

        # Step 2: Check eligibility
        eligibility_result = check_refund_eligibility(order_id, tool_context=mock_tool_context)
        assert eligibility_result["status"] == "success", f"Eligibility check failed: {eligibility_result}"
        assert eligibility_result["eligible"], f"Order not eligible: {eligibility_result}"

        # Step 3: Process refund
        refund_result = process_refund(order_id, reason, tool_context=mock_tool_context)
        assert refund_result["status"] == "success", f"Refund failed: {refund_result}"
        assert "refund_id" in refund_result

        # Cleanup
        firestore_client.collection("refunds").document(refund_result["refund_id"]).delete()

    def test_refund_workflow_stops_at_invalid_order(self, mock_tool_context):
        """Test workflow stops at validation for invalid order."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        # Pre-check - should fail for non-existent order
        precheck_result = check_if_refundable("ORD-99999", tool_context=mock_tool_context)
        assert precheck_result["status"] == "not_eligible"
        assert not precheck_result["eligible"]

        # Workflow should stop here - no further steps

    def test_refund_workflow_stops_at_eligibility(self, mock_tool_context):
        """Test workflow stops at eligibility check for ineligible order."""
        from customer_support_mas.agents.refund.tools import check_if_refundable

        order_id = "ORD-11111"  # Past return window

        # Pre-check - should fail due to past 30-day window
        precheck_result = check_if_refundable(order_id, tool_context=mock_tool_context)
        assert precheck_result["status"] == "not_eligible"
        assert not precheck_result["eligible"]
        assert "days" in precheck_result.get("reason", "").lower()

        # Workflow should stop here - refund not processed

    def test_new_conversational_flow(self, mock_tool_context, firestore_client):
        """Test the new conversational refund flow:
        1. User requests refund (just order_id)
        2. System checks eligibility first
        3. If eligible, would ask for reason (simulated here)
        4. Process with reason
        """
        from customer_support_mas.agents.refund.tools import check_if_refundable, process_refund

        order_id = "ORD-67890"

        # Step 1: Pre-check eligibility (no reason needed yet)
        precheck = check_if_refundable(order_id, tool_context=mock_tool_context)
        assert precheck["eligible"]
        assert "next_step" in precheck  # Should prompt for reason

        # Step 2: User provides reason, process refund
        reason = "Product arrived damaged"
        result = process_refund(order_id, reason, tool_context=mock_tool_context)
        assert result["status"] == "success"

        # Cleanup
        firestore_client.collection("refunds").document(result["refund_id"]).delete()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
