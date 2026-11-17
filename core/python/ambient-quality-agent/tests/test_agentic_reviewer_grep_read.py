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

"""Unit tests for the session reviewer's `grep` and `read` tools.

Executes the tools against synthetic session data.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.agent_revision import AgentRevision
from ambient_quality_agent.tools.review.agentic_reviewer import trajectory_json
from ambient_quality_agent.tools.review.agentic_reviewer.tools import grep_read
from google.genai import types

from tests.test_agentic_reviewer_trajectory_json import (
    LONG_INSTRUCTION,
    REQUEST,
    SHIPMENTS,
    WORKER,
    _build_case,
    _build_event,
    _build_revision,
    _build_sample_case,
)

PAGED_INSTRUCTION = "".join(
    f"line {index:05d} of the rules\n" for index in range(3_000)
)

RULES = "\n".join(f"rule {number}" for number in range(1, 26))
"""A 25-line string whose line N reads ``rule N``."""

_HIT_LINE = re.compile(r"  (?P<line>\d+): (?P<text>.*)")


def _build_tools_from_trajectory(
    trajectory: dict[str, Any] | None = None,
) -> tuple[grep_read.GrepTool, grep_read.ReadTool]:
    """Build `GrepTool` and `ReadTool` bound to a trajectory JSON.

    Args:
        trajectory: Trajectory JSON; defaults to the sample session with
            `LONG_INSTRUCTION`.

    Returns:
        ``(GrepTool, ReadTool)`` tuple.
    """
    if trajectory is None:
        trajectory = trajectory_json.build_trajectory_json(
            _build_sample_case(),
            [_build_revision(LONG_INSTRUCTION)],
            outline_line_numbers=True,
        )
    return grep_read.GrepTool(trajectory), grep_read.ReadTool(trajectory)


def _build_tools(
    case: EvalCase | None = None, revisions: list[AgentRevision] | None = None
) -> tuple[grep_read.GrepTool, grep_read.ReadTool]:
    """Build `GrepTool` and `ReadTool` bound to the trajectory JSON of a case.

    Args:
        case: Evaluation case; defaults to `_build_sample_case()`.
        revisions: Agent configurations; defaults to a single worker with
            `LONG_INSTRUCTION`.

    Returns:
        ``(GrepTool, ReadTool)`` tuple.
    """
    return _build_tools_from_trajectory(
        trajectory_json.build_trajectory_json(
            case or _build_sample_case(),
            revisions or [_build_revision(LONG_INSTRUCTION)],
            outline_line_numbers=True,
        )
    )


def _get_tool_response(tool: Any, args: dict[str, Any]) -> dict[str, str]:
    """Execute a single tool call through `run_async`.

    Args:
        tool: `GrepTool` or `ReadTool` instance.
        args: Tool call arguments.

    Returns:
        Tool response dict (``{"result": ...}`` or ``{"error": ...}``).
    """
    return asyncio.run(tool.run_async(args=args, tool_context=None))


def _format_compact_json(value: Any) -> str:
    """Encode a value as compact JSON, as `read` does.

    Args:
        value: JSON value.

    Returns:
        The JSON text.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _list_matches(
    trajectory: dict[str, Any], path: str
) -> list[tuple[list[str | int], Any]] | grep_read.ToolOutput:
    """Resolve a path, which may hold ``[*]`` steps, to the values it matches.

    Args:
        trajectory: Trajectory JSON.
        path: Path in a form that `grep_read.parse_path` accepts.

    Returns:
        ``(canonical_components, value)`` pairs, or the error `ToolOutput` of
        a path that does not parse or matches no value.
    """
    components = grep_read.parse_path(path)
    if isinstance(components, grep_read.ToolOutput):
        return components
    return grep_read._match_components(trajectory, components, path)


def _parse_grouped_hits(output: str) -> list[tuple[str, int, str]]:
    """List the hit lines of grouped `grep` output under their headers.

    Args:
        output: `grep` output without an omission note.

    Returns:
        ``(path, line_number, text)`` for each hit line.
    """
    hits = []
    path = ""
    for line in output.splitlines():
        if not line.startswith("  "):
            assert line.endswith(":"), line
            path = line[:-1]
        elif hit := _HIT_LINE.fullmatch(line):
            hits.append((path, int(hit["line"]), hit["text"]))
    return hits


# --------------------------------------------------------------------------- #
# Declarations                                                                #
# --------------------------------------------------------------------------- #


def test_the_declarations_carry_the_design_texts() -> None:
    grep = grep_read.build_grep_declaration()
    read = grep_read.build_read_declaration()

    assert grep.name == "grep"
    assert read.name == "read"
    grep_text = grep.description or ""
    read_text = read.description or ""
    assert grep_text.startswith("Search this session with a")
    assert (
        "A text that has a parsed `text_json` form is searched in that form "
        "only."
    ) in " ".join(grep_text.split())
    assert "[*]" in grep_text
    assert (
        "  t0.e8.content.parts[0].text:\n"
        "    4- The order has shipped.\n"
        "    5: Refund refused.\n"
    ) in grep_text
    assert "30,000 characters" in grep_text
    assert read_text.startswith("Read values of this session")
    assert grep_read.LINE_NUMBER_INTERVAL == 10
    assert "every 10th line (10, 20, ...) show their number" in read_text
    assert "count from the nearest number" in read_text
    for phrase in (
        "compact JSON",
        "2,000",
        "[*]",
        "`fields`",
        'read(".turns[0].events[*].content.parts[0].function_response", '
        'fields=["name", "response"])',
        'read(".turns[0].events[*]", fields=["author", '
        '".content.parts[0].function_call.name"])',
    ):
        assert phrase in read_text
    assert grep.parameters == types.Schema(
        type=types.Type.OBJECT,
        properties={
            "pattern": types.Schema(
                type=types.Type.STRING,
                description="A regular expression (Python syntax).",
            ),
            "path": types.Schema(
                type=types.Type.STRING, description='Default ".".'
            ),
            "context": types.Schema(
                type=types.Type.INTEGER, description="Default 0."
            ),
            "ignore_case": types.Schema(
                type=types.Type.BOOLEAN, description="Default true."
            ),
            "max_hits": types.Schema(
                type=types.Type.INTEGER, description="Default 100."
            ),
        },
        required=["pattern"],
    )
    assert read.parameters == types.Schema(
        type=types.Type.OBJECT,
        properties={
            "path": types.Schema(type=types.Type.STRING),
            "offset": types.Schema(
                type=types.Type.INTEGER, description="Default 1."
            ),
            "limit": types.Schema(
                type=types.Type.INTEGER, description="Default 400."
            ),
            "fields": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.STRING),
                description="Keys or relative paths to keep from each object.",
            ),
        },
        required=["path"],
    )


