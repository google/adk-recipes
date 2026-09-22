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

"""Toolset registry — the static catalogue the dynamic delegate dispatches against.

but expressed as a plain ``dict[str, list[Callable | BaseTool]]`` keyed by
short human names (``"file"``, ``"shell"``, ``"web"``). The
dynamic delegate reads this registry to materialize a child agent's tool
list at dispatch time.

Tests pin shape (which names exist, which tools they contain) rather
than behavior — behavior is covered by the existing per-tool tests.
"""

from __future__ import annotations


def test_toolsets_module_importable():
    from horizon.subagents import toolsets  # noqa: F401


def test_toolsets_registry_is_a_dict_of_lists():
    from horizon.subagents.toolsets import TOOLSETS

    assert isinstance(TOOLSETS, dict)
    assert TOOLSETS, "Registry must not be empty."
    for name, tools in TOOLSETS.items():
        assert isinstance(name, str) and name, (
            f"Toolset key must be non-empty str; got {name!r}"
        )
        assert isinstance(tools, list) and tools, (
            f"Toolset {name!r} must have a non-empty tool list"
        )


def test_file_toolset_has_file_ops():
    # read_file/view_file were merged into ReadTool (Task 3); the file
    # bundle carries the merged tool, not the bare function, so children
    # keep media reads.
    from horizon.subagents.toolsets import TOOLSETS
    from horizon.tools.file_ops import edit, search_files, write
    from horizon.tools.read import ReadTool

    file_tools = TOOLSETS["file"]
    assert any(isinstance(t, ReadTool) for t in file_tools)
    assert write in file_tools
    assert edit in file_tools
    assert search_files in file_tools


def test_shell_toolset_has_bash():
    from horizon.subagents.toolsets import TOOLSETS
    from horizon.tools.processes.terminal import bash

    assert bash in TOOLSETS["shell"]


def test_web_toolset_wraps_web_research_agent_in_agent_tool():
    """``google_search`` can't co-exist with function tools on the same
    agent (the Gemini limitation that drives our ``web_research_agent``
    carve-out). The web toolset therefore exposes the web-research
    sub-agent via ``AgentTool``, identical to how the root agent reaches
    it."""
    from google.adk.tools.agent_tool import AgentTool

    from horizon.subagents.toolsets import TOOLSETS
    from horizon.subagents.web_research import web_research_agent

    web = TOOLSETS["web"]
    wrappers = [
        t
        for t in web
        if isinstance(t, AgentTool) and t.agent is web_research_agent
    ]
    assert len(wrappers) == 1


def test_resolve_returns_unique_tool_list():
    """``resolve_toolsets(["file", "shell", "file"])`` should produce a
    list with no duplicates — the same tool listed under two toolsets is
    a single entry on the child's agent."""
    from horizon.subagents.toolsets import TOOLSETS, resolve_toolsets

    resolved = resolve_toolsets(["file", "shell", "file"])
    assert len(resolved) == len(set(map(id, resolved)))
    # And it must be a superset of file + shell.
    expected_ids = {id(t) for t in TOOLSETS["file"]} | {
        id(t) for t in TOOLSETS["shell"]
    }
    actual_ids = {id(t) for t in resolved}
    assert expected_ids <= actual_ids


def test_resolve_unknown_toolset_raises_keyerror():
    """Unknown toolset names are LLM mistakes — fail loudly so the
    delegate tool can surface a structured error back to the parent."""
    import pytest

    from horizon.subagents.toolsets import resolve_toolsets

    with pytest.raises(KeyError):
        resolve_toolsets(["bogus"])


def test_default_toolsets_is_file_and_shell():
    """When the LLM omits ``toolsets=[]``, default to ``["file", "shell"]`` —
    the most common scratch-work shape."""
    from horizon.subagents.toolsets import DEFAULT_TOOLSETS

    assert DEFAULT_TOOLSETS == ["file", "shell"]


def test_available_toolset_names_is_sorted_and_complete():
    """The dynamic delegate's docstring enumerates available toolsets to
    the parent LLM. The enumeration is sorted for stable schema output."""
    from horizon.subagents.toolsets import (
        TOOLSETS,
        available_toolset_names,
    )

    names = available_toolset_names()
    assert names == sorted(TOOLSETS.keys())


def test_resolve_tools_by_name_returns_concrete_tools():
    """Caller can pick individual tools by name (not just whole bundles).
    Matches against the union of every tool in every toolset."""
    from horizon.subagents.toolsets import resolve_tools_by_name
    from horizon.tools.processes.terminal import bash
    from horizon.tools.read import ReadTool

    resolved = resolve_tools_by_name(["read", "bash"])
    assert any(isinstance(t, ReadTool) for t in resolved)
    assert bash in resolved
    assert len(resolved) == 2


def test_resolve_tools_by_name_dedups():
    from horizon.subagents.toolsets import resolve_tools_by_name

    resolved = resolve_tools_by_name(["read", "read"])
    assert len(resolved) == 1


def test_resolve_tools_by_name_unknown_raises_keyerror():
    """Unknown tool names are LLM mistakes — same loud-failure shape as
    unknown toolset names so the delegate layer can return a structured
    error to the parent."""
    import pytest

    from horizon.subagents.toolsets import resolve_tools_by_name

    with pytest.raises(KeyError):
        resolve_tools_by_name(["read_nonsense_tool"])
