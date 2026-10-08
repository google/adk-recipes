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

"""Tests for the dashboard's mock deployment (``tools/mock_aqa_*.py``).

A fixture is only worth having if what is built against it also works against
production, so the two halves are pinned separately:

* the **data** has to be what the agent's own models accept, and its counters
  have to hold the funnel relations a chart will read them as. A generator whose
  stages don't add up produces a picture that looks fine and is wrong.
* the **wire** has to be what the dashboard's client parses: the command routes
  and the A2A chat framing a locally served agent answers with.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import dataclasses
import datetime as dt
import importlib.util
import json
import os
import re
import sys
from typing import Any

import httpx
import pytest
from ambient_quality_agent.tools.insights.models import (
    InsightOccurrence,
    InsightStatus,
    InsightView,
)
from ambient_quality_agent.tools.investigations.models import (
    InvestigationCounters,
    InvestigationRecord,
    list_counter_names,
)


def _load(name: str) -> Any:
    """Loads a script from `tools/` as a dynamic module.

    Args:
        name: Base filename of the script without extension.

    Returns:
        Loaded module instance.
    """
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"{name}.py"
    )
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclasses.dataclass` resolves the
    # defining module out of `sys.modules`, and fails on a module that is not there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock_data = _load("mock_aqa_data")
mock_server = _load("mock_aqa_server")


_ANCHOR = dt.datetime(2026, 7, 15, 9, 30, tzinfo=dt.UTC)

_DAYS = 21
"""Long enough for the whole scenario to be present: the fixed defects stop
occurring 15-17 days back and have to then age past the 14-day auto-resolution,
so a shorter history would quietly hold no `RESOLVED` insight to test."""


@pytest.fixture(scope="module")
def dataset() -> dict[str, Any]:
    scenario = mock_data.Scenario(
        days=_DAYS, runs_per_day=4, seed=11, anchor=_ANCHOR
    )
    return mock_data.build_dataset(scenario, mock_data.load())


@pytest.fixture(scope="module")
def served(dataset: dict[str, Any]) -> Any:
    # Nothing settles under this fixture. It is module-scoped and the scheduling
    # test appends a run to it, so a wall-clock deadline would let a slow suite
    # finish that run halfway through the module and move every total that
    # follows -- a failure that only reproduces on a loaded machine.
    return mock_server.Dataset(dataset, settle_seconds=0)


# --------------------------------------------------------------------------- #
# The data is what the agent's models say it is                                #
# --------------------------------------------------------------------------- #
# The generator builds every record as a real model and serializes it with the
# real presenter, so these assertions are about the round trip: what it wrote
# has to validate back, or the fixture has drifted from the schema it imitates.


def test_runs_validate_as_investigation_records(dataset) -> None:
    for run in dataset["runs"]:
        record = InvestigationRecord.model_validate(run)
        assert record.run_id == run["run_id"]
        assert record.status.value == run["status"]


def test_every_run_carries_every_counter(dataset) -> None:
    # `format_run` promises a full mapping on every run, whatever it was
    # recorded by, so a chart can read the same keys off all of them.
    for run in dataset["runs"]:
        assert tuple(run["counters"]) == list_counter_names()


def test_insights_and_occurrences_validate(dataset) -> None:
    for insight in dataset["insights"]:
        InsightView.model_validate(insight)
    for occurrence in dataset["occurrences"]:
        InsightOccurrence.model_validate(occurrence)


def test_config_is_the_serialized_effective_config(dataset) -> None:
    config = dataset["config"]
    # Spot-check the fields the dashboard's configuration panel groups on.
    assert config["observed_agent_name"]
    assert config["data_lookback_window"] >= 1
    assert isinstance(config["multi_turn_metrics"], list)


# --------------------------------------------------------------------------- #
# The funnel adds up                                                           #
# --------------------------------------------------------------------------- #
# These are the relations a Sankey or a funnel chart draws as flows. They are
# asserted here rather than trusted because a generator that violates one still
# produces a plausible-looking dashboard -- the error only shows up as a chart
# whose arrows don't conserve anything.


def _measured(dataset) -> list[InvestigationCounters]:
    """Extracts non-zero counter objects from completed runs in the dataset.

    Args:
        dataset: Dataset dictionary containing run records.

    Returns:
        List of validated InvestigationCounters instances.
    """
    return [
        InvestigationCounters.model_validate(run["counters"])
        for run in dataset["runs"]
        if any((run["counters"] or {}).values())
    ]


def test_ingestion_splits_the_sample(dataset) -> None:
    for c in _measured(dataset):
        sampled = (
            c.traces_ingested
            + c.traces_ingested_partial
            + c.traces_ingested_failed
        )
        assert sampled <= c.traces_scanned
        assert (
            c.traces_evaluated == c.traces_ingested + c.traces_ingested_partial
        )


def test_evaluation_outcomes_partition_the_evaluated(dataset) -> None:
    for c in _measured(dataset):
        assert (
            c.traces_eval_passed + c.traces_eval_failed + c.traces_eval_errored
            == (c.traces_evaluated)
        )


def test_findings_partition_into_new_and_recurring(dataset) -> None:
    for c in _measured(dataset):
        assert c.insights_created + c.insights_recurring == c.clusters_created
        # A cluster needs a failing trace behind it, and evidence to be built of.
        if c.clusters_created:
            assert c.traces_eval_failed > 0
        assert c.rubrics_errored + c.rubrics_unclustered <= c.rubrics_generated


def test_sampling_actually_bites(dataset) -> None:
    """At least some windows hold more traces than the budget lets through.

    The point of `traces_scanned` next to `traces_ingested` is that they differ;
    a fixture where the budget never binds would let a chart drop the
    distinction and still look right.
    """
    sampled_away = [
        c
        for c in _measured(dataset)
        if c.traces_scanned
        > c.traces_ingested
        + c.traces_ingested_partial
        + c.traces_ingested_failed
    ]
    assert sampled_away


# --------------------------------------------------------------------------- #
# The two ways to count new insights agree                                     #
# --------------------------------------------------------------------------- #


def test_new_insights_per_day_matches_the_insight_records(dataset) -> None:
    """`insights_created` summed per day == insights first seen that day.

    The "new insights over time" series can be built from either side -- the
    runs' counters or the insights' `created_at` -- and a fixture where the two
    disagree would make a correct chart look broken (or the reverse).
    """
    from_runs: collections.Counter[str] = collections.Counter()
    for run in dataset["runs"]:
        created = (run["counters"] or {}).get("insights_created", 0)
        if created:
            from_runs[(run["window_end"] or "")[:10]] += created

    from_insights = collections.Counter(
        i["created_at"][:10] for i in dataset["insights"]
    )
    assert from_runs == from_insights


def test_totals_match_the_sum_of_the_runs(dataset, served) -> None:
    totals, _ = served.sum_counters()
    assert totals["investigations"] == len(dataset["runs"])
    for name in list_counter_names():
        assert totals[name] == sum(r["counters"][name] for r in dataset["runs"])


def test_totals_can_be_scoped_to_a_period(dataset, served) -> None:
    """The mock scopes on ``window_end`` as `InvestigationStore.sum_counters` does, so
    a period read here meets the rules it will meet in production."""
    cutoff = _ANCHOR - dt.timedelta(days=3)
    scoped, _ = served.sum_counters(cutoff.isoformat(), "")

    expected = [
        run
        for run in dataset["runs"]
        if run["window_end"]
        and dt.datetime.fromisoformat(run["window_end"]) >= cutoff
    ]
    assert scoped["investigations"] == len(expected)
    assert 0 < scoped["investigations"] < len(dataset["runs"])
    assert scoped["traces_scanned"] == sum(
        r["counters"]["traces_scanned"] for r in expected
    )


def test_a_period_is_not_a_page(dataset, served) -> None:
    """The whole table is aggregated, so a window wider than the runs list still
    counts every run in it -- which is the reason the period exists rather than
    being summed off `list_recent`."""
    wide, _ = served.sum_counters(
        (_ANCHOR - dt.timedelta(days=90)).isoformat(), ""
    )

    assert wide["investigations"] == len(dataset["runs"])
    assert wide["investigations"] > mock_server.LIST_LIMIT
    assert len(served.list_recent_runs()) == mock_server.LIST_LIMIT


def test_recent_runs_honours_a_period(served) -> None:
    """The list is scoped like the totals, so the mock exercises the dashboard's
    single period control the way the deployment will."""
    week = (_ANCHOR - dt.timedelta(days=7)).isoformat()
    scoped = served.list_recent_runs(week, "")

    assert scoped, "a week of a 30-day dataset should not be empty"
    assert len(scoped) < len(served.list_recent_runs())
    assert all(run["window_end"] >= week for run in scoped)


def test_recent_runs_caps_after_the_period_not_before(dataset, served) -> None:
    """A period holding more runs than the cap yields its most recent page. Were
    the cap applied first, a wide period would be filtered down from one page
    and could return far fewer runs than it holds."""
    wide = (_ANCHOR - dt.timedelta(days=90)).isoformat()
    runs = served.list_recent_runs(wide, "")

    assert len(dataset["runs"]) > mock_server.LIST_LIMIT
    assert len(runs) == mock_server.LIST_LIMIT
    # The page kept is the newest end of the period, not the oldest.
    assert runs[-1]["window_end"] == max(
        r["window_end"] for r in dataset["runs"] if r.get("window_end")
    )


def test_insights_are_scoped_by_last_sighting(served) -> None:
    """`updated_at`, not `created_at`: a defect first found last month but seen
    yesterday belongs in this week's list."""
    week = (_ANCHOR - dt.timedelta(days=7)).isoformat()
    page = mock_server._list_insights(served, {"window_start": week})

    assert page["insights"], "the regression week should leave insights in view"
    assert all(i["updated_at"] >= week for i in page["insights"])
    assert page["total"] == len(
        [i for i in served.insights if i["updated_at"] >= week]
    )


