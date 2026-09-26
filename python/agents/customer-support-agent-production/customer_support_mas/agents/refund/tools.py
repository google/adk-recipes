"""
Workflow-related tools for the customer support system.

This module contains sequential refund workflow tools with comprehensive validation:
- Ownership verification (user can only refund their own orders)
- Item verification (items must exist in the order)
- Dynamic eligibility (calculated based on delivery date, status, previous refunds)
- Duplicate prevention (can't refund same items twice)

Note: We use helper functions instead of decorators because
 SequentialAgent tools need to control escalation behavior.
"""

import logging
from datetime import datetime
from typing import List, Optional

from google.adk.tools.tool_context import ToolContext

from customer_support_mas.auth import audit_log, verify_order_ownership
from customer_support_mas.database import db_client
from customer_support_mas.validation import (
    validate_order_id,
    validate_refund_reason,
    validation_error_response,
)

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

REFUND_WINDOW_DAYS = 30  # Items can be refunded within 30 days of delivery

# Acceptable refund reasons - these qualify for refunds
ACCEPTABLE_REFUND_REASONS = {
    "defective": "Product has a defect or malfunction",
    "damaged": "Product arrived damaged",
    "wrong_item": "Received wrong item",
    "not_as_described": "Product doesn't match description",
    "missing_parts": "Product missing parts or accessories",
    "quality_issue": "Product quality below expectations",
    "arrived_late": "Product arrived significantly late",
    "duplicate_order": "Accidentally ordered twice",
}

