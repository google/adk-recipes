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

"""Tests for `tools/ingestion/trace_converter.py`."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from typing import Any

import pytest
from agentplatform._genai.types.evals import AgentData
from ambient_quality_agent.tools.ingestion import trace_converter
from ambient_quality_agent.tools.ingestion.trace_converter import (
    MessageParseError,
    TraceAgentDataConverter,
)
from ambient_quality_agent.tools.ingestion.trace_models import (
    LogEntryView,
    Span,
    SpanEvent,
    SpanStatus,
)


def _span(raw: dict[str, Any]) -> Span:
    """Builds a typed `Span` from a test dictionary.

    Args:
        raw: Dictionary containing span attributes and envelope metadata.

    Returns:
        Constructed Span instance.
    """
    events = [
        SpanEvent(name=e.get("name"), attributes=e.get("attributes") or {})
        for e in raw.get("events") or []
    ]
    return Span(
        span_id=raw.get("span_id") or "",
        parent_span_id=raw.get("parent_span_id"),
        name=raw.get("name"),
        end_time=raw.get("end_time"),
        attributes=raw.get("attributes") or {},
        status=SpanStatus(
            code=raw.get("status_code"), message=raw.get("status_message")
        ),
        events=events,
    )


def _llm_span(
    span_id: str,
    parent_span_id: str,
    *,
    request: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
) -> Span:
    attrs: dict[str, Any] = {}
    if request is not None:
        attrs["gcp.vertex.agent.llm_request"] = json.dumps(request)
    if response is not None:
        attrs["gcp.vertex.agent.llm_response"] = json.dumps(response)
    return _span(
        {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": "call_llm",
            "attributes": attrs,
        }
    )


def _agent_span(span_id: str, parent_span_id: str | None, name: str) -> Span:
    return _span(
        {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": "invoke_agent",
            "attributes": {"gen_ai.agent.name": name},
        }
    )


def _log_entry(event_name: str, content: Any) -> LogEntryView:
    return LogEntryView(
        labels={"event.name": event_name}, json_payload={"content": content}
    )


def _single_agent_trace() -> list[Span]:
    """Creates a sample trace with an invoke_agent span and child call_llm span.

    Returns:
        List of Span instances representing a single turn.
    """
    request = {
        "config": {
            "system_instruction": "You are a car service agent.",
            "tools": [{"function_declarations": [{"name": "find_slot"}]}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "When can I change my tires?"}],
            },
        ],
    }
    response = {
        "content": {
            "role": "model",
            "parts": [
                {"function_call": {"name": "find_slot", "args": {"size": "19"}}}
            ],
        }
    }
    return [
        _agent_span("agent-1", "root", "car_service_agent"),
        _llm_span("llm-1", "agent-1", request=request, response=response),
    ]


def test_convert_builds_turn_events_from_request_and_response() -> None:
    converter = TraceAgentDataConverter()

    agent_data = converter.convert([_single_agent_trace()])

    assert len(agent_data.turns) == 1
    turn = agent_data.turns[0]
    assert turn.turn_index == 0

    # User message (request) then model function-call (response).
    user_event = turn.events[0]
    assert user_event.author == "user"
    assert user_event.content.parts[0].text == "When can I change my tires?"

    model_event = turn.events[1]
    assert model_event.author == "car_service_agent"
    assert model_event.content.parts[0].function_call.name == "find_slot"
    assert model_event.content.parts[0].function_call.args == {"size": "19"}


def test_convert_promotes_system_instruction_and_tools() -> None:
    converter = TraceAgentDataConverter()

    agent_data = converter.convert([_single_agent_trace()])

    agent = agent_data.agents["car_service_agent"]
    assert agent.instruction == "You are a car service agent."
    assert agent.tools


def test_convert_dedupes_replayed_history_within_a_turn() -> None:
    """History re-sent across call_llm spans of one turn is deduplicated."""
    converter = TraceAgentDataConverter()

    # Two call_llm spans in the SAME trace (turn): the second replays the
    # first user message before adding a new one.
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span(
            "l1",
            "a1",
            request={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            response={
                "content": {"role": "model", "parts": [{"text": "hello"}]}
            },
        ),
        _llm_span(
            "l2",
            "a1",
            request={
                "contents": [
                    {"role": "user", "parts": [{"text": "hi"}]},
                    {"role": "model", "parts": [{"text": "hello"}]},
                    {"role": "user", "parts": [{"text": "bye"}]},
                ]
            },
            response={
                "content": {"role": "model", "parts": [{"text": "goodbye"}]}
            },
        ),
    ]

    agent_data = converter.convert([spans])

    texts = [
        event.content.parts[0].text for event in agent_data.turns[0].events
    ]
    assert texts == ["hi", "hello", "bye", "goodbye"]


def test_convert_resolves_gcs_ref_attributes() -> None:
    """A *_ref attribute is downloaded and parsed when inline is absent."""
    reads: list[str] = []

    def reader(uri: str) -> str:
        reads.append(uri)
        return json.dumps([{"role": "user", "parts": [{"text": "from gcs"}]}])

    converter = TraceAgentDataConverter(gcs_reader=reader)
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages_ref": "gs://bucket/messages.json",
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    assert reads == ["gs://bucket/messages.json"]
    assert agent_data.turns[0].events[0].content.parts[0].text == "from gcs"


def test_convert_ref_failure_is_skipped(caplog: Any) -> None:
    def reader(uri: str) -> str:
        raise RuntimeError("gcs down")

    converter = TraceAgentDataConverter(gcs_reader=reader)
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages_ref": "gs://bucket/x.json"
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])
    assert agent_data.turns == []


def test_convert_attributes_call_llm_to_parent_agent() -> None:
    """call_llm spans inherit the nearest ancestor's agent name."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("root-agent", "inv", "root_agent"),
        _agent_span("sub-agent", "root-agent", "sub_agent"),
        _llm_span(
            "llm-sub",
            "sub-agent",
            response={
                "content": {"role": "model", "parts": [{"text": "sub reply"}]}
            },
        ),
        _llm_span(
            "llm-root",
            "root-agent",
            response={
                "content": {"role": "model", "parts": [{"text": "root reply"}]}
            },
        ),
    ]

    agent_data = converter.convert([spans])

    by_text = {
        event.content.parts[0].text: event.author
        for event in agent_data.turns[0].events
    }
    assert by_text["sub reply"] == "sub_agent"
    assert by_text["root reply"] == "root_agent"
    assert set(agent_data.agents) == {"root_agent", "sub_agent"}


def test_convert_fallback_agent_for_orphan_llm_span() -> None:
    """A call_llm with no parent chain falls back to the only agent."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "inv", "car_service_agent"),
        _llm_span(
            "orphan",
            "missing-parent",
            response={"content": {"role": "model", "parts": [{"text": "hi"}]}},
        ),
    ]

    agent_data = converter.convert([spans])

    assert agent_data.turns[0].events[0].author == "car_service_agent"


def test_convert_parses_otel_message_parts() -> None:
    """OTEL gen_ai.input/output.messages (typed parts) are supported."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": "hello"}],
                            }
                        ]
                    ),
                    "gen_ai.output.messages": json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [
                                    {
                                        "type": "tool_call",
                                        "name": "find_slot",
                                        "arguments": {"x": 1},
                                    }
                                ],
                            }
                        ]
                    ),
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    events = agent_data.turns[0].events
    assert events[0].author == "user"
    assert events[0].content.parts[0].text == "hello"
    assert events[1].author == "car_service_agent"
    assert events[1].content.parts[0].function_call.name == "find_slot"


