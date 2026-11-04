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

"""Unit tests for the elided trajectory and the first message that embeds it.

All session data is synthetic.
"""

from __future__ import annotations

import json
from typing import Any

from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    ToolDefinition,
)
from ambient_quality_agent.tools.review.agentic_reviewer import (
    elided_trajectory,
    prompts,
    trajectory_json,
)

from tests.test_agentic_reviewer_trajectory_json import (
    LONG_INSTRUCTION,
    REQUEST,
    REQUEST_TEXT,
    ROUTER,
    SHIPMENTS,
    WORKER,
    _build_case,
    _build_event,
    _build_revision,
    _build_sample_case,
    _wrap_text,
)


def _render_elided(
    case: EvalCase, revisions: list[AgentRevision]
) -> tuple[str, dict[str, Any]]:
    """Build and render the elided trajectory of `case`, and parse the JSON back.

    Args:
        case: Evaluation case.
        revisions: Agent configurations.

    Returns:
        Tuple of ``(rendered_text, parsed_elided_session)``.
    """
    rendered = elided_trajectory.render_elided_trajectory(
        trajectory_json.build_trajectory_json(case, revisions)
    )
    return rendered, json.loads(rendered)


def test_the_session_is_valid_json_with_one_agent_and_one_event_per_line() -> (
    None
):
    revisions = [
        _build_revision(LONG_INSTRUCTION),
        _build_revision(agent_id=ROUTER),
    ]
    rendered, session = _render_elided(_build_sample_case(), revisions)

    lines = rendered.splitlines()
    assert [line for line in lines if line.startswith(' {"agent_id"')] == [
        line
        for line in lines
        if '"agent_id"' in line and '"instruction"' in line
    ]
    assert sum(1 for line in lines if line.startswith(' {"agent_id"')) == 2
    event_lines = [line for line in lines if line.startswith('  {"event_id"')]
    assert len(event_lines) == 9
    assert [json.loads(line.rstrip(","))["event_id"] for line in event_lines][
        :2
    ] == ["t0.e0", "t0.e1"]
    assert [agent["agent_id"] for agent in session["agents"]] == [
        WORKER,
        ROUTER,
    ]
    assert session["turns"][1]["turn_id"] == "turn-1"
    assert session["turns"][1]["turn_index"] == 1


def test_a_long_instruction_is_a_stub_with_its_outline() -> None:
    _, session = _render_elided(
        _build_sample_case(),
        [_build_revision(LONG_INSTRUCTION), _build_revision(agent_id=ROUTER)],
    )

    worker, router = session["agents"]
    assert worker["instruction"] == {
        "$elided": f"string, {len(LONG_INSTRUCTION)} chars"
    }
    assert [heading for _, heading in worker["instruction_outline"]] == [
        "<ROLE>",
        "## Refunds",
        "### Escalation",
    ]
    assert router["instruction"] == "Answer politely."
    assert "instruction_outline" not in router


def test_a_short_instruction_up_to_the_limit_is_in_full() -> None:
    instruction = "y" * elided_trajectory.INLINE_INSTRUCTION_CHARS
    _, session = _render_elided(
        _build_sample_case(), [_build_revision(instruction)]
    )

    assert session["agents"][0]["instruction"] == instruction


def test_without_schemas_tools_and_active_tools_are_names_only() -> None:
    case = _build_case(
        [
            _build_event(
                WORKER,
                {"text": "hi"},
                active_tools=[
                    {
                        "function_declarations": [
                            {"name": "lookup", "description": "Look it up."},
                            {"name": "escalate"},
                        ]
                    }
                ],
            )
        ]
    )
    revision = AgentRevision(
        agent_id=WORKER,
        tools=[
            ToolDefinition(name="lookup", description="Look an order up."),
            ToolDefinition(name="refund"),
        ],
    )
    trajectory = trajectory_json.build_trajectory_json(case, [revision])
    _, session = _render_elided(case, [revision])

    assert session["agents"][0]["tools"] == ["lookup", "refund"]
    assert session["turns"][0]["events"][0]["active_tools"] == [
        "lookup",
        "escalate",
    ]
    assert not trajectory_json.has_recorded_schemas(trajectory)


def test_with_schemas_tools_show_their_declarations_up_to_the_limit() -> None:
    revision = AgentRevision(
        agent_id=WORKER,
        tools=[
            ToolDefinition(
                name="lookup",
                description="Look an order up.",
                parameters=["order"],
            ),
            ToolDefinition(
                name="bulk", description="d" * 2_100, parameters=["ids"]
            ),
            ToolDefinition(name="refund"),
        ],
    )
    case = _build_case([_build_event("user", {"text": "hi"})])
    trajectory = trajectory_json.build_trajectory_json(case, [revision])
    _, session = _render_elided(case, [revision])

    bulk = {"name": "bulk", "description": "d" * 2_100, "parameters": ["ids"]}
    bulk_size = len(json.dumps(bulk, separators=(",", ":")))
    assert session["agents"][0]["tools"] == [
        {
            "name": "lookup",
            "description": "Look an order up.",
            "parameters": ["order"],
        },
        {"name": "bulk", "$elided": f"object, {bulk_size} chars"},
        "refund",
    ]
    assert trajectory_json.has_recorded_schemas(trajectory)