def test_a_days_insights_are_the_ones_its_chart_column_counts(
    dataset, served
) -> None:
    """The per-day filter has to list what the dashboard's per-day chart counts:
    the day's `insights_created`, summed over its sweeps, and the insights
    resolved that day. A mock that disagreed would demo a chart whose columns
    open onto a different number of rows."""
    days = served.sum_counters_by_day("", "")
    resolved_on: dict[str, int] = {}
    for insight in dataset["insights"]:
        if insight["status"] == "RESOLVED":
            day = insight["resolved_at"][:10]
            resolved_on[day] = resolved_on.get(day, 0) + 1

    assert any(d["insights_created"] for d in days)
    assert resolved_on, "the scenario should resolve something to compare"
    for row in days:
        day = row["day"]
        new = mock_server._list_insights(served, {"day": day, "status": "NEW"})
        gone = mock_server._list_insights(
            served, {"day": day, "status": "RESOLVED"}
        )
        assert new["total"] == row["insights_created"], day
        assert gone["total"] == resolved_on.get(day, 0), day
        either = mock_server._list_insights(served, {"day": day})
        assert either["total"] == len(
            {i["insight_id"] for i in new["insights"] + gone["insights"]}
        ), day


def test_insights_refuse_a_day_that_is_not_one(served) -> None:
    assert "error" in mock_server._list_insights(served, {"day": "2026-02-30"})
    assert "error" in mock_server._list_insights(
        served, {"day": "2026-07-01", "status": "RECURRING"}
    )


def test_counters_by_day_covers_a_period_the_list_cannot(
    dataset, served
) -> None:
    """The point of aggregating server-side: a thirty-day chart holds more runs
    than the list returns, so bucketing the list would have drawn most of the
    period as empty."""
    days = served.sum_counters_by_day(
        (_ANCHOR - dt.timedelta(days=30)).isoformat(), ""
    )

    charted = sum(int(d["investigations"]) for d in days)
    assert charted > mock_server.LIST_LIMIT
    assert charted == len([r for r in dataset["runs"] if r.get("window_end")])
    # Oldest first, one row per day that actually saw a sweep.
    assert [d["day"] for d in days] == sorted({d["day"] for d in days})


def test_counters_by_day_totals_reconcile_with_the_totals(served) -> None:
    """Two different aggregations of the same rows, so they have to agree --
    the charts and the funnel are read side by side."""
    window = (_ANCHOR - dt.timedelta(days=7)).isoformat()
    days = served.sum_counters_by_day(window, "")
    stats, _ = served.sum_counters(window, "")

    for counter in (
        "traces_eval_failed",
        "traces_eval_passed",
        "insights_created",
    ):
        assert sum(int(d.get(counter, 0)) for d in days) == stats[counter]


def test_counters_by_day_flags_sweeps_that_measured_nothing(served) -> None:
    """A failed or skipped sweep is a day with a gap in it, not a quiet day."""
    days = served.sum_counters_by_day("", "")

    assert any(int(d["unmeasured"]) for d in days), (
        "the scenario includes failed/skipped runs, which should show up here"
    )
    assert all(int(d["unmeasured"]) <= int(d["investigations"]) for d in days)


def test_occurrence_counts_match_the_occurrence_rows(dataset) -> None:
    rows = collections.Counter(o["insight_id"] for o in dataset["occurrences"])
    for insight in dataset["insights"]:
        assert insight["occurrence_count"] == rows[insight["insight_id"]]


# --------------------------------------------------------------------------- #
# The scenario is worth drawing                                                #
# --------------------------------------------------------------------------- #
# A fixture that only holds healthy, finished, recurring runs would let a
# dashboard ship with no rendering for anything else. These assert that the
# cases the UI has code for are actually present.


def test_every_run_status_the_dashboard_renders_is_present(dataset) -> None:
    statuses = {run["status"] for run in dataset["runs"]}
    assert {"done", "running", "failed", "skipped"} <= statuses


def test_every_insight_lifecycle_state_is_present(dataset) -> None:
    statuses = {i["status"] for i in dataset["insights"]}
    assert {s.value for s in InsightStatus} == statuses


def test_a_resolved_insight_carries_the_date_it_was_resolved(dataset) -> None:
    """The fixture records the resolution rather than leaving it to be
    estimated, so the dashboard's resolved series is drawn from the same field
    a deployment now writes."""
    resolved = [i for i in dataset["insights"] if i["status"] == "RESOLVED"]
    assert resolved

    for insight in resolved:
        assert insight["resolved_at"], insight["insight_id"]
        # Resolution follows the last sighting; the two are separate facts, and
        # conflating them is what the column exists to stop.
        assert insight["resolved_at"] > insight["updated_at"]

    # An open issue has no resolution date, so null means "not resolved" here
    # rather than "not recorded".
    for insight in dataset["insights"]:
        if insight["status"] != "RESOLVED":
            assert insight["resolved_at"] is None


def test_the_incident_lifts_the_failure_rate(dataset) -> None:
    """The seeded regression has to be visible as a rate, not just a wiggle."""
    incident_start = _ANCHOR - dt.timedelta(days=3)
    before: list[tuple[int, int]] = []
    during: list[tuple[int, int]] = []
    for run in dataset["runs"]:
        c = run["counters"]
        if not c["traces_evaluated"]:
            continue
        bucket = (
            during
            if dt.datetime.fromisoformat(run["window_end"]) >= incident_start
            else before
        )
        bucket.append((c["traces_eval_failed"], c["traces_evaluated"]))

    def rate(rows: list[tuple[int, int]]) -> float:
        return sum(f for f, _ in rows) / sum(e for _, e in rows)

    assert before and during
    assert rate(during) > 2 * rate(before)


def test_a_failed_run_carries_an_error_and_no_counters(dataset) -> None:
    failed = [r for r in dataset["runs"] if r["status"] == "failed"]
    assert failed
    for run in failed:
        assert run["error"]
        assert not any(run["counters"].values())


def test_a_deployment_with_no_telemetry_only_ever_fails() -> None:
    """`--no-telemetry`: the state a customer meets AQuA in (b/563290003).

    Nothing has been exported, so every sweep fails its first BigQuery read and
    none reaches a stage that could produce a finding. The dashboard reads those
    failures as an empty deployment rather than a broken one, and this is the
    only fixture that puts it in that state.
    """
    dataset = mock_data.build_dataset(
        mock_data.Scenario(days=3, runs_per_day=2, no_telemetry=True),
        mock_data.load(),
    )

    runs = dataset["runs"]
    assert runs
    assert {r["status"] for r in runs} == {"failed"}
    for run in runs:
        assert run["error"] == mock_data._NO_TELEMETRY_ERROR
        assert not any(run["counters"].values())
    assert dataset["insights"] == []
    assert dataset["occurrences"] == []


def test_the_no_telemetry_error_is_the_one_the_dashboard_classifies() -> None:
    """Pins the fixture to `MISSING_COLUMN_RE` in `ui/web/lib/failure.ts`.

    The dashboard decides a failure is an empty deployment by matching this
    text. Reworded here, the fixture would still look plausible while no longer
    exercising the reading it exists to check.
    """
    assert re.search(
        r"field name (\w+) does not exist in struct<",
        mock_data._NO_TELEMETRY_ERROR,
        re.IGNORECASE,
    )


def test_generation_is_deterministic_for_a_seed() -> None:
    scenario = mock_data.Scenario(
        days=3, runs_per_day=2, seed=3, anchor=_ANCHOR
    )
    first = mock_data.build_dataset(scenario, mock_data.load())
    second = mock_data.build_dataset(scenario, mock_data.load())
    # `generated_at` is wall-clock by design; everything else has to repeat.
    for payload in (first, second):
        payload.pop("generated_at")
    assert first == second


# --------------------------------------------------------------------------- #
# The served surface behaves like the real store                               #
# --------------------------------------------------------------------------- #


def test_every_limit_the_mock_copies_matches_the_agent() -> None:
    """The mock restates the agent's paging constants rather than importing
    them, so it can stay a plain JSON server. Nothing but this holds the copies
    level -- and a mock that pages differently from production hides the paging
    bugs it exists to surface.
    """
    from ambient_quality_agent.tools.investigations.store import (
        DEFAULT_LIST_LIMIT,
    )
    from ambient_quality_agent.tools.orchestrator import insight_tools

    assert mock_server.LIST_LIMIT == DEFAULT_LIST_LIMIT
    assert mock_server.INSIGHTS_PAGE_SIZE == insight_tools.INSIGHTS_PAGE_SIZE
    assert (
        mock_server.MAX_INSIGHTS_PAGE_SIZE
        == insight_tools.MAX_INSIGHTS_PAGE_SIZE
    )
    assert (
        mock_server.OCCURRENCES_PAGE_SIZE == insight_tools.OCCURRENCES_PAGE_SIZE
    )


def test_the_orders_the_mock_copies_match_the_reader() -> None:
    """The same hand-copy problem as the statuses below: an order the reader
    grows and this forgets comes back "invalid order_by" from the mock, for a
    ranking production serves."""
    from ambient_quality_agent.tools.insights.reader import InsightOrder

    assert set(mock_server.INSIGHT_ORDERS) == {o.value for o in InsightOrder}


def test_the_status_filter_accepts_exactly_the_lifecycle_states() -> None:
    """The same hand-copy problem: a state added to `InsightStatus` and
    forgotten here would come back from the mock as "invalid status" for a
    filter production accepts."""
    assert set(mock_server.INSIGHT_STATUSES) == {s.value for s in InsightStatus}


def test_the_list_is_capped_like_list_recent(dataset) -> None:
    from ambient_quality_agent.tools.investigations.store import (
        DEFAULT_LIST_LIMIT,
    )

    assert mock_server.LIST_LIMIT == DEFAULT_LIST_LIMIT
    big = mock_server.Dataset(
        {**dataset, "runs": dataset["runs"] * 3}  # more runs than the cap
    )
    runs = big.list_recent_runs()
    assert len(runs) == DEFAULT_LIST_LIMIT
    # Oldest first, the order `list_recent` returns.
    assert [r["created_at"] for r in runs] == sorted(
        r["created_at"] for r in runs
    )


def test_the_list_view_carries_no_events(served) -> None:
    assert all(run["events"] == [] for run in served.list_recent_runs())