def test_convert_resolves_tool_call_response_name_by_id() -> None:
    """OTEL tool_call_response (no name, only id) recovers its name.

    The response part carries only the call ``id``; the function name must
    be recovered from the matching ``tool_call`` part emitted earlier.
    """
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": "book it"}
                                ],
                            },
                            {
                                "role": "assistant",
                                "parts": [
                                    {
                                        "type": "tool_call",
                                        "id": "find_slot_0",
                                        "name": "find_slot",
                                        "arguments": {"car_make": "Alfa"},
                                    }
                                ],
                            },
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "type": "tool_call_response",
                                        "id": "find_slot_0",
                                        "response": {"time": "10:00"},
                                    }
                                ],
                            },
                        ]
                    ),
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    responses = [
        p.function_response
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert len(responses) == 1
    assert responses[0].name == "find_slot"
    assert responses[0].response == {"time": "10:00"}


@pytest.mark.parametrize(
    "response_part",
    [
        pytest.param(
            {
                "type": "tool_call_response",
                "name": "list_dir",
                "response": {"entries": ["README.md"]},
            },
            id="name_present",
        ),
        pytest.param(
            {
                "type": "tool_call_response",
                "id": "call_1",
                "name": "list_dir",
                "response": {"entries": ["README.md"]},
            },
            id="adk_go_shape",
        ),
        pytest.param(
            {
                "type": "tool_call_response",
                "id": "call_1",
                "response": {"entries": ["README.md"]},
            },
            id="name_omitted",
        ),
    ],
)
def test_convert_keeps_tool_role_messages_in_otel_input_messages(
    response_part: dict[str, Any],
) -> None:
    """OTEL input messages with role ``tool`` are preserved under ``user``."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "pairgo"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "generate_content gemini-2.5-flash",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "type": "text",
                                        "content": "list the repo",
                                    }
                                ],
                            },
                            {
                                "role": "assistant",
                                "parts": [
                                    {
                                        "type": "tool_call",
                                        "id": "call_1",
                                        "name": "list_dir",
                                        "arguments": {"path": "."},
                                    }
                                ],
                            },
                            {"role": "tool", "parts": [response_part]},
                        ]
                    ),
                    "gen_ai.output.messages": json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [
                                    {
                                        "type": "text",
                                        "content": "Found README.md.",
                                    }
                                ],
                            }
                        ]
                    ),
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    events = agent_data.turns[0].events
    assert len(events) == 4
    assert events[0].author == "user"
    assert events[1].author == "pairgo"
    assert events[2].author == "user"
    assert events[2].content.role == "user"
    assert events[2].content.parts[0].function_response.name == "list_dir"
    assert events[2].content.parts[0].function_response.response == {
        "entries": ["README.md"]
    }
    assert events[3].author == "pairgo"


def test_convert_merges_structured_log_messages() -> None:
    """Metadata-only spans get their content from per-span log messages."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        # call_llm span with no inline payloads.
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {},
            }
        ),
    ]
    logs = {
        "l1": [
            _log_entry("gen_ai.system.message", "be a helpful agent"),
            _log_entry(
                "gen_ai.user.message",
                {"role": "user", "parts": [{"text": "hi"}]},
            ),
            _log_entry(
                "gen_ai.choice", {"role": "model", "parts": [{"text": "hello"}]}
            ),
        ]
    }

    agent_data = converter.convert([spans], logs)

    assert (
        agent_data.agents["car_service_agent"].instruction
        == "be a helpful agent"
    )
    texts = [e.content.parts[0].text for e in agent_data.turns[0].events]
    assert texts == ["hi", "hello"]


def test_convert_prefers_span_inline_over_logs() -> None:
    """Span inline attributes win over the log fallback."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": "inline"}
                                ],
                            }
                        ]
                    )
                },
            }
        ),
    ]
    logs = {
        "l1": [
            _log_entry(
                "gen_ai.user.message",
                {"role": "user", "parts": [{"text": "from-log"}]},
            )
        ]
    }

    agent_data = converter.convert([spans], logs)

    texts = [e.content.parts[0].text for e in agent_data.turns[0].events]
    assert texts == ["inline"]


def test_convert_promotes_structured_tools_from_request() -> None:
    """config.tools are parsed into structured Tool/FunctionDeclaration."""
    converter = TraceAgentDataConverter()
    request = {
        "config": {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "find_slot",
                            "description": "Find a slot.",
                            "parameters": {"type": "object"},
                        }
                    ]
                }
            ]
        },
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
    }
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span("l1", "a1", request=request),
    ]

    agent_data = converter.convert([spans])

    tools = agent_data.agents["car_service_agent"].tools
    assert tools and tools[0].function_declarations
    decl = tools[0].function_declarations[0]
    assert decl.name == "find_slot"
    assert decl.description == "Find a slot."
    assert decl.parameters_json_schema == {"type": "object"}


def _build_user_message(text: str) -> dict[str, Any]:
    """Builds a user text message in the genai `Content` shape.

    Args:
        text: Message text.

    Returns:
        Message mapping.
    """
    return {"role": "user", "parts": [{"text": text}]}


def _build_model_message(text: str) -> dict[str, Any]:
    """Builds a model text message in the genai `Content` shape.

    Args:
        text: Message text.

    Returns:
        Message mapping.
    """
    return {"role": "model", "parts": [{"text": text}]}


def _build_instructed_llm_span(
    span_id: str,
    parent_span_id: str,
    instruction: str | None,
    contents: Sequence[dict[str, Any]],
    reply: str,
) -> Span:
    """Builds a ``call_llm`` span for one LLM call with a text reply.

    Args:
        span_id: Span ID.
        parent_span_id: Parent span ID.
        instruction: System instruction on the request, or None to omit it.
        contents: Request messages, history first.
        reply: Model text reply.

    Returns:
        Constructed Span instance.
    """
    request: dict[str, Any] = {"contents": list(contents)}
    if instruction is not None:
        request["config"] = {"system_instruction": instruction}
    return _llm_span(
        span_id,
        parent_span_id,
        request=request,
        response={"content": _build_model_message(reply)},
    )


def _build_baseline_turn(
    agent_span_id: str, llm_span_id: str, agent: str, instruction: str
) -> list[Span]:
    """Builds a one-call turn that sets an agent's baseline instruction.

    Args:
        agent_span_id: ID of the ``invoke_agent`` span.
        llm_span_id: ID of the ``call_llm`` span.
        agent: Agent name.
        instruction: Baseline system instruction.

    Returns:
        Spans for the turn.
    """
    return [
        _agent_span(agent_span_id, "root", agent),
        _build_instructed_llm_span(
            llm_span_id,
            agent_span_id,
            instruction,
            [_build_user_message(f"hi {agent}")],
            f"hello from {agent}",
        ),
    ]


def _list_shown_instructions(
    agent_data: AgentData, turn_index: int
) -> list[tuple[str | None, str | None, str | None]]:
    """Lists each event of a turn with the instruction it shows.

    Args:
        agent_data: Converted `AgentData`.
        turn_index: Index of the turn to inspect.

    Returns:
        Ordered ``(author, text, shown_instruction)`` tuples for the turn's
        events.
    """
    return [
        (
            event.author,
            event.content.parts[0].text,
            (event.state_delta or {}).get("system_instruction"),
        )
        for event in agent_data.turns[turn_index].events
    ]


def test_convert_records_dynamic_instruction_in_state_delta() -> None:
    """A changed system instruction is shown on the agent's own event."""
    converter = TraceAgentDataConverter()
    turn1 = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span(
            "l1",
            "a1",
            request={
                "config": {"system_instruction": "Be brief."},
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            },
        ),
    ]
    turn2 = [
        _agent_span("a2", "root", "car_service_agent"),
        _llm_span(
            "l2",
            "a2",
            request={
                "config": {"system_instruction": "Be verbose."},
                "contents": [{"role": "user", "parts": [{"text": "more"}]}],
            },
            response={"content": _build_model_message("Here is more.")},
        ),
    ]

    agent_data = converter.convert([turn1, turn2])

    # Static config keeps the first instruction.
    assert agent_data.agents["car_service_agent"].instruction == "Be brief."
    user_event, model_event = agent_data.turns[1].events
    assert user_event.author == "user"
    assert user_event.state_delta is None
    assert model_event.author == "car_service_agent"
    assert model_event.state_delta == {"system_instruction": "Be verbose."}


