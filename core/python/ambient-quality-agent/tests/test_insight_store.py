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

"""Tests for `InsightStore` (offline, no BigQuery).

Patches ``bigquery.Client`` and asserts on the SQL each method emits and the
parameters it binds. The same-issue judge `find_existing_insights` calls is
covered in `test_insight_matching.py`, and faked here, so these tests stay about
the statements.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.insights import matching
from ambient_quality_agent.tools.insights.bigquery_store import (
    BigQueryInsightStore,
)
from ambient_quality_agent.tools.insights.clustering import Cluster
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
    RubricExample,
)

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"

_INSIGHTS_REF = f"{_PROJECT}.{_DATASET}.insights"
_OCCURRENCES_REF = f"{_PROJECT}.{_DATASET}.insight_occurrences"

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _store(client: mock.MagicMock) -> BigQueryInsightStore:
    return BigQueryInsightStore(
        client=client,
        project_id=_PROJECT,
        dataset=_DATASET,
        agent_name=_AGENT,
    )


def _params(call: Any) -> dict[str, Any]:
    """Extracts bound parameter names and values from a query call.

    Scalars expose ``.value``; arrays and structs expose ``.values``/sub-params.

    Args:
        call: Mock query call object.

    Returns:
        Mapping of parameter names to their bound values.
    """
    job_config = call.kwargs["job_config"]
    return {
        p.name: getattr(p, "value", None) if hasattr(p, "value") else p.values
        for p in job_config.query_parameters
    }


def _sql(call: Any) -> str:
    return call.args[0]


def _cluster(label: str) -> Cluster:
    return Cluster(label=label, finding_ids=[1], item_count=1)


def _insight(insight_id: str = "ins-1") -> Insight:
    return Insight(
        insight_id=insight_id,
        agent_name=_AGENT,
        label="made no tool call",
        status=InsightStatus.NEW,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _occurrence(insight_id: str = "ins-1") -> InsightOccurrence:
    return InsightOccurrence(
        occurrence_id="occ-1",
        insight_id=insight_id,
        occurrence_state=OccurrenceState.TRACKED,
        run_id="run-1",
        created_at=_NOW,
        agent_name=_AGENT,
        label="made no tool call",
        item_count=1,
        rubrics=[
            RubricExample(
                rubric=Finding(
                    expected_behavior="call create_ticket",
                    actual_behavior="no tool call was made",
                    session_id="case-1",
                ),
                eval_case_id="case-1",
            )
        ],
    )


# --- construction -------------------------------------------------------------


def test_uses_injected_client() -> None:
    client = mock.MagicMock()
    store = _store(client)

    store.delete_failed_insights("run-1")

    # The store runs against the caller-provided client, not one it built.
    client.query.assert_called()


# --- delete_failed_insights --------------------------------------------------


def test_cleanup_failed_insights_deletes_occurrences_then_orphans_in_order() -> (
    None
):
    client = mock.MagicMock()
    store = _store(client)

    store.delete_failed_insights("run-1")

    first, second = client.query.call_args_list
    # Occurrences are deleted before the orphan check reads the survivors.
    assert f"DELETE FROM `{_OCCURRENCES_REF}`" in _sql(first)  # noqa: S608 - trusted test constants
    assert f"DELETE FROM `{_INSIGHTS_REF}`" in _sql(second)  # noqa: S608 - trusted test constants
    assert "NOT EXISTS" in _sql(second)
    assert _params(first) == {"run_id": "run-1", "agent_name": _AGENT}


# --- find_existing_insights ---------------------------------------------------


def test_find_existing_insights_no_candidates_runs_no_query() -> None:
    client = mock.MagicMock()
    store = _store(client)

    assert store.find_existing_insights([]) == {}
    client.query.assert_not_called()


def test_find_existing_insights_reads_open_labels_and_asks_the_judge() -> None:
    """The query carries no AI.

    It lists this agent's open insights newest-first, and the judge chooses
    among them.
    """
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"insight_id": "ins-1", "label": "made no tool call"},
        {"insight_id": "ins-2", "label": "answered from memory"},
    ]
    store = _store(client)
    asked: dict[str, object] = {}

    def judge(candidates, existing, **kwargs):
        asked["candidates"] = list(candidates)
        asked["existing"] = list(existing)
        return {0: 0, 1: None}

    with mock.patch.object(matching, "match_candidates", judge):
        result = store.find_existing_insights(
            [
                _cluster("made no tool call"),
                _cluster("produced an empty response"),
            ]
        )

    # Candidates are matched back by index, not by their (collidable) label.
    assert result == {0: "ins-1", 1: None}
    assert asked["candidates"] == [
        "made no tool call",
        "produced an empty response",
    ]
    assert asked["existing"] == ["made no tool call", "answered from memory"]

    sql = _sql(client.query.call_args)
    assert "AI.IF" not in sql
    # Resolved insights are excluded, so a resolved issue mints a new one.
    assert f"status != '{InsightStatus.RESOLVED.value}'" in sql
    # A merged insight is skipped; a dismissed one is deliberately still matched.
    assert "merged_into_insight_id IS NULL" in sql
    # `insight_id` is the secondary key. A sweep stamps one `updated_at` across
    # everything it touches, and the order decides which batch a label lands in.
    assert "ORDER BY updated_at DESC, insight_id" in sql
    assert _params(client.query.call_args) == {"agent_name": _AGENT}


def test_find_existing_insights_with_an_empty_store_asks_nothing() -> None:
    """Every candidate is new by definition, so the judge is never called."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    store = _store(client)

    def forbidden(*args, **kwargs):
        raise AssertionError("the judge was called against an empty store")

    with mock.patch.object(matching, "match_candidates", forbidden):
        assert store.find_existing_insights([_cluster("a")]) == {0: None}