def test_the_tools_declare_what_they_build() -> None:
    grep, read = _build_tools()

    assert grep.name == "grep"
    assert read.name == "read"
    assert grep._get_declaration() == grep_read.build_grep_declaration()
    assert read._get_declaration() == grep_read.build_read_declaration()


# --------------------------------------------------------------------------- #
# Outline positions                                                           #
# --------------------------------------------------------------------------- #


def test_each_outline_line_opens_its_heading_in_read() -> None:
    grep, read = _build_tools()

    outline = grep._trajectory_json["agents"][0]["instruction_outline"]
    assert [line for line, _ in outline] == [1, 303, 605]
    for line, heading in outline:
        assert read.read(".agents[0].instruction", line, 1).text.startswith(
            f"{line}: {heading}"
        )


# --------------------------------------------------------------------------- #
# read: values and paths                                                      #
# --------------------------------------------------------------------------- #


def test_read_numbers_the_lines_of_a_string_sparsely() -> None:
    _, read = _build_tools()

    assert read.read(".agents[0].instruction", 2, 2).text == (
        "2: You are a support worker.\n"
        "Filler line about policy.\n"
        "[returned lines 2-3 of 606; continue with offset 4]"
    )


def test_read_numbers_the_first_line_and_every_tenth_line() -> None:
    _, read = _build_tools_from_trajectory({"rules": RULES})

    output = read.read(".rules").text

    assert output.splitlines() == [
        "1: rule 1",
        *[f"rule {number}" for number in range(2, 10)],
        "10: rule 10",
        *[f"rule {number}" for number in range(11, 20)],
        "20: rule 20",
        *[f"rule {number}" for number in range(21, 26)],
    ]


def test_read_numbers_a_page_from_an_offset_off_the_interval() -> None:
    _, read = _build_tools_from_trajectory({"rules": RULES})

    output = read.read(".rules", 7, 15).text

    assert output.splitlines() == [
        "7: rule 7",
        "rule 8",
        "rule 9",
        "10: rule 10",
        *[f"rule {number}" for number in range(11, 20)],
        "20: rule 20",
        "rule 21",
        "[returned lines 7-21 of 25; continue with offset 22]",
    ]


def test_read_numbers_a_page_that_starts_on_the_interval_once() -> None:
    _, read = _build_tools_from_trajectory({"rules": RULES})

    assert read.read(".rules", 10, 2).text == (
        "10: rule 10\nrule 11\n"
        "[returned lines 10-11 of 25; continue with offset 12]"
    )


def test_read_takes_a_path_or_an_event_id_followed_by_a_path() -> None:
    _, read = _build_tools()

    by_path = read.read(
        ".turns[0].events[4].content.parts[0].function_response.response"
        ".shipments[0].status"
    )
    by_event = read.read(
        "t0.e4.content.parts[0].function_response.response.shipments[0].status"
    )
    assert by_path.text == "1: SHIPPED"
    assert by_event.text == "1: SHIPPED"


def test_read_stops_at_30000_chars_with_the_next_offset() -> None:
    _, read = _build_tools_from_trajectory({"rules": PAGED_INSTRUCTION})

    *lines, note = read.read(".rules", 3, 3_000).text.split("\n")
    last = 3 + len(lines) - 1
    following = read.read(".rules", last + 1, 1).text

    assert grep_read.TOOL_OUTPUT_CHARS == 30_000
    assert len("\n".join(lines)) <= grep_read.TOOL_OUTPUT_CHARS
    assert len("\n".join(lines)) > grep_read.TOOL_OUTPUT_CHARS - 40
    for number, line in enumerate(lines, start=3):
        text = f"line {number - 1:05d} of the rules"
        numbered = number == 3 or number % 10 == 0
        assert line == (f"{number}: {text}" if numbered else text)
    assert note == (
        f"[returned lines 3-{last} of 3000; continue with offset {last + 1}]"
    )
    assert following.startswith(f"{last + 1}: line {last:05d} of the rules")


def test_a_long_line_of_a_string_is_split_into_lines() -> None:
    case = _build_case(
        [
            _build_event(
                WORKER,
                {
                    "function_response": {
                        "name": "lookup",
                        "response": {"blob": "z" * 4_500},
                    }
                },
            )
        ]
    )
    _, read = _build_tools(case, [_build_revision()])
    path = ".turns[0].events[0].content.parts[0].function_response.response"

    assert read.read(path + ".blob").text.splitlines() == [
        "1: " + "z" * 2_000,
        "z" * 2_000,
        "z" * 500,
    ]


def test_read_shows_a_value_as_compact_json() -> None:
    _, read = _build_tools()

    output = read.read("t0.e4.content.parts[0].function_response.response")

    assert output.text == "1: " + _format_compact_json(SHIPMENTS)


def test_read_cuts_compact_json_into_numbered_lines() -> None:
    value = {"blob": "z" * 4_500}
    _, read = _build_tools_from_trajectory({"v": value})
    text = _format_compact_json(value)

    whole = read.read(".v").text.splitlines()
    page = read.read(".v", 2, 1).text

    assert whole == [
        "1: " + text[:2_000],
        text[2_000:4_000],
        text[4_000:],
    ]
    assert page == (
        "2: " + text[2_000:4_000] + "\n"
        "[returned lines 2-2 of 3; continue with offset 3]"
    )


def test_read_cuts_compact_json_into_sparsely_numbered_lines() -> None:
    value = {"blob": "z" * 22_000}
    _, read = _build_tools_from_trajectory({"v": value})
    text = _format_compact_json(value)
    chunks = [
        text[start : start + 2_000] for start in range(0, len(text), 2_000)
    ]

    output = read.read(".v").text.splitlines()

    assert len(chunks) == 12
    assert output == [
        f"1: {chunks[0]}",
        *chunks[1:9],
        f"10: {chunks[9]}",
        *chunks[10:],
    ]