def test_convert_shows_a_changed_instruction_once_across_a_turns_llm_calls() -> (
    None
):
    converter = TraceAgentDataConverter()
    go, one = _build_user_message("go"), _build_model_message("one")
    two, three = _build_user_message("two"), _build_model_message("three")
    four = _build_user_message("four")
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _build_instructed_llm_span("l2", "a2", "v2", [go], "one"),
        _build_instructed_llm_span("l3", "a2", "v2", [go, one, two], "three"),
        _build_instructed_llm_span(
            "l4", "a2", "v2", [go, one, two, three, four], "five"
        ),
    ]

    agent_data = converter.convert(
        [_build_baseline_turn("a1", "l1", "agent", "v1"), turn2]
    )

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", "v2"),
        ("user", "two", None),
        ("agent", "three", None),
        ("user", "four", None),
        ("agent", "five", None),
    ]


def test_convert_shows_a_change_back_to_the_baseline_within_a_turn() -> None:
    converter = TraceAgentDataConverter()
    go, one = _build_user_message("go"), _build_model_message("one")
    two = _build_user_message("two")
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _build_instructed_llm_span("l2", "a2", "v2", [go], "one"),
        _build_instructed_llm_span("l3", "a2", "v1", [go, one, two], "three"),
    ]

    agent_data = converter.convert(
        [_build_baseline_turn("a1", "l1", "agent", "v1"), turn2]
    )

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", "v2"),
        ("user", "two", None),
        ("agent", "three", "v1"),
    ]


def test_convert_keeps_the_shown_instruction_across_a_span_without_one() -> (
    None
):
    converter = TraceAgentDataConverter()
    go, one = _build_user_message("go"), _build_model_message("one")
    two, three = _build_user_message("two"), _build_model_message("three")
    four = _build_user_message("four")
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _build_instructed_llm_span("l2", "a2", "v2", [go], "one"),
        _build_instructed_llm_span("l3", "a2", None, [go, one, two], "three"),
        _build_instructed_llm_span(
            "l4", "a2", "v2", [go, one, two, three, four], "five"
        ),
    ]

    agent_data = converter.convert(
        [_build_baseline_turn("a1", "l1", "agent", "v1"), turn2]
    )

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", "v2"),
        ("user", "two", None),
        ("agent", "three", None),
        ("user", "four", None),
        ("agent", "five", None),
    ]


def test_convert_shows_a_changed_instruction_again_in_the_next_turn() -> None:
    converter = TraceAgentDataConverter()
    go, one = _build_user_message("go"), _build_model_message("one")
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _build_instructed_llm_span("l2", "a2", "v2", [go], "one"),
    ]
    turn3 = [
        _agent_span("a3", "root", "agent"),
        _build_instructed_llm_span(
            "l3", "a3", "v2", [go, one, _build_user_message("next")], "ok"
        ),
    ]

    agent_data = converter.convert(
        [_build_baseline_turn("a1", "l1", "agent", "v1"), turn2, turn3]
    )

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", "v2"),
    ]
    assert _list_shown_instructions(agent_data, 2) == [
        ("user", "next", None),
        ("agent", "ok", "v2"),
    ]


def test_convert_tracks_each_agents_changed_instruction_independently() -> None:
    converter = TraceAgentDataConverter()
    turn1 = [
        *_build_baseline_turn("p1", "pl1", "planner", "P1"),
        *_build_baseline_turn("w1", "wl1", "writer", "W1"),
    ]
    go, plan = _build_user_message("go"), _build_model_message("plan")
    write, draft = _build_user_message("write"), _build_model_message("draft")
    turn2 = [
        _agent_span("p2", "root", "planner"),
        _agent_span("w2", "p2", "writer"),
        _build_instructed_llm_span("pl2", "p2", "P2", [go], "plan"),
        _build_instructed_llm_span("wl2", "w2", "W2", [write], "draft"),
        _build_instructed_llm_span(
            "pl3", "p2", "P2", [go, plan, _build_user_message("more")], "plan2"
        ),
        _build_instructed_llm_span(
            "wl3",
            "w2",
            "W2",
            [write, draft, _build_user_message("again")],
            "draft2",
        ),
    ]

    agent_data = converter.convert([turn1, turn2])

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("planner", "plan", "P2"),
        ("user", "write", None),
        ("writer", "draft", "W2"),
        ("user", "more", None),
        ("planner", "plan2", None),
        ("user", "again", None),
        ("writer", "draft2", None),
    ]


def _build_otel_chat_span(
    span_id: str,
    parent_span_id: str,
    instruction: str,
    inputs: Sequence[tuple[str, str]],
    reply: str,
) -> Span:
    """Builds a non-ADK OTel GenAI chat span for one LLM call.

    Args:
        span_id: Span ID.
        parent_span_id: Parent span ID.
        instruction: ``gen_ai.system_instructions`` text.
        inputs: Input messages as ``(role, text)`` pairs, history first.
        reply: Assistant text reply.

    Returns:
        Constructed Span instance.
    """
    return _span(
        {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": "chat gemini",
            "attributes": {
                "gen_ai.system_instructions": json.dumps(
                    [{"type": "text", "content": instruction}]
                ),
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": role,
                            "parts": [{"type": "text", "content": text}],
                        }
                        for role, text in inputs
                    ]
                ),
                "gen_ai.output.messages": json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": reply}],
                        }
                    ]
                ),
            },
        }
    )


def test_convert_shows_a_changed_otel_system_instruction_once_per_turn() -> (
    None
):
    converter = TraceAgentDataConverter()
    turn1 = [
        _agent_span("a1", "root", "agent"),
        _build_otel_chat_span("c1", "a1", "v1", [("user", "hi")], "hello"),
    ]
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _build_otel_chat_span("c2", "a2", "v2", [("user", "go")], "one"),
        _build_otel_chat_span(
            "c3",
            "a2",
            "v2",
            [("user", "go"), ("assistant", "one"), ("user", "two")],
            "three",
        ),
    ]

    agent_data = converter.convert([turn1, turn2])

    assert agent_data.agents["agent"].instruction == "v1"
    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", "v2"),
        ("user", "two", None),
        ("agent", "three", None),
    ]


def test_convert_prefers_the_llm_request_instruction_over_the_otel_one() -> (
    None
):
    converter = TraceAgentDataConverter()
    go = _build_user_message("go")
    llm_span = _build_instructed_llm_span("l2", "a2", "v1", [go], "one")
    llm_span.attributes["gen_ai.system_instructions"] = "v2"
    turn2 = [_agent_span("a2", "root", "agent"), llm_span]

    agent_data = converter.convert(
        [_build_baseline_turn("a1", "l1", "agent", "v1"), turn2]
    )

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", None),
    ]


