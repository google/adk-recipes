"""
Billing-related tools for the customer support system.

This module contains all tools for invoices, payments, and refunds.
All tools verify ownership using decorators - users can only access their own billing data.
"""

import logging

from google.adk.tools.tool_context import ToolContext
from google.cloud.firestore_v1.base_query import FieldFilter

from customer_support_mas.auth import (
    requires_authenticated_user,
    requires_invoice_ownership,
    requires_order_ownership,
)
from customer_support_mas.database import db_client
from customer_support_mas.validation import (
    validate_invoice_id,
    validate_order_id,
    validation_error_response,
)

logger = logging.getLogger(__name__)


# =============================================================================
# INVOICE TOOLS (ownership verified)
# =============================================================================


@requires_invoice_ownership
def get_invoice(invoice_id: str, tool_context: ToolContext, _invoice_data: dict = None, **kwargs) -> dict:
    """Get invoice by invoice ID (e.g., INV-2025-001). Only accessible if the invoice belongs to you.

    Args:
        invoice_id: The invoice ID to retrieve
        tool_context: ADK ToolContext (automatically injected)
        _invoice_data: Pre-fetched invoice data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_invoice_id(invoice_id)
    if not is_valid:
        return validation_error_response(error_msg)

    return {"status": "success", "invoice": {"invoice_id": invoice_id, **_invoice_data}}


@requires_order_ownership
def get_invoice_by_order_id(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Get invoice by order ID (e.g., ORD-12345). Only accessible if the order belongs to you.

    Use this when customer asks for invoice for a specific order.

    Args:
        order_id: The order ID to get the invoice for
        tool_context: ADK ToolContext (automatically injected)
        _order_data: Pre-fetched order data (injected by decorator)
    """
    # Input validation (decorator handles authorization after this)
    is_valid, error_msg = validate_order_id(order_id)
    if not is_valid:
        return validation_error_response(error_msg)

    # Ownership already verified by decorator
    # Now fetch the invoice for this order
    query = db_client.collection("invoices").where(filter=FieldFilter("order_id", "==", order_id))
    invoices = list(query.stream())

    if invoices:
        doc = invoices[0]  # Assume one invoice per order
        return {"status": "success", "invoice": {"invoice_id": doc.id, **doc.to_dict()}}

    return {"status": "not_found", "message": f"No invoice found for order {order_id}"}


@requires_authenticated_user
def get_my_invoices(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get all invoices for the authenticated user.

    Args:
        tool_context: ADK ToolContext (automatically injected)
        _user_id: Authenticated user ID (injected by decorator)
    """
    logger.info(f"[BILLING] Fetching all invoices for user: {_user_id}")

    query = db_client.collection("invoices").where(filter=FieldFilter("customer_id", "==", _user_id))
    invoices = [{"invoice_id": doc.id, **doc.to_dict()} for doc in query.stream()]

    if invoices:
        logger.info(f"[BILLING] Found {len(invoices)} invoices for user {_user_id}")
        return {
            "status": "success",
            "invoices": invoices,
            "total_invoices": len(invoices),
        }

    logger.info(f"[BILLING] No invoices found for user {_user_id}")
    return {
        "status": "no_invoices",
        "message": "No invoices found for your account.",
    }


# =============================================================================
# PAYMENT TOOLS (ownership verified)
# =============================================================================


@requires_order_ownership
def check_payment_status(order_id: str, tool_context: ToolContext, _order_data: dict = None, **kwargs) -> dict:
    """Check payment status for an order. Only accessible if the order belongs to you.

    Args:
        order_id: The order ID to check payment status for
        tool_context: ADK ToolContext (automatically injected)
        _order_data: Pre-fetched order data (injected by decorator)
    """
    # Ownership already verified by decorator
    # Now fetch the payment status
    doc = db_client.collection("payments").document(order_id).get()

    if doc.exists:
        return {"status": "success", "payment": {"order_id": doc.id, **doc.to_dict()}}

    return {"status": "not_found", "message": f"No payment record found for order {order_id}"}


@requires_authenticated_user
def get_my_payments(tool_context: ToolContext, _user_id: str = None, **kwargs) -> dict:
    """Get all payment records for the authenticated user.

    Args:
        tool_context: ADK ToolContext (automatically injected)
        _user_id: Authenticated user ID (injected by decorator)
    """
    logger.info(f"[BILLING] Fetching all payments for user: {_user_id}")

    query = db_client.collection("payments").where(filter=FieldFilter("customer_id", "==", _user_id))
    payments = [{"order_id": doc.id, **doc.to_dict()} for doc in query.stream()]

    if payments:
        logger.info(f"[BILLING] Found {len(payments)} payments for user {_user_id}")
        return {
            "status": "success",
            "payments": payments,
            "total_payments": len(payments),
        }

    logger.info(f"[BILLING] No payments found for user {_user_id}")
    return {
        "status": "no_payments",
        "message": "No payment records found for your account.",
    }
