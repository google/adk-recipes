"""Mocked SAP connector (PRD §5: "SAP connector (mocked for POC)").

Represents the billing-doc lookup that would, in production, be a real SAP
RFC/OData integration. For the POC it just returns canned "blocked invoice"
data so the agent has a reason to start investigating a customer.
"""

from __future__ import annotations

from typing import Any

_MOCK_BLOCKED_INVOICES: dict[str, dict[str, Any]] = {
    "acme corp": {
        "invoice_id": "INV-Q3-8842",
        "customer": "Acme Corp",
        "status": "BLOCKED",
        "block_reason": "Payment term could not be confirmed for Product X.",
    },
    "globex inc.": {
        "invoice_id": "INV-Q3-9910",
        "customer": "Globex Inc.",
        "status": "BLOCKED",
        "block_reason": "Missing customer ID.",
    },
}


def check_sap_invoice_status(customer: str) -> dict[str, Any]:
    """Looks up whether a customer has a SAP-blocked invoice (mocked).

    Args:
      customer: Customer/company name, e.g. "Acme Corp".

    Returns:
      dict describing the blocked invoice, or {"status": "NOT_FOUND"} if
      this customer has no blocked invoice in the mock data.
    """
    query = customer.strip().lower()
    for key, record in _MOCK_BLOCKED_INVOICES.items():
        if query in key or key in query:
            return record
    return {"status": "NOT_FOUND", "customer": customer}
