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

"""The offline runner: the production investigation with nothing in a Cloud project.

Every model call here is a fake, so the runner's contract is what is under
test: what it binds, what it gives back, and what it leaves behind.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any

import pytest
from ambient_quality_agent import backends, offline
from ambient_quality_agent.core import state as core_state
from ambient_quality_agent.core.nodes import (
    _common,
    eval_multi_turn_node,
    init_node,
    insight_correlation_node,
    review_node,
)
from ambient_quality_agent.core.workflow import investigation_workflow
from ambient_quality_agent.tools.documents.goal import InvestigationGoal
from ambient_quality_agent.tools.insights.findings import (
    Finding,
    merge_finding_sets,
)
from google.adk import Runner
from google.adk.workflow import START, Workflow

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.events.event import Event

from .conftest import (
    FAKE_VERIFY_RESPONSE,
    FakeEvaluator,
    FakeFetcher,
    FakeInsightModelCall,
    FakeInsightStore,
    FakeReviewCall,
    ScriptedReviewer,
    make_agent_case,
    make_page,
)

_ONE_FINDING = json.dumps(
    {
        "findings": [
            {
                "agent_id": "root_agent",
                "expected_behavior": "calls the tool the prompt requires",
                "actual_behavior": "answered without calling it",
            }
        ]
    }
)

_ONE_CLUSTER = json.dumps(
    {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
)


def _build_fake_calls() -> offline.ModelCalls:
    return offline.ModelCalls(
        cluster=FakeInsightModelCall(_ONE_CLUSTER),
        merge=FakeInsightModelCall('{"groups": []}'),
        verify=FakeInsightModelCall(FAKE_VERIFY_RESPONSE),
        match=FakeInsightModelCall('{"matches": []}'),
    )


def _run(**overrides: Any) -> offline.OfflineRun:
    arguments: dict[str, Any] = {
        "agent_name": "root_agent",
        "investigation_id": "offline-1",
        "fetcher": lambda: FakeFetcher(
            pages=[make_page([make_agent_case("s1")])]
        ),
        "analysis_mode": "session_review",
        "budget": 10,
        "model_calls": _build_fake_calls(),
        "session_reviewer": ScriptedReviewer(FakeReviewCall(_ONE_FINDING)),
    }
    arguments.update(overrides)
    return offline.run_investigation(**arguments)


def _build_arguments_for(name: str, reply: FakeReviewCall) -> dict[str, Any]:
    """Builds arguments for a single-session run.

    Args:
        name: The session text and run ID suffix.
        reply: The canned reviewer reply that records prompts.

    Returns:
        Keyword arguments for `offline.run_investigation[_async]`.
    """
    return {
        "agent_name": "root_agent",
        "investigation_id": f"offline-{name}",
        "fetcher": lambda: FakeFetcher(
            pages=[make_page([make_agent_case(name, text=f"I am {name}.")])]
        ),
        "analysis_mode": "session_review",
        "budget": 10,
        "model_calls": _build_fake_calls(),
        "session_reviewer": ScriptedReviewer(reply),
    }


def _assert_each_run_kept_to_its_own(
    replies: dict[str, FakeReviewCall], runs: dict[str, offline.OfflineRun]
) -> None:
    # A run that raised in its thread is missing from `runs`.
    assert runs.keys() == replies.keys()
    for name, run in runs.items():
        prompts = replies[name].prompts
        assert [f"I am {name}." in prompt for prompt in prompts] == [True]
        assert len(run.store.insights) == 1
        assert run.state["investigation_id"] == f"offline-{name}"


def _read_bound_providers() -> dict[str, Any]:
    return {
        name: getattr(target.module, target.attribute)
        for name, target in backends.PROVIDERS.items()
    }


def test_a_run_writes_its_insights_to_the_in_memory_store() -> None:
    run = _run()

    assert run.state["counters"]["trajectories_evaluated"] == 1
    assert [insight.label for insight in run.store.insights.values()] == [
        "made no tool call"
    ]


def test_each_stage_answers_through_its_own_model_call() -> None:
    calls = _build_fake_calls()
    reply = FakeReviewCall(_ONE_FINDING)

    # Verification is off by default, so only a run that asks for it verifies.
    _run(
        model_calls=calls,
        session_reviewer=ScriptedReviewer(reply),
        state_overrides={"insights_verification_enabled": True},
    )

    assert len(reply.prompts) == 1
    assert len(calls.cluster.prompts) == 1
    assert len(calls.verify.prompts) == 1


def test_without_a_session_reviewer_the_bound_reviewer_reviews_each_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `session_reviewer` is `None`, the run uses the reviewer bound in the provider."""
    reply = FakeReviewCall(_ONE_FINDING)
    monkeypatch.setattr(
        _common, "session_reviewer_factory", lambda ctx: ScriptedReviewer(reply)
    )

    run = _run(session_reviewer=None)

    assert len(reply.prompts) == 1
    assert run.state["counters"]["trajectories_evaluated"] == 1


