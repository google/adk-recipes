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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADK agent demonstrating session-stable tool routing and result caching.

Pairs `SmartRouterPlugin` (`adk-smart-router`) with ADK's built-in
`BigQueryAgentAnalyticsPlugin` and `ContextCacheConfig` to prune multi-domain
tool catalogs once per session and log A/B routing provenance into BigQuery.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping

from adk_smart_router import SmartRouterPlugin, load_config
from google.adk import agents, apps, models
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)

from app.config import config

_ORDER_STATUS_TTL_SECONDS = 300
_LOYALTY_BALANCE_TTL_SECONDS = 600
_RETURN_POLICY_TTL_SECONDS = 3600
_CONTEXT_CACHE_MIN_TOKENS = 2048
_CONTEXT_CACHE_TTL_SECONDS = 1800
_UNCONFIGURED_PROJECT_PLACEHOLDER = "unconfigured-project"

_CACHEABLE_TOOL_TTLS: Mapping[str, int] = types.MappingProxyType(
    {
        "get_order_status": _ORDER_STATUS_TTL_SECONDS,
        "get_return_policy": _RETURN_POLICY_TTL_SECONDS,
        "get_loyalty_balance": _LOYALTY_BALANCE_TTL_SECONDS,
    }
)
_NEVER_PRUNE_TOOLS: frozenset[str] = frozenset({"escalate_to_human"})
_ANALYTICS_METADATA_ALLOWLIST: tuple[str, ...] = ("smart_router",)


def lookup_order_by_email(email: str) -> dict[str, str]:
    """Find the most recent order ID and customer ID for a customer email.

    Args:
        email: Customer email address provided in the support request.

    Returns:
        Dictionary containing `email`, `order_id`, `customer_id`, and
        `order_date`.
    """
    return {
        "email": email,
        "order_id": "ORD-10492",
        "customer_id": "CUST-8821",
        "order_date": "2026-10-01",
    }


def get_order_status(order_id: str) -> dict[str, str]:
    """Look up the shipping status, carrier, and tracking ID for an order.

    Args:
        order_id: Order identifier such as `ORD-10492`.

    Returns:
        Dictionary containing `order_id`, `status`, `carrier`, and
        `shipment_id`.
    """
    return {
        "order_id": order_id,
        "status": "in_transit",
        "carrier": "UPS",
        "shipment_id": "SHP-99014",
    }


def get_shipment_checkpoints(shipment_id: str) -> dict[str, object]:
    """Retrieve carrier scan checkpoints and estimated delivery date.

    Args:
        shipment_id: Carrier shipment identifier from `get_order_status`.

    Returns:
        Dictionary containing `shipment_id`, `eta`, and `last_hub`.
    """
    return {
        "shipment_id": shipment_id,
        "eta": "2026-10-15",
        "last_hub": "Chicago, IL",
    }


def get_return_policy(category: str) -> dict[str, str]:
    """Return the refund and exchange policy window for a product category.

    Args:
        category: Product category name such as `electronics` or `apparel`.

    Returns:
        Dictionary containing `category`, `window_days`, and `restocking_fee`.
    """
    return {
        "category": category,
        "window_days": "30",
        "restocking_fee": "0%",
    }


def check_return_eligibility(order_id: str) -> dict[str, object]:
    """Verify whether an order is eligible for a self-service return label.

    Args:
        order_id: Order identifier such as `ORD-10492`.

    Returns:
        Dictionary containing `order_id`, `eligible`, and `rma_code`.
    """
    return {
        "order_id": order_id,
        "eligible": True,
        "rma_code": "RMA-4410",
    }


def create_return_label(rma_code: str, reason: str) -> dict[str, str]:
    """Issue a prepaid return shipping label for an approved RMA code.

    Args:
        rma_code: Authorization code from `check_return_eligibility`.
        reason: Customer-stated reason for the return.

    Returns:
        Dictionary containing `rma_code`, `reason`, `label_id`, and `status`.
    """
    return {
        "rma_code": rma_code,
        "reason": reason,
        "label_id": f"LBL-{rma_code}",
        "status": "issued",
    }


def get_loyalty_balance(customer_id: str) -> dict[str, object]:
    """Look up loyalty tier and available reward points for a customer.

    Args:
        customer_id: Customer identifier from `lookup_order_by_email`.

    Returns:
        Dictionary containing `customer_id`, `tier`, and `points`.
    """
    return {
        "customer_id": customer_id,
        "tier": "Gold",
        "points": 4250,
    }


def escalate_to_human(summary: str) -> dict[str, str]:
    """Transfer the conversation to a human support specialist.

    Args:
        summary: Concise summary of the customer's issue and steps taken.

    Returns:
        Dictionary containing `ticket_id`, `queue`, and `summary`.
    """
    return {
        "ticket_id": "ESC-7001",
        "queue": "priority_support",
        "summary": summary,
    }


ALL_TOOLS: tuple[Callable[..., object], ...] = (
    lookup_order_by_email,
    get_order_status,
    get_shipment_checkpoints,
    get_return_policy,
    check_return_eligibility,
    create_return_label,
    get_loyalty_balance,
    escalate_to_human,
)

INSTRUCTION = """You are an enterprise customer support assistant.

Use your tools to answer order tracking, shipping checkpoint, return policy,
return label, and loyalty balance questions accurately:
- If the customer gives an email address instead of an order ID, call
  `lookup_order_by_email` first.
- Before issuing a return label with `create_return_label`, call
  `check_return_eligibility` to obtain the `rma_code`.
- Escalate to a human specialist with `escalate_to_human` when requested."""

root_agent = agents.Agent(
    name="support_agent",
    model=models.Gemini(model=config.GEMINI_MODEL),
    description="Customer support agent with session-stable smart tool routing",
    instruction=INSTRUCTION,
    tools=list(ALL_TOOLS),
)

router_plugin = SmartRouterPlugin(
    load_config(
        embedding_model=config.SMART_ROUTER_EMBEDDING_MODEL,
        cacheable_tools=dict(_CACHEABLE_TOOL_TTLS),
        never_prune=_NEVER_PRUNE_TOOLS,
    )
)


def _build_plugins() -> list[BasePlugin]:
    """Register SmartRouterPlugin before BigQueryAgentAnalyticsPlugin."""
    bigquery_project = config.SMART_ROUTER_BQ_PROJECT
    bigquery_dataset = config.SMART_ROUTER_BQ_DATASET
    analytics_plugin = BigQueryAgentAnalyticsPlugin(
        project_id=bigquery_project or _UNCONFIGURED_PROJECT_PLACEHOLDER,
        dataset_id=bigquery_dataset,
        config=BigQueryLoggerConfig(
            enabled=bool(bigquery_project),
            custom_metadata_allowlist=list(_ANALYTICS_METADATA_ALLOWLIST),
        ),
    )
    return [router_plugin, analytics_plugin]


app = apps.App(
    name="app",
    root_agent=root_agent,
    context_cache_config=ContextCacheConfig(
        min_tokens=_CONTEXT_CACHE_MIN_TOKENS,
        ttl_seconds=_CONTEXT_CACHE_TTL_SECONDS,
    ),
    plugins=_build_plugins(),
)
