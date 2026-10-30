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

"""Runnability, wiring, and Day-0 capability tests for smart-tool-router."""

import numpy as np

from app.agent import (
    _ORDER_STATUS_TTL_SECONDS,
    _RETURN_POLICY_TTL_SECONDS,
    ALL_TOOLS,
    check_return_eligibility,
    get_order_status,
    get_return_policy,
    lookup_order_by_email,
)

_EXPECTED_DOCTOR_CHECK_COUNT = 3
_MIN_PREREQUISITE_SCORE = 0.90


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines root_agent, app, and router_plugin."""
    import app.agent

    assert app.agent.root_agent is not None
    assert app.agent.app is not None
    assert app.agent.router_plugin in app.agent.app.plugins


def test_doctor_wiring_checks() -> None:
    """Verify smart-router doctor passes all 3 App wiring checks."""
    from adk_smart_router.cli import inspect_app

    import app.agent

    checks = inspect_app(app.agent.app)
    assert len(checks) == _EXPECTED_DOCTOR_CHECK_COUNT
    assert all(check.ok for check in checks), [
        (check.title, check.detail) for check in checks if not check.ok
    ]


def test_day_zero_prerequisite_graph_and_tool_cache() -> None:
    """Verify Day-0 prerequisite matrix and principal-scoped caching."""
    from adk_smart_router import RouterConfig
    from adk_smart_router.judges import build_catalog_index
    from adk_smart_router.request_edits import ToolSpec
    from adk_smart_router.tool_cache import (
        ToolResultCache,
        cache_key,
        user_scope,
    )

    # 1. Verify Day-0 prerequisite matrix links downstream tools to producers
    names = [tool_function.__name__ for tool_function in ALL_TOOLS]
    specs = {
        tool_function.__name__: ToolSpec(
            name=tool_function.__name__,
            description=tool_function.__doc__ or "",
            parameters=tuple(
                (parameter_name, "")
                for parameter_name in tool_function.__code__.co_varnames[
                    : tool_function.__code__.co_argcount
                ]
            ),
        )
        for tool_function in ALL_TOOLS
    }
    vectors = np.eye(len(names), dtype=np.float32)
    catalog = build_catalog_index(names, vectors, specs)
    index_by_name = {name: i for i, name in enumerate(catalog.names)}
    assert (
        catalog.prereq_matrix[
            index_by_name["lookup_order_by_email"],
            index_by_name["get_order_status"],
        ]
        >= _MIN_PREREQUISITE_SCORE
    )
    assert (
        catalog.prereq_matrix[
            index_by_name["lookup_order_by_email"],
            index_by_name["get_loyalty_balance"],
        ]
        >= _MIN_PREREQUISITE_SCORE
    )
    assert (
        catalog.prereq_matrix[
            index_by_name["get_order_status"],
            index_by_name["check_return_eligibility"],
        ]
        >= _MIN_PREREQUISITE_SCORE
    )

    # 2. Verify principal-scoped tool result cache and invalidation
    router_config = RouterConfig(
        cacheable_tools={
            "get_order_status": _ORDER_STATUS_TTL_SECONDS,
            "get_return_policy": _RETURN_POLICY_TTL_SECONDS,
        }
    )
    cache = ToolResultCache(router_config)
    alice_scope = user_scope("app", "alice")
    bob_scope = user_scope("app", "bob")
    alice_key = cache_key(
        "app",
        "alice",
        "support_agent",
        "get_order_status",
        "1.0.0",
        {"order_id": "ORD-10492"},
    )
    bob_key = cache_key(
        "app",
        "bob",
        "support_agent",
        "get_order_status",
        "1.0.0",
        {"order_id": "ORD-10492"},
    )

    order = get_order_status("ORD-10492")
    assert cache.put(
        alice_key,
        order,
        user_scope=alice_scope,
        tool_name="get_order_status",
        ttl_s=_ORDER_STATUS_TTL_SECONDS,
        ttl_source="declared",
    )

    # Same principal hits cache; different principal is isolated
    hit = cache.get(alice_key)
    assert hit is not None and hit.payload["status"] == "in_transit"
    assert cache.get(bob_key) is None
    assert bob_scope != alice_scope

    # Mutating / non-cacheable tool execution purges the principal's cache
    assert cache.purge_user(alice_scope) == 1
    assert cache.get(alice_key) is None

    # 3. Verify tool functions return structured dictionaries
    assert lookup_order_by_email("alice@cymbal.demo")["order_id"] == "ORD-10492"
    assert get_return_policy("electronics")["window_days"] == "30"
    assert check_return_eligibility("ORD-10492")["eligible"] is True
