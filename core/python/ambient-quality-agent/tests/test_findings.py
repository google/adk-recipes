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

"""Unit tests for `findings.extract_trajectory_turns`."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.insights.findings import (
    extract_trajectory_turns,
)


def _build_case(agents: dict[str, dict[str, Any]]) -> EvalCase:
    """Build an `EvalCase` fixture with the given agent definitions.

    Args:
        agents: Mapping of agent names to their configurations.

    Returns:
        Constructed `EvalCase` instance.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": "session-1",
            "agent_data": {
                "agents": {
                    name: {"agent_id": name, **config}
                    for name, config in agents.items()
                },
                "turns": [
                    {
                        "turn_index": 0,
                        "events": [
                            {
                                "author": "user",
                                "content": {
                                    "role": "user",
                                    "parts": [{"text": "book me a flight"}],
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )


def test_the_turns_are_serialized_without_empty_fields() -> None:
    (turn,) = extract_trajectory_turns(
        _build_case({"root_agent": {"instruction": "be helpful"}})
    )

    assert turn["turn_index"] == 0
    assert turn["events"][0]["content"]["parts"] == [
        {"text": "book me a flight"}
    ]


def test_a_trajectory_without_turns_has_no_turns() -> None:
    """Verify cases lacking agent data are handled gracefully without error."""
    assert extract_trajectory_turns(EvalCase(eval_case_id="c")) == []
    assert extract_trajectory_turns(None) == []


def test_a_turn_that_fails_to_serialize_yields_no_turns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    case = _build_case({"root_agent": {"instruction": "be helpful"}})
    turn_type = type(case.agent_data.turns[0])  # type: ignore[union-attr]

    def fail(*args: Any, **kwargs: Any) -> Any:
        """Raise ValueError to simulate a serialization failure.

        Args:
            *args: Ignored positional arguments.
            **kwargs: Ignored keyword arguments.

        Raises:
            ValueError: Always raised to simulate unserializable turn data.
        """
        raise ValueError("unserializable")

    monkeypatch.setattr(turn_type, "model_dump", fail)

    with caplog.at_level(logging.WARNING):
        assert extract_trajectory_turns(case) == []
    assert any(
        "could not serialize" in record.getMessage()
        for record in caplog.records
    )
