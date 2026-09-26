"""
Workflow agents for the customer support system.

This module contains ParallelAgent, SequentialAgent, and LoopAgent patterns.
"""

from google.adk.agents import Agent, SequentialAgent

# Import tools
from customer_support_mas.agents.refund.tools import (
    check_refund_eligibility,  # Step 2: Dynamic eligibility check
    process_refund,  # Step 3: Creates refund record
    validate_refund_request,  # Step 1: Validates ownership, delivery, items
)

# Import centralized configuration
from customer_support_mas.config import get_agent_config

validator_config = get_agent_config("order_validator")
validation_agent = Agent(
    name=validator_config["name"],
    model=validator_config["model"],
    description="Validates refund request: ownership, delivery status, and items in order",
    instruction=validator_config["instruction"],
    tools=[validate_refund_request],
)

eligibility_config = get_agent_config("eligibility_checker")
eligibility_agent = Agent(
    name=eligibility_config["name"],
    model=eligibility_config["model"],
    description="Checks if the order is eligible for a refund based on business rules",
    instruction=eligibility_config["instruction"],
    tools=[check_refund_eligibility],
)

refund_config = get_agent_config("refund_processor")
refund_processor = Agent(
    name=refund_config["name"],
    model=refund_config["model"],
    description="Processes the refund after validation and eligibility checks pass",
    instruction=refund_config["instruction"],
    tools=[process_refund],
)

sequential_refund_workflow = SequentialAgent(
    name="refund_workflow",
    description="""Validated refund processing workflow with comprehensive item verification.

WORKFLOW STEPS:
1. Validate Request - Verify ownership, delivery status, and items exist in order
2. Check Eligibility - Dynamic check: 30-day return window, items not already refunded
3. Process Refund - Create refund record with item tracking to prevent duplicates

SECURITY:
- Each step verifies the user owns the order (defense in depth)
- Users can only refund their own orders
- Audit logging at each step

FEATURES:
- Partial refunds: Refund specific items with item_ids parameter
- Duplicate prevention: Already-refunded items are automatically excluded
- Dynamic eligibility: Calculated from delivery date, not static records

EXPECTED INPUT:
- Order ID (e.g., "I want a refund for order ORD-12345")
- Reason (e.g., "broken item", "defective", "wrong item")
- Optional: item_ids list for partial refund

VALIDATION GATES:
If validation fails at any step, the workflow stops immediately with clear error message.""",
    sub_agents=[validation_agent, eligibility_agent, refund_processor],
)  # Sub-agents execute one-by-one with validation gates

root_agent = sequential_refund_workflow