def test_read_shows_a_short_value_as_one_numbered_line() -> None:
    _, read = _build_tools()

    assert read.read("t0.e3.content.parts[0].function_call.name").text == (
        "1: lookup"
    )
    assert read.read("t1.e1.content").text == (
        '1: {"parts":[{"text":"Done again."}],"role":"model"}'
    )


def test_compact_json_escapes_the_line_breaks_it_would_split_at() -> None:
    _, read = _build_tools_from_trajectory({"v": {"a": "x\u2028y\x85z"}})

    assert read.read(".v").text == '1: {"a":"x\\u2028y\\u0085z"}'


def test_read_leaves_out_the_raw_text_beside_text_json() -> None:
    _, read = _build_tools()
    part = "t0.e6.content.parts[0]"

    event = read.read("t0.e6").text
    whole_part = read.read(part).text
    raw = read.read(part + ".text").text

    assert '"text_json":{"answer":"Refunded.","source":"TEMPLATE"}' in event
    assert '"text":' not in event
    assert whole_part == (
        '1: {"text_wrapper":"text: \'…\' (added by the telemetry recorder)",'
        '"text_json":{"answer":"Refunded.","source":"TEMPLATE"}}'
    )
    assert raw.startswith('1: text: \'{"answer": "Refunded."')


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (".agents | length", "is not a path that this tool accepts"),
        (".turns[]", "is not a path that this tool accepts"),
        (".agents[7]", "selects no value"),
        (".agents[0].missing", "selects no value"),
        (".agents[0].instruction.key", "selects no value"),
        (".agents.key", "selects no value"),
        (".agents[0][0]", "selects no value"),
        ("t9.e0", "selects no value"),
        ("t0.e0.missing", "selects no value"),
        (".agents[0].instruction[0:10]", "slices a value"),
        (".agents[", "is not a path that this tool accepts"),
    ],
)
def test_a_bad_path_is_an_error_the_model_can_fix(
    path: str, message: str
) -> None:
    grep, read = _build_tools()

    for output in (read.read(path), grep.grep("x", path)):
        assert output.is_error
        assert message in output.text


def test_an_unparsable_path_names_the_accepted_forms() -> None:
    _, read = _build_tools()

    output = read.read(".agents[")

    assert output.text == (
        "'.agents[' is not a path that this tool accepts\n"
        "`path` must be a chain of keys, indexes and [*] steps in jq syntax, "
        'such as .agents[0].instruction, .turns[-1], .a["ship-to"] or '
        ".turns[0].events[*].author; an event id, such as t0.e8, optionally "
        "followed by such a chain, such as t0.e8.content.parts[*]; or "
        '"." for the whole session. Filters such as |, .[] and select() are '
        "not accepted"
    )


def test_read_past_the_end_is_an_error() -> None:
    _, read = _build_tools_from_trajectory({"rules": RULES})

    output = read.read(".rules", 26)

    assert output.is_error
    assert output.text == "offset 26 is past the end: the value has 25 lines"


def test_read_counts_a_negative_index_from_the_end() -> None:
    _, read = _build_tools()

    assert read.read(".turns[-1].events[-1].author").text == "1: worker"


# --------------------------------------------------------------------------- #
# read: wildcard paths and fields                                             #
# --------------------------------------------------------------------------- #


def test_wildcard_read_returns_one_line_per_match() -> None:
    _, read = _build_tools()

    output = read.read(".turns[*].events[0].content.parts[0].text_json")

    assert output.text.splitlines() == [
        "t0.e0.content.parts[0].text_json: " + _format_compact_json(REQUEST),
        "t1.e0.content.parts[0].text_json: " + _format_compact_json(REQUEST),
    ]


def test_wildcard_read_prints_event_paths_as_event_ids() -> None:
    _, read = _build_tools()

    output = read.read(
        ".turns[0].events[*]",
        fields=["author", ".content.parts[0].function_call.name"],
    )

    assert output.text.splitlines() == [
        't0.e0: {"author":"user"}',
        't0.e1: {"author":"worker"}',
        't0.e2: {"author":"worker"}',
        't0.e3: {"author":"worker",'
        '".content.parts[0].function_call.name":"lookup"}',
        't0.e4: {"author":"worker"}',
        't0.e5: {"author":"worker",'
        '".content.parts[0].function_call.name":"refund"}',
        't0.e6: {"author":"worker"}',
    ]


def test_wildcard_read_pages_by_match() -> None:
    _, read = _build_tools()
    path = ".turns[0].events[*].author"

    page = read.read(path, 2, 2)
    past_end = read.read(path, 8)

    assert page.text.splitlines() == [
        't0.e1.author: "worker"',
        't0.e2.author: "worker"',
        "[returned matches 2-3 of 7; continue with offset 4]",
    ]
    assert past_end.is_error
    assert past_end.text == ("offset 8 is past the end: the path has 7 matches")


def test_wildcard_read_keeps_other_paths_and_pages_by_match() -> None:
    _, read = _build_tools_from_trajectory(
        {"items": [1, 2, 3], "turns": [{"x": 1}]}
    )

    page = read.read(".items[*]", 2, 1).text
    turns = read.read(".turns[*]").text

    assert page == (
        ".items[1]: 2\n[returned matches 2-2 of 3; continue with offset 3]"
    )
    assert turns == '.turns[0]: {"x":1}'


def test_wildcard_read_stops_at_30000_chars_with_the_next_offset() -> None:
    _, read = _build_tools_from_trajectory({"items": ["y" * 1_500] * 40})

    *lines, note = read.read(".items[*]").text.split("\n")
    shown = len(lines)
    following = read.read(".items[*]", shown + 1, 1).text

    assert len("\n".join(lines)) <= grep_read.TOOL_OUTPUT_CHARS
    assert shown < 40
    assert lines[0] == '.items[0]: "' + "y" * 1_500 + '"'
    assert note == (
        f"[returned matches 1-{shown} of 40; continue with offset {shown + 1}]"
    )
    assert following.startswith(f".items[{shown}]: ")