def _build_tools_request(
    tool_name: str, contents: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Builds an ``llm_request`` offering one function tool.

    Args:
        tool_name: Name of the offered function.
        contents: Request messages, history first.

    Returns:
        Request payload dictionary.
    """
    return {
        "config": {
            "tools": [{"function_declarations": [{"name": tool_name}]}],
        },
        "contents": list(contents),
    }


def test_convert_records_dynamic_tools_in_active_tools() -> None:
    """Changed tools go on every event of every span that uses them."""
    converter = TraceAgentDataConverter()
    hi = _build_user_message("hi")
    book, booked = _build_user_message("book"), _build_model_message("booked")
    thanks = _build_user_message("thanks")
    turn1 = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span("l1", "a1", request=_build_tools_request("find_slot", [hi])),
    ]
    turn2 = [
        _agent_span("a2", "root", "car_service_agent"),
        _llm_span(
            "l2",
            "a2",
            request=_build_tools_request("book_appointment", [book]),
            response={"content": booked},
        ),
        _llm_span(
            "l3",
            "a2",
            request=_build_tools_request(
                "book_appointment", [book, booked, thanks]
            ),
            response={"content": _build_model_message("bye")},
        ),
    ]

    agent_data = converter.convert([turn1, turn2])

    static = agent_data.agents["car_service_agent"].tools
    assert static[0].function_declarations[0].name == "find_slot"
    events = agent_data.turns[1].events
    assert [event.author for event in events] == [
        "user",
        "car_service_agent",
        "user",
        "car_service_agent",
    ]
    for event in events:
        assert event.active_tools
        assert (
            event.active_tools[0].function_declarations[0].name
            == "book_appointment"
        )


def _build_generate_content_copy_span(
    span_id: str,
    parent_span_id: str,
    inputs: Sequence[tuple[str, str]],
    reply: str,
    **attrs: Any,
) -> Span:
    """Builds ADK's ``generate_content`` child span duplicating a ``call_llm`` span.

    Args:
        span_id: Span ID.
        parent_span_id: ID of the parent ``call_llm`` span.
        inputs: Input messages as ``(role, text)`` pairs, history first.
        reply: Model text reply.
        **attrs: Additional span attributes.

    Returns:
        Constructed Span instance.
    """
    return _span(
        {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": "generate_content gemini",
            "attributes": {
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": role,
                            "parts": [{"type": "text", "content": text}],
                        }
                        for role, text in inputs
                    ]
                ),
                "gen_ai.output.messages": json.dumps(
                    [
                        {
                            "role": "model",
                            "parts": [{"type": "text", "content": reply}],
                        }
                    ]
                ),
                **attrs,
            },
        }
    )


def test_convert_does_not_warn_about_an_instruction_on_a_span_without_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    converter = TraceAgentDataConverter()
    go = _build_user_message("go")
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _build_instructed_llm_span("l2", "a2", "v2", [go], "one"),
        _build_generate_content_copy_span(
            "g2",
            "l2",
            [("user", "go")],
            "one",
            **{"gen_ai.system_instructions": "v2"},
        ),
    ]

    with caplog.at_level("WARNING", logger=trace_converter.logger.name):
        agent_data = converter.convert(
            [_build_baseline_turn("a1", "l1", "agent", "v1"), turn2]
        )

    assert _list_shown_instructions(agent_data, 1) == [
        ("user", "go", None),
        ("agent", "one", "v2"),
    ]
    assert not caplog.records


def test_convert_warns_when_changed_tools_yield_no_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    converter = TraceAgentDataConverter()
    hi, go = _build_user_message("hi"), _build_user_message("go")
    turn1 = [
        _agent_span("a1", "root", "agent"),
        _llm_span("l1", "a1", request=_build_tools_request("find_slot", [hi])),
    ]
    turn2 = [
        _agent_span("a2", "root", "agent"),
        _llm_span(
            "l2",
            "a2",
            request=_build_tools_request("book_appointment", [go]),
            response={"content": _build_model_message("one")},
        ),
        _build_generate_content_copy_span(
            "g2",
            "l2",
            [("user", "go")],
            "one",
            **{
                "gen_ai.tool.definitions": json.dumps(
                    [{"name": "book_appointment", "type": "function"}]
                )
            },
        ),
    ]

    with caplog.at_level("WARNING", logger=trace_converter.logger.name):
        converter.convert([turn1, turn2])

    assert [record.getMessage() for record in caplog.records] == [
        "Span changed the tools of agent agent but produced no events; "
        "dropping the changed tools."
    ]


def test_convert_preserves_native_rich_parts() -> None:
    """Native parts beyond text/function are kept (e.g. thought)."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span(
            "l1",
            "a1",
            response={
                "content": {
                    "role": "model",
                    "parts": [{"text": "thinking", "thought": True}],
                }
            },
        ),
    ]

    agent_data = converter.convert([spans])

    part = agent_data.turns[0].events[-1].content.parts[0]
    assert part.text == "thinking"
    assert part.thought is True


def test_convert_sets_event_time_from_span_end_time() -> None:
    """event_time is populated from the span's end_time."""
    converter = TraceAgentDataConverter()
    span = _llm_span(
        "l1",
        "a1",
        response={"content": {"role": "model", "parts": [{"text": "hi"}]}},
    )
    span.end_time = "2026-06-30T12:00:00Z"
    spans = [_agent_span("a1", "root", "car_service_agent"), span]

    agent_data = converter.convert([spans])

    event = agent_data.turns[0].events[-1]
    assert event.event_time is not None
    assert event.event_time.year == 2026


def test_convert_reads_tool_response_result_key_and_name() -> None:
    """tool_call_response uses `result` + explicit `name`."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_cr_pydantic"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "type": "tool_call_response",
                                        "id": "pyd_ai_1",
                                        "name": "find_slot",
                                        "result": {
                                            "date": "April 8, 2026",
                                            "time": "10:00",
                                        },
                                    }
                                ],
                            }
                        ]
                    ),
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    fr = agent_data.turns[0].events[0].content.parts[0].function_response
    assert fr is not None
    assert fr.name == "find_slot"
    assert fr.response == {"date": "April 8, 2026", "time": "10:00"}


def test_convert_wraps_string_tool_result() -> None:
    """A plain-string tool result is wrapped as {output: ...}."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_cr_pydantic"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "type": "tool_call_response",
                                        "name": "book_appointment",
                                        "result": "Scheduled!",
                                    }
                                ],
                            }
                        ]
                    ),
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    fr = agent_data.turns[0].events[0].content.parts[0].function_response
    assert fr is not None
    assert fr.name == "book_appointment"
    assert fr.response == {"output": "Scheduled!"}


def test_convert_surfaces_span_error_from_exception_event() -> None:
    """A span exception event becomes an error function_response."""
    converter = TraceAgentDataConverter()
    spans = [
        _span(
            {
                "span_id": "a1",
                "parent_span_id": "root",
                "name": "invoke_agent it_support_agent",
                "attributes": {"gen_ai.agent.name": "it_support_agent"},
                "status_code": 2,
                "status_message": "ValueError: Invalid priority level.",
                "events": [
                    {
                        "name": "exception",
                        "attributes": {
                            "exception.message": "Invalid priority level."
                        },
                    }
                ],
            }
        ),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": "help"}],
                            }
                        ]
                    )
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    errors = [
        p.function_response
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert len(errors) == 1
    assert errors[0].response == {"error": "Invalid priority level."}
    error_event = next(
        e
        for t in agent_data.turns
        for e in t.events
        if e.content.parts[0].function_response is not None
    )
    assert error_event.author == "it_support_agent"


def test_convert_surfaces_span_error_from_status_only() -> None:
    """A span with ERROR status but no exception event still surfaces."""
    converter = TraceAgentDataConverter()
    spans = [
        _span(
            {
                "span_id": "a1",
                "parent_span_id": "root",
                "name": "invoke_agent x",
                "attributes": {
                    "gen_ai.agent.name": "x",
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": "hi"}],
                            }
                        ]
                    ),
                },
                "status_code": 2,
                "status_message": "boom",
                "events": [],
            }
        )
    ]

    agent_data = converter.convert([spans])

    errors = [
        p.function_response.response
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert errors == [{"error": "boom"}]


def test_convert_suppresses_orphan_span_error_without_content() -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _span(
            {
                "span_id": "a1",
                "parent_span_id": "root",
                "name": "invoke_agent x",
                "attributes": {"gen_ai.agent.name": "x"},
                "status_code": 2,
                "status_message": "boom",
                "events": [],
            }
        )
    ]

    agent_data = converter.convert([spans])

    assert agent_data.turns == []


