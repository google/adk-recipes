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

"""Unit tests for `get_full_trajectories`.

Tests BigQuery query generation, payload parsing, status handling for missing or
lossy archives, and pagination logic using mock BigQuery clients.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core import rca_tools
from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.orchestrator import (
    insight_tools,
    paging,
    trajectory_tools,
)
from ambient_quality_agent.tools.trajectories import payloads
from ambient_quality_agent.tools.trajectories.bigquery_reader import (
    BigQueryTrajectoryReader,
)
from ambient_quality_agent.tools.trajectories.models import PayloadStatus
from google.api_core import exceptions as api_exceptions

from .conftest import StateContext

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

# SQL fragments identifying specific mock query statements.
_INSIGHT_LOOKUP_SQL = "AS occurrence_count"
_OCCURRENCE_COUNT_SQL = "COUNT(*) AS total"
_OCCURRENCE_PAGE_SQL = "SELECT o.occurrence_id, o.insight_id"


def _query_returning(*result_sets: list[dict[str, Any]]) -> mock.MagicMock:
    """Creates a mock client yielding sequential query results.

    Args:
        *result_sets: Row lists returned by consecutive ``query().result()`` calls.

    Returns:
        Configured MagicMock client.
    """
    client = mock.MagicMock()
    client.query.side_effect = [
        mock.MagicMock(**{"result.return_value": rows}) for rows in result_sets
    ]
    return client


def _sql(call: Any) -> str:
    """Extracts the SQL query string from a mock `client.query` call.

    Args:
        call: Mock call object from `client.query`.

    Returns:
        The SQL query string passed as the first positional argument.
    """
    return call.args[0]


def _params(call: Any) -> dict[str, Any]:
    """Extracts named query parameters from a mock ``client.query`` call.

    Args:
        call: Mock call object from ``client.query``.

    Returns:
        Dictionary mapping parameter names to their bound values.
    """
    return {
        p.name: getattr(p, "value", None)
        if hasattr(p, "value")
        else list(p.values)
        for p in call.kwargs["job_config"].query_parameters
    }


def _calls_matching(client: mock.MagicMock, fragment: str) -> list[Any]:
    """Selects mock query calls whose SQL contains ``fragment``.

    Matches calls by statement text rather than positional index because direct
    occurrence lookups skip the insight lookup query.

    Args:
        client: Mock BigQuery client the reader ran against.
        fragment: Distinguishing substring of the target statement.

    Returns:
        Matching mock calls in the order they were executed.
    """
    return [
        call for call in client.query.call_args_list if fragment in _sql(call)
    ]


def _occurrence_page_call(client: mock.MagicMock) -> Any:
    """Returns the single mock call that paged the occurrences table.

    Args:
        client: Mock BigQuery client the reader ran against.

    Returns:
        The matching mock call object.
    """
    calls = _calls_matching(client, _OCCURRENCE_PAGE_SQL)
    assert len(calls) == 1
    return calls[0]


def _insight_row(**overrides: Any) -> dict[str, Any]:
    """Generates a mock BigQuery row for the insights table.

    Args:
        **overrides: Field values to override the default row values.

    Returns:
        Dictionary representing the mock insight record.
    """
    row = {
        "insight_id": "ins-1",
        "agent_name": _AGENT,
        "label": "made no tool call",
        "status": "RECURRING",
        "created_at": _NOW,
        "updated_at": _NOW,
        "resolved_at": None,
        "dismissed_at": None,
        "merged_into_insight_id": None,
        "merged_at": None,
        "occurrence_count": 1,
        "trace_count": 2,
        "last_run_at": _NOW,
        "last_run_id": "run-9",
        "has_root_cause": False,
    }
    row.update(overrides)
    return row


def _occurrence_row(
    trajectory_ids: list[str], **overrides: Any
) -> dict[str, Any]:
    """Generates a mock BigQuery row for the insight occurrences table.

    Args:
        trajectory_ids: List of trajectory identifiers linked to the occurrence.
        **overrides: Field values to override the default occurrence row.

    Returns:
        Dictionary representing the mock occurrence record.
    """
    row = {
        "occurrence_id": "occ-1",
        "insight_id": "ins-1",
        "run_id": "run-9",
        "created_at": _NOW,
        "agent_name": _AGENT,
        "agent_revision": None,
        "label": "made no tool call",
        "item_count": len(trajectory_ids),
        "trace_count": len(trajectory_ids),
        "trajectory_ids": trajectory_ids,
        "analyses": None,
        "rubrics": [],
    }
    row.update(overrides)
    return row


def _payload_row(
    trajectory_id: str,
    *,
    turn_index: int | None = 0,
    status: PayloadStatus = PayloadStatus.INGESTED,
    turn_count: int = 1,
    text: str = "hello",
) -> dict[str, Any]:
    """Generates a mock BigQuery row representing a joined manifest and turn.

    Args:
        trajectory_id: Trajectory identifier.
        turn_index: Index of the turn, or None if no turns exist.
        status: Archive payload status.
        turn_count: Total turn count recorded in the manifest.
        text: Text content of the turn event.

    Returns:
        Dictionary representing the query row.
    """
    return {
        "agent_name": _AGENT,
        "trajectory_id": trajectory_id,
        "created_at": _NOW,
        "status": status.value,
        "turn_count": turn_count,
        "agents": {_AGENT: {"instruction": "be helpful"}},
        "turn_index": turn_index,
        "turn": None if turn_index is None else {"events": [{"text": text}]},
    }


@pytest.fixture
def readers(monkeypatch: pytest.MonkeyPatch):
    """Fixture providing mock insight and trajectory readers.

    Uses separate clients for insight and archive queries to allow independent
    assertions. The insight client answers by statement text so queries that
    resolve occurrences directly still receive the expected rows.
    """

    def _install(
        *,
        occurrence_rows: list[dict[str, Any]],
        payload_rows: list[dict[str, Any]],
        insight_rows: list[dict[str, Any]] | None = None,
        occurrence_total: int | None = None,
    ) -> tuple[mock.MagicMock, mock.MagicMock]:
        total = (
            len(occurrence_rows)
            if occurrence_total is None
            else occurrence_total
        )
        answers = {
            _INSIGHT_LOOKUP_SQL: _insight_rows_or_default(insight_rows),
            _OCCURRENCE_COUNT_SQL: [{"total": total}],
            _OCCURRENCE_PAGE_SQL: occurrence_rows,
        }

        def _answer(sql: str, **_: Any) -> mock.MagicMock:
            for fragment, rows in answers.items():
                if fragment in sql:
                    return mock.MagicMock(**{"result.return_value": rows})
            raise AssertionError(f"unexpected query: {sql}")

        insight_client = mock.MagicMock()
        insight_client.query.side_effect = _answer
        payload_client = _query_returning(payload_rows)
        monkeypatch.setattr(
            insight_tools,
            "reader_factory",
            lambda state: BigQueryInsightReader(
                client=insight_client,
                project_id=_PROJECT,
                dataset=_DATASET,
                agent_name=_AGENT,
            ),
        )
        monkeypatch.setattr(
            trajectory_tools,
            "reader_factory",
            lambda state: BigQueryTrajectoryReader(
                client=payload_client,
                project_id=_PROJECT,
                dataset=_DATASET,
                agent_name=_AGENT,
            ),
        )
        return insight_client, payload_client

    return _install


def _insight_rows_or_default(
    rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Returns the provided insight rows or a single-item default list when None.

    Args:
        rows: Optional list of insight rows.

    Returns:
        Non-empty list of insight rows.
    """
    return [_insight_row()] if rows is None else rows


