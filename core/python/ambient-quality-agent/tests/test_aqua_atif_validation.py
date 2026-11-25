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

"""Tests for strict parsing and the version rule in `aqua_atif`."""

from __future__ import annotations

import copy
import inspect
import json
import sys
from typing import Any

import pytest
from aqua_atif import (
    DocumentRejectedError,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    bound_text,
    format_json_path,
    get_value,
    list_holders,
    parse_document,
    parse_json_path,
    parsing,
    validate_document,
)
from aqua_atif.markers import find_missing_markers


def _document(**fields: Any) -> dict[str, Any]:
    """Builds a minimal valid document.

    Args:
        **fields: Root fields to set or replace.

    Returns:
        The document.
    """
    document: dict[str, Any] = {
        "schema_version": "ATIF-v1.8",
        "agent": {"name": "a", "version": "1"},
        "steps": [{"step_id": 1, "source": "user", "message": "hi"}],
    }
    document.update(fields)
    return document


def _reject_reason(document: Any) -> str:
    """Validates a document that must be rejected.

    Args:
        document: The parsed document.

    Returns:
        The rejection reason.
    """
    with pytest.raises(DocumentRejectedError) as raised:
        validate_document(document)
    return raised.value.reason


# Parsing


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b"\xff\xfe\xfa",
    ],
)
def test_parse_rejects_invalid_json(raw):
    with pytest.raises(DocumentRejectedError) as raised:
        parse_document(raw)
    assert raised.value.reason == "invalid JSON"


def test_parse_accepts_256_levels_and_rejects_257():
    def nested(depth: int) -> str:
        return '{"a": ' * (depth - 1) + "{}" + "}" * (depth - 1)

    assert parse_document(nested(256)).value is not None
    with pytest.raises(DocumentRejectedError, match="invalid JSON"):
        parse_document(nested(257))
    with pytest.raises(DocumentRejectedError, match="invalid JSON"):
        parse_document("[" * 5000 + "]" * 5000)


def test_replacing_surrogates_in_256_levels_needs_no_deep_recursion():
    """The walk over the parsed value uses its own stack.

    The value is built here, not parsed: on Python 3.11 `json` itself counts
    each nesting level against the recursion limit.
    """
    nested: Any = "x\ud800"
    for _ in range(255):
        nested = {"a": nested}
    replaced: list[Any] = []
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 120)
    try:
        parsing._sanitize(nested, replaced)
    finally:
        sys.setrecursionlimit(limit)
    assert replaced == [("a",) * 255]


def test_parse_replaces_lone_surrogates_and_marks_the_field():
    document = _document()
    document["steps"][0]["message"] = [
        {"type": "text", "text": "ok"},
        {"type": "text", "text": "bad PLACEHOLDER"},
    ]
    document["steps"][0]["extra"] = {"note": "PLACEHOLDER"}
    raw = json.dumps(document).replace("PLACEHOLDER", "\\ud800")

    parsed = parse_document(raw.encode())

    step = parsed.value["steps"][0]
    assert step["message"][1]["text"] == "bad \ufffd"
    assert step["extra"]["note"] == "\ufffd"
    assert [marker.marker.path for marker in parsed.markers] == [
        "message[1].text",
        "extra.note",
    ]
    assert parsed.markers == (
        PendingMissingMarker(
            ("steps", 0),
            MissingMarker(
                "message[1].text", Reason.INVALID_CONTENT, "lone_surrogate"
            ),
        ),
        PendingMissingMarker(
            ("steps", 0),
            MissingMarker(
                "extra.note", Reason.INVALID_CONTENT, "lone_surrogate"
            ),
        ),
    )
    json.dumps(parsed.value).encode("utf-8")


def test_parse_keeps_surrogate_markers_in_document_order():
    raw = '{"a": "x\\ud800", "nested": {"k": "y\\ud800"}, "b": ["z\\ud800", "w\\ud800"]}'
    parsed = parse_document(raw)
    assert [marker.marker.path for marker in parsed.markers] == [
        "a",
        "nested.k",
        "b[0]",
        "b[1]",
    ]