# Unacceptable refund reasons - these do NOT qualify for refunds
UNACCEPTABLE_REFUND_REASONS = {
    "changed_mind": "Changed my mind / No longer want it",
    "found_cheaper": "Found it cheaper elsewhere",
    "no_longer_need": "No longer need the product",
    "gift_unwanted": "Gift recipient didn't want it",
    "ordering_mistake": "Ordered by mistake (but item is fine)",
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse date string to datetime object."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def _get_existing_refunds(order_id: str) -> List[dict]:
    """Get all existing refunds for an order."""
    query = db_client.collection("refunds").where("order_id", "==", order_id)
    return [doc.to_dict() for doc in query.stream()]


def _get_refunded_item_ids(order_id: str) -> set:
    """Get set of product IDs that have already been refunded for this order."""
    refunds = _get_existing_refunds(order_id)
    refunded_ids = set()
    for refund in refunds:
        if refund.get("status") != "cancelled":  # Don't count cancelled refunds
            for item in refund.get("items", []):
                refunded_ids.add(item.get("product_id"))
    return refunded_ids


def _validate_items_in_order(order_items: List[dict], requested_item_ids: List[str]) -> tuple[bool, List[dict], str]:
    """
    Validate that requested items exist in the order.

    Returns:
        tuple: (is_valid, matched_items, error_message)
    """
    order_item_map = {item["product_id"]: item for item in order_items}
    matched_items = []
    missing_items = []

    for item_id in requested_item_ids:
        if item_id in order_item_map:
            matched_items.append(order_item_map[item_id])
        else:
            missing_items.append(item_id)

    if missing_items:
        return False, [], f"Items not found in order: {', '.join(missing_items)}"

    return True, matched_items, ""


def _calculate_refund_amount(items: List[dict]) -> float:
    """Calculate total refund amount for items."""
    total = 0.0
    for item in items:
        price = item.get("price", 0)
        qty = item.get("qty", 1)
        total += price * qty
    return round(total, 2)


def _classify_refund_reason(reason: str) -> tuple[bool, str, str]:
    """
    Classify a refund reason as acceptable or not.

    Uses fuzzy matching to categorize user-provided reasons.

    Args:
        reason: The refund reason provided by the user

    Returns:
        tuple: (is_acceptable, category, explanation)
    """
    reason_lower = reason.lower().strip()

    # Check for unacceptable reasons first (stricter matching)
    unacceptable_keywords = {
        "changed_mind": [
            "changed my mind",
            "changed mind",
            "don't want",
            "dont want",
            "no longer want",
            "decided not to",
        ],
        "found_cheaper": ["cheaper", "better price", "lower price", "found it for less", "price match"],
        "no_longer_need": ["don't need", "dont need", "no longer need", "not needed", "unnecessary"],
        "gift_unwanted": ["gift", "unwanted gift", "didn't like the gift"],
        "ordering_mistake": ["ordered by mistake", "accidental order", "didn't mean to order", "wrong click"],
    }

    for category, keywords in unacceptable_keywords.items():
        for keyword in keywords:
            if keyword in reason_lower:
                return False, category, UNACCEPTABLE_REFUND_REASONS[category]

    # Check for acceptable reasons
    acceptable_keywords = {
        "defective": ["defective", "defect", "malfunction", "doesn't work", "not working", "broken", "faulty"],
        "damaged": ["damaged", "damage", "cracked", "dented", "scratched", "torn"],
        "wrong_item": ["wrong item", "wrong product", "not what i ordered", "different item", "incorrect item"],
        "not_as_described": ["not as described", "different from", "doesn't match", "false advertising", "misleading"],
        "missing_parts": ["missing", "incomplete", "parts missing", "accessories missing"],
        "quality_issue": ["poor quality", "quality issue", "low quality", "cheap", "flimsy"],
        "arrived_late": ["late", "delayed", "took too long", "never arrived on time"],
        "duplicate_order": ["duplicate", "ordered twice", "double order"],
    }

    for category, keywords in acceptable_keywords.items():
        for keyword in keywords:
            if keyword in reason_lower:
                return True, category, ACCEPTABLE_REFUND_REASONS[category]

    # If no match found, default to requiring manual review
    # Return as potentially acceptable but flag for review
    return True, "other", "Reason requires manual review - proceeding with refund"


def get_acceptable_refund_reasons() -> dict:
    """Return list of acceptable refund reasons for display to users."""
    return {
        "acceptable": ACCEPTABLE_REFUND_REASONS,
        "not_acceptable": UNACCEPTABLE_REFUND_REASONS,
        "note": "Refunds are granted for product issues. 'Changed my mind' or similar reasons do not qualify.",
    }


# =============================================================================
# Sequential Refund Workflow Tools
# =============================================================================


def validate_refund_request(order_id: str, tool_context: ToolContext, item_ids: Optional[List[str]] = None) -> dict:
    """
    Step 1: Validate the refund request.

    Verifies:
    - Order exists and belongs to the authenticated user
    - Order status is "Delivered" (can't refund undelivered orders)
    - If item_ids provided, verifies items exist in the order
    - Returns order details for user confirmation

    Args:
        order_id: The order ID to validate (e.g., "ORD-12345")
        tool_context: ADK ToolContext (automatically injected)
        item_ids: Optional list of product IDs to refund. If None, refund all items.

    Returns:
        dict with order details and items to be refunded, or error message
    """
    # Input validation
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    user_id = tool_context.user_id
    logger.info(f"[Refund Workflow - Step 1] User {user_id} validating refund for order: {order_id}")

    # Verify ownership
    is_authorized, order_data, error_msg = verify_order_ownership(order_id, user_id, action="validate_refund_request")

    if not is_authorized:
        logger.warning(f"[Refund Workflow - Step 1] STOPPING - {error_msg}")
        tool_context.actions.escalate = True
        if "not found" in error_msg.lower():
            return {"status": "invalid", "message": error_msg}
        return {"status": "unauthorized", "message": error_msg}

    # Check order status - must be delivered
    order_status = order_data.get("status", "").lower()
    if order_status != "delivered":
        logger.warning(f"[Refund Workflow - Step 1] Order {order_id} status is '{order_status}', not delivered")
        tool_context.actions.escalate = True

        if order_status == "processing":
            return {
                "status": "not_eligible",
                "message": f"Order {order_id} is still being processed. You can cancel it instead of requesting a refund.",
                "suggestion": "cancel_order",
            }
        elif order_status == "in transit":
            return {
                "status": "not_eligible",
                "message": f"Order {order_id} is currently in transit. Please wait until it's delivered to request a refund.",
                "estimated_delivery": order_data.get("estimated_delivery"),
            }
        else:
            return {
                "status": "not_eligible",
                "message": f"Order {order_id} has status '{order_status}' and cannot be refunded.",
            }

    # Get order items
    order_items = order_data.get("items", [])
    if not order_items:
        logger.error(f"[Refund Workflow - Step 1] Order {order_id} has no items")
        tool_context.actions.escalate = True
        return {"status": "error", "message": "Order has no items to refund"}

    # If specific items requested, validate they exist in order
    if item_ids:
        is_valid, items_to_refund, error_msg = _validate_items_in_order(order_items, item_ids)
        if not is_valid:
            logger.warning(f"[Refund Workflow - Step 1] {error_msg}")
            tool_context.actions.escalate = True
            return {
                "status": "invalid_items",
                "message": error_msg,
                "available_items": [{"product_id": i["product_id"], "name": i["name"]} for i in order_items],
            }
    else:
        # Refund all items
        items_to_refund = order_items

    # Store items to refund in session state for next steps
    tool_context.state["refund_order_id"] = order_id
    tool_context.state["refund_items"] = items_to_refund
    tool_context.state["refund_order_data"] = order_data

    logger.info(f"[Refund Workflow - Step 1] Validated {len(items_to_refund)} items for refund")

    return {
        "status": "valid",
        "order_id": order_id,
        "order_date": order_data.get("date"),
        "delivered_date": order_data.get("delivered_date"),
        "items_to_refund": [
            {"product_id": item["product_id"], "name": item["name"], "qty": item.get("qty", 1), "price": item["price"]}
            for item in items_to_refund
        ],
        "estimated_refund": _calculate_refund_amount(items_to_refund),
    }


def check_refund_eligibility(order_id: str, tool_context: ToolContext) -> dict:
    """
    Step 2: Check refund eligibility with DYNAMIC calculation.

    Calculates eligibility based on:
    - Time since delivery (must be within 30-day window)
    - Items not already refunded (prevents duplicate refunds)

    Args:
        order_id: The order ID to check eligibility for
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        dict with eligibility status, reason, and refund details
    """
    # Input validation
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    user_id = tool_context.user_id
    logger.info(f"[Refund Workflow - Step 2] Checking eligibility for order: {order_id}")

    # Re-verify ownership (defense in depth)
    is_authorized, order_data, error_msg = verify_order_ownership(order_id, user_id, action="check_refund_eligibility")

    if not is_authorized:
        logger.warning(f"[Refund Workflow - Step 2] STOPPING - {error_msg}")
        tool_context.actions.escalate = True
        return {"status": "unauthorized", "message": error_msg}

    # Get items to refund from session state (set by Step 1)
    items_to_refund = tool_context.state.get("refund_items", order_data.get("items", []))

    # Check 1: Delivery date and return window
    delivered_date_str = order_data.get("delivered_date")
    if not delivered_date_str:
        logger.warning(f"[Refund Workflow - Step 2] No delivery date for order {order_id}")
        tool_context.actions.escalate = True
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": "Order delivery date not recorded. Please contact support.",
        }

    delivered_date = _parse_date(delivered_date_str)
    if not delivered_date:
        tool_context.actions.escalate = True
        return {"status": "error", "eligible": False, "reason": "Invalid delivery date format"}

    days_since_delivery = (datetime.now() - delivered_date).days

    if days_since_delivery > REFUND_WINDOW_DAYS:
        logger.warning(
            f"[Refund Workflow - Step 2] Order {order_id} past {REFUND_WINDOW_DAYS}-day window ({days_since_delivery} days)"
        )
        tool_context.actions.escalate = True
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": f"Order was delivered {days_since_delivery} days ago. Refunds must be requested within {REFUND_WINDOW_DAYS} days of delivery.",
            "delivered_date": delivered_date_str,
            "days_since_delivery": days_since_delivery,
            "refund_window_days": REFUND_WINDOW_DAYS,
        }

    # Check 2: Items not already refunded
    already_refunded_ids = _get_refunded_item_ids(order_id)
    eligible_items = []
    already_refunded_items = []

    for item in items_to_refund:
        product_id = item["product_id"]
        if product_id in already_refunded_ids:
            already_refunded_items.append(item)
        else:
            eligible_items.append(item)

    if not eligible_items:
        logger.warning("[Refund Workflow - Step 2] All requested items already refunded")
        tool_context.actions.escalate = True
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": "All requested items have already been refunded.",
            "already_refunded": [{"product_id": i["product_id"], "name": i["name"]} for i in already_refunded_items],
        }

    # Calculate refund amount for eligible items
    refund_amount = _calculate_refund_amount(eligible_items)

    # Store eligible items in session state
    tool_context.state["eligible_items"] = eligible_items
    tool_context.state["refund_amount"] = refund_amount

    logger.info(f"[Refund Workflow - Step 2] {len(eligible_items)} items eligible, refund amount: ${refund_amount}")

    result = {
        "status": "success",
        "eligible": True,
        "reason": f"Within {REFUND_WINDOW_DAYS}-day return window ({days_since_delivery} days since delivery)",
        "days_since_delivery": days_since_delivery,
        "days_remaining": REFUND_WINDOW_DAYS - days_since_delivery,
        "eligible_items": [
            {"product_id": item["product_id"], "name": item["name"], "qty": item.get("qty", 1), "price": item["price"]}
            for item in eligible_items
        ],
        "refund_amount": refund_amount,
    }

    # Warn about already refunded items
    if already_refunded_items:
        result["already_refunded"] = [
            {"product_id": i["product_id"], "name": i["name"]} for i in already_refunded_items
        ]
        result["partial_refund_note"] = "Some items were already refunded and are excluded."

    return result