def test_convert_promotes_only_deepest_span_in_error_chain() -> None:
    """Ensure only the originating leaf span in an error propagation chain is promoted."""
    converter = TraceAgentDataConverter()
    propagated = "ValueError: Unknown expense category: transportation."
    origin = "Unknown expense category: transportation."
    exception_event = {
        "name": "exception",
        "attributes": {"exception.message": origin},
    }
    spans = [
        _span(
            {
                "span_id": "w1",
                "parent_span_id": None,
                "name": "invoke_workflow travel_desk_sr1",
                "status_code": 2,
                "status_message": propagated,
            }
        ),
        _span(
            {
                "span_id": "a1",
                "parent_span_id": "w1",
                "name": "invoke_agent expense_agent",
                "attributes": {"gen_ai.agent.name": "expense_agent"},
                "status_code": 2,
                "status_message": propagated,
                "events": [exception_event],
            }
        ),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": "file my taxi"}
                                ],
                            }
                        ]
                    )
                },
            }
        ),
        _span(
            {
                "span_id": "t1",
                "parent_span_id": "l1",
                "name": "execute_tool file_expense",
                "attributes": {"gen_ai.tool.name": "file_expense"},
                "status_code": 2,
                "events": [exception_event],
            }
        ),
    ]

    agent_data = converter.convert([spans])

    error_events = [
        e
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert len(error_events) == 1
    assert error_events[0].author == "expense_agent"
    fr = error_events[0].content.parts[0].function_response
    assert fr.name == "file_expense"
    assert fr.response == {"error": origin}


def test_convert_keeps_one_response_for_a_tool_error_the_model_saw() -> None:
    """A tool error both recorded as a tool result and on the span appears once.

    ADK Go returns a failed tool's error to the model as a function response
    and also marks the ``execute_tool`` span ERROR with the same message.
    """
    converter = TraceAgentDataConverter()
    tool_call = {
        "type": "tool_call",
        "id": "call_1",
        "name": "list_dir",
        "arguments": {"path": "missing"},
    }
    spans = [
        _agent_span("a1", "root", "pairgo"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "generate_content gemini-2.5-flash",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": "list missing"}
                                ],
                            }
                        ]
                    ),
                    "gen_ai.output.messages": json.dumps(
                        [{"role": "assistant", "parts": [tool_call]}]
                    ),
                },
            }
        ),
        _span(
            {
                "span_id": "t1",
                "parent_span_id": "a1",
                "name": "execute_tool list_dir",
                "attributes": {"gen_ai.tool.name": "list_dir"},
                "status_code": 2,
                "status_message": "no such dir",
            }
        ),
        _span(
            {
                "span_id": "l2",
                "parent_span_id": "a1",
                "name": "generate_content gemini-2.5-flash",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": "list missing"}
                                ],
                            },
                            {"role": "assistant", "parts": [tool_call]},
                            {
                                "role": "tool",
                                "parts": [
                                    {
                                        "type": "tool_call_response",
                                        "id": "call_1",
                                        "name": "list_dir",
                                        "response": {"error": "no such dir"},
                                    }
                                ],
                            },
                        ]
                    ),
                    "gen_ai.output.messages": json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [
                                    {
                                        "type": "text",
                                        "content": "That directory is missing.",
                                    }
                                ],
                            }
                        ]
                    ),
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    responses = [
        p.function_response
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert len(responses) == 1
    assert responses[0].name == "list_dir"
    assert responses[0].response == {"error": "no such dir"}


def test_convert_leaves_unresolvable_span_error_unattributed() -> None:
    """Ensure unresolvable root span errors remain unattributed rather than defaulting to an arbitrary agent."""
    converter = TraceAgentDataConverter()
    spans = [
        _span(
            {
                "span_id": "w1",
                "parent_span_id": None,
                "name": "invoke_workflow travel_desk_sr1",
                "status_code": 2,
                "status_message": "ValueError: Unknown expense category.",
            }
        ),
        _agent_span("a1", "w1", "travel_desk_sr1"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": "file my taxi"}
                                ],
                            }
                        ]
                    )
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    error_events = [
        e
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert len(error_events) == 1
    assert error_events[0].author != "travel_desk_sr1"
    assert error_events[0].author == "user"


def test_convert_attributes_sibling_llm_span_to_the_only_agent() -> None:
    """Ensure single-agent traces attribute sibling ``call_llm`` spans to the sole declared agent."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "travel_desk_sr1"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "root",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.output.messages": json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [{"type": "text", "content": "hi"}],
                            }
                        ]
                    )
                },
            }
        ),
    ]

    agent_data = converter.convert([spans])

    authors = [e.author for t in agent_data.turns for e in t.events]
    assert authors == ["travel_desk_sr1"]


def test_convert_surfaces_independent_tool_errors_separately() -> None:
    """Ensure independent sibling tool errors are each promoted without shadowing each other."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", None, "expense_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": "go"}],
                            }
                        ]
                    )
                },
            }
        ),
        _span(
            {
                "span_id": "t1",
                "parent_span_id": "l1",
                "name": "execute_tool file_expense",
                "attributes": {"gen_ai.tool.name": "file_expense"},
                "status_code": 2,
                "events": [
                    {
                        "name": "exception",
                        "attributes": {
                            "exception.message": "Unknown category."
                        },
                    }
                ],
            }
        ),
        _span(
            {
                "span_id": "t2",
                "parent_span_id": "l1",
                "name": "execute_tool lookup_policy",
                "attributes": {"gen_ai.tool.name": "lookup_policy"},
                "status_code": 2,
                "events": [
                    {
                        "name": "exception",
                        "attributes": {
                            "exception.message": "Policy not found."
                        },
                    }
                ],
            }
        ),
    ]

    agent_data = converter.convert([spans])

    errors = [
        (p.function_response.name, p.function_response.response)
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert errors == [
        ("file_expense", {"error": "Unknown category."}),
        ("lookup_policy", {"error": "Policy not found."}),
    ]


def test_convert_no_error_when_status_ok() -> None:
    """A healthy span produces no error function_response."""
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "x"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {
                    "gen_ai.input.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": "hi"}],
                            }
                        ]
                    )
                },
                "status_code": 1,
            }
        ),
    ]

    agent_data = converter.convert([spans])

    errors = [
        p
        for t in agent_data.turns
        for e in t.events
        for p in e.content.parts
        if p.function_response is not None
    ]
    assert errors == []


def test_convert_raises_on_truncated_message_json() -> None:
    """A truncated (unparseable) message payload raises MessageParseError."""
    converter = TraceAgentDataConverter()
    truncated = '[{"role":"user","parts":[{"type":"text","content":"hel'
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _span(
            {
                "span_id": "l1",
                "parent_span_id": "a1",
                "name": "call_llm",
                "attributes": {"gen_ai.input.messages": truncated},
            }
        ),
    ]

    with pytest.raises(MessageParseError):
        converter.convert([spans])


def test_convert_empty_traces_yields_no_turns() -> None:
    converter = TraceAgentDataConverter()
    assert converter.convert([]).turns == []
    assert converter.convert([[]]).turns == []


def test_content_attribute_keys_match_the_opentelemetry_constants() -> None:
    # These four keys drive the content-resolution cascade, so a rename upstream
    # would empty every replayed conversation rather than raise. Spelled out
    # above to keep the module import-light; pinned here instead.
    from ambient_quality_agent.tools.ingestion import trace_converter
    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes

    assert trace_converter._CONTENT_KEYS == (
        gen_ai_attributes.GEN_AI_INPUT_MESSAGES,
        gen_ai_attributes.GEN_AI_OUTPUT_MESSAGES,
        gen_ai_attributes.GEN_AI_SYSTEM_INSTRUCTIONS,
        gen_ai_attributes.GEN_AI_TOOL_DEFINITIONS,
    )


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


def _build_placeholder(data: bytes, mime_type: str = "image/png") -> str:
    """Builds the attachment placeholder the converter emits for inline bytes.

    Args:
        data: The attachment bytes.
        mime_type: The attachment MIME type.

    Returns:
        The expected placeholder text.
    """
    digest = hashlib.sha256(data).hexdigest()[:8]
    return f"<attachment: {mime_type}, {len(data)} bytes, sha256:{digest}>"


