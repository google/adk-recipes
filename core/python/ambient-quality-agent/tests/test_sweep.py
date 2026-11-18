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

"""Tests for the shared sweep: one ingestion, several evaluations.

The producers reach this through their own nodes, which those tests cover. These
drive `run_sweep` directly, because the properties that matter -- the window is
paged once, one failing evaluation does not discard another's work, sessions are
counted once -- are properties of the sweep and not of any producer.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core.nodes import _common, _stage, _sweep
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    PASSED,
)
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import AGENT_COUNT_LIMIT
from ambient_quality_agent.tools.ingestion.models import IngestionCounts, Page

from .conftest import (
    FakeReviewCall,
    ScriptedReviewer,
    make_agent_case,
    make_page,
)


class _Fetcher:
    """Serves fixed pages, standing in for BigQuery.

    `count_scanned` answers `counts.scanned`, or raises when `count_raises` is
    set. Those are the two inputs the sweep's empty-window reporting turns on:
    a counted zero and a count that could not be taken say different things.
    `count_by_agent` answers `by_agent`, or raises when it is `None`.
    """

    def __init__(
        self, pages: list[Page], *, scanned: int = 7, count_raises: bool = False
    ) -> None:
        self._pages = pages
        self.counts = IngestionCounts(
            scanned=scanned, ingested=5, partial=1, failed=1
        )
        self.count_raises = count_raises
        self.by_agent: dict[str, int] | None = {}
        self.by_agent_calls = 0
        self.submitted = 0
        self.fetches = 0
        self.has_selector = False
        self.prechecks = 0
        self.rejection: selector.Rejection | None = None

    def precheck_selector(
        self, *args: Any, **kwargs: Any
    ) -> selector.PrecheckResult:
        self.prechecks += 1
        return selector.PrecheckResult(rejection=self.rejection)

    def count_scanned(self, *args: Any, **kwargs: Any) -> int:
        if self.count_raises:
            raise RuntimeError("the counting query failed")
        return self.counts.scanned

    def count_by_agent(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        self.by_agent_calls += 1
        if self.by_agent is None:
            raise RuntimeError("the per-agent query failed")
        return self.by_agent

    def submit_query(self, **kwargs: Any) -> str:
        self.submitted += 1
        return "exec-1"

    def fetch_page(self, **kwargs: Any) -> Page:
        self.fetches += 1
        token = kwargs.get("page_token")
        return self._pages[0] if token is None else self._pages[int(token)]


class _Recorder:
    """A minimal `Evaluation` that records what it was given."""

    def __init__(
        self, name: str, fail_on_finish: Exception | None = None
    ) -> None:
        self.name = name
        self.pages: list[Page] = []
        self.finished = False
        self._fail = fail_on_finish
        # The sweep reads these off every analyzer to total them once.
        self.outcomes: dict[str, str] = {}
        self.findings_count = 0
        self.rubrics_count = 0

    def evaluate(self, state: Any, page: Page) -> None:
        self.pages.append(page)

    def finish(self, state: Any) -> None:
        self.finished = True
        if self._fail is not None:
            raise self._fail

    def render_summary(self) -> str:
        return f"**{self.name}:** ok"


@pytest.fixture
def ctx() -> Any:
    now = dt.datetime.now(tz=dt.UTC)
    context = mock.MagicMock()
    context.state = {
        "observed_agent_name": "watched",
        "project_id": "p",
        "budget_per_metric": 100,
        "window_start": (now - dt.timedelta(days=1)).isoformat(),
        "window_end": now.isoformat(),
    }
    return context


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[Page],
    *,
    scanned: int = 7,
    count_raises: bool = False,
) -> _Fetcher:
    """Installs the fake fetcher while keeping `_common.count_scanned` real.

    The real function converts the fetcher's answer or error into the
    optional integer the sweep reports on.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        pages: Pages to return during fetch calls.
        scanned: Total scanned count reported by the fetcher.
        count_raises: Whether count_scanned should raise an error.

    Returns:
        The configured fake fetcher instance.
    """
    fetcher = _Fetcher(pages, scanned=scanned, count_raises=count_raises)
    monkeypatch.setattr(_common, "fetcher_factory", lambda ctx: fetcher)
    return fetcher


def _pages(*sizes: int) -> list[Page]:
    out = []
    for index, size in enumerate(sizes):
        cases = [make_agent_case(f"s{index}-{n}") for n in range(size)]
        last = index == len(sizes) - 1
        out.append(
            make_page(cases, next_page_token=None if last else str(index + 1))
        )
    return out


def test_the_window_is_queried_once_for_any_number_of_evaluations(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the sweep exists. Two producers each paging the window is the
    duplication it removes."""
    fetcher = _wire(monkeypatch, _pages(2, 1))
    one, two = _Recorder("one"), _Recorder("two")

    _sweep.run_sweep(ctx, [one, two], node_name="sweep", budget=100)

    assert fetcher.submitted == 1
    assert fetcher.fetches == 2, "two pages, fetched once each"


