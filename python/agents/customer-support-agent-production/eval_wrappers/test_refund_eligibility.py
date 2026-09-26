"""Test agent with only check_if_refundable for eligibility unit tests.

The check_if_refundable tool lives on the root agent, but for unit testing
we create a minimal agent that only has this tool, avoiding root agent
complexity (memory callbacks, sub-agent routing, etc.).
"""

from google.adk.agents import Agent

from customer_support_mas.agents.refund.tools import check_if_refundable
from customer_support_mas.config import get_agent_config

root_config = get_agent_config("root_agent")

agent = Agent(
    name="refund_eligibility_tester",
    model=root_config["model"],
    instruction=(
        "You help users check if their orders are eligible for refunds. "
        "When a user asks about a refund, use the check_if_refundable tool "
        "with the order ID they provide."
    ),
    tools=[check_if_refundable],
)