def test_eval_service_mode_asks_the_factory_for_a_fetcher_per_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each scope pages its own fetcher, so no scope's counts are added twice."""
    built: list[FakeFetcher] = []

    def build() -> FakeFetcher:
        fetcher = FakeFetcher(pages=[make_page([make_agent_case("s1")])])
        built.append(fetcher)
        return fetcher

    monkeypatch.setattr(
        _common, "evaluator_factory", lambda ctx: FakeEvaluator()
    )

    run = _run(
        fetcher=build,
        analysis_mode="eval_service",
        state_overrides={
            "multi_turn_metrics": ["task_success"],
            "single_turn_metrics": ["safety"],
        },
    )

    assert len(built) == 2
    assert [fetcher.page_tokens_seen for fetcher in built] == [[None], [None]]
    assert run.state["counters"]["trajectories_scanned"] == 2


def test_a_fetcher_factory_is_asked_for_each_fetcher() -> None:
    """The harness's per-scope fetchers come from a factory; review asks once."""
    built: list[FakeFetcher] = []

    def build() -> FakeFetcher:
        fetcher = FakeFetcher(pages=[make_page([make_agent_case("s1")])])
        built.append(fetcher)
        return fetcher

    run = _run(fetcher=build)

    assert len(built) == 1
    assert built[0].page_tokens_seen == [None]
    assert run.state["counters"]["trajectories_evaluated"] == 1


def test_a_given_insight_writer_receives_the_run_instead_of_the_store() -> None:
    writer = FakeInsightStore()

    run = _run(insight_writer=writer)

    assert writer.saved
    assert not run.store.insights


def test_providers_are_given_back_after_a_run() -> None:
    before = _read_bound_providers()

    _run()

    assert _read_bound_providers() == before


def test_providers_are_given_back_after_a_failing_run() -> None:
    before = _read_bound_providers()

    def explode() -> Any:
        raise RuntimeError("no telemetry")

    with pytest.raises(RuntimeError, match="no telemetry"):
        _run(fetcher=explode)

    assert _read_bound_providers() == before


def test_overlapping_async_runs_take_turns_and_keep_to_their_own_providers() -> (
    None
):
    """Providers are process-wide, so a second run waits for the first."""
    before = _read_bound_providers()
    replies = {
        name: FakeReviewCall(_ONE_FINDING) for name in ("alpha", "bravo")
    }
    arguments = {
        name: _build_arguments_for(name, reply)
        for name, reply in replies.items()
    }

    async def run_both() -> list[offline.OfflineRun]:
        return await asyncio.gather(
            *(
                offline.run_investigation_async(**arguments[name])
                for name in arguments
            )
        )

    runs = dict(zip(arguments, asyncio.run(run_both()), strict=True))

    _assert_each_run_kept_to_its_own(replies, runs)
    assert _read_bound_providers() == before


def test_runs_in_two_threads_take_turns_and_keep_to_their_own_providers() -> (
    None
):
    before = _read_bound_providers()
    replies = {
        name: FakeReviewCall(_ONE_FINDING) for name in ("alpha", "bravo")
    }
    arguments = {
        name: _build_arguments_for(name, reply)
        for name, reply in replies.items()
    }
    runs: dict[str, offline.OfflineRun] = {}

    def run(name: str) -> None:
        runs[name] = offline.run_investigation(**arguments[name])

    threads = [threading.Thread(target=run, args=(name,)) for name in arguments]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_each_run_kept_to_its_own(replies, runs)
    assert _read_bound_providers() == before


def test_a_run_whose_session_is_gone_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rather than hand back a state without the counters every caller reads."""

    class ForgetfulRunner(Runner):
        async def run_async(self, **kwargs: Any) -> AsyncGenerator[Event, None]:
            async for event in super().run_async(**kwargs):
                yield event
            await self.session_service.delete_session(
                app_name=self.app_name,
                user_id=kwargs["user_id"],
                session_id=kwargs["session_id"],
            )

    monkeypatch.setattr(offline, "Runner", ForgetfulRunner)

    with pytest.raises(RuntimeError, match="offline-1"):
        _run()


def test_a_run_records_no_trajectory_and_writes_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What an outer binding would persist stays out of reach of the run."""
    reached: list[str] = []
    monkeypatch.setattr(
        core_state,
        "event_writer",
        lambda state, text, source: reached.append("event_writer"),
    )
    monkeypatch.setattr(
        _common,
        "trajectory_recorder_factory",
        lambda ctx: reached.append("trajectory_recorder"),
    )

    run = _run()

    assert reached == []
    # The progress the event writer would have persisted stays in the state.
    assert run.state["progress_log"]


