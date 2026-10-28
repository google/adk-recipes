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

"""Tests for the ``score`` code-metric producer node."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.core.investigation import job_scheduling
from ambient_quality_agent.core.nodes import _code_metrics, _common, _sweep
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.models import IngestionCounts, Page
from ambient_quality_agent.tools.metrics import runner
from ambient_quality_agent.tools.metrics.library import (
    CodeMetric,
    MetricLibrary,
)

from .conftest import make_agent_case, make_code_metric, make_config, make_page

_case = make_agent_case
_metric = make_code_metric


def _page(cases: list[EvalCase], token: str | None = None) -> Page:
    return make_page(cases, next_page_token=token)


class _Fetcher:
    """A fetcher serving fixed pages, standing in for BigQuery."""

    def __init__(self, pages: list[Page]) -> None:
        self._pages = pages
        # Non-zero, so an assertion on an ingestion counter cannot pass by
        # comparing zero to zero.
        self.counts = IngestionCounts(
            scanned=7, ingested=5, partial=1, failed=1
        )
        self.submitted = 0
        self.has_selector = False
        self.limit: int | None = None
        self.scopes: list[Any] = []

    def count_scanned(self, *args: Any, **kwargs: Any) -> int:
        return self.counts.scanned

    def submit_query(self, **kwargs: Any) -> str:
        self.submitted += 1
        self.limit = kwargs["limit"]
        self.scopes.append(kwargs["metric_type"])
        return "exec-1"

    def fetch_page(self, **kwargs: Any) -> Page:
        self.scopes.append(kwargs["metric_type"])
        token = kwargs.get("page_token")
        return self._pages[0] if token is None else self._pages[int(token)]


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


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the node's two factories at fakes."""

    def _wire(pages: list[Page], library: Mapping[str, CodeMetric]) -> _Fetcher:
        published = MetricLibrary(library)
        fetcher = _Fetcher(pages)
        monkeypatch.setattr(_common, "fetcher_factory", lambda ctx: fetcher)
        monkeypatch.setattr(
            _common, "metric_library_factory", lambda ctx: (published, "")
        )
        return fetcher

    return _wire


def _run(ctx: Any) -> Any:
    """Drives the code-metric evaluation through the shared sweep.

    Args:
        ctx: Test context containing run state.

    Returns:
        An event-like object carrying sweep output text.
    """
    library, warning = _common.metric_library_factory(ctx)
    lines = _sweep.run_sweep(
        ctx,
        [_code_metrics.CodeMetricEvaluation(library)],
        node_name="custom_metrics",
        budget=int(ctx.state.get("budget_per_metric", 0) or 0),
    )
    return _event_like(lines[0] + warning)


@dataclasses.dataclass(frozen=True)
class _event_like:
    """Test stand-in representing a node event output."""

    output: str


def test_scores_a_page_and_records_findings(ctx: Any, wire: Any) -> None:
    wire(
        [_page([_case("s1"), _case("s2")])],
        {"m": _metric({"score": 0.0, "explanation": "nope"})},
    )

    _run(ctx)

    assert len(ctx.state["finding_sets"]) == 1
    assert ctx.state["counters"]["findings_generated"] == 2
    assert ctx.state["counters"]["traces_eval_failed"] == 2
    assert ctx.state["multi_turn_pages_evaluated"] == 1


def test_the_whole_budget_reaches_the_query(ctx: Any, wire: Any) -> None:
    fetcher = wire([_page([_case("s1")])], {"m": _metric({"score": 1.0})})

    _run(ctx)

    assert fetcher.limit == 100


def test_no_sessions_skips_but_still_records_what_it_scanned(
    ctx: Any, wire: Any
) -> None:
    wire([_page([])], {"m": _metric({"score": 1.0})})

    event = _run(ctx)

    assert "Skipped" in event.output
    assert ctx.state["counters"]["traces_scanned"] == 7


def test_the_ingestion_counters_reach_the_run(ctx: Any, wire: Any) -> None:
    wire([_page([_case("s1")])], {"m": _metric({"score": 1.0})})

    _run(ctx)

    counters = ctx.state["counters"]
    assert counters["traces_scanned"] == 7
    assert counters["traces_ingested"] == 5
    assert counters["traces_ingested_partial"] == 1
    assert counters["traces_ingested_failed"] == 1


