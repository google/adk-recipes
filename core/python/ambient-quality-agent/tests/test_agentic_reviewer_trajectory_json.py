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

"""Unit tests for the trajectory JSON that the reviewer's tools query.

The jq tool tests share the constants here, and the elided-trajectory tests
share the constants and builders.
All session data is synthetic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    ToolDefinition,
)
from ambient_quality_agent.tools.review.agentic_reviewer import trajectory_json

WORKER = "worker"
ROUTER = "router"

LONG_INSTRUCTION = (
    "<ROLE>\nYou are a support worker.\n"
    + "Filler line about policy.\n" * 300
    + "## Refunds\nNever refund an order that has shipped.\n"
    + "Filler line about policy.\n" * 300
    + "### Escalation\nEscalate when the customer asks twice.\n"
)


def _build_event(
    author: str, *parts: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    """Build a serialized event dict.

    Args:
        author: Event author.
        *parts: Content part dicts.
        **extra: Additional event fields (e.g., ``state_delta``).

    Returns:
        Serialized event dict.
    """
    role = "user" if author == "user" else "model"
    return {
        "author": author,
        "content": {"role": role, "parts": list(parts)},
        **extra,
    }


def _build_case(
    *turns: list[dict[str, Any]], case_id: str = "session-1"
) -> EvalCase:
    """Build an `EvalCase` from per-turn lists of serialized events.

    Args:
        *turns: Event lists for each turn.
        case_id: Evaluation case ID.

    Returns:
        Validated `EvalCase`.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {WORKER: {"agent_id": WORKER}},
                "turns": [
                    {
                        "turn_index": index,
                        "turn_id": f"turn-{index}",
                        "events": events,
                    }
                    for index, events in enumerate(turns)
                ],
            },
        }
    )


def _build_revision(
    instruction: str = "Answer politely.",
    agent_id: str = WORKER,
    tools: tuple[str, ...] = ("lookup", "refund"),
    parameters: list[str] | None = None,
) -> AgentRevision:
    """Build an `AgentRevision` for test fixtures.

    Args:
        instruction: Agent system instruction.
        agent_id: Agent identifier.
        tools: Tool names.
        parameters: Parameter names applied to each tool; None omits parameter
            schemas.

    Returns:
        Configured `AgentRevision`.
    """
    return AgentRevision(
        agent_id=agent_id,
        instruction=instruction,
        tools=[
            ToolDefinition(name=name, parameters=parameters) for name in tools
        ],
    )


def _wrap_text(payload: Any) -> str:
    """Wrap a JSON-encoded value in the telemetry recorder's ``text: '...'`` format.

    Args:
        payload: JSON-serializable value.

    Returns:
        Wrapped string ``text: '<json>'``.
    """
    return f"text: '{json.dumps(payload)}'"


SHIPMENTS = {
    "status": "ok",
    "shipments": [{"id": "S-1", "status": "SHIPPED", "weight_kg": 12}],
    "ship-to": {"city": "Springfield"},
    "remarks": "Parcel left the depot on time. " * 10,
}


REQUEST = {
    "caseNumber": "C-1",
    "emailBody": "Refund order 7",
    "signature": "Kind regards, a customer who is still waiting for a reply",
}
REQUEST_TEXT = json.dumps(REQUEST)


def _build_sample_case() -> EvalCase:
    """Build a two-turn evaluation case with varied event content.

    The first turn holds a JSON user text, thoughts, tool calls and a JSON
    answer wrapped by the telemetry recorder.

    Returns:
        Sample `EvalCase`.
    """
    request = REQUEST_TEXT
    return _build_case(
        [
            _build_event("user", {"text": request}),
            _build_event(WORKER, {"text": "Let me check.", "thought": True}),
            _build_event(WORKER, {"text": "x" * 1_500, "thought": True}),
            _build_event(
                WORKER,
                {"function_call": {"name": "lookup", "args": {"order": "7"}}},
            ),
            _build_event(
                WORKER,
                {
                    "function_response": {
                        "name": "lookup",
                        "response": SHIPMENTS,
                    }
                },
            ),
            _build_event(
                WORKER,
                {
                    "function_call": {
                        "name": "refund",
                        "args": {"order": "7", "note": "n" * 2_500},
                    }
                },
            ),
            _build_event(
                WORKER,
                {
                    "text": _wrap_text(
                        {"answer": "Refunded.", "source": "TEMPLATE"}
                    )
                },
                state_delta={
                    "system_instruction": LONG_INSTRUCTION + "\nExtra rule."
                },
            ),
        ],
        [
            _build_event("user", {"text": request}),
            _build_event(WORKER, {"text": "Done again."}),
        ],
    )