def test_the_detail_view_carries_events(served) -> None:
    with_events = [
        run for run in served.runs if run["status"] in ("done", "running")
    ]
    assert with_events
    assert served.get_run(with_events[-1]["run_id"])["events"]


def test_insight_pages_are_opaque_and_round_trip(served) -> None:
    page = mock_server._list_insights(served, {})
    assert page["total"] == len(served.insights)
    assert len(page["insights"]) <= mock_server.INSIGHTS_PAGE_SIZE


def test_the_insight_list_serves_the_page_size_asked_for(served) -> None:
    page = mock_server._list_insights(served, {"page_size": 10})

    assert len(page["insights"]) == 10
    assert page["total"] == len(served.insights)
    assert page["next_page_token"]


def test_the_insight_list_pages_a_ranking_of_every_match(served) -> None:
    """Walking the pages yields the whole list in impact order, once each.

    The dashboard reads one page at a time, so a server that ranked inside the
    page would hand it a first page that is not the top of anything.
    """
    seen: list[dict] = []
    token = ""
    while True:
        page = mock_server._list_insights(
            served, {"order_by": "impact", "page_size": 10, "page_token": token}
        )
        seen.extend(page["insights"])
        token = page["next_page_token"]
        if not token:
            break

    assert len(seen) == len(served.list_visible_insights())
    assert len({i["insight_id"] for i in seen}) == len(seen)
    traces = [int(i["trace_count"]) for i in seen]
    assert traces == sorted(traces, reverse=True)


def test_the_insight_list_refuses_an_order_it_does_not_have(served) -> None:
    page = mock_server._list_insights(served, {"order_by": "loudest"})

    assert "invalid order_by" in page["error"]


def test_occurrence_paging_matches_the_tool(served) -> None:
    insight = max(served.insights, key=lambda i: i["occurrence_count"])
    # A fixture whose deepest insight fits on one page would pass the paging
    # assertions below without ever paging.
    assert insight["occurrence_count"] > mock_server.OCCURRENCES_PAGE_SIZE
    first = mock_server._get_insight(
        served, {"insight_id": insight["insight_id"]}
    )
    assert len(first["occurrences"]) == mock_server.OCCURRENCES_PAGE_SIZE
    assert first["next_page_token"]
    second = mock_server._get_insight(
        served,
        {
            "insight_id": insight["insight_id"],
            "page_token": first["next_page_token"],
        },
    )
    seen = {o["occurrence_id"] for o in first["occurrences"]}
    assert not seen & {o["occurrence_id"] for o in second["occurrences"]}


def test_traces_are_opt_out(served) -> None:
    # The default has to match `insight_tools.get_insight`, which the container
    # route calls: a caller that omits the argument gets traces from both.
    insight = served.insights[0]["insight_id"]
    by_default = mock_server._get_insight(served, {"insight_id": insight})
    declined = mock_server._get_insight(
        served, {"insight_id": insight, "include_traces": False}
    )
    assert any(
        rubric["trace"]
        for occurrence in by_default["occurrences"]
        for rubric in occurrence["rubrics"]
    )
    assert all(
        not rubric["trace"]
        for occurrence in declined["occurrences"]
        for rubric in occurrence["rubrics"]
    )


def test_a_sighting_resolves_its_conversations_with_links(served) -> None:
    """The demo has to show the relation production has. Without these the
    panel renders plain chips and the trace links look unimplemented."""
    insight = served.insights[0]["insight_id"]
    detail = mock_server._get_insight(served, {"insight_id": insight})

    occurrence = next(
        o for o in detail["occurrences"] if o.get("trajectory_ids")
    )
    assert [
        t["trajectory_id"] for t in occurrence["trajectories"]
    ] == occurrence["trajectory_ids"]
    assert any(t["console_url"] for t in occurrence["trajectories"])


def test_some_conversations_have_no_trace_to_open(served) -> None:
    """A `big_query` trajectory carries no trace ids, and the panel must render
    it as text. A mock where every chip links would hide that path."""
    insight = served.insights[0]["insight_id"]
    detail = mock_server._get_insight(served, {"insight_id": insight})

    wide = next(
        o
        for o in detail["occurrences"]
        if len(o.get("trajectory_ids") or []) >= 3
    )
    assert any(not t["console_url"] for t in wide["trajectories"])


def test_the_links_are_opt_out_independently_of_the_traces(served) -> None:
    """The mock has to answer `include_trajectories` the way the engine does,
    or the dashboard's own request shape goes untested until deployment."""
    insight = served.insights[0]["insight_id"]
    linked = mock_server._get_insight(
        served,
        {
            "insight_id": insight,
            "include_traces": False,
            "include_trajectories": True,
        },
    )
    declined = mock_server._get_insight(
        served, {"insight_id": insight, "include_trajectories": False}
    )

    assert any(o.get("trajectories") for o in linked["occurrences"])
    assert all(
        not rubric["trace"]
        for occurrence in linked["occurrences"]
        for rubric in occurrence["rubrics"]
    )
    assert all("trajectories" not in o for o in declined["occurrences"])


def test_unknown_ids_report_the_same_errors_the_tools_do(served) -> None:
    assert (
        "No run found with id"
        in mock_server._get_investigation(served, "nope")["error"]
    )
    assert (
        "No insight found with id"
        in mock_server._get_insight(served, {"insight_id": "nope"})["error"]
    )
    assert (
        "insight_id is required"
        in mock_server._get_insight(served, {})["error"]
    )


def test_scheduling_appends_a_pending_run(served) -> None:
    before = len(served.runs)
    run = mock_server._schedule_investigation(served)
    assert run["status"] == "pending"
    assert run["run_id"]
    assert not any(run["counters"].values())
    assert len(served.runs) == before + 1
    # It has to be shaped like every other run, or the list breaks on it.
    InvestigationRecord.model_validate(run)


def test_a_scheduled_run_finishes_once_its_delay_is_up(dataset) -> None:
    """The dashboard keeps ▶ Run investigation disabled while a sweep is in
    flight, so a mock that never finished one would demo a dead button."""
    served = mock_server.Dataset(dataset, settle_seconds=40)
    run = mock_server._schedule_investigation(served)
    assert served.settle_due() == 0
    assert served.get_run(run["run_id"])["status"] == "pending"

    settled = served.settle_due(
        dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=41)
    )
    assert settled == 1
    record = served.get_run(run["run_id"])
    assert record["status"] == "done"
    assert record["finished_at"]
    # It finishes with results: a sweep that completed with every counter at
    # zero reads as broken rather than as done.
    assert any(record["counters"].values())
    InvestigationRecord.model_validate(record)
    # Nothing is owed twice.
    assert (
        served.settle_due(dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1))
        == 0
    )


def test_the_list_read_is_what_settles_a_due_run(dataset) -> None:
    """Settling is driven by reads, and this is the read that drives it.

    ▶ Run investigation derives its disabled state from this list and polls it.
    If listing did not finish a due run, a sweep started from the dashboard
    would stay `pending` and the button would never come back.
    """
    served = mock_server.Dataset(dataset, settle_seconds=40)
    run_id = mock_server._schedule_investigation(served)["run_id"]
    # Not due yet, so listing leaves it alone and the button stays disabled.
    assert _listed(served, run_id)["status"] == "pending"

    # Bring its due time forward rather than sleeping through the delay.
    due, finished = served._settling[run_id]
    served._settling[run_id] = (due - dt.timedelta(seconds=41), finished)

    assert _listed(served, run_id)["status"] == "done"


def _listed(data, run_id: str) -> dict[str, Any]:
    runs = mock_server._list_investigations(data, {})["runs"]
    return next(r for r in runs if r["run_id"] == run_id)


def test_the_fixtures_own_running_sweep_is_finished_only_if_it_would_block(
    dataset,
) -> None:
    """The generator leaves its newest sweep `running`, and how old that is at
    serve time depends on the clock. Fresh, it disables ▶ Run investigation and
    nothing here would ever finish it -- so this server does. Old enough that
    the dashboard has already written it off, it is left as the stalled record
    it is, which is its own state worth demoing."""
    now = dt.datetime.now(tz=dt.UTC)

    fresh_payload = copy.deepcopy(dataset)
    fresh_run = max(fresh_payload["runs"], key=lambda r: r["created_at"])
    assert str(fresh_run["status"]).lower() == "running"
    fresh_run["created_at"] = (now - dt.timedelta(minutes=1)).isoformat()

    served = mock_server.Dataset(fresh_payload, settle_seconds=40)
    # The deadline is set from the clock while the dataset is built, which on a
    # loaded runner can be seconds after `now`, so it is checked against the
    # clock after that.
    assert (
        served.settle_due(dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=41))
        == 1
    )
    assert served.get_run(fresh_run["run_id"])["status"] == "done"

    stale_payload = copy.deepcopy(dataset)
    stale_run = max(stale_payload["runs"], key=lambda r: r["created_at"])
    stale_run["created_at"] = (
        now - dt.timedelta(seconds=mock_server.STALE_AFTER_SECONDS + 60)
    ).isoformat()

    ignored = mock_server.Dataset(stale_payload, settle_seconds=40)
    assert ignored.settle_due(now + dt.timedelta(days=1)) == 0
    assert (
        str(ignored.get_run(stale_run["run_id"])["status"]).lower() == "running"
    )


def test_settle_seconds_zero_leaves_the_run_pending(dataset) -> None:
    served = mock_server.Dataset(dataset, settle_seconds=0)
    run = mock_server._schedule_investigation(served)
    assert (
        served.settle_due(dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1))
        == 0
    )
    assert served.get_run(run["run_id"])["status"] == "pending"


# --------------------------------------------------------------------------- #
# The wire: the framing the dashboard's client actually parses                 #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def app(dataset) -> Any:
    return mock_server.build_app(
        mock_server.Dataset(dataset), app_name="mock_aqa", latency_ms=0
    )


def test_list_apps_names_the_mock_app(app) -> None:
    # `tools/local_ui.sh` probes `/list-apps` to detect the server and check
    # the app name it was given.
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mock"
        ) as c:
            return await c.get("/list-apps")

    assert asyncio.run(call()).json() == ["mock_aqa"]