def test_parse_keeps_valid_surrogate_pairs():
    parsed = parse_document('{"a": "\\ud83d\\ude00"}')
    assert parsed.value == {"a": "\U0001f600"}
    assert parsed.markers == ()


def test_parse_replaces_surrogates_in_keys():
    parsed = parse_document(
        '{"agent": {"name": "a", "version": "1", "extra": {"k\\udc00": 1}}}'
    )
    assert parsed.value["agent"]["extra"] == {"k\ufffd": 1}
    assert parsed.markers == (
        PendingMissingMarker(
            ("agent",),
            MissingMarker("extra.*", Reason.INVALID_CONTENT, "lone_surrogate"),
        ),
    )


# The version rule, one test per row


@pytest.mark.parametrize("minor", range(9))
def test_v1_0_to_v1_8_validate_strictly(minor):
    validated = validate_document(_document(schema_version=f"ATIF-v1.{minor}"))
    assert str(validated.schema_version) == f"ATIF-v1.{minor}"
    assert validated.schema_version.is_strict
    assert validated.markers == ()


def test_unknown_field_in_v1_8_rejects_with_the_pydantic_style_reason():
    document = _document()
    document["steps"] = [
        {"step_id": 1, "source": "user", "message": "a"},
        {"step_id": 2, "source": "user", "message": "b"},
        {"step_id": 3, "source": "agent", "message": "c", "tool_call": []},
    ]
    assert (
        _reject_reason(document)
        == "steps.2.tool_call: Extra inputs are not permitted"
    )


def test_model_failure_in_v1_8_rejects_with_a_reason():
    document = _document()
    document["steps"][0]["source"] = "tool"
    assert _reject_reason(document).startswith("steps.0.source: ")


def test_unknown_part_type_in_v1_8_rejects():
    document = _document()
    document["steps"][0]["message"] = [
        {"type": "video", "source": {"uri": "x"}}
    ]
    assert _reject_reason(document) == (
        "steps.0.message.0.type: Input should be 'text', 'image' or 'audio'"
    )


def test_unknown_media_type_in_v1_8_rejects():
    document = _document()
    document["steps"][0]["message"] = [
        {"type": "image", "source": {"media_type": "image/bmp", "path": "a"}}
    ]
    assert _reject_reason(document).startswith(
        "steps.0.message.0.source.media_type: Input should be 'image/jpeg'"
    )


def test_bad_part_in_a_list_message_names_the_part():
    document = _document()
    document["steps"][0]["message"] = [{"type": "text"}]
    assert _reject_reason(document).startswith("steps.0.message.0: ")


def test_v1_9_keeps_and_marks_unknown_fields():
    document = _document(schema_version="ATIF-v1.9", new_root={"x": 1})
    document["steps"] = [
        {"step_id": 1, "source": "user", "message": "a"},
        {"step_id": 2, "source": "user", "message": "b"},
        {
            "step_id": 3,
            "source": "agent",
            "message": "c",
            "new_field": {"y": 2},
        },
    ]
    original = copy.deepcopy(document)

    validated = validate_document(document)

    assert document == original
    assert not validated.schema_version.is_strict
    assert set(validated.markers) == {
        PendingMissingMarker(
            (),
            MissingMarker(
                "new_root", Reason.UNSUPPORTED_VERSION, detail="ATIF-v1.9"
            ),
        ),
        PendingMissingMarker(
            ("steps", 2),
            MissingMarker(
                "new_field", Reason.UNSUPPORTED_VERSION, detail="ATIF-v1.9"
            ),
        ),
    }