def test_every_evaluation_sees_every_page(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, _pages(2, 3))
    one, two = _Recorder("one"), _Recorder("two")

    _sweep.run_sweep(ctx, [one, two], node_name="sweep", budget=100)

    assert len(one.pages) == 2
    assert [p.dataset.eval_cases for p in one.pages] == [
        p.dataset.eval_cases for p in two.pages
    ]


def test_sessions_are_counted_once_not_once_per_evaluation(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two evaluations reading the same page do not make twice as many
    sessions; reporting six where three were ingested is a lie a dashboard
    repeats."""
    _wire(monkeypatch, _pages(3))

    _sweep.run_sweep(
        ctx, [_Recorder("one"), _Recorder("two")], node_name="sweep", budget=100
    )

    assert ctx.state["counters"]["trajectories_evaluated"] == 3


def test_one_failing_evaluation_does_not_discard_the_others_work(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer outage must not throw away a healthy code-metric run beside
    it. The sweep still fails -- but after everyone has finished."""
    _wire(monkeypatch, _pages(2))
    broken = _Recorder(
        "broken", fail_on_finish=RuntimeError("reviewer is down")
    )
    healthy = _Recorder("healthy")

    with pytest.raises(RuntimeError, match="reviewer is down"):
        _sweep.run_sweep(ctx, [broken, healthy], node_name="sweep", budget=100)

    assert healthy.finished, "the healthy evaluation was never finished"


def test_an_empty_window_skips_without_finishing_anything(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _wire(monkeypatch, _pages(0))
    only = _Recorder("only")

    lines = _sweep.run_sweep(ctx, [only], node_name="sweep", budget=100)

    assert "Skipped" in lines[0]
    assert not only.finished
    assert (
        ctx.state["counters"]["trajectories_ingested"]
        == fetcher.counts.ingested
    )


# --- what an empty window says ------------------------------------------- #


def test_a_window_holding_nothing_names_the_agent_the_window_and_the_source(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one line a reader gets for the plan's two indistinguishable faults.

    An idle agent and one attached under the wrong name produce the same
    clean run over nothing, and AQuA cannot tell them apart without a second
    query. So the line states what it filtered on, which is enough for someone
    who knows their own agent to recognise which of the two they are looking at.
    """
    _wire(monkeypatch, _pages(0), scanned=0)
    ctx.state["telemetry_ingestion_source"] = "cloud_logging"

    line = _sweep.run_sweep(
        ctx, [_Recorder("only")], node_name="sweep", budget=100
    )[0]

    assert "no traces at all" in line
    assert "`watched`" in line, "the agent name it filtered on"
    assert "`cloud_logging`" in line, "the telemetry source it read"
    assert ctx.state["window_start"][:16] in line
    assert ctx.state["window_end"][:16] in line
    assert "`agents-cli aqua attach` sets the name" in line


def test_a_selection_that_matched_nothing_does_not_blame_the_agent_name(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a custom selector matches zero sessions, skip reporting attributes the
    empty result to the selection rather than agent name configuration."""
    _wire(monkeypatch, _pages(0), scanned=0)
    ctx.state["selector_sql"] = "SELECT session_id AS target_id FROM t"

    line = _sweep.run_sweep(
        ctx, [_Recorder("only")], node_name="sweep", budget=100
    )[0]

    assert "the selection matched no sessions" in line
    assert ctx.state["window_start"][:16] in line
    assert ctx.state["window_end"][:16] in line
    assert "agents-cli aqua attach" not in line


def test_a_window_that_held_traces_and_yielded_none_is_not_called_empty(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingestion dropped every trace. That is a different fault, and blaming the
    agent name for it would send the reader to the wrong setting."""
    _wire(monkeypatch, _pages(0), scanned=12)

    line = _sweep.run_sweep(
        ctx, [_Recorder("only")], node_name="sweep", budget=100
    )[0]

    assert line == "_Skipped (no trajectories found)._"


def test_a_count_that_could_not_be_taken_claims_nothing_about_the_window(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`None` is not zero. Reporting an empty window off a failed count would
    name a misconfiguration on the strength of a query that never answered.

    The count stays best-effort: the sweep runs on, and the failure is logged
    rather than swallowed.
    """
    _wire(monkeypatch, _pages(0), count_raises=True)

    with caplog.at_level(logging.WARNING):
        line = _sweep.run_sweep(
            ctx, [_Recorder("only")], node_name="sweep", budget=100
        )[0]

    assert line == "_Skipped (no trajectories found)._"
    assert any(
        "could not count the traces" in r.message for r in caplog.records
    )


def test_the_empty_window_line_is_the_first_one(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`eval.py` renders `lines[0]` and drops the rest, so a warning appended
    after the evaluations' summaries would never reach the eval nodes' ledger."""
    _wire(monkeypatch, _pages(0), scanned=0)

    lines = _sweep.run_sweep(
        ctx, [_Recorder("a"), _Recorder("b")], node_name="sweep", budget=100
    )

    assert len(lines) == 1
    assert "no traces at all" in lines[0]


# --- an empty window, standalone ------------------------------------------ #


def test_a_standalone_window_holding_nothing_fails_and_names_the_agents_present(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locally the investigation was started by hand to see results, so an empty window is
    a setting to fix. The names the telemetry does record are the fix."""
    fetcher = _wire(monkeypatch, _pages(0), scanned=0)
    fetcher.by_agent = {"it_support_agent": 12, "other_agent": 3}
    ctx.state.update(
        standalone=True,
        telemetry_ingestion_source="big_query",
        telemetry_dataset="agent_analytics",
        telemetry_table="agent_events",
    )
    only = _Recorder("only")

    with pytest.raises(_sweep.EmptyWindowError) as raised:
        _sweep.run_sweep(ctx, [only], node_name="sweep", budget=100)

    message = str(raised.value)
    assert "'watched'" in message, "the name it filtered on"
    assert "big_query (agent_analytics.agent_events)" in message
    assert "'it_support_agent' (12), 'other_agent' (3)" in message
    assert "agents-cli aqua attach <agent name>" in message
    assert ctx.state["window_start"][:16] in message
    assert "busiest" not in message
    assert not only.finished


def test_a_standalone_error_says_when_it_lists_only_the_busiest_agents(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _wire(monkeypatch, _pages(0), scanned=0)
    fetcher.by_agent = {
        f"agent_{n:02d}": 100 - n for n in range(AGENT_COUNT_LIMIT)
    }
    ctx.state["standalone"] = True

    with pytest.raises(
        _sweep.EmptyWindowError, match=f"the {AGENT_COUNT_LIMIT} busiest"
    ):
        _sweep.run_sweep(
            ctx, [_Recorder("only")], node_name="sweep", budget=100
        )


def test_a_standalone_window_no_agent_wrote_to_points_at_the_telemetry_settings(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no name to offer, the agent name is not the suspect: either nothing
    ran, or AQuA is reading the wrong table."""
    _wire(monkeypatch, _pages(0), scanned=0)
    ctx.state["standalone"] = True

    with pytest.raises(_sweep.EmptyWindowError) as raised:
        _sweep.run_sweep(
            ctx, [_Recorder("only")], node_name="sweep", budget=100
        )

    message = str(raised.value)
    assert "No agent recorded any traces there" in message
    assert "dataset" in message
    assert "agents-cli aqua attach" not in message


def test_a_failed_per_agent_count_still_fails_the_standalone_run(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The names only add detail to a failure already decided, so losing them
    must not turn it into a different one."""
    fetcher = _wire(monkeypatch, _pages(0), scanned=0)
    fetcher.by_agent = None
    ctx.state["standalone"] = True

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(_sweep.EmptyWindowError, match="could not be listed"),
    ):
        _sweep.run_sweep(
            ctx, [_Recorder("only")], node_name="sweep", budget=100
        )

    assert any(
        "could not count the traces per agent" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("scanned", "count_raises", "selector_sql"),
    [
        pytest.param(12, False, "", id="traces-held-none-ingested"),
        pytest.param(7, True, "", id="count-not-taken"),
        pytest.param(
            0, False, "SELECT session_id AS target_id FROM t", id="selector"
        ),
    ],
)
def test_a_standalone_sweep_fails_only_on_a_counted_empty_window(
    ctx: Any,
    monkeypatch: pytest.MonkeyPatch,
    scanned: int,
    count_raises: bool,
    selector_sql: str,
) -> None:
    """The other empty sweeps are not a wrong setting, or not known to be one,
    so standalone reports them as a deployment does."""
    fetcher = _wire(
        monkeypatch, _pages(0), scanned=scanned, count_raises=count_raises
    )
    ctx.state.update(standalone=True, selector_sql=selector_sql)

    lines = _sweep.run_sweep(
        ctx, [_Recorder("only")], node_name="sweep", budget=100
    )

    assert "Skipped" in lines[0]
    assert fetcher.by_agent_calls == 0


def test_a_deployed_empty_window_asks_nothing_per_agent(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deployed, an empty window is a warning, and the extra query is not paid."""
    fetcher = _wire(monkeypatch, _pages(0), scanned=0)

    lines = _sweep.run_sweep(
        ctx, [_Recorder("only")], node_name="sweep", budget=100
    )

    assert "no traces at all" in lines[0]
    assert fetcher.by_agent_calls == 0


def test_ingestion_is_recorded_once_however_many_evaluations_read_it(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _wire(monkeypatch, _pages(1))

    _sweep.run_sweep(
        ctx,
        [_Recorder("a"), _Recorder("b"), _Recorder("c")],
        node_name="sweep",
        budget=100,
    )

    assert (
        ctx.state["counters"]["trajectories_ingested"]
        == fetcher.counts.ingested
    )


def test_a_memory_stop_reaches_the_counters_and_names_what_it_left_unread(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped sweep is told apart from a smaller sample: the unread count is
    a counter the dashboard can show, not only a note in the summary."""
    cases = [make_agent_case("s0-0")]
    fetcher = _wire(monkeypatch, [make_page(cases, memory_truncated=True)])
    fetcher.counts.sampled = 10
    fetcher.counts.unread = 3

    lines = _sweep.run_sweep(
        ctx, [_Recorder("one")], node_name="sweep", budget=100
    )

    assert ctx.state["counters"]["trajectories_sampled"] == 10
    assert ctx.state["counters"]["trajectories_unread"] == 3
    assert lines == [
        "**one:** ok (stopped early near the memory limit; "
        "3 sampled trajectories left unread)"
    ]


def test_a_sweep_read_in_full_reports_nothing_unread(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _wire(monkeypatch, _pages(2))
    fetcher.counts.sampled = 2

    lines = _sweep.run_sweep(
        ctx, [_Recorder("one")], node_name="sweep", budget=100
    )

    assert ctx.state["counters"]["trajectories_sampled"] == 2
    assert ctx.state["counters"]["trajectories_unread"] == 0
    assert lines == ["**one:** ok"]


def _analyzer(
    outcomes: dict[str, str], *, findings: int = 0, rubrics: int = 0
) -> Any:
    """Creates a stand-in analyzer carrying only what `collapse_outcomes` reads.

    Args:
        outcomes: Dictionary of session outcomes.
        findings: Number of findings to report.
        rubrics: Number of rubrics to report.

    Returns:
        Stand-in evaluator instance with specified outcomes.
    """

    class _Eval:
        def __init__(self) -> None:
            self.name = "e"
            self.outcomes = outcomes
            self.findings_count = findings
            self.rubrics_count = rubrics

    return _Eval()


def test_a_session_several_analyzers_judged_counts_once() -> None:
    """One session is one number, collapsed the way `compute_metric_status` already
    ranks the metrics within one eval case.

    The dashboard reads `trajectories_eval_passed` and `trajectories_eval_failed`, so
    letting each analyzer add its own tally inflated both the moment anyone
    published a metric beside the reviewer.
    """
    counts = _sweep.collapse_outcomes(
        [
            {"s1": PASSED, "s2": FAILED, "s3": PASSED},
            {"s1": FAILED, "s2": PASSED, "s3": PASSED},
        ]
    )

    # Three sessions in, three sessions out.
    assert sum(counts.values()) == 3
    # The worse verdict wins: s1 and s2 were each failed by one analyzer.
    assert counts == {PASSED: 1, FAILED: 2, ERRORED: 0}


def test_an_errored_session_outranks_a_failure() -> None:
    """An error hides whether the session would have passed or failed, so it
    must not be reported as either."""
    counts = _sweep.collapse_outcomes([{"s1": FAILED}, {"s1": ERRORED}])

    assert counts == {PASSED: 0, FAILED: 0, ERRORED: 1}


def test_no_analyzers_is_no_verdicts() -> None:
    assert _sweep.collapse_outcomes([]) == {PASSED: 0, FAILED: 0, ERRORED: 0}


def test_analyzers_key_the_same_case_the_same_way() -> None:
    """The sweep merges `outcomes` by key, so a case judged by two analyzers is
    one case only while they name it identically. Nothing in the type system
    enforces that, and getting it wrong restores the double-count this collapse
    exists to prevent, so pin it on the real implementations rather than on
    stand-ins. Blank ids are included because production filters them out but
    the fixtures do not, which is how the collapse went unnoticed.
    """
    from agentplatform._genai.types import (
        EvalCaseMetricResult,
        EvalCaseResult,
        EvaluationResult,
        ResponseCandidateResult,
    )
    from ambient_quality_agent.core.nodes import (
        _code_metrics,
        _remote_code_metrics,
        review,
    )
    from ambient_quality_agent.tools.documents.goal import InvestigationGoal
    from ambient_quality_agent.tools.metrics.library import (
        CodeMetric,
        RemoteCodeMetric,
    )

    cases = [make_agent_case("s1"), make_agent_case("s2"), make_agent_case("")]
    page = make_page(cases)

    reviewer = review.ReviewEvaluation(
        lambda _case, _revisions, **_scope: [], InvestigationGoal()
    )
    metric = CodeMetric(
        name="always_passes",
        expected="The agent answers.",
        evaluate=lambda instance: {"score": 1.0},
    )
    metrics = _code_metrics.CodeMetricEvaluation({"always_passes": metric})

    # The remote analyzer reaches these keys by another route -- it tallies a
    # whole page in one step where the local runner tallies per case -- so its
    # agreement with the other two does not follow from either being right.
    class _Service:
        def evaluate(self, dataset: Any, _metrics: Any) -> EvaluationResult:
            return EvaluationResult(
                eval_case_results=[
                    EvalCaseResult(
                        eval_case_index=index,
                        response_candidate_results=[
                            ResponseCandidateResult(
                                response_index=0,
                                metric_results={
                                    "remote": EvalCaseMetricResult(score=1.0)
                                },
                            )
                        ],
                    )
                    for index, _case in enumerate(dataset.eval_cases or [])
                ]
            )

    remote_metrics = _remote_code_metrics.RemoteCodeMetricEvaluation(
        _Service(),
        [
            RemoteCodeMetric(
                name="remote",
                service_name="remote",
                expected="The agent answers.",
                source="def evaluate(instance):\n    return 1.0\n",
            )
        ],
    )

    state: dict[str, Any] = {}
    reviewer.evaluate(state, page)
    metrics.evaluate(state, page)
    remote_metrics.evaluate(state, page)

    assert (
        set(reviewer.outcomes)
        == set(metrics.outcomes)
        == set(remote_metrics.outcomes)
    )
    assert len(reviewer.outcomes) == len(cases)
    assert _sweep.collapse_outcomes(
        [reviewer.outcomes, metrics.outcomes, remote_metrics.outcomes]
    ) == {
        PASSED: len(cases),
        FAILED: 0,
        ERRORED: 0,
    }


def test_local_and_remote_metrics_both_reach_the_breakdown() -> None:
    """Merges metrics_by_name and metrics_detail across evaluations."""
    from ambient_quality_agent.tools.metrics import runner

    state: dict[str, Any] = {}
    runner.merge_breakdown(
        state, {"local_one": {PASSED: 2, FAILED: 1, ERRORED: 0}}, kind="local"
    )
    runner.merge_breakdown(
        state, {"remote_one": {PASSED: 3, FAILED: 0, ERRORED: 1}}, kind="remote"
    )

    assert state["metrics_by_name"] == {
        "local_one": {PASSED: 2, FAILED: 1, ERRORED: 0},
        "remote_one": {PASSED: 3, FAILED: 0, ERRORED: 1},
    }
    assert {n: r["kind"] for n, r in state["metrics_detail"].items()} == {
        "local_one": "local",
        "remote_one": "remote",
    }


# --- a selector-backed sweep re-validates its own SQL ---------------------- #


_REFUSAL = selector.Rejection(
    reason=selector.RejectionReason.UNAUTHORIZED_TABLE,
    explanation="The selector must read p.d.t, but it reads no table at all.",
)


def test_a_selector_backed_sweep_revalidates_before_it_queries(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that sweeps validate custom selectors before submitting query jobs.

    Durable runs reconstruct state without prior validation proofs, requiring
    sweeps to recheck selector SQL before execution.
    """
    fetcher = _wire(monkeypatch, _pages(1))
    fetcher.has_selector = True

    _sweep.run_sweep(ctx, [_Recorder("one")], node_name="sweep", budget=100)

    assert fetcher.prechecks == 1
    assert fetcher.submitted == 1


def test_a_refused_selector_fails_the_run_before_it_reads_anything(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that invalid selectors abort the sweep before submitting query jobs.

    Prevents executing unverified or unauthorized SQL under the engine service
    account credentials.
    """
    fetcher = _wire(monkeypatch, _pages(1))
    fetcher.has_selector = True
    fetcher.rejection = _REFUSAL

    with pytest.raises(selector.SelectorRefused, match="unauthorized_table"):
        _sweep.run_sweep(ctx, [_Recorder("one")], node_name="sweep", budget=100)

    assert fetcher.submitted == 0
    assert fetcher.fetches == 0


def test_a_sweep_sampling_at_random_pays_for_no_dry_run(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that default sampling sweeps bypass selector dry-run prechecks.

    Avoids unnecessary dry-run RPC overhead when runs do not configure custom
    SQL selectors.
    """
    fetcher = _wire(monkeypatch, _pages(1))

    _sweep.run_sweep(ctx, [_Recorder("one")], node_name="sweep", budget=100)

    assert fetcher.prechecks == 0


# --- live progress -------------------------------------------------------- #


class _Finder(_Recorder):
    """A `_Recorder` that files one finding per page it is given."""

    def evaluate(self, state: Any, page: Page) -> None:
        super().evaluate(state, page)
        self.findings_count += 1


def _stages(store: Any) -> list[dict[str, Any]]:
    """The stage rows the sweep wrote, each read back from its JSON."""
    return [
        json.loads(r["text"])
        for r in store.event_rows
        if r["source"] == "stage"
    ]


def _review(evaluated: int, findings: int) -> dict[str, Any]:
    """A session review's stage row, out of the seven the fake window holds."""
    return {
        "stage": "Session review",
        "evaluated": evaluated,
        "total": 7,
        "findings": findings,
    }


def test_the_sweep_reports_how_far_it_got_after_every_page(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, investigation_store: Any
) -> None:
    """A long sweep writes its progress as it goes, so a running
    investigation can say how far it got before any counter is written."""
    _wire(monkeypatch, _pages(2, 1), scanned=7)
    ctx.state["investigation_id"] = "investigation-1"

    _sweep.run_sweep(
        ctx,
        [_Finder("one")],
        node_name="review",
        budget=100,
        progress=_sweep.SweepProgress(ctx.state, "Session review"),
    )

    assert [r["source"] for r in investigation_store.event_rows] == [
        "stage"
    ] * 3
    assert _stages(investigation_store) == [
        _review(0, 0),
        _review(2, 1),
        _review(3, 2),
    ]
    # Progress rows are for the live view only: the run's summary, which the
    # chat reads back, keeps to what the nodes reported.
    assert "progress_log" not in ctx.state


@pytest.mark.parametrize(
    ("scanned", "count_raises", "total"),
    [(500, False, 100), (7, True, 100)],
    ids=["capped-by-budget", "count-unknown"],
)
def test_the_progress_total_is_what_the_sweep_can_read(
    ctx: Any,
    monkeypatch: pytest.MonkeyPatch,
    investigation_store: Any,
    scanned: int,
    count_raises: bool,
    total: int,
) -> None:
    _wire(monkeypatch, _pages(1), scanned=scanned, count_raises=count_raises)
    ctx.state["investigation_id"] = "investigation-1"

    _sweep.run_sweep(ctx, [_Recorder("one")], node_name="sweep", budget=100)

    assert _stages(investigation_store)[0] == {
        "stage": "sweep",
        "evaluated": 0,
        "total": total,
        "findings": 0,
    }


class _Clock:
    """A `monotonic` the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_page_reports_as_it_is_judged_at_most_once_an_interval(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, investigation_store: Any
) -> None:
    """A window usually arrives as one page, so its counts move only if the
    evaluation judging it reports as it goes. Each row is a BigQuery load job,
    so those reports are written at most once an interval."""
    clock = _Clock()
    monkeypatch.setattr(_stage.time, "monotonic", clock)
    _wire(monkeypatch, _pages(3), scanned=7)
    ctx.state["investigation_id"] = "investigation-1"
    progress = _sweep.SweepProgress(ctx.state, "Session review")

    class _Judge(_Recorder):
        def evaluate(self, state: Any, page: Page) -> None:
            super().evaluate(state, page)
            progress.advance(1, 0)  # Too soon after the first row.
            clock.now += _stage.PROGRESS_INTERVAL_SECONDS
            progress.advance(2, 1)
            progress.advance(3, 1)  # Too soon after the one before.
            self.findings_count = 1

    _sweep.run_sweep(
        ctx,
        [_Judge("review")],
        node_name="review",
        budget=100,
        progress=progress,
    )

    assert _stages(investigation_store) == [
        _review(0, 0),
        _review(2, 1),
        _review(3, 1),
    ]


def test_a_report_from_a_later_page_counts_the_pages_before_it(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, investigation_store: Any
) -> None:
    clock = _Clock()
    monkeypatch.setattr(_stage.time, "monotonic", clock)
    _wire(monkeypatch, _pages(2, 1), scanned=7)
    ctx.state["investigation_id"] = "investigation-1"
    progress = _sweep.SweepProgress(ctx.state, "Session review")

    class _Judge(_Recorder):
        def evaluate(self, state: Any, page: Page) -> None:
            super().evaluate(state, page)
            clock.now += _stage.PROGRESS_INTERVAL_SECONDS
            progress.advance(1, 1)
            self.findings_count += 1

    _sweep.run_sweep(
        ctx,
        [_Judge("review")],
        node_name="review",
        budget=100,
        progress=progress,
    )

    # The second page's own report already said 3 of 7 with 2 findings, so
    # the page's end writes nothing new.
    assert _stages(investigation_store) == [
        _review(0, 0),
        _review(1, 1),
        _review(2, 1),
        _review(3, 2),
    ]


def test_a_page_judged_in_one_call_keeps_its_row_alive(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, investigation_store: Any
) -> None:
    """The eval service judges a page in one call that reports nothing, so
    while it runs the sweep's newest row is written again each interval: the
    dashboard must not call a working investigation stalled."""
    clock = _Clock()
    monkeypatch.setattr(_stage.time, "monotonic", clock)
    monkeypatch.setattr(_stage, "_KEEP_ALIVE_CHECK_SECONDS", 0.01)
    _wire(monkeypatch, _pages(3), scanned=7)
    ctx.state["investigation_id"] = "investigation-1"

    class _OneCall(_Recorder):
        def evaluate(self, state: Any, page: Page) -> None:
            super().evaluate(state, page)
            clock.now += _stage.PROGRESS_INTERVAL_SECONDS
            deadline = time.perf_counter() + 5
            while len(investigation_store.event_rows) < 2:
                assert time.perf_counter() < deadline, "no row was kept alive"
                time.sleep(0.01)

    _sweep.run_sweep(
        ctx,
        [_OneCall("eval")],
        node_name="eval",
        budget=100,
        progress=_sweep.SweepProgress(ctx.state, "Session review"),
    )

    assert _stages(investigation_store) == [
        _review(0, 0),
        _review(0, 0),
        _review(3, 0),
    ]


def test_session_review_reports_after_each_session_it_reviews() -> None:
    """The review is the long part of an investigation and takes a page's
    sessions eight at a time, so it says how far it got after each one."""
    from ambient_quality_agent.core.nodes import review
    from ambient_quality_agent.tools.documents.goal import InvestigationGoal

    reports: list[tuple[int, int]] = []
    threads: set[int] = set()

    def on_reviewed(judged: int, findings: int) -> None:
        reports.append((judged, findings))
        threads.add(threading.get_ident())

    evaluation = review.ReviewEvaluation(
        ScriptedReviewer(
            FakeReviewCall(
                '{"findings": [{"expected_behavior": "call book_flight", '
                '"actual_behavior": "made no tool call"}]}'
            )
        ),
        InvestigationGoal(),
        on_reviewed=on_reviewed,
    )

    evaluation.evaluate(
        {}, make_page([make_agent_case(f"s{n}") for n in range(3)])
    )

    assert reports == [(1, 1), (2, 2), (3, 3)]
    # The reviews run on a pool, but the reports come back on the node's own
    # thread, so `SweepProgress` needs no lock of its own for its counts.
    assert threads == {threading.get_ident()}
    assert evaluation.findings_count == 3
