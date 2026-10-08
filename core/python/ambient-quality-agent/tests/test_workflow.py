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

"""Tests for the ADK workflow graph, its nodes, and the orchestration that
drives the graph end to end (schedule -> keyword dispatch -> execute)."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import (
    EvalCase,
    EvalCaseMetricResult,
    EvalCaseResult,
    EvaluationDataset,
    EvaluationResult,
    LLMMetric,
    MetricResult,
    ResponseCandidateResult,
    Rubric,
    RubricContent,
    RubricContentProperty,
    RubricVerdict,
)
from ambient_quality_agent import backends
from ambient_quality_agent.config import Config
from ambient_quality_agent.core import nodes, orchestrator
from ambient_quality_agent.core.investigation import job_scheduling as schedule
from ambient_quality_agent.core.investigation.model import RunStatus
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.core.workflow import investigation_workflow
from ambient_quality_agent.tools.documents import goal as goal_doc
from ambient_quality_agent.tools.documents.goal import InvestigationGoal
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.base import BigQueryJobFetcher
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)
from ambient_quality_agent.tools.ingestion.models import IngestionCounts, Page
from ambient_quality_agent.tools.metrics.library import (
    CodeMetric,
    MetricLibrary,
    RemoteCodeMetric,
    RemoteJudgeMetric,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.review import session_review
from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START
from google.genai import types as gt

from .conftest import (
    CallbackContext,
    FakeEvaluator,
    FakeFetcher,
    FakeReviewCall,
    StateContext,
    ToolContext,
    get_run,
    make_agent_case,
    make_code_metric,
    make_config,
)


def _patch_config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Config:
    cfg = make_config(**overrides)
    monkeypatch.setattr("ambient_quality_agent.config.config", cfg)
    return cfg


def _state(metric_type: MetricType, metrics: list[str]) -> dict[str, Any]:
    """Builds workflow state for a metric-scoring run of one evaluation scope.

    Args:
        metric_type: Multi-turn or single-turn evaluation metric type.
        metrics: List of metric names to evaluate.

    Returns:
        Workflow state dictionary serialized from WorkflowState.
    """
    metrics_key = (
        "multi_turn_metrics"
        if metric_type is MetricType.MULTI_TURN
        else "single_turn_metrics"
    )
    return WorkflowState(
        observed_agent_name="test-agent",
        project_id="test-project",
        location="us-central1",
        quality_analysis_mode="eval_service",
        budget_per_metric=10,
        window_start=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 1, 8, tzinfo=dt.UTC),
        **{metrics_key: metrics},  # type: ignore[arg-type]
    ).model_dump(mode="json")


def _ctx(state: dict[str, Any]) -> mock.MagicMock:
    """Creates a minimal mock context object wrapping state for workflow nodes.

    Args:
        state: Workflow state dictionary.

    Returns:
        Mock context object.
    """
    ctx = mock.MagicMock()
    ctx.state = state
    return ctx


def _case_result(metric: str, score: float) -> EvalCaseResult:
    """Builds an evaluation case result with a single metric score.

    Args:
        metric: Metric name string.
        score: Score value for the metric result.

    Returns:
        Constructed EvalCaseResult instance.
    """
    return EvalCaseResult(
        eval_case_index=0,
        response_candidate_results=[
            ResponseCandidateResult(
                response_index=0,
                metric_results={metric: MetricResult(score=score)},
            )
        ],
    )


def _rubric_case_result(
    case_index: int, verdicts: list[bool]
) -> EvalCaseResult:
    """Builds an evaluation case result carrying rubric verdicts.

    Args:
        case_index: Positional index of the evaluation case.
        verdicts: List of boolean verdicts for the evaluated rubrics.

    Returns:
        Constructed EvalCaseResult containing rubric verdicts.
    """
    return EvalCaseResult(
        eval_case_index=case_index,
        response_candidate_results=[
            ResponseCandidateResult(
                response_index=0,
                metric_results={
                    "task_success": EvalCaseMetricResult(
                        score=1.0,
                        rubric_verdicts=[
                            RubricVerdict(
                                evaluated_rubric=Rubric(
                                    rubric_id=f"r{i}",
                                    content=RubricContent(
                                        property=RubricContentProperty(
                                            description="d"
                                        )
                                    ),
                                ),
                                verdict=verdict,
                                reasoning="because",
                            )
                            for i, verdict in enumerate(verdicts)
                        ],
                    )
                },
            )
        ],
    )


def test_graph_branches_on_the_quality_analysis_mode() -> None:
    """Verify workflow graph routes between producer branches to a common consumer."""
    graph = investigation_workflow.graph
    assert graph is not None

    assert sorted(n.name for n in graph.nodes) == sorted(
        [
            START.name,
            "init",
            "eval_multi_turn",
            "eval_single_turn",
            "review",
            "insight_correlation",
        ]
    )
    assert {
        (e.from_node.name, e.to_node.name, e.route) for e in graph.edges
    } == {
        (START.name, "init", None),
        ("init", "eval_multi_turn", "eval_service"),
        ("init", "review", "session_review"),
        ("eval_multi_turn", "eval_single_turn", None),
        ("eval_single_turn", "insight_correlation", None),
        ("review", "insight_correlation", None),
    }
    assert graph._terminal_node_names == {"insight_correlation"}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param("eval_service", ["eval_multi_turn"], id="eval_service"),
        pytest.param("session_review", ["review"], id="session_review"),
    ],
)
def test_init_routes_to_one_producer(mode: str, expected: list[str]) -> None:
    """Verify init node routes exclusively to the mode-specified producer branch."""
    ctx = _ctx(
        {"observed_agent_name": "test-agent", "quality_analysis_mode": mode}
    )

    event = nodes.init_node._func(ctx)

    assert event.actions.route == mode
    graph = investigation_workflow.graph
    assert graph is not None
    assert graph.get_next_pending_nodes("init", mode) == expected


def test_init_omits_the_metric_lines_in_review_mode() -> None:
    """Verify init node suppresses metric output in session_review mode."""
    ctx = _ctx(
        {
            "observed_agent_name": "test-agent",
            "quality_analysis_mode": "session_review",
            "multi_turn_metrics": ["task_success"],
        }
    )

    event = nodes.init_node._func(ctx)

    assert event.content is not None
    text = event.content.parts[0].text
    assert "`session_review`" in text
    assert "task_success" not in text


def test_init_emits_progress_from_seeded_state() -> None:
    # Pin eval_service mode so init renders metric lines from seeded inputs.
    ctx = _ctx(
        {
            "observed_agent_name": "test-agent",
            "quality_analysis_mode": "eval_service",
            "multi_turn_metrics": ["task_success"],
            "single_turn_metrics": ["safety"],
            "budget_per_metric": 50,
            "window_start": "2026-01-01T00:00:00+00:00",
            "window_end": "2026-01-08T00:00:00+00:00",
        }
    )

    event = nodes.init_node._func(ctx)

    assert event.content is not None
    text = event.content.parts[0].text
    assert text == event.output
    assert "test-agent" in text and "task_success" in text and "safety" in text
    # Render-only: emits the seeded budget/window, not re-derived values.
    assert "`50`" in text and "2026-01-08" in text
    # The emitted snippet is also recorded in the run's progress log.
    assert ctx.state["progress_log"] == [text]


def test_init_renders_none_with_no_metrics() -> None:
    ctx = _ctx(
        {
            "observed_agent_name": "test-agent",
            "quality_analysis_mode": "eval_service",
            "multi_turn_metrics": [],
            "single_turn_metrics": [],
            "budget_per_metric": 0,
        }
    )

    event = nodes.init_node._func(ctx)

    assert event.content is not None
    text = event.content.parts[0].text
    assert "Multi-turn metrics: _none_" in text
    assert "Single-turn metrics: _none_" in text


@pytest.mark.parametrize(
    "state_overrides",
    [
        pytest.param({"budget_per_metric": 0}, id="zero_budget"),
        pytest.param({"multi_turn_metrics": []}, id="no_metrics"),
    ],
)
def test_eval_skips(
    patched_factories: dict[str, Any],
    state_overrides: dict[str, Any],
) -> None:
    state = _state(MetricType.MULTI_TURN, ["task_success"]) | state_overrides
    fetcher = FakeFetcher()
    patched_factories["fetcher"] = fetcher
    patched_factories["evaluator"] = FakeEvaluator()

    nodes.eval_multi_turn_node._func(_ctx(state))

    assert fetcher.page_tokens_seen == []
    assert state["finding_sets"] == []


def test_eval_runs_full_page_loop(
    patched_factories: dict[str, Any],
) -> None:
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase(), EvalCase()]),
                next_page_token="page-2",
            ),
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase()]),
                next_page_token=None,
            ),
        ]
    )
    patched_factories["evaluator"] = FakeEvaluator(
        [
            EvaluationResult(eval_case_results=[_case_result("M", 0.9)]),
            EvaluationResult(eval_case_results=[_case_result("M", 0.7)]),
        ]
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    fetcher = patched_factories["fetcher"]
    assert fetcher.page_tokens_seen == [None, "page-2"]
    assert len(state["finding_sets"]) == 2
    assert state["multi_turn_pages_evaluated"] == 2


def test_eval_keeps_the_other_scope_findings_when_it_skips(
    patched_factories: dict[str, Any],
) -> None:
    """Verify that a skipped evaluation scope preserves existing finding sets."""
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase()]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["evaluator"] = FakeEvaluator(
        [EvaluationResult(eval_case_results=[_rubric_case_result(0, [False])])]
    )

    nodes.eval_multi_turn_node._func(_ctx(state))
    multi_turn_sets = list(state["finding_sets"])
    assert [len(s["findings"]) for s in multi_turn_sets] == [1]

    # Single-turn scope has no configured metrics and skips execution.
    nodes.eval_single_turn_node._func(_ctx(state))

    assert state["finding_sets"] == multi_turn_sets
    assert state["single_turn_pages_evaluated"] == 0


def test_eval_logs_around_each_page(
    patched_factories: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each non-empty page logs its evaluate -> store bracket.

    Memory is not asserted here: it is prepended per line by the run-scoped
    log factory (covered in test_logging), not by these call sites.
    """
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase(), EvalCase()]),
                next_page_token="page-2",
            ),
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase()]),
                next_page_token=None,
            ),
        ]
    )
    patched_factories["evaluator"] = FakeEvaluator(
        [
            EvaluationResult(eval_case_results=[_case_result("M", 0.9)]),
            EvaluationResult(eval_case_results=[_case_result("M", 0.7)]),
        ]
    )

    with caplog.at_level(logging.INFO):
        nodes.eval_multi_turn_node._func(_ctx(state))

    messages = [r.getMessage() for r in caplog.records]
    assert any("page 0 evaluated" in m for m in messages)
    assert any("page 0 stored" in m for m in messages)
    assert any("page 1 evaluated" in m for m in messages)
    assert any("page 1 stored" in m for m in messages)


