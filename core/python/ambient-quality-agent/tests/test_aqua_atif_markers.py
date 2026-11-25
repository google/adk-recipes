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

"""Tests for errors and markers in `aqua_atif`."""

from __future__ import annotations

import copy
from typing import Any

import aqua_atif
import pytest
from aqua_atif import (
    DROPPED_CONTENT_CHECK,
    MEDIA_CHECK,
    AquaError,
    IngestStatus,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    annotate_document,
    extract_error,
    resolve_disabled_check,
)
from aqua_atif.errors import records_tool_errors
from aqua_atif.markers import losses_to_markers

_COMPLETE_AGENT = {
    "name": "a",
    "version": "1",
    "model_name": "m",
    "tool_definitions": [],
}


def _step(step_id: int, source: str = "agent", **fields: Any) -> dict[str, Any]:
    """Builds a step.

    Args:
        step_id: The step id.
        source: The step source.
        **fields: Other fields.

    Returns:
        The step.
    """
    return {"step_id": step_id, "source": source, "message": "m", **fields}


def _call_step(
    step_id: int,
    results: list[dict[str, Any]],
    call_ids: tuple[str, ...] = ("c1",),
    **fields: Any,
) -> dict[str, Any]:
    """Builds an agent step with tool calls and results.

    Args:
        step_id: The step id.
        results: The observation results.
        call_ids: The tool call ids.
        **fields: Other step fields.

    Returns:
        The step.
    """
    return _step(
        step_id,
        tool_calls=[
            {"tool_call_id": call_id, "function_name": "f", "arguments": {}}
            for call_id in call_ids
        ],
        observation={"results": results},
        **fields,
    )


def _complete(**fields: Any) -> dict[str, Any]:
    """Builds a document that records everything, so it gets no root marker.

    Args:
        **fields: Root fields to set or replace.

    Returns:
        The document.
    """
    document: dict[str, Any] = {
        "schema_version": "ATIF-v1.8",
        "agent": copy.deepcopy(_COMPLETE_AGENT),
        "steps": [
            _step(
                1,
                "system",
                message="be helpful",
                timestamp="2026-01-01T00:00:00Z",
            ),
            _step(2, "user"),
            _step(3, reasoning_content="r", metrics={"prompt_tokens": 1}),
        ],
        "final_metrics": {"total_steps": 3},
    }
    document.update(fields)
    return document


def _ingest(document: dict[str, Any]) -> IngestStatus:
    """Runs a document through the package's stages, in place.

    Args:
        document: The parsed document.

    Returns:
        The ingest status.
    """
    validated = aqua_atif.validate_document(document)
    return annotate_document(document, markers=validated.markers)


def _missing(holder: dict[str, Any]) -> list[dict[str, str]]:
    """Gets the stored markers of one object.

    Args:
        holder: An object that can hold `extra`.

    Returns:
        Its `extra.aqua.missing`, or an empty list.
    """
    return holder.get("extra", {}).get("aqua", {}).get("missing", [])


def _root_paths(document: dict[str, Any]) -> list[str]:
    """Gets the paths of the root markers.

    Args:
        document: The annotated document.

    Returns:
        The paths.
    """
    return [marker["path"] for marker in _missing(document)]


# Tool errors: the two conventions, in order


def _error_of(result: dict[str, Any], step_extra: dict | None = None) -> Any:
    """Annotates a one-result document and reads the result's error.

    Args:
        result: The observation result, without `source_call_id`.
        step_extra: The step's `extra`.

    Returns:
        `extract_error` of the result, and the result as stored.
    """
    step = _call_step(3, [{"source_call_id": "c1", **result}])
    if step_extra is not None:
        step["extra"] = step_extra
    document = _complete()
    document["steps"][2] = step
    _ingest(document)
    stored = document["steps"][2]["observation"]["results"][0]
    return extract_error(stored), stored


def test_convention_1_error_string_becomes_the_message():
    error, stored = _error_of({"content": "", "extra": {"error": "boom"}})
    assert error == AquaError(type=None, message="boom")
    assert stored["extra"]["aqua"]["error"] == {"type": None, "message": "boom"}