def _build_otel_llm_span(
    span_id: str, parent_span_id: str, **attrs: Any
) -> Span:
    """Builds a ``call_llm`` span carrying the given OTEL attributes.

    Args:
        span_id: Span id.
        parent_span_id: Parent span id.
        **attrs: Span attributes.

    Returns:
        Constructed Span instance.
    """
    return _span(
        {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": "call_llm",
            "attributes": attrs,
        }
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            json.dumps(
                [
                    {"type": "text", "content": "A"},
                    {"type": "text", "content": "B"},
                ]
            ),
            id="json-array",
        ),
        pytest.param(
            json.dumps({"type": "text", "content": "A"})
            + "\n"
            + json.dumps({"type": "text", "content": "B"}),
            id="jsonl",
        ),
        pytest.param(
            json.dumps(
                {"role": "system", "parts": [{"text": "A"}, {"text": "B"}]}
            ),
            id="content-json",
        ),
        pytest.param(
            {"role": "system", "parts": [{"text": "A"}, {"text": "B"}]},
            id="content-mapping",
        ),
        pytest.param(["A", "B"], id="string-list"),
        pytest.param(
            [
                {"type": "text", "content": "A"},
                {"type": "blob", "content": _PNG_B64, "mime_type": "image/png"},
                {"type": "text", "content": "B"},
            ],
            id="blob-skipped",
        ),
    ],
)
def test_parse_system_instruction_joins_every_text_part(value: Any) -> None:
    assert trace_converter._parse_system_instruction(value) == "A\n\nB"


def test_parse_system_instruction_keeps_plain_text() -> None:
    assert trace_converter._parse_system_instruction("Be brief.") == "Be brief."


def _build_instruction_parts_json() -> str:
    """Builds a JSON array of instruction parts, one of them a blob.

    Returns:
        The serialized parts.
    """
    return json.dumps(
        [
            {"type": "text", "content": "A"},
            {"type": "blob", "content": _PNG_B64, "mime_type": "image/png"},
            {"type": "text", "content": "B"},
        ]
    )


def _build_instruction_parts_jsonl() -> str:
    """Builds JSONL instruction parts, one per line.

    Returns:
        The serialized parts.
    """
    return "\n".join(
        json.dumps(part)
        for part in (
            {"type": "text", "content": "A"},
            {"type": "text", "content": "B"},
        )
    )


@pytest.mark.parametrize(
    "instructions",
    [
        pytest.param(_build_instruction_parts_json(), id="json-array"),
        pytest.param(_build_instruction_parts_jsonl(), id="jsonl"),
    ],
)
def test_convert_joins_multi_part_system_instructions_from_span(
    instructions: str,
) -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _build_otel_llm_span(
            "l1",
            "a1",
            **{
                "gen_ai.system_instructions": instructions,
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": "user",
                            "parts": [{"type": "text", "content": "hi"}],
                        }
                    ]
                ),
            },
        ),
    ]

    agent_data = converter.convert([spans])

    assert agent_data.agents["car_service_agent"].instruction == "A\n\nB"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            {"role": "system", "parts": [{"text": "A"}, {"text": "B"}]},
            id="content-dict",
        ),
        pytest.param(
            [
                {"type": "text", "content": "A"},
                {"type": "text", "content": "B"},
            ],
            id="part-list",
        ),
    ],
)
def test_convert_joins_structured_system_instructions_from_logs(
    content: Any,
) -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _build_otel_llm_span("l1", "a1"),
    ]
    logs = {
        "l1": [
            _log_entry("gen_ai.system.message", content),
            _log_entry(
                "gen_ai.user.message",
                {"role": "user", "parts": [{"text": "hi"}]},
            ),
        ]
    }

    agent_data = converter.convert([spans], logs)

    assert agent_data.agents["car_service_agent"].instruction == "A\n\nB"


def test_convert_joins_a_structured_llm_request_system_instruction() -> None:
    converter = TraceAgentDataConverter()
    request = {
        "config": {
            "system_instruction": {
                "role": "user",
                "parts": [{"text": "A"}, {"text": "B"}],
            }
        },
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
    }
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span("l1", "a1", request=request),
    ]

    agent_data = converter.convert([spans])

    assert agent_data.agents["car_service_agent"].instruction == "A\n\nB"


def _build_image_request(*images: bytes) -> dict[str, Any]:
    """Builds an ``llm_request`` with one user message per image.

    Args:
        *images: Image bytes, sent as native inline data.

    Returns:
        The request payload.
    """
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "data": base64.b64encode(image).decode(),
                            "mime_type": "image/png",
                        }
                    }
                ],
            }
            for image in images
        ]
    }


def test_convert_keeps_distinct_same_size_images_and_dedupes_replays() -> None:
    converter = TraceAgentDataConverter()
    first = b"\x01" * 32
    second = b"\x02" * 32
    turn1 = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span("l1", "a1", request=_build_image_request(first)),
    ]
    # Turn 2 replays the first image as history before sending the second.
    turn2 = [
        _agent_span("a2", "root", "car_service_agent"),
        _llm_span("l2", "a2", request=_build_image_request(first, second)),
    ]

    agent_data = converter.convert([turn1, turn2])

    texts = [
        [event.content.parts[0].text for event in turn.events]
        for turn in agent_data.turns
    ]
    assert texts == [[_build_placeholder(first)], [_build_placeholder(second)]]


def test_parse_tools_list_maps_built_in_tools_in_every_shape_alike() -> None:
    otel = trace_converter.parse_tools_list(
        [{"name": "google_search", "type": "google_search"}]
    )
    snake = trace_converter.parse_tools_list([{"google_search": {}}])
    camel = trace_converter.parse_tools_list(
        [{"googleSearch": {}, "functionDeclarations": [{"name": "find_slot"}]}]
    )

    assert otel[0].function_declarations is not None
    assert [d.name for d in otel[0].function_declarations] == ["google_search"]
    assert trace_converter.is_equivalent_toolset(otel, snake)
    assert camel[0].function_declarations is not None
    assert {d.name for d in camel[0].function_declarations} == {
        "google_search",
        "find_slot",
    }


def test_parse_tools_list_reads_bare_tool_names() -> None:
    """The BigQuery Agent Analytics plugin can log tools by name alone."""
    tools = trace_converter.parse_tools_list(
        ["transfer_to_agent", "", "lookup"]
    )

    assert tools[0].function_declarations is not None
    assert [
        (d.name, d.parameters_json_schema)
        for d in tools[0].function_declarations
    ] == [("transfer_to_agent", None), ("lookup", None)]


def test_parse_tools_list_reads_names_and_declarations_mixed() -> None:
    tools = trace_converter.parse_tools_list(
        [
            "transfer_to_agent",
            {
                "name": "lookup",
                "description": "Look a record up.",
                "parameters": {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                },
            },
        ]
    )

    assert tools[0].function_declarations is not None
    first, second = tools[0].function_declarations
    assert first.name == "transfer_to_agent"
    assert second.name == "lookup"
    assert second.description == "Look a record up."
    assert second.parameters_json_schema == {
        "type": "object",
        "properties": {"record_id": {"type": "string"}},
    }


_LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "record_id": {"type": "string"},
        "region": {"type": "string"},
    },
    "required": ["record_id"],
}


@pytest.mark.parametrize(
    ("parameters_key", "response_key"),
    [
        ("parameters_json_schema", "response_json_schema"),
        ("parametersJsonSchema", "responseJsonSchema"),
    ],
)
def test_convert_keeps_the_json_schema_of_a_dumped_genai_declaration(
    parameters_key: str, response_key: str
) -> None:
    """ADK dumps a FunctionTool's declaration with its JSON schema fields, so
    reading only ``parameters`` would drop every argument name."""
    converter = TraceAgentDataConverter()
    request = {
        "config": {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "lookup",
                            "description": "Look a record up.",
                            parameters_key: _LOOKUP_SCHEMA,
                            response_key: {"type": "object"},
                        }
                    ]
                }
            ]
        },
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
    }
    spans = [
        _agent_span("a1", "root", "records_agent"),
        _llm_span("l1", "a1", request=request),
    ]

    agent_data = converter.convert([spans])

    tools = agent_data.agents["records_agent"].tools
    assert tools and tools[0].function_declarations
    decl = tools[0].function_declarations[0]
    assert decl.parameters_json_schema == _LOOKUP_SCHEMA
    assert decl.response_json_schema == {"type": "object"}