def test_it_asks_for_sessions_not_single_turns(ctx: Any, wire: Any) -> None:
    fetcher = wire([_page([_case("s1")])], {"m": _metric({"score": 1.0})})

    _run(ctx)

    assert set(fetcher.scopes) == {MetricType.MULTI_TURN}


def test_a_dead_metric_is_named_even_when_a_session_could_not_be_shaped(
    ctx: Any, wire: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case that fails before the metric loop still counts as a case, so
    counting errors per metric and comparing to the case count under-reports.
    The summary is driven by which metrics ever *worked* instead."""
    real = runner.build_instance

    def refuse_one(eval_case: Any) -> dict[str, Any]:
        if eval_case.eval_case_id == "s2":
            raise ValueError("cannot serialize this session")
        return real(eval_case)

    monkeypatch.setattr(runner, "build_instance", refuse_one)
    library = {
        "dead": _metric(RuntimeError("boom"), name="dead"),
        "alive": _metric({"score": 1.0}, name="alive"),
    }
    wire([_page([_case("s1"), _case("s2")])], library)

    event = _run(ctx)

    assert "`dead`" in event.output
    assert "`alive`" not in event.output


def test_a_metric_library_warning_reaches_the_run(
    ctx: Any, monkeypatch: Any
) -> None:
    """A bucket that could not be read must not read as a healthy sweep."""
    monkeypatch.setattr(
        _common, "fetcher_factory", lambda c: _Fetcher([_page([_case("s1")])])
    )
    monkeypatch.setattr(
        _common,
        "metric_library_factory",
        lambda ctx: (
            MetricLibrary({"m": _metric({"score": 1.0})}),
            " **could not be read**",
        ),
    )

    event = _run(ctx)

    assert "could not be read" in event.output


def test_every_case_erroring_raises_rather_than_reporting_zero_defects(
    ctx: Any, wire: Any
) -> None:
    wire(
        [_page([_case("s1")])],
        {"m": _metric(RuntimeError("library is broken"))},
    )

    with pytest.raises(RuntimeError, match="verdict on any session") as caught:
        _run(ctx)

    # Wrapped, not re-raised: the cause may be a SystemExit, which would walk
    # straight through the serving stack's `except Exception`.
    assert str(caught.value.__cause__) == "library is broken"


def test_a_metric_that_exits_the_process_does_not_escape_as_systemexit(
    ctx: Any, wire: Any
) -> None:
    wire([_page([_case("s1")])], {"m": _metric(SystemExit(2))})

    with pytest.raises(RuntimeError):
        _run(ctx)


def test_one_broken_metric_among_several_does_not_fail_the_run(
    ctx: Any, wire: Any
) -> None:
    """A single always-raising metric must not discard the other metrics'
    findings, nor fail a sweep that otherwise worked -- but it must still be
    counted and named."""
    library = {
        "bad": _metric(RuntimeError("boom"), name="bad"),
        "good": _metric(
            {"score": 0.0, "explanation": "still found it"}, name="good"
        ),
    }
    wire([_page([_case("s1"), _case("s2")])], library)

    event = _run(ctx)

    assert ctx.state["counters"]["findings_generated"] == 2
    assert "2 finding(s)" in event.output
    # The cases ARE errored -- alerting reads that counter, and a metric that
    # never works must not pass for a clean sweep...
    assert ctx.state["counters"]["traces_eval_errored"] == 2
    # ...and the run must say which half of the library never ran.
    assert "never returned a verdict" in event.output
    assert "`bad`" in event.output


def test_code_metrics_gets_the_whole_cap_not_a_per_metric_slice() -> None:
    """`derive_window_and_budget` partitions the cap in eval_service mode.
    Session review reads each session once however many evaluations score it,
    so it takes the cap whole."""
    config = make_config(
        quality_analysis_mode="session_review", data_evaluation_cap=900
    )

    _, _, budget = job_scheduling.derive_window_and_budget(config)

    assert budget == 900


def test_eval_service_still_partitions_the_cap() -> None:
    config = make_config(
        quality_analysis_mode="eval_service", data_evaluation_cap=900
    )

    _, _, budget = job_scheduling.derive_window_and_budget(config)

    assert budget < 900


def test_the_summary_names_metrics_that_call_a_model() -> None:
    """The bill lands on the engine and nothing else in the sweep shows it."""
    library = {
        "judge": CodeMetric(
            "judge", "expected", lambda i: 1.0, ("google.genai",)
        ),
        "plain": CodeMetric("plain", "expected", lambda i: 1.0),
    }
    tally = runner.Tally(cases=42, scored_metrics={"judge", "plain"})

    text = _code_metrics._render_code_metrics_summary(
        library, tally, truncated=False
    )

    assert "1 metric(s) import a model client" in text
    assert "`judge` (google.genai)" in text
    assert "42 session(s)" in text
    assert "plain" not in text.split("import a model client")[1]


def test_the_summary_says_nothing_when_no_metric_calls_a_model() -> None:
    library = {"plain": CodeMetric("plain", "expected", lambda i: 1.0)}
    tally = runner.Tally(cases=3, scored_metrics={"plain"})

    assert "model client" not in _code_metrics._render_code_metrics_summary(
        library, tally, truncated=False
    )


def test_the_summary_breaks_outcomes_down_by_metric(
    ctx: Any, wire: Any
) -> None:
    """Verifies per-metric pass, fail, and error counts in metrics_by_name."""
    library = {
        "always_passes": make_code_metric({"score": 1.0}, name="always_passes"),
        "always_fails": make_code_metric({"score": 0.0}, name="always_fails"),
        "always_breaks": make_code_metric(
            RuntimeError("boom"), name="always_breaks"
        ),
    }
    wire([make_page([_case("s1"), _case("s2")])], library)

    _run(ctx)

    assert ctx.state["metrics_by_name"] == {
        "always_passes": {"passed": 2, "failed": 0, "errored": 0},
        "always_fails": {"passed": 0, "failed": 2, "errored": 0},
        "always_breaks": {"passed": 0, "failed": 0, "errored": 2},
    }


def test_the_summary_describes_each_metric_not_just_its_counts(
    ctx: Any, wire: Any
) -> None:
    """Verifies metrics_detail records runnable, declined, and refused rows."""
    library = {
        "plain": make_code_metric({"score": 1.0}, name="plain"),
    }
    wire([make_page([_case("s1")])], library)

    _run(ctx)

    detail = ctx.state["metrics_detail"]["plain"]
    assert detail["kind"] == "local"
    assert detail["expected"] == "The agent answers."
    assert detail["threshold"] == pytest.approx(1.0)
    assert detail["model_modules"] == []


def test_a_library_with_nothing_local_does_not_fail_the_investigation(
    ctx: Any, wire: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skips page evaluation when the library has no runnable local metrics."""
    library = MetricLibrary()
    library.declined = (
        (
            "bleu",
            "it only parameterizes a metric computed elsewhere",
            "predefined",
        ),
    )
    monkeypatch.setattr(
        _common, "metric_library_factory", lambda _ctx: (library, "")
    )
    monkeypatch.setattr(
        runner, "build_instance", _raise(ValueError("corrupt session"))
    )
    fetcher = _Fetcher([make_page([make_agent_case("s1")])])
    monkeypatch.setattr(_common, "fetcher_factory", lambda _ctx: fetcher)

    _run(ctx)

    assert sorted(ctx.state["metrics_detail"]) == ["bleu"]


def test_a_session_no_metric_could_be_shaped_for_errors_every_metric(
    ctx: Any, wire: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records an error per local metric when build_instance raises."""
    wire(
        [make_page([make_agent_case("s1"), make_agent_case("s2")])],
        {"m": make_code_metric(1.0)},
    )
    real = runner.build_instance

    def _shape(eval_case: Any) -> Any:
        if getattr(eval_case, "eval_case_id", "") == "s2":
            raise ValueError("corrupt session")
        return real(eval_case)

    monkeypatch.setattr(runner, "build_instance", _shape)

    _run(ctx)

    assert ctx.state["metrics_by_name"] == {
        "m": {"passed": 1, "failed": 0, "errored": 1}
    }


def _raise(exc: Exception) -> Any:
    """Returns a callable that always raises the specified exception.

    Args:
        exc: Exception to raise.

    Returns:
        Callable raising the given exception.
    """

    def _fail(_eval_case: Any) -> Any:
        raise exc

    return _fail
