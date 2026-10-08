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

"""BigQuery ingestion for the Ambient Quality Agent.

`BigQueryFetcher` pulls agent telemetry from the BigQuery Agent
Analytics table (populated by the ADK ``BigQueryAgentAnalyticsPlugin``)
and shapes it into evaluation dataset form.

The two query shapes mirror the two evaluation scopes in Gemini platform:

* ``MetricType.MULTI_TURN`` — one row per session, with the full ordered
  event trajectory aggregated into an ``events`` array (session-level).
* ``MetricType.SINGLE_TURN`` — one row per invocation/turn, with that
  turn's ordered events aggregated into an ``events`` array
  (invocation-level).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, NamedTuple

from agentplatform._genai.types import EvalCase
from agentplatform._genai.types.evals import (
    AgentConfig,
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import BigQueryJobFetcher
from ambient_quality_agent.tools.ingestion.instruction_changes import (
    TurnInstructionTracker,
)
from ambient_quality_agent.tools.ingestion.message_parts import (
    AGENT_ERROR_RESPONSE,
    INVOCATION_ERROR_RESPONSE,
    LLM_ERROR_RESPONSE,
)
from ambient_quality_agent.tools.ingestion.models import MappedRow
from ambient_quality_agent.tools.ingestion.trace_converter import (
    is_equivalent_toolset,
    parse_tools_list,
)
from google.genai.types import (
    Content,
    FunctionCall,
    FunctionResponse,
    Part,
    Tool,
)

logger = logging.getLogger(__name__)

# BigQuery event_type values emitted by the ADK analytics plugin.
_EVENT_USER_MESSAGE = "USER_MESSAGE_RECEIVED"
_EVENT_AGENT_STARTING = "AGENT_STARTING"
_EVENT_LLM_REQUEST = "LLM_REQUEST"
_EVENT_AGENT_RESPONSE = "AGENT_RESPONSE"
_EVENT_TOOL_STARTING = "TOOL_STARTING"
_EVENT_TOOL_COMPLETED = "TOOL_COMPLETED"
_EVENT_TOOL_ERROR = "TOOL_ERROR"
_EVENT_LLM_ERROR = "LLM_ERROR"
_EVENT_AGENT_ERROR = "AGENT_ERROR"
_EVENT_INVOCATION_ERROR = "INVOCATION_ERROR"
_EVENT_HITL_CREDENTIAL_REQUEST = "HITL_CREDENTIAL_REQUEST"
_EVENT_HITL_CONFIRMATION_REQUEST = "HITL_CONFIRMATION_REQUEST"
_EVENT_HITL_INPUT_REQUEST = "HITL_INPUT_REQUEST"
_EVENT_HITL_CREDENTIAL_REQUEST_COMPLETED = "HITL_CREDENTIAL_REQUEST_COMPLETED"
_EVENT_HITL_CONFIRMATION_REQUEST_COMPLETED = (
    "HITL_CONFIRMATION_REQUEST_COMPLETED"
)
_EVENT_HITL_INPUT_REQUEST_COMPLETED = "HITL_INPUT_REQUEST_COMPLETED"
_EVENT_A2A_INTERACTION = "A2A_INTERACTION"

# Placeholders the ADK analytics plugin writes in place of values it dropped.
# The budget marker means entries after it were cut, so a toolset carrying it is
# incomplete.
_PLUGIN_BUDGET_MARKER = "[SANITIZE_BUDGET_EXCEEDED]"
_PLUGIN_PLACEHOLDERS = frozenset(
    {_PLUGIN_BUDGET_MARKER, "[MAX_DEPTH_EXCEEDED]", "[UNSUPPORTED_OBJECT]"}
)


class _PairedEvent(NamedTuple):
    """One conversational event and the agent configuration in force for it."""

    event: Mapping[str, Any]
    """The conversational event."""

    instruction: str | None
    """System prompt of the event's agent when the event was emitted."""

    active_tools: list[Tool] | None
    """Toolset of the event's agent when it differs from the agent's baseline;
    None when it matches or no toolset was logged."""