def process_refund(order_id: str, reason: str, tool_context: ToolContext) -> dict:
    """
    Step 3: Process the refund.

    Creates refund record with:
    - Specific items being refunded
    - Refund amount
    - Customer tracking
    - Prevents duplicate refunds by recording refunded items
    - Validates refund reason is acceptable

    Args:
        order_id: The order ID to refund
        reason: The reason for the refund (must be product-related, e.g., "damaged", "defective", "wrong item")
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        dict with refund confirmation including refund_id and amount, or rejection if reason not acceptable
    """
    # Input validation
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    is_valid, error_msg = validate_refund_reason(reason)
    if not is_valid:
        return validation_error_response(error_msg)

    user_id = tool_context.user_id
    logger.info(f"[Refund Workflow - Step 3] Processing refund for order: {order_id}, reason: {reason}")

    # Validate refund reason FIRST (before any other processing)
    is_acceptable, reason_category, reason_explanation = _classify_refund_reason(reason)

    if not is_acceptable:
        logger.warning(f"[Refund Workflow - Step 3] Reason not acceptable: {reason} -> {reason_category}")
        audit_log(user_id, "process_refund", "order", order_id, False, f"Reason rejected: {reason_category}")
        return {
            "status": "reason_not_acceptable",
            "message": f"Sorry, '{reason}' is not an acceptable refund reason.",
            "explanation": reason_explanation,
            "policy": "Refunds are only granted for product-related issues (defective, damaged, wrong item, etc.).",
            "acceptable_reasons": list(ACCEPTABLE_REFUND_REASONS.keys()),
            "suggestion": "If your product has an actual issue, please describe the problem (e.g., 'product is defective', 'arrived damaged').",
        }

    logger.info(f"[Refund Workflow - Step 3] Reason accepted: {reason} -> {reason_category}")

    # Final ownership verification
    is_authorized, order_data, error_msg = verify_order_ownership(order_id, user_id, action="process_refund")

    if not is_authorized:
        logger.error(f"[Refund Workflow - Step 3] BLOCKED - {error_msg}")
        audit_log(user_id, "process_refund", "order", order_id, False, error_msg)
        return {"status": "error", "message": error_msg}

    # Get eligible items from session state (set by Step 2)
    eligible_items = tool_context.state.get("eligible_items")
    refund_amount = tool_context.state.get("refund_amount")

    if not eligible_items:
        # Fallback: recalculate if session state lost
        logger.warning("[Refund Workflow - Step 3] Session state lost, recalculating")
        eligible_items = order_data.get("items", [])
        refund_amount = _calculate_refund_amount(eligible_items)

        # Check for already refunded items
        already_refunded_ids = _get_refunded_item_ids(order_id)
        eligible_items = [i for i in eligible_items if i["product_id"] not in already_refunded_ids]

        if not eligible_items:
            audit_log(user_id, "process_refund", "order", order_id, False, "No eligible items")
            return {"status": "error", "message": "No eligible items to refund"}

        refund_amount = _calculate_refund_amount(eligible_items)

    # Generate refund ID
    existing_refunds = _get_existing_refunds(order_id)
    refund_sequence = len(existing_refunds) + 1
    refund_id = f"REF-{order_id.replace('ORD-', '')}-{refund_sequence:02d}"

    # Create refund record
    refund_record = {
        "refund_id": refund_id,
        "order_id": order_id,
        "customer_id": user_id,
        "reason": reason,
        "reason_category": reason_category,  # Categorized reason for analytics
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "items": [
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "qty": item.get("qty", 1),
                "price": item["price"],
                "refund_amount": item["price"] * item.get("qty", 1),
            }
            for item in eligible_items
        ],
        "total_refund_amount": refund_amount,
        "original_order_total": order_data.get("total", 0),
    }

    db_client.collection("refunds").document(refund_id).set(refund_record)

    audit_log(user_id, "process_refund", "order", order_id, True, f"Refund {refund_id} created for ${refund_amount}")

    logger.info(f"[Refund Workflow - Step 3] Refund {refund_id} created: ${refund_amount}")

    return {
        "status": "success",
        "refund_id": refund_id,
        "order_id": order_id,
        "refund_amount": refund_amount,
        "items_refunded": [
            {"product_id": i["product_id"], "name": i["name"], "price": i["price"]} for i in eligible_items
        ],
        "refund_status": "pending",
        "message": f"Refund of ${refund_amount} submitted successfully. Refund ID: {refund_id}",
        "estimated_processing": "3-5 business days",
    }