def test_a_long_match_line_is_cut_with_a_notice() -> None:
    _, read = _build_tools_from_trajectory({"items": ["z" * 2_500]})

    line = read.read(".items[*]").text

    assert line.startswith('.items[0]: "zzz')
    assert line.endswith(
        "… [line cut at 2000 of 2513 chars; read this value's own path to "
        "see all of it]"
    )


def test_fields_work_without_a_wildcard_and_skip_parsed_text() -> None:
    _, read = _build_tools()

    response = read.read(
        "t0.e4.content.parts[0].function_response",
        fields=["name", ".response.shipments[0].status", "missing"],
    )
    part = read.read("t0.e6", fields=[".content.parts[0]"])

    assert response.text == (
        '1: {"name":"lookup",".response.shipments[0].status":"SHIPPED"}'
    )
    assert part.text == (
        '1: {".content.parts[0]":{"text_wrapper":"text: \'…\' (added by the '
        'telemetry recorder)","text_json":{"answer":"Refunded.",'
        '"source":"TEMPLATE"}}}'
    )


def test_fields_leave_values_that_are_not_objects_unchanged() -> None:
    _, read = _build_tools()

    output = read.read("t0.e0.author", fields=["x"])
    matches = read.read(".turns[1].events[*].author", fields=["x"])

    assert output.text == "1: user"
    assert matches.text.splitlines() == [
        't1.e0.author: "user"',
        't1.e1.author: "worker"',
    ]


def test_a_malformed_field_is_an_error() -> None:
    _, read = _build_tools()

    output = read.read("t0.e0", fields=[".content[*]"])

    assert output.is_error
    assert output.text.startswith("field '.content[*]' is neither a key nor")


# --------------------------------------------------------------------------- #
# grep                                                                        #
# --------------------------------------------------------------------------- #


def test_grep_reports_the_path_and_line_of_each_hit() -> None:
    grep, _ = _build_tools()
    instruction = ".agents[0].instruction"
    system_instruction = "t0.e6.state_delta.system_instruction"

    assert grep.grep("refund|escalat").text.splitlines() == [
        f"{instruction}:",
        "  303: ## Refunds",
        "  304: Never refund an order that has shipped.",
        "  605: ### Escalation",
        "  606: Escalate when the customer asks twice.",
        ".agents[0].instruction_outline[1][1]:",
        "  1: ## Refunds",
        ".agents[0].instruction_outline[2][1]:",
        "  1: ### Escalation",
        ".agents[0].tools[1]:",
        "  1: refund",
        "t0.e0.content.parts[0].text_json.emailBody:",
        '  1: "emailBody": Refund order 7',
        "t0.e5.content.parts[0].function_call.name:",
        '  1: "name": refund',
        "t0.e6.content.parts[0].text_json.answer:",
        '  1: "answer": Refunded.',
        f"{system_instruction}:",
        "  303: ## Refunds",
        "  304: Never refund an order that has shipped.",
        "  605: ### Escalation",
        "  606: Escalate when the customer asks twice.",
        "t1.e0.content.parts[0].text_json.emailBody:",
        '  1: "emailBody": Refund order 7',
    ]


def test_grep_groups_hits_and_context_under_one_header_per_value() -> None:
    trajectory = {
        "agents": [{"instruction": "intro\nrefund rule\nmiddle\n" + RULES}],
        "turns": [
            {
                "events": [
                    {"author": "user", "text": "please\nrefund now\nthanks"},
                ]
            }
        ],
    }
    grep, _ = _build_tools_from_trajectory(trajectory)

    output = grep.grep("refund|rule 2$|rule 6$", context=1).text

    assert output.splitlines() == [
        ".agents[0].instruction:",
        '  1- "instruction": intro',
        "  2: refund rule",
        "  3- middle",
        "  4- rule 1",
        "  5: rule 2",
        "  6- rule 3",
        "  8- rule 5",
        "  9: rule 6",
        "  10- rule 7",
        "t0.e0.text:",
        '  1- "text": please',
        "  2: refund now",
        "  3- thanks",
    ]
    assert "--" not in output


def test_grep_skips_the_raw_text_of_a_parsed_part_unless_asked() -> None:
    grep, read = _build_tools()
    raw = "t0.e6.content.parts[0].text"

    whole = grep.grep("TEMPLATE")
    direct = grep.grep("TEMPLATE", raw)

    assert whole.text == (
        't0.e6.content.parts[0].text_json.source:\n  1: "source": TEMPLATE'
    )
    assert direct.text.startswith(f"{raw}:\n  1: text: '")
    assert read.read(raw).text.startswith("1: text: '")


def test_grep_searches_non_string_values_as_key_value_lines() -> None:
    grep, _ = _build_tools()

    assert grep.grep(r'"weight_kg": 1\d', "t0.e4").text == (
        "t0.e4.content.parts[0].function_response.response"
        '.shipments[0].weight_kg:\n  1: "weight_kg": 12'
    )


def test_grep_finds_a_string_value_by_its_key_and_quotes_odd_keys() -> None:
    grep, _ = _build_tools()

    assert grep.grep("status|city", "t0.e4").text.splitlines() == [
        "t0.e4.content.parts[0].function_response.response.status:",
        '  1: "status": ok',
        "t0.e4.content.parts[0].function_response.response.shipments[0]"
        ".status:",
        '  1: "status": SHIPPED',
        't0.e4.content.parts[0].function_response.response["ship-to"].city:',
        '  1: "city": Springfield',
    ]


def test_grep_finds_a_key_that_holds_an_object_or_array() -> None:
    grep, read = _build_tools()
    base = "t0.e4.content.parts[0].function_response.response"

    output = grep.grep("shipments|ship-to", "t0.e4")

    assert output.text.splitlines() == [
        f"{base}.shipments:",
        '  1: "shipments": [',
        f'{base}["ship-to"]:',
        '  1: "ship-to": {',
    ]
    assert read.read(f'{base}["ship-to"]', 1, 1).text.startswith("1: {")


def test_grep_shows_context_lines_in_the_same_string() -> None:
    grep, _ = _build_tools()
    path = ".agents[0].instruction"

    output = grep.grep("^## Refunds$|^### Escalation$", path, context=1)

    assert output.text.splitlines() == [
        f"{path}:",
        "  302- Filler line about policy.",
        "  303: ## Refunds",
        "  304- Never refund an order that has shipped.",
        "  604- Filler line about policy.",
        "  605: ### Escalation",
        "  606- Escalate when the customer asks twice.",
    ]