def _error_case_result(metric: str) -> EvalCaseResult:
    """Builds an evaluation case result with an error message instead of a score.

    Args:
        metric: Metric name that errored.

    Returns:
        Constructed EvalCaseResult instance.
    """
    return EvalCaseResult(
        eval_case_index=0,
        response_candidate_results=[
            ResponseCandidateResult(
                response_index=0,
                metric_results={
                    metric: EvalCaseMetricResult(error_message="boom")
                },
            )
        ],
    )


def test_eval_accumulates_outcome_counts(
    patched_factories: dict[str, Any],
) -> None:
    """Each eval node tallies pass/fail/error and accumulates across
    scopes into a single per-investigation count."""
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase()]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["evaluator"] = FakeEvaluator(
        [
            EvaluationResult(
                eval_case_results=[
                    _case_result("task_success", 1.0),  # pass
                    _case_result("task_success", 0.0),  # fail
                    _error_case_result("task_success"),  # error
                ]
            )
        ]
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    assert state["eval_outcome_counts"] == {
        "passed": 1,
        "failed": 1,
        "errored": 1,
    }

    # A second scope accumulates onto the same running tally.
    state["single_turn_metrics"] = ["safety"]
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase()]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["evaluator"] = FakeEvaluator(
        [EvaluationResult(eval_case_results=[_case_result("safety", 0.0)])]
    )

    nodes.eval_single_turn_node._func(_ctx(state))

    assert state["eval_outcome_counts"] == {
        "passed": 1,
        "failed": 2,
        "errored": 1,
    }


