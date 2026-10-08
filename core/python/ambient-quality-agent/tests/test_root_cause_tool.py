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

"""Tests for record_root_cause validation, snapshot anchoring, and persistence.

Uses in-memory fakes for GCS and BigQuery to verify range checks, snapshot
anchoring of the 'before' text, and all-or-nothing save semantics.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from ambient_quality_agent.core import root_cause_tools
from ambient_quality_agent.tools.insights.models import (
    InsightOccurrence,
    OccurrenceState,
    ProposedEdit,
    RootCause,
)
from ambient_quality_agent.tools.orchestrator import insight_tools
from ambient_quality_agent.tools.source_code import reader as reader_module
from ambient_quality_agent.tools.source_code import snapshot

from .conftest import InMemoryRootCauseStore, StateContext
from .test_source_code_reader import (
    BUCKET,
    FakeStorageClient,
    build_reader,
    publish_snapshot,
)

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

REVISION = "10"
AGENT_PY = "\n".join(f"line {n}" for n in range(1, 6)) + "\n"
"""Five sample lines used to test valid ranges and past-end boundary conditions."""

IGNORE_PATTERNS = ["terraform/"]


class _FakeReader:
    """In-memory test fake for InsightReader returning canned occurrences and root causes."""

    def __init__(
        self,
        *,
        occurrences: list[InsightOccurrence] | None = None,
        root_causes: list[RootCause] | None = None,
    ) -> None:
        self.occurrences = occurrences or []
        self.root_causes = root_causes or []

    def list_occurrences(
        self,
        *,
        occurrence_id: str | None = None,
        limit: int,
        offset: int,
        **_: Any,
    ) -> tuple[list[InsightOccurrence], int]:
        found = [
            o for o in self.occurrences if o.occurrence_id == occurrence_id
        ]
        return found[offset : offset + limit], len(found)

    def list_root_causes(self, insight_id: str, **_: Any) -> list[RootCause]:
        return [r for r in self.root_causes if r.insight_id == insight_id]


def _occurrence(
    occurrence_id: str = "occ-1",
    insight_id: str = "ins-1",
    agent_revision: str = REVISION,
) -> InsightOccurrence:
    return InsightOccurrence(
        occurrence_id=occurrence_id,
        insight_id=insight_id,
        occurrence_state=OccurrenceState.TRACKED,
        run_id="run-9",
        created_at=_NOW,
        agent_name="agent",
        agent_revision=agent_revision,
        label="invented an expense category",
    )


@pytest.fixture(autouse=True)
def _isolated_manifest_cache() -> Iterator[None]:
    """Clears the manifest cache before and after each test to isolate snapshot state."""
    reader_module.clear_manifest_cache()
    yield
    reader_module.clear_manifest_cache()


@pytest.fixture
def gcs() -> FakeStorageClient:
    """Provides a mock GCS client pre-populated with a 5-line sample file at revision 10."""
    fake = FakeStorageClient()
    publish_snapshot(
        fake,
        REVISION,
        {"app/agent.py": AGENT_PY},
        ignore_patterns=IGNORE_PATTERNS,
    )
    return fake


@pytest.fixture(autouse=True)
def wired(
    gcs: FakeStorageClient,
    root_cause_store: InMemoryRootCauseStore,
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeReader:
    """Wires snapshot, insight reader, and store seams to in-memory test fakes.

    Args:
        gcs: Mock GCS storage client.
        root_cause_store: In-memory root cause store.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Configured fake insight reader.
    """
    reader = _FakeReader(occurrences=[_occurrence()])
    monkeypatch.setattr(
        reader_module,
        "reader_factory",
        lambda state: build_reader(gcs),
    )
    monkeypatch.setattr(insight_tools, "reader_factory", lambda state: reader)
    monkeypatch.setattr(
        root_cause_tools, "store_factory", lambda state: root_cause_store
    )
    return reader


def record(**kwargs: Any) -> dict[str, Any]:
    """Invokes record_root_cause with default test arguments.

    Args:
        **kwargs: Overrides for record_root_cause parameters.

    Returns:
        Result dictionary returned by the tool.
    """
    call: dict[str, Any] = {
        "insight_id": "ins-1",
        "occurrence_id": "occ-1",
        "revision": REVISION,
        "summary": "The tool takes a free-form category, so the model invents one.",
        "edits": [],
    }
    call.update(kwargs)
    return asyncio.run(
        root_cause_tools.record_root_cause(StateContext(), **call)
    )


def edit(
    start_line: int = 2,
    end_line: int = 3,
    path: str = "app/agent.py",
    **kwargs: Any,
) -> ProposedEdit:
    """Constructs a ProposedEdit with default test values.

    Args:
        start_line: 1-based inclusive start line number.
        end_line: 1-based inclusive end line number.
        path: Repository-relative target file path.
        **kwargs: Additional ProposedEdit field overrides.

    Returns:
        Configured ProposedEdit instance.
    """
    return ProposedEdit(
        path=path,
        start_line=start_line,
        end_line=end_line,
        after="replacement\n",
        rationale="why",
        **kwargs,
    )


def _reasons(result: dict[str, Any]) -> str:
    return " ".join(r["reason"] for r in result["rejected"])


# --------------------------------------------------------------------------- #
# The occurrence the record is written against                                 #
# --------------------------------------------------------------------------- #


def test_a_hallucinated_occurrence_is_refused(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies that non-existent occurrence IDs are rejected with an error."""
    result = record(occurrence_id="occ-does-not-exist")

    assert "occ-does-not-exist" in result["error"]
    assert root_cause_store.saved == []


