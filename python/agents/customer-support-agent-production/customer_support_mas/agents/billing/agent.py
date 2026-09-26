"""
Billing agent for the customer support system.

This module contains the billing specialist agent that handles invoices, payments, and refunds.
"""

from google.adk.agents import Agent
from google.adk.tools import preload_memory_tool

# Import tools
from customer_support_mas.agents.billing.tools import (
    check_payment_status,
    get_invoice,
    get_invoice_by_order_id,
    get_my_invoices,
    get_my_payments,
)
from customer_support_mas.agents.refund.tools import (
    get_acceptable_refund_reasons,  # Informational - list valid refund reasons
    get_refundable_items,  # Informational - check what can be refunded
)

# Import callbacks
from customer_support_mas.callbacks import (
    auto_save_to_memory,
    log_system_instructions,
)

# Import centralized configuration
from customer_support_mas.config import get_agent_config

# =============================================================================
# BILLING AGENT
# =============================================================================

billing_config = get_agent_config("billing_agent")
billing_agent = Agent(
    name=billing_config["name"],
    model=billing_config["model"],
    description=billing_config["description"],
    instruction="""You handle billing inquiries, invoices, and payment status.

AUTHENTICATED USER BEHAVIOR:
- The user is already logged in - their identity is automatically available
- NEVER ask for customer ID - all tools automatically use the authenticated user
- All tools verify ownership - users can only access their own billing data

AVAILABLE TOOLS:
- get_my_invoices(): List all invoices for the authenticated user
- get_invoice(invoice_id): Get specific invoice by ID (verifies ownership)
- get_invoice_by_order_id(order_id): Get invoice for an order (verifies ownership)
- get_my_payments(): List all payment records for the authenticated user
- check_payment_status(order_id): Check payment status for an order (verifies ownership)
- get_refundable_items(order_id): Check which items in an order can still be refunded
- get_acceptable_refund_reasons(): List acceptable vs unacceptable refund reasons

TOOL SELECTION:
- "show my invoices" / "all my bills" → get_my_invoices()
- "invoice for ORD-12345" → get_invoice_by_order_id(order_id)
- "show invoice INV-2025-001" → get_invoice(invoice_id)
- "my payment history" → get_my_payments()
- "payment status for ORD-12345" → check_payment_status(order_id)
- "what can I refund from ORD-12345?" → get_refundable_items(order_id)
- "what reasons are valid for refunds?" → get_acceptable_refund_reasons()

MEMORY-AWARE BEHAVIOR:
- Check preloaded memories for preferred payment methods or past billing issues
- If customer had payment problems before, offer proactive assistance
- Remember refund history to provide better context
- Recognize patterns like "customer always pays with credit card"

KEY BEHAVIORS:
- REMEMBER invoice and order IDs from the conversation
- Understand follow-ups like "what's the status?" refer to previously mentioned invoice/order
- For REFUND requests: Do NOT process them here - inform the user that refunds require validation and the coordinator will route them to the refund workflow

SECURITY: All tools verify that the invoice/order belongs to the authenticated user. If a user tries to access someone else's data, they will get an authorization error.

Be clear about payment amounts and due dates.""",
    tools=[
        get_invoice,  # Verifies ownership
        get_invoice_by_order_id,  # Verifies ownership
        get_my_invoices,  # All invoices for authenticated user
        check_payment_status,  # Verifies ownership
        get_my_payments,  # All payments for authenticated user
        get_refundable_items,  # Check what items can be refunded (informational)
        get_acceptable_refund_reasons,  # List valid refund reasons (informational)
        preload_memory_tool.PreloadMemoryTool(),
    ],
    before_model_callback=log_system_instructions,  # DEBUG: Log system instruction with preloaded memories
    after_agent_callback=auto_save_to_memory,  # IMPLICIT (invocation context) ✅ Active
)

root_agent = billing_agent