def test_grep_merges_overlapping_context() -> None:
    grep, _ = _build_tools()
    path = ".agents[0].instruction"

    assert grep.grep("refund", path, context=1).text.splitlines() == [
        f"{path}:",
        "  302- Filler line about policy.",
        "  303: ## Refunds",
        "  304: Never refund an order that has shipped.",
        "  305- Filler line about policy.",
    ]


def test_grep_can_match_case() -> None:
    grep, _ = _build_tools()

    assert grep.grep("REFUNDS", ".agents[0].instruction").text == (
        ".agents[0].instruction:\n  303: ## Refunds"
    )
    assert (
        grep.grep("REFUNDS", ".agents[0].instruction", ignore_case=False).text
        == "(no matches)"
    )


def test_grep_searches_under_every_wildcard_match() -> None:
    grep, _ = _build_tools()
    base = ".content.parts[0].function_response.response"

    output = grep.grep(
        "SHIPPED|ok", f".turns[0].events[*]{base}", ignore_case=False
    )
    no_match = grep.grep("x", ".turns[0].events[*].missing")

    assert output.text.splitlines() == [
        f"t0.e4{base}.status:",
        '  1: "status": ok',
        f"t0.e4{base}.shipments[0].status:",
        '  1: "status": SHIPPED',
    ]
    assert no_match.is_error
    assert no_match.text == (
        "'.turns[0].events[*].missing' selects no value in this session"
    )


def test_grep_searches_each_wildcard_match_as_its_own_root() -> None:
    grep, _ = _build_tools()

    assert grep.grep(
        "worker", ".turns[*].events[*].author"
    ).text.splitlines() == [
        line
        for path in (
            *[f"t0.e{index}.author" for index in range(1, 7)],
            "t1.e1.author",
        )
        for line in (f"{path}:", "  1: worker")
    ]


def test_grep_stops_at_max_hits_and_counts_what_it_left_out() -> None:
    grep, _ = _build_tools()

    *lines, note = grep.grep("filler", max_hits=5).text.splitlines()

    assert lines == [
        ".agents[0].instruction:",
        *[f"  {number}: Filler line about policy." for number in range(3, 8)],
    ]
    assert note == (
        "[stopped at max_hits=5: 1195 more hits left out; narrow the pattern "
        "or the path, or raise max_hits]"
    )


def test_grep_ends_at_the_last_kept_hit_and_its_after_context() -> None:
    grep, _ = _build_tools()
    path = ".agents[0].instruction"
    pattern = "^## Refunds$|^### Escalation$"
    note = (
        "[stopped at max_hits={}: {} more hits left out; narrow the pattern "
        "or the path, or raise max_hits]"
    )

    in_one_value = grep.grep(pattern, path, context=1, max_hits=1)
    across_values = grep.grep(pattern, context=1, max_hits=2)
    later_hit_in_context = grep.grep("refund", path, context=1, max_hits=1)

    assert in_one_value.text.splitlines() == [
        f"{path}:",
        "  302- Filler line about policy.",
        "  303: ## Refunds",
        "  304- Never refund an order that has shipped.",
        note.format(1, 1),
    ]
    assert across_values.text.splitlines()[-4:] == [
        "  604- Filler line about policy.",
        "  605: ### Escalation",
        "  606- Escalate when the customer asks twice.",
        note.format(2, 4),
    ]
    assert later_hit_in_context.text.splitlines() == [
        f"{path}:",
        "  302- Filler line about policy.",
        "  303: ## Refunds",
        "  304- Never refund an order that has shipped.",
        note.format(1, 1),
    ]


def test_grep_stops_at_30000_chars_and_counts_what_it_left_out() -> None:
    grep, _ = _build_tools()

    *lines, note = grep.grep("filler", max_hits=10_000).text.splitlines()

    hits = [line for line in lines if _HIT_LINE.fullmatch(line)]
    assert len("\n".join(lines)) <= grep_read.TOOL_OUTPUT_CHARS
    assert lines[-1] in hits
    assert note == (
        f"[output stopped at 30000 chars: {1_200 - len(hits)} more hits left "
        "out; narrow the pattern or the path]"
    )


def test_grep_never_ends_with_a_header_alone_at_the_char_cap() -> None:
    def build_first_value(last_chars: int) -> str:
        lines = ["hit " + "a" * 96] * 269 + ["hit " + "a" * (last_chars - 4)]
        return "\n".join(lines)

    # Size the first value so its output leaves 10 characters: room for the
    # second value's header but not for the header and its hit line.
    probe, _ = _build_tools_from_trajectory({"a": build_first_value(500)})
    used = len(probe.grep("hit", max_hits=10_000).text) + 1
    first = build_first_value(500 + grep_read.TOOL_OUTPUT_CHARS - 10 - used)
    grep, _ = _build_tools_from_trajectory(
        {"a": first, "b": "hit " + "b" * 100}
    )

    *lines, note = grep.grep("hit", max_hits=10_000).text.splitlines()

    assert len("\n".join(lines)) + 1 == grep_read.TOOL_OUTPUT_CHARS - 10
    assert ".b:" not in lines
    assert lines[-1].startswith("  270: hit aaa")
    assert note == (
        "[output stopped at 30000 chars: 1 more hits left out; narrow the "
        "pattern or the path]"
    )


def test_grep_reports_no_matches_and_invalid_patterns() -> None:
    grep, _ = _build_tools()

    invalid = grep.grep("(unclosed")

    assert grep.grep("no such text anywhere").text == "(no matches)"
    assert invalid.is_error
    assert invalid.text.startswith("invalid regular expression '(unclosed'")


# --------------------------------------------------------------------------- #
# Path syntax                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("components", "path"),
    [
        ([], "."),
        (["agents", 0, "instruction"], ".agents[0].instruction"),
        (["a", "ship-to", "if"], '.a["ship-to"].if'),
        (["a.b", "c d", "", 'q"'], '.["a.b"]["c d"][""]["q\\""]'),
        ([0, "x"], ".[0].x"),
    ],
)
def test_hit_paths_are_jq_paths_that_parse_back(
    components: list[Any], path: str
) -> None:
    assert grep_read.render_jq_path(components) == path
    assert grep_read.parse_path(path) == components