def test_an_occurrence_belonging_to_another_insight_is_refused(
    wired: _FakeReader, root_cause_store: InMemoryRootCauseStore
) -> None:
    wired.occurrences = [_occurrence(insight_id="ins-other")]

    result = record()

    assert "ins-other" in result["error"]
    assert root_cause_store.saved == []


def test_a_summary_is_required(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    result = record(summary="   ")

    assert "summary" in result["error"]
    assert root_cause_store.saved == []


# --------------------------------------------------------------------------- #
# Absence: the three ways a path can fail to resolve                           #
# --------------------------------------------------------------------------- #


def test_a_path_excluded_by_an_ignore_pattern_says_so() -> None:
    """Verifies that paths ignored during snapshot creation report exclusion rather than missing files."""
    result = record(edits=[edit(path="terraform/main.tf")])

    assert result["recorded"] is False
    assert "excluded by pattern `terraform/`" in _reasons(result)


def test_a_path_that_does_not_exist_at_this_revision_says_so() -> None:
    result = record(edits=[edit(path="app/imaginary.py")])

    assert result["recorded"] is False
    assert "no such path" in _reasons(result)


def test_a_published_path_whose_body_is_missing_says_so(
    gcs: FakeStorageClient,
) -> None:
    """Verifies rejection when a file listed in the manifest has missing object data in storage."""
    del gcs.contents[
        BUCKET, snapshot.build_file_object_name(REVISION, "app/agent.py")
    ]

    result = record(edits=[edit()])

    assert result["recorded"] is False
    assert "body is missing from the snapshot" in _reasons(result)


def test_a_revision_with_no_snapshot_refuses_every_edit(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies that all edits are rejected if the snapshot revision cannot be resolved."""
    result = record(revision="does-not-exist", edits=[edit()])

    assert result["recorded"] is False
    assert "no complete source snapshot" in _reasons(result)
    assert root_cause_store.saved == []


# --------------------------------------------------------------------------- #
# Range validation                                                             #
# --------------------------------------------------------------------------- #


def test_a_range_past_the_end_of_the_file_is_refused() -> None:
    """Verifies rejection when requested line numbers exceed the file's line count."""
    result = record(edits=[edit(start_line=4, end_line=9)])

    assert result["recorded"] is False
    assert "run past the end of app/agent.py, which has 5 lines" in _reasons(
        result
    )


@pytest.mark.parametrize(
    ("start_line", "end_line", "expected"),
    [(0, 2, "start_line must be 1 or greater"), (4, 2, "is before start_line")],
    ids=["zero-start", "inverted"],
)
def test_an_impossible_range_is_refused_before_any_read(
    start_line: int, end_line: int, expected: str, gcs: FakeStorageClient
) -> None:
    """Verifies that non-positive start lines and inverted ranges are rejected before storage reads."""
    result = record(edits=[edit(start_line=start_line, end_line=end_line)])

    assert result["recorded"] is False
    assert expected in _reasons(result)


def test_two_edits_to_the_same_lines_are_refused() -> None:
    """Verifies rejection when multiple edits overlap the same line range in a file."""
    result = record(edits=[edit(1, 3), edit(2, 4)])

    assert result["recorded"] is False
    assert "overlaps the edit to app/agent.py at lines 1-3" in _reasons(result)


def test_edits_to_the_same_file_that_do_not_touch_are_kept(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    result = record(edits=[edit(1, 2), edit(3, 4)])

    assert result["recorded"] is True
    assert len(root_cause_store.saved[0].edits) == 2


def test_two_spellings_of_one_path_still_collide(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """A leading slash must not smuggle a second edit over the same lines."""
    result = record(edits=[edit(1, 3), edit(2, 4, path="/app/agent.py")])

    assert result["recorded"] is False
    assert "overlaps the edit to app/agent.py at lines 1-3" in _reasons(result)
    assert root_cause_store.saved == []


def test_a_stored_edit_carries_the_normalized_path(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """A coding harness anchors against HEAD, so the persisted path is canonical."""
    result = record(edits=[edit(1, 2, path="/app/agent.py")])

    assert result["recorded"] is True
    assert root_cause_store.saved[0].edits[0].path == "app/agent.py"


# --------------------------------------------------------------------------- #
# All or nothing, with anchored survivors                                      #
# --------------------------------------------------------------------------- #


def test_one_bad_edit_stores_none_of_them(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies all-or-nothing save semantics: any invalid edit rejects the entire record."""
    result = record(edits=[edit(1, 2), edit(path="app/imaginary.py")])

    assert result["recorded"] is False
    assert result["record"] is None
    assert root_cause_store.saved == []


def test_the_edits_that_did_resolve_come_back_anchored() -> None:
    """Verifies that valid edits return anchored 'before' text even when other edits fail."""
    result = record(edits=[edit(1, 2), edit(path="app/imaginary.py")])

    assert [(e["start_line"], e["end_line"]) for e in result["anchored"]] == [
        (1, 2)
    ]
    assert result["anchored"][0]["before"] == "line 1\nline 2"


# --------------------------------------------------------------------------- #
# Anchoring                                                                    #
# --------------------------------------------------------------------------- #


def test_the_server_overwrites_whatever_before_the_model_sent(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies that the server populates 'before' text from the snapshot, overriding caller input."""
    result = record(edits=[edit(2, 3, before="     2: line 2\n     3: line 3")])

    assert root_cause_store.saved[0].edits[0].before == "line 2\nline 3"
    assert result["record"]["edits"][0]["before"] == "line 2\nline 3"


def test_the_stored_record_comes_back_whole(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies that successful calls return the complete persisted RootCause payload."""
    result = record(edits=[edit()])

    saved = root_cause_store.saved[0]
    assert result["recorded"] is True
    assert result["root_cause_id"] == saved.root_cause_id
    assert result["edits_recorded"] == 1
    assert result["record"]["summary"] == saved.summary
    assert result["record"]["agent_revision"] == REVISION
    assert result["record"]["edits"][0]["before"] == "line 2\nline 3"


def test_whole_float_line_numbers_are_stored_and_echoed_as_integers(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies that float line numbers in tool input are coerced and returned as integers."""
    result = record(
        edits=[
            {
                "path": "app/agent.py",
                "start_line": 2.0,
                "end_line": 3.0,
                "after": "replacement\n",
                "rationale": "why",
            }
        ]
    )

    stored = root_cause_store.saved[0].edits[0]
    assert (type(stored.start_line), type(stored.end_line)) == (int, int)
    echoed = result["record"]["edits"][0]
    assert (echoed["start_line"], echoed["end_line"]) == (2, 3)
    assert type(result["edits_recorded"]) is int


def test_a_diagnosis_with_no_code_fix_is_recorded(
    root_cause_store: InMemoryRootCauseStore,
) -> None:
    """Verifies that diagnoses with an empty edit list are successfully recorded."""
    result = record(edits=[])

    assert result["recorded"] is True
    assert result["edits_recorded"] == 0
    assert root_cause_store.saved[0].edits == []


# --------------------------------------------------------------------------- #
# Warnings                                                                     #
# --------------------------------------------------------------------------- #


def test_an_edit_the_previous_record_had_and_this_one_drops_is_flagged(
    wired: _FakeReader, root_cause_store: InMemoryRootCauseStore
) -> None:
    """Verifies that dropping an edit present in a prior record generates an advisory warning."""
    wired.root_causes = [
        RootCause(
            root_cause_id="rc-1",
            insight_id="ins-1",
            occurrence_id="occ-1",
            agent_revision=REVISION,
            summary="earlier",
            edits=[edit(1, 2), edit(4, 5)],
            created_at=_NOW,
        )
    ]

    result = record(edits=[edit(1, 2)])

    assert result["recorded"] is True
    assert result["warnings"] == [
        "app/agent.py:4-5 was in the previous record for this occurrence and is "
        "not in this one."
    ]


def test_a_previous_record_for_another_occurrence_is_not_a_shrink(
    wired: _FakeReader,
) -> None:
    """Verifies that prior records on different occurrences do not trigger withdrawal warnings."""
    wired.root_causes = [
        RootCause(
            root_cause_id="rc-1",
            insight_id="ins-1",
            occurrence_id="occ-other",
            agent_revision=REVISION,
            summary="earlier",
            edits=[edit(4, 5)],
            created_at=_NOW,
        )
    ]

    result = record(edits=[edit(1, 2)])

    assert result["warnings"] == []


def test_a_revision_other_than_the_sightings_is_flagged_not_refused(
    wired: _FakeReader,
) -> None:
    """Verifies that anchoring against a different revision emits an advisory warning without failing."""
    assert record(edits=[])["warnings"] == []
    wired.occurrences = [_occurrence(agent_revision="8")]

    result = record(revision=REVISION, edits=[])

    assert result["recorded"] is True
    assert "was seen on '8'" in result["warnings"][0]


@pytest.mark.parametrize(
    ("revision", "agent_revision"),
    [("", REVISION), (REVISION, "")],
    ids=["tool", "sighting"],
)
def test_an_empty_revision_on_either_side_is_not_a_mismatch(
    revision: str, agent_revision: str, wired: _FakeReader
) -> None:
    """Verifies that missing revision metadata on either side does not trigger a mismatch warning."""
    wired.occurrences = [_occurrence(agent_revision=agent_revision)]

    result = record(revision=revision, edits=[])

    assert result["recorded"] is True
    assert result["warnings"] == []


# --------------------------------------------------------------------------- #
# Backend failures reach the model as an error, not as an exception            #
# --------------------------------------------------------------------------- #


def test_a_failing_occurrence_read_is_reported_not_raised(
    wired: _FakeReader,
    root_cause_store: InMemoryRootCauseStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that occurrence read failures return an error dictionary instead of raising an exception."""

    def _boom(**_: Any) -> tuple[list[InsightOccurrence], int]:
        raise RuntimeError("dataset is gone")

    monkeypatch.setattr(wired, "list_occurrences", _boom)

    result = record(edits=[])

    assert "dataset is gone" in result["error"]
    assert root_cause_store.saved == []


def test_a_failing_save_is_reported_as_unrecorded(
    root_cause_store: InMemoryRootCauseStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that storage save failures return an error dictionary instead of raising an exception."""

    def _boom(record: RootCause) -> None:
        raise RuntimeError("load job rejected")

    monkeypatch.setattr(root_cause_store, "save", _boom)

    result = record(edits=[edit()])

    assert "load job rejected" in result["error"]
    assert "recorded" not in result