# --- save_investigation_result ------------------------------------------------


def test_save_result_inserts_new_insights_and_occurrences_and_marks_recurring() -> (
    None
):
    client = mock.MagicMock()
    store = _store(client)

    store.save_investigation_result(
        new_insights=[_insight("ins-new")],
        seen_insight_ids=["ins-old"],
        recurring_insight_ids=["ins-old"],
        occurrences=[_occurrence("ins-new")],
        now=_NOW,
    )

    loaded = {
        call.args[1]: call.args[0]
        for call in client.load_table_from_json.call_args_list
    }
    assert set(loaded) == {_INSIGHTS_REF, _OCCURRENCES_REF}
    assert loaded[_INSIGHTS_REF][0]["insight_id"] == "ins-new"
    assert loaded[_INSIGHTS_REF][0]["status"] == "NEW"

    # The rubrics list is serialized into the JSON `rubrics` column.
    rubrics = json.loads(loaded[_OCCURRENCES_REF][0]["rubrics"])
    assert rubrics[0]["rubric"]["expected_behavior"] == "call create_ticket"
    assert rubrics[0]["eval_case_id"] == "case-1"

    # One statement: an insight is never dated to this sweep without the status
    # that goes with it.
    assert client.query.call_count == 1
    call = client.query.call_args
    assert f"UPDATE `{_INSIGHTS_REF}`" in _sql(call)
    assert "updated_at = @now" in _sql(call)
    assert f"'{InsightStatus.RECURRING.value}'" in _sql(call)
    assert _params(call)["ids"] == ["ins-old"]
    assert _params(call)["recurring_ids"] == ["ins-old"]


def test_save_result_nothing_writes_nothing() -> None:
    client = mock.MagicMock()
    store = _store(client)

    store.save_investigation_result(
        new_insights=[],
        seen_insight_ids=[],
        recurring_insight_ids=[],
        occurrences=[],
        now=_NOW,
    )

    client.load_table_from_json.assert_not_called()
    client.query.assert_not_called()