def test_eval_counts_the_traces_through_every_stage(
    patched_factories: dict[str, Any],
) -> None:
    """The node records what the window held, what was ingested, and how the
    evaluated traces came out -- per trace, not per (trace, metric)."""
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase(), EvalCase()]),
                next_page_token=None,
            )
        ],
        counts=IngestionCounts(scanned=90, ingested=2, partial=1, failed=3),
    )
    patched_factories["evaluator"] = FakeEvaluator(
        [
            EvaluationResult(
                eval_case_results=[
                    # One case failing under two metrics is one failed trace.
                    _case_result("task_success", 0.0),
                    _case_result("task_success", 0.0),
                    _rubric_case_result(1, verdicts=[True, True]),
                ]
            )
        ]
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    counters = state["counters"]
    assert counters["traces_scanned"] == 90
    assert counters["traces_ingested"] == 2
    assert counters["traces_ingested_partial"] == 1
    assert counters["traces_ingested_failed"] == 3
    assert counters["traces_evaluated"] == 2
    assert counters["traces_eval_failed"] == 1
    assert counters["traces_eval_passed"] == 1
    assert counters["rubrics_generated"] == 2


def test_eval_counts_ingestion_even_with_nothing_to_evaluate(
    patched_factories: dict[str, Any],
) -> None:
    """A window whose every trace was dropped still has to say so -- that is
    the case these counters exist to make visible."""
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(dataset=EvaluationDataset(eval_cases=[]), next_page_token=None)
        ],
        counts=IngestionCounts(scanned=40, failed=40),
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    assert state["counters"]["traces_scanned"] == 40
    assert state["counters"]["traces_ingested_failed"] == 40
    assert state["counters"].get("traces_evaluated", 0) == 0


def test_an_empty_window_explains_itself_in_the_run_event(
    patched_factories: dict[str, Any],
) -> None:
    """The delivery end to end: the node emits the warning as its own event.

    An eval node renders `lines[0]` through `record_progress_event`, which becomes an
    `investigation_events` row and is drawn by `RunEventLedger` -- so covering
    the event here covers the dashboard, and no UI change is needed to show it.
    """
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(dataset=EvaluationDataset(eval_cases=[]), next_page_token=None)
        ],
        counts=IngestionCounts(scanned=0),
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    event = "\n".join(state["progress_log"])
    assert "no traces at all" in event
    assert "AQA_OBSERVED_AGENT_NAME" in event
    assert state["counters"]["traces_scanned"] == 0


def test_a_window_that_dropped_every_trace_is_not_blamed_on_the_agent_name(
    patched_factories: dict[str, Any],
) -> None:
    """Ingestion lost all 40, which the counters already report. Telling the
    reader their agent name might be wrong would send them to the wrong place."""
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(dataset=EvaluationDataset(eval_cases=[]), next_page_token=None)
        ],
        counts=IngestionCounts(scanned=40, failed=40),
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    event = "\n".join(state["progress_log"])
    assert "no traces at all" not in event
    assert "AQA_OBSERVED_AGENT_NAME" not in event


def test_eval_counts_nothing_when_the_scope_is_skipped(
    patched_factories: dict[str, Any],
) -> None:
    """A scope with no metrics scans nothing, so it must not report a window it
    never looked at."""
    state = _state(MetricType.MULTI_TURN, [])
    patched_factories["fetcher"] = FakeFetcher(
        counts=IngestionCounts(scanned=90)
    )

    nodes.eval_multi_turn_node._func(_ctx(state))

    assert state.get("counters", {}) == {}


def test_eval_survives_a_failing_scanned_count(
    patched_factories: dict[str, Any],
) -> None:
    """Counting the window is one extra query for a figure the run reports, not
    one it needs; losing it must not cost the evaluation."""
    state = _state(MetricType.MULTI_TURN, ["task_success"])
    fetcher = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[EvalCase()]),
                next_page_token=None,
            )
        ],
        counts=IngestionCounts(ingested=1),
    )
    fetcher.count_scanned = mock.MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("no such table")
    )
    patched_factories["fetcher"] = fetcher

    nodes.eval_multi_turn_node._func(_ctx(state))

    assert state["counters"]["traces_scanned"] == 0
    assert state["counters"]["traces_evaluated"] == 1


# --------------------------------------------------------------------------- #
# review: the LLM reviewer producer                                  #
# --------------------------------------------------------------------------- #


def _review_state(**overrides: Any) -> dict[str, Any]:
    """Builds workflow state for a session review run.

    Args:
        **overrides: Key-value overrides for the workflow state.

    Returns:
        Workflow state dictionary configured for session_review mode.
    """
    return (
        _state(MetricType.MULTI_TURN, [])
        | {"quality_analysis_mode": "session_review"}
        | overrides
    )


