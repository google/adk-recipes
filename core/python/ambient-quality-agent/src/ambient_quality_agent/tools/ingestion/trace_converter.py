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

"""Convert Cloud Trace agent spans into evaluation `AgentData`."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple

from agentplatform._genai.types.evals import (
    AgentConfig,
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.tools.ingestion.instruction_changes import (
    TurnInstructionTracker,
)
from ambient_quality_agent.tools.ingestion.message_parts import (
    build_attachment_placeholder,
    extract_field,
    parse_inline_data_placeholder,
    parse_part,
)
from ambient_quality_agent.tools.ingestion.trace_models import (
    LogEntryView,
    Span,
)
from google.genai.types import (
    Content,
    FunctionDeclaration,
    Part,
    Tool,
)

logger = logging.getLogger(__name__)


class MessageParseError(Exception):
    """A message payload was present but not parseable as JSON.

    Raised when a span/log message attribute looks like JSON (e.g. a
    truncated array) but ``json.loads`` fails. Signals that the eval case
    is built from incomplete telemetry and should be skipped.
    """


@dataclasses.dataclass
class ConversionReport:
    """What one `TraceAgentDataConverter.convert` call lost on the way.

    A conversion that raises `MessageParseError` costs the whole case, and the
    caller sees that as an exception. This is the other half: telemetry the
    conversion silently did without, which leaves a case that is real but built
    from less than the agent produced. The fetchers pass one in per row and
    count a `degraded` conversion as a partial ingestion.

    Only losses of *content* are recorded. A malformed individual part or JSON
    payload is logged where it is found and not counted here -- those sit deep
    in the parsing helpers, and the two signals below already cover the ways a
    whole span's or trace's worth of conversation goes missing.
    """

    unresolved_refs: int = 0
    """Content offloaded to GCS that could not be read back."""

    dropped_traces: int = 0
    """Traces that produced no conversation turn, so contributed nothing."""

    @property
    def degraded(self) -> bool:
        """Whether anything was lost."""
        return bool(self.unresolved_refs or self.dropped_traces)


@dataclasses.dataclass
class _Accumulators:
    """Cross-turn state gathered while converting a session's spans."""

    configs: dict[str, AgentConfig] = dataclasses.field(default_factory=dict)
    instructions: dict[str, str] = dataclasses.field(default_factory=dict)
    tools: dict[str, list[Tool]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _SpanDynamicConfig:
    """Instruction and changed tools for one span's LLM call.

    Applied to the span's events rather than overwriting the static
    `AgentConfig`: `TurnInstructionTracker` places the instruction in
    ``state_delta`` on the event that should show it, and changed tools become
    every event's ``active_tools``.
    """

    instruction: str | None = None
    """System instruction the span's LLM call ran under, including when equal to
    the baseline so a return to baseline can be detected. None if the span
    records no instruction."""

    tools: list[Tool] | None = None
    """Toolset of the span when it differs from the agent's first-seen toolset,
    or None."""


# Span attribute keys (OpenTelemetry GenAI + `gcp.vertex.agent.*` extensions).
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_AGENT_DESC = "gen_ai.agent.description"
ATTR_SESSION_ID = "gen_ai.conversation.id"
ATTR_LLM_REQUEST = "gcp.vertex.agent.llm_request"
ATTR_LLM_RESPONSE = "gcp.vertex.agent.llm_response"
ATTR_INPUT_MESSAGES = "gen_ai.input.messages"
ATTR_OUTPUT_MESSAGES = "gen_ai.output.messages"
ATTR_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
ATTR_TOOL_DEFINITIONS = "gen_ai.tool.definitions"
ATTR_TOOL_DEFINITIONS_ALT = "gen_ai.tool_definitions"

# The four content keys resolved per span via the ref/log fallback cascade.
_CONTENT_KEYS = (
    ATTR_INPUT_MESSAGES,
    ATTR_OUTPUT_MESSAGES,
    ATTR_SYSTEM_INSTRUCTIONS,
    ATTR_TOOL_DEFINITIONS,
)

# Each inline attribute above may instead be stored as a GCS reference
# under the same key with this suffix, for payloads too large to inline.
_REF_SUFFIX = "_ref"

_SPAN_CALL_LLM = "call_llm"

# OTEL span status code for an error (google.rpc-style: 2 == ERROR).
_STATUS_CODE_ERROR = 2
# OTEL span event recorded when an exception is raised in a span.
_EVENT_EXCEPTION = "exception"
_ATTR_EXCEPTION_MESSAGE = "exception.message"

# Maps a log entry's ``event.name`` to the content key it contributes to.
_EVENT_NAME_TO_KEY = {
    "gen_ai.user.message": ATTR_INPUT_MESSAGES,
    "gen_ai.choice": ATTR_OUTPUT_MESSAGES,
    "gen_ai.system.message": ATTR_SYSTEM_INSTRUCTIONS,
}

# A callable that downloads a ``gs://`` URI and returns its text body.
GcsReader = Callable[[str], str]


class TraceAgentDataConverter:
    """Builds evaluation `AgentData` from a session's trace spans."""

    def __init__(self, gcs_reader: GcsReader | None = None) -> None:
        """Initialize the converter.

        Args:
            gcs_reader: Resolves a ``gs://`` URI to its text content, used
                for ``*_ref`` attributes that offload large payloads to
                GCS. When ``None``, ref attributes are skipped.
        """
        self._gcs_reader = gcs_reader
        # Replaced per `convert` call; here so the resolution helpers can
        # always record into one, whoever called them.
        self._report = ConversionReport()

    def convert(
        self,
        traces: Sequence[Sequence[Span]],
        log_entries_by_span: Mapping[str, Sequence[LogEntryView]] | None = None,
        report: ConversionReport | None = None,
    ) -> AgentData:
        """Convert a session's traces into `AgentData`.

        First resolve each span's OTEL content attributes via the
        span-ref -> span-inline -> log-ref -> log-inline -> log-jsonPayload
        cascade, then assemble turns from the resolved values.

        Args:
            traces: The session's traces, ordered chronologically; each is
                that trace's `Span` list. One trace becomes one
                `ConversationTurn`.
            log_entries_by_span: Optional Cloud Logging entries indexed by
                span id (see `LogFetcher.fetch_entries_by_span`), used as
                the fallback content source when span attributes are empty.
            report: Collects the telemetry this conversion had to do
                without (see `ConversionReport`). Pass one to learn whether
                the returned data is everything the traces held.

        Returns:
            The assembled `AgentData` with per-agent configs and turns.
        """
        logs = log_entries_by_span or {}
        self._report = report if report is not None else ConversionReport()
        acc = _Accumulators()
        turns: list[ConversationTurn] = []
        # Session-wide dedup: a message replayed as history in a later
        # turn's spans is skipped, so each turn holds only its new events.
        seen: set[str] = set()

        turn_index = 0
        for spans in traces:
            turn = self._build_turn(turn_index, spans, logs, acc, seen)
            if turn.events:
                turns.append(turn)
                turn_index += 1
            else:
                self._report.dropped_traces += 1

        agents = {
            agent_id: config.model_copy(
                update={
                    "instruction": acc.instructions.get(agent_id)
                    or config.instruction,
                    "tools": acc.tools.get(agent_id) or config.tools,
                }
            )
            for agent_id, config in acc.configs.items()
        }
        return AgentData(agents=agents, turns=turns)

    def _resolve_span_attributes(
        self, attrs: Mapping[str, Any], log_entries: Sequence[LogEntryView]
    ) -> dict[str, str]:
        """Resolve a span's OTEL content keys via the fallback cascade.

        For each of the four content keys, returns the first value found in priority
        order (span ref -> span inline -> log ref label -> log inline label
        -> log jsonPayload). Keys with no value are omitted.

        Args:
            attrs: Raw span attributes mapping.
            log_entries: Log entries correlated with this span.

        Returns:
            Mapping of content attribute keys to their resolved string values.
        """
        resolved: dict[str, str] = {}
        for key in _CONTENT_KEYS:
            value = self._resolve_attribute_with_log_fallback(
                attrs, key, key + _REF_SUFFIX, log_entries
            )
            if value is not None:
                resolved[key] = value
        # The alternate tool-definitions key, only if the primary is absent.
        if ATTR_TOOL_DEFINITIONS not in resolved:
            alt = _get_attribute(
                attrs, ATTR_TOOL_DEFINITIONS_ALT
            ) or _get_log_label(log_entries, ATTR_TOOL_DEFINITIONS_ALT)
            if alt is not None:
                resolved[ATTR_TOOL_DEFINITIONS] = alt
        return resolved

    def _resolve_attribute_with_log_fallback(
        self,
        attrs: Mapping[str, Any],
        inline_key: str,
        ref_key: str,
        log_entries: Sequence[LogEntryView],
    ) -> str | None:
        """Resolve one content value, span first then log entries.

        Args:
            attrs: Raw span attributes mapping.
            inline_key: Attribute key for inline content.
            ref_key: Attribute key for GCS reference pointer.
            log_entries: Log entries correlated with this span.

        Returns:
            Resolved content string, or None if not found across span or logs.
        """
        span_value = self._resolve_attribute(attrs, inline_key, ref_key)
        if span_value is not None:
            return span_value
        log_ref = _get_log_label(log_entries, ref_key)
        if log_ref is not None:
            resolved = self._read_ref(log_ref)
            if resolved is not None:
                return resolved
        log_inline = _get_log_label(log_entries, inline_key)
        if log_inline is not None:
            return log_inline
        return _build_messages_from_json_payload(log_entries, inline_key)

    def _resolve_attribute(
        self, attrs: Mapping[str, Any], inline_key: str, ref_key: str
    ) -> str | None:
        """Resolve a span attribute: GCS ref first, inline as fallback.

        Args:
            attrs: Raw span attributes mapping.
            inline_key: Attribute key for inline content.
            ref_key: Attribute key for GCS reference pointer.

        Returns:
            Resolved content string, or None if absent.
        """
        ref_uri = _get_attribute(attrs, ref_key)
        if ref_uri is not None:
            content = self._read_ref(ref_uri)
            if content is not None:
                return content
            return _get_attribute(attrs, inline_key)
        return _get_attribute(attrs, inline_key)

    def _read_ref(self, ref_uri: str) -> str | None:
        """Download a ``*_ref`` GCS pointer, or ``None`` when unavailable.

        An unreadable ref is recorded on the conversion's report: the payload
        was offloaded precisely because it was too large to inline, so losing
        it loses conversation content even when the fallback cascade finds
        something else to use.

        Args:
            ref_uri: GCS URI (``gs://...``) to download.

        Returns:
            Downloaded content string, or None if download failed or reader unavailable.
        """
        if self._gcs_reader is None:
            return None
        try:
            return self._gcs_reader(ref_uri)
        except Exception as exc:
            # The cause tells a missing grant on the payload bucket from an
            # object that is gone.
            logger.warning(
                "Failed to resolve trace attribute ref %s: %s: %s",
                ref_uri,
                type(exc).__name__,
                getattr(exc, "message", "") or exc,
            )
            self._report.unresolved_refs += 1
            return None

    def _build_turn(
        self,
        turn_index: int,
        spans: Sequence[Span],
        logs: Mapping[str, Sequence[LogEntryView]],
        acc: _Accumulators,
        seen: set[str],
    ) -> ConversationTurn:
        """Build one turn from a trace's spans.

        Args:
            turn_index: Zero-based index of this conversation turn.
            spans: Ordered spans for this trace.
            logs: Correlated log entries indexed by span ID.
            acc: Accumulated session state across turns.
            seen: Set of serialized Content JSON keys seen so far for deduplication.

        Returns:
            Constructed conversation turn with parsed events.
        """
        span_to_agent = _build_span_to_agent_index(spans)
        strict_span_to_agent = _build_span_to_agent_index(
            spans, use_fallback=False
        )
        events: list[AgentEvent] = []
        # Shared by every span in the turn, so the copies ADK records of one
        # LLM call merge into one event (see `_add_contents`).
        merge_index: dict[str, AgentEvent] = {}

        # Phase 1: resolve each span's OTEL content attributes.
        resolved_by_span: dict[str, dict[str, str]] = {}
        for span in spans:
            resolved_by_span[span.span_id] = self._resolve_span_attributes(
                span.attributes, logs.get(span.span_id, ())
            )

        # First pass: register every agent (from its invoke_agent span).
        for span in spans:
            name = span.attributes.get(ATTR_AGENT_NAME)
            if name and name not in acc.configs:
                acc.configs[name] = AgentConfig(
                    agent_id=name,
                    description=span.attributes.get(ATTR_AGENT_DESC),
                )

        # Second pass: process call_llm / gen_ai spans into events. Reset
        # instruction tracking per turn so each turn reads on its own.
        instruction_tracker = TurnInstructionTracker(acc.instructions)
        for span in spans:
            attrs = span.attributes
            resolved = resolved_by_span.get(span.span_id, {})
            is_call_llm = span.name == _SPAN_CALL_LLM
            is_gen_ai = (
                ATTR_INPUT_MESSAGES in resolved
                or _get_attribute(attrs, "gen_ai.system") is not None
            )
            if not is_call_llm and not is_gen_ai:
                continue

            agent = span_to_agent.get(span.span_id)
            event_start = len(events)
            dynamic = _SpanDynamicConfig()
            event_time = _parse_span_end_time(span.end_time)

            # 1 & 2. Gemini API-format llm_request / llm_response (raw attrs).
            self._collect_llm_request(
                agent,
                attrs,
                acc,
                dynamic,
                events=events,
                seen=seen,
                merge_index=merge_index,
                event_time=event_time,
            )
            self._collect_llm_response(
                agent,
                attrs,
                events=events,
                seen=seen,
                merge_index=merge_index,
                event_time=event_time,
            )

            # 3 & 4. OTEL input/output messages (resolved); one tool-call id
            # map shared across both, scoped to this span (Java parity).
            span_tool_call_ids: dict[str, str] = {}
            for content in _parse_message_list(
                resolved.get(ATTR_INPUT_MESSAGES)
            ):
                _add_contents(
                    agent,
                    [content],
                    events,
                    seen,
                    merge_index,
                    span_tool_call_ids,
                    event_time=event_time,
                )
            for content in _parse_message_list(
                resolved.get(ATTR_OUTPUT_MESSAGES)
            ):
                _add_contents(
                    agent,
                    [content],
                    events,
                    seen,
                    merge_index,
                    span_tool_call_ids,
                    event_time=event_time,
                )

            # 5. OTEL system instructions & tool definitions (resolved).
            self._collect_otel_config(agent, resolved, acc, dynamic)

            _apply_dynamic_configs(
                events, event_start, agent, dynamic, instruction_tracker
            )

        if not events:
            return ConversationTurn(turn_index=turn_index, events=events)

        # Surface span errors. A tool that raises aborts without a
        # tool_call_response, so the failure only appears as the span's
        # ERROR status / ``exception`` event. Promote it into a function_response
        # so the eval sees it.
        #
        # Attribute errors strictly through the span ancestry chain. Root or
        # infrastructure spans (such as ``invoke_workflow``) do not define an
        # agent; assigning unowned errors to an arbitrary trace agent causes
        # misleading attribution. Unresolved errors remain unattributed.
        #
        # A tool that returns its error to the model (as ADK Go does) also
        # marks its span ERROR; skip that span when the turn already holds the
        # same function_response, so the failure appears once.
        recorded_responses = [
            (part.function_response.name, part.function_response.response)
            for event in events
            if event.content
            for part in event.content.parts or []
            if part.function_response is not None
        ]
        for span, error in _list_origin_error_spans(spans):
            tool_name = _resolve_error_tool_name(span)
            response = {"error": error}
            if (tool_name, response) in recorded_responses:
                continue
            agent = strict_span_to_agent.get(span.span_id)
            events.append(
                AgentEvent(
                    author=agent or "user",
                    content=Content(
                        role="user",
                        parts=[
                            Part.from_function_response(
                                name=tool_name, response=response
                            )
                        ],
                    ),
                    event_time=_parse_span_end_time(span.end_time),
                )
            )

        return ConversationTurn(turn_index=turn_index, events=events)

    def _collect_llm_request(
        self,
        agent: str | None,
        attrs: Mapping[str, Any],
        acc: _Accumulators,
        dynamic: _SpanDynamicConfig,
        *,
        events: list[AgentEvent],
        seen: set[str],
        merge_index: dict[str, AgentEvent],
        event_time: Any,
    ) -> None:
        """Process the Gemini API-format ``llm_request`` payload (raw attr).

        Args:
            agent: Owning agent ID.
            attrs: Raw span attributes mapping.
            acc: Accumulated session state.
            dynamic: Dynamic configuration holder for this span.
            events: Target list to append parsed events to.
            seen: Set of serialized Content JSON keys seen so far.
            merge_index: The current turn's events by merge key (see
                `_add_contents`).
            event_time: Timestamp to associate with parsed events.
        """
        request = _parse_json(_get_attribute(attrs, ATTR_LLM_REQUEST))
        if not isinstance(request, dict):
            return
        config = request.get("config")
        if isinstance(config, dict) and agent is not None:
            instruction = _extract_instruction_text(
                config.get("system_instruction")
            )
            if instruction is not None:
                acc.instructions.setdefault(agent, instruction)
                dynamic.instruction = instruction
            parsed_tools = parse_tools_list(config.get("tools"))
            if parsed_tools:
                if agent not in acc.tools:
                    acc.tools[agent] = parsed_tools
                elif not is_equivalent_toolset(parsed_tools, acc.tools[agent]):
                    dynamic.tools = parsed_tools

        contents = request.get("contents")
        if isinstance(contents, list):
            # Function-call map scoped to this generation span (fresh, per Java).
            _add_contents(
                agent,
                contents,
                events,
                seen,
                merge_index,
                {},
                event_time=event_time,
            )

    def _collect_llm_response(
        self,
        agent: str | None,
        attrs: Mapping[str, Any],
        *,
        events: list[AgentEvent],
        seen: set[str],
        merge_index: dict[str, AgentEvent],
        event_time: Any,
    ) -> None:
        """Process the Gemini API-format ``llm_response`` payload (raw attr).

        Args:
            agent: Owning agent ID.
            attrs: Raw span attributes mapping.
            events: Target list to append parsed events to.
            seen: Set of serialized Content JSON keys seen so far.
            merge_index: The current turn's events by merge key (see
                `_add_contents`).
            event_time: Timestamp to associate with parsed events.
        """
        response = _parse_json(_get_attribute(attrs, ATTR_LLM_RESPONSE))
        content = (
            response.get("content") if isinstance(response, dict) else None
        )
        if isinstance(content, dict):
            _add_contents(
                agent,
                [content],
                events,
                seen,
                merge_index,
                {},
                event_time=event_time,
            )

    def _collect_otel_config(
        self,
        agent: str | None,
        resolved: Mapping[str, str],
        acc: _Accumulators,
        dynamic: _SpanDynamicConfig,
    ) -> None:
        """Process OTEL system instructions / tool definitions.

        Args:
            agent: Owning agent ID.
            resolved: Resolved span attributes mapping.
            acc: Accumulated session state.
            dynamic: Dynamic configuration holder for this span.
        """
        if agent is None:
            return
        instruction = _parse_system_instruction(
            resolved.get(ATTR_SYSTEM_INSTRUCTIONS)
        )
        if instruction:
            acc.instructions.setdefault(agent, instruction)
            if dynamic.instruction is None:
                dynamic.instruction = instruction

        parsed_tools = parse_tools_list(resolved.get(ATTR_TOOL_DEFINITIONS))
        if parsed_tools:
            if agent not in acc.tools:
                acc.tools[agent] = parsed_tools
            elif dynamic.tools is None and not is_equivalent_toolset(
                parsed_tools, acc.tools[agent]
            ):
                dynamic.tools = parsed_tools


def _get_attribute(attrs: Mapping[str, Any], key: str) -> str | None:
    """Return a string span attribute, or ``None`` if absent/non-string.

    Args:
        attrs: Span attributes mapping.
        key: Attribute key name.

    Returns:
        Attribute value as string, or None.
    """
    value = attrs.get(key)
    return value if isinstance(value, str) and value else None


def _parse_span_end_time(end_time: str | None) -> dt.datetime | None:
    """Parse a span's ISO-8601 ``end_time`` into a datetime, or ``None``.

    Args:
        end_time: ISO-8601 formatted timestamp string.

    Returns:
        Parsed datetime object, or None if absent or invalid.
    """
    if not end_time:
        return None
    try:
        return dt.datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_span_error(span: Span) -> str | None:
    """Return a span's error message if it failed, else ``None``.

    Args:
        span: Span to check.

    Returns:
        Error message string, or None if span did not fail.
    """
    message: str | None = None
    for event in span.events:
        if event.name == _EVENT_EXCEPTION:
            msg = event.attributes.get(_ATTR_EXCEPTION_MESSAGE)
            if isinstance(msg, str) and msg:
                message = msg
    if span.status.code == _STATUS_CODE_ERROR:
        return message or span.status.message or "error"
    return message


def _list_origin_error_spans(spans: Sequence[Span]) -> list[tuple[Span, str]]:
    """Return errored spans that originated failures, paired with error messages.

    OpenTelemetry sets error status on every ancestor of a failing span. Because
    ancestor spans format or wrap the exception differently (e.g., prefixing the
    exception class name) than the originating event, deduplication must follow
    span ancestry rather than message text equality. Any errored span with an
    errored descendant is treated as a propagated error and excluded.

    Args:
        spans: Ordered sequence of trace spans.

    Returns:
        List of (span, error_message) tuples for spans originating an error.
    """
    span_by_id = {span.span_id: span for span in spans}
    errors = {
        span.span_id: message
        for span in spans
        if (message := _extract_span_error(span))
    }

    shadowed: set[str] = set()
    for span_id in errors:
        current = span_by_id[span_id].parent_span_id
        visited: set[str] = {span_id}
        while current is not None and current not in visited:
            visited.add(current)
            if current in errors:
                shadowed.add(current)
            parent = span_by_id.get(current)
            current = parent.parent_span_id if parent else None

    return [
        (span, errors[span.span_id])
        for span in spans
        if span.span_id in errors and span.span_id not in shadowed
    ]


def _resolve_error_tool_name(span: Span) -> str:
    """Tool name for a failed span's error response.

    Args:
        span: Failed span.

    Returns:
        Tool name string to use in the error response.
    """
    tool = _get_attribute(span.attributes, "gen_ai.tool.name")
    if tool:
        return tool
    if span.name and span.name.startswith("execute_tool "):
        return span.name.removeprefix("execute_tool ").strip()
    return "tool_error"


def _get_log_label(
    log_entries: Sequence[LogEntryView], label_key: str
) -> str | None:
    """Return the first non-empty ``label_key`` across a span's log entries.

    Args:
        log_entries: Sequence of log entries for a span.
        label_key: Label key to look up.

    Returns:
        First non-empty label value, or None if absent.
    """
    for entry in log_entries:
        value = entry.labels.get(label_key)
        if value:
            return value
    return None


def _build_messages_from_json_payload(
    log_entries: Sequence[LogEntryView], attribute_key: str
) -> str | None:
    """Assemble messages from log ``jsonPayload.content``, by event name.

    Groups a span's log entries by the ``event.name`` label mapped to
    ``attribute_key``. System instructions return the first content, as-is when
    plain text or as JSON when structured (parsed later by
    `_parse_system_instruction`); user/model messages are collected into a JSON
    array string (parsed later by the message parser). Returns ``None`` when
    nothing matches.

    Args:
        log_entries: Sequence of log entries for a span.
        attribute_key: Target content attribute key to filter and build.

    Returns:
        Serialized JSON string of assembled messages, plain instruction string, or None.
    """
    if not log_entries:
        return None
    wanted = {
        name for name, key in _EVENT_NAME_TO_KEY.items() if key == attribute_key
    }
    if not wanted:
        return None

    messages: list[Any] = []
    for entry in log_entries:
        if entry.labels.get("event.name") not in wanted:
            continue
        content = entry.json_payload.get("content")
        if content is None:
            continue
        if attribute_key == ATTR_SYSTEM_INSTRUCTIONS:
            if isinstance(content, str):
                return content
            if isinstance(content, Mapping | list):
                return json.dumps(content)
        elif isinstance(content, Mapping) and "role" in content:
            messages.append(content)
    if not messages:
        return None
    return json.dumps(messages)


def _build_span_to_agent_index(
    spans: Sequence[Span], *, use_fallback: bool = True
) -> dict[str, str]:
    """Map span ids to their owning agent names.

    Resolves agent ownership by walking upward from each span to the nearest
    ancestor carrying ``gen_ai.agent.name``.

    When ``use_fallback`` is True, spans unresolved by the ancestry walk default
    to the first agent declared in the trace. Conversation content spans rely on
    this fallback in single-agent traces where ``call_llm`` spans are siblings to
    the agent span rather than descendants. When ``use_fallback`` is False,
    unresolved spans are omitted so callers can avoid misattributing unowned spans.

    Args:
        spans: Sequence of spans for a trace.
        use_fallback: Whether to fall back to the first declared agent for unowned spans.

    Returns:
        Mapping of span ID to owning agent name.
    """
    span_by_id = {span.span_id: span for span in spans}
    direct = {
        span.span_id: span.attributes[ATTR_AGENT_NAME]
        for span in spans
        if span.attributes.get(ATTR_AGENT_NAME)
    }
    fallback = next(iter(direct.values()), None) if use_fallback else None

    result: dict[str, str] = {}
    for span in spans:
        current: str | None = span.span_id
        visited: set[str] = set()
        while current is not None and current not in visited:
            visited.add(current)
            if current in direct:
                result[span.span_id] = direct[current]
                break
            parent = span_by_id.get(current)
            current = parent.parent_span_id if parent else None
        if span.span_id not in result and fallback is not None:
            result[span.span_id] = fallback
    return result


def _add_contents(
    agent: str | None,
    messages: Sequence[Any],
    events: list[AgentEvent],
    seen: set[str],
    merge_index: dict[str, AgentEvent],
    tool_call_ids: dict[str, str],
    *,
    event_time: Any,
) -> None:
    """Build an `AgentEvent` per user/model message, deduplicating repeats.

    When ADK's legacy span content and the experimental gen_ai content capture
    are both on, ADK records each LLM call twice: ``llm_request`` and
    ``llm_response`` on the ``call_llm`` span, and the gen_ai messages on its
    child ``generate_content`` span. The copies differ in their attachments,
    which `message_parts` turns from ``blob`` and inline-data parts into
    placeholders:

    1. A request appears without its inline data in ``llm_request`` and with a
       placeholder in ``gen_ai.input.messages``.
    2. A response appears with decodable inline data in ``llm_response``, which
       becomes a digest placeholder, and with ADK's ``"<not serializable>"``
       sentinel in ``gen_ai.output.messages``, which becomes a ``size
       unknown`` placeholder.

    Within a turn, a message carrying placeholders therefore merges into an
    earlier event from the same author that holds another copy of it (see
    `_get_mergeable_event`). The more detailed copy (see
    `_compute_attachment_detail`) is kept in the earlier event's place.

    Args:
        agent: Owning agent ID.
        messages: Sequence of message mappings.
        events: Target list to append new AgentEvents to. Holds only the
            current turn's events.
        seen: Serialized Content JSON keys seen so far in the session. A
            message carrying placeholders also adds its key without them, so a
            later history replay of it without its inline data is skipped.
        merge_index: The current turn's events by merge key, updated in place.
            Share one across every call for a turn.
        tool_call_ids: Mapping of tool call IDs to tool names.
        event_time: Timestamp to associate with created events.
    """
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = _parse_content(message, tool_call_ids)
        if content is None:
            continue
        key = content.model_dump_json(exclude_none=True)
        if key in seen:
            continue
        seen.add(key)
        author = agent if content.role == "model" else "user"
        attachment_keys = _build_attachment_merge_keys(content)
        if attachment_keys is None:
            merge_keys = [key]
        else:
            merge_keys = [attachment_keys.normalized]
            if attachment_keys.stripped is not None:
                merge_keys.append(attachment_keys.stripped)
                seen.add(attachment_keys.stripped)
            earlier = _get_mergeable_event(
                merge_index, attachment_keys, content, author
            )
            if earlier is not None:
                detail = _compute_attachment_detail(content)
                if detail > _compute_attachment_detail(earlier.content):
                    earlier.content = content
                merge_index.update(dict.fromkeys(merge_keys, earlier))
                continue
        event = AgentEvent(
            author=author, content=content, event_time=event_time or None
        )
        events.append(event)
        merge_index.update(dict.fromkeys(merge_keys, event))


class _AttachmentMergeKeys(NamedTuple):
    """Keys under which two copies of a message with attachments match."""

    normalized: str
    """The content with each inline-data placeholder reduced to its MIME type,
    in place."""

    stripped: str | None
    """The content without its inline-data placeholders, or None if nothing
    else remains."""


def _build_attachment_merge_keys(
    content: Content,
) -> _AttachmentMergeKeys | None:
    """Build the keys that match copies of a message differing in attachments.

    Args:
        content: Parsed message content.

    Returns:
        The message's merge keys, or None if it has no inline-data
        placeholder.
    """
    parts = content.parts or []
    normalized: list[Part] = []
    stripped: list[Part] = []
    for part in parts:
        placeholder = parse_inline_data_placeholder(part)
        if placeholder is None:
            normalized.append(part)
            stripped.append(part)
        else:
            normalized.append(
                build_attachment_placeholder(placeholder.mime_type)
            )
    if len(stripped) == len(parts):
        return None
    return _AttachmentMergeKeys(
        normalized=_build_content_key(content, normalized),
        stripped=_build_content_key(content, stripped) if stripped else None,
    )


def _build_content_key(content: Content, parts: list[Part]) -> str:
    """Build the dedup key of ``content`` with its parts replaced.

    Args:
        content: Parsed message content.
        parts: The parts to serialize in place of the content's own.

    Returns:
        The serialized content, in the form `_add_contents` uses as a key.
    """
    return content.model_copy(update={"parts": parts}).model_dump_json(
        exclude_none=True
    )


def _get_mergeable_event(
    merge_index: Mapping[str, AgentEvent],
    attachment_keys: _AttachmentMergeKeys,
    content: Content,
    author: str | None,
) -> AgentEvent | None:
    """Get the current turn's event that holds another copy of ``content``.

    Args:
        merge_index: The current turn's events by merge key.
        attachment_keys: The merge keys of ``content``.
        content: Parsed message content carrying inline-data placeholders.
        author: The author an event for ``content`` would have.

    Returns:
        The event from ``author`` found under the normalized key whose digests
        do not conflict with those of ``content``; else the event from
        ``author`` found under the stripped key if it holds no inline-data
        placeholder, as only the ``llm_request`` copy lacks them; else None.
    """
    event = merge_index.get(attachment_keys.normalized)
    if (
        event is not None
        and event.author == author
        and not _has_conflicting_digests(event.content, content)
    ):
        return event
    if attachment_keys.stripped is None:
        return None
    event = merge_index.get(attachment_keys.stripped)
    if (
        event is not None
        and event.author == author
        and _compute_attachment_detail(event.content) == 0
    ):
        return event
    return None


def _has_conflicting_digests(first: Content | None, second: Content) -> bool:
    """Whether two contents carry different decoded attachments.

    A ``size unknown`` placeholder can stand for any bytes, so only digest
    placeholders in the same position are compared.

    Args:
        first: An earlier event's content.
        second: A new message's content with the same normalized key.

    Returns:
        True if any position holds a digest placeholder in both contents, with
        different text.
    """
    first_parts = (first.parts or []) if first is not None else []
    for first_part, second_part in zip(
        first_parts, second.parts or [], strict=False
    ):
        if first_part.text != second_part.text and all(
            (placeholder := parse_inline_data_placeholder(part)) is not None
            and placeholder.has_digest
            for part in (first_part, second_part)
        ):
            return True
    return False


def _compute_attachment_detail(content: Content | None) -> int:
    """Score how much a message records about its inline attachments.

    Args:
        content: Parsed message content.

    Returns:
        The sum over its inline-data placeholders of 2 for one naming a digest
        and 1 for one of unknown size; 0 when it has none.
    """
    if content is None:
        return 0
    detail = 0
    for part in content.parts or []:
        placeholder = parse_inline_data_placeholder(part)
        if placeholder is not None:
            detail += 2 if placeholder.has_digest else 1
    return detail


def _parse_content(
    message: Mapping[str, Any], tool_call_ids: dict[str, str]
) -> Content | None:
    """Convert one trace message dict into a genai `Content`.

    ``user`` and ``model`` messages are kept. ``tool`` messages (tool results)
    become ``user``, as in genai; ``assistant`` becomes ``model``; other roles
    are dropped. Returns ``None`` when no usable parts are present.

    Args:
        message: Raw message mapping from trace or log.
        tool_call_ids: Mapping of tool call IDs to tool names.

    Returns:
        SDK Content object, or None if message role is unsupported or has no valid parts.
    """
    role = message.get("role") or "user"
    if role == "assistant":
        role = "model"
    elif role == "tool":
        role = "user"
    if role not in ("user", "model"):
        return None
    parts = [
        p
        for p in (
            parse_part(part, tool_call_ids)
            for part in message.get("parts") or []
        )
        if p
    ]
    if not parts:
        return None
    return Content(role=role, parts=parts)


def _parse_json(value: Any) -> Any:
    """Parse a JSON string, passing through dicts and tolerating garbage.

    Args:
        value: String or mapping value.

    Returns:
        Parsed JSON object or mapping, or empty dict on failure.
    """
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Failed to parse trace JSON payload.")
        return {}


def _parse_message_list(value: Any) -> list[Any]:
    """Parse a JSON or JSONL message payload into a list of dicts.

    Args:
        value: Raw message list payload (string, list, or None).

    Returns:
        List of parsed message dictionaries.

    Raises:
        MessageParseError: ``value`` is a non-empty string that looks like
            JSON but fails to parse (e.g. truncated telemetry).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    try:
        if text.startswith("["):
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else []
        messages: list[Any] = []
        for line in text.splitlines():
            if line.strip():
                messages.append(json.loads(line))
        return messages
    except json.JSONDecodeError as exc:
        raise MessageParseError("message payload is not valid JSON") from exc


def _parse_system_instruction(value: Any) -> str | None:
    """Extract plain system-instruction text from an OTEL payload.

    Args:
        value: Raw system instruction payload: plain text, or a JSON / JSONL
            encoding of parts, a list of parts, or a `Content`.

    Returns:
        The text parts joined by blank lines, or None if there are none.

    Raises:
        MessageParseError: ``value`` looks like JSON but fails to parse.
    """
    if isinstance(value, str):
        if not value.strip().startswith(("[", "{")):
            return value
        return _extract_instruction_text(_parse_message_list(value))
    return _extract_instruction_text(value)


def _extract_instruction_text(value: Any) -> str | None:
    """Extract the text of an already-parsed system instruction.

    Only text is kept: typed parts of any other ``type`` are skipped, since a
    ``blob`` part also carries its base64 under ``content``.

    Args:
        value: A string, a part, a `Content` mapping, or a sequence of those.

    Returns:
        The text parts joined by blank lines, or None if there are none.
    """
    if isinstance(value, str):
        return value
    texts = _list_instruction_texts(value)
    return "\n\n".join(texts) if texts else None


def _list_instruction_texts(value: Any) -> list[str]:
    """List the non-empty text parts of a parsed system instruction, in order.

    Args:
        value: A string, a part, a `Content` mapping, or a sequence of those.

    Returns:
        The text of every text part, flattened across nested sequences and
        `Content` parts.
    """
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        parts = value.get("parts")
        if isinstance(parts, Sequence) and not isinstance(parts, str):
            return _list_instruction_texts(parts)
        if "type" in value:
            text = value.get("content") if value["type"] == "text" else None
        else:
            text = extract_field(value, "text", "content")
        return [text] if isinstance(text, str) and text else []
    if isinstance(value, Sequence):
        return [
            text for item in value for text in _list_instruction_texts(item)
        ]
    return []


_TOOL_FIELD_BY_KEY = {name: name for name in Tool.model_fields} | {
    field.alias: name
    for name, field in Tool.model_fields.items()
    if field.alias
}


def parse_tools_list(tools_value: Any) -> list[Tool]:
    """Parse a tool-definitions payload into a list with one `Tool`.

    Accepts OTEL tool definitions, genai `Tool` dicts and bare tool names. A
    built-in tool becomes a name-only declaration in both dict shapes, so they
    compare equal in `is_equivalent_toolset`.

    Args:
        tools_value: Raw tool definitions payload (string or list).

    Returns:
        List containing a single Tool with parsed FunctionDeclarations, or empty list.

    Raises:
        MessageParseError: ``tools_value`` is a string that looks like JSON
            but fails to parse.
    """
    tools = (
        tools_value
        if isinstance(tools_value, list)
        else _parse_message_list(tools_value)
    )
    declarations: list[FunctionDeclaration] = []
    for tool in tools:
        # Telemetry that records tools by name alone (e.g. older releases of
        # the BigQuery Agent Analytics plugin) yields declarations without a
        # schema.
        if isinstance(tool, str):
            if tool:
                declarations.append(FunctionDeclaration(name=tool))
            continue
        if not isinstance(tool, Mapping):
            continue
        if tool.get("name") or tool.get("type") == "function":
            declarations.append(_parse_function_declaration(tool))
        elif "type" not in tool:
            # `Tool` has no ``type`` field, so a nameless OTEL definition is
            # skipped rather than misread as a built-in tool called "type".
            declarations.extend(_list_tool_declarations(tool))
    if not declarations:
        return []
    return [Tool(function_declarations=declarations)]


def _list_tool_declarations(
    tool: Mapping[str, Any],
) -> list[FunctionDeclaration]:
    """List the declarations of a genai `Tool` dict.

    Mirrors ADK's OTEL tool definitions: each function declaration, plus one
    declaration named after every other populated field (a built-in tool such
    as ``google_search``).

    Args:
        tool: A genai `Tool` dict, in snake_case or camelCase.

    Returns:
        The tool's function declarations.
    """
    declarations: list[FunctionDeclaration] = []
    for key, value in tool.items():
        if value is None:
            continue
        field = _TOOL_FIELD_BY_KEY.get(key, key)
        if field != "function_declarations":
            declarations.append(FunctionDeclaration(name=field))
        elif isinstance(value, list):
            declarations.extend(
                _parse_function_declaration(d)
                for d in value
                if isinstance(d, Mapping)
            )
    return declarations


def _parse_function_declaration(
    function: Mapping[str, Any],
) -> FunctionDeclaration:
    """Parse one JSON function declaration into a `FunctionDeclaration`.

    OTEL tool definitions carry the schemas under ``parameters`` and
    ``response``. A dumped genai declaration, as in ADK's ``llm_request``,
    carries them under ``parameters_json_schema`` and
    ``response_json_schema`` (camelCase when dumped by alias).

    Args:
        function: Function declaration mapping.

    Returns:
        SDK FunctionDeclaration object.
    """
    decl = FunctionDeclaration()
    if _has_non_null(function, "name"):
        decl.name = str(function["name"])
    if _has_non_null(function, "description"):
        decl.description = str(function["description"])
    parameters = extract_field(
        function, "parameters", "parameters_json_schema", "parametersJsonSchema"
    )
    if parameters is not None:
        decl.parameters_json_schema = parameters
    response = extract_field(
        function, "response", "response_json_schema", "responseJsonSchema"
    )
    if response is not None:
        decl.response_json_schema = response
    return decl


def _has_non_null(obj: Mapping[str, Any], key: str) -> bool:
    """Whether ``obj`` has ``key`` set to a non-null value.

    Args:
        obj: Mapping to inspect.
        key: Key name to check.

    Returns:
        True if key is present and value is not None.
    """
    return obj.get(key) is not None


def is_equivalent_toolset(tools1: list[Tool], tools2: list[Tool]) -> bool:
    """Whether two tool lists describe the same set of functions.

    Compares the flattened function declarations using only the fields
    populated on both sides (field-intersection semantics), order independent.
    This tolerates cross-format differences (e.g. one source omitting ``parameters``).

    Args:
        tools1: First list of Tools.
        tools2: Second list of Tools.

    Returns:
        True if the two toolsets declare equivalent functions.
    """
    fds1 = [fd for t in tools1 for fd in (t.function_declarations or [])]
    fds2 = [fd for t in tools2 for fd in (t.function_declarations or [])]
    if len(fds1) != len(fds2):
        return False
    fields = _list_populated_fields(fds1) & _list_populated_fields(fds2)
    return _normalize_decls(fds1, fields) == _normalize_decls(fds2, fields)


def _list_populated_fields(fds: Sequence[FunctionDeclaration]) -> set[str]:
    """Field names meaningfully populated in at least one declaration.

    Args:
        fds: Sequence of function declarations.

    Returns:
        Set of field names having non-empty values in at least one declaration.
    """
    populated: set[str] = set()
    for fd in fds:
        for field in FunctionDeclaration.model_fields:
            value = getattr(fd, field, None)
            if value not in (None, "", [], {}):
                populated.add(field)
    return populated


def _normalize_decls(
    fds: Sequence[FunctionDeclaration], fields: set[str]
) -> frozenset[str]:
    """Project declarations onto ``fields`` for order-independent compare.

    Args:
        fds: Sequence of function declarations.
        fields: Subset of field names to compare.

    Returns:
        Frozenset of serialized declaration JSON strings.
    """
    return frozenset(
        json.dumps(
            {f: _to_jsonable(getattr(fd, f, None)) for f in sorted(fields)},
            sort_keys=True,
            default=str,
        )
        for fd in fds
    )


def _to_jsonable(value: Any) -> Any:
    """Best-effort conversion of an SDK value to a comparable form.

    Args:
        value: Any SDK model or primitive value.

    Returns:
        JSON-serializable representation of value.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value


def _apply_dynamic_configs(
    events: list[AgentEvent],
    start: int,
    agent: str | None,
    dynamic: _SpanDynamicConfig,
    instruction_tracker: TurnInstructionTracker,
) -> None:
    """Apply a span's instruction and changed tools to the events it produced.

    `instruction_tracker` places a changed instruction in
    ``state_delta[SYSTEM_INSTRUCTION_KEY]`` once per turn to keep review prompts
    small. Changed tools become every event's ``active_tools``.

    A span that produced no events logs a warning only for changed tools. ADK
    records each LLM call on a ``call_llm`` span and again on a child
    ``generate_content`` span whose duplicate messages yield no new events;
    because every such span carries an instruction, warning on instructions
    would fire on every call even though the parent span already passed the
    instruction to the tracker.

    Args:
        events: Events of the current turn.
        start: Index of the first event produced by the span.
        agent: Agent that owns the span, or None if unknown.
        dynamic: Instruction and changed tools from the span.
        instruction_tracker: Tracker shared across the turn's spans.
    """
    if dynamic.instruction is None and dynamic.tools is None:
        return
    if start >= len(events):
        if dynamic.tools is not None:
            logger.warning(
                "Span changed the tools of agent %s but produced no events; "
                "dropping the changed tools.",
                agent,
            )
        return
    for event in events[start:]:
        delta = instruction_tracker.build_state_delta(
            agent, event.author, dynamic.instruction
        )
        if delta:
            event.state_delta = {**(event.state_delta or {}), **delta}
        if dynamic.tools is not None:
            event.active_tools = dynamic.tools