_HITL_REQUEST_EVENTS = frozenset(
    {
        _EVENT_HITL_CREDENTIAL_REQUEST,
        _EVENT_HITL_CONFIRMATION_REQUEST,
        _EVENT_HITL_INPUT_REQUEST,
    }
)
_HITL_COMPLETED_EVENTS = frozenset(
    {
        _EVENT_HITL_CREDENTIAL_REQUEST_COMPLETED,
        _EVENT_HITL_CONFIRMATION_REQUEST_COMPLETED,
        _EVENT_HITL_INPUT_REQUEST_COMPLETED,
    }
)
_RUNTIME_ERROR_EVENTS = {
    _EVENT_LLM_ERROR: LLM_ERROR_RESPONSE,
    _EVENT_AGENT_ERROR: AGENT_ERROR_RESPONSE,
    _EVENT_INVOCATION_ERROR: INVOCATION_ERROR_RESPONSE,
}
"""Maps each runtime failure event type to its ``function_response`` name."""
_ERROR_EVENTS = frozenset({_EVENT_TOOL_ERROR, *_RUNTIME_ERROR_EVENTS})
# Rows that carry agent configuration, not conversation.
_CONFIGURATION_EVENTS = frozenset({_EVENT_LLM_REQUEST, _EVENT_AGENT_STARTING})


# Default target definitions for multi-turn and single-turn queries.
# The counting and sampling queries share the same targets CTE so both measure
# the exact same population. `session_id IS NOT NULL` prevents grouping unrelated
# events into an invalid NULL session case.
#
# The observed agent is the root of the ADK agent tree. The plugin records it
# on every row as `attributes.root_agent_name`, even when the Runner starts at
# a sub-agent and no row names the root in `agent`. An app does that when it
# already knows which sub-agent must answer, to skip a root that only transfers.
# Invocations without an agent have no root name and fall back to `agent`.
_OBSERVED_AGENT_NAME_SQL = (
    "COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent)"
)

_MULTI_TURN_TARGETS = f"""
  SELECT session_id
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_OBSERVED_AGENT_NAME_SQL} = @agent_name
    AND session_id IS NOT NULL
  GROUP BY session_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

_SINGLE_TURN_TARGETS = f"""
  SELECT invocation_id
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_OBSERVED_AGENT_NAME_SQL} = @agent_name
    AND event_type = 'USER_MESSAGE_RECEIVED'
  GROUP BY invocation_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

_SINGLE_TURN_COUNT_SQL = (
    f"SELECT COUNT(*) AS scanned FROM ({_SINGLE_TURN_TARGETS})"  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
)