def _session(
    case_id: str,
    agent: str = "root_agent",
    tools: list[str] | None = None,
    instruction: str = "be helpful",
) -> EvalCase:
    """Constructs an EvalCase fixture with embedded case_id in user turns.

    Args:
        case_id: Session identifier string.
        agent: Agent name string.
        tools: Optional list of tool function declaration names.
        instruction: System instruction for the agent.

    Returns:
        Configured EvalCase instance.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {
                    agent: {
                        "agent_id": agent,
                        "instruction": instruction,
                        "tools": [
                            {"function_declarations": [{"name": name}]}
                            for name in tools or []
                        ],
                    }
                },
                "turns": [
                    {
                        "turn_index": 0,
                        "events": [
                            {
                                "author": "user",
                                "content": {
                                    "role": "user",
                                    "parts": [{"text": f"session {case_id}"}],
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )


def _finding_response(*expectations: str) -> str:
    """Generates a canned review response containing findings for given expectations.

    Args:
        *expectations: Strings describing expected behavior for each finding.

    Returns:
        JSON-encoded string representing the review response.
    """
    return json.dumps(
        {
            "findings": [
                {
                    "agent_id": "root_agent",
                    "expected_behavior": expectation,
                    "actual_behavior": "did nothing",
                }
                for expectation in expectations
            ]
        }
    )


def test_review_skips_on_zero_budget(
    patched_factories: dict[str, Any],
) -> None:
    state = _review_state(budget_per_metric=0)
    fetcher = FakeFetcher()
    patched_factories["fetcher"] = fetcher

    nodes.review_node._func(_ctx(state))

    assert fetcher.page_tokens_seen == []
    assert state["finding_sets"] == []


def test_review_runs_full_page_loop(
    patched_factories: dict[str, Any],
) -> None:
    """Verify all pages are reviewed and generate corresponding FindingSets."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[_session("a"), _session("b")]
                ),
                next_page_token="page-2",
            ),
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("c")]),
                next_page_token=None,
            ),
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    nodes.review_node._func(_ctx(state))

    fetcher = patched_factories["fetcher"]
    assert fetcher.page_tokens_seen == [None, "page-2"]
    assert [len(s["findings"]) for s in state["finding_sets"]] == [2, 1]
    assert state["multi_turn_pages_evaluated"] == 2
    # Verify each session triggers an independent review call.
    assert len(patched_factories["reviewer"].prompts) == 3


def test_review_counts_each_session_outcome(
    patched_factories: dict[str, Any],
) -> None:
    """Verify session outcomes (pass, fail, error) update respective counters."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[
                        _session("ok"),
                        _session("bad"),
                        _session("boom"),
                    ]
                ),
                next_page_token=None,
            )
        ],
        counts=IngestionCounts(scanned=30, ingested=3),
    )

    def reviewer(prompt: str) -> str:
        # Match the "session <id>" marker from `_session` to avoid false positives
        # in the reviewer prompt instructions.
        if "session boom" in prompt:
            raise RuntimeError("model unreachable")
        if "session bad" in prompt:
            return _finding_response("call create_ticket", "answer in English")
        return '{"findings": []}'

    patched_factories["reviewer"] = reviewer

    nodes.review_node._func(_ctx(state))

    counters = state["counters"]
    assert counters["traces_scanned"] == 30
    assert counters["traces_evaluated"] == 3
    assert counters["traces_eval_passed"] == 1
    assert counters["traces_eval_failed"] == 1
    assert counters["traces_eval_errored"] == 1
    # Verify distinct defects remain unmerged.
    assert counters["findings_generated"] == 2
    # Rubrics counter is not incremented during session review.
    assert counters.get("rubrics_generated", 0) == 0
    (finding_set,) = state["finding_sets"]
    assert [f["session_id"] for f in finding_set["findings"]] == ["bad", "bad"]
    # Store traces only for sessions with reported findings.
    assert list(finding_set["sessions"]) == ["bad"]


def test_review_passes_the_session_review_focus_to_every_session(
    patched_factories: dict[str, Any],
) -> None:
    """Verify review_node propagates the session review focus from state to all session prompts."""
    state = _review_state(session_review_focus="tool arguments only")
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[_session("a"), _session("b")]
                ),
                next_page_token=None,
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall()

    nodes.review_node._func(_ctx(state))

    prompts = patched_factories["reviewer"].prompts
    assert len(prompts) == 2
    assert all("<session_review_focus>" in prompt for prompt in prompts)
    assert all(prompt.count("tool arguments only") == 1 for prompt in prompts)


def test_review_without_a_focus_leaves_the_prompt_unnarrowed(
    patched_factories: dict[str, Any],
) -> None:
    """Verify review_node leaves prompts unfocused when no focus is configured in state."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall()

    nodes.review_node._func(_ctx(state))

    (prompt,) = patched_factories["reviewer"].prompts
    assert "session_review_focus" not in prompt


def test_review_fails_when_every_review_fails(
    patched_factories: dict[str, Any],
) -> None:
    """Verify exception is raised when all session reviews fail."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[_session("a"), _session("b")]
                ),
                next_page_token=None,
            )
        ]
    )

    def reviewer(prompt: str) -> str:
        raise RuntimeError("model unreachable")

    patched_factories["reviewer"] = reviewer

    with pytest.raises(RuntimeError, match="model unreachable"):
        nodes.review_node._func(_ctx(state))


def test_review_carries_each_sessions_own_configuration(
    patched_factories: dict[str, Any],
) -> None:
    """Two sessions on one build can run different system instructions, and
    each is recorded against the session that ran it.
    `test_review_keeps_a_toolset_that_differs_within_one_revision`
    pins the same for a toolset."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[
                        _session("a", instruction="answer in English"),
                        _session("b", instruction="answer in Italian"),
                    ]
                ),
                next_page_token=None,
                revisions={"a": "4", "b": "4"},
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    nodes.review_node._func(_ctx(state))

    (finding_set,) = state["finding_sets"]
    assert {
        session_id: [
            (r["agent_id"], r["revision_id"], r["instruction"])
            for r in revisions
        ]
        for session_id, revisions in finding_set["agent_revisions"].items()
    } == {
        "a": [("root_agent", "4", "answer in English")],
        "b": [("root_agent", "4", "answer in Italian")],
    }