# --------------------------------------------------------------------------- #
# A2A: the chat sidebar's wire                                                 #
# --------------------------------------------------------------------------- #
# The chat is the one panel with no command route behind it, so what is pinned
# here is the framing rather than the prose: a card the client can build a
# transport from, frames it can parse, and the two states -- terminal, and
# awaiting an answer -- that decide whether a turn ever ends.

_CHAT_APP = "aqa_chat"
"""The A2A app name the UI proxy forwards to. Pinned against both sides below."""


@pytest.fixture(scope="module")
def chat_data(dataset) -> Any:
    """A dataset of the chat's own, settling nothing.

    Approving the confirmation card schedules a run, so these tests mutate what
    they are served; and `settle_seconds=0` keeps that run pending rather than
    letting a wall-clock deadline finish it partway through the module -- the
    same reason `served` is built that way.
    """
    return mock_server.Dataset(dataset, settle_seconds=0)


@pytest.fixture(scope="module")
def chat_app(chat_data) -> Any:
    return mock_server.build_app(chat_data, app_name="mock_aqa", latency_ms=0)


def _rpc(app: Any, method: str, params: dict[str, Any]) -> httpx.Response:
    """Executes a JSON-RPC 2.0 call against the mock chat endpoint.

    Args:
        app: ASGI application instance.
        method: JSON-RPC method name.
        params: JSON-RPC method parameters dictionary.

    Returns:
        HTTP response object from the endpoint.
    """

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mock"
        ) as c:
            return await c.post(
                f"/a2a/{_CHAT_APP}",
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": method,
                    "params": params,
                },
            )

    return asyncio.run(call())


def _turn(app: Any, message: dict[str, Any]) -> list[dict[str, Any]]:
    """Drives a SendStreamingMessage turn and extracts ordered frame results.

    Args:
        app: ASGI application instance.
        message: A2A message payload dictionary.

    Returns:
        List of JSON-RPC result dictionaries extracted from SSE data lines.
    """
    response = _rpc(app, "SendStreamingMessage", {"message": message})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(frame[len("data:") :].strip())
        for frame in response.text.split("\n\n")
        if frame.strip().startswith("data:")
    ]
    # The client rejects any frame whose id is not the one it sent, so echoing
    # it is part of the contract rather than decoration.
    assert {f["id"] for f in frames} == {7}
    return [f["result"] for f in frames]


def _say(app: Any, text: str, context_id: str = "ctx") -> list[dict[str, Any]]:
    return _turn(
        app,
        {
            "messageId": f"m-{context_id}-{len(text)}",
            "contextId": context_id,
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        },
    )


def _artifact_frames(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r["artifactUpdate"] for r in results if "artifactUpdate" in r]


def _streamed_text(results: list[dict[str, Any]]) -> str:
    """Concatenates streamed text chunks from artifact update frames.

    Args:
        results: Sequence of frame results from a streaming turn.

    Returns:
        Reassembled text string.
    """
    return "".join(
        f["artifact"]["parts"][0]["text"] for f in _artifact_frames(results)
    )


def test_the_chat_answers_at_the_name_the_ui_proxy_forwards() -> None:
    """The mock serves the chat under a path parameter, so it has no opinion
    about the name -- but the UI proxy and the CLI's `run` do, and both read
    `CHAT_A2A_APP`. A rename on either side that missed the other reads as a
    chat that never connects.
    """
    from ambient_quality_agent.core import chat_agent
    from ambient_quality_shared.protocol import CHAT_A2A_APP

    assert CHAT_A2A_APP == chat_agent.AGENT_NAME == _CHAT_APP


def test_the_confirmation_tool_is_the_one_adk_synthesizes() -> None:
    """The approve/decline card renders for this exact name and no other; a
    drifted copy is a silent downgrade to a generic tool row, not an error."""
    from google.adk.flows.llm_flows.functions import (
        REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
    )

    assert (
        mock_server.CONFIRMATION_TOOL == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    )


def test_the_agent_card_can_build_a_transport(chat_app) -> None:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=chat_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mock"
        ) as c:
            return await c.get(f"/a2a/{_CHAT_APP}/.well-known/agent-card.json")

    card = asyncio.run(call()).json()
    interfaces = card["supportedInterfaces"]
    # `createLhaClient` throws "backend predates A2A 1.0" on an empty list, and
    # `pickMatchingInterface` matches on the binding and the version.
    assert interfaces
    assert interfaces[0]["protocolBinding"] == mock_server.A2A_PROTOCOL_BINDING
    assert interfaces[0]["protocolVersion"] == mock_server.A2A_PROTOCOL_VERSION
    # Absolute and under the server's own base, as a deployed engine advertises,
    # so the UI proxy's `_rewrite_card_to_same_origin` has something to rewrite. A relative
    # URL here would leave that rewrite unexercised in the one configuration a
    # developer runs every day.
    assert interfaces[0]["url"] == f"http://mock/a2a/{_CHAT_APP}"