def test_a_rename_travels_in_the_statement_that_dates_the_sighting() -> None:
    """The label is the recurrence key, so replacing it replaces what the next
    sweep matches against. It rides the same UPDATE as the sighting and the
    status: a row cannot be dated under a name the same sweep meant to change.
    An insight with no rename keeps the label it has, which is what the COALESCE
    is for.
    """
    client = mock.MagicMock()

    _store(client).save_investigation_result(
        new_insights=[],
        seen_insight_ids=["ins-1", "ins-2"],
        recurring_insight_ids=["ins-1"],
        occurrences=[],
        now=_NOW,
        relabelled={"ins-1": "called the tool with no city"},
    )

    assert client.query.call_count == 1
    call = client.query.call_args
    sql = _sql(call)
    assert "label = COALESCE((SELECT r.label FROM UNNEST(@renames) r" in sql
    assert "WHERE r.insight_id = i.insight_id), i.label)" in sql
    renames = _params(call)["renames"]
    assert [
        (r.struct_values["insight_id"], r.struct_values["label"])
        for r in renames
    ] == [("ins-1", "called the tool with no city")]
    # A mocked client never sends the request, so the parameters are otherwise
    # only ever checked as objects. An array of structs whose element type is
    # left undeclared fails inside the client at `json.dumps`, against BigQuery
    # and never against a test.
    json.dumps(
        [p.to_api_repr() for p in call.kwargs["job_config"].query_parameters]
    )


def test_an_insight_seen_but_unjudged_is_dated_without_a_status_change() -> (
    None
):
    """An unjudged candidate writes no occurrence against the insight it
    matched, and `resolve_stale_insights` reads exactly that absence -- so the
    sighting is recorded here instead, keeping an insight nobody judged out of
    the terminal RESOLVED state. An absent verdict is no recurrence, so the
    lifecycle state does not advance with it."""
    client = mock.MagicMock()

    _store(client).save_investigation_result(
        new_insights=[],
        seen_insight_ids=["ins-1", "ins-2"],
        recurring_insight_ids=[],
        occurrences=[],
        now=_NOW,
    )

    call = client.query.call_args
    assert f"UPDATE `{_INSIGHTS_REF}`" in _sql(call)
    assert "updated_at = @now" in _sql(call)
    # Named as seen, absent from the confirmed set, so the IF leaves status be.
    assert _params(call)["ids"] == ["ins-1", "ins-2"]
    assert _params(call)["recurring_ids"] == []
    assert client.query.call_count == 1


# --- resolve_stale_insights ---------------------------------------------------


def test_resolve_stale_insights_updates_with_window() -> None:
    client = mock.MagicMock()
    store = _store(client)

    store.resolve_stale_insights(_NOW, window_days=14)

    call = client.query.call_args
    assert f"UPDATE `{_INSIGHTS_REF}`" in _sql(call)
    assert f"status = '{InsightStatus.RESOLVED.value}'" in _sql(call)
    assert "TIMESTAMP_SUB(@now, INTERVAL @days DAY)" in _sql(call)
    assert _params(call) == {"now": _NOW, "days": 14, "agent_name": _AGENT}


def test_resolving_stamps_when_it_happened_and_leaves_the_sighting_alone() -> (
    None
):
    """The resolution date is recorded, and ``updated_at`` is not touched.

    Those two halves are one requirement: ``updated_at`` is the last sighting
    and is exactly what the staleness predicate reads, so writing the
    resolution time there would move the cutoff this statement just measured --
    and every reader would then see an issue "seen" on the day it was declared
    dead.
    """
    client = mock.MagicMock()

    _store(client).resolve_stale_insights(_NOW, window_days=14)

    sql = _sql(client.query.call_args)
    assert "resolved_at = @now" in sql
    assert "updated_at =" not in sql
    # Only rows crossing into RESOLVED are touched, so a later sweep cannot
    # restamp a date it already wrote.
    assert f"status != '{InsightStatus.RESOLVED.value}'" in sql


def test_a_newly_minted_insight_is_written_open() -> None:
    """It carries the column and leaves it null: the issue is not resolved, and
    a null there means "no resolution known", which is the same thing here."""
    client = mock.MagicMock()

    _store(client).save_investigation_result(
        new_insights=[_insight()],
        seen_insight_ids=[],
        recurring_insight_ids=[],
        occurrences=[],
        now=_NOW,
    )

    (row,) = client.load_table_from_json.call_args.args[0]
    assert "resolved_at" in row
    assert row["resolved_at"] is None


@pytest.mark.parametrize("window_days", [0, -1])
def test_resolve_stale_insights_zero_or_negative_is_a_noop(
    window_days: int,
) -> None:
    """A disabled window must resolve nothing, not everything with a 0-day cut."""
    client = mock.MagicMock()
    store = _store(client)

    store.resolve_stale_insights(_NOW, window_days=window_days)

    client.query.assert_not_called()