# Targets population grouped by the observed-agent expression, returned by
# `count_by_agent` when the configured agent name matches nothing.
_MULTI_TURN_COUNT_BY_AGENT_SQL = f"""
  SELECT {_OBSERVED_AGENT_NAME_SQL} AS agent_name, COUNT(DISTINCT session_id) AS scanned
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_OBSERVED_AGENT_NAME_SQL} IS NOT NULL
    AND session_id IS NOT NULL
  GROUP BY agent_name
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

_SINGLE_TURN_COUNT_BY_AGENT_SQL = f"""
  SELECT {_OBSERVED_AGENT_NAME_SQL} AS agent_name, COUNT(DISTINCT invocation_id) AS scanned
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_OBSERVED_AGENT_NAME_SQL} IS NOT NULL
    AND event_type = 'USER_MESSAGE_RECEIVED'
  GROUP BY agent_name
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_scanned_count_sql(targets: str) -> str:
    """Build a query calculating the total count of matching target records.

    Args:
        targets: Target selection subquery SQL fragment.

    Returns:
        SQL query returning the total record count as ``scanned``.
    """
    return f"SELECT COUNT(*) AS scanned FROM ({targets})"  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def _build_multi_turn_sql(targets: str, table_ref: str) -> str:
    """Build the session-level ingestion query over ``targets``.

    Random sampling and limit application occur inside the CTE to ensure
    unbiased sampling when matches exceed the evaluation budget.

    Args:
        targets: Targets CTE SQL fragment projecting ``session_id``.
        table_ref: Fully-qualified table containing raw event telemetry.

    Returns:
        Complete multi-turn SQL query.
    """
    return f"""
WITH TargetSessions AS (
{targets}
  ORDER BY RAND()
  LIMIT @limit
)
SELECT
  e.session_id,
  ARRAY_AGG(
    STRUCT(
      e.invocation_id,
      e.event_type,
      e.agent,
      IF(e.event_type = 'LLM_REQUEST', NULL, e.content) AS content,
      IF(e.event_type = 'LLM_REQUEST', NULL, e.content_parts) AS content_parts,
      e.error_message,
      IF(
        e.event_type = 'LLM_REQUEST',
        JSON_VALUE(e.content.system_prompt),
        NULL
      ) AS system_prompt,
      IF(
        e.event_type = 'LLM_REQUEST',
        JSON_QUERY(e.attributes, '$.tools'),
        NULL
      ) AS tools
    )
    ORDER BY e.timestamp ASC
  ) AS events
FROM `{table_ref}` AS e
JOIN TargetSessions AS t USING (session_id)
WHERE e.event_type IN (
    'USER_MESSAGE_RECEIVED',
    'AGENT_STARTING',
    'LLM_REQUEST',
    'LLM_ERROR',
    'AGENT_RESPONSE',
    'AGENT_ERROR',
    'TOOL_STARTING',
    'TOOL_COMPLETED',
    'TOOL_ERROR',
    'INVOCATION_ERROR',
    'HITL_CREDENTIAL_REQUEST',
    'HITL_CONFIRMATION_REQUEST',
    'HITL_INPUT_REQUEST',
    'HITL_CREDENTIAL_REQUEST_COMPLETED',
    'HITL_CONFIRMATION_REQUEST_COMPLETED',
    'HITL_INPUT_REQUEST_COMPLETED',
    'A2A_INTERACTION'
)
GROUP BY e.session_id
"""  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