def test_a_turn_opens_with_its_task_and_closes_on_a_terminal_state(
    chat_app,
) -> None:
    results = _say(chat_app, "hello")
    assert len(results) > 1, "a single frame exercises no streaming"
    # `extractTaskId` reads `id` off a task frame and `taskId` off every other
    # kind. The UI needs it mid-turn, which is the whole reason for this frame.
    task = results[0]["task"]
    assert task["id"] and task["contextId"] == "ctx"
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert (
        results[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    )
    assert all(f["taskId"] == task["id"] for f in _artifact_frames(results))


def test_the_prose_rides_on_artifact_updates(chat_app) -> None:
    """Text streamed as status messages alone would arrive twice: the UI proxy
    reads a turn with no artifact frame as one Agent Runtime dropped, fetches the
    finished task and synthesizes them (`_fetch_task_artifact_frames`)."""
    frames = _artifact_frames(_say(chat_app, "hello"))
    assert len(frames) > 1, (
        "one frame is not a stream, and skips the typing state"
    )
    assert frames[0]["append"] is False
    assert [f["lastChunk"] for f in frames] == [False] * (len(frames) - 1) + [
        True
    ]


def test_the_chunks_rejoin_into_exactly_the_reply(chat_app) -> None:
    """The client concatenates consecutive text deltas verbatim, so a chunker
    that dropped the spaces would render one run-on word and read as a font
    bug. The completed task holds the reply whole; the stream must match it."""
    results = _say(chat_app, "which insights are worst?", context_id="chunks")
    task = _rpc(chat_app, "GetTask", {"id": results[0]["task"]["id"]}).json()[
        "result"
    ]

    assert _streamed_text(results) == task["artifacts"][0]["parts"][0]["text"]


def test_the_replies_are_keyed_off_the_dataset(chat_app, chat_data) -> None:
    """A canned reply that disagreed with the panels would be worse than none."""
    worst = max(chat_data.insights, key=lambda i: i["trace_count"])
    assert worst["label"] in _streamed_text(
        _say(chat_app, "what insights are there?")
    )

    newest = chat_data.runs[-1]
    assert newest["run_id"] in _streamed_text(
        _say(chat_app, "how did the last run go?")
    )


def test_an_unrecognised_message_says_what_it_knows(
    chat_app, chat_data
) -> None:
    reply = _streamed_text(_say(chat_app, "good morning"))
    assert str(len(chat_data.insights)) in reply
    assert mock_server.CONFIRM_KEYWORD in reply


def test_the_confirmation_card_leaves_the_task_awaiting_an_answer(
    chat_app,
) -> None:
    results = _say(
        chat_app, f"start a {mock_server.CONFIRM_KEYWORD}", context_id="hitl"
    )
    status = results[-1]["statusUpdate"]["status"]
    # INPUT_REQUIRED is terminal for the client -- the turn is over until the
    # user answers -- and a non-terminal state here leaves `busy` stuck on.
    assert status["state"] == "TASK_STATE_INPUT_REQUIRED"
    # The card is read off `status.message`, not off an artifact: that is the
    # only place `task-to-segments` looks, and only while the task is awaiting.
    part = status["message"]["parts"][0]
    assert part["metadata"]["adk_type"] == "function_call"
    assert part["data"]["name"] == mock_server.CONFIRMATION_TOOL
    assert part["data"]["args"]["toolConfirmation"]["hint"]


def _card(app: Any, context_id: str) -> tuple[str, str]:
    """Generates a confirmation card and returns `(task_id, call_id)`.

    Args:
        app: ASGI application instance.
        context_id: Context identifier for the turn.

    Returns:
        Tuple of `(task_id, call_id)` strings.
    """
    results = _say(
        app, f"run a {mock_server.CONFIRM_KEYWORD}", context_id=context_id
    )
    status = results[-1]["statusUpdate"]
    return status["taskId"], status["status"]["message"]["parts"][0]["data"][
        "id"
    ]


def _answer(
    app: Any, call_id: str, *, confirmed: bool, context_id: str
) -> list[dict]:
    """Sends a confirmation card response message to the chat endpoint.

    Args:
        app: ASGI application instance.
        call_id: Function call identifier being answered.
        confirmed: Boolean confirmation decision.
        context_id: Context identifier for the conversation.

    Returns:
        List of frame result dictionaries from the continuation turn.
    """
    return _turn(
        app,
        {
            "messageId": f"m-{call_id}",
            "contextId": context_id,
            "role": "ROLE_USER",
            "parts": [
                {
                    "data": {
                        "id": call_id,
                        "name": mock_server.CONFIRMATION_TOOL,
                        "response": {"confirmed": confirmed, "payload": None},
                    },
                    "metadata": {"adk_type": "function_response"},
                }
            ],
        },
    )


def test_approving_opens_its_own_task_and_schedules_a_run(
    chat_app, chat_data
) -> None:
    task_id, call_id = _card(chat_app, "approve")
    before = len(chat_data.runs)

    results = _answer(chat_app, call_id, confirmed=True, context_id="approve")

    # A task of its own, not a continuation of the one that asked. The client
    # sends `taskId: ""`, and answering into the asking task's id makes the
    # reply disappear: the history-versus-live merge drops an optimistic bubble
    # the moment a canonical copy of its task lands, and that task has one.
    # Found in the browser, which is the only place it shows.
    assert results[0]["task"]["id"] != task_id
    assert (
        results[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    )
    assert len(chat_data.runs) == before + 1
    assert chat_data.runs[-1]["run_id"] in _streamed_text(results)
    assert chat_data.runs[-1]["status"] == "pending"


def test_the_answer_rides_in_its_own_tasks_history(chat_app) -> None:
    """Which is what resolves the card after a reload: `task-to-segments` reads
    the `adk_request_confirmation` function_response out of history, and
    `resolveHitlSegments` matches it back to the card by call id."""
    _, call_id = _card(chat_app, "replay-hitl")
    results = _answer(
        chat_app, call_id, confirmed=True, context_id="replay-hitl"
    )

    task = _rpc(chat_app, "GetTask", {"id": results[0]["task"]["id"]}).json()[
        "result"
    ]

    part = task["history"][0]["parts"][0]
    assert part["metadata"]["adk_type"] == "function_response"
    assert part["data"]["id"] == call_id


def test_declining_schedules_nothing(chat_app, chat_data) -> None:
    _, call_id = _card(chat_app, "decline")
    before = len(chat_data.runs)

    results = _answer(chat_app, call_id, confirmed=False, context_id="decline")

    assert len(chat_data.runs) == before
    assert "Nothing was scheduled" in _streamed_text(results)


def _goal_card(app: Any, text: str, context_id: str) -> dict[str, Any]:
    """Asks for a goal card and returns its `adk_request_confirmation` call."""
    results = _say(app, text, context_id=context_id)
    status = results[-1]["statusUpdate"]["status"]
    assert status["state"] == "TASK_STATE_INPUT_REQUIRED"
    return status["message"]["parts"][0]["data"]


def test_a_goal_card_carries_the_set_goal_call_and_its_text(chat_app) -> None:
    """The dashboard's card shows the goal from `originalFunctionCall.args`,
    as it does against the real agent."""
    call = _goal_card(
        chat_app,
        "goal: Look hardest at refunds.\nTone does not matter.",
        "goal-card",
    )

    original = call["args"]["originalFunctionCall"]
    assert original == {
        "name": "set_goal",
        "args": {"goal": "Look hardest at refunds.\nTone does not matter."},
    }
    assert (
        call["args"]["toolConfirmation"]["hint"]
        == "Save this as the developer goal?"
    )


def test_approving_a_goal_card_saves_that_goal(chat_app, chat_data) -> None:
    call = _goal_card(
        chat_app, "goal: Look hardest at refunds.", "goal-approve"
    )

    results = _answer(
        chat_app, call["id"], confirmed=True, context_id="goal-approve"
    )

    assert chat_data.goal == "Look hardest at refunds."
    assert "Saved" in _streamed_text(results)


def test_declining_a_goal_card_saves_nothing(chat_app, chat_data) -> None:
    before = chat_data.goal
    call = _goal_card(
        chat_app, "goal: Look hardest at refunds.", "goal-decline"
    )

    _answer(chat_app, call["id"], confirmed=False, context_id="goal-decline")

    assert chat_data.goal == before


def test_approving_a_removal_card_removes_the_goal(chat_app, chat_data) -> None:
    mock_server._set_goal(chat_data, {"goal": "The real goal."})
    call = _goal_card(chat_app, "remove goal", "goal-remove")
    assert call["args"]["originalFunctionCall"]["args"] == {"goal": ""}
    assert (
        call["args"]["toolConfirmation"]["hint"] == "Remove the developer goal?"
    )

    _answer(chat_app, call["id"], confirmed=True, context_id="goal-remove")

    assert mock_server._get_goal(chat_data)["goal"] is None


def test_remember_stores_a_memory_from_this_chat(chat_app, chat_data) -> None:
    """The mock has no model to decide to call remember, so a message
    starting with "remember:" does what the agent's tool does."""
    text = "The booking tool rejects dates in the past."

    reply = _streamed_text(
        _say(chat_app, f"remember: {text}", context_id="learn-1")
    )

    row = next(x for x in chat_data.memories if x["text"] == text)
    assert row["source"] == "learn-1"
    assert row["id"] in {
        r["id"] for r in mock_server._list_memories(chat_data)["memories"]
    }
    assert "Memory card" in reply

    again = _streamed_text(
        _say(chat_app, f"remember: {text}", context_id="learn-2")
    )
    assert "already" in again
    assert [x["text"] for x in chat_data.memories].count(text) == 1


def test_get_task_replays_the_turn(chat_app) -> None:
    """The dashboard refetches a task once the turn ends and rebuilds the bubble
    from it, reading the prose off `artifacts` and the turns off `history`."""
    results = _say(chat_app, "hello again", context_id="replay")

    task = _rpc(chat_app, "GetTask", {"id": results[0]["task"]["id"]}).json()[
        "result"
    ]

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0]["text"] == _streamed_text(results)
    assert [m["role"] for m in task["history"]] == ["ROLE_USER"]


def test_get_task_answers_the_v0_3_spelling_too(chat_app) -> None:
    """The UI proxy issues a `GetTask` of its own for artifact repair, and its
    `_STREAMING_METHODS` shows both dialects reach this endpoint."""
    task_id = _say(chat_app, "hello", context_id="dialect")[0]["task"]["id"]

    assert (
        _rpc(chat_app, "tasks/get", {"id": task_id}).json()["result"]["id"]
        == task_id
    )


def test_cancelling_marks_the_task_cancelled(chat_app) -> None:
    task_id = _say(chat_app, "hello", context_id="cancel")[0]["task"]["id"]

    task = _rpc(chat_app, "CancelTask", {"id": task_id}).json()["result"]

    assert task["status"]["state"] == "TASK_STATE_CANCELED"


def test_an_unmocked_method_is_an_error_not_an_empty_stream(chat_app) -> None:
    """Nothing calls resubscribe yet. An empty stream renders as a reply that
    never arrives; a JSON-RPC error names the gap instead."""
    body = _rpc(chat_app, "SubscribeToTask", {"id": "whatever"}).json()

    assert body["error"]["code"] == -32601
    assert "SubscribeToTask" in body["error"]["message"]


def test_an_unknown_task_is_reported_rather_than_invented(chat_app) -> None:
    assert "error" in _rpc(chat_app, "GetTask", {"id": "no-such-task"}).json()


# --------------------------------------------------------------------------- #
# Verification results, and the diagnosis the dashboard makes of them          #
# --------------------------------------------------------------------------- #


def test_no_sighting_is_diagnosed_by_default() -> None:
    """Because none is in production: `verify_clusters` exists and nothing calls
    it, so no occurrence in BigQuery carries an analysis. A mock that generated
    them by default would demo a dashboard nobody can actually get."""
    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())
    assert all(not o["analyses"] for o in dataset["occurrences"])


def test_the_knob_produces_results_the_agent_model_accepts() -> None:
    from ambient_quality_agent.tools.insights.models import (
        VERIFICATION_ANALYSIS_KEY,
        ClusterVerification,
    )

    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, verified_rate=1.0
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())

    assert dataset["occurrences"]
    for occurrence in dataset["occurrences"]:
        analysis = occurrence["analyses"][VERIFICATION_ANALYSIS_KEY]
        verification = ClusterVerification.model_validate(analysis)
        assert verification.explanation
        # A rejected cluster produces no insight, so a sighting carrying that
        # verdict is a state the pipeline cannot reach.
        assert verification.valid


def test_the_explanation_describes_the_sighting_it_is_attached_to() -> None:
    """Composed from the defect rather than written separately, so the prose and
    the rubrics under it cannot describe two different defects -- the failure a
    hand-written fixture makes and nobody notices, since each half reads fine."""
    from ambient_quality_agent.tools.insights.models import (
        VERIFICATION_ANALYSIS_KEY,
    )

    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, verified_rate=1.0
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())

    for occurrence in dataset["occurrences"]:
        analysis = occurrence["analyses"][VERIFICATION_ANALYSIS_KEY]
        assert occurrence["label"] in analysis["explanation"]


def test_the_knob_changes_the_analyses_and_nothing_else() -> None:
    """So one seed is one deployment at any rate, and its diagnosed and
    undiagnosed renderings can be put side by side. The verification draws from
    a stream of its own for this: taken from the shared one, they shifted a case
    id sampled into a run's event prose."""
    kwargs = {"days": 7, "runs_per_day": 2, "seed": 5, "anchor": _ANCHOR}
    plain = mock_data.build_dataset(
        mock_data.Scenario(**kwargs), mock_data.load()
    )
    verified = mock_data.build_dataset(
        mock_data.Scenario(**kwargs, verified_rate=1.0), mock_data.load()
    )

    assert plain["runs"] == verified["runs"]
    assert plain["insights"] == verified["insights"]
    without = lambda d: [  # noqa: E731
        {k: v for k, v in o.items() if k != "analyses"}
        for o in d["occurrences"]
    ]
    assert without(plain) == without(verified)


def test_a_run_event_names_the_same_cases_in_any_process() -> None:
    """`hash()` salts strings per process, so the built-in made this one field
    differ between two runs of the same seed -- quietly breaking `--seed` and
    `MOCK_KEEP`, whose purpose is a fixed fixture to compare renderings against.
    This test cannot catch a regression on its own (one process, one salt), so
    it pins the derivation instead."""
    first = mock_data._compute_stable_case_id("some-defect", 12)
    assert first == mock_data._compute_stable_case_id("some-defect", 12)
    assert first != mock_data._compute_stable_case_id("some-defect", 13)
    # Fixed by construction, not merely stable within this interpreter.
    assert first == "6386f5"


# --------------------------------------------------------------------------- #
# The goal and the memories                                                   #
# --------------------------------------------------------------------------- #


def test_the_document_paths_match_the_agent() -> None:
    """The mock restates them rather than importing the agent. Nothing but this
    holds the copies level, and a rename would leave the config page linking at
    a path nothing is stored under."""
    from ambient_quality_agent.tools.documents import goal, memories

    assert mock_server.GOAL_OBJECT == goal.GOAL_OBJECT
    assert mock_server.MEMORIES_PREFIX == memories.MEMORIES_PREFIX
    assert mock_server.MAX_MEMORY_CHARS == memories.MAX_MEMORY_CHARS