def test_v1_9_marks_unknown_parts_and_nested_fields_on_the_holder():
    document = _document(schema_version="ATIF-v1.12")
    document["steps"][0]["message"] = [
        {"type": "text", "text": "a", "lang": "en"},
        {"type": "video", "source": {"uri": "x"}},
        {
            "type": "image",
            "source": {"media_type": "image/png", "path": "p", "w": 3},
        },
    ]
    document["steps"][0]["observation"] = {"results": [], "summary": "s"}

    markers = {
        placed.marker.path for placed in validate_document(document).markers
    }

    assert markers == {
        "message[0].lang",
        "message[1]",
        "message[2].source.w",
        "observation.summary",
    }


def test_v2_rejects_with_upgrade_reason():
    assert _reject_reason(_document(schema_version="ATIF-v2.0")) == (
        "ATIF-v2.0 is a newer major version than AQuA reads; upgrade AQuA"
    )
    assert _reject_reason(_document(schema_version="3.1")) == (
        "ATIF-v3.1 is a newer major version than AQuA reads; upgrade AQuA"
    )


def test_bare_version_reads_as_atif_version():
    validated = validate_document(_document(schema_version="1.7"))
    assert str(validated.schema_version) == "ATIF-v1.7"
    assert validated.schema_version.is_strict
    with pytest.raises(DocumentRejectedError, match="Extra inputs"):
        validate_document(_document(schema_version="1.7", unknown=1))


@pytest.mark.parametrize(
    "version",
    ["ATIF-v1", "v1.7", "ATIF-1.7", "ATIF-v01.7", "0.9", "", 1.7, ["x"]],
)
def test_other_values_reject_as_unknown_schema_version(version):
    assert _reject_reason(_document(schema_version=version)) == (
        "unknown schema_version"
    )


@pytest.mark.parametrize("absent", ["missing", "null"])
def test_no_version_with_steps_reads_as_v1_8(absent):
    document = _document()
    if absent == "missing":
        del document["schema_version"]
    else:
        document["schema_version"] = None

    validated = validate_document(document)

    assert str(validated.schema_version) == "ATIF-v1.8"
    assert validated.trajectory.schema_version == "ATIF-v1.8"
    assert all(
        item.marker.path != "schema_version"
        for item in find_missing_markers(document)
    )


def test_no_version_without_steps_is_not_atif():
    assert _reject_reason({"agent": {"name": "a"}}) == "not an ATIF document"
    assert _reject_reason([1, 2]) == "not an ATIF document"


def test_embedded_subagent_follows_its_own_version():
    subagent = _document(
        schema_version="ATIF-v1.9", trajectory_id="sub", extra2=1
    )
    document = _document(subagent_trajectories=[subagent])
    validated = validate_document(document)
    assert validated.markers == (
        PendingMissingMarker(
            ("subagent_trajectories", 0),
            MissingMarker(
                "extra2", Reason.UNSUPPORTED_VERSION, detail="ATIF-v1.9"
            ),
        ),
    )
    subagent["schema_version"] = "ATIF-v2.0"
    assert _reject_reason(document).startswith(
        "subagent_trajectories.0: ATIF-v2.0 is a newer major version"
    )


def test_v1_6_session_id_only_reference_is_kept():
    document = _document(schema_version="ATIF-v1.6")
    document["steps"] = [
        {
            "step_id": 1,
            "source": "agent",
            "message": "delegating",
            "tool_calls": [
                {"tool_call_id": "c1", "function_name": "task", "arguments": {}}
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c1",
                        "subagent_trajectory_ref": [{"session_id": "s-sub"}],
                    }
                ]
            },
        }
    ]
    validated = validate_document(document)
    assert validated.markers == ()
    ref = validated.trajectory.steps[0].observation.results[0]
    assert ref.subagent_trajectory_ref[0].session_id == "s-sub"


def test_extra_outside_aqua_is_never_checked():
    document = _document(extra={"anything": [1], "free": {"x": 1}})
    document["steps"][0]["extra"] = {"aquatic": True}
    assert validate_document(document).markers == ()