# Samples invocations directly up to the evaluation budget for single-turn evaluation.
_SINGLE_TURN_SQL = f"""
WITH TargetInvocations AS (
{_SINGLE_TURN_TARGETS}
  ORDER BY RAND()
  LIMIT @limit
)
SELECT
  e.invocation_id,
  -- The conversation this turn was part of, so a reader can walk from one to
  -- the other. Not used to build the case, whose id is the invocation.
  ANY_VALUE(e.session_id) AS session_id,
  ARRAY_AGG(
    STRUCT(
      e.event_type,
      e.agent,
      IF(e.event_type = 'LLM_REQUEST', NULL, e.content) AS content,
      IF(e.event_type = 'LLM_REQUEST', NULL, e.content_parts) AS content_parts,
      e.error_message,
      IF(
        e.event_type = 'LLM_REQUEST',
        JSON_VALUE(e.content.system_prompt),
        NULL
      ) AS system_prompt,
      IF(
        e.event_type = 'LLM_REQUEST',
        JSON_QUERY(e.attributes, '$.tools'),
        NULL
      ) AS tools
    )
    ORDER BY e.timestamp ASC
  ) AS events
FROM `{{table_ref}}` AS e
JOIN TargetInvocations AS t USING (invocation_id)
WHERE e.event_type IN (
    'USER_MESSAGE_RECEIVED',
    'AGENT_STARTING',
    'LLM_REQUEST',
    'LLM_ERROR',
    'AGENT_RESPONSE',
    'AGENT_ERROR',
    'TOOL_STARTING',
    'TOOL_COMPLETED',
    'TOOL_ERROR',
    'INVOCATION_ERROR',
    'HITL_CREDENTIAL_REQUEST',
    'HITL_CONFIRMATION_REQUEST',
    'HITL_INPUT_REQUEST',
    'HITL_CREDENTIAL_REQUEST_COMPLETED',
    'HITL_CONFIRMATION_REQUEST_COMPLETED',
    'HITL_INPUT_REQUEST_COMPLETED',
    'A2A_INTERACTION'
)
GROUP BY e.invocation_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


class BigQueryFetcher(BigQueryJobFetcher):
    """Fetches and shapes agent telemetry from BigQuery.

    Inherits all BigQuery job/paging plumbing from `BigQueryJobFetcher`;
    only the SQL templates and the (never-dropping, inline) row mapping
    are specific to the flat analytics-events schema.
    """

    TELEMETRY_SOURCE = selector.BIG_QUERY_SOURCE

    def _build_ingestion_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return _build_multi_turn_sql(
                self._resolve_multi_turn_targets(), self.table_ref
            )
        return self._substitute_table_ref(_SINGLE_TURN_SQL)

    def _build_count_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return _build_scanned_count_sql(self._resolve_multi_turn_targets())
        return self._substitute_table_ref(_SINGLE_TURN_COUNT_SQL)

    def _build_count_by_agent_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return self._substitute_table_ref(_MULTI_TURN_COUNT_BY_AGENT_SQL)
        return self._substitute_table_ref(_SINGLE_TURN_COUNT_BY_AGENT_SQL)

    def _resolve_multi_turn_targets(self) -> str:
        """Resolve the target session IDs for multi-turn ingestion.

        Returns:
            SQL fragment producing distinct ``session_id`` values.
        """
        return self._resolve_targets(_MULTI_TURN_TARGETS, "session_id")

    def _map_row(
        self, row: Mapping[str, Any], metric_type: MetricType
    ) -> MappedRow:
        """Build one `EvalCase` for ``metric_type``.

        Never drops or degrades a row: the analytics schema carries its content
        inline, so there is no payload to fail to resolve and nothing to parse
        that could come back short, so `_map_page` always classifies its rows
        as ingested.

        The ADK events table records no deployment revision, so
        `MappedRow.agent_revision` stays empty and this source's findings are
        attributed to a single unnamed revision. It records no OTel trace ids
        either, so `MappedRow.trace_ids` stays empty and this source's cases
        get no trace-console link -- a property of the telemetry, not of the
        trajectory store.

        Args:
            row: Query result row mapping.
            metric_type: Evaluation metric scope (single-turn or multi-turn).

        Returns:
            Mapped row carrying the evaluation case and metadata.
        """
        case = self._build_case(row, metric_type)
        return MappedRow(
            case=case,
            trajectory_id=str(case.eval_case_id or ""),
            session_id=row.get("session_id"),
        )

    def _build_case(
        self, row: Mapping[str, Any], metric_type: MetricType
    ) -> EvalCase:
        """Build one `EvalCase` for ``metric_type``.

        Each agent's `AgentConfig` carries its baseline instruction (see
        `_collect_agent_instructions`) and the first complete, non-empty
        toolset its ``LLM_REQUEST`` events logged. A later toolset that
        differs is attached to the events it governs, and a changed
        instruction is shown per `TurnInstructionTracker`, matching the Cloud
        Trace path.

        Args:
            row: Query result row mapping.
            metric_type: Ingestion scope (single-turn or multi-turn).

        Returns:
            Constructed evaluation case. An agent whose events logged no tools
            has `AgentConfig.tools` set to None.
        """
        events = _drop_propagated_errors(row.get("events") or [])
        instructions = _collect_agent_instructions(events)
        tool_baselines = _collect_agent_tools(events)
        turn_events = _pair_with_active_config(events, tool_baselines)
        if metric_type is MetricType.MULTI_TURN:
            eval_case_id = row.get("session_id")
            turn_specs = _group_by_invocation(turn_events)
        else:
            eval_case_id = row.get("invocation_id")
            turn_specs = [(0, eval_case_id, turn_events)]

        agents = {
            name: AgentConfig(
                agent_id=name,
                instruction=instruction,
                tools=tool_baselines.get(name),
            )
            for name, instruction in instructions.items()
        }
        return EvalCase(
            eval_case_id=eval_case_id,
            agent_data=AgentData(
                agents=agents,
                turns=_build_turns(turn_specs, instructions),
            ),
        )


def _extract_event_type(event: Mapping[str, Any]) -> str | None:
    return event.get("event_type")


def _extract_content(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the event's ``content`` as a mapping (empty if absent).

    Args:
        event: Event mapping.

    Returns:
        Content mapping or empty dict if absent or invalid.
    """
    content = event.get("content")
    return content if isinstance(content, Mapping) else {}