# --- the two operator judgements ----------------------------------------------


def _dml(client: mock.MagicMock, affected: int) -> None:
    """Configures the next query statement to report `affected` rows changed.

    Args:
        client: Mock BigQuery client to configure.
        affected: Count of DML affected rows to return.
    """
    client.query.return_value.num_dml_affected_rows = affected


def test_dismiss_insight_stamps_the_row_and_reports_it_found() -> None:
    client = mock.MagicMock()
    _dml(client, 1)

    assert _store(client).dismiss_insight("ins-1") is True

    call = client.query.call_args
    assert f"UPDATE `{_INSIGHTS_REF}`" in _sql(call)
    # COALESCE, so a second dismissal keeps the first judgement's time while
    # still matching the row -- which is what tells "already dismissed" from
    # "no such insight".
    assert "SET dismissed_at = COALESCE(dismissed_at, @now)" in _sql(call)
    assert set(_params(call)) == {"now", "agent_name", "insight_id"}
    assert _params(call)["insight_id"] == "ins-1"
    assert _params(call)["agent_name"] == _AGENT


def test_dismissing_an_unknown_insight_changes_nothing() -> None:
    client = mock.MagicMock()
    _dml(client, 0)

    assert _store(client).dismiss_insight("nope") is False


def test_merge_moves_the_source_and_everything_pointing_at_it() -> None:
    """One statement, so a chain flattens as it is written rather than
    deepening: A into B then B into C leaves both A and C's duplicates on C."""
    client = mock.MagicMock()
    _dml(client, 2)

    assert (
        _store(client).merge_insight(
            insight_id="ins-1", target_insight_id="ins-2"
        )
        is True
    )

    sql = _sql(client.query.call_args)
    assert f"UPDATE `{_INSIGHTS_REF}`" in sql
    assert (
        "SET merged_into_insight_id = @target_insight_id, merged_at = @now"
        in sql
    )
    assert (
        "(insight_id = @insight_id OR merged_into_insight_id = @insight_id)"
        in sql
    )
    assert _params(client.query.call_args)["insight_id"] == "ins-1"
    assert _params(client.query.call_args)["target_insight_id"] == "ins-2"


def test_merge_refuses_a_target_that_is_itself_a_duplicate_in_the_statement() -> (
    None
):
    """The refusal is an EXISTS inside the UPDATE, not a read then a write: the
    dashboard fires N-1 of these concurrently at one target, so a check that ran
    separately could be true when it ran and false when the update landed."""
    client = mock.MagicMock()
    _dml(client, 0)

    merged = _store(client).merge_insight(
        insight_id="ins-1", target_insight_id="ins-gone"
    )

    assert merged is False
    sql = _sql(client.query.call_args)
    assert "AND EXISTS (SELECT 1 FROM" in sql
    assert "t.merged_into_insight_id IS NULL" in sql


def test_the_match_query_skips_a_deduplicated_insight() -> None:
    """Otherwise the next sweep re-attaches a recurrence to the row an operator
    hid, and it comes straight back."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).find_existing_insights([_cluster("made no tool call")])

    assert "merged_into_insight_id IS NULL" in _sql(client.query.call_args)


def test_the_match_query_still_considers_a_dismissed_insight() -> None:
    """Deliberately, and it is the difference between the two actions: skipping
    it would mint a fresh insight every sweep for exactly the defect somebody
    asked not to see."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).find_existing_insights([_cluster("made no tool call")])

    assert "dismissed_at" not in _sql(client.query.call_args)


def test_a_newly_minted_insight_carries_no_operator_judgement() -> None:
    """All three columns are written, and all three null: a sweep never makes
    one of these, and a column left off the row would read as a default."""
    client = mock.MagicMock()

    _store(client).save_investigation_result(
        new_insights=[_insight()],
        seen_insight_ids=[],
        recurring_insight_ids=[],
        occurrences=[],
        now=_NOW,
    )

    (row,) = client.load_table_from_json.call_args.args[0]
    assert row["dismissed_at"] is None
    assert row["merged_into_insight_id"] is None
    assert row["merged_at"] is None
