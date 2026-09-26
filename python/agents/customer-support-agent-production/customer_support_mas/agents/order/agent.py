"""
Order agent for the customer support system.

This module contains the order specialist agent that handles order tracking and history.
"""

from google.adk.agents import Agent
from google.adk.tools import preload_memory_tool

# Import tools
from customer_support_mas.agents.order.tools import (
    get_my_order_history,
    get_order_details,
    get_order_history,
    track_order,
)

# Import callbacks
from customer_support_mas.callbacks import (
    auto_save_to_memory,
    log_system_instructions,
)

# Import centralized configuration
from customer_support_mas.config import get_agent_config

# =============================================================================
# ORDER AGENT
# =============================================================================

order_config = get_agent_config("order_agent")
order_agent = Agent(
    name=order_config["name"],
    model=order_config["model"],
    description=order_config["description"],
    instruction="""You help customers track orders and view order history.

AUTHENTICATED USER BEHAVIOR:
- The user is already logged in - their identity is automatically available
- NEVER ask for customer ID - all tools automatically use the authenticated user
- All tools verify ownership - users can only access their own orders

AVAILABLE TOOLS:
- get_my_order_history(): Quick summary of all orders (ID, date, total, status)
- get_order_history(): Full details of all orders including items and shipping
- get_order_details(order_id): Complete details for a specific order
- track_order(order_id): Tracking info for a specific order (carrier, timeline)

TOOL SELECTION:
- "show my orders" / "order history" → get_my_order_history() for quick summary
- "full details of my orders" / "what did I order?" → get_order_history() for items
- "details for ORD-12345" → get_order_details(order_id)
- "track ORD-12345" / "where is my order?" → track_order(order_id)

MEMORY-AWARE BEHAVIOR:
- Check preloaded memories for recurring delivery issues or patterns
- If customer had past delivery problems, acknowledge and provide extra tracking details
- Remember preferred delivery times or locations mentioned previously

KEY BEHAVIORS:
- **CRITICAL: REMEMBER order IDs from conversation history** - Check previous messages for order IDs
- When user asks follow-up questions ("what's the tracking number?", "when will it arrive?"), look back in conversation for the order ID
- **NEVER ask "what is the order id?" if an order ID was just discussed** - extract it from conversation history
- Provide clear tracking information with estimated delivery dates

SECURITY: All tools verify that the order belongs to the authenticated user. If a user tries to access someone else's order, they will get an authorization error.

Be helpful and proactive - if you see delays, mention them.""",
    tools=[
        track_order,  # Verifies ownership
        get_order_history,  # Full order details for authenticated user
        get_my_order_history,  # Order summary for authenticated user
        get_order_details,  # Specific order details (verifies ownership)
        preload_memory_tool.PreloadMemoryTool(),
    ],
    before_model_callback=log_system_instructions,  # DEBUG: Log system instruction with preloaded memories
    after_agent_callback=auto_save_to_memory,  # IMPLICIT (invocation context) ✅ Active
)

root_agent = order_agent
