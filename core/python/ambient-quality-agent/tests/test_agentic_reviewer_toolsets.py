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

"""Unit tests for the session reviewer's toolset switch.

`prompts.REVIEW_TOOLSET` picks the tools of a review and the prompt that
describes them. All session data is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest
from ambient_quality_agent.tools.review import agentic_reviewer
from ambient_quality_agent.tools.review.agentic_reviewer import (
    prompts,
    toolsets,
)
from ambient_quality_agent.tools.review.agentic_reviewer.tools import (
    grep_read,
    jq,
)
from google.genai import types

from tests.test_agentic_reviewer import (
    FINDING,
    FakeClient,
    _answer,
    _build_case,
    _build_revisions,
)
from tests.test_agentic_reviewer_trajectory_json import (
    LONG_INSTRUCTION,
    _build_revision,
    _build_sample_case,
)

JQ_PROMPT_SHA256 = {
    True: (
        "c6063ef33f529c669b13dae65487a13d9cceafc248147081de92d3571c0bbd18",
        "a35709f25854e9326badd3677df65c503c11043618066d0bc720f9fe4921974f",
    ),
    False: (
        "40f302b738fc9f4751faf48d7a1fdb0666d41fc55def0e43e8e515ee4cca0124",
        "a35709f25854e9326badd3677df65c503c11043618066d0bc720f9fe4921974f",
    ),
}
"""SHA-256 hex digests of the system instruction and the first message that
the jq toolset builds for `_build_sample_prompt`, keyed by
`prompts.ARGON_RULES`."""

_GOAL = "Ship refunds within a day."


def _build_sample_prompt() -> agentic_reviewer.ReviewPrompt:
    """Build the prompt of the sample session with a long instruction.

    Returns:
        The prompt for the current `prompts.REVIEW_TOOLSET`, with the
        developer goal and a review focus.
    """
    return agentic_reviewer.build_review_prompt(
        _build_sample_case(),
        [_build_revision(LONG_INSTRUCTION)],
        session_review_focus="refunds",
        goal=_GOAL,
    )


def test_grep_read_is_the_default_toolset() -> None:
    prompt = _build_sample_prompt()

    assert prompts.REVIEW_TOOLSET == toolsets.GREP_READ
    assert prompt.toolset == toolsets.GREP_READ


@pytest.mark.parametrize("argon_rules", [True, False])
def test_the_jq_toolset_prompt_matches_its_pinned_digests(
    monkeypatch: pytest.MonkeyPatch, argon_rules: bool
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.JQ)
    monkeypatch.setattr(prompts, "ARGON_RULES", argon_rules)

    prompt = _build_sample_prompt()

    assert prompt.toolset == toolsets.JQ
    assert (
        hashlib.sha256(prompt.system_instruction.encode("utf-8")).hexdigest(),
        hashlib.sha256(prompt.initial_message.encode("utf-8")).hexdigest(),
    ) == JQ_PROMPT_SHA256[argon_rules]


def test_the_jq_toolset_declares_only_the_query_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.JQ)
    client = FakeClient([_answer([FINDING])])

    agentic_reviewer.review_session(
        _build_case(), _build_revisions(), client=client
    )

    ((_, config),) = client.models.requests
    assert config.tools == [
        types.Tool(function_declarations=[jq.build_query_declaration()])
    ]


def test_the_grep_read_toolset_declares_grep_and_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.GREP_READ)
    declarations = [
        grep_read.build_grep_declaration(),
        grep_read.build_read_declaration(),
    ]
    client = FakeClient([_answer([FINDING])])

    outcome = agentic_reviewer.review_session(
        _build_case(), _build_revisions(), client=client
    )

    ((_, config),) = client.models.requests
    assert config.tools == [types.Tool(function_declarations=declarations)]
    assert outcome.prompt.toolset == toolsets.GREP_READ
    assert len(outcome.findings) == 1


def test_the_grep_read_toolset_prompt_describes_grep_and_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.JQ)
    jq_prompt = _build_sample_prompt()
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.GREP_READ)

    prompt = _build_sample_prompt()

    assert prompts.GREP_READ_TOOLS_PARAGRAPH in prompt.system_instruction
    assert prompt.system_instruction == jq_prompt.system_instruction.replace(
        prompts.JQ_TOOLS_PARAGRAPH, prompts.GREP_READ_TOOLS_PARAGRAPH
    )
    for text in (
        prompt.system_instruction,
        prompt.initial_message,
        prompts.TEXT_REASK,
        prompts.TEXT_FORCE,
        prompts.PRUNED_NOTE,
        prompts.UNKNOWN_TOOL_ERROR,
    ):
        assert jq.QUERY_TOOL not in text
        assert not re.search(r"\bjq\b", text)


def test_the_grep_read_toolset_outline_gives_read_line_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.GREP_READ)

    prompt = _build_sample_prompt()

    outline = prompt.trajectory_json["agents"][0]["instruction_outline"]
    assert outline == [
        [1, "<ROLE>"],
        [303, "## Refunds"],
        [605, "### Escalation"],
    ]
    assert '"instruction_outline": [[1, "<ROLE>"], [303,' in (
        prompt.initial_message
    )
    read = grep_read.ReadTool(prompt.trajectory_json)
    for line, heading in outline:
        assert read.read(".agents[0].instruction", line, 1).text.startswith(
            f"{line}: {heading}"
        )


def test_the_jq_toolset_outline_gives_character_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.JQ)

    prompt = _build_sample_prompt()

    outline = prompt.trajectory_json["agents"][0]["instruction_outline"]
    for offset, heading in outline:
        assert LONG_INSTRUCTION[offset:].startswith(heading)
    assert outline[1][0] > 303


def test_the_grep_read_toolset_string_stub_counts_read_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.JQ)
    jq_message = _build_sample_prompt().initial_message
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", toolsets.GREP_READ)

    message = _build_sample_prompt().initial_message

    chars = len(LONG_INSTRUCTION)
    lines = len(LONG_INSTRUCTION.splitlines())
    assert f'{{"$elided": "string, {chars} chars"}}' in jq_message
    assert f'{{"$elided": "string, {chars} chars, {lines} lines"}}' in message
    assert not re.search(r"chars, \d+ lines", jq_message)


def test_an_unknown_toolset_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "REVIEW_TOOLSET", "sed")

    with pytest.raises(ValueError, match="unknown review toolset 'sed'"):
        _build_sample_prompt()
    with pytest.raises(ValueError, match="unknown review toolset 'sed'"):
        toolsets.build_review_tools("sed", {})


def test_each_toolset_builds_fresh_tools_bound_to_the_session() -> None:
    session: dict[str, Any] = {"agents": [], "turns": []}

    first = toolsets.build_review_tools(toolsets.GREP_READ, session)
    second = toolsets.build_review_tools(toolsets.GREP_READ, session)

    assert [type(tool) for tool in first] == [
        grep_read.GrepTool,
        grep_read.ReadTool,
    ]
    assert [tool.name for tool in first] == ["grep", "read"]
    assert all(a is not b for a, b in zip(first, second, strict=True))
    assert [
        tool.name for tool in toolsets.build_review_tools("jq", session)
    ] == [jq.QUERY_TOOL]
    assert toolsets.build_tool_declarations(toolsets.GREP_READ) == [
        grep_read.build_grep_declaration(),
        grep_read.build_read_declaration(),
    ]


def test_the_toolsets_are_jq_and_grep_read() -> None:
    assert toolsets.TOOLSETS == (toolsets.JQ, toolsets.GREP_READ)
    assert toolsets.TOOLSETS == ("jq", "grep_read")
