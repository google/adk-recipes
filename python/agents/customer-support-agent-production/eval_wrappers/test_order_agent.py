"""Thin wrapper to expose order_agent as `agent` for AgentEvaluator."""

from customer_support_mas.agents.order.agent import order_agent

agent = order_agent