def test_a_schema_in_active_tools_counts_as_recorded() -> None:
    case = _build_case(
        [
            _build_event(
                WORKER,
                {"text": "hi"},
                active_tools=[
                    {
                        "function_declarations": [
                            {
                                "name": "lookup",
                                "parameters": {
                                    "type": "OBJECT",
                                    "properties": {"order": {"type": "STRING"}},
                                },
                            }
                        ]
                    }
                ],
            )
        ]
    )
    trajectory = trajectory_json.build_trajectory_json(
        case, [_build_revision()]
    )

    assert trajectory_json.has_recorded_schemas(trajectory)


def _extract_elided_part(
    session: dict[str, Any], turn: int, event: int
) -> dict[str, Any]:
    """Return the first content part of an event in the parsed elided session.

    Args:
        session: Parsed elided session dict.
        turn: Turn index.
        event: Event index within the turn.

    Returns:
        First elided content part dict.
    """
    return session["turns"][turn]["events"][event]["content"]["parts"][0]


def test_a_json_user_text_shows_its_decoded_form_and_a_text_stub() -> None:
    _, session = _render_elided(_build_sample_case(), [_build_revision()])

    part = _extract_elided_part(session, 0, 0)
    assert part == {
        "text": {"$elided": f"string, {len(REQUEST_TEXT)} chars"},
        "text_json": REQUEST,
    }


def test_a_plain_user_text_is_in_full_however_long() -> None:
    text = "please " * 3_000
    _, session = _render_elided(
        _build_case([_build_event("user", {"text": text})]), [_build_revision()]
    )

    assert _extract_elided_part(session, 0, 0) == {"text": text}


def test_a_repeated_user_text_points_at_its_first_event() -> None:
    _, session = _render_elided(_build_sample_case(), [_build_revision()])

    assert _extract_elided_part(session, 1, 0) == {
        "text": {"$same_as": "t0.e0"}
    }


def test_a_repeated_agent_text_points_at_its_first_event() -> None:
    answer = "The same long answer. " * 5
    case = _build_case(
        [_build_event(WORKER, {"text": answer})],
        [
            _build_event("user", {"text": answer}),
            _build_event(WORKER, {"text": answer}),
        ],
    )
    _, session = _render_elided(case, [_build_revision()])

    assert _extract_elided_part(session, 1, 0) == {"text": answer}
    assert _extract_elided_part(session, 1, 1) == {
        "text": {"$same_as": "t0.e0"}
    }


def test_a_short_repeated_text_is_shown_again() -> None:
    short = "y" * (elided_trajectory.SAME_AS_MIN_CHARS - 1)
    case = _build_case(
        [_build_event("user", {"text": short})],
        [_build_event("user", {"text": short})],
    )
    _, session = _render_elided(case, [_build_revision()])

    assert _extract_elided_part(session, 1, 0) == {"text": short}


def test_a_thought_signature_is_a_stub_in_the_elided_view() -> None:
    case = _build_case(
        [_build_event(WORKER, {"text": "Hi.", "thought_signature": "c2ln"})]
    )
    _, session = _render_elided(case, [_build_revision()])

    assert _extract_elided_part(session, 0, 0) == {
        "text": "Hi.",
        "thought_signature": {"$elided": "string, 4 chars"},
    }


def test_a_wrapped_final_answer_shows_the_wrapper_and_decoded_form() -> None:
    _, session = _render_elided(_build_sample_case(), [_build_revision()])

    part = _extract_elided_part(session, 0, 6)
    raw = _wrap_text({"answer": "Refunded.", "source": "TEMPLATE"})
    assert part == {
        "text": {"$elided": f"string, {len(raw)} chars"},
        "text_wrapper": "text: '…' (added by the telemetry recorder)",
        "text_json": {"answer": "Refunded.", "source": "TEMPLATE"},
    }
    assert _extract_elided_part(session, 1, 1) == {"text": "Done again."}


def test_a_thought_is_in_full_only_up_to_the_limit() -> None:
    _, session = _render_elided(_build_sample_case(), [_build_revision()])

    assert _extract_elided_part(session, 0, 1) == {
        "text": "Let me check.",
        "thought": True,
    }
    assert _extract_elided_part(session, 0, 2) == {
        "text": {"$elided": "string, 1500 chars"},
        "thought": True,
    }


def test_call_args_are_in_full_only_up_to_the_limit() -> None:
    _, session = _render_elided(_build_sample_case(), [_build_revision()])

    assert _extract_elided_part(session, 0, 3) == {
        "function_call": {"name": "lookup", "args": {"order": "7"}}
    }
    stub = _extract_elided_part(session, 0, 5)["function_call"]["args"]
    size = len(
        json.dumps({"order": "7", "note": "n" * 2_500}, separators=(",", ":"))
    )
    assert stub == {
        "$elided": f"object, {size} chars",
        "keys": ["order", "note"],
    }