# --------------------------------------------------------------------------- #
# Trajectory JSON                                                             #
# --------------------------------------------------------------------------- #


def test_every_event_gets_its_event_id_first() -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_sample_case(), [_build_revision()]
    )

    ids = [
        [event["event_id"] for event in turn["events"]]
        for turn in trajectory["turns"]
    ]
    assert ids == [[f"t0.e{index}" for index in range(7)], ["t1.e0", "t1.e1"]]
    assert all(
        next(iter(event)) == "event_id"
        for turn in trajectory["turns"]
        for event in turn["events"]
    )


def test_a_recorded_event_id_does_not_replace_the_computed_one() -> None:
    event = trajectory_json._add_event_fields(
        {"author": WORKER, "event_id": "recorded"}, "t0.e0"
    )

    assert event == {"event_id": "t0.e0", "author": WORKER}
    assert next(iter(event)) == "event_id"


def _extract_first_part(
    trajectory: dict[str, Any], turn: int, event: int
) -> dict[str, Any]:
    """Return the first content part of an event in `trajectory`.

    Args:
        trajectory: Trajectory JSON.
        turn: Turn index.
        event: Event index within the turn.

    Returns:
        First content part dict.
    """
    return trajectory["turns"][turn]["events"][event]["content"]["parts"][0]


def test_a_wrapped_agent_text_is_unwrapped_and_decoded() -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_sample_case(), [_build_revision()]
    )

    part = _extract_first_part(trajectory, 0, 6)
    assert part["text"] == _wrap_text(
        {"answer": "Refunded.", "source": "TEMPLATE"}
    )
    assert part["text_wrapper"] == (
        "text: '…' (added by the telemetry recorder)"
    )
    assert part["text_json"] == {"answer": "Refunded.", "source": "TEMPLATE"}


def test_a_user_text_that_is_json_is_decoded_without_a_wrapper() -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_sample_case(), [_build_revision()]
    )

    part = _extract_first_part(trajectory, 0, 0)
    assert part["text_json"] == REQUEST
    assert "text_wrapper" not in part


def test_an_escaped_quote_inside_the_wrapper_is_unescaped() -> None:
    text = """text: '{"answer": "it\\'s done"}'"""
    trajectory = trajectory_json.build_trajectory_json(
        _build_case([_build_event(WORKER, {"text": text})]),
        [_build_revision()],
    )

    assert _extract_first_part(trajectory, 0, 0)["text_json"] == {
        "answer": "it's done"
    }


@pytest.mark.parametrize(
    ("author", "text", "wrapper", "decoded"),
    [
        (WORKER, "text: 'not json'", True, False),
        (WORKER, "Plain answer.", False, False),
        (WORKER, "42", False, False),
        ("user", _wrap_text({"a": 1}), False, False),
        ("user", "[1, 2]", False, True),
    ],
)
def test_only_json_objects_and_arrays_are_decoded(
    author: str, text: str, wrapper: bool, decoded: bool
) -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_case([_build_event(author, {"text": text})]),
        [_build_revision()],
    )

    part = _extract_first_part(trajectory, 0, 0)
    assert part["text"] == text
    assert ("text_wrapper" in part) is wrapper
    assert ("text_json" in part) is decoded


def test_a_thought_is_never_decoded() -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_case(
            [_build_event(WORKER, {"text": '{"a": 1}', "thought": True})]
        ),
        [_build_revision()],
    )

    assert "text_json" not in _extract_first_part(trajectory, 0, 0)


def test_the_outline_gives_the_character_offset_of_each_section() -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_sample_case(), [_build_revision(LONG_INSTRUCTION)]
    )

    offsets = trajectory["agents"][0]["instruction_outline"]
    assert [heading for _, heading in offsets] == [
        "<ROLE>",
        "## Refunds",
        "### Escalation",
    ]
    for offset, heading in offsets:
        assert LONG_INSTRUCTION[offset:].startswith(heading)