def test_state_overrides_reach_the_workflow_state() -> None:
    run = _run(
        state_overrides={
            "session_review_focus": "refund handling",
            "insights_auto_resolve_days": 0,
        }
    )

    assert run.state["session_review_focus"] == "refund handling"
    assert run.state["insights_auto_resolve_days"] == 0


def test_an_unknown_state_override_is_refused_before_anything_is_bound() -> (
    None
):
    """A misspelled field would otherwise be dropped and the run look fine."""
    before = _read_bound_providers()

    with pytest.raises(ValueError, match="session_review_focsu"):
        _run(state_overrides={"session_review_focsu": "refunds"})

    assert _read_bound_providers() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("investigation_id", "offline-2"),
        ("observed_agent_name", "other_agent"),
        ("quality_analysis_mode", "eval_service"),
        ("budget_per_metric", 1),
        ("window_start", "2026-01-01T00:00:00+00:00"),
        ("window_end", "2026-01-02T00:00:00+00:00"),
    ],
)
def test_a_state_override_of_a_field_the_arguments_set_is_refused(
    field: str, value: Any
) -> None:
    """The state would otherwise disagree with the run it describes."""
    before = _read_bound_providers()

    with pytest.raises(ValueError, match=f"set by the runner.*{field}"):
        _run(state_overrides={field: value})

    assert _read_bound_providers() == before


def test_a_goal_reaches_the_review_prompt_and_the_run_record() -> None:
    reply = FakeReviewCall(_ONE_FINDING)

    run = _run(
        session_reviewer=ScriptedReviewer(reply),
        goal=InvestigationGoal(text="Refunds first.", source="file"),
    )

    assert "Refunds first." in reply.prompts[0]
    assert run.state["goal"]["source"] == "file"


class _RecordingSessionReviewer:
    """Session reviewer that records the sessions it reviews, with one finding each."""

    def __init__(self) -> None:
        self.session_ids: list[str] = []

    def __call__(
        self,
        eval_case: Any,
        revisions: Any,
        *,
        session_review_focus: str,
        goal: Any,
    ) -> list[Finding]:
        self.session_ids.append(eval_case.eval_case_id)
        return [
            Finding(
                expected_behavior="calls the tool the prompt requires",
                actual_behavior="answered without calling it",
                agent_id="root_agent",
            )
        ]


def test_a_session_reviewer_is_the_reviewer_the_review_node_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bound_reviewer(ctx: Any) -> Any:
        raise AssertionError("the bound session reviewer was reached")

    monkeypatch.setattr(_common, "session_reviewer_factory", bound_reviewer)
    reviewer = _RecordingSessionReviewer()

    run = _run(session_reviewer=reviewer)

    assert reviewer.session_ids == ["s1"]
    assert run.state["counters"]["trajectories_evaluated"] == 1
    assert _common.session_reviewer_factory is bound_reviewer


def test_skip_insights_reviews_sessions_and_never_reaches_the_insight_stages() -> (
    None
):
    calls = _build_fake_calls()
    reviewer = _RecordingSessionReviewer()

    run = _run(
        model_calls=calls,
        session_reviewer=reviewer,
        skip_insights=True,
        state_overrides={"insights_verification_enabled": True},
    )

    assert reviewer.session_ids == ["s1"]
    findings = merge_finding_sets(run.state["finding_sets"]).findings
    assert [finding.actual_behavior for finding in findings] == [
        "answered without calling it"
    ]
    for call in (calls.cluster, calls.merge, calls.verify, calls.match):
        assert call.prompts == []
    assert not run.store.insights


def test_the_producer_workflow_is_the_production_graph_cut_before_insights() -> (
    None
):
    """Verifies that `offline._build_producer_workflow` retains the production producer
    nodes and routes while omitting `insight_correlation_node`."""
    producer_workflow = offline._build_producer_workflow(investigation_workflow)
    producer = producer_workflow.graph.nodes
    production = investigation_workflow.graph.nodes

    assert [node.name for node in producer] == [
        node.name for node in production if node is not insight_correlation_node
    ]
    assert all(
        any(node is original for original in production) for node in producer
    )
    start_edge = producer_workflow.edges[0]
    assert start_edge[1] is init_node
    assert start_edge[2] == {
        "session_review": review_node,
        "eval_service": eval_multi_turn_node,
    }


def test_a_graph_without_the_insight_stage_cannot_be_cut() -> None:
    graph = Workflow(name="other", edges=[(START, init_node, review_node)])

    with pytest.raises(ValueError, match="no edge to insight_correlation"):
        offline._build_producer_workflow(graph)