def _call(**kwargs: Any) -> dict[str, Any]:
    """Helper to synchronously execute ``get_full_trajectories`` in tests.

    Args:
        **kwargs: Arguments passed to ``get_full_trajectories``, with optional
            ``state`` dictionary.

    Returns:
        Tool output dictionary.
    """
    state = kwargs.pop("state", None)
    return asyncio.run(
        rca_tools.get_full_trajectories(StateContext(state), **kwargs)
    )


# --- resolving what to read ---------------------------------------------------


def test_neither_id_asks_for_one(readers) -> None:
    """Verifies an error is returned when neither insight nor occurrence ID is given."""
    insight_client, payload_client = readers(
        occurrence_rows=[], payload_rows=[]
    )

    out = _call()

    assert "insight_id" in out["error"]
    assert "occurrence_id" in out["error"]
    assert "get_insight" in out["error"]
    insight_client.query.assert_not_called()
    payload_client.query.assert_not_called()


def test_the_insight_id_reaches_the_query(readers) -> None:
    """Verifies the requested insight_id is passed to the reader query."""
    insight_client, _ = readers(occurrence_rows=[], payload_rows=[])

    out = _call(insight_id="ins-other")

    assert out["insight_id"] == "ins-other"
    assert (
        _params(_occurrence_page_call(insight_client))["insight_id"]
        == "ins-other"
    )


