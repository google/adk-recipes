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

"""Static toolset registry — the catalogue the ``subagent`` tool dispatches against.

The ``subagent`` tool materializes a child agent's tool list from this
registry. The LLM names
toolsets (``["file", "shell"]``); the delegate runner expands those to
the concrete tool callables and hands them to the child.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.agent_tool import AgentTool

from horizon.subagents.web_research import web_research_agent
from horizon.tools.file_ops import edit, search_files, write
from horizon.tools.processes.process import process
from horizon.tools.processes.terminal import bash
from horizon.tools.read import ReadTool

TOOLSETS: dict[str, list[Any]] = {
    "file": [ReadTool(), write, edit, search_files],
    "shell": [bash, process],
    # google_search can't co-exist with function tools on the same agent;
    # the web-research sub-agent is the carve-out that holds it.
    "web": [AgentTool(agent=web_research_agent)],
}

DEFAULT_TOOLSETS: list[str] = ["file", "shell"]


def available_toolset_names() -> list[str]:
    """Sorted toolset names — feeds the delegate tool's dynamic schema."""
    return sorted(TOOLSETS.keys())


def resolve_toolsets(names: list[str]) -> list[Any]:
    """Expand toolset names to a deduped list of concrete tools.

    Raises ``KeyError`` if any name is unknown — the dynamic delegate
    catches this and returns a structured error to the parent LLM.
    """
    seen: set[int] = set()
    resolved: list[Any] = []
    for name in names:
        if name not in TOOLSETS:
            raise KeyError(name)
        for tool in TOOLSETS[name]:
            if id(tool) in seen:
                continue
            seen.add(id(tool))
            resolved.append(tool)
    return resolved


def _tool_name(tool: Any) -> str | None:
    return getattr(tool, "__name__", None) or getattr(tool, "name", None)


def _tools_by_name() -> dict[str, Any]:
    index: dict[str, Any] = {}
    for tools in TOOLSETS.values():
        for tool in tools:
            n = _tool_name(tool)
            if n and n not in index:
                index[n] = tool
    return index


def resolve_tools_by_name(names: list[str]) -> list[Any]:
    """Resolve concrete tools by their `.name` / `__name__` — the
    fine-grained counterpart to `resolve_toolsets`. Raises ``KeyError``
    on unknown names so the delegate layer can surface a structured
    error to the parent."""
    index = _tools_by_name()
    seen: set[int] = set()
    resolved: list[Any] = []
    for name in names:
        if name not in index:
            raise KeyError(name)
        tool = index[name]
        if id(tool) in seen:
            continue
        seen.add(id(tool))
        resolved.append(tool)
    return resolved
