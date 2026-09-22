# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""The recipe's tools, over a small in-memory order book.

Every tool appends to `EXECUTED` as its first statement. That list is
how the demo and the tests show that a refused call was refused *before*
its body ran, rather than run and then reported. Nothing else in the
recipe depends on it; delete it when you adapt this to your own tools.

Two layers, not one: `attenu-guard` decides whether an agent may call a
tool at all, and under which ceilings (`app/permissions.py`) — that
check runs at the ADK callback, before a tool body is ever entered. It
does not, and should not, know what a valid `amount_cents` is or which
address is the right one for a given invoice; that is ordinary
input validation, and it belongs here, inside the tool body, the same
as it would in a tool with no delegation guard at all. Every value
below is something the model chose, so every one of them is untrusted
whether or not the caller was authorized to place the call.
"""

from typing import Any

# (tool_name, notable_argument) for every tool body that actually ran.
EXECUTED: list[tuple[str, Any]] = []


def reset() -> None:
    """Clear the execution record between runs."""
    EXECUTED.clear()


# One order, one invoice, one amount: the demo refund is for the full invoice.
INVOICE_TOTAL_CENTS = 48000

_ORDERS = {
    "ORD-8812": {
        "customer": "Ada Ellis",
        "email": "ada@attenu-io.com",
        "invoice_id": "INV-4471",
        "total_cents": INVOICE_TOTAL_CENTS,
        "status": "delivered",
    }
}

_INVOICES = {
    "INV-4471": {
        "order_id": "ORD-8812",
        "total_cents": INVOICE_TOTAL_CENTS,
        "paid": True,
        "issued": "2026-07-02",
    }
}


def lookup_order(order_id: str) -> dict:
    """Look up one order by its identifier.

    Args:
        order_id: the order reference, e.g. "ORD-8812".
    """
    EXECUTED.append(("lookup_order", order_id))
    return _ORDERS.get(order_id, {"error": "unknown order"})


def get_invoice(invoice_id: str) -> dict:
    """Read one invoice.

    Args:
        invoice_id: the invoice reference, e.g. "INV-4471".
    """
    EXECUTED.append(("get_invoice", invoice_id))
    return _INVOICES.get(invoice_id, {"error": "unknown invoice"})


def issue_refund(invoice_id: str, amount_cents: int) -> dict:
    """Move money back to the customer. Irreversible.

    Args:
        invoice_id: the invoice to refund.
        amount_cents: how much to refund, in cents. Must be a positive
            integer no greater than the invoice's own total — the model
            names an amount, but this body is what holds it to a real
            one, not attenu-guard (see the module docstring).
    """
    EXECUTED.append(("issue_refund", (invoice_id, amount_cents)))
    invoice = _INVOICES.get(invoice_id)
    if invoice is None:
        return {"error": "unknown invoice", "invoice_id": invoice_id}
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        return {"error": "invalid amount", "invoice_id": invoice_id}
    if amount_cents > invoice["total_cents"]:
        return {
            "error": "amount exceeds invoice total",
            "invoice_id": invoice_id,
            "invoice_total_cents": invoice["total_cents"],
        }
    return {"refunded": amount_cents, "invoice_id": invoice_id}


def email_customer(invoice_id: str, body: str) -> dict:
    """Notify the customer on an invoice.

    The recipient is not a model argument: it is looked up from the
    invoice's order record, so the model cannot redirect the
    notification to an address of its own choosing.

    Args:
        invoice_id: the invoice whose customer to notify.
        body: message body.
    """
    # `body` is read into EXECUTED below rather than only accepted —
    # it is part of the declared tool signature (ADK builds the
    # model-facing function declaration from it), and this in-memory
    # stub does not send anything, so recording it is what stands in
    # for using it.
    EXECUTED.append(("email_customer", (invoice_id, body)))
    invoice = _INVOICES.get(invoice_id)
    if invoice is None:
        return {"error": "unknown invoice", "invoice_id": invoice_id}
    order = _ORDERS.get(invoice["order_id"])
    if order is None:
        return {"error": "unknown order", "invoice_id": invoice_id}
    return {"sent_to": order["email"]}
