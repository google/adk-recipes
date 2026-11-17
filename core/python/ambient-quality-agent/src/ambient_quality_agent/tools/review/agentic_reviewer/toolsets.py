# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Toolsets for the session reviewer.

`prompts.REVIEW_TOOLSET` selects one toolset per review:

- ``grep_read`` (default): the `grep` and `read` tools (`tools.grep_read`).
- ``jq``: the `query_trajectory` jq tool (`tools.jq`).

When ``grep_read`` is active:
1. The system instruction uses `prompts.GREP_READ_TOOLS_PARAGRAPH` in place of
   `prompts.JQ_TOOLS_PARAGRAPH`.
2. The instruction outline uses `read` line numbers.
3. String stubs include line counts.

Wiring::

    build_review_tools()
        ──► grep_read.GrepTool(), grep_read.ReadTool()
        or  jq.JqQueryTool()
    build_tool_declarations() ──► build_review_tools()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.review.agentic_reviewer.tools import (
    grep_read,
    jq,
)

if TYPE_CHECKING:
    from google.adk.tools.base_tool import BaseTool
    from google.genai import types

JQ = "jq"
"""Toolset of the `query_trajectory` jq tool."""

GREP_READ = "grep_read"
"""Toolset of the `grep` and `read` tools."""

TOOLSETS = (JQ, GREP_READ)
"""Every toolset name, in the order the harness knob lists them."""


def build_review_tools(
    toolset: str, trajectory_json: dict[str, Any]
) -> list[BaseTool]:
    """Build the tools of one review.

    Each review needs its own tools, bound to its trajectory JSON; the jq tool
    also holds its page origins.

    Args:
        toolset: Toolset name from `TOOLSETS`.
        trajectory_json: Trajectory JSON built by
            `trajectory_json.build_trajectory_json`.

    Returns:
        The toolset's tools, in declaration order.

    Raises:
        ValueError: If `toolset` is not in `TOOLSETS`.
    """
    if toolset == JQ:
        return [jq.JqQueryTool(trajectory_json)]
    if toolset == GREP_READ:
        return [
            grep_read.GrepTool(trajectory_json),
            grep_read.ReadTool(trajectory_json),
        ]
    raise ValueError(
        f"unknown review toolset {toolset!r}; expected one of "
        f"{', '.join(TOOLSETS)}"
    )


def build_tool_declarations(toolset: str) -> list[types.FunctionDeclaration]:
    """Build the function declarations a toolset sends to the model.

    Args:
        toolset: Toolset name from `TOOLSETS`.

    Returns:
        One declaration per tool, in declaration order.

    Raises:
        ValueError: If `toolset` is not in `TOOLSETS`.
    """
    return [
        declaration
        for tool in build_review_tools(toolset, {})
        if (declaration := tool._get_declaration()) is not None
    ]
