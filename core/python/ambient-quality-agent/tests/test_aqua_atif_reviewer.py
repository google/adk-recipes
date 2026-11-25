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

"""Tests for the session reviewer's document, and for the fixtures end to end."""

from __future__ import annotations

import copy
import inspect
import json
import pathlib
import sys
from typing import Any

import aqua_atif
import pytest
from aqua_atif import (
    IngestStatus,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    annotate_document,
    build_reviewer_document,
    parse_document,
    validate_document,
)
from aqua_atif.paths import get_value

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "atif"
_FIXTURE_FILES = sorted(
    path for path in _FIXTURES.rglob("*.json") if path.is_file()
)


def _ingest_bytes(
    raw: bytes, markers: tuple[PendingMissingMarker, ...] = ()
) -> tuple[dict[str, Any], IngestStatus]:
    """Runs a file through every stage of the package.

    Args:
        raw: The file's bytes.
        markers: Markers from outside the document, such as unread parts.

    Returns:
        The stored document and its ingest status.
    """
    parsed = parse_document(raw)
    validated = validate_document(parsed.value)
    document = parsed.value
    status = annotate_document(
        document, markers=[*parsed.markers, *validated.markers, *markers]
    )
    return document, status


def _sample_run() -> dict[str, Any]:
    """Loads the sample run fixture.

    Returns:
        The parsed document.
    """
    return json.loads((_FIXTURES / "sample_run.atif.json").read_text())


def test_sample_run_stored_and_reviewer_forms():
    document = _sample_run()
    del document["agent"]["tool_definitions"]
    stored, status = _ingest_bytes(
        json.dumps(document).encode(),
        markers=(
            PendingMissingMarker(
                ("steps", 0),
                MissingMarker("message[1]", Reason.READ_ERROR, "403"),
            ),
        ),
    )

    assert status == IngestStatus.PARTIAL
    assert {"path": "agent.tool_definitions", "reason": "not_recorded"} in (
        stored["extra"]["aqua"]["missing"]
    )
    assert stored["steps"][0]["extra"] == {
        "aqua": {
            "missing": [
                {"path": "message[1]", "reason": "read_error", "detail": "403"}
            ]
        }
    }

    view = build_reviewer_document(stored)

    assert view["agent"] == {"name": "checkout-fixer", "version": "2.3"}
    assert view["steps"][0]["extra"] == {"aqua": {"missing": ["message[1]"]}}
    assert "agent.tool_definitions" in view["extra"]["aqua"]["missing"]
    assert set(view["extra"]["aqua"]) == {"missing"}
    assert view["steps"][0]["message"] == stored["steps"][0]["message"]


def test_reviewer_view_keeps_errors_and_drops_marker_reasons():
    stored = {
        "agent": {"name": "a", "version": "1"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "",
                "extra": {
                    "error": {"type": "RESOURCE_EXHAUSTED"},
                    "aqua": {
                        "error": {"type": "RESOURCE_EXHAUSTED"},
                        "missing": [
                            {
                                "path": "message[0]",
                                "reason": "read_error",
                                "detail": "404",
                            },
                            {"path": "message[0]", "reason": "too_large"},
                        ],
                    },
                },
            }
        ],
    }
    view = build_reviewer_document(stored)
    assert view["steps"][0]["extra"] == {
        "aqua": {
            "error": {"type": "RESOURCE_EXHAUSTED"},
            "missing": ["message[0]"],
        },
    }
    assert (
        stored["steps"][0]["extra"]["aqua"]["missing"][0]["reason"]
        == "read_error"
    )
    assert stored["steps"][0]["extra"]["error"] == {
        "type": "RESOURCE_EXHAUSTED"
    }


