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

"""Tests that crafted files cannot make `aqua_atif` do quadratic work.

Each input is a small file (under 1 MB) shaped to hit one place where the work
could grow with the square of its size. With linear work each runs in well
under a second; quadratic work takes minutes. The 10-second bound leaves a
wide margin for slow machines.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aqua_atif import (
    MissingMarker,
    PendingMissingMarker,
    Reason,
    annotate_document,
    build_reviewer_document,
    parse_document,
    validate_document,
)

_BOUND_SECONDS = 10.0


def _document(steps: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    """Builds a document with the given steps.

    Args:
        steps: The steps.
        **fields: Other root fields.

    Returns:
        The document.
    """
    return {
        "schema_version": "ATIF-v1.8",
        "agent": {"name": "a", "version": "1"},
        "steps": steps,
        **fields,
    }


def _call_step(index: int, **result: Any) -> dict[str, Any]:
    """Builds an agent step with one tool call and its result.

    Args:
        index: Zero-based position of the step.
        **result: Extra fields for the result.

    Returns:
        The step.
    """
    return {
        "step_id": index + 1,
        "source": "agent",
        "message": "m",
        "tool_calls": [
            {"tool_call_id": f"c{index}", "function_name": "f", "arguments": {}}
        ],
        "observation": {
            "results": [
                {"source_call_id": f"c{index}", "content": "x", **result}
            ]
        },
    }


def _ingest_within_bound(raw: str) -> dict[str, Any]:
    """Runs a file through every stage and checks the time taken.

    Args:
        raw: The file's text.

    Returns:
        The stored document.
    """
    start = time.monotonic()
    parsed = parse_document(raw)
    validated = validate_document(parsed.value)
    document = parsed.value
    annotate_document(document, markers=[*parsed.markers, *validated.markers])
    build_reviewer_document(document)
    elapsed = time.monotonic() - start
    assert elapsed < _BOUND_SECONDS, f"took {elapsed:.1f} s"
    return document


def test_many_subagents_without_tool_error_keys():
    count = 800
    steps = [
        _call_step(i, subagent_trajectory_ref=[{"trajectory_id": f"s{i}"}])
        for i in range(count)
    ]
    subagents = [
        _document([_call_step(0)], trajectory_id=f"s{i}") for i in range(count)
    ]
    document = _ingest_within_bound(
        json.dumps(_document(steps, subagent_trajectories=subagents))
    )
    assert len(document["subagent_trajectories"]) == count


def test_many_replaced_surrogates_on_one_step():
    count = 4000
    parts = [{"type": "text", "text": "x\ud800"} for _ in range(count)]
    step = {"step_id": 1, "source": "user", "message": parts}
    document = _ingest_within_bound(json.dumps(_document([step])))
    assert len(document["steps"][0]["extra"]["aqua"]["missing"]) == count


def test_many_unknown_parts_in_a_newer_version():
    count = 4000
    parts = [{"type": "video", "n": i} for i in range(count)]
    step = {"step_id": 1, "source": "user", "message": parts}
    document = _ingest_within_bound(
        json.dumps(_document([step], schema_version="ATIF-v1.9"))
    )
    assert len(document["steps"][0]["extra"]["aqua"]["missing"]) == count


@pytest.mark.parametrize("count", [20000])
def test_many_markers_on_one_step_added_twice(count):
    markers = [
        PendingMissingMarker(
            ("steps", 0), MissingMarker(f"message[{i}]", Reason.READ_ERROR)
        )
        for i in range(count)
    ]
    document = _document([{"step_id": 1, "source": "user", "message": "m"}])
    start = time.monotonic()
    annotate_document(document, markers=markers)
    annotate_document(document, markers=markers)
    elapsed = time.monotonic() - start
    assert elapsed < _BOUND_SECONDS, f"took {elapsed:.1f} s"
    assert len(document["steps"][0]["extra"]["aqua"]["missing"]) == count
