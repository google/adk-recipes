"""
Order-related tools for the customer support system.

This module contains all tools for order tracking and order history.
All tools verify ownership using decorators - users can only access their own orders.
"""

import logging

from google.adk.tools.tool_context import ToolContext
from google.cloud.firestore_v1.base_query import FieldFilter

from customer_support_mas.auth import (
    requires_authenticated_user,
    requires_order_ownership,
)
from customer_support_mas.database import db_client
from customer_support_mas.validation import (
    validate_order_id,
    validation_error_response,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ORDER TRACKING (requires ownership)
# =============================================================================


@requires_order_ownership
def track_order(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Track an order by order ID. Only accessible if the order belongs to you.

    Args:
        order_id: The order ID to track (e.g., "ORD-12345")
        tool_context: ADK ToolContext (automatically injected)
        _order_data: Pre-fetched order data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    # _order_data is already fetched and ownership verified by decorator
    return {
        "status": "success",
        "order": {
            "order_id": order_id,
            "status": _order_data.get("status"),
            "carrier": _order_data.get("carrier"),
            "tracking_number": _order_data.get("tracking_number"),
            "estimated_delivery": _order_data.get("estimated_delivery"),
            "timeline": _order_data.get("timeline", []),
        },
    }


@requires_order_ownership
def get_order_details(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Get full details for a specific order. Only accessible if the order belongs to you.

    Args:
        order_id: The order ID to get details for (e.g., "ORD-12345")
        tool_context: ADK ToolContext (automatically injected)
        _order_data: Pre-fetched order data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    return {
        "status": "success",
        "order": {
            "order_id": order_id,
            "date": _order_data.get("date"),
            "status": _order_data.get("status"),
            "items": _order_data.get("items", []),
            "subtotal": _order_data.get("subtotal"),
            "tax": _order_data.get("tax"),
            "total": _order_data.get("total"),
            "carrier": _order_data.get("carrier"),
            "tracking_number": _order_data.get("tracking_number"),
            "estimated_delivery": _order_data.get("estimated_delivery"),
            "delivered_date": _order_data.get("delivered_date"),
            "shipping_address": _order_data.get("shipping_address"),
            "timeline": _order_data.get("timeline", []),
        },
    }


# =============================================================================
# ORDER HISTORY (authenticated user - no specific order ID)
# =============================================================================


@requires_authenticated_user
def get_order_history(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get complete order history for the authenticated user with full details.

    Returns all orders with items, totals, and shipping information.

    Args:
        tool_context: ADK ToolContext (automatically injected)
        _user_id: Authenticated user ID (injected by decorator)
    """
    logger.info(f"[ORDER HISTORY] Fetching full order history for user: {_user_id}")

    query = db_client.collection("orders").where(filter=FieldFilter("customer_id", "==", _user_id))
    orders = [{"order_id": doc.id, **doc.to_dict()} for doc in query.stream()]

    if orders:
        detailed_orders = []
        for o in orders:
            detailed_orders.append(
                {
                    "order_id": o["order_id"],
                    "date": o.get("date"),
                    "status": o.get("status"),
                    "total": o.get("total"),
                    "items": o.get("items", []),
                    "carrier": o.get("carrier"),
                    "tracking_number": o.get("tracking_number"),
                    "shipping_address": o.get("shipping_address"),
                }
            )

        logger.info(f"[ORDER HISTORY] Found {len(detailed_orders)} orders for user {_user_id}")
        return {
            "status": "success",
            "orders": detailed_orders,
            "total_orders": len(detailed_orders),
        }

    logger.info(f"[ORDER HISTORY] No orders found for user {_user_id}")
    return {
        "status": "no_orders",
        "message": "No orders found for your account.",
    }


@requires_authenticated_user
def get_my_order_history(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get order history summary for the authenticated user.

    Returns a brief summary of all orders (ID, date, total, status).
    Use get_order_history() for full details including items.

    Args:
        tool_context: ADK ToolContext (automatically injected)
        _user_id: Authenticated user ID (injected by decorator)
    """
    logger.info(f"[ORDER HISTORY] Fetching order summary for user: {_user_id}")

    query = db_client.collection("orders").where(filter=FieldFilter("customer_id", "==", _user_id))
    orders = [{"order_id": doc.id, **doc.to_dict()} for doc in query.stream()]

    if orders:
        summaries = [
            {"order_id": o["order_id"], "date": o.get("date"), "total": o.get("total"), "status": o.get("status")}
            for o in orders
        ]
        logger.info(f"[ORDER HISTORY] Found {len(summaries)} orders for user {_user_id}")
        return {"status": "success", "orders": summaries}

    logger.info(f"[ORDER HISTORY] No orders found for user {_user_id}")
    return {
        "status": "no_orders",
        "message": "No orders found for your account.",
    }