def test_an_unknown_insight_is_an_error_not_an_empty_page(readers) -> None:
    """Verifies querying an unknown insight ID returns an error instead of an empty list."""
    readers(occurrence_rows=[], payload_rows=[], insight_rows=[])

    out = _call(insight_id="nope")

    assert "No insight found" in out["error"]


def test_an_insight_with_no_trajectory_ids_costs_no_archive_read(
    readers,
) -> None:
    """Verifies archive query is skipped when occurrences contain no trajectory IDs."""
    _, payload_client = readers(
        occurrence_rows=[_occurrence_row([])], payload_rows=[]
    )

    out = _call(insight_id="ins-1")

    assert out == {
        "trajectories": [],
        "next_page_token": None,
        "insight_id": "ins-1",
        "occurrence_id": None,
        "scope": "insight",
        "agent_revision": None,
        "total_trajectories": 0,
        "occurrences_read": 1,
        "total_occurrences": 1,
        "occurrences_truncated": False,
    }
    payload_client.query.assert_not_called()


def test_a_failed_read_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies BigQuery failures are caught and returned as error dicts."""
    monkeypatch.setattr(
        insight_tools,
        "reader_factory",
        mock.MagicMock(
            side_effect=api_exceptions.NotFound("dataset not found")
        ),
    )

    out = _call(insight_id="ins-1")

    assert "dataset not found" in out["error"]


# --- which sightings the conversations come from ------------------------------


def test_an_occurrence_id_alone_needs_no_insight_id(readers) -> None:
    """Verifies a globally unique occurrence_id resolves without an insight_id."""
    insight_client, _ = readers(
        occurrence_rows=[
            _occurrence_row(
                ["conv-b"],
                occurrence_id="occ-2",
                insight_id="ins-7",
                agent_revision="rev-2",
            )
        ],
        payload_rows=[_payload_row("conv-b")],
    )

    out = _call(occurrence_id="occ-2")

    assert out["scope"] == "occurrence"
    assert out["occurrence_id"] == "occ-2"
    # Insight ID is extracted from the resolved occurrence.
    assert out["insight_id"] == "ins-7"
    assert out["agent_revision"] == "rev-2"
    page_call = _occurrence_page_call(insight_client)
    assert _params(page_call)["occurrence_id"] == "occ-2"
    assert "insight_id = @insight_id" not in _sql(page_call)
    assert "insight_id" not in _params(page_call)
    assert not _calls_matching(insight_client, _INSIGHT_LOOKUP_SQL)


def test_occurrence_scope_claims_no_insight_wide_counts(readers) -> None:
    """Verifies single-sighting queries do not report overall insight occurrence counts."""
    readers(
        occurrence_rows=[_occurrence_row(["conv-b"], occurrence_id="occ-2")],
        payload_rows=[_payload_row("conv-b")],
        # Insight has more sightings than the single requested one.
        occurrence_total=31,
    )

    out = _call(occurrence_id="occ-2")

    assert out["occurrences_read"] == 1
    assert out["total_occurrences"] is None
    assert out["occurrences_truncated"] is False


def test_an_occurrence_id_alone_that_matches_nothing_is_an_error(
    readers,
) -> None:
    """Verifies an unresolvable occurrence_id returns an error without naming an insight."""
    readers(occurrence_rows=[], payload_rows=[])

    out = _call(occurrence_id="occ-nope")

    assert "occ-nope" in out["error"]
    assert "on insight" not in out["error"]
    assert "trajectories" not in out


def test_a_named_sighting_is_filtered_in_the_query(readers) -> None:
    """Verifies occurrence_id and insight_id are both bound when provided."""
    insight_client, _ = readers(
        occurrence_rows=[
            _occurrence_row(
                ["conv-b"], occurrence_id="occ-2", agent_revision="rev-2"
            )
        ],
        payload_rows=[_payload_row("conv-b")],
    )

    out = _call(insight_id="ins-1", occurrence_id="occ-2")

    count_call = _calls_matching(insight_client, _OCCURRENCE_COUNT_SQL)[0]
    page_call = _occurrence_page_call(insight_client)
    assert "occurrence_id = @occurrence_id" in _sql(count_call)
    assert "occurrence_id = @occurrence_id" in _sql(page_call)
    assert _params(page_call)["occurrence_id"] == "occ-2"
    assert _params(page_call)["insight_id"] == "ins-1"
    assert out["scope"] == "occurrence"
    assert out["occurrence_id"] == "occ-2"
    assert out["agent_revision"] == "rev-2"
    assert [e["trajectory_id"] for e in out["trajectories"]] == ["conv-b"]


def test_a_sighting_that_does_not_exist_is_an_error(readers) -> None:
    """Verifies an unmatched occurrence_id returns an error rather than widening scope."""
    readers(
        occurrence_rows=[],
        payload_rows=[],
        # Existing sightings do not match the requested ID.
        occurrence_total=4,
    )

    out = _call(insight_id="ins-1", occurrence_id="occ-nope")

    assert "occ-nope" in out["error"]
    assert "get_insight" in out["error"]
    assert "trajectories" not in out


def test_an_insight_alone_returns_every_sighting(readers) -> None:
    """Verifies omitting occurrence_id reads all sightings under insight scope."""
    insight_client, _ = readers(
        occurrence_rows=[
            _occurrence_row(
                ["conv-a"], occurrence_id="occ-2", agent_revision="rev-2"
            ),
            _occurrence_row(
                ["conv-b"], occurrence_id="occ-1", agent_revision="rev-1"
            ),
        ],
        payload_rows=[_payload_row("conv-a"), _payload_row("conv-b")],
    )

    out = _call(insight_id="ins-1")

    assert out["scope"] == "insight"
    assert out["occurrence_id"] is None
    assert out["agent_revision"] is None
    assert [e["trajectory_id"] for e in out["trajectories"]] == [
        "conv-a",
        "conv-b",
    ]
    page_call = _occurrence_page_call(insight_client)
    assert _params(page_call)["limit"] == rca_tools.MAX_OCCURRENCES
    assert "occurrence_id = @occurrence_id" not in _sql(page_call)


def test_the_sightings_read_are_capped(readers) -> None:
    """Verifies insight-level queries cap the number of requested sightings."""
    insight_client, _ = readers(
        occurrence_rows=[_occurrence_row(["conv-a"])], payload_rows=[]
    )

    _call(insight_id="ins-1")

    assert rca_tools.MAX_OCCURRENCES == 10
    assert _params(_occurrence_page_call(insight_client))["limit"] == 10


def test_an_insight_past_the_cap_reports_its_truncation(readers) -> None:
    """Verifies truncation is reported when available sightings exceed MAX_OCCURRENCES."""
    rows = [
        _occurrence_row([f"conv-{i}"], occurrence_id=f"occ-{i}")
        for i in range(rca_tools.MAX_OCCURRENCES)
    ]
    readers(occurrence_rows=rows, payload_rows=[], occurrence_total=57)

    out = _call(insight_id="ins-1")

    assert out["occurrences_read"] == rca_tools.MAX_OCCURRENCES
    assert out["total_occurrences"] == 57
    assert out["occurrences_truncated"] is True


def test_an_insight_within_the_cap_is_not_truncated(readers) -> None:
    """Verifies truncation is False when all sightings fit within the cap."""
    readers(
        occurrence_rows=[
            _occurrence_row(["conv-a"]),
            _occurrence_row(["conv-b"]),
        ],
        payload_rows=[_payload_row("conv-a"), _payload_row("conv-b")],
    )

    out = _call(insight_id="ins-1")

    assert out["occurrences_read"] == 2
    assert out["total_occurrences"] == 2
    assert out["occurrences_truncated"] is False


def test_each_conversation_names_the_sighting_it_came_from(readers) -> None:
    """Verifies trajectories in insight scope include per-conversation sighting attribution."""
    readers(
        occurrence_rows=[
            _occurrence_row(
                ["conv-a"], occurrence_id="occ-2", agent_revision="rev-2"
            ),
            _occurrence_row(
                ["conv-b"], occurrence_id="occ-1", agent_revision="rev-1"
            ),
        ],
        payload_rows=[_payload_row("conv-a"), _payload_row("conv-b")],
    )

    out = _call(insight_id="ins-1")

    assert [
        (e["trajectory_id"], e["occurrence_id"], e["agent_revision"])
        for e in out["trajectories"]
    ] == [("conv-a", "occ-2", "rev-2"), ("conv-b", "occ-1", "rev-1")]


def test_an_unarchived_conversation_still_names_its_sighting(readers) -> None:
    """Verifies unarchived trajectories retain their sighting and revision attribution."""
    readers(
        occurrence_rows=[
            _occurrence_row(
                ["conv-gone"], occurrence_id="occ-3", agent_revision="rev-3"
            )
        ],
        payload_rows=[],
    )

    entry = _call(insight_id="ins-1")["trajectories"][0]

    assert entry["status"] == payloads.NOT_ARCHIVED
    assert entry["occurrence_id"] == "occ-3"
    assert entry["agent_revision"] == "rev-3"


def test_occurrence_scope_leaves_attribution_to_the_top_level(readers) -> None:
    """Verifies single-sighting scope omits per-trajectory attribution fields."""
    readers(
        occurrence_rows=[
            _occurrence_row(
                ["conv-b"], occurrence_id="occ-2", agent_revision="rev-2"
            )
        ],
        payload_rows=[_payload_row("conv-b")],
    )

    entry = _call(occurrence_id="occ-2")["trajectories"][0]

    assert "occurrence_id" not in entry
    assert "agent_revision" not in entry


# --- what a conversation comes back as ----------------------------------------


def test_a_conversation_comes_back_turn_by_turn(readers) -> None:
    readers(
        occurrence_rows=[_occurrence_row(["conv-a"])],
        payload_rows=[
            _payload_row("conv-a", turn_index=0, turn_count=2, text="first"),
            _payload_row("conv-a", turn_index=1, turn_count=2, text="second"),
        ],
    )

    entry = _call(insight_id="ins-1")["trajectories"][0]

    assert entry["status"] == "ingested"
    assert entry["turn_count"] == 2
    assert entry["turns_returned"] == 2
    assert entry["recorded_at"] == _NOW.isoformat()
    assert [t["events"][0]["text"] for t in entry["turns"]] == [
        "first",
        "second",
    ]
    assert entry["agents"] == {_AGENT: {"instruction": "be helpful"}}


def test_a_conversation_nobody_archived_is_labelled_not_archived(
    readers,
) -> None:
    """Verifies unarchived trajectory IDs are explicitly returned with not_archived status."""
    readers(occurrence_rows=[_occurrence_row(["conv-gone"])], payload_rows=[])

    entry = _call(insight_id="ins-1")["trajectories"][0]

    assert entry["trajectory_id"] == "conv-gone"
    assert entry["status"] == payloads.NOT_ARCHIVED
    assert entry["turns"] == []
    assert entry["turn_count"] == 0
    assert entry["turns_returned"] == 0
    assert entry["agents"] is None
    assert entry["recorded_at"] is None


def test_an_archived_conversation_with_no_turns_is_not_the_same_answer(
    readers,
) -> None:
    """Verifies expired conversations retain manifest turn counts with zero returned turns."""
    readers(
        occurrence_rows=[_occurrence_row(["conv-a"])],
        payload_rows=[_payload_row("conv-a", turn_index=None, turn_count=3)],
    )

    entry = _call(insight_id="ins-1")["trajectories"][0]

    assert entry["status"] == "ingested"
    assert entry["turn_count"] == 3
    assert entry["turns_returned"] == 0


@pytest.mark.parametrize("status", list(PayloadStatus))
def test_a_lossy_copy_carries_its_status(
    readers, status: PayloadStatus
) -> None:
    """Verifies payload status values (e.g. partial, truncated) are preserved in output."""
    readers(
        occurrence_rows=[_occurrence_row(["conv-a"])],
        payload_rows=[_payload_row("conv-a", status=status)],
    )

    assert (
        _call(insight_id="ins-1")["trajectories"][0]["status"] == status.value
    )


# --- the archive read ---------------------------------------------------------


def test_the_archive_read_is_scoped_by_agent_and_the_ids_of_the_page(
    readers,
) -> None:
    """Verifies payload queries filter by agent name and page trajectory IDs without run_id."""
    _, payload_client = readers(
        occurrence_rows=[_occurrence_row(["conv-b", "conv-a"])],
        payload_rows=[_payload_row("conv-a"), _payload_row("conv-b")],
    )

    _call(insight_id="ins-1")

    call = payload_client.query.call_args
    assert _params(call) == {
        "agent_name": _AGENT,
        "trajectory_ids": ["conv-a", "conv-b"],
    }
    assert "trajectory_payloads" in _sql(call)
    assert "run_id" not in _sql(call)


def test_one_query_serves_the_whole_page(readers) -> None:
    """Verifies all trajectory payloads for a page are fetched in a single batch query."""
    _, payload_client = readers(
        occurrence_rows=[_occurrence_row(["conv-a", "conv-b"])],
        payload_rows=[_payload_row("conv-a"), _payload_row("conv-b")],
    )

    _call(insight_id="ins-1")

    assert payload_client.query.call_count == 1


def test_the_index_is_deduplicated_across_sightings(readers) -> None:
    """Verifies duplicate trajectory IDs across occurrences are deduplicated."""
    readers(
        occurrence_rows=[
            _occurrence_row(["conv-a", "conv-b"], agent_revision="rev-2"),
            _occurrence_row(
                ["conv-b"],
                occurrence_id="occ-2",
                run_id="run-8",
                agent_revision="rev-1",
            ),
        ],
        payload_rows=[_payload_row("conv-a"), _payload_row("conv-b")],
    )

    out = _call(insight_id="ins-1")

    assert [e["trajectory_id"] for e in out["trajectories"]] == [
        "conv-a",
        "conv-b",
    ]
    assert out["total_trajectories"] == 2
    # Duplicate trajectories are attributed to their newest sighting.
    assert [e["agent_revision"] for e in out["trajectories"]] == [
        "rev-2",
        "rev-2",
    ]


# --- paging -------------------------------------------------------------------


def test_the_second_page_continues_where_the_first_stopped(
    readers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rca_tools, "MAX_TRAJECTORIES_PER_PAGE", 2)
    ids = ["conv-a", "conv-b", "conv-c"]
    readers(
        occurrence_rows=[_occurrence_row(ids)],
        payload_rows=[_payload_row(i) for i in ids[:2]],
    )

    first = _call(insight_id="ins-1")

    assert [e["trajectory_id"] for e in first["trajectories"]] == [
        "conv-a",
        "conv-b",
    ]
    assert first["total_trajectories"] == 3
    assert first["next_page_token"] is not None
    assert paging.decode_page_token(first["next_page_token"]) == 2

    readers(
        occurrence_rows=[_occurrence_row(ids)],
        payload_rows=[_payload_row("conv-c")],
    )
    second = _call(insight_id="ins-1", page_token=first["next_page_token"])

    assert [e["trajectory_id"] for e in second["trajectories"]] == ["conv-c"]
    assert second["total_trajectories"] == 3
    assert second["next_page_token"] is None


def test_a_token_past_the_end_returns_an_empty_final_page(readers) -> None:
    """Verifies paging beyond available trajectories returns an empty page without error."""
    _, payload_client = readers(
        occurrence_rows=[_occurrence_row(["conv-a"])], payload_rows=[]
    )

    out = _call(insight_id="ins-1", page_token=paging.encode_page_token(50))

    assert out["trajectories"] == []
    assert out["next_page_token"] is None
    payload_client.query.assert_not_called()


def test_the_byte_budget_cuts_a_page_short(
    readers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies page terminates early when cumulative payload exceeds byte budget."""
    monkeypatch.setattr(rca_tools, "PAGE_BYTE_BUDGET", 400)
    readers(
        occurrence_rows=[_occurrence_row(["conv-a", "conv-b"])],
        payload_rows=[
            _payload_row("conv-a", text="x" * 500),
            _payload_row("conv-b", text="y" * 500),
        ],
    )

    out = _call(insight_id="ins-1")

    assert [e["trajectory_id"] for e in out["trajectories"]] == ["conv-a"]
    assert paging.decode_page_token(out["next_page_token"]) == 1


def test_one_oversized_conversation_is_still_returned(
    readers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies at least one conversation is returned even if it exceeds the byte budget."""
    monkeypatch.setattr(rca_tools, "PAGE_BYTE_BUDGET", 1)
    readers(
        occurrence_rows=[_occurrence_row(["conv-a"])],
        payload_rows=[_payload_row("conv-a", text="x" * 5000)],
    )

    out = _call(insight_id="ins-1")

    assert len(out["trajectories"]) == 1