def test_the_generated_memory_ids_are_the_agents(dataset) -> None:
    """A dashboard delete addresses a memory by id, so a fixture whose ids
    were derived differently would exercise a delete that cannot happen."""
    from ambient_quality_agent.tools.documents.memories import (
        compute_memory_id,
    )

    assert dataset["memories"]
    for row in dataset["memories"]:
        assert row["id"] == compute_memory_id(row["text"])


def test_the_goal_is_served_and_replaced(dataset) -> None:
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    assert mock_server._get_goal(data)["goal"].startswith(
        "The travel desk agent"
    )

    assert mock_server._set_goal(data, {"goal": "  A new goal.  "}) == {
        "goal": "A new goal.",
        "version": mock_server._compute_goal_version("A new goal."),
    }
    assert mock_server._get_goal(data)["goal"] == "A new goal."
    assert mock_server._get_goal(data)[
        "version"
    ] == mock_server._compute_goal_version("A new goal.")


def test_goal_versions_follow_every_save(dataset) -> None:
    """The mock keeps the history the agent keeps, so restoring a goal from the
    Goals card can be tried locally."""
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    original = mock_server._get_goal(data)["goal"]
    [only] = mock_server._list_goal_versions(data)["versions"]
    assert (only["text"], only["active"]) == (original, True)
    assert set(only) == {
        "version",
        "text",
        "created_at",
        "last_activated_at",
        "active",
    }

    mock_server._set_goal(data, {"goal": "A new goal."})
    mock_server._set_goal(data, {"goal": original})

    versions = mock_server._list_goal_versions(data)["versions"]
    assert [(v["text"], v["active"]) for v in versions] == [
        (original, True),
        ("A new goal.", False),
    ]
    assert versions[0]["version"] == mock_server._compute_goal_version(original)
    assert versions[0]["created_at"] == only["created_at"]


def test_the_mock_goal_rules_match_the_agent() -> None:
    """The mock restates the version id and the size cap rather than importing
    them; a goal must get the same id and the same refusal in both."""
    from ambient_quality_agent.tools.documents import goal

    for text in ("A new goal.", "  padded \n", "é" * 10):
        assert mock_server._compute_goal_version(
            text
        ) == goal.compute_goal_version(text)
    assert mock_server.GOAL_MAX_BYTES == goal.GOAL_MAX_BYTES


def test_an_oversized_goal_is_refused_here_too(dataset) -> None:
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    result = mock_server._set_goal(
        data, {"goal": "x" * (mock_server.GOAL_MAX_BYTES + 1)}
    )
    assert "8 KiB" in result["error"]


def test_an_empty_goal_removes_it_here_too(dataset) -> None:
    """As in the agent: saving an empty goal leaves no goal, and the removed
    text stays a version that can be restored."""
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    mock_server._set_goal(data, {"goal": "The real goal."})

    assert mock_server._set_goal(data, {"goal": "   "}) == {
        "goal": "",
        "version": None,
    }

    assert mock_server._get_goal(data)["goal"] is None
    versions = mock_server._list_goal_versions(data)["versions"]
    assert "The real goal." in [v["text"] for v in versions]
    assert not any(v["active"] for v in versions)


def test_memories_are_served_newest_first(dataset) -> None:
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    rows = mock_server._list_memories(data)["memories"]
    assert [r["created_at"] for r in rows] == sorted(
        (r["created_at"] for r in rows), reverse=True
    )
    assert set(rows[0]) == {"id", "text", "source", "created_at"}


def test_deleting_a_memory_mutates_the_dataset(dataset) -> None:
    """Not `{"ok": true}` over an unchanged fixture: the row would come back on
    the next read and the delete would look broken when the mock was."""
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    rows = mock_server._list_memories(data)["memories"]
    target = rows[0]["id"]

    assert mock_server._delete_memory(data, {"memory_id": target}) == {
        "deleted": True
    }

    remaining = [r["id"] for r in mock_server._list_memories(data)["memories"]]
    assert target not in remaining
    assert len(remaining) == len(rows) - 1


def test_deleting_an_unknown_memory_reports_it(dataset) -> None:
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    assert "error" in mock_server._delete_memory(data, {"memory_id": "nope"})


def test_the_health_vocabulary_is_the_agents(dataset) -> None:
    """The hand-copy problem again: a verdict renamed in the agent and forgotten
    here would reach the badge as a state it has no branch for."""
    from ambient_quality_agent.tools.investigations import health, models

    assert mock_server.WATCHING == health.WATCHING
    assert mock_server.OVERDUE == health.OVERDUE
    assert mock_server.NEVER == health.NEVER
    assert mock_server.IN_FLIGHT == {s.value for s in health.IN_FLIGHT}
    assert mock_server.IN_FLIGHT <= {s.value for s in models.RunStatus}


def test_health_answers_in_the_shape_the_badge_reads(dataset) -> None:
    """Crude verdict, exact envelope: the badge reads these keys whichever
    server answered, so a missing one is a broken card, not a wrong color."""
    from ambient_quality_agent.tools.investigations import health

    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    payload = mock_server._get_ambient_health(data)

    assert set(payload) == {f.name for f in dataclasses.fields(health.Health)}
    assert payload["reason"]
    # The generated month has finished sweeps and open insights, so the badge
    # has something to say about both.
    assert payload["last_run"]["run_id"]
    assert payload["open_findings"] > 0
    assert payload["worst_finding"]["insight_id"]


def test_a_sweep_in_flight_turns_the_badge_amber(dataset) -> None:
    """The lever the mock exists to give: ▶ Run investigation makes the badge
    move, and it settles back on its own."""
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    # The generator leaves its newest sweep running; settle it so the fixture
    # starts from rest.
    for run in data.runs:
        if str(run.get("status")) in mock_server.IN_FLIGHT:
            run.update(
                {
                    "status": "done",
                    "finished_at": mock_server._format_utc_now_iso(),
                }
            )
    assert (
        mock_server._get_ambient_health(data)["verdict"] == mock_server.WATCHING
    )

    _schedule_investigation_without_settling(data)

    assert (
        mock_server._get_ambient_health(data)["verdict"] == mock_server.OVERDUE
    )


def test_health_with_no_finished_run_is_never() -> None:
    empty = {"runs": [], "insights": [], "version": mock_server.DATASET_VERSION}
    data = mock_server.Dataset(empty)
    assert mock_server._get_ambient_health(data)["verdict"] == mock_server.NEVER


def test_a_stale_sweep_does_not_pin_the_badge_amber(dataset) -> None:
    """The fixture's newest sweep is left in flight and is never settled. Counted
    as running, it would hold the badge amber and make `watching` unreachable."""
    data = mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)
    stale = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=mock_server.STALE_AFTER_SECONDS + 60
    )
    for run in data.runs:
        if str(run.get("status")) in mock_server.IN_FLIGHT:
            run["created_at"] = stale.isoformat()

    assert (
        mock_server._get_ambient_health(data)["verdict"] == mock_server.WATCHING
    )


def _schedule_investigation_without_settling(data) -> None:
    """Queues a run and keeps it pending without settling.

    Args:
        data: Mock Dataset instance to queue the run on.
    """
    mock_server._schedule_investigation(data)
    data._settling.clear()


# --------------------------------------------------------------------------- #
# The source snapshot                                                          #
# --------------------------------------------------------------------------- #


def test_the_publishing_caps_are_the_agents() -> None:
    """The hand-copy problem once more. `--source-dir` counts a real tree under
    these, so a cap that drifted would put the card's amber "truncated" note at
    a size a deployment never reaches."""
    from ambient_quality_agent.tools.source_code import snapshot

    assert mock_data.MAX_FILE_BYTES == snapshot.MAX_FILE_BYTES
    assert mock_data.MAX_SNAPSHOT_BYTES == snapshot.MAX_SNAPSHOT_BYTES


def test_the_source_card_reads_a_published_snapshot(served) -> None:
    """A deployment publishes at deploy time, so the card displays the
    published snapshot."""
    body = mock_server._get_source_snapshot(served)

    assert body["available"] is True
    assert body["revision"]
    assert body["file_count"] > 0
    assert body["uri"].endswith(f"/{body['revision']}/manifest.json")


def test_an_unpublished_deployment_is_the_other_card() -> None:
    dataset = mock_data.build_dataset(
        mock_data.Scenario(days=2, runs_per_day=1, source_published=False),
        mock_data.load(),
    )

    body = mock_server._get_source_snapshot(mock_server.Dataset(dataset))

    assert body["available"] is False
    assert mock_server.MOCK_SOURCE_BUCKET in body["reason"]