# =============================================================================
# Additional Helper Tool for Item-Level Queries
# =============================================================================


def check_if_refundable(order_id: str, tool_context: ToolContext) -> dict:
    """
    Pre-check if an order is eligible for refund (before asking for reason).

    This tool checks eligibility WITHOUT requiring a reason, enabling a smoother
    conversational flow:
    1. User requests refund with order_id
    2. System checks if refundable → if yes, asks for reason
    3. Only then processes the refund

    Checks performed:
    - Order exists and belongs to the authenticated user
    - Order status is "Delivered"
    - Within 30-day return window
    - Has items that haven't been refunded yet

    Args:
        order_id: The order ID to check (e.g., "ORD-12345")
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        dict with eligibility status and details:
        - If eligible: items that can be refunded, estimated amount, days remaining
        - If not eligible: clear reason why (not delivered, past window, already refunded)
    """
    # Input validation
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    user_id = tool_context.user_id
    logger.info(f"[Refund Pre-Check] User {user_id} checking if order {order_id} is refundable")

    # Check 1: Verify ownership
    is_authorized, order_data, error_msg = verify_order_ownership(order_id, user_id, action="check_if_refundable")

    if not is_authorized:
        logger.warning(f"[Refund Pre-Check] Not authorized: {error_msg}")
        return {"status": "not_eligible", "eligible": False, "reason": error_msg}

    # Check 2: Order must be delivered
    order_status = order_data.get("status", "").lower()
    if order_status != "delivered":
        logger.info(f"[Refund Pre-Check] Order {order_id} status is '{order_status}', not delivered")

        if order_status == "processing":
            return {
                "status": "not_eligible",
                "eligible": False,
                "reason": f"Order {order_id} is still being processed. You can cancel it instead of requesting a refund.",
                "current_status": order_status,
                "suggestion": "cancel_order",
            }
        elif order_status == "in transit":
            return {
                "status": "not_eligible",
                "eligible": False,
                "reason": f"Order {order_id} is currently in transit. Please wait until it's delivered to request a refund.",
                "current_status": order_status,
                "estimated_delivery": order_data.get("estimated_delivery"),
            }
        else:
            return {
                "status": "not_eligible",
                "eligible": False,
                "reason": f"Order {order_id} has status '{order_status}' and cannot be refunded.",
                "current_status": order_status,
            }

    # Check 3: Within 30-day return window
    delivered_date_str = order_data.get("delivered_date")
    if not delivered_date_str:
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": "Order delivery date not recorded. Please contact support.",
        }

    delivered_date = _parse_date(delivered_date_str)
    if not delivered_date:
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": "Invalid delivery date format. Please contact support.",
        }

    days_since_delivery = (datetime.now() - delivered_date).days

    if days_since_delivery > REFUND_WINDOW_DAYS:
        logger.info(
            f"[Refund Pre-Check] Order {order_id} past {REFUND_WINDOW_DAYS}-day window ({days_since_delivery} days)"
        )
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": f"Order was delivered {days_since_delivery} days ago. Refunds must be requested within {REFUND_WINDOW_DAYS} days of delivery.",
            "delivered_date": delivered_date_str,
            "days_since_delivery": days_since_delivery,
            "refund_window_days": REFUND_WINDOW_DAYS,
        }

    # Check 4: Has items that can be refunded
    order_items = order_data.get("items", [])
    already_refunded_ids = _get_refunded_item_ids(order_id)

    refundable_items = []
    already_refunded_items = []

    for item in order_items:
        if item["product_id"] in already_refunded_ids:
            already_refunded_items.append(item)
        else:
            refundable_items.append(item)

    if not refundable_items:
        logger.info(f"[Refund Pre-Check] All items in order {order_id} already refunded")
        return {
            "status": "not_eligible",
            "eligible": False,
            "reason": "All items in this order have already been refunded.",
            "already_refunded": [{"product_id": i["product_id"], "name": i["name"]} for i in already_refunded_items],
        }

    # All checks passed - order is eligible for refund
    refund_amount = _calculate_refund_amount(refundable_items)
    days_remaining = REFUND_WINDOW_DAYS - days_since_delivery

    logger.info(f"[Refund Pre-Check] Order {order_id} is eligible. {len(refundable_items)} items, ${refund_amount}")

    # Store in session state for later use
    tool_context.state["refund_eligible_order_id"] = order_id
    tool_context.state["refund_eligible_items"] = refundable_items
    tool_context.state["refund_eligible_amount"] = refund_amount

    result = {
        "status": "eligible",
        "eligible": True,
        "order_id": order_id,
        "message": f"Order {order_id} is eligible for refund.",
        "refundable_items": [
            {"product_id": item["product_id"], "name": item["name"], "qty": item.get("qty", 1), "price": item["price"]}
            for item in refundable_items
        ],
        "estimated_refund_amount": refund_amount,
        "days_remaining_in_window": days_remaining,
        "next_step": "Please provide the reason for your refund request.",
    }

    if already_refunded_items:
        result["already_refunded"] = [
            {"product_id": i["product_id"], "name": i["name"]} for i in already_refunded_items
        ]
        result["note"] = "Some items were already refunded and are excluded."

    return result