def _build_failed_run(
    result_extra: dict[str, Any], step_extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Annotates a one-call run whose result and step carry the given extras.

    Args:
        result_extra: The `extra` of the run's one observation result.
        step_extra: The `extra` of the step; none when omitted.

    Returns:
        The annotated document.
    """
    step = {
        "step_id": 1,
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": "c1", "function_name": "f", "arguments": {}}
        ],
        "observation": {
            "results": [
                {
                    "source_call_id": "c1",
                    "content": "denied",
                    "extra": result_extra,
                }
            ]
        },
    }
    if step_extra is not None:
        step["extra"] = step_extra
    document = {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "a", "version": "1"},
        "steps": [step],
    }
    annotate_document(document)
    return document


@pytest.mark.parametrize(
    ("result_extra", "expected"),
    [
        (
            {"is_error": True, "error_type": "EACCES"},
            {"aqua": {"error": {"type": "EACCES"}}},
        ),
        ({"is_error": False, "exit_code": 1}, {"exit_code": 1}),
        ({"tool_result_is_error": True}, {"tool_result_is_error": True}),
        (
            {"error": "boom"},
            {"aqua": {"error": {"type": None, "message": "boom"}}},
        ),
        (
            {"error": {"type": "TIMEOUT", "message": "30 s"}},
            {"aqua": {"error": {"type": "TIMEOUT", "message": "30 s"}}},
        ),
        (
            {"error": {"type": "TIMEOUT", "code": 504}},
            {
                "error": {"type": "TIMEOUT", "code": 504},
                "aqua": {"error": {"type": "TIMEOUT"}},
            },
        ),
        (
            {"failed": True, "status": "error"},
            {"failed": True, "status": "error"},
        ),
        # `is_error` is not a boolean, so AQuA read neither key.
        (
            {"is_error": "yes", "error_type": "EACCES"},
            {"is_error": "yes", "error_type": "EACCES"},
        ),
        # `error` was read first, so the flags beside it stay.
        (
            {"error": "boom", "is_error": True, "error_type": "NET_TIMEOUT"},
            {
                "is_error": True,
                "error_type": "NET_TIMEOUT",
                "aqua": {"error": {"type": None, "message": "boom"}},
            },
        ),
        ({"error": 500}, {"error": 500}),
    ],
)
def test_reviewer_document_removes_the_error_keys_aqua_read(
    result_extra, expected
):
    stored = _build_failed_run(dict(result_extra))
    view = build_reviewer_document(stored)
    result = view["steps"][0]["observation"]["results"][0]
    assert result["extra"] == expected
    stored_extra = stored["steps"][0]["observation"]["results"][0]["extra"]
    assert {
        key: value for key, value in stored_extra.items() if key != "aqua"
    } == result_extra


def test_reviewer_document_keeps_tool_result_is_error_on_a_step():
    stored = _build_failed_run({}, {"tool_result_is_error": True})
    view = build_reviewer_document(stored)
    assert view["steps"][0]["extra"] == {"tool_result_is_error": True}
    assert view["steps"][0]["observation"]["results"][0]["extra"] == {}


def test_reviewer_view_keeps_resolved_subagent_refs_and_strips_extra_aqua_keys():
    stored = {
        "agent": {"name": "a", "version": "1"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "delegate",
                "tool_calls": [
                    {
                        "tool_call_id": "c",
                        "function_name": "task",
                        "arguments": {},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "c",
                            "subagent_trajectory_ref": [
                                {
                                    "trajectory_path": "sub.json",
                                    "trajectory_id": "sub.json",
                                },
                                {"trajectory_path": "unread.json"},
                            ],
                        }
                    ]
                },
            }
        ],
        "subagent_trajectories": [
            {
                "trajectory_id": "sub.json",
                "agent": {"name": "s", "version": "1"},
                "steps": [{"step_id": 1, "source": "user", "message": "x"}],
                "extra": {
                    "aqua": {
                        "missing": [
                            {"path": "final_metrics", "reason": "not_recorded"}
                        ],
                    }
                },
            }
        ],
    }
    view = build_reviewer_document(stored)
    refs = view["steps"][0]["observation"]["results"][0][
        "subagent_trajectory_ref"
    ]
    assert refs == [
        {"trajectory_path": "sub.json", "trajectory_id": "sub.json"},
        {"trajectory_path": "unread.json"},
    ]
    assert view["subagent_trajectories"][0]["extra"] == {
        "aqua": {"missing": ["final_metrics"]}
    }


def test_reviewer_view_removes_empty_aqua_and_keeps_sibling_extra():
    stored = {
        "agent": {"name": "a", "version": "1"},
        "steps": [{"step_id": 1, "source": "user", "message": "x"}],
        "extra": {
            "task": "t",
            "aqua": {"records_tool_errors": True},
        },
    }
    assert build_reviewer_document(stored)["extra"] == {"task": "t"}


def test_reviewer_view_removes_newer_fields_and_keeps_their_paths():
    document = {
        "schema_version": "ATIF-v1.9",
        "agent": {"name": "a", "version": "1", "skills": ["x"]},
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": [
                    {"type": "text", "text": "hi", "lang": "en"},
                    {"type": "video", "source": {"uri": "v"}, "fps": 30},
                    {
                        "type": "image",
                        "source": {
                            "media_type": "image/png",
                            "path": "p",
                            "w": 1,
                        },
                    },
                ],
                "new_field": 1,
                "observation": {"results": [], "summary": "s"},
            }
        ],
    }
    stored, status = _ingest_bytes(json.dumps(document).encode())
    assert status == IngestStatus.INGESTED

    view = build_reviewer_document(stored)

    step = view["steps"][0]
    assert "new_field" not in step
    assert step["observation"] == {"results": []}
    assert step["message"] == [
        {"type": "text", "text": "hi"},
        {"type": "video", "source": {"uri": "v"}, "fps": 30},
        {"type": "image", "source": {"media_type": "image/png", "path": "p"}},
    ]
    assert set(step["extra"]["aqua"]["missing"]) == {
        "new_field",
        "observation.summary",
        "message[0].lang",
        "message[1]",
        "message[2].source.w",
    }
    assert "skills" not in view["agent"]
    assert stored["steps"][0]["new_field"] == 1


@pytest.mark.parametrize(
    "path", _FIXTURE_FILES, ids=[path.name for path in _FIXTURE_FILES]
)
def test_every_fixture_ingests_and_has_a_reviewer_view(path):
    raw = path.read_bytes()
    original = json.loads(raw)

    stored, status = _ingest_bytes(raw)

    assert status == IngestStatus.INGESTED
    aqua_atif.Trajectory.model_validate(stored)
    without_aqua = copy.deepcopy(stored)
    for holder in aqua_atif.list_holders(without_aqua):
        extra = holder.value.get("extra")
        if isinstance(extra, dict):
            extra.pop("aqua", None)
            if not extra and "extra" not in get_value(original, holder.path):
                del holder.value["extra"]
    assert without_aqua == original

    view = build_reviewer_document(stored)

    aqua_atif.Trajectory.model_validate(view)
    for holder in aqua_atif.list_holders(view):
        aqua = aqua_atif.get_aqua(holder.value) or {}
        assert set(aqua) <= {"missing", "error"}
        assert all(isinstance(item, str) for item in aqua.get("missing", []))


def test_complete_v1_7_fixture_errors_and_references():
    stored, _ = _ingest_bytes(
        (_FIXTURES / "complete_v1_7.atif.json").read_bytes()
    )
    errors = [
        aqua_atif.extract_error(holder.value)
        for holder in aqua_atif.list_holders(stored)
        if holder.kind == aqua_atif.HolderKind.RESULT
    ]
    failed = [error for error in errors if error is not None]
    assert failed
    assert all(error.message is None for error in failed)
    paths = [marker["path"] for marker in stored["extra"]["aqua"]["missing"]]
    assert "continued_trajectory" in paths
    assert "tool_errors" not in paths
    by_path = [
        placed
        for placed in aqua_atif.list_placed_markers(stored)
        if placed.marker.path == "trajectory"
    ]
    assert len(by_path) == 1


def test_harbor_terminus_2_fixture_reads_v1_6():
    path = (
        _FIXTURES / "harbor_terminus_2" / "hello-world-timeout.trajectory.json"
    )
    validated = validate_document(json.loads(path.read_bytes()))
    assert str(validated.schema_version) == "ATIF-v1.6"


def test_reviewer_view_cleans_newer_fields_in_message_parts():
    part = {"type": "text", "text": "Fix it.", "new_field": "newer"}
    document = {
        "schema_version": "ATIF-v1.9",
        "agent": {"name": "a", "version": "1"},
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": [part],
            }
        ],
    }
    stored, _ = _ingest_bytes(json.dumps(document).encode())
    view = build_reviewer_document(stored)
    assert view["steps"][0]["message"] == [{"type": "text", "text": "Fix it."}]
    assert view["steps"][0]["extra"]["aqua"]["missing"] == [
        "message[0].new_field"
    ]


def test_reviewer_view_of_a_256_level_document_needs_no_deep_recursion():
    nested: Any = {}
    for _ in range(250):
        nested = {"a": nested}
    stored = {
        "agent": {"name": "a", "version": "1"},
        "steps": [{"step_id": 1, "source": "user", "message": "x"}],
        "extra": {"deep": nested},
    }
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 120)
    try:
        view = build_reviewer_document(stored)
    finally:
        sys.setrecursionlimit(limit)
    assert view["extra"]["deep"] == nested
    assert view["extra"]["deep"] is not nested
