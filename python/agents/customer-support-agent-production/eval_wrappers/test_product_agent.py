"""Thin wrapper to expose product_agent as `agent` for AgentEvaluator."""

from customer_support_mas.agents.product.agent import product_agent

agent = product_agent
