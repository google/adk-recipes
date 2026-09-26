"""Thin wrapper to expose root_agent as `agent` for AgentEvaluator.

Disables the memory bank callback to avoid Vertex AI Memory Bank API calls
during testing. The memory callback is tested separately.
"""

from customer_support_mas.agents.root.agent import root_agent

# Disable memory callback for testing
root_agent.after_agent_callback = None

agent = root_agent