# --------------------------------------------------------------------------- #
# Tool schemas                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        pytest.param(
            {"name": "lookup", "parameters": ["order"]}, True, id="dict"
        ),
        pytest.param({"name": "lookup", "parameters": None}, False, id="none"),
        pytest.param({"name": "lookup"}, False, id="missing"),
        pytest.param("lookup", False, id="name_only"),
    ],
)
def test_only_a_tool_with_parameters_has_a_schema(
    tool: Any, expected: bool
) -> None:
    assert trajectory_json.has_tool_schema(tool) is expected


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [(["order"], True), (None, False)],
    ids=["parameters", "names_only"],
)
def test_agent_tool_parameters_count_as_recorded_schemas(
    parameters: list[str] | None, expected: bool
) -> None:
    trajectory = trajectory_json.build_trajectory_json(
        _build_sample_case(), [_build_revision(parameters=parameters)]
    )

    assert trajectory_json.has_recorded_schemas(trajectory) is expected


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        pytest.param(
            {"name": "lookup", "parameters": {}}, True, id="parameters"
        ),
        pytest.param(
            {"name": "lookup", "parameters_json_schema": {}},
            True,
            id="json_schema",
        ),
        pytest.param({"name": "lookup"}, False, id="no_schema"),
    ],
)
def test_active_tool_declarations_count_as_recorded_schemas(
    declaration: dict[str, Any], expected: bool
) -> None:
    trajectory = {
        "agents": [{"tools": ["lookup"]}],
        "turns": [
            {
                "events": [
                    {"active_tools": [{"function_declarations": [declaration]}]}
                ]
            }
        ],
    }

    assert trajectory_json.has_recorded_schemas(trajectory) is expected


# --------------------------------------------------------------------------- #
# Attachments                                                                  #
# --------------------------------------------------------------------------- #


def _build_attachment_trajectory(part: dict[str, Any]) -> dict[str, Any]:
    """Build the trajectory JSON of a one-event session whose user part is `part`.

    Args:
        part: The user's content part.

    Returns:
        The trajectory JSON.
    """
    return trajectory_json.build_trajectory_json(
        _build_case([_build_event("user", {"text": "book me a flight"}, part)]),
        [_build_revision()],
    )


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {"text": "<attachment: image/png, 2048 bytes, sha256:0a1b2c3d>"},
            id="inline_bytes",
        ),
        pytest.param(
            {"text": "<attachment: image/png, size unknown>"},
            id="inline_size_unknown",
        ),
        pytest.param(
            {
                "file_data": {
                    "file_uri": "gs://bucket/itinerary.pdf",
                    "mime_type": "application/pdf",
                }
            },
            id="file_data",
        ),
        pytest.param(
            {"text": "<attachment: application/pdf, file_id=file-123>"},
            id="file_id",
        ),
        pytest.param({"text": "<attachment: application/pdf>"}, id="no_uri"),
    ],
)
def test_every_attachment_reference_form_counts(part: dict[str, Any]) -> None:
    assert trajectory_json.has_attachment(_build_attachment_trajectory(part))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("see <attachment: image/png>", id="not_at_start"),
        pytest.param("<attachment: image/png> above", id="not_at_end"),
        pytest.param("<attachments>", id="other_tag"),
    ],
)
def test_text_that_only_mentions_an_attachment_does_not_count(
    text: str,
) -> None:
    assert not trajectory_json.has_attachment(
        _build_attachment_trajectory({"text": text})
    )


@pytest.mark.parametrize(
    "turns",
    [
        pytest.param([], id="no_turns"),
        pytest.param([{}], id="no_events"),
        pytest.param([{"events": None}], id="null_events"),
        pytest.param([{"events": [{"content": None}]}], id="null_content"),
        pytest.param(
            [{"events": [{"content": {"parts": None}}]}], id="null_parts"
        ),
        pytest.param(
            [{"events": [{"content": {"parts": [{"text": None}]}}]}],
            id="null_text",
        ),
        pytest.param(
            [{"events": [{"content": {"parts": [{"file_data": None}]}}]}],
            id="null_file_data",
        ),
    ],
)
def test_incomplete_turns_have_no_attachment(turns: list[dict]) -> None:
    """Verify missing or null turn fields are skipped rather than raising."""
    assert not trajectory_json.has_attachment({"turns": turns})
