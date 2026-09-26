"""
Input validation utilities for tool parameters.

Provides consistent validation across all tools to prevent:
- DoS via extremely long inputs
- Injection attacks via special characters
- Invalid parameter formats
"""

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# VALIDATION CONSTANTS
# =============================================================================

MAX_QUERY_LENGTH = 500
MAX_REASON_LENGTH = 1000
MAX_ORDER_ID_LENGTH = 20
MAX_PRODUCT_ID_LENGTH = 20
MAX_INVOICE_ID_LENGTH = 30

# Patterns for valid IDs
ORDER_ID_PATTERN = re.compile(r"^ORD-\d{5,10}$")
PRODUCT_ID_PATTERN = re.compile(r"^PROD-\d{3,10}$")
INVOICE_ID_PATTERN = re.compile(r"^INV-\d{4}-\d{3,10}$")
REFUND_ID_PATTERN = re.compile(r"^REF-\d{5,10}-\d{2}$")

# Safe characters for search queries (alphanumeric, spaces, basic punctuation)
SAFE_QUERY_PATTERN = re.compile(r'^[a-zA-Z0-9\s\-$.,!?\'"()]+$')


# =============================================================================
# VALIDATION FUNCTIONS
# =============================================================================


def validate_order_id(order_id: str) -> Tuple[bool, Optional[str]]:
    """
    Validate an order ID.

    Args:
        order_id: The order ID to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not order_id:
        return False, "Order ID is required"

    if not isinstance(order_id, str):
        return False, "Order ID must be a string"

    order_id = order_id.strip()

    if len(order_id) > MAX_ORDER_ID_LENGTH:
        return False, f"Order ID too long (max {MAX_ORDER_ID_LENGTH} characters)"

    if not ORDER_ID_PATTERN.match(order_id):
        return False, "Invalid order ID format. Expected: ORD-XXXXX (e.g., ORD-12345)"

    return True, None


def validate_product_id(product_id: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a product ID.

    Args:
        product_id: The product ID to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not product_id:
        return False, "Product ID is required"

    if not isinstance(product_id, str):
        return False, "Product ID must be a string"

    product_id = product_id.strip()

    if len(product_id) > MAX_PRODUCT_ID_LENGTH:
        return False, f"Product ID too long (max {MAX_PRODUCT_ID_LENGTH} characters)"

    if not PRODUCT_ID_PATTERN.match(product_id):
        return False, "Invalid product ID format. Expected: PROD-XXX (e.g., PROD-001)"

    return True, None


def validate_invoice_id(invoice_id: str) -> Tuple[bool, Optional[str]]:
    """
    Validate an invoice ID.

    Args:
        invoice_id: The invoice ID to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not invoice_id:
        return False, "Invoice ID is required"

    if not isinstance(invoice_id, str):
        return False, "Invoice ID must be a string"

    invoice_id = invoice_id.strip()

    if len(invoice_id) > MAX_INVOICE_ID_LENGTH:
        return False, f"Invoice ID too long (max {MAX_INVOICE_ID_LENGTH} characters)"

    if not INVOICE_ID_PATTERN.match(invoice_id):
        return False, "Invalid invoice ID format. Expected: INV-YYYY-XXX (e.g., INV-2025-001)"

    return True, None


def validate_search_query(query: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a search query string.

    Args:
        query: The search query to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not query:
        return False, "Search query is required"

    if not isinstance(query, str):
        return False, "Search query must be a string"

    query = query.strip()

    if len(query) == 0:
        return False, "Search query cannot be empty"

    if len(query) > MAX_QUERY_LENGTH:
        return False, f"Search query too long (max {MAX_QUERY_LENGTH} characters)"

    if not SAFE_QUERY_PATTERN.match(query):
        return False, "Search query contains invalid characters"

    return True, None


def validate_refund_reason(reason: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a refund reason string.

    Args:
        reason: The refund reason to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not reason:
        return False, "Refund reason is required"

    if not isinstance(reason, str):
        return False, "Refund reason must be a string"

    reason = reason.strip()

    if len(reason) == 0:
        return False, "Refund reason cannot be empty"

    if len(reason) > MAX_REASON_LENGTH:
        return False, f"Refund reason too long (max {MAX_REASON_LENGTH} characters)"

    # Allow more characters in reason (users describe problems)
    if not re.match(r'^[a-zA-Z0-9\s\-.,!?\'"()/:;]+$', reason):
        return False, "Refund reason contains invalid characters"

    return True, None


def sanitize_string(value: str, max_length: int = 500) -> str:
    """
    Sanitize a string by stripping and truncating.

    Args:
        value: The string to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized string
    """
    if not value or not isinstance(value, str):
        return ""

    return value.strip()[:max_length]


# =============================================================================
# VALIDATION ERROR RESPONSE HELPER
# =============================================================================


def validation_error_response(error_message: str) -> dict:
    """
    Create a standardized validation error response.

    Args:
        error_message: The validation error message

    Returns:
        Error response dict
    """
    logger.warning(f"[VALIDATION] {error_message}")
    return {"status": "validation_error", "message": error_message}