def test_convention_1_error_object_is_kept():
    error, stored = _error_of(
        {
            "content": "",
            "extra": {
                "error": {
                    "type": "TIMEOUT",
                    "message": "no response after 30 s",
                }
            },
        }
    )
    assert error == AquaError("TIMEOUT", "no response after 30 s")
    assert stored["extra"]["aqua"]["error"] == {
        "type": "TIMEOUT",
        "message": "no response after 30 s",
    }


@pytest.mark.parametrize("value", ["", {}, {"code": 3}])
def test_convention_1_empty_error_is_a_success(value):
    assert _error_of({"content": "x", "extra": {"error": value}})[0] is None


def test_convention_1_wins_over_is_error():
    error, _ = _error_of(
        {"content": "x", "extra": {"error": "", "is_error": True}}
    )
    assert error is None


def test_convention_2_is_error_does_not_copy_the_output():
    error, stored = _error_of(
        {"content": "permission denied", "extra": {"is_error": True}}
    )
    assert error == AquaError(type=None)
    assert stored["extra"]["aqua"]["error"] == {"type": None}


def test_convention_2_error_type_becomes_the_type():
    error, _ = _error_of(
        {"content": "x", "extra": {"is_error": True, "error_type": "NotFound"}}
    )
    assert error == AquaError(type="NotFound")


def test_convention_2_empty_error_type_becomes_none():
    error, stored = _error_of(
        {"content": "x", "extra": {"is_error": True, "error_type": ""}}
    )
    assert error == AquaError(type=None)
    assert stored["extra"]["aqua"]["error"] == {"type": None}


def test_convention_2_false_is_a_success_even_with_an_exit_code():
    error, stored = _error_of(
        {"content": "x", "extra": {"is_error": False, "exit_code": 1}}
    )
    assert error is None
    assert "aqua" not in stored["extra"]


@pytest.mark.parametrize("flag", [True, False])
def test_tool_result_is_error_on_the_result_is_not_read(flag):
    error, stored = _error_of(
        {"content": "x", "extra": {"tool_result_is_error": flag}}
    )
    assert error is None
    assert stored["extra"] == {"tool_result_is_error": flag}


def test_tool_result_is_error_on_the_step_is_not_read():
    error, stored = _error_of(
        {"content": "denied"}, {"tool_result_is_error": True}
    )
    assert error is None
    assert "extra" not in stored


def test_several_results_get_one_reading_each():
    document = _complete()
    document["steps"][2] = _call_step(
        3,
        [
            {"source_call_id": "c1", "extra": {"is_error": True}},
            {"source_call_id": "c1", "extra": {"is_error": False}},
        ],
    )
    _ingest(document)
    results = document["steps"][2]["observation"]["results"]
    assert [extract_error(result) for result in results] == [AquaError(), None]


def test_step_error_is_read():
    document = _complete()
    document["steps"][2]["extra"] = {
        "error": {"type": "RESOURCE_EXHAUSTED", "message": "429"}
    }
    _ingest(document)
    assert extract_error(document["steps"][2]) == AquaError(
        "RESOURCE_EXHAUSTED", "429"
    )
    assert extract_error(document["steps"][1]) is None


def test_root_error_is_read_on_the_root_and_on_subagents():
    subagent = _complete(trajectory_id="sub", extra={"error": "crashed"})
    document = _complete(
        extra={"error": "run failed"}, subagent_trajectories=[subagent]
    )
    _ingest(document)
    assert extract_error(document) == AquaError(message="run failed")
    assert extract_error(document["subagent_trajectories"][0]) == AquaError(
        message="crashed"
    )


def test_extract_error_reads_only_aqua_error():
    result = {"content": "x", "extra": {"aqua": {"error": {"type": "T"}}}}
    assert extract_error(result) == AquaError(type="T")
    assert extract_error({"extra": {"error": "producer key only"}}) is None


def test_annotating_again_preserves_producer_aqua_error_and_updates_from_extra_error():
    document = _complete()
    _ingest(document)
    document["steps"][1]["extra"] = {"aqua": {"error": {"type": "DIRECT"}}}
    annotate_document(document)
    assert extract_error(document["steps"][1]) == AquaError(type="DIRECT")

    document["steps"][1]["extra"]["error"] = {"type": "UPDATED"}
    annotate_document(document)
    assert extract_error(document["steps"][1]) == AquaError(type="UPDATED")