def test_a_source_directory_is_summarized_as_itself(tmp_path) -> None:
    """The point of `--source-dir`: the counts are that tree's, not invented."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "agent.py").write_text("x" * 100)
    (tmp_path / "README.md").write_text("y" * 50)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ignored")

    dataset = mock_data.build_dataset(
        mock_data.Scenario(days=2, runs_per_day=1, source_dir=str(tmp_path)),
        mock_data.load(),
    )

    assert dataset["source"]["file_count"] == 2
    assert dataset["source"]["total_bytes"] == 150
    assert dataset["source"]["agent_directory"] == tmp_path.name


def test_a_file_over_the_cap_is_reported_as_truncated(tmp_path) -> None:
    """The amber state a synthesized fixture never reaches."""
    (tmp_path / "big.bin").write_bytes(b"z" * (mock_data.MAX_FILE_BYTES + 1))
    (tmp_path / "small.py").write_text("ok")

    dataset = mock_data.build_dataset(
        mock_data.Scenario(days=2, runs_per_day=1, source_dir=str(tmp_path)),
        mock_data.load(),
    )

    assert dataset["source"]["truncated_files"] == 1
    assert dataset["source"]["total_bytes"] == mock_data.MAX_FILE_BYTES + 2


# --------------------------------------------------------------------------- #
# Recorded root causes                                                         #
# --------------------------------------------------------------------------- #


def test_no_insight_is_diagnosed_by_default() -> None:
    """Verifies generated datasets contain no root causes when root_cause_rate is 0.0."""
    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())

    assert dataset["root_causes"] == []
    assert all(not i["has_root_cause"] for i in dataset["insights"])


def test_the_knob_produces_records_the_agent_model_accepts() -> None:
    from ambient_quality_agent.tools.insights.models import RootCause

    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, root_cause_rate=1.0
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())

    assert dataset["root_causes"]
    for record in dataset["root_causes"]:
        parsed = RootCause.model_validate(record)
        assert parsed.summary
        assert parsed.edits
        assert parsed.edits[0].before and parsed.edits[0].after


def test_a_diagnosed_insight_is_marked_in_the_list_view() -> None:
    """Verifies has_root_cause on insights matches the presence of root-cause records."""
    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, root_cause_rate=1.0
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())

    diagnosed = {r["insight_id"] for r in dataset["root_causes"]}
    marked = {
        i["insight_id"] for i in dataset["insights"] if i["has_root_cause"]
    }
    assert marked == diagnosed


def test_a_record_names_a_sighting_the_dataset_holds() -> None:
    """Verifies root causes reference the most recent occurrence of each insight."""
    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, root_cause_rate=1.0
    )
    dataset = mock_data.build_dataset(scenario, mock_data.load())

    newest: dict[str, dict[str, Any]] = {}
    for occurrence in dataset["occurrences"]:
        current = newest.get(occurrence["insight_id"])
        if current is None or occurrence["created_at"] > current["created_at"]:
            newest[occurrence["insight_id"]] = occurrence
    for record in dataset["root_causes"]:
        assert (
            record["occurrence_id"]
            == newest[record["insight_id"]]["occurrence_id"]
        )


def test_the_knob_changes_the_root_causes_and_nothing_else() -> None:
    """Verifies root_cause_rate does not alter runs, occurrences, or other insight fields."""
    kwargs = {"days": 7, "runs_per_day": 2, "seed": 5, "anchor": _ANCHOR}
    plain = mock_data.build_dataset(
        mock_data.Scenario(**kwargs), mock_data.load()
    )
    diagnosed = mock_data.build_dataset(
        mock_data.Scenario(**kwargs, root_cause_rate=1.0), mock_data.load()
    )

    assert plain["runs"] == diagnosed["runs"]
    assert plain["occurrences"] == diagnosed["occurrences"]
    without = lambda d: [  # noqa: E731
        {k: v for k, v in i.items() if k != "has_root_cause"}
        for i in d["insights"]
    ]
    assert without(plain) == without(diagnosed)


# --------------------------------------------------------------------------- #
# ...and what the server does with them                                        #
# --------------------------------------------------------------------------- #
# Verify the mock server endpoints expose root-cause records matching the production API.


@pytest.fixture(scope="module")
def diagnosed_served() -> Any:
    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, root_cause_rate=1.0
    )
    return mock_server.Dataset(
        mock_data.build_dataset(scenario, mock_data.load()), settle_seconds=0
    )


def _diagnosed_insight_id(served: Any) -> str:
    """Return the ID of an insight that has a recorded root cause.

    Args:
        served: Mock server dataset fixture.

    Returns:
        Insight ID string.
    """
    return next(i["insight_id"] for i in served.insights if i["has_root_cause"])


def test_get_insight_serves_the_records_for_the_insight(
    diagnosed_served,
) -> None:
    insight_id = _diagnosed_insight_id(diagnosed_served)
    detail = mock_server._get_insight(
        diagnosed_served, {"insight_id": insight_id}
    )

    assert detail["root_causes"]
    assert all(r["insight_id"] == insight_id for r in detail["root_causes"])
    assert all("summary" in r and "edits" in r for r in detail["root_causes"])


def test_each_record_is_attached_to_the_sighting_it_names(
    diagnosed_served,
) -> None:
    insight_id = _diagnosed_insight_id(diagnosed_served)
    detail = mock_server._get_insight(
        diagnosed_served, {"insight_id": insight_id}
    )

    attached = [
        (o["occurrence_id"], record)
        for o in detail["occurrences"]
        for record in o["root_causes"]
    ]
    assert attached, "the generator writes against the newest sighting"
    assert all(o_id == record["occurrence_id"] for o_id, record in attached)


def test_a_record_carries_its_edit_count(diagnosed_served) -> None:
    """Verify edit_count is present to match production serialization."""
    insight_id = _diagnosed_insight_id(diagnosed_served)
    detail = mock_server._get_insight(
        diagnosed_served, {"insight_id": insight_id}
    )

    for record in detail["root_causes"]:
        assert record["edit_count"] == len(record["edits"])


def test_declining_the_edits_drops_the_code_and_keeps_the_count(
    diagnosed_served,
) -> None:
    insight_id = _diagnosed_insight_id(diagnosed_served)
    declined = mock_server._get_insight(
        diagnosed_served, {"insight_id": insight_id, "include_edits": False}
    )

    for record in declined["root_causes"]:
        assert record["edit_count"] >= 1
        for edit in record["edits"]:
            assert "before" not in edit and "after" not in edit
            # Verify file location metadata is preserved when diff bodies are stripped.
            assert edit["path"] and edit["start_line"]


def test_the_list_filters_on_has_root_cause(diagnosed_served) -> None:
    every = mock_server._list_insights(diagnosed_served, {})["total"]
    diagnosed = mock_server._list_insights(
        diagnosed_served, {"has_root_cause": "true"}
    )
    undiagnosed = mock_server._list_insights(
        diagnosed_served, {"has_root_cause": "false"}
    )

    assert diagnosed["total"] + undiagnosed["total"] == every
    assert diagnosed["total"] > 0
    assert all(i["has_root_cause"] for i in diagnosed["insights"])
    assert not any(i["has_root_cause"] for i in undiagnosed["insights"])


def test_an_empty_has_root_cause_filters_nothing(diagnosed_served) -> None:
    """Verify an empty sentinel disables root-cause filtering."""
    every = mock_server._list_insights(diagnosed_served, {})["total"]
    filtered = mock_server._list_insights(
        diagnosed_served, {"has_root_cause": ""}
    )
    assert filtered["total"] == every


def test_an_unreadable_has_root_cause_is_refused(diagnosed_served) -> None:
    page = mock_server._list_insights(
        diagnosed_served, {"has_root_cause": "maybe"}
    )
    assert "invalid has_root_cause" in page["error"]


def _dataset_with_records(records: list[dict[str, Any]]) -> Any:
    """Build a Dataset holding only root-cause records.

    Args:
        records: Root-cause record dictionaries to serve.

    Returns:
        Mock server Dataset.
    """
    return mock_server.Dataset(
        {"version": mock_server.DATASET_VERSION, "root_causes": records},
        settle_seconds=0,
    )


def _record(
    root_cause_id: str, occurrence_id: str, created_at: str
) -> dict[str, Any]:
    """Build a root-cause record carrying only the fields dedup reads.

    Args:
        root_cause_id: Record identifier, the tie-break on equal timestamps.
        occurrence_id: Sighting the record diagnoses.
        created_at: ISO timestamp the record was written at.

    Returns:
        Root-cause record dictionary.
    """
    return {
        "root_cause_id": root_cause_id,
        "insight_id": "ins-1",
        "occurrence_id": occurrence_id,
        "created_at": created_at,
    }


def test_only_the_newest_record_per_sighting_is_served() -> None:
    """The dashboard's "latest of N" note reads this, so it must dedup as the
    reader's QUALIFY does."""
    data = _dataset_with_records(
        [
            _record("rc-old", "occ-1", "2026-06-01T00:00:00Z"),
            _record("rc-new", "occ-1", "2026-06-02T00:00:00Z"),
            _record("rc-other", "occ-2", "2026-06-03T00:00:00Z"),
        ]
    )

    served = data.list_root_causes("ins-1")

    assert [r["root_cause_id"] for r in served] == ["rc-other", "rc-new"]


def test_records_written_in_the_same_instant_resolve_to_the_lowest_id() -> None:
    """Matches the reader's `ORDER BY created_at DESC, root_cause_id`."""
    data = _dataset_with_records(
        [
            _record("rc-b", "occ-1", "2026-06-01T00:00:00Z"),
            _record("rc-a", "occ-1", "2026-06-01T00:00:00Z"),
        ]
    )

    assert [r["root_cause_id"] for r in data.list_root_causes("ins-1")] == [
        "rc-a"
    ]


def test_an_undiagnosed_dataset_serves_an_empty_records_list(served) -> None:
    """Verify unrecorded root causes return empty lists rather than missing keys."""
    insight_id = served.insights[0]["insight_id"]
    detail = mock_server._get_insight(served, {"insight_id": insight_id})

    assert detail["root_causes"] == []
    assert all(o["root_causes"] == [] for o in detail["occurrences"])


# --------------------------------------------------------------------------- #
# The case conversation                                                        #
# --------------------------------------------------------------------------- #


def test_the_payload_vocabulary_is_the_agents() -> None:
    """The hand-copy problem once more: the dashboard branches on these, so a
    status the mock invented would demo a state production cannot produce."""
    from ambient_quality_agent.tools.trajectories import payloads
    from ambient_quality_agent.tools.trajectories.models import PayloadStatus

    assert set(mock_server.PAYLOAD_STATUSES) == {s.value for s in PayloadStatus}
    assert mock_server.NOT_ARCHIVED == payloads.NOT_ARCHIVED


def test_a_generated_turn_is_a_real_conversation_turn() -> None:
    """Built by hand here rather than through the model, so the shape is pinned
    against the SDK type the payload table actually holds."""
    from agentplatform._genai.types.evals import ConversationTurn

    case = mock_server._build_conversation("case-abc123", _ANCHOR)

    for turn in case["turns"]:
        parsed = ConversationTurn.model_validate(turn)
        assert parsed.events


def test_a_conversation_is_the_same_on_every_read() -> None:
    """Derived from the id rather than stored, so it has to be stable -- the
    page is re-read on every navigation."""
    assert mock_server._build_conversation(
        "case-abc123", _ANCHOR
    ) == mock_server._build_conversation("case-abc123", _ANCHOR)


