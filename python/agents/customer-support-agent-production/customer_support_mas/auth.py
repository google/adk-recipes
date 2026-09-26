"""
Authorization decorators for tool ownership verification.

Production-ready patterns:
- Single database fetch (no redundant calls)
- Audit logging for security compliance
- Clean decorator syntax
- Reusable across all tools
"""

import logging
from functools import wraps
from typing import Callable, Optional

from google.adk.tools.tool_context import ToolContext

from customer_support_mas.database import db_client

logger = logging.getLogger(__name__)


# =============================================================================
# AUDIT LOGGING
# =============================================================================


def audit_log(
    user_id: str, action: str, resource_type: str, resource_id: str, success: bool, details: Optional[str] = None
):
    """
    Log access attempts for security compliance.

    In production, this would write to a secure audit log (e.g., Cloud Logging,
    SIEM system, or dedicated audit database).

    Args:
        user_id: The user attempting the action
        action: The action being attempted (e.g., "view_order", "request_refund")
        resource_type: The type of resource (e.g., "order", "invoice")
        resource_id: The ID of the resource
        success: Whether the action was authorized
        details: Additional context
    """
    if success:
        logger.info(f"[AUDIT] AUTHORIZED: {user_id} -> {action} on {resource_type}/{resource_id}")
    else:
        logger.warning(f"[AUDIT] DENIED: {user_id} -> {action} on {resource_type}/{resource_id} - {details}")


# =============================================================================
# OWNERSHIP VERIFICATION DECORATORS
# =============================================================================


def requires_order_ownership(func: Callable) -> Callable:
    """
    Decorator that verifies the user owns the order before executing the tool.

    - Fetches the order once and passes it to the function via `_order_data`
    - Logs all access attempts for audit
    - Returns error response if unauthorized

    Usage:
        @requires_order_ownership
        def track_order(order_id: str, tool_context: ToolContext, _order_data: dict = None):
            # _order_data is automatically populated with the order document
            return {"status": "success", "order": _order_data}
    """

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        # Extract tool_context and order_id from arguments
        tool_context = kwargs.get("tool_context")
        order_id = kwargs.get("order_id")

        # Also check positional args if not in kwargs
        if tool_context is None:
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break

        if order_id is None and args:
            # First string arg is likely order_id
            for arg in args:
                if isinstance(arg, str) and arg.startswith("ORD-"):
                    order_id = arg
                    break

        if tool_context is None:
            logger.error(f"[AUTH] No tool_context provided to {func.__name__}")
            return {"status": "error", "message": "Internal error: missing context"}

        user_id = tool_context.user_id
        action = func.__name__

        # Fetch the order
        doc = db_client.collection("orders").document(order_id).get()

        if not doc.exists:
            audit_log(user_id, action, "order", order_id, False, "Order not found")
            return {"status": "error", "message": f"Order {order_id} not found"}

        order_data = doc.to_dict()
        order_customer_id = order_data.get("customer_id")

        # Verify ownership
        if order_customer_id != user_id:
            audit_log(user_id, action, "order", order_id, False, f"Belongs to {order_customer_id}")
            return {"status": "error", "message": f"You don't have permission to access order {order_id}"}

        # Authorized - log and execute
        audit_log(user_id, action, "order", order_id, True)

        # Inject the fetched data to avoid re-fetching
        kwargs["_order_data"] = order_data
        kwargs["_order_id"] = order_id

        return func(*args, **kwargs)

    return wrapper


def requires_invoice_ownership(func: Callable) -> Callable:
    """
    Decorator that verifies the user owns the invoice before executing the tool.

    Usage:
        @requires_invoice_ownership
        def get_invoice(invoice_id: str, tool_context: ToolContext, _invoice_data: dict = None):
            return {"status": "success", "invoice": _invoice_data}
    """

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        tool_context = kwargs.get("tool_context")
        invoice_id = kwargs.get("invoice_id")

        if tool_context is None:
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break

        if invoice_id is None and args:
            for arg in args:
                if isinstance(arg, str) and arg.startswith("INV-"):
                    invoice_id = arg
                    break

        if tool_context is None:
            return {"status": "error", "message": "Internal error: missing context"}

        user_id = tool_context.user_id
        action = func.__name__

        # Fetch the invoice
        doc = db_client.collection("invoices").document(invoice_id).get()

        if not doc.exists:
            audit_log(user_id, action, "invoice", invoice_id, False, "Invoice not found")
            return {"status": "error", "message": f"Invoice {invoice_id} not found"}

        invoice_data = doc.to_dict()
        invoice_customer_id = invoice_data.get("customer_id")

        # Verify ownership
        if invoice_customer_id != user_id:
            audit_log(user_id, action, "invoice", invoice_id, False, f"Belongs to {invoice_customer_id}")
            return {"status": "error", "message": f"You don't have permission to access invoice {invoice_id}"}

        # Authorized
        audit_log(user_id, action, "invoice", invoice_id, True)
        kwargs["_invoice_data"] = invoice_data
        kwargs["_invoice_id"] = invoice_id

        return func(*args, **kwargs)

    return wrapper


def requires_authenticated_user(func: Callable) -> Callable:
    """
    Decorator that ensures the user is authenticated.
    Extracts user_id and passes it to the function.

    Usage:
        @requires_authenticated_user
        def get_my_orders(tool_context: ToolContext, _user_id: str = None):
            # _user_id is automatically populated
            ...
    """

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        tool_context = kwargs.get("tool_context")

        if tool_context is None:
            for arg in args:
                if isinstance(arg, ToolContext):
                    tool_context = arg
                    break

        if tool_context is None:
            return {"status": "error", "message": "Internal error: missing context"}

        user_id = tool_context.user_id

        if not user_id:
            logger.warning(f"[AUTH] Unauthenticated access attempt to {func.__name__}")
            return {"status": "error", "message": "Authentication required"}

        # Log the action
        logger.info(f"[AUTH] User {user_id} calling {func.__name__}")

        kwargs["_user_id"] = user_id
        return func(*args, **kwargs)

    return wrapper


# =============================================================================
# HELPER: Verify ownership without decorator (for workflow tools)
# =============================================================================


def verify_order_ownership(order_id: str, user_id: str, action: str = "access") -> tuple[bool, Optional[dict], str]:
    """
    Verify that an order belongs to the authenticated user.

    Use this in workflow tools where decorators don't fit well
    (e.g., SequentialAgent tools that need to control escalation).

    Args:
        order_id: The order ID to check
        user_id: The authenticated user's ID
        action: The action for audit logging

    Returns:
        tuple: (is_authorized, order_data, error_message)
    """
    doc = db_client.collection("orders").document(order_id).get()

    if not doc.exists:
        audit_log(user_id, action, "order", order_id, False, "Order not found")
        return False, None, f"Order {order_id} not found"

    order_data = doc.to_dict()
    order_customer_id = order_data.get("customer_id")

    if order_customer_id != user_id:
        audit_log(user_id, action, "order", order_id, False, f"Belongs to {order_customer_id}")
        return False, None, f"You don't have permission to access order {order_id}"

    audit_log(user_id, action, "order", order_id, True)
    return True, order_data, ""