def _drop_propagated_errors(
    events: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep one error row for each failure.

    ADK logs one unhandled exception once per level it escapes: the failed
    step, an ``AGENT_ERROR`` for each enclosing agent, and an
    ``INVOCATION_ERROR``, all with the same ``error_message``. Keep only the
    row that started the failure, matching the Cloud Trace path, which keeps
    only the span that started an error. Any other row between two errors, such
    as an ``LLM_REQUEST`` after a callback handled the first one, ends the
    chain, so the next error is a new failure. Rows that concurrent work writes
    into the chain, such as a parallel tool call's result, end it as well, so
    that failure appears more than once.

    Args:
        events: Chronological sequence of event mappings.

    Returns:
        The events without each ``AGENT_ERROR`` or ``INVOCATION_ERROR`` whose
        previous row in the same invocation is an error row with the same
        ``error_message``.
    """
    previous: dict[Any, Mapping[str, Any]] = {}
    kept: list[Mapping[str, Any]] = []
    for event in events:
        invocation_id = event.get("invocation_id")
        before = previous.get(invocation_id)
        previous[invocation_id] = event
        if (
            _extract_event_type(event)
            in (_EVENT_AGENT_ERROR, _EVENT_INVOCATION_ERROR)
            and before is not None
            and _extract_event_type(before) in _ERROR_EVENTS
            and (before.get("error_message") or "")
            == (event.get("error_message") or "")
        ):
            continue
        kept.append(event)
    return kept


def _collect_agent_instructions(
    events: list[Mapping[str, Any]],
) -> dict[str, str | None]:
    """Map every distinct agent in a session to its instruction text.

    Prefers the system prompt of the agent's first ``LLM_REQUEST``.

    Args:
        events: Chronological sequence of event mappings.

    Returns:
        An ordered ``{agent_name: instruction_text}`` mapping (first-seen
        order) covering every distinct agent that emitted at least one
        event in ``events``. Instruction text is ``None`` for agents whose
        events carry neither instruction.
    """
    agents: dict[str, str | None] = {}
    authored: dict[str, str | None] = {}
    for event in events:
        agent = event.get("agent")
        if not agent:
            continue
        agents.setdefault(agent, None)
        event_type = _extract_event_type(event)
        if event_type == _EVENT_LLM_REQUEST and agents[agent] is None:
            agents[agent] = _extract_llm_request_instruction(event)
        elif (
            event_type == _EVENT_AGENT_STARTING and authored.get(agent) is None
        ):
            authored[agent] = _extract_agent_starting_instruction(event)
    return {
        agent: instruction if instruction is not None else authored.get(agent)
        for agent, instruction in agents.items()
    }


def _extract_llm_request_instruction(event: Mapping[str, Any]) -> str | None:
    """Resolved system prompt from an ``LLM_REQUEST`` event.

    Args:
        event: Event mapping.

    Returns:
        System prompt string, or None if absent or empty.
    """
    system_prompt = event.get("system_prompt")
    return (
        system_prompt
        if isinstance(system_prompt, str) and system_prompt
        else None
    )


def _extract_agent_starting_instruction(event: Mapping[str, Any]) -> str | None:
    """Instruction text from an ``AGENT_STARTING`` event's content.

    Args:
        event: Event mapping.

    Returns:
        Instruction text string, or None if absent.
    """
    content = event.get("content")
    if isinstance(content, str):
        return content
    return _extract_content(event).get("text")


def _extract_llm_request_tools(event: Mapping[str, Any]) -> list[Tool]:
    """Extract the toolset an ``LLM_REQUEST`` event offered to the model.

    The plugin logs either bare tool names or declaration dicts with ``name``,
    ``description``, and ``parameters``. The value arrives as a list from a
    JSON column, or as JSON text from a STRING column.

    Args:
        event: Event mapping.

    Returns:
        A single-element list holding one `Tool` with the parsed declarations.
        Empty when the event logged no tools, a non-list value, or a list the
        plugin cut short (an incomplete toolset would make every dropped tool
        look like one the agent was not given).
    """
    value = event.get("tools")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list) or _has_budget_marker(value):
        return []
    return parse_tools_list(
        [
            item
            for item in value
            if not (isinstance(item, str) and item in _PLUGIN_PLACEHOLDERS)
        ]
    )


def _has_budget_marker(value: Any) -> bool:
    """Check whether the plugin cut entries from ``value`` to fit its budget.

    Args:
        value: Decoded JSON value. The marker can stand in for a list item, a
            mapping key or a value at any depth.

    Returns:
        True if the budget marker appears anywhere in ``value``.
    """
    if isinstance(value, str):
        return value == _PLUGIN_BUDGET_MARKER
    if isinstance(value, Mapping):
        return any(
            key == _PLUGIN_BUDGET_MARKER or _has_budget_marker(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_budget_marker(item) for item in value)
    return False


def _collect_agent_tools(
    events: list[Mapping[str, Any]],
) -> dict[str, list[Tool]]:
    """Map each agent to the first non-empty toolset its ``LLM_REQUEST``s logged.

    Args:
        events: Chronological sequence of event mappings.

    Returns:
        A ``{agent_name: tools}`` mapping. Agents that logged no tools are
        absent.
    """
    baselines: dict[str, list[Tool]] = {}
    for event in events:
        agent = event.get("agent")
        if (
            not agent
            or agent in baselines
            or _extract_event_type(event) != _EVENT_LLM_REQUEST
        ):
            continue
        tools = _extract_llm_request_tools(event)
        if tools:
            baselines[agent] = tools
    return baselines


def _pair_with_active_config(
    events: list[Mapping[str, Any]],
    tool_baselines: Mapping[str, list[Tool]],
) -> list[_PairedEvent]:
    """Drop configuration rows, pairing each remaining event with its active config.

    ``LLM_REQUEST`` and ``AGENT_STARTING`` rows carry configuration, not
    conversation, and an unmapped event would reach the reviewer as an empty
    message. An ``LLM_REQUEST`` without a system prompt or usable tools preserves the
    agent's previous value for that field. Toolsets are compared against the
    baseline once per ``LLM_REQUEST`` rather than per event, because the
    comparison serializes every declaration on both sides.

    Args:
        events: Chronological sequence of event mappings.
        tool_baselines: First non-empty toolset of each agent, keyed by agent
            name.

    Returns:
        Each conversational event paired with its agent's active instruction and
        any active toolset that differs from the baseline.
    """
    active_instructions: dict[str, str] = {}
    changed_tools: dict[str, list[Tool] | None] = {}
    paired: list[_PairedEvent] = []
    for event in events:
        agent = event.get("agent")
        event_type = _extract_event_type(event)
        if event_type == _EVENT_LLM_REQUEST and agent:
            instruction = _extract_llm_request_instruction(event)
            if instruction:
                active_instructions[agent] = instruction
            tools = _extract_llm_request_tools(event)
            if tools:
                changed_tools[agent] = (
                    None
                    if is_equivalent_toolset(
                        tools, tool_baselines.get(agent, [])
                    )
                    else tools
                )
        if event_type in _CONFIGURATION_EVENTS:
            continue
        paired.append(
            _PairedEvent(
                event=event,
                instruction=active_instructions.get(agent) if agent else None,
                active_tools=changed_tools.get(agent) if agent else None,
            )
        )
    return paired


def _group_by_invocation(
    events: list[_PairedEvent],
) -> list[tuple[int, str | None, list[_PairedEvent]]]:
    """Group events by ``invocation_id`` into ``(turn_index, turn_id, events)`` tuples.

    Args:
        events: List of paired events.

    Returns:
        List of tuples containing turn index, invocation ID, and associated paired events.
    """
    order: list[str | None] = []
    grouped: dict[str | None, list[_PairedEvent]] = {}
    for paired in events:
        invocation_id = paired.event.get("invocation_id")
        if invocation_id not in grouped:
            grouped[invocation_id] = []
            order.append(invocation_id)
        grouped[invocation_id].append(paired)
    return [
        (index, invocation_id, grouped[invocation_id])
        for index, invocation_id in enumerate(order)
    ]


def _build_turns(
    turn_specs: list[tuple[int, str | None, list[_PairedEvent]]],
    baselines: Mapping[str, str | None],
) -> list[ConversationTurn]:
    """Build `ConversationTurn`s from ``(turn_index, turn_id, events)`` tuples.

    Each turn uses a fresh `TurnInstructionTracker` so the turn reads on its
    own: a changed system instruction appears in ``state_delta`` on the first
    event the agent authors under it in the turn, and again whenever it changes
    within the turn.

    Args:
        turn_specs: List of (turn_index, turn_id, paired_events) specifications.
        baselines: Baseline instructions keyed by agent name, from the case's
            `AgentConfig`s.

    Returns:
        List of assembled conversation turns.
    """
    return [
        ConversationTurn(
            turn_index=index,
            turn_id=turn_id,
            events=_build_turn_events(events, baselines),
        )
        for index, turn_id, events in turn_specs
    ]


def _build_turn_events(
    events: list[_PairedEvent], baselines: Mapping[str, str | None]
) -> list[AgentEvent]:
    """Build the `AgentEvent`s of one turn.

    Args:
        events: Ordered paired events for the turn.
        baselines: Baseline instructions keyed by agent name.

    Returns:
        Turn events with changed instructions in `state_delta` per
        `TurnInstructionTracker`, and a toolset that differs from the agent's
        baseline in `active_tools`.
    """
    tracker = TurnInstructionTracker(baselines)
    agent_events: list[AgentEvent] = []
    for paired in events:
        author = _resolve_event_author(paired.event)
        agent_events.append(
            AgentEvent(
                author=author,
                content=_build_event_content(paired.event),
                state_delta=tracker.build_state_delta(
                    paired.event.get("agent"), author, paired.instruction
                ),
                active_tools=paired.active_tools,
            )
        )
    return agent_events


def _build_event_part(event: Mapping[str, Any]) -> Part:
    """Build the `Part` for one event based on its type.

    Args:
        event: Event mapping.

    Returns:
        SDK Part representing the event payload.
    """
    event_type = _extract_event_type(event)
    content = _extract_content(event)
    if event_type == _EVENT_USER_MESSAGE:
        return Part(text=content.get("text_summary"))
    if event_type == _EVENT_AGENT_RESPONSE:
        return Part(text=content.get("response"))
    if event_type == _EVENT_TOOL_STARTING or event_type in _HITL_REQUEST_EVENTS:
        return Part(
            function_call=FunctionCall(
                name=content.get("tool"), args=content.get("args")
            )
        )
    if (
        event_type == _EVENT_TOOL_COMPLETED
        or event_type in _HITL_COMPLETED_EVENTS
    ):
        return Part(
            function_response=FunctionResponse(
                name=content.get("tool"),
                response=_to_response_dict(content.get("result")),
            )
        )
    if event_type == _EVENT_TOOL_ERROR:
        return Part(
            function_response=FunctionResponse(
                name=content.get("tool"),
                response={"error": _resolve_error_message(event, content)},
            )
        )
    if event_type in _RUNTIME_ERROR_EVENTS:
        return Part(
            function_response=FunctionResponse(
                name=_RUNTIME_ERROR_EVENTS[event_type],
                response={"error": _resolve_error_message(event, content)},
            )
        )
    if event_type == _EVENT_A2A_INTERACTION:
        return Part(
            function_response=FunctionResponse(
                name="a2a_response",
                response=_build_a2a_response_payload(content),
            )
        )
    return Part()


def _resolve_error_message(
    event: Mapping[str, Any], content: Mapping[str, Any]
) -> str:
    """Choose the text that describes an error event.

    The plugin writes the error text to ``error_message``, which can be empty
    (for example, on a timeout with no message). From a traceback, only the
    last line is used because it names the exception type and message, whereas
    the full traceback is long and contains file paths.

    Args:
        event: Error event mapping.
        content: The event's ``content`` mapping.

    Returns:
        The first non-empty string among ``error_message``, ``content["error"]``,
        and the last non-empty line of ``content["error_traceback"]``, or
        ``"error"``.
    """
    message = event.get("error_message")
    if isinstance(message, str) and message:
        return message
    error = content.get("error")
    if isinstance(error, str) and error:
        return error
    error_traceback = content.get("error_traceback")
    if isinstance(error_traceback, str):
        for line in reversed(error_traceback.splitlines()):
            if line.strip():
                return line.strip()
    return "error"


def _to_response_dict(value: Any) -> dict[str, Any]:
    """Coerce a tool result into a `FunctionResponse.response` dict.

    Args:
        value: Raw tool result value.

    Returns:
        Dictionary payload suitable for FunctionResponse.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {"output": value}


def _build_a2a_response_payload(content: Mapping[str, Any]) -> dict[str, Any]:
    """Distil an A2A_INTERACTION ``content`` into a function-response payload.

    Args:
        content: Event content mapping.

    Returns:
        Dictionary payload representing the agent-to-agent interaction response.
    """
    if not content:
        return {"error": "no_response"}
    texts = [
        part.get("text")
        for artifact in content.get("artifacts") or []
        for part in artifact.get("parts") or []
        if part.get("kind") == "text" and part.get("text")
    ]
    payload: dict[str, Any] = {}
    if texts:
        payload["text"] = "\n".join(texts)
    status_state = (content.get("status") or {}).get("state")
    if status_state:
        payload["status"] = status_state
    return payload or {"error": "no_artifact"}


def _build_event_content(event: Mapping[str, Any]) -> Content:
    """Build a `Content` (single part) for one event.

    Args:
        event: Event mapping.

    Returns:
        SDK Content object.
    """
    return Content(
        parts=[_build_event_part(event)], role=_resolve_event_role(event)
    )


def _resolve_event_role(event: Mapping[str, Any]) -> str:
    """Resolve the role of an event: ``user`` for input and runtime failures, else ``model``.

    The agent runtime, not the model, records runtime failures, so they take
    the ``user`` role to match the Cloud Trace path.

    Args:
        event: Event mapping.

    Returns:
        Role string ('user' or 'model').
    """
    event_type = _extract_event_type(event)
    if (
        event_type == _EVENT_USER_MESSAGE
        or event_type in _HITL_COMPLETED_EVENTS
        or event_type in _RUNTIME_ERROR_EVENTS
    ):
        return "user"
    return "model"


def _resolve_event_author(event: Mapping[str, Any]) -> str | None:
    """Author of an event: ``user`` for user messages, else its agent.

    Args:
        event: Event mapping.

    Returns:
        Author identifier or 'user'.
    """
    event_type = _extract_event_type(event)
    if (
        event_type == _EVENT_USER_MESSAGE
        or event_type in _HITL_COMPLETED_EVENTS
    ):
        return "user"
    return event.get("agent")
