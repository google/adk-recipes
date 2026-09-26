"""
Standalone Refund Workflow Test - Direct Function Testing

Tests the three refund workflow functions directly without ADK infrastructure.
This test requires Firestore database to be seeded.

Run with: python -m pytest tests/test_refund_standalone.py -v -s
"""

import pytest

from customer_support_mas.database import db_client


def validate_order_id(order_id: str) -> dict:
    """Validate order exists."""
    return (
        {"status": "valid"} if db_client.collection("orders").document(order_id).get().exists else {"status": "invalid"}
    )


def check_refund_eligibility(order_id: str) -> dict:
    """Check refund eligibility."""
    doc = db_client.collection("refund_eligibility").document(order_id).get()
    return {"status": "success", **doc.to_dict()} if doc.exists else {"status": "not_found"}


def process_refund(order_id: str, reason: str) -> dict:
    """Process refund."""
    order_doc = db_client.collection("orders").document(order_id).get()
    if not order_doc.exists:
        return {"status": "error", "message": "Order not found"}
    refund_id = f"REF-{order_id.replace('ORD-', '')}"
    db_client.collection("refunds").document(refund_id).set(
        {"order_id": order_id, "reason": reason, "status": "pending"}
    )
    return {"status": "success", "refund_id": refund_id, "message": "Refund submitted"}


class TestRefundWorkflowStandalone:
    """Standalone tests for refund workflow tools."""

    def test_passing_scenario_ord_67890(self):
        """TEST 1: PASSING - Order within return window (ORD-67890)"""
        order_id = "ORD-67890"
        reason = "Defective audio quality"

        # Step 1: Validate
        result1 = validate_order_id(order_id)
        assert result1["status"] == "valid", f"Expected valid, got {result1}"

        # Step 2: Check eligibility
        result2 = check_refund_eligibility(order_id)
        assert result2.get("status") == "success", f"Expected success, got {result2}"
        assert result2.get("eligible", False), f"Expected eligible=True, got {result2}"
        assert "Within 30-day return window" in result2.get("reason", ""), f"Wrong reason: {result2}"

        # Step 3: Process refund
        result3 = process_refund(order_id, reason)
        assert result3["status"] == "success", f"Expected success, got {result3}"
        assert result3["refund_id"] == "REF-67890", f"Wrong refund ID: {result3}"

        # No cleanup needed — mock is in-memory

    def test_failing_scenario_ord_11111(self):
        """TEST 2: FAILING - Order past return window (ORD-11111)"""
        order_id = "ORD-11111"

        # Step 1: Validate (should pass)
        result1 = validate_order_id(order_id)
        assert result1["status"] == "valid", f"Expected valid, got {result1}"

        # Step 2: Check eligibility (should fail here)
        result2 = check_refund_eligibility(order_id)
        assert result2.get("status") == "success", f"Expected success, got {result2}"
        assert not result2.get("eligible", False), f"Expected eligible=False, got {result2}"
        assert "Past 30-day return window" in result2.get("reason", ""), f"Wrong reason: {result2}"

        # Step 3: Would not execute in SequentialAgent workflow

    def test_failing_scenario_invalid_order(self):
        """TEST 3: FAILING - Invalid order (ORD-99999)"""
        order_id = "ORD-99999"

        # Step 1: Validate (should fail here)
        result1 = validate_order_id(order_id)
        assert result1["status"] == "invalid", f"Expected invalid, got {result1}"

        # Steps 2 & 3: Would not execute in SequentialAgent workflow

    def test_passing_scenario_ord_12345(self):
        """TEST 4: PASSING - In-transit order cancellation (ORD-12345)"""
        order_id = "ORD-12345"
        reason = "Ordered wrong model"

        # Step 1: Validate
        result1 = validate_order_id(order_id)
        assert result1["status"] == "valid", f"Expected valid, got {result1}"

        # Step 2: Check eligibility
        result2 = check_refund_eligibility(order_id)
        assert result2.get("status") == "success", f"Expected success, got {result2}"
        assert result2.get("eligible", False), f"Expected eligible=True, got {result2}"

        # Step 3: Process refund
        result3 = process_refund(order_id, reason)
        assert result3["status"] == "success", f"Expected success, got {result3}"
        assert result3["refund_id"] == "REF-12345", f"Wrong refund ID: {result3}"

        # No cleanup needed — mock is in-memory

    def test_passing_scenario_ord_22222(self):
        """TEST 5: PASSING - Processing order cancellation (ORD-22222)"""
        order_id = "ORD-22222"
        reason = "Need to modify purchase"

        # Step 1: Validate
        result1 = validate_order_id(order_id)
        assert result1["status"] == "valid", f"Expected valid, got {result1}"

        # Step 2: Check eligibility
        result2 = check_refund_eligibility(order_id)
        assert result2.get("status") == "success", f"Expected success, got {result2}"
        assert result2.get("eligible", False), f"Expected eligible=True, got {result2}"

        # Step 3: Process refund
        result3 = process_refund(order_id, reason)
        assert result3["status"] == "success", f"Expected success, got {result3}"
        assert result3["refund_id"] == "REF-22222", f"Wrong refund ID: {result3}"

        # No cleanup needed — mock is in-memory


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