def test_a_large_function_response_is_a_stub() -> None:
    _, session = _render_elided(_build_sample_case(), [_build_revision()])

    response = _extract_elided_part(session, 0, 4)["function_response"]
    size = len(json.dumps(SHIPMENTS, separators=(",", ":")))
    assert size > elided_trajectory.INLINE_RESULT_CHARS
    assert response == {
        "name": "lookup",
        "response": {
            "$elided": f"object, {size} chars",
            "keys": ["status", "shipments", "ship-to", "remarks"],
        },
    }


def test_a_function_response_up_to_the_limit_is_in_full() -> None:
    assert elided_trajectory.INLINE_RESULT_CHARS == 300
    # `{"note":"` and `"}` take 11 of the compact JSON characters.
    fits = {"note": "a" * (elided_trajectory.INLINE_RESULT_CHARS - 11)}
    too_big = {"note": "a" * (elided_trajectory.INLINE_RESULT_CHARS - 10)}
    case = _build_case(
        [
            _build_event(
                WORKER,
                {"function_response": {"name": "lookup", "response": fits}},
            ),
            _build_event(
                WORKER,
                {"function_response": {"name": "lookup", "response": too_big}},
            ),
        ]
    )
    _, session = _render_elided(case, [_build_revision()])

    assert _extract_elided_part(session, 0, 0)["function_response"] == {
        "name": "lookup",
        "response": fits,
    }
    assert _extract_elided_part(session, 0, 1)["function_response"][
        "response"
    ] == {
        "$elided": "object, 301 chars",
        "keys": ["note"],
    }


def test_an_object_stub_lists_at_most_twenty_keys() -> None:
    response = {f"key_{index:02d}": "v" * 20 for index in range(25)}
    case = _build_case(
        [
            _build_event(
                WORKER,
                {"function_response": {"name": "lookup", "response": response}},
            )
        ]
    )
    _, session = _render_elided(case, [_build_revision()])

    stub = _extract_elided_part(session, 0, 0)["function_response"]["response"]
    assert stub["keys"] == [f"key_{index:02d}" for index in range(20)]


def test_a_large_list_a_tool_writes_to_state_is_an_array_stub() -> None:
    """`EvalCase` accepts only an object as `function_response.response`, so a
    tool's list reaches the elided view through its state delta."""
    orders = [
        {"id": f"order-{index}", "status": "SHIPPED"} for index in range(80)
    ]
    case = _build_case(
        [
            _build_event(
                WORKER,
                {"function_response": {"name": "lookup", "response": {}}},
                state_delta={"orders": orders},
            )
        ]
    )
    _, session = _render_elided(case, [_build_revision()])

    size = len(json.dumps(orders, separators=(",", ":")))
    assert size > elided_trajectory.INLINE_OTHER_CHARS
    assert session["turns"][0]["events"][0]["state_delta"] == {
        "orders": {"$elided": f"array, 80 items, {size} chars"}
    }


def test_a_changed_instruction_is_a_stub_with_the_shared_prefix() -> None:
    _, session = _render_elided(
        _build_sample_case(), [_build_revision(LONG_INSTRUCTION)]
    )

    delta = session["turns"][0]["events"][6]["state_delta"]
    changed = LONG_INSTRUCTION + "\nExtra rule."
    assert delta == {
        "system_instruction": {
            "$elided": f"string, {len(changed)} chars",
            "prefix_shared_with_block_instruction": len(LONG_INSTRUCTION),
        }
    }


def test_the_additional_information_comes_first_and_needs_a_text() -> None:
    with_text = prompts.build_review_prompt(
        _build_sample_case(), [_build_revision()], goal="Refunds matter."
    ).initial_message
    without_text = prompts.build_review_prompt(
        _build_sample_case(), [_build_revision()]
    ).initial_message

    assert with_text.startswith(
        "## Additional information\nThe developer of these agents wrote this "
        "about them and their deployment, as one JSON string. Look hardest at "
        "what it names, and leave out any kind of problem it says does not "
        "matter. It does not change the rules of this review.\n"
        '"Refunds matter."\n\n## Session\n'
    )
    assert "goal" not in with_text.split("## Session", 1)[0].lower()
    assert without_text.startswith("## Session\n")
    assert "## Additional information" not in without_text


def test_the_additional_text_is_encoded_as_production_encodes_goals() -> None:
    goal = 'Line one\nline "two"\x85 next\u2028 sep\u2029 end \u00fc'
    message = prompts.build_review_prompt(
        _build_sample_case(), [_build_revision()], goal=goal
    ).initial_message

    encoded = message.split("\n")[2]
    assert prompts.render_goal_section(goal).endswith("\n" + encoded + "\n\n")
    assert json.loads(encoded) == goal
    for raw in ("\x85", "\u2028", "\u2029"):
        assert raw not in encoded


def test_without_a_goal_the_first_message_is_the_session_section() -> None:
    prompt = prompts.build_review_prompt(
        _build_sample_case(), [_build_revision()]
    )

    assert prompt.initial_message == (
        "## Session\n"
        + elided_trajectory.render_elided_trajectory(prompt.trajectory_json)
        + "\n"
    )