def get_refundable_items(order_id: str, tool_context: ToolContext) -> dict:
    """
    Get list of items in an order that can still be refunded.

    Useful when user asks "what can I refund from my order?"

    Args:
        order_id: The order ID to check
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        dict with list of refundable items and already refunded items
    """
    # Input validation
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    user_id = tool_context.user_id
    logger.info(f"[Refund Helper] User {user_id} checking refundable items for order: {order_id}")

    # Verify ownership
    is_authorized, order_data, error_msg = verify_order_ownership(order_id, user_id, action="get_refundable_items")

    if not is_authorized:
        return {"status": "error", "message": error_msg}

    # Check order status
    if order_data.get("status", "").lower() != "delivered":
        return {
            "status": "not_delivered",
            "message": f"Order {order_id} is not yet delivered. Status: {order_data.get('status')}",
            "order_status": order_data.get("status"),
        }

    # Check return window
    delivered_date = _parse_date(order_data.get("delivered_date", ""))
    if delivered_date:
        days_since_delivery = (datetime.now() - delivered_date).days
        if days_since_delivery > REFUND_WINDOW_DAYS:
            return {
                "status": "window_expired",
                "message": f"Return window expired. Order was delivered {days_since_delivery} days ago.",
                "refund_window_days": REFUND_WINDOW_DAYS,
            }
        days_remaining = REFUND_WINDOW_DAYS - days_since_delivery
    else:
        days_remaining = None

    # Get items
    order_items = order_data.get("items", [])
    already_refunded_ids = _get_refunded_item_ids(order_id)

    refundable = []
    already_refunded = []

    for item in order_items:
        item_info = {
            "product_id": item["product_id"],
            "name": item["name"],
            "qty": item.get("qty", 1),
            "price": item["price"],
        }
        if item["product_id"] in already_refunded_ids:
            already_refunded.append(item_info)
        else:
            refundable.append(item_info)

    return {
        "status": "success",
        "order_id": order_id,
        "refundable_items": refundable,
        "already_refunded_items": already_refunded,
        "days_remaining_in_window": days_remaining,
        "total_refundable_amount": _calculate_refund_amount(
            [i for i in order_items if i["product_id"] not in already_refunded_ids]
        ),
    }