def _document_with_every_holder() -> dict[str, Any]:
    """Builds a document with one object of each kind that can hold `extra`.

    Returns:
        The document, with an embedded subagent that a reference resolves to.
    """
    subagent = _document(trajectory_id="sub")
    return _document(
        steps=[
            {"step_id": 1, "source": "user", "message": "hi"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "m",
                "tool_calls": [
                    {"tool_call_id": "c", "function_name": "f", "arguments": {}}
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "c",
                            "content": "ok",
                            "subagent_trajectory_ref": [
                                {"trajectory_id": "sub"}
                            ],
                        }
                    ]
                },
                "metrics": {"prompt_tokens": 1},
            },
        ],
        final_metrics={"total_steps": 2},
        subagent_trajectories=[subagent],
    )


_RESULT = ("steps", 1, "observation", "results", 0)


@pytest.mark.parametrize(
    ("holder", "reason"),
    [
        (("agent",), "agent.extra.aqua"),
        (
            (*_RESULT, "subagent_trajectory_ref", 0),
            "steps.1.observation.results.0.subagent_trajectory_ref.0"
            ".extra.aqua",
        ),
        (("steps", 1, "metrics"), "steps.1.metrics.extra.aqua"),
        (("final_metrics",), "final_metrics.extra.aqua"),
    ],
)
@pytest.mark.parametrize(
    "aqua",
    [{"missing": ["message"]}, {}, None],
    ids=["markers", "empty", "null"],
)
def test_an_extra_aqua_key_rejects_on_disallowed_holders(holder, reason, aqua):
    document = _document_with_every_holder()
    get_value(document, holder)["extra"] = {"aqua": aqua, "free": 1}
    assert _reject_reason(document) == (
        f"{reason}: only AQuA writes extra.aqua"
    )


def test_a_document_without_extra_aqua_validates():
    document = _document_with_every_holder()
    for holder in list_holders(document):
        holder.value["extra"] = {"free": 1}
    assert validate_document(document).markers == ()


def test_an_invalid_extra_aqua_key_rejects_a_document_newer_than_v1_8():
    document = _document(schema_version="ATIF-v1.9", extra={"aqua": {}})
    assert _reject_reason(document) == (
        "extra.aqua: extra.aqua must not be empty"
    )


def test_embedded_from_on_a_subagent_rejects_on_input():
    document = _document_with_every_holder()
    document["subagent_trajectories"][0]["extra"] = {
        "aqua": {"embedded_from": "sub.json"}
    }
    assert _reject_reason(document) == (
        "subagent_trajectories.0.extra.aqua.embedded_from: "
        "Extra inputs are not permitted"
    )


@pytest.mark.parametrize(
    "version",
    ["ATIF-v1." + "9" * 5000, "ATIF-v" + "9" * 5000 + ".0", "1." + "9" * 10],
    ids=["long-minor", "long-major", "ten-digits"],
)
def test_overlong_version_numbers_reject_as_unknown_schema_version(version):
    assert _reject_reason(_document(schema_version=version)) == (
        "unknown schema_version"
    )
    subagent = _document(schema_version=version, trajectory_id="s")
    assert _reject_reason(_document(subagent_trajectories=[subagent])) == (
        "subagent_trajectories.0: unknown schema_version"
    )


@pytest.mark.parametrize("number", ["1e999", "-1e999", "1E400"])
def test_parse_rejects_numbers_that_overflow_to_infinity(number):
    with pytest.raises(DocumentRejectedError) as raised:
        parse_document('{"final_metrics": {"total_cost_usd": ' + number + "}}")
    assert raised.value.reason == "invalid JSON"
    assert parse_document('{"a": 1e308}').value == {"a": 1e308}