@pytest.mark.parametrize(
    ("components", "path"),
    [
        (["turns", 0, "events", 8], "t0.e8"),
        (
            ["turns", 0, "events", 8, "content", "parts", 0],
            "t0.e8.content.parts[0]",
        ),
        (
            ["turns", 3, "events", 12, "key with space"],
            't3.e12["key with space"]',
        ),
        (["turns", 0, "events", 8, 2], "t0.e8[2]"),
        (["turns", 123_456, "events", 7_890, "a"], "t123456.e7890.a"),
        (["turns", -1, "events", -2], ".turns[-1].events[-2]"),
        (["turns", 0, "events", -1, "a"], ".turns[0].events[-1].a"),
        (["turns", 0], ".turns[0]"),
        (["turns", 0, "events"], ".turns[0].events"),
        (["turns", "x", "events", 1], ".turns.x.events[1]"),
        (["agents", 0, "instruction"], ".agents[0].instruction"),
        ([], "."),
    ],
)
def test_short_paths_parse_back_to_their_components(
    components: list[Any], path: str
) -> None:
    assert grep_read.render_short_path(components) == path
    assert grep_read.parse_path(path) == components


def test_printed_paths_resolve_to_the_value_that_read_shows() -> None:
    grep, read = _build_tools()
    trajectory = grep._trajectory_json

    hits = _parse_grouped_hits(
        grep.grep("refund|shipped|status|12|C-1", max_hits=1_000).text
    )
    for path, number, text in hits:
        resolved = _list_matches(trajectory, path)
        assert not isinstance(resolved, grep_read.ToolOutput)
        assert grep_read.render_short_path(resolved[0][0]) == path
        shown = read.read(path, number, 1).text.split("\n", 1)[0]
        assert shown.startswith(f"{number}: ")
        assert text.endswith(shown.split(": ", 1)[1])
    matches = read.read(".turns[*].events[*].author").text.splitlines()
    for line in matches:
        path, value = line.split(": ", 1)
        resolved = _list_matches(trajectory, path)
        assert not isinstance(resolved, grep_read.ToolOutput)
        assert _format_compact_json(resolved[0][1]) == value
    assert len(hits) > 10
    assert len(matches) == 9


@pytest.mark.parametrize(
    ("path", "components"),
    [
        (".", []),
        ("", []),
        ("  .agents  ", ["agents"]),
        (".agents", ["agents"]),
        ('."ship-to"', ["ship-to"]),
        (".[0]", [0]),
        (".[-1]", [-1]),
        ('.["a.b c"]', ["a.b c"]),
        ('.a["b"][2].c', ["a", "b", 2, "c"]),
        (".a.[2]", ["a", 2]),
        ('.a."b.c".["d e"][-3]', ["a", "b.c", "d e", -3]),
        ('.["q\\"\\u00e9"]', ['q"é']),
        ("t0.e8", ["turns", 0, "events", 8]),
        ("t12.e345", ["turns", 12, "events", 345]),
        (
            "t0.e8.content.parts[0]",
            ["turns", 0, "events", 8, "content", "parts", 0],
        ),
        ('t0.e8["content"]', ["turns", 0, "events", 8, "content"]),
        (".[*]", [grep_read.WILDCARD]),
        (".a[*]", ["a", grep_read.WILDCARD]),
        (".a.[*].b", ["a", grep_read.WILDCARD, "b"]),
        (
            ".turns[*].events[*]",
            ["turns", grep_read.WILDCARD, "events", grep_read.WILDCARD],
        ),
        (
            "t0.e3.content.parts[*]",
            ["turns", 0, "events", 3, "content", "parts", grep_read.WILDCARD],
        ),
        ('.a[*]["b c"][-1]', ["a", grep_read.WILDCARD, "b c", -1]),
        ("t0", ["turns", 0]),
        ("  t12  ", ["turns", 12]),
        ("*", [grep_read.WILDCARD]),
        (".*", [grep_read.WILDCARD]),
        (".turns[1].*", ["turns", 1, grep_read.WILDCARD]),
        (
            ".turns[0].events[*].*",
            ["turns", 0, "events", grep_read.WILDCARD, grep_read.WILDCARD],
        ),
        (".a.*.b", ["a", grep_read.WILDCARD, "b"]),
        ("t0.e8.*", ["turns", 0, "events", 8, grep_read.WILDCARD]),
        ("[*]", [grep_read.WILDCARD]),
        ("[*].author", [grep_read.WILDCARD, "author"]),
        ("[*][0]", [grep_read.WILDCARD, 0]),
    ],
)
def test_parse_path_accepts_keys_indexes_wildcards_and_event_ids(
    path: str, components: list[Any]
) -> None:
    assert grep_read.parse_path(path) == components


@pytest.mark.parametrize(
    "path",
    [
        ".a | .b",
        ".a[]",
        ".[]",
        "..",
        ".a.",
        ".a..b",
        "agents",
        "[0]",
        "*.a",
        "**",
        ".**",
        ".*x",
        "t0.*",
        "t0[1]",
        "t0.a",
        "t-1",
        ".a b",
        '.a["b"',
        ".a[1",
        ".a[*",
        ".a[**]",
        ".a*",
        ".a[x]",
        ".a[1.5]",
        ".a[ 1 ]",
        ".a[true]",
        ".a.1",
        'select(.event_id == "t0.e3")',
        ".agents | length",
        ".a?",
        "t0.e",
        "t0.e8x",
        "t0.e8 .a",
    ],
)
def test_parse_path_rejects_filters_and_malformed_paths(path: str) -> None:
    output = grep_read.parse_path(path)

    assert isinstance(output, grep_read.ToolOutput)
    assert output.is_error
    assert output.text.startswith(
        f"{path!r} is not a path that this tool accepts\n"
        "`path` must be a chain of keys, indexes and [*] steps"
    )