def test_a_tool_call_sits_between_the_text_around_it() -> None:
    """The interleaving is the thing the dashboard renders and the thing a flat
    two-table copy cannot express."""
    events = mock_server._build_conversation("case-abc123", _ANCHOR)["turns"][
        0
    ]["events"]
    kinds = [next(iter(e["content"]["parts"][0])) for e in events]

    assert kinds.index("function_call") > kinds.index("text")
    assert kinds.index("function_response") < len(kinds) - 1


def test_both_degradations_are_reachable() -> None:
    """A conversation nobody archived and one whose turns expired, neither of
    which a healthy fixture would otherwise show."""
    seen = {
        mock_server._build_conversation(f"case-{n:06x}", _ANCHOR)["status"]
        for n in range(200)
    }
    assert mock_server.NOT_ARCHIVED in seen

    expired = [
        c
        for n in range(200)
        if (c := mock_server._build_conversation(f"case-{n:06x}", _ANCHOR))[
            "turn_count"
        ]
        > c["turns_returned"]
    ]
    assert expired


def test_the_case_route_answers_the_shape_the_agent_does(served) -> None:
    case_id = served.occurrences[0]["trajectory_ids"][0]

    body = mock_server._get_case_conversation(
        served,
        {"trajectory_id": case_id, "run_id": served.occurrences[0]["run_id"]},
    )

    assert set(body["case"]) >= {
        "trajectory_id",
        "status",
        "turn_count",
        "turns_returned",
        "turns",
        "agents",
        "recorded_at",
    }
    assert body["case"]["trajectory"]["trajectory_id"] == case_id


def test_the_case_route_needs_an_id() -> None:
    assert "error" in mock_server._get_case_conversation(None, {})


# --------------------------------------------------------------------------- #
# The two operator judgements                                                  #
# --------------------------------------------------------------------------- #
# Both mutate the dataset, for the reason `_delete_memory` does: a stub that
# reported success and changed nothing would show the row again on the next
# read, and the button would look broken when it was the mock that was.


@pytest.fixture
def judged(dataset: dict[str, Any]) -> Any:
    """A private copy of the fixture, because these tests write to it."""
    return mock_server.Dataset(copy.deepcopy(dataset), settle_seconds=0)


def _visible_ids(data: Any) -> list[str]:
    return [
        i["insight_id"]
        for i in mock_server._list_insights(data, {})["insights"]
    ]


def test_the_served_stats_reproduce_the_generators_own_numbers(judged) -> None:
    """The list recomputes the four aggregate fields over the sightings each
    insight owns, so that a merge moves them. Before anything is merged that
    recomputation has to land on exactly what the generator wrote, or every
    card in the demo is wrong by a different amount."""
    generated = {i["insight_id"]: i for i in judged.insights}

    for served_insight in mock_server._list_insights(judged, {})["insights"]:
        original = generated[served_insight["insight_id"]]
        for field in (
            "occurrence_count",
            "trace_count",
            "last_run_id",
            "last_run_at",
        ):
            assert served_insight[field] == original[field], field


def test_a_dismissed_insight_leaves_the_list_and_keeps_its_first_date(
    judged,
) -> None:
    target = _visible_ids(judged)[0]

    assert mock_server._dismiss_insight(judged, {"insight_id": target}) == {
        "dismissed": True
    }
    assert target not in _visible_ids(judged)

    dismissed_at = judged.get_insight(target)["dismissed_at"]
    # Idempotent, as the UPDATE's COALESCE is: a second click is not an error
    # and does not restamp the judgement.
    assert mock_server._dismiss_insight(judged, {"insight_id": target})[
        "dismissed"
    ]
    assert judged.get_insight(target)["dismissed_at"] == dismissed_at


def test_a_dismissed_insight_still_resolves_on_its_own_page(judged) -> None:
    target = _visible_ids(judged)[0]
    mock_server._dismiss_insight(judged, {"insight_id": target})

    detail = mock_server._get_insight(judged, {"insight_id": target})

    assert detail["insight"]["insight_id"] == target


def test_merging_moves_the_duplicates_sightings_into_the_targets_counts(
    judged,
) -> None:
    duplicate, target = _visible_ids(judged)[:2]
    before = mock_server._get_insight(judged, {"insight_id": target})["insight"]
    moved = mock_server._get_insight(judged, {"insight_id": duplicate})[
        "insight"
    ]

    assert mock_server._merge_insight(
        judged, {"insight_id": duplicate, "target_insight_id": target}
    ) == {"merged": True}

    assert duplicate not in _visible_ids(judged)
    after = mock_server._get_insight(judged, {"insight_id": target})["insight"]
    assert (
        after["occurrence_count"]
        == before["occurrence_count"] + moved["occurrence_count"]
    )
    assert after["trace_count"] == before["trace_count"] + moved["trace_count"]
    # And the duplicate now owns none of its own, so its page cannot claim
    # evidence its header counts as zero.
    emptied = mock_server._get_insight(judged, {"insight_id": duplicate})
    assert emptied["insight"]["occurrence_count"] == 0
    assert emptied["occurrences"] == []


@pytest.fixture
def judged_diagnosed() -> Any:
    """A writable dataset where some insights are diagnosed and some are not.

    Half-diagnosed on purpose: a merge has to be observable in both directions,
    and at `root_cause_rate=1.0` there is no undiagnosed target to move one into.
    """
    scenario = mock_data.Scenario(
        days=7, runs_per_day=2, seed=5, anchor=_ANCHOR, root_cause_rate=0.5
    )
    return mock_server.Dataset(
        mock_data.build_dataset(scenario, mock_data.load()), settle_seconds=0
    )


def test_merging_moves_the_duplicates_diagnosis_with_its_sightings(
    judged_diagnosed,
) -> None:
    """The evidence and the badge fold together. A target that absorbed a
    diagnosed insight's sightings but not its diagnosis would count traces it
    could not explain, and hide the explanation on a page off the list."""
    served = mock_server._list_insights(judged_diagnosed, {})["insights"]
    duplicate = next(i["insight_id"] for i in served if i["has_root_cause"])
    target = next(i["insight_id"] for i in served if not i["has_root_cause"])
    moved = mock_server._get_insight(
        judged_diagnosed, {"insight_id": duplicate}
    )
    assert moved["root_causes"], "the duplicate has to start out diagnosed"

    assert mock_server._merge_insight(
        judged_diagnosed, {"insight_id": duplicate, "target_insight_id": target}
    ) == {"merged": True}

    after = mock_server._get_insight(judged_diagnosed, {"insight_id": target})
    assert len(after["root_causes"]) == len(moved["root_causes"])
    assert after["insight"]["has_root_cause"] is True
    # Each record still names the insight it was written against, so the target
    # does not claim the diagnosis was made about it.
    assert {r["insight_id"] for r in after["root_causes"]} == {duplicate}
    # And the duplicate keeps none, exactly as it keeps none of its sightings.
    emptied = mock_server._get_insight(
        judged_diagnosed, {"insight_id": duplicate}
    )
    assert emptied["root_causes"] == []
    assert emptied["insight"]["has_root_cause"] is False


def test_the_diagnosed_filter_follows_the_merge_too(judged_diagnosed) -> None:
    """The filter and the badge are one claim; a row the list draws as diagnosed
    has to survive "diagnosed only"."""
    served = mock_server._list_insights(judged_diagnosed, {})["insights"]
    duplicate = next(i["insight_id"] for i in served if i["has_root_cause"])
    target = next(i["insight_id"] for i in served if not i["has_root_cause"])

    mock_server._merge_insight(
        judged_diagnosed, {"insight_id": duplicate, "target_insight_id": target}
    )

    diagnosed = mock_server._list_insights(
        judged_diagnosed, {"has_root_cause": "true"}
    )
    assert target in [i["insight_id"] for i in diagnosed["insights"]]
    undiagnosed = mock_server._list_insights(
        judged_diagnosed, {"has_root_cause": "false"}
    )
    assert target not in [i["insight_id"] for i in undiagnosed["insights"]]


def test_a_chain_flattens_so_one_hop_always_resolves_it(judged) -> None:
    first, second, third = _visible_ids(judged)[:3]

    mock_server._merge_insight(
        judged, {"insight_id": first, "target_insight_id": second}
    )
    mock_server._merge_insight(
        judged, {"insight_id": second, "target_insight_id": third}
    )

    assert judged.get_insight(first)["merged_into_insight_id"] == third
    assert judged.get_insight(second)["merged_into_insight_id"] == third
    assert judged.resolve_owner_id(first) == third


def test_merging_into_a_duplicate_is_refused(judged) -> None:
    """Which is what keeps the chain one hop deep. The dashboard cannot offer
    it -- duplicates are hidden from the list the target is picked from -- but
    these ids arrive over HTTP."""
    duplicate, target, other = _visible_ids(judged)[:3]
    mock_server._merge_insight(
        judged, {"insight_id": duplicate, "target_insight_id": target}
    )

    refused = mock_server._merge_insight(
        judged, {"insight_id": other, "target_insight_id": duplicate}
    )

    assert "error" in refused
    assert judged.get_insight(other)["merged_into_insight_id"] is None


def test_merging_an_insight_into_itself_is_refused(judged) -> None:
    target = _visible_ids(judged)[0]

    refused = mock_server._merge_insight(
        judged, {"insight_id": target, "target_insight_id": target}
    )

    assert "error" in refused
    assert target in _visible_ids(judged)


def test_a_merged_insight_still_shows_under_the_run_that_found_it(
    judged,
) -> None:
    """Under the target, since the duplicate is hidden. Resolving the run filter
    by owner too is what keeps a sweep's own view from listing neither."""
    duplicate, target = _visible_ids(judged)[:2]
    run_id = mock_server._get_insight(judged, {"insight_id": duplicate})[
        "occurrences"
    ][0]["run_id"]

    mock_server._merge_insight(
        judged, {"insight_id": duplicate, "target_insight_id": target}
    )

    in_run = {
        i["insight_id"]
        for i in mock_server._list_insights(judged, {"run_id": run_id})[
            "insights"
        ]
    }
    assert duplicate not in in_run
    assert target in in_run