# The `tool_errors` marker


def _tool_doc(*results: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Builds a complete document whose last step has results.

    Args:
        *results: Observation results, without `source_call_id`.
        **fields: Root fields to set or replace.

    Returns:
        The document.
    """
    document = _complete(**fields)
    document["steps"][2] = _call_step(
        3,
        [{"source_call_id": "c1", **result} for result in results],
        reasoning_content="r",
        metrics={"prompt_tokens": 1},
    )
    return document


@pytest.mark.parametrize(
    "extra",
    [
        {"is_error": "yes", "error_type": "EACCES"},
        {"is_error": 1},
        {"error": 500},
        {"error": ["boom"]},
    ],
)
def test_an_error_key_of_another_type_is_not_read(extra: dict[str, Any]):
    document = _tool_doc({"content": "denied", "extra": dict(extra)})
    _ingest(document)
    stored = document["steps"][2]["observation"]["results"][0]
    assert extract_error(stored) is None
    assert stored["extra"] == extra
    assert "tool_errors" in _root_paths(document)


@pytest.mark.parametrize(
    "error", [{"type": "", "message": ""}, {"type": ""}, {"message": ""}, {}]
)
def test_an_error_object_without_type_or_message_is_not_a_failure(
    error: dict[str, str],
):
    error_read, stored = _error_of({"content": "ok", "extra": {"error": error}})
    assert error_read is None
    assert "aqua" not in stored["extra"]


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_message", "expected_dict"),
    [
        (
            {"type": "", "message": "boom"},
            None,
            "boom",
            {"type": None, "message": "boom"},
        ),
        (
            {"type": "TIMEOUT", "message": ""},
            "TIMEOUT",
            None,
            {"type": "TIMEOUT"},
        ),
    ],
)
def test_an_error_object_drops_an_empty_type_or_message(
    error: dict[str, str],
    expected_type: str | None,
    expected_message: str | None,
    expected_dict: dict[str, Any],
):
    error_read, stored = _error_of(
        {"content": "err", "extra": {"error": error}}
    )
    assert error_read == AquaError(type=expected_type, message=expected_message)
    assert stored["extra"]["aqua"]["error"] == expected_dict


def test_tool_errors_marker_when_no_result_has_a_key():
    document = _tool_doc({"content": "ok"})
    _ingest(document)
    assert _root_paths(document) == ["tool_errors"]


@pytest.mark.parametrize(
    "extra",
    [{"error": "x"}, {"is_error": False}],
)
def test_no_tool_errors_marker_when_a_result_has_a_key(extra):
    document = _tool_doc({"content": "ok"}, {"content": "ok", "extra": extra})
    _ingest(document)
    assert _root_paths(document) == []


def test_tool_errors_marker_when_the_only_key_is_tool_result_is_error():
    # AQuA does not read the key, so the file shows no evidence that failures
    # are recorded, on the result or on the step.
    document = _tool_doc(
        {"content": "denied", "extra": {"tool_result_is_error": True}}
    )
    document["steps"][2]["extra"] = {"tool_result_is_error": True}
    assert records_tool_errors(document) is False
    _ingest(document)
    assert _root_paths(document) == ["tool_errors"]
    step = document["steps"][2]
    assert step["extra"] == {"tool_result_is_error": True}
    assert step["observation"]["results"][0]["extra"] == {
        "tool_result_is_error": True
    }


def test_tool_errors_marker_on_a_clean_run_of_a_failure_only_producer():
    # No call failed, so no result has a key: the file shows no evidence.
    document = _tool_doc({"content": "ok"})
    document["agent"]["name"] = "kimi-code"
    _ingest(document)
    assert _root_paths(document) == ["tool_errors"]


def test_no_tool_errors_marker_without_results():
    document = _complete()
    _ingest(document)
    assert _root_paths(document) == []


# Markers produced by `find_missing_markers`, one test each


def test_marker_tool_definitions():
    document = _complete()
    del document["agent"]["tool_definitions"]
    _ingest(document)
    assert _root_paths(document) == ["agent.tool_definitions"]


def test_marker_agent_version():
    document = _complete()
    document["agent"]["version"] = ""
    _ingest(document)
    assert _root_paths(document) == ["agent.version"]
    assert aqua_atif.resolve_agent_revision(document) is None


def test_marker_system_prompt():
    document = _complete()
    document["steps"][0]["source"] = "user"
    document["steps"][0]["timestamp"] = "2026-01-01T00:00:00Z"
    _ingest(document)
    assert _root_paths(document) == ["system_prompt"]


def test_marker_system_prompt_after_the_first_agent_step():
    document = _complete()
    document["steps"] = [
        _step(1, "user", timestamp="2026-01-01T00:00:00Z"),
        _step(2, reasoning_content="r", metrics={"prompt_tokens": 1}),
        _step(3, "system"),
    ]
    _ingest(document)
    assert _root_paths(document) == ["system_prompt"]


def test_marker_step_timestamps():
    document = _complete()
    del document["steps"][0]["timestamp"]
    _ingest(document)
    assert _root_paths(document) == ["steps[].timestamp"]


def test_marker_step_model_names():
    document = _complete()
    del document["agent"]["model_name"]
    _ingest(document)
    assert _root_paths(document) == ["steps[].model_name"]
    document = _complete()
    del document["agent"]["model_name"]
    document["steps"][2]["model_name"] = "m"
    _ingest(document)
    assert _root_paths(document) == []


def test_marker_step_reasoning():
    document = _complete()
    del document["steps"][2]["reasoning_content"]
    _ingest(document)
    assert _root_paths(document) == ["steps[].reasoning_content"]


def test_marker_step_metrics():
    document = _complete()
    del document["steps"][2]["metrics"]
    _ingest(document)
    assert _root_paths(document) == ["steps[].metrics"]


def test_steps_without_a_model_call_count_for_no_per_step_marker():
    document = _complete()
    document["agent"].pop("model_name")
    document["steps"][2] = _step(3, llm_call_count=0)
    document["steps"].append(_step(4, llm_call_count=0))
    _ingest(document)
    assert _root_paths(document) == []


def test_marker_final_metrics():
    document = _complete()
    del document["final_metrics"]
    _ingest(document)
    assert _root_paths(document) == ["final_metrics"]


def test_absent_schema_version_emits_no_marker():
    document = _complete()
    del document["schema_version"]
    _ingest(document)
    assert _missing(document) == []
    assert "extra" not in document


def test_marker_continued_trajectory():
    document = _complete(continued_trajectory_ref="trajectory.cont-1.json")
    _ingest(document)
    assert _missing(document) == [
        {"path": "continued_trajectory", "reason": "out_of_scope"}
    ]


def test_marker_tool_call_result():
    document = _complete()
    document["steps"][2] = _call_step(
        3,
        [{"source_call_id": "c1", "extra": {"is_error": False}}],
        call_ids=("c1", "c2"),
        reasoning_content="r",
        metrics={"prompt_tokens": 1},
    )
    _ingest(document)
    calls = document["steps"][2]["tool_calls"]
    assert _missing(calls[0]) == []
    assert _missing(calls[1]) == [{"path": "result", "reason": "not_recorded"}]


def _ref_doc(ref: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Builds a complete document with one subagent reference.

    Args:
        ref: The subagent reference.
        **fields: Root fields to set or replace.

    Returns:
        The document.
    """
    document = _complete(**fields)
    document["steps"][2] = _call_step(
        3,
        [
            {
                "source_call_id": "c1",
                "extra": {"is_error": False},
                "subagent_trajectory_ref": [ref],
            }
        ],
        reasoning_content="r",
        metrics={"prompt_tokens": 1},
    )
    return document


def _ref_markers(document: dict[str, Any]) -> list[dict[str, str]]:
    """Gets the markers of the document's subagent reference.

    Args:
        document: A document built by `_ref_doc`.

    Returns:
        The reference's markers.
    """
    result = document["steps"][2]["observation"]["results"][0]
    return _missing(result["subagent_trajectory_ref"][0])


def test_marker_subagent_trajectory_when_unresolved():
    document = _ref_doc({"trajectory_id": "nowhere"})
    _ingest(document)
    assert _ref_markers(document) == [
        {"path": "trajectory", "reason": "not_recorded"}
    ]


def test_marker_subagent_trajectory_from_the_source_is_kept():
    document = _ref_doc({"trajectory_path": "sub.json"})
    validated = aqua_atif.validate_document(document)
    ref_path = (
        "steps",
        2,
        "observation",
        "results",
        0,
        "subagent_trajectory_ref",
        0,
    )
    status = annotate_document(
        document,
        markers=[
            *validated.markers,
            PendingMissingMarker(
                ref_path, MissingMarker("trajectory", Reason.READ_ERROR, "404")
            ),
        ],
    )
    assert _ref_markers(document) == [
        {"path": "trajectory", "reason": "read_error", "detail": "404"}
    ]
    assert status == IngestStatus.PARTIAL


def test_no_subagent_marker_when_resolved_by_id():
    by_id = _ref_doc(
        {"trajectory_id": "sub"},
        subagent_trajectories=[_complete(trajectory_id="sub")],
    )
    _ingest(by_id)
    assert _ref_markers(by_id) == []


def test_a_path_only_reference_without_id_is_marked_not_recorded():
    by_path = _ref_doc(
        {"trajectory_path": "sub.json"},
        subagent_trajectories=[_complete(trajectory_id="copy")],
    )
    _ingest(by_path)
    assert _ref_markers(by_path) == [
        {"path": "trajectory", "reason": "not_recorded"}
    ]


def test_an_id_reference_resolves_in_an_enclosing_trajectory():
    nested = _ref_doc({"trajectory_id": "helper"}, trajectory_id="sub")
    document = _ref_doc(
        {"trajectory_id": "sub"},
        subagent_trajectories=[_complete(trajectory_id="helper"), nested],
    )
    _ingest(document)
    assert _ref_markers(document["subagent_trajectories"][1]) == []


def test_v1_6_session_id_only_reference_is_marked_once():
    document = _ref_doc({"session_id": "s"}, schema_version="ATIF-v1.6")
    _ingest(document)
    assert _ref_markers(document) == [
        {"path": "trajectory", "reason": "not_recorded"}
    ]


@pytest.mark.parametrize(
    "reason",
    [
        Reason.READ_ERROR,
        Reason.INVALID_CONTENT,
        Reason.PATH_TRAVERSAL,
        Reason.TOO_LARGE,
        Reason.OUT_OF_SCOPE,
    ],
)
def test_marker_part_not_read(reason):
    document = _complete()
    document["steps"][1]["message"] = [
        {"type": "text", "text": "see"},
        {
            "type": "image",
            "source": {"media_type": "image/png", "path": "s3://b/x.png"},
        },
    ]
    aqua_atif.validate_document(document)
    status = annotate_document(
        document,
        markers=[
            PendingMissingMarker(
                ("steps", 1), MissingMarker("message[1]", reason)
            )
        ],
    )
    assert _missing(document["steps"][1]) == [
        {"path": "message[1]", "reason": str(reason)}
    ]
    expected = (
        IngestStatus.INGESTED
        if reason == Reason.OUT_OF_SCOPE
        else IngestStatus.PARTIAL
    )
    assert status == expected
    assert (
        resolve_disabled_check(MissingMarker("message[1]", reason))
        == MEDIA_CHECK
    )


def test_marker_unsupported_version():
    document = _complete(schema_version="ATIF-v1.9")
    document["steps"][2]["new_field"] = 1
    assert _ingest(document) == IngestStatus.INGESTED
    assert _missing(document["steps"][2]) == [
        {
            "path": "new_field",
            "reason": "unsupported_version",
            "detail": "ATIF-v1.9",
        }
    ]
    aqua_atif.Trajectory.model_validate(document)


def test_markers_on_embedded_subagents_are_their_own():
    subagent = _complete(trajectory_id="sub")
    del subagent["final_metrics"]
    document = _complete(subagent_trajectories=[subagent])
    _ingest(document)
    assert _root_paths(document) == []
    assert "extra" not in document
    assert _missing(document["subagent_trajectories"][0]) == [
        {"path": "final_metrics", "reason": "not_recorded"}
    ]


def test_complete_document_gets_no_extra_aqua():
    document = _complete()
    assert _ingest(document) == IngestStatus.INGESTED
    assert "extra" not in document
    assert document["steps"][1] == _step(2, "user")


# Ingest status, merge and the disabled-check map


def test_ingest_status_is_partial_for_a_marker_in_a_subagent():
    subagent = _complete(trajectory_id="sub")
    document = _complete(subagent_trajectories=[subagent])
    status = annotate_document(
        document,
        markers=[
            PendingMissingMarker(
                ("subagent_trajectories", 0, "steps", 1),
                MissingMarker("message[0]", Reason.TOO_LARGE, "70000000"),
            )
        ],
    )
    assert status == IngestStatus.PARTIAL


def test_annotating_again_does_not_duplicate_markers():
    document = _complete()
    del document["final_metrics"]
    _ingest(document)
    assert _missing(document) == [
        {"path": "final_metrics", "reason": "not_recorded"}
    ]
    annotate_document(document)
    assert len(_missing(document)) == 1


def test_disabled_check_map_covers_every_marker_in_the_table():
    expected = {
        "agent.tool_definitions": "unlisted_tool",
        "agent.version": "agent_revision",
        "system_prompt": "instructions",
        "steps[].timestamp": "timing",
        "steps[].model_name": "model",
        "steps[].reasoning_content": "reasoning",
        "steps[].metrics": "tokens",
        "tool_errors": "tool_errors",
        "final_metrics": "totals",
        "continued_trajectory": "completion",
        "result": "failed_call",
        "trajectory": "subagent",
        "content[3]": "media",
    }
    for path, check_id in expected.items():
        check = resolve_disabled_check(MissingMarker(path, Reason.NOT_RECORDED))
        assert check is not None and check.check_id == check_id, path
    check = resolve_disabled_check(
        MissingMarker("steps[].message", Reason.UNSUPPORTED_TYPE)
    )
    assert check is not None and check.check_id == "dropped_content"
    check = resolve_disabled_check(
        MissingMarker("x.y", Reason.UNSUPPORTED_VERSION)
    )
    assert check is not None and check.check_id == "completeness"
    assert (
        resolve_disabled_check(
            MissingMarker("message[0].text", Reason.INVALID_CONTENT)
        )
        is None
    )


def test_list_disabled_checks_names_the_holders_that_switch_each_off():
    document = _complete()
    document["steps"][2]["tool_calls"] = [
        {
            "tool_call_id": "c",
            "function_name": "f",
            "arguments": {},
            "extra": {
                "aqua": {
                    "missing": [{"path": "result", "reason": "not_recorded"}]
                }
            },
        }
    ]
    document["steps"][0]["extra"] = {
        "aqua": {
            "missing": [
                {"path": "message[0]", "reason": "read_error", "detail": "403"}
            ]
        }
    }
    document["steps"][1]["extra"] = {
        "aqua": {
            "missing": [
                {
                    "path": "message[0]",
                    "reason": "read_error",
                    "detail": "404",
                }
            ]
        }
    }
    document["extra"] = {
        "aqua": {
            "missing": [
                {
                    "path": "steps[].timestamp",
                    "reason": "not_recorded",
                }
            ]
        }
    }

    uses = {
        use.check.check_id: use.holders
        for use in aqua_atif.list_disabled_checks(document)
    }

    assert uses["timing"] == ((),)
    assert uses["media"] == (("steps", 0), ("steps", 1))
    assert uses["failed_call"] == (("steps", 2, "tool_calls", 0),)


# extra.losses


def test_losses_become_root_markers():
    document = _complete(
        extra={
            "losses": [
                "per-step token usage unavailable",
                "unsupported content parts dropped",
                "something AQuA does not know",
            ]
        }
    )
    _ingest(document)
    assert _missing(document) == [
        {"path": "steps[].metrics", "reason": "not_recorded"},
        {"path": "steps[].message", "reason": "unsupported_type"},
    ]
    assert (
        resolve_disabled_check(
            MissingMarker("steps[].message", Reason.UNSUPPORTED_TYPE)
        )
        == DROPPED_CONTENT_CHECK
    )
    assert document["extra"]["losses"][2] == "something AQuA does not know"


def test_losses_ignores_values_that_are_not_lists():
    assert losses_to_markers("per-step token usage unavailable") == []
    assert losses_to_markers([1, None]) == []


def test_field_markers_survive_a_second_annotation():
    document = _complete(schema_version="ATIF-v1.9", **{"new-field": 1})
    document["steps"][1]["message"] = [{"type": "text", "text": "\ufffd"}]
    validated = aqua_atif.validate_document(document)
    annotate_document(
        document,
        markers=[
            *validated.markers,
            PendingMissingMarker(
                ("steps", 1),
                MissingMarker("message[0].text", Reason.INVALID_CONTENT),
            ),
        ],
    )
    stored = copy.deepcopy(document)
    annotate_document(document)
    assert {m["path"] for m in _missing(document)} == {
        m["path"] for m in _missing(stored)
    }
    assert {m["path"] for m in _missing(document)} >= {"*"}
    assert [m["path"] for m in _missing(document["steps"][1])] == [
        "message[0].text"
    ]


def test_whitespace_agent_version_is_recorded_and_kept_as_the_revision():
    document = _complete()
    document["agent"]["version"] = "  "
    _ingest(document)
    assert _root_paths(document) == []
    assert aqua_atif.resolve_agent_revision(document) == "  "


def test_llm_call_count_string_zero_counts_as_no_model_call():
    document = _complete()
    document["agent"].pop("model_name")
    document["steps"][2] = _step(3, llm_call_count="0")
    document["steps"].append(_step(4, llm_call_count=0.0))
    assert _ingest(document) == IngestStatus.INGESTED
    assert _root_paths(document) == []
    document = _complete()
    document["steps"][2] = _step(3, llm_call_count="2")
    _ingest(document)
    assert _root_paths(document) == [
        "steps[].reasoning_content",
        "steps[].metrics",
    ]


def test_add_embedded_subagent_indexes_the_copy_and_its_subagents():
    ref_path = ("steps", 0, "observation", "results", 0)
    ref_path = (*ref_path, "subagent_trajectory_ref", 0)
    ref = {"trajectory_id": "b", "trajectory_path": "x.json"}
    document = {"steps": [], "subagent_trajectories": [{"trajectory_id": "a"}]}

    index = aqua_atif.build_subagent_index(document)

    assert aqua_atif.resolve_subagent(index, ref_path, ref) is None
    assert aqua_atif.has_subagent_id(index, (), "a")
    embedded = {
        "trajectory_id": "b",
        "subagent_trajectories": [{"trajectory_id": "nested"}],
    }
    aqua_atif.add_embedded_subagent(
        index, (), ("subagent_trajectories", 1), embedded
    )
    assert aqua_atif.resolve_subagent(index, ref_path, ref) is embedded
    assert aqua_atif.has_subagent_id(
        index, ("subagent_trajectories", 1), "nested"
    )


@pytest.mark.parametrize("value", [123, True, "abc", {"a": 1}])
def test_build_subagent_index_ignores_non_list_subagents(value: Any) -> None:
    document = {"steps": [], "subagent_trajectories": value}
    assert aqua_atif.build_subagent_index(document) == {(): {}}


def test_find_embedded_subagent_resolves_and_handles_missing_target() -> None:
    embedded = {"trajectory_id": "qa", "steps": []}
    ref_path = (
        "steps",
        0,
        "observation",
        "results",
        0,
        "subagent_trajectory_ref",
        0,
    )
    document = {
        "steps": [
            {
                "observation": {
                    "results": [
                        {"subagent_trajectory_ref": [{"trajectory_id": "qa"}]}
                    ]
                }
            }
        ],
        "subagent_trajectories": [embedded],
    }

    assert aqua_atif.find_embedded_subagent(document, ref_path) is embedded
    assert aqua_atif.find_embedded_subagent(document, ("steps", 99)) is None
    assert aqua_atif.find_embedded_subagent(document, ("steps",)) is None


def test_add_markers_drops_marker_for_missing_holder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = _complete()
    with caplog.at_level("WARNING"):
        aqua_atif.add_markers(
            document,
            [
                PendingMissingMarker(
                    ("steps", 99),
                    MissingMarker("message[0]", Reason.READ_ERROR, "404"),
                )
            ],
        )

    assert "Dropped a" in caplog.text
    assert aqua_atif.list_placed_markers(document) == []
