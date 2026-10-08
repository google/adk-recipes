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

"""Observed agent configurations recovered from eval cases.

`AgentRevisionCache` provides tools and system instructions for each agent in an
evaluation case as per-agent (instruction, toolset) pairs.

`resolve_latest_revision` resolves the newest revision identifier from records
carrying a single revision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

    from google.genai.types import FunctionDeclaration


UNRECORDED_PARAMETERS = "<PARAMETERS_UNKNOWN>"
"""Prompt marker substituted when `ToolDefinition.parameters` is None, so a tool
without a recorded schema is not read as taking no arguments."""


class ToolDefinition(BaseModel):
    """Tool schema definition extracted from agent telemetry."""

    name: str
    """Unique function name of the tool."""

    parameters: list[str] | None = None
    """Sorted parameter names extracted from declaration schema or OpenAPI definitions.

    None when the telemetry recorded no schema, so the argument names are
    unknown rather than empty.
    """

    description: str = ""
    """Tool function description extracted from telemetry."""


class AgentRevision(BaseModel):
    """Static configuration (system instruction and toolset) for an agent deployment.

    An empty ``revision_id`` denotes the default unnamed deployment revision.
    """

    agent_id: str
    """The identifier of the root agent or subagent."""

    revision_id: str = ""
    """Deployment revision identifier; empty string denotes the default unnamed revision."""

    instruction: str = ""
    """Agent system instruction extracted from AgentConfig."""

    tools: list[ToolDefinition] = Field(default_factory=list)
    """Tools declared for this agent, preserving per-agent definitions for shared tool names."""


class AgentRevisionCache:
    """Interns `AgentRevision` instances by full serialized configuration.

    Identifiers alone cannot separate configurations: unversioned agents carry
    an empty ``revision_id``, while instructions and toolsets can vary per
    session (such as dynamically filtered tools, per-tenant subsets, or
    request-scoped MCP and A2A toolsets). Interning by full configuration ensures
    each session is evaluated against its exact runtime state while identical
    sessions share a single instance.
    """

    def __init__(self) -> None:
        self._interned: dict[str, AgentRevision] = {}

    def build_revisions(
        self, eval_case: Any, revision_id: str = ""
    ) -> list[AgentRevision]:
        """Returns `AgentRevision` records for all agents in an eval case.

        Identical configurations share a cached instance (treated as read-only)
        to reduce memory footprint across similar sessions. Thread-safe: concurrent
        misses converge on one shared instance.

        Args:
            eval_case: Evaluation case containing telemetry agent data. Cases
                without agent data return an empty list.
            revision_id: Deployment revision identifier.

        Returns:
            List of `AgentRevision` records sorted by `agent_id`. Agents without
            declared tools receive an empty `tools` list.
        """
        agent_data = getattr(eval_case, "agent_data", None)
        agents = (
            (getattr(agent_data, "agents", None) or {}) if agent_data else {}
        )
        revisions: list[AgentRevision] = []
        for agent_id in sorted(agents):
            revision = _build_agent_revision(
                agent_id, agents[agent_id], revision_id
            )
            # Keys on all serialized fields to prevent collisions across schema changes.
            revisions.append(
                self._interned.setdefault(revision.model_dump_json(), revision)
            )
        return revisions


def _build_agent_revision(
    agent_id: str, agent_config: Any, revision_id: str
) -> AgentRevision:
    """Constructs an AgentRevision from an agent configuration.

    Args:
        agent_id: Root agent or subagent identifier.
        agent_config: Agent configuration containing instructions and tools.
        revision_id: Deployment revision identifier.

    Returns:
        The constructed AgentRevision instance.
    """
    return AgentRevision(
        agent_id=agent_id,
        revision_id=revision_id,
        instruction=str(getattr(agent_config, "instruction", "") or ""),
        tools=[
            _declaration_to_tool_definition(declaration)
            for declaration in _flatten_tools(
                getattr(agent_config, "tools", None)
            )
            if declaration.name
        ],
    )


def resolve_latest_revision(revision_ids: Iterable[str]) -> str:
    """Resolves the newest revision identifier from a collection of revision IDs.

    Resolution order:
    1. Filter out empty IDs, which denote unnamed revisions that never outrank named ones.
    2. Compare candidates numerically as integers to identify the newest build.
    3. Fall back to lexicographical comparison if any revision candidate is non-numeric.

    Args:
        revision_ids: Collection of revision identifier strings.

    Returns:
        The newest revision identifier, or an empty string if no named revision exists.
    """
    named = [revision_id for revision_id in revision_ids if revision_id]
    if not named:
        return ""
    try:
        return max(named, key=int)
    except ValueError:
        return max(named)


def _flatten_tools(tools: Any) -> Iterable[FunctionDeclaration]:
    """Yields function declarations extracted from tool objects.

    Args:
        tools: Sequence of tool objects.

    Yields:
        FunctionDeclaration objects defined on each tool.
    """
    for tool in tools or []:
        yield from getattr(tool, "function_declarations", None) or []


def _declaration_to_tool_definition(
    declaration: FunctionDeclaration,
) -> ToolDefinition:
    """Converts a FunctionDeclaration into a ToolDefinition.

    Args:
        declaration: Tool declaration containing schema details.

    Returns:
        A ToolDefinition with sorted parameter names, or None parameters when
        the declaration has no schema, and a stripped description.
    """
    parameter_names = _extract_parameter_names(declaration)
    return ToolDefinition(
        name=str(declaration.name),
        parameters=(
            sorted(parameter_names) if parameter_names is not None else None
        ),
        description=(declaration.description or "").strip(),
    )


def _extract_parameter_names(
    declaration: FunctionDeclaration,
) -> set[str] | None:
    """Extracts parameter property names from declaration schema or parameters object.

    Args:
        declaration: Function declaration containing schema definitions.

    Returns:
        Set of extracted parameter property names, empty for a schema without
        properties. None when the declaration carries no schema at all, as for
        a tool recorded by name only.
    """
    schema = getattr(declaration, "parameters_json_schema", None)
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            return {str(key) for key in properties}
    parameters = getattr(declaration, "parameters", None)
    properties = getattr(parameters, "properties", None)
    if isinstance(properties, Mapping):
        return {str(key) for key in properties}
    if schema is None and parameters is None:
        return None
    return set()
