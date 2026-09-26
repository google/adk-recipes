"""Thin wrapper to expose billing_agent as `agent` for AgentEvaluator."""

from customer_support_mas.agents.billing.agent import billing_agent

agent = billing_agent