@pytest.mark.parametrize(
    ("lenient", "canonical"),
    [
        ("t1", ".turns[1]"),
        (".*", ".[*]"),
        ("*", ".[*]"),
        (".turns[1].*", ".turns[1][*]"),
        (".turns[0].events[*].*", ".turns[0].events[*][*]"),
        ("[*]", ".[*]"),
        ("[*][0].events[1].author", ".[*][0].events[1].author"),
    ],
)
def test_read_and_grep_take_lenient_path_shapes_as_their_canonical_paths(
    lenient: str, canonical: str
) -> None:
    grep, read = _build_tools()

    read_output = read.read(lenient)
    grep_output = grep.grep("worker|refund", lenient)

    assert not read_output.is_error
    assert not grep_output.is_error
    assert read_output.text == read.read(canonical).text
    assert grep_output.text == grep.grep("worker|refund", canonical).text
    assert grep_output.text != "(no matches)"


def test_lenient_path_shapes_print_canonical_paths() -> None:
    grep, read = _build_tools()

    turn = read.read(".turns[*]", limit=1).text.splitlines()[0]
    events = read.read(".turns[1].*").text.splitlines()
    hits = _parse_grouped_hits(grep.grep("worker", ".turns[1].*").text)

    assert turn.startswith(".turns[0]: ")
    assert [line.split(": ", 1)[0] for line in events] == [
        ".turns[1].turn_index",
        ".turns[1].turn_id",
        ".turns[1].events",
    ]
    assert {path for path, _, _ in hits} == {"t1.e1.author"}
    assert read.read("t1").text == read.read(".turns[1]").text


@pytest.mark.parametrize("path", [".a[0:10]", ".a[:5]", ".a[3:]", ".a[-2:]"])
def test_parse_path_explains_that_slices_are_not_paths(path: str) -> None:
    output = grep_read.parse_path(path)

    assert isinstance(output, grep_read.ToolOutput)
    assert output.text.startswith(f"{path!r} slices a value")


def test_a_negative_index_resolves_to_its_position() -> None:
    trajectory = {"a": [10, 20, 30]}

    assert _list_matches(trajectory, ".a[-1]") == [(["a", 2], 30)]
    assert _list_matches(trajectory, ".a[-3]") == [(["a", 0], 10)]
    output = _list_matches(trajectory, ".a[-4]")
    assert isinstance(output, grep_read.ToolOutput)
    assert output.text == "'.a[-4]' selects no value in this session"


def test_quoted_keys_with_dots_and_spaces_resolve() -> None:
    trajectory = {"a.b": {"c d": "found"}}

    assert _list_matches(trajectory, '.["a.b"]["c d"]') == [
        (["a.b", "c d"], "found")
    ]
    assert _list_matches(trajectory, '."a.b"."c d"') == [
        (["a.b", "c d"], "found")
    ]


def test_wildcards_match_array_elements_and_object_values_in_order() -> None:
    trajectory = {
        "a": [{"b": 1}, {"c": 2}, {"b": 3}, "leaf"],
        "o": {"x": [10, 20], "y": [30]},
    }

    assert _list_matches(trajectory, ".a[*].b") == [
        (["a", 0, "b"], 1),
        (["a", 2, "b"], 3),
    ]
    assert _list_matches(trajectory, ".o[*][-1]") == [
        (["o", "x", 1], 20),
        (["o", "y", 0], 30),
    ]
    assert _list_matches(trajectory, ".o[*][*]") == [
        (["o", "x", 0], 10),
        (["o", "x", 1], 20),
        (["o", "y", 0], 30),
    ]
    assert _list_matches(trajectory, ".a[0]") == [(["a", 0], {"b": 1})]


@pytest.mark.parametrize("path", [".a[*].z", ".missing[*]", ".a[3][*]"])
def test_a_wildcard_path_without_matches_is_an_error(path: str) -> None:
    output = _list_matches({"a": [{"b": 1}, [], {}, "s"]}, path)

    assert isinstance(output, grep_read.ToolOutput)
    assert output.text == f"{path!r} selects no value in this session"


# --------------------------------------------------------------------------- #
# Argument coercion and responses                                             #
# --------------------------------------------------------------------------- #


def test_an_argument_of_the_wrong_type_is_an_error_result() -> None:
    grep, read = _build_tools()

    assert _get_tool_response(read, {"path": "t0.e0", "offset": "abc"}) == {
        "error": "invalid arguments for read: could not convert string to "
        "float: 'abc'"
    }
    assert _get_tool_response(grep, {"pattern": "x", "max_hits": 2.5}) == {
        "error": "invalid arguments for grep: expected an integer, got 2.5"
    }
    assert _get_tool_response(grep, {"pattern": "x", "context": True}) == {
        "error": "invalid arguments for grep: expected an integer, got True"
    }


def test_whole_floats_and_text_booleans_are_accepted() -> None:
    grep, read = _build_tools()

    assert _get_tool_response(
        read, {"path": ".agents[0].instruction", "offset": 2.0, "limit": 1}
    ) == {
        "result": "2: You are a support worker.\n"
        "[returned lines 2-2 of 606; continue with offset 3]"
    }
    assert _get_tool_response(
        grep,
        {
            "pattern": "REFUNDS",
            "path": ".agents[0].instruction",
            "ignore_case": "false",
        },
    ) == {"result": "(no matches)"}
    assert _get_tool_response(
        grep,
        {
            "pattern": "REFUNDS",
            "path": ".agents[0].instruction",
            "ignore_case": "",
        },
    ) == {"result": ".agents[0].instruction:\n  303: ## Refunds"}


def test_absent_arguments_take_their_defaults() -> None:
    grep, read = _build_tools()

    assert _get_tool_response(read, {}) == {
        "result": read.read(".", 1, grep_read.READ_LIMIT).text
    }
    assert _get_tool_response(grep, {"pattern": "Springfield"}) == {
        "result": grep.grep("Springfield").text
    }


def test_the_fields_argument_takes_an_array_or_one_string() -> None:
    _, read = _build_tools()

    as_string = _get_tool_response(read, {"path": "t0.e0", "fields": "author"})
    as_array = _get_tool_response(read, {"path": "t0.e0", "fields": ["author"]})
    wrong = _get_tool_response(read, {"path": "t0.e0", "fields": [1]})

    assert as_string == as_array == {"result": '1: {"author":"user"}'}
    assert wrong == {
        "error": "invalid arguments for read: expected an array of strings, "
        "got [1]"
    }


