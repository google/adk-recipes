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

"""Wiring tests for ADK's ``google_search`` built-in.

**Path A:** this re-exports ADK's ``google.adk.tools.google_search`` singleton and
registers it on ``root_agent.tools``. There is no custom Python wrapper
— ``google_search`` is a model-side built-in tool that flips Gemini's
native Search-grounding bit at LLM-call time via
``process_llm_request``. There is no callable surface to assert a
response shape against from pytest.

These tests therefore pin WIRING, not behavior:

* the singleton is re-exported from ``horizon.tools.web_search``
* it is the actual ADK class instance (no accidental shadow)
* it is present in ``root_agent.tools``
* importing the app does not crash with the tool added
* the module exposes nothing custom (no HTTP code, no provider
  registry, no API-key handling — the whole point of Path A is to
  avoid a custom multi-provider plugin layer)

LLM-side behavioral validation (grounded results actually appear, the
model cites them, the tool fires on factual queries) lives in
the ``tests/eval/evalsets/`` suite rather than pytest.
"""

from __future__ import annotations

import inspect

# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_google_search_singleton():
    """This module re-exports ADK's ``google_search`` so consumers can
    ``from horizon.tools.web_search import google_search`` instead of
    reaching into ``google.adk.tools``. Keeps the import surface
    consistent with our other tools."""
    from horizon.tools.web_search import google_search

    assert google_search is not None


def test_re_exported_singleton_is_adk_built_in():
    """Must be the actual ADK ``GoogleSearchTool`` instance, not a
    look-alike. Identity check against the upstream singleton catches
    an accidental subclass or re-construction (which would lose ADK's
    internal wiring to Gemini's search-grounding bit)."""
    from google.adk.tools import google_search as adk_google_search

    from horizon.tools.web_search import google_search

    assert google_search is adk_google_search


def test_re_exported_singleton_is_google_search_tool_class():
    """Belt-and-suspenders on the identity check above: confirm the
    type, so a future ADK refactor that swaps the singleton's class
    surfaces here rather than silently shipping a broken tool."""
    from google.adk.tools.google_search_tool import GoogleSearchTool

    from horizon.tools.web_search import google_search

    assert isinstance(google_search, GoogleSearchTool)


def test_google_search_tool_name_is_stable():
    """The Gemini server-side trigger keys off ``tool.name ==
    'google_search'``. A rename in ADK would silently disable
    grounding — pin the literal so a drift fails a unit test instead
    of being noticed in production."""
    from horizon.tools.web_search import google_search

    assert google_search.name == "google_search"


# =============================================================================
# Module is intentionally tiny
# =============================================================================


def test_module_does_not_define_a_custom_wrapper():
    """Path A's whole value is "no custom wrapper" — if a contributor
    later adds a ``web_search()`` function or a ``WebSearchProvider``
    class, the bait-and-switch needs a conscious decision (and a new
    test), not a silent slide back into lha-shaped complexity.
    """
    import horizon.tools.web_search as module

    # Allow stdlib imports + the singleton re-export. Anything else
    # added to the module's public surface is a flag for review.
    public_names = {n for n in dir(module) if not n.startswith("_")}
    assert "google_search" in public_names
    # No custom functions, no provider classes, no http client.
    for name in public_names - {"google_search"}:
        value = getattr(module, name)
        assert inspect.ismodule(value), (
            f"Unexpected public symbol {name!r} in horizon.tools.web_search — "
            "Path A requires the module stay a thin re-export."
        )


def test_module_does_not_import_an_http_client():
    """The defining property of Path A is "no local HTTP". If
    ``requests`` / ``httpx`` / ``urllib3`` / ``aiohttp`` ever appear
    in the module's import graph, somebody has started reimplementing
    a provider — fail loudly so the team can re-discuss."""
    import sys

    # Drop any stale cached copy so the import-side effects are
    # observable in this test run.
    sys.modules.pop("horizon.tools.web_search", None)

    import horizon.tools.web_search  # noqa: F401

    module_imports = {
        name
        for name in sys.modules
        if name.startswith(("requests", "httpx", "aiohttp", "urllib3"))
    }
    # `urllib3` is a transitive dep of half the SDK universe — only
    # fail if the WEB_SEARCH module itself imports an HTTP client
    # directly. Check its source.
    import horizon.tools.web_search as web_search_module

    source = inspect.getsource(web_search_module)
    for forbidden in ("import requests", "import httpx", "import aiohttp"):
        assert forbidden not in source, (
            f"horizon.tools.web_search must not {forbidden!r} — Path A means "
            "no local HTTP. Found in module source."
        )
    # Silence the unused-var lint; the broader-sdk check above is
    # informational only.
    del module_imports


# =============================================================================
# root_agent registration
# =============================================================================


def test_google_search_lives_on_web_research_sub_agent_not_root():
    """Wiring contract after the sub-agent split:
    ``google_search`` cannot live alongside non-search function tools
    in the same agent — the Gemini API rejects the combo with
    "Multiple tools are supported only when they are all search
    tools." So it lives on ``web_research_agent.tools`` (alone), and
    the root agent reaches it through an ``AgentTool`` wrapper.

    Pin three things: (a) google_search is NOT on root_agent.tools,
    (b) google_search IS on web_research_agent.tools, (c) an AgentTool
    wrapping web_research_agent is on root_agent.tools."""
    from google.adk.tools.agent_tool import AgentTool

    from horizon.agent import root_agent
    from horizon.subagents.web_research import web_research_agent
    from horizon.tools.web_search import google_search

    assert google_search not in root_agent.tools, (
        "google_search must NOT be on root_agent.tools — Gemini API "
        "rejects search tools combined with function tools."
    )

    assert google_search in web_research_agent.tools, (
        "google_search must be on the web_research sub-agent's tools — "
        "that's the only place it can fire without tripping the API mutex."
    )

    agent_tool_wrappers = [
        tool
        for tool in root_agent.tools
        if isinstance(tool, AgentTool) and tool.agent is web_research_agent
    ]
    assert len(agent_tool_wrappers) == 1, (
        f"Expected exactly one AgentTool wrapping web_research_agent on "
        f"root_agent.tools; got {len(agent_tool_wrappers)}."
    )


def test_root_agent_boots_with_web_search_registered():
    """Sanity: importing ``horizon.agent`` must not raise after web_search
    is added. ADK validates the tool list at construction time, so a
    misconfigured tool (e.g., conflicting with another grounding tool)
    would surface as an import-time ValueError. This test catches
    that without needing to spin up the Runner."""
    import importlib

    import horizon.agent

    # Force a fresh import so any deferred ValueError fires in this
    # test run rather than relying on the import cache from another
    # test.
    importlib.reload(horizon.agent)

    assert horizon.agent.root_agent is not None
    assert horizon.agent.root_agent.tools, "root_agent must have tools wired"
