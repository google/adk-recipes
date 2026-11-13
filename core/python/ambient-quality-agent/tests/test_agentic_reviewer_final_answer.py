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

"""Unit tests for reading the findings out of the reviewer's final reply."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from ambient_quality_agent.tools.insights import findings as findings_module
from ambient_quality_agent.tools.review.agentic_reviewer import final_answer
from google.genai import types

FINDING = {
    "agent_id": "worker",
    "expected_behavior": "not refund a shipped order",
    "actual_behavior": "called refund",
}


def _render_findings_json(*actuals: str) -> str:
    """Serialize a findings JSON object with one entry per `actuals` string.

    Args:
        *actuals: ``actual_behavior`` descriptions.

    Returns:
        Serialized JSON object string.
    """
    return json.dumps(
        {"findings": [{**FINDING, "actual_behavior": a} for a in actuals]}
    )


@pytest.mark.parametrize(
    ("template", "path"),
    [
        ("OBJECT", final_answer.FINAL_PARSE_STRICT),
        ("```json\nOBJECT\n```", final_answer.FINAL_PARSE_STRICT),
        (
            "Prose.\n```json\nOBJECT\n```\nMore.",
            final_answer.FINAL_PARSE_FENCED,
        ),
        ("ARRAY", final_answer.FINAL_PARSE_ARRAY),
        (
            'I checked {"turn": 1}. OBJECT Done.',
            final_answer.FINAL_PARSE_EMBEDDED,
        ),
    ],
    ids=["plain", "fenced", "prose-fence", "bare-array", "embedded"],
)
def test_each_parse_path_yields_the_validated_findings(
    template: str, path: str
) -> None:
    payload = _render_findings_json("a", "b")
    array = json.dumps(json.loads(payload)["findings"])
    text = template.replace("ARRAY", array).replace("OBJECT", payload)

    parsed = final_answer.parse_final_text(text, "s-1", {"worker"})

    assert parsed is not None
    findings, parse_path = parsed
    assert parse_path == path
    assert [(f.actual_behavior, f.trajectory_id) for f in findings] == [
        ("a", "s-1"),
        ("b", "s-1"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "prose",
        '{"defects": []}',
        '{"findings": "none"}',
        '{"findings": {}}',
        '{"findings": [',
        "",
    ],
)
def test_a_text_without_findings_json_parses_to_none(text: str) -> None:
    assert final_answer.parse_final_text(text, "s-1", set()) is None


def test_the_findings_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(findings_module, "MAX_FINDINGS_PER_TRAJECTORY", 2)

    parsed = final_answer.parse_final_text(
        _render_findings_json("a", "b", "c"), "s-1", {"worker"}
    )

    assert parsed is not None
    assert [f.actual_behavior for f in parsed[0]] == ["a", "b"]


def test_the_reply_text_leaves_out_thoughts() -> None:
    content = types.Content(
        role="model",
        parts=[
            types.Part(text="Thinking.", thought=True),
            types.Part(text="{"),
            types.Part(text="}"),
        ],
    )

    assert final_answer.extract_reply_text(content) == "{}"
    assert final_answer.extract_reply_text(None) == ""


def _parse(
    raw_findings: list[Any], known_agent_ids: set[str] | None = None
) -> list[Any]:
    """Validate raw findings for session ``session-1``.

    Args:
        raw_findings: The findings list of a findings JSON.
        known_agent_ids: Valid agent identifiers; defaults to ``root_agent``.

    Returns:
        List of parsed findings.
    """
    return final_answer.build_findings(
        raw_findings,
        "session-1",
        {"root_agent"} if known_agent_ids is None else known_agent_ids,
    )


def test_a_clean_session_yields_nothing() -> None:
    assert _parse([]) == []


def test_two_unrelated_defects_stay_two_findings() -> None:
    parsed = _parse(
        [
            {
                "agent_id": "root_agent",
                "expected_behavior": "call book_flight",
                "actual_behavior": "made no tool call",
            },
            {
                "agent_id": "root_agent",
                "expected_behavior": "answer in English",
                "actual_behavior": "answered in French",
            },
        ]
    )

    assert [f.expected_behavior for f in parsed] == [
        "call book_flight",
        "answer in English",
    ]
    assert {f.trajectory_id for f in parsed} == {"session-1"}
    assert {f.agent_id for f in parsed} == {"root_agent"}


def test_an_empty_or_malformed_finding_is_dropped() -> None:
    parsed = _parse(
        [
            {
                "agent_id": "root_agent",
                "expected_behavior": "",
                "actual_behavior": "",
            },
            "not a finding",
            {
                "agent_id": "root_agent",
                "expected_behavior": "call book_flight",
                "actual_behavior": "",
            },
        ]
    )

    assert [f.expected_behavior for f in parsed] == ["call book_flight"]


def test_the_cap_truncates_a_runaway_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    over = findings_module.MAX_FINDINGS_PER_TRAJECTORY + 3
    raw_findings = [
        {
            "agent_id": "root_agent",
            "expected_behavior": f"expectation {i}",
            "actual_behavior": "fell short",
        }
        for i in range(over)
    ]

    with caplog.at_level(logging.WARNING):
        parsed = _parse(raw_findings)

    assert len(parsed) == findings_module.MAX_FINDINGS_PER_TRAJECTORY
    assert any("keeping the first" in r.getMessage() for r in caplog.records)


def test_an_invented_agent_id_is_blanked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_finding = {
        "agent_id": "billing_agent",
        "expected_behavior": "call file_expense",
        "actual_behavior": "made no tool call",
    }

    with caplog.at_level(logging.WARNING):
        (finding,) = _parse([raw_finding], {"travel_desk_agent"})

    assert finding.agent_id == ""
    assert finding.expected_behavior == "call file_expense"
    assert any("does not contain" in r.getMessage() for r in caplog.records)