def test_run_async_returns_the_text_as_result() -> None:
    grep, read = _build_tools_from_trajectory({"rules": RULES})

    assert _get_tool_response(read, {"path": ".rules", "offset": 24.0}) == {
        "result": "24: rule 24\nrule 25"
    }
    assert _get_tool_response(
        grep, {"pattern": "RULE 25", "ignore_case": "false"}
    ) == {"result": "(no matches)"}
    assert _get_tool_response(grep, {"pattern": "rule 25"}) == {
        "result": ".rules:\n  25: rule 25"
    }
    assert _get_tool_response(read, {"path": ".rules", "fields": "x"}) == {
        "result": read.read(".rules").text
    }


def test_run_async_returns_errors_under_the_error_key() -> None:
    grep, read = _build_tools_from_trajectory({"rules": RULES})

    assert _get_tool_response(read, {"path": ".missing"}) == {
        "error": "'.missing' selects no value in this session"
    }
    assert _get_tool_response(read, {"path": ".rules", "offset": "abc"}) == {
        "error": "invalid arguments for read: could not convert string to "
        "float: 'abc'"
    }
    assert _get_tool_response(read, {"path": ".rules", "fields": [1]}) == {
        "error": "invalid arguments for read: expected an array of strings, "
        "got [1]"
    }
    assert _get_tool_response(grep, {"pattern": "x", "max_hits": 2.5}) == {
        "error": "invalid arguments for grep: expected an integer, got 2.5"
    }
    assert _get_tool_response(grep, {"pattern": "(x"})["error"].startswith(
        "invalid regular expression '(x'"
    )


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #


def test_a_runaway_pattern_is_stopped_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grep_read, "GREP_TIMEOUT_SECONDS", 1)
    case = _build_case([_build_event("user", {"text": "a" * 5_000 + "!"})])
    grep, _ = _build_tools(case, [_build_revision()])

    output = grep.grep(r"(a|aa)+$")

    assert output.is_error
    assert output.text == (
        "the pattern used more than 1 s of CPU time and was stopped; "
        "simplify it, for example by removing nested repetition"
    )


def test_a_runaway_pattern_leaves_other_threads_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A match that held the GIL would leave the sleeping loop no turns."""
    monkeypatch.setattr(grep_read, "GREP_TIMEOUT_SECONDS", 1)
    case = _build_case([_build_event("user", {"text": "a" * 5_000 + "!"})])
    grep, _ = _build_tools(case, [_build_revision()])
    search = threading.Thread(target=grep.grep, args=(r"(a|aa)+$",))
    turns = 0

    search.start()
    while search.is_alive():
        time.sleep(0.01)
        turns += 1
    search.join()

    assert turns > 10


class _FakeClock:
    """Stands in for the `time` module in `grep_read` with scripted clocks.

    Each read of a clock advances it by its step, so a test sets how much
    wall-clock and CPU time pass between two reads.
    """

    def __init__(self, *, wall_step: float, cpu_step: float) -> None:
        """Initialize both clocks at zero.

        Args:
            wall_step: Seconds that `monotonic` advances on each read.
            cpu_step: Seconds that `thread_time` advances on each read.
        """
        self._wall = 0.0
        self._cpu = 0.0
        self._wall_step = wall_step
        self._cpu_step = cpu_step

    def monotonic(self) -> float:
        """Advance and read the wall clock.

        Returns:
            Wall-clock seconds.
        """
        self._wall += self._wall_step
        return self._wall

    def thread_time(self) -> float:
        """Advance and read the thread's CPU clock.

        Returns:
            CPU seconds.
        """
        self._cpu += self._cpu_step
        return self._cpu


_MANY_LINES = [[(f"line {index}",)] for index in range(200)]


def test_a_search_that_waits_for_the_cpu_is_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread that waits for the GIL sees wall-clock time pass without
    CPU time; only the CPU time counts."""
    monkeypatch.setattr(
        grep_read, "time", _FakeClock(wall_step=1_000.0, cpu_step=0.0)
    )

    hits = grep_read._match_lines("line 1", False, _MANY_LINES)

    assert not isinstance(hits, grep_read.ToolOutput)
    assert [value_index for value_index, _ in hits] == [
        1,
        *range(10, 20),
        *range(100, 200),
    ]


def test_a_search_that_uses_its_cpu_time_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        grep_read, "time", _FakeClock(wall_step=1.0, cpu_step=1.0)
    )

    output = grep_read._match_lines("line 1", False, _MANY_LINES)

    assert isinstance(output, grep_read.ToolOutput)
    assert output.is_error
    assert output.text.startswith(
        f"the pattern used more than {grep_read.GREP_TIMEOUT_SECONDS} s of "
        "CPU time and was stopped"
    )


def test_a_match_that_times_out_with_cpu_time_left_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `regex` timeout is wall-clock time, so a match that times out
    while waiting for a CPU still has CPU time left."""
    real_compile = grep_read.regex.compile
    timeouts = [TimeoutError("regex timed out")]

    class StallingPattern:
        """Times out the first search, then searches for real."""

        def __init__(self, compiled: Any) -> None:
            self._compiled = compiled

        def search(self, form: str, **kwargs: Any) -> Any:
            if timeouts:
                raise timeouts.pop()
            return self._compiled.search(form, **kwargs)

    monkeypatch.setattr(
        grep_read.regex,
        "compile",
        lambda *args: StallingPattern(real_compile(*args)),
    )

    hits = grep_read._match_lines("^b", False, [[("a",)], [("b",)]])

    assert hits == [(1, [0])]
    assert timeouts == []


def test_matching_takes_any_form_of_a_line() -> None:
    assert grep_read._match_lines(
        "^b", False, [[("a",), ("x", "b")], [("c",)], [("B",)]]
    ) == [(0, [1])]


def test_lone_surrogates_in_the_session_and_pattern_do_not_fail_the_tools() -> (
    None
):
    case = _build_case(
        [_build_event("user", {"text": "first \ud800 line\nsecond line"})]
    )
    grep, read = _build_tools(case, [_build_revision()])
    path = "t0.e0.content.parts[0].text"

    assert grep.grep("first", path).text == f"{path}:\n  1: first \ud800 line"
    assert grep.grep("\ud800", path).text == f"{path}:\n  1: first \ud800 line"
    assert read.read(path).text == "1: first \ud800 line\nsecond line"