def test_convert_treats_a_request_schema_and_otel_definitions_as_one_toolset() -> (
    None
):
    """A span records its toolset twice: as dumped genai declarations in the
    request and as OTel definitions. Both carry the same schema, so they must
    not read as a toolset change."""
    converter = TraceAgentDataConverter()

    def _build_turn(index: int, text: str) -> list[Span]:
        return [
            _agent_span(f"a{index}", "root", "records_agent"),
            _build_otel_llm_span(
                f"l{index}",
                f"a{index}",
                **{
                    "gcp.vertex.agent.llm_request": json.dumps(
                        {
                            "config": {
                                "tools": [
                                    {
                                        "function_declarations": [
                                            {
                                                "name": "lookup",
                                                "description": "Look up.",
                                                "parameters_json_schema": (
                                                    _LOOKUP_SCHEMA
                                                ),
                                            }
                                        ]
                                    }
                                ]
                            },
                            "contents": [
                                {"role": "user", "parts": [{"text": text}]}
                            ],
                        }
                    ),
                    "gen_ai.tool.definitions": json.dumps(
                        [
                            {
                                "type": "function",
                                "name": "lookup",
                                "description": "Look up.",
                                "parameters": _LOOKUP_SCHEMA,
                            }
                        ]
                    ),
                },
            ),
        ]

    agent_data = converter.convert(
        [_build_turn(1, "hi"), _build_turn(2, "more")]
    )

    tools = agent_data.agents["records_agent"].tools
    assert tools and tools[0].function_declarations
    assert tools[0].function_declarations[0].parameters_json_schema == (
        _LOOKUP_SCHEMA
    )
    assert len(agent_data.turns) == 2
    assert all(
        event.active_tools is None
        for turn in agent_data.turns
        for event in turn.events
    )


def test_convert_treats_legacy_and_otel_built_in_tools_as_one_toolset() -> None:
    converter = TraceAgentDataConverter()
    turn1 = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span(
            "l1",
            "a1",
            request={
                "config": {"tools": [{"google_search": {}}]},
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            },
        ),
    ]
    turn2 = [
        _agent_span("a2", "root", "car_service_agent"),
        _build_otel_llm_span(
            "l2",
            "a2",
            **{
                "gen_ai.tool.definitions": json.dumps(
                    [{"name": "google_search", "type": "google_search"}]
                ),
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": "user",
                            "parts": [{"type": "text", "content": "more"}],
                        }
                    ]
                ),
            },
        ),
    ]

    agent_data = converter.convert([turn1, turn2])

    assert len(agent_data.turns) == 2
    assert all(
        event.active_tools is None
        for turn in agent_data.turns
        for event in turn.events
    )


def _build_image_message_spans(
    blob_data: str, *, otel_first: bool
) -> list[Span]:
    """Builds ADK's two records of one user message that carries an image.

    ``llm_request`` drops the inline data; ``gen_ai.input.messages`` keeps it.

    Args:
        blob_data: The blob ``data`` value in the OTEL copy.
        otel_first: Whether the OTEL copy's span comes first.

    Returns:
        The agent span followed by both content spans.
    """
    legacy = _llm_span(
        "l1",
        "a1",
        request={
            "contents": [{"role": "user", "parts": [{"text": "What is this?"}]}]
        },
    )
    otel = _build_otel_llm_span(
        "g1",
        "a1",
        **{
            "gen_ai.input.messages": json.dumps(
                [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "type": "blob",
                                "data": blob_data,
                                "mime_type": "image/png",
                            },
                            {"type": "text", "content": "What is this?"},
                        ],
                    }
                ]
            )
        },
    )
    content_spans = [otel, legacy] if otel_first else [legacy, otel]
    return [_agent_span("a1", "root", "car_service_agent"), *content_spans]


@pytest.mark.parametrize("otel_first", [False, True])
def test_convert_merges_a_message_recorded_with_and_without_its_image(
    otel_first: bool,
) -> None:
    converter = TraceAgentDataConverter()

    agent_data = converter.convert(
        [
            _build_image_message_spans(
                "<not serializable>", otel_first=otel_first
            )
        ]
    )

    events = agent_data.turns[0].events
    assert len(events) == 1
    assert [part.text for part in events[0].content.parts] == [
        "<attachment: image/png, size unknown>",
        "What is this?",
    ]


def test_convert_keeps_the_same_question_about_a_new_decodable_image_in_a_later_turn() -> (
    None
):
    converter = TraceAgentDataConverter()
    first = b"\x01" * 32
    second = b"\x02" * 32
    turn1 = _build_image_message_spans(
        base64.b64encode(first).decode(), otel_first=False
    )
    turn2 = _build_image_message_spans(
        base64.b64encode(second).decode(), otel_first=False
    )

    agent_data = converter.convert([turn1, turn2])

    assert [
        [part.text for event in turn.events for part in event.content.parts]
        for turn in agent_data.turns
    ] == [
        [_build_placeholder(first), "What is this?"],
        [_build_placeholder(second), "What is this?"],
    ]


_JPEG_BYTES = b"\xff\xd8\xff\xe0" + bytes(range(40))


def _build_inline_part(data: bytes, mime_type: str) -> dict[str, Any]:
    """Builds a native inline-data part, as ``llm_response`` records it.

    Args:
        data: The attachment bytes.
        mime_type: The attachment MIME type.

    Returns:
        The part, carrying the bytes as base64.
    """
    return {
        "inline_data": {
            "data": base64.b64encode(data).decode(),
            "mime_type": mime_type,
        }
    }


def _build_sentinel_blob_part(mime_type: str) -> dict[str, Any]:
    """Builds a blob part holding ADK's ``"<not serializable>"`` sentinel.

    Args:
        mime_type: The attachment MIME type.

    Returns:
        The part, as ``gen_ai.output.messages`` records it.
    """
    return {
        "type": "blob",
        "data": "<not serializable>",
        "mime_type": mime_type,
    }


def _build_generate_content_span(
    span_id: str, parent_span_id: str, output_parts: list[dict[str, Any]]
) -> Span:
    """Builds ADK's ``generate_content`` span recording one model reply.

    Args:
        span_id: Span id.
        parent_span_id: Parent span id, the ``call_llm`` span.
        output_parts: The reply's OTEL message parts.

    Returns:
        Constructed Span instance.
    """
    return _span(
        {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": "generate_content gemini-2.5-flash",
            "attributes": {
                "gen_ai.system": "gemini",
                "gen_ai.output.messages": json.dumps(
                    [{"role": "assistant", "parts": output_parts}]
                ),
            },
        }
    )


def _build_model_reply_spans(
    images: Sequence[tuple[bytes, str]],
    *,
    text: str | None = None,
    output_first: bool = False,
    span_prefix: str = "",
    parent_span_id: str = "a1",
) -> list[Span]:
    """Builds ADK's two records of one model reply.

    ``llm_response`` on the ``call_llm`` span keeps each image as base64
    inline data; ``gen_ai.output.messages`` on its child ``generate_content``
    span holds ADK's ``"<not serializable>"`` sentinel.

    Args:
        images: Each image's bytes and MIME type, in reply order.
        text: Optional reply text, sent before the images.
        output_first: Whether the ``generate_content`` span comes first.
        span_prefix: Prefix that keeps span ids unique across replies.
        parent_span_id: The owning agent's span id.

    Returns:
        Both content spans.
    """
    native_parts: list[dict[str, Any]] = []
    otel_parts: list[dict[str, Any]] = []
    if text is not None:
        native_parts.append({"text": text})
        otel_parts.append({"type": "text", "content": text})
    for data, mime_type in images:
        native_parts.append(_build_inline_part(data, mime_type))
        otel_parts.append(_build_sentinel_blob_part(mime_type))
    legacy = _llm_span(
        f"{span_prefix}l",
        parent_span_id,
        response={"content": {"role": "model", "parts": native_parts}},
    )
    otel = _build_generate_content_span(
        f"{span_prefix}g", f"{span_prefix}l", otel_parts
    )
    return [otel, legacy] if output_first else [legacy, otel]


def _list_turn_texts(agent_data: Any) -> list[list[list[str | None]]]:
    """Lists each event's part texts, turn by turn.

    Args:
        agent_data: Converted agent data.

    Returns:
        Part texts per event per turn.
    """
    return [
        [[part.text for part in event.content.parts] for event in turn.events]
        for turn in agent_data.turns
    ]