def test_review_keeps_a_toolset_that_differs_within_one_revision(
    patched_factories: dict[str, Any],
) -> None:
    """Two sessions on one build can run different toolsets -- dynamically
    filtered tools, per-tenant subsets, request-scoped toolsets. Collapsing them
    by agent and revision would judge the second session against the first
    session's tools, which is how a real call reads as a hallucinated one."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[
                        _session("a", tools=["create_incident"]),
                        _session("b", tools=["create_case"]),
                    ]
                ),
                next_page_token=None,
                revisions={"a": "4", "b": "4"},
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    nodes.review_node._func(_ctx(state))

    (finding_set,) = state["finding_sets"]
    assert {
        session_id: [t["name"] for r in revisions for t in r["tools"]]
        for session_id, revisions in finding_set["agent_revisions"].items()
    } == {"a": ["create_incident"], "b": ["create_case"]}


def test_review_carries_no_configuration_for_a_passing_session(
    patched_factories: dict[str, Any],
) -> None:
    """A configuration nothing failed under is weight the persisted state
    carries and the verification pass never reads."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[
                        _session("ok", agent="quiet_agent"),
                        _session("bad", agent="root_agent"),
                    ]
                ),
                next_page_token=None,
            )
        ]
    )

    def reviewer(prompt: str) -> str:
        if "session bad" in prompt:
            return _finding_response("call create_ticket")
        return '{"findings": []}'

    patched_factories["reviewer"] = reviewer

    nodes.review_node._func(_ctx(state))

    (finding_set,) = state["finding_sets"]
    assert {
        session_id: [r["agent_id"] for r in revisions]
        for session_id, revisions in finding_set["agent_revisions"].items()
    } == {"bad": ["root_agent"]}


def test_review_attributes_findings_to_the_session_revision(
    patched_factories: dict[str, Any],
) -> None:
    """The reviewer reads one conversation and cannot know which build served it;
    the fetcher resolved that per session, so the node is what carries the
    revision onto the findings. Without this the review path -- the mode that
    becomes the default -- would report every defect unattributed."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[_session("a"), _session("b")]
                ),
                next_page_token=None,
                revisions={"a": "4"},
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    nodes.review_node._func(_ctx(state))

    (finding_set,) = state["finding_sets"]
    by_session = {
        f["session_id"]: f["agent_revision"] for f in finding_set["findings"]
    }
    assert by_session["a"] == "4"
    # A session the telemetry did not attribute keeps the single unnamed
    # revision rather than borrowing another session's.
    assert by_session["b"] == ""


def test_review_keeps_findings_from_another_producer(
    patched_factories: dict[str, Any],
) -> None:
    """Verify existing finding_sets in state are preserved and appended to."""
    state = _review_state()
    seeded: dict[str, Any] = {"findings": [], "sessions": {}}
    state["finding_sets"] = [seeded]
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    nodes.review_node._func(_ctx(state))

    assert state["finding_sets"][0] == seeded
    assert len(state["finding_sets"]) == 2


def test_review_skips_when_no_sessions_are_found(
    patched_factories: dict[str, Any],
) -> None:
    """Verify review execution handles empty telemetry datasets gracefully."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(dataset=EvaluationDataset(eval_cases=[]), next_page_token=None)
        ],
        counts=IngestionCounts(scanned=40, failed=40),
    )

    nodes.review_node._func(_ctx(state))

    assert state["finding_sets"] == []
    assert state["counters"]["traces_scanned"] == 40
    assert state["counters"].get("traces_evaluated", 0) == 0