@pytest.mark.parametrize("key", ["new-field", "x y", "1abc"])
def test_v1_9_unknown_field_with_an_unwritable_name_is_marked(key):
    document = _document(schema_version="ATIF-v1.9", **{key: 1})
    document["steps"][0]["observation"] = {"results": [], key: 2}
    document["steps"][0]["message"] = [{"type": "text", "text": "a", key: 3}]

    markers = validate_document(document).markers

    assert {(placed.holder, placed.marker.path) for placed in markers} == {
        ((), "*"),
        (("steps", 0), "observation.*"),
        (("steps", 0), "message[0].*"),
    }


def test_surrogate_under_an_unwritable_key_is_marked():
    parsed = parse_document('{"x-y": "a\\ud800b", "agent": {"n\\ud800": 1}}')
    assert {
        (placed.holder, placed.marker.path) for placed in parsed.markers
    } == {
        ((), "*"),
        (("agent",), "*"),
    }


def test_surrogate_keys_that_collide_are_marked():
    parsed = parse_document('{"extra": {"k\\udc00": 1, "k\\udc01": 2}}')
    assert parsed.value["extra"] == {"k\ufffd": 2}
    assert [placed.marker.path for placed in parsed.markers] == ["extra.*"]


def test_bad_media_source_reason_names_the_source():
    document = _document()
    document["steps"][0]["message"] = [
        {"type": "image", "source": {"path": "a"}}
    ]
    assert _reject_reason(document) == (
        "steps.0.message.0.source.media_type: Field required"
    )


def test_null_steps_without_version_is_not_atif():
    assert _reject_reason(
        {"agent": {"name": "a", "version": "1"}, "steps": None}
    ) == ("not an ATIF document")


def test_values_are_coerced_as_harbor_does():
    document = _document()
    document["steps"] = [
        {"step_id": "1", "source": "user", "message": "m"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "m",
            "llm_call_count": "0",
        },
    ]
    validated = validate_document(document)
    assert validated.trajectory.steps[0].step_id == 1
    assert validated.trajectory.steps[1].llm_call_count == 0
    assert document["steps"][0]["step_id"] == "1"


def test_rejection_reason_is_capped_and_printable():
    document = _document()
    document["steps"][0]["k" * 100_000 + "\n\u00e9"] = 1
    reason = _reject_reason(document)
    assert len(reason) == 300
    assert reason.endswith("...")
    assert reason.isascii()
    assert reason.isprintable()


def test_reason_drops_union_branch_names_and_pydantic_prefixes():
    document = _document()
    document["steps"][0]["message"] = {"not": "a string"}
    assert not _reject_reason(document).startswith("steps.0.message.str")
    document = _document()
    document["steps"][0]["step_id"] = 7
    reason = _reject_reason(document)
    assert not reason.startswith("Value error")
    assert "step_id" in reason


@pytest.mark.parametrize(
    ("path", "formatted"),
    [
        ((), ""),
        (("steps", 0, "message", 1), "steps[0].message[1]"),
        (("extra", "x-request-id", 0), 'extra["x-request-id"][0]'),
        (("a b", "c.d", 12, "ok_1"), '["a b"]["c.d"][12].ok_1'),
        ((0, "key"), "[0].key"),
    ],
)
def test_json_path_round_trips(
    path: tuple[str | int, ...], formatted: str
) -> None:
    assert format_json_path(path) == formatted
    assert parse_json_path(formatted) == path


@pytest.mark.parametrize(
    "bad",
    [
        '["unclosed"',
        '["a"extra]',
        "[01]",
        "[-1]",
        "[abc]",
        ".steps",
        "steps.",
        "steps[0]extra",
        "steps..a",
    ],
)
def test_parse_json_path_rejects_malformed_paths(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_json_path(bad)


def test_bound_text_replaces_unprintable_and_truncates() -> None:
    assert bound_text("ok-123", limit=10) == "ok-123"
    assert bound_text("a\nb\tc\u00e9", limit=10) == "a?b?c?"
    assert bound_text("abcdefghij", limit=7) == "abcd..."