@pytest.mark.parametrize("output_first", [False, True])
def test_convert_merges_an_image_only_reply_recorded_by_both_sources(
    output_first: bool,
) -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        *_build_model_reply_spans(
            [(_PNG_BYTES, "image/png")], output_first=output_first
        ),
    ]

    agent_data = converter.convert([spans])

    events = agent_data.turns[0].events
    assert len(events) == 1
    assert events[0].author == "car_service_agent"
    assert _list_turn_texts(agent_data) == [[[_build_placeholder(_PNG_BYTES)]]]


@pytest.mark.parametrize("output_first", [False, True])
def test_convert_merges_a_text_and_image_reply_recorded_by_both_sources(
    output_first: bool,
) -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        *_build_model_reply_spans(
            [(_PNG_BYTES, "image/png")],
            text="Here it is.",
            output_first=output_first,
        ),
    ]

    agent_data = converter.convert([spans])

    assert _list_turn_texts(agent_data) == [
        [["Here it is.", _build_placeholder(_PNG_BYTES)]]
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            (_PNG_BYTES, "image/png"),
            (_JPEG_BYTES, "image/jpeg"),
            id="different-mime-type",
        ),
        pytest.param(
            (b"\x01" * 32, "image/png"),
            (b"\x02" * 32, "image/png"),
            id="different-digest",
        ),
    ],
)
def test_convert_keeps_two_different_image_only_replies_in_one_turn_apart(
    first: tuple[bytes, str], second: tuple[bytes, str]
) -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        *_build_model_reply_spans([first], span_prefix="r1"),
        *_build_model_reply_spans([second], span_prefix="r2"),
    ]

    agent_data = converter.convert([spans])

    assert _list_turn_texts(agent_data) == [
        [
            [_build_placeholder(*first)],
            [_build_placeholder(*second)],
        ]
    ]


def test_convert_skips_a_text_and_image_reply_replayed_in_a_later_turn() -> (
    None
):
    converter = TraceAgentDataConverter()
    question = {"role": "user", "parts": [{"text": "Draw a cat."}]}
    turn1 = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span("q1", "a1", request={"contents": [question]}),
        *_build_model_reply_spans(
            [(_PNG_BYTES, "image/png")], text="Here it is."
        ),
    ]
    # ADK's llm_request replays the reply without its inline data, and
    # gen_ai.input.messages replays it with the blob sentinel.
    replayed_reply = {"role": "model", "parts": [{"text": "Here it is."}]}
    follow_up = {"role": "user", "parts": [{"text": "Now a dog."}]}
    turn2 = [
        _agent_span("a2", "root", "car_service_agent"),
        _llm_span(
            "l2",
            "a2",
            request={"contents": [question, replayed_reply, follow_up]},
            response={
                "content": {"role": "model", "parts": [{"text": "Done."}]}
            },
        ),
        _build_otel_llm_span(
            "g2",
            "a2",
            **{
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [
                                {"type": "text", "content": "Here it is."},
                                {
                                    "type": "blob",
                                    "data": "<not serializable>",
                                    "mime_type": "image/png",
                                },
                            ],
                        }
                    ]
                )
            },
        ),
    ]

    agent_data = converter.convert([turn1, turn2])

    assert _list_turn_texts(agent_data) == [
        [["Draw a cat."], ["Here it is.", _build_placeholder(_PNG_BYTES)]],
        [["Now a dog."], ["Done."]],
    ]


def test_convert_keeps_text_and_image_replies_of_different_types_apart() -> (
    None
):
    converter = TraceAgentDataConverter()
    # gen_ai capture alone: no digest tells the two images apart.
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _build_generate_content_span(
            "g1",
            "a1",
            [
                {"type": "text", "content": "Here."},
                _build_sentinel_blob_part("image/png"),
            ],
        ),
        _build_generate_content_span(
            "g2",
            "a1",
            [
                {"type": "text", "content": "Here."},
                _build_sentinel_blob_part("image/jpeg"),
            ],
        ),
    ]

    agent_data = converter.convert([spans])

    assert _list_turn_texts(agent_data) == [
        [
            ["Here.", "<attachment: image/png, size unknown>"],
            ["Here.", "<attachment: image/jpeg, size unknown>"],
        ]
    ]


def test_convert_keeps_a_reply_with_an_extra_image_apart() -> None:
    converter = TraceAgentDataConverter()
    first = b"\x01" * 32
    second = b"\x02" * 32
    spans = [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span(
            "l1",
            "a1",
            response={
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Here."},
                        _build_inline_part(first, "image/png"),
                    ],
                }
            },
        ),
        _llm_span(
            "l2",
            "a1",
            response={
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Here."},
                        _build_inline_part(first, "image/png"),
                        _build_inline_part(second, "image/png"),
                    ],
                }
            },
        ),
    ]

    agent_data = converter.convert([spans])

    assert _list_turn_texts(agent_data) == [
        [
            ["Here.", _build_placeholder(first)],
            [
                "Here.",
                _build_placeholder(first),
                _build_placeholder(second),
            ],
        ]
    ]


def test_convert_does_not_merge_messages_from_different_agents() -> None:
    converter = TraceAgentDataConverter()
    spans = [
        _agent_span("a1", "root", "planner"),
        _agent_span("a2", "root", "illustrator"),
        _llm_span(
            "l1",
            "a1",
            response={
                "content": {"role": "model", "parts": [{"text": "Done."}]}
            },
        ),
        *_build_model_reply_spans(
            [(_PNG_BYTES, "image/png")],
            text="Done.",
            span_prefix="r2",
            parent_span_id="a2",
        ),
    ]

    agent_data = converter.convert([spans])

    events = agent_data.turns[0].events
    assert [event.author for event in events] == ["planner", "illustrator"]
    assert _list_turn_texts(agent_data) == [
        [["Done."], ["Done.", _build_placeholder(_PNG_BYTES)]]
    ]


def _build_two_image_reply_spans(
    output_parts: list[dict[str, Any]], native_images: Sequence[bytes]
) -> list[Span]:
    """Builds a two-image PNG reply recorded by both sources.

    Args:
        output_parts: The ``gen_ai.output.messages`` copy's image parts.
        native_images: The ``llm_response`` copy's image bytes, in order.

    Returns:
        The agent span followed by both content spans.
    """
    return [
        _agent_span("a1", "root", "car_service_agent"),
        _llm_span(
            "l1",
            "a1",
            response={
                "content": {
                    "role": "model",
                    "parts": [
                        _build_inline_part(image, "image/png")
                        for image in native_images
                    ],
                }
            },
        ),
        _build_generate_content_span("g1", "l1", output_parts),
    ]


def test_convert_merges_copies_whose_digests_agree_in_place() -> None:
    converter = TraceAgentDataConverter()
    first = b"\x01" * 32
    second = b"\x02" * 32
    spans = _build_two_image_reply_spans(
        [
            {
                "type": "blob",
                "data": base64.b64encode(first).decode(),
                "mime_type": "image/png",
            },
            _build_sentinel_blob_part("image/png"),
        ],
        [first, second],
    )

    agent_data = converter.convert([spans])

    assert _list_turn_texts(agent_data) == [
        [[_build_placeholder(first), _build_placeholder(second)]]
    ]


def test_convert_keeps_copies_whose_digests_differ_in_place_apart() -> None:
    converter = TraceAgentDataConverter()
    first = b"\x01" * 32
    second = b"\x02" * 32
    spans = _build_two_image_reply_spans(
        [
            {
                "type": "blob",
                "data": base64.b64encode(first).decode(),
                "mime_type": "image/png",
            },
            _build_sentinel_blob_part("image/png"),
        ],
        [second, first],
    )

    agent_data = converter.convert([spans])

    assert _list_turn_texts(agent_data) == [
        [
            [_build_placeholder(second), _build_placeholder(first)],
            [
                _build_placeholder(first),
                "<attachment: image/png, size unknown>",
            ],
        ]
    ]