def test_review_sends_the_goal_with_every_session_and_records_it(
    patched_factories: dict[str, Any],
) -> None:
    """Read once per run, sent with every review, and recorded on the run with
    the hash of the exact text it added to the prompt."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[_session("a"), _session("b")]
                ),
                next_page_token="page-2",
            ),
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("c")]),
                next_page_token=None,
            ),
        ]
    )
    goal = InvestigationGoal(text="Refunds first.", source="saved")
    loader = mock.Mock(return_value=goal)

    with mock.patch.object(_common, "goal_loader", loader):
        nodes.review_node._func(_ctx(state))

    loader.assert_called_once()
    section = session_review.render_goal_section("Refunds first.")
    prompts = patched_factories["reviewer"].prompts
    assert len(prompts) == 3
    assert all(prompt.endswith(section) for prompt in prompts)
    assert state["goal"] == {
        "version": goal.version,
        "source": "saved",
        "prompt_sha256": hashlib.sha256(section.encode("utf-8")).hexdigest(),
    }
    assert f"goal version `{goal.version}`" in state["progress_log"][-1]


def test_review_without_a_readable_goal_reviews_without_one(
    patched_factories: dict[str, Any],
) -> None:
    state = _review_state()
    patched_factories["goal"] = InvestigationGoal(source="unreadable")
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )

    nodes.review_node._func(_ctx(state))

    (prompt,) = patched_factories["reviewer"].prompts
    assert prompt.endswith(session_review._REVIEW_TASK)
    assert state["goal"] == {
        "version": None,
        "source": "unreadable",
        "prompt_sha256": None,
    }
    assert "goal could not be read" in state["progress_log"][-1]


def test_the_goal_never_reaches_the_finding_sets(
    patched_factories: dict[str, Any],
) -> None:
    """The verification pass judges candidates from the finding sets alone, so
    keeping the goal out of them keeps it away from the verifier."""
    state = _review_state()
    patched_factories["goal"] = InvestigationGoal(
        text="GOAL-MARKER refunds first.", source="saved"
    )
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    nodes.review_node._func(_ctx(state))

    assert state["finding_sets"][0]["findings"]
    assert "GOAL-MARKER" not in json.dumps(state["finding_sets"])


def test_the_default_goal_loader_reads_the_jobs_store(
    monkeypatch: pytest.MonkeyPatch, jobs_store: Any
) -> None:
    stores: list[Any] = []

    def load(store: Any) -> InvestigationGoal:
        stores.append(store)
        return InvestigationGoal()

    monkeypatch.setattr(goal_doc, "load_investigation_goal", load)

    _common._load_default_goal(_ctx({"jobs_gcs_bucket": "aqa-jobs"}))

    assert stores == [jobs_store]


def test_full_workflow_runs_through_adk(
    patched_factories: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default config runs the session-review chain: init -> review -> correlation."""
    cfg = _patch_config(monkeypatch)
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )

    runner = Runner(
        app_name="aqa-test",
        agent=investigation_workflow,
        session_service=InMemorySessionService(),
    )

    # Seed the initial graph state (config and window/budget) the way
    # run_investigation_graph does.
    seed = WorkflowState.from_config(cfg).model_dump(mode="json")
    seed["window_start"] = "2026-01-01T00:00:00+00:00"
    seed["window_end"] = "2026-01-08T00:00:00+00:00"
    seed["budget_per_metric"] = 50

    async def _drive() -> tuple[list[str], dict[str, Any]]:
        session = await runner.session_service.create_session(
            app_name="aqa-test", user_id="u", state=seed
        )
        texts = [
            event.content.parts[0].text
            async for event in runner.run_async(
                user_id="u",
                session_id=session.id,
                new_message=gt.Content(role="user", parts=[gt.Part(text="go")]),
            )
            if event.content
            and event.content.parts
            and event.content.parts[0].text
        ]
        final = await runner.session_service.get_session(
            app_name="aqa-test", user_id="u", session_id=session.id
        )
        return texts, (dict(final.state) if final is not None else {})

    texts, final_state = asyncio.run(_drive())
    (
        init_text,
        review_text,
        insights_text,
    ) = texts
    assert "test-agent" in init_text
    assert "Session review:" in review_text
    # insight_correlation is the terminal node and always runs. The fake
    # reviewer reports no findings, so it correlates nothing this sweep.
    assert "Insights" in insights_text
    # Every node that calls `record_progress_event` records to the progress log; the
    # 3-node chain is init, review, and insight_correlation.
    progress_log = final_state["progress_log"]
    assert progress_log == [
        init_text,
        review_text,
        insights_text,
    ]
    # review tallies each session it reviewed; the fake reviewer
    # returns no findings, so the one session passes.
    counters = final_state["counters"]
    assert counters["traces_evaluated"] == 1
    assert counters["traces_eval_passed"] == 1
    assert counters["traces_eval_failed"] == 0


def test_review_mode_workflow_runs_the_review_branch_only(
    patched_factories: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify session_review workflow executes only the review branch before correlation."""
    cfg = _patch_config(monkeypatch, quality_analysis_mode="session_review")
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["reviewer"] = FakeReviewCall(
        _finding_response("call a tool")
    )

    runner = Runner(
        app_name="aqa-test",
        agent=investigation_workflow,
        session_service=InMemorySessionService(),
    )
    seed = WorkflowState.from_config(cfg).model_dump(mode="json")
    seed["window_start"] = "2026-01-01T00:00:00+00:00"
    seed["window_end"] = "2026-01-08T00:00:00+00:00"
    seed["budget_per_metric"] = 50

    async def _drive() -> dict[str, Any]:
        session = await runner.session_service.create_session(
            app_name="aqa-test", user_id="u", state=seed
        )
        async for _ in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=gt.Content(role="user", parts=[gt.Part(text="go")]),
        ):
            pass
        final = await runner.session_service.get_session(
            app_name="aqa-test", user_id="u", session_id=session.id
        )
        return dict(final.state) if final is not None else {}

    final_state = asyncio.run(_drive())

    progress_log = final_state["progress_log"]
    assert len(progress_log) == 3
    assert "`session_review`" in progress_log[0]
    assert "Session review:" in progress_log[1]
    assert "Insights" in progress_log[2]
    assert not any("evaluation:" in text for text in progress_log)
    assert len(final_state["finding_sets"]) == 1


# --------------------------------------------------------------------------- #
# Orchestration end to end (schedule -> keyword dispatch -> execute) over the  #
# real graph, with telemetry mocked.                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def mocked_telemetry(
    patched_factories: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Wire fakes through the node factories and stub the self-query submit.

    Reuses the graph-level `FakeFetcher`/`FakeEvaluator`; the background run
    invocation is simulated by `_dispatch_run` via the keyword route.
    """
    one_page = Page(
        dataset=EvaluationDataset(eval_cases=[EvalCase(), EvalCase()]),
        next_page_token=None,
    )
    # Fresh fetcher per metric fetch (multi + single), each returning one page.
    patched_factories["fetcher"] = FakeFetcher(pages=[one_page, one_page])
    patched_factories["evaluator"] = FakeEvaluator()
    monkeypatch.setattr(
        schedule,
        "submit_investigation",
        lambda run_id, ctx: {"job_name": "op/1"},
    )
    yield


def _dispatch_run(state: dict[str, Any], run_id: str) -> Any:
    """Simulates the background run invocation hitting the keyword route.

    Args:
        state: Shared state dictionary for the run.
        run_id: Investigation run identifier.

    Returns:
        Response from the before_agent callback.
    """
    cb = CallbackContext(f"{orchestrator.RUN_KEYWORD} {run_id}", state)
    return asyncio.run(orchestrator.before_agent(cb))


def _configure(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> None:
    """Pins the deploy-time configuration the run reads.

    The only way in: nothing in a session can change what a run is scheduled
    with, so a test that needs a non-default setting states it here.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        **fields: Config field overrides to set on env_config.
    """
    monkeypatch.setattr(
        effective_config,
        "env_config",
        dataclasses.replace(effective_config.env_config, **fields),
    )


def test_investigation_end_to_end(
    mocked_telemetry: None,
    investigation_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Metric scoring, so the run walks both evaluation scopes and the event
    # sources below name all four nodes.
    _configure(monkeypatch, quality_analysis_mode="eval_service")
    tc = ToolContext()

    # 1. Submit -> pending, returns immediately. The scheduled inputs are the
    #    run's first row.
    result = asyncio.run(orchestrator.schedule_investigation(tc))
    assert result["status"] == RunStatus.PENDING
    run_id = result["run_id"]
    assert result["trigger_type"] == "manual"

    # 2. The keyword route runs the real graph in a background invocation.
    out = _dispatch_run(tc.state, run_id)
    assert out is not None and run_id in out.parts[0].text

    # 3. Run is done with a summary, visible via get_investigation.
    fetched = asyncio.run(orchestrator.get_investigation(run_id, tc))
    assert fetched["status"] == RunStatus.DONE
    assert fetched["summary"]["multi_turn_pages_evaluated"] >= 1

    # 4. Each node that emitted a progress event wrote its own row against the
    #    run, so the detail view reports who said what.
    assert [e["source"] for e in fetched["events"]] == [
        "init",
        "eval_multi_turn",
        "eval_single_turn",
        "insight_correlation",
    ]
    assert fetched["summary"]["events"] == [
        e["text"] for e in fetched["events"]
    ]

    # 5. ...and the list view carries the same run without those events.
    listed = asyncio.run(orchestrator.list_investigations(tc))["runs"]
    assert [r["run_id"] for r in listed] == [run_id]
    assert listed[0]["events"] == []
    assert "events" not in listed[0]["summary"]


def test_investigation_end_to_end_on_code_metrics(
    mocked_telemetry: None,
    investigation_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third producer, through the real graph: the node tests drive it in
    isolation and the edge set is pinned, but neither proves the route fires.

    The library is published to a bucket, so it is stubbed here rather than
    reaching Cloud Storage.
    """
    metric = CodeMetric(
        name="always_passes",
        expected="The agent answers.",
        evaluate=lambda instance: {"score": 1.0},
    )
    monkeypatch.setattr(
        _common,
        "metric_library_factory",
        lambda ctx: (MetricLibrary({"always_passes": metric}), ""),
    )
    tc = ToolContext()

    result = asyncio.run(orchestrator.schedule_investigation(tc))
    run_id = result["run_id"]
    _dispatch_run(tc.state, run_id)

    fetched = asyncio.run(orchestrator.get_investigation(run_id, tc))
    assert fetched["status"] == RunStatus.DONE
    assert [e["source"] for e in fetched["events"]] == [
        "init",
        "review",
        "insight_correlation",
    ]
    assert fetched["summary"]["metrics_by_name"] == {
        "always_passes": {"passed": 2, "failed": 0, "errored": 0}
    }
    # Both analyzers judge both cases, so the verdicts must still total the
    # cases evaluated: the reviewer and the code metric agreeing on a case is
    # one verdict, and the fixture's two id-less cases are two, not one.
    counters = fetched["counters"]
    assert counters["traces_evaluated"] == 2
    outcomes = (
        "traces_eval_passed",
        "traces_eval_failed",
        "traces_eval_errored",
    )
    assert sum(counters[name] for name in outcomes) == 2


def test_the_configured_window_sets_the_runs_window(
    mocked_telemetry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookback window is the observed agent's setting, and it reaches the
    run through the config the scheduling turn reads."""
    _configure(monkeypatch, data_lookback_window=14)
    tc = ToolContext()

    result = asyncio.run(orchestrator.schedule_investigation(tc))
    _dispatch_run(tc.state, result["run_id"])

    record = get_run(tc.state, result["run_id"])
    window_span_days = (
        dt.datetime.fromisoformat(record.window_end)
        - dt.datetime.fromisoformat(record.window_start)
    ).days
    assert window_span_days == 14


@pytest.mark.parametrize(
    ("source", "expected_class"),
    [
        ("big_query", BigQueryFetcher),
        ("cloud_ops", CloudOpsFetcher),
        ("cloud_logging", CloudLoggingFetcher),
    ],
)
def test_fetcher_factory_builds_each_source_from_unified_config(
    source: str,
    expected_class: type[BigQueryJobFetcher],
) -> None:
    """One dataset/table/location drives whichever fetcher the source selects."""
    state = {
        "project_id": "p",
        # The factory also binds the recorder ingestion writes through, so it
        # needs the sweep's identity as well as the telemetry config.
        "observed_agent_name": "watched-agent",
        "run_id": "run-1",
        "telemetry_ingestion_source": source,
        "telemetry_dataset": "some_dataset",
        "telemetry_table": "some_table",
        "telemetry_location": "europe-west1",
    }
    with (
        mock.patch.object(backends.bigquery, "Client") as bq_client,
        mock.patch.object(_common.storage, "Client"),
    ):
        fetcher = _common._create_default_fetcher(StateContext(state))

    assert isinstance(fetcher, expected_class)
    assert fetcher.table_ref == "p.some_dataset.some_table"
    assert bq_client.call_args.kwargs["location"] == "europe-west1"


def test_fetcher_factory_requires_a_dataset() -> None:
    """A source with no dataset default fails naming the unified setting."""
    state = {
        "project_id": "p",
        "telemetry_ingestion_source": "cloud_logging",
        "telemetry_dataset": "",
    }
    with pytest.raises(ValueError, match="AQA_TELEMETRY_DATASET"):
        _common._create_default_fetcher(StateContext(state))


def test_fetcher_factory_rejects_an_unknown_source() -> None:
    """An unsupported source lists the ones that are."""
    state = {
        "project_id": "p",
        "telemetry_ingestion_source": "nope",
        "telemetry_dataset": "some_dataset",
    }
    with pytest.raises(
        ValueError, match="'big_query', 'cloud_ops', or 'cloud_logging'"
    ):
        _common._create_default_fetcher(StateContext(state))


# --------------------------------------------------------------------------- #
# review: code metrics join the sweep when the library has any                #
# --------------------------------------------------------------------------- #


def _with_library(
    monkeypatch: pytest.MonkeyPatch,
    library: dict[str, Any],
    warning: str = "",
    remote: tuple[Any, ...] = (),
    judged: tuple[Any, ...] = (),
) -> None:
    """Points the metric-library factory at a fixed metric library.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        library: Mapping of metric names to local metric definitions.
        warning: Optional load warning string.
        remote: Tuple of remote code metric definitions.
    """
    published = MetricLibrary(library)
    published.remote = remote
    published.judged = judged
    monkeypatch.setattr(
        _common, "metric_library_factory", lambda ctx: (published, warning)
    )


def test_review_runs_alone_when_no_metric_is_published(
    patched_factories: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing nothing leaves the reviewer as the only evaluation."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    _with_library(monkeypatch, {})

    event = nodes.review_node._func(_ctx(state))

    assert "metric" not in event.content.parts[0].text.lower()


def test_publishing_a_metric_adds_it_beside_the_review(
    patched_factories: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One published metric turns code metrics on for the same sweep, with no
    mode to select and no second pass over the window."""
    state = _review_state()
    fetcher = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    patched_factories["fetcher"] = fetcher
    _with_library(monkeypatch, {"m": make_code_metric({"score": 1.0})})

    event = nodes.review_node._func(_ctx(state))

    text = event.content.parts[0].text
    assert "Session review:" in text
    assert "metric(s)" in text
    # One ingestion serves both evaluations: the window is paged once.
    assert fetcher.page_tokens_seen == [None]


def test_publishing_a_remote_metric_runs_it_on_the_same_sweep(
    patched_factories: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metric marked remote reaches the eval service through the review node.

    The unit tests drive the evaluation directly; this proves the node builds
    it at all, which a library holding no local metric would otherwise skip.
    """
    state = _review_state()
    fetcher = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[make_agent_case("a", text="Hello.")]
                ),
                next_page_token=None,
            )
        ]
    )
    patched_factories["fetcher"] = fetcher
    # A service that answers about the case, because answering about
    # nothing fails the evaluation rather than reporting a clean sweep.
    evaluator = FakeEvaluator(
        results=[
            EvaluationResult(
                eval_case_results=[
                    EvalCaseResult(
                        eval_case_index=0,
                        response_candidate_results=[
                            ResponseCandidateResult(
                                response_index=0,
                                # Keyed as the service keys it -- lowercased,
                                # whatever the config spelled. A lookup built
                                # from the config spelling misses this.
                                metric_results={
                                    "remote_one": EvalCaseMetricResult(
                                        score=1.0
                                    )
                                },
                            )
                        ],
                    )
                ]
            )
        ]
    )
    patched_factories["evaluator"] = evaluator
    _with_library(
        monkeypatch,
        {},
        remote=(
            RemoteCodeMetric(
                name="Remote_One",
                service_name="remote_one",
                expected="The agent answers.",
                source="def evaluate(instance):\n    return 1.0\n",
            ),
        ),
    )

    event = nodes.review_node._func(_ctx(state))

    text = event.content.parts[0].text
    assert "Remote custom metrics:" in text
    # Named with a capital on purpose: the SDK lowercases it, so a node that
    # keyed its lookup by the config spelling would match no result and report
    # this session a clean pass.
    assert "never returned a verdict" not in text
    dataset, metrics = evaluator.calls[0]
    # The author's source travelled to the service rather than running here.
    assert [m.custom_function for m in metrics] == [
        "def evaluate(instance):\n    return 1.0\n"
    ]
    # And it travelled with the agent's answer attached. Sent the page's own
    # cases, the service receives no response at all and every remote metric
    # judges an answer it cannot see.
    assert [
        c.responses[0].response.parts[0].text for c in dataset.eval_cases
    ] == ["Hello."]
    # One ingestion serves both evaluations: the window is paged once.
    assert fetcher.page_tokens_seen == [None]


def test_review_sends_a_judged_only_library_to_the_eval_service(
    patched_factories: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library holding only judged metrics still turns the remote evaluation
    on -- tested apart from `library.remote`, as that one is from `library`."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(
                    eval_cases=[make_agent_case("a", text="Hello.")]
                ),
                next_page_token=None,
            )
        ]
    )
    evaluator = FakeEvaluator(
        results=[
            EvaluationResult(
                eval_case_results=[
                    EvalCaseResult(
                        eval_case_index=0,
                        response_candidate_results=[
                            ResponseCandidateResult(
                                response_index=0,
                                metric_results={
                                    "j": EvalCaseMetricResult(
                                        score=0.0, explanation="curt"
                                    )
                                },
                            )
                        ],
                    )
                ]
            )
        ]
    )
    patched_factories["evaluator"] = evaluator
    metric = LLMMetric(name="j", prompt_template="Polite? {response}")
    _with_library(
        monkeypatch,
        {},
        judged=(
            RemoteJudgeMetric(
                name="j",
                service_name="j",
                expected="The agent is polite.",
                metric=metric,
                threshold=1.0,
            ),
        ),
    )

    nodes.review_node._func(_ctx(state))

    assert len(evaluator.calls) == 1
    _dataset, metrics = evaluator.calls[0]
    assert metrics == [metric]
    assert state["metrics_detail"]["j"]["kind"] == "judged"
    assert state["metrics_by_name"]["j"] == {
        "passed": 0,
        "failed": 1,
        "errored": 0,
    }


def test_a_library_that_will_not_read_warns_without_failing_the_review(
    patched_factories: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer is still doing its job, so a broken library costs its own
    findings and says so rather than failing the investigation."""
    state = _review_state()
    patched_factories["fetcher"] = FakeFetcher(
        pages=[
            Page(
                dataset=EvaluationDataset(eval_cases=[_session("a")]),
                next_page_token=None,
            )
        ]
    )
    _with_library(monkeypatch, {}, warning=" **could not be read**")

    event = nodes.review_node._func(_ctx(state))

    assert "could not be read" in event.content.parts[0].text
