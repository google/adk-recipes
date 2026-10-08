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

"""Tests for the ``insight_correlation`` node (offline, no GCP).

Drives the node through ``._func`` with hand-built finding sets, a fake
clustering call, a fake merge call, a fake verification call, and a
`FakeInsightStore`: the gate, correlation (new vs. recurring), what the
verification pass does to a candidate, retry-safe ordering, and the fail-loud
paths.

The store is the node's observable output -- the candidates it correlates become
recorded calls on the fake, and those calls are what these assert on.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import (
    EvalCase,
    EvalCaseMetricResult,
    EvalCaseResult,
    EvaluationDataset,
    EvaluationResult,
    ResponseCandidateResult,
    Rubric,
    RubricContent,
    RubricContentProperty,
    RubricVerdict,
)
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.nodes.insight_correlation import (
    insight_correlation_node,
)
from ambient_quality_agent.tools.evaluation.findings_adapter import (
    eval_results_to_finding_set,
)
from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.insights.bigquery_store import (
    BigQueryInsightStore,
)
from ambient_quality_agent.tools.insights.models import (
    InsightStatus,
    OccurrenceState,
)
from ambient_quality_agent.tools.investigations.models import (
    CUSTOM_TRIGGER_TYPE,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config

from .conftest import FakeInsightModelCall, FakeInsightStore

_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _ctx(state: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(state=state)


def _verdict(
    valid: bool = True,
    description: str = "described",
    proposed_label: str = "",
) -> str:
    """Builds a JSON cluster-verification response.

    Args:
        valid: Whether the cluster represents a verified issue.
        description: Diagnostic description of the issue.
        proposed_label: Refined label proposed for the cluster.

    Returns:
        JSON-encoded verification result string.
    """
    return json.dumps(
        {
            "valid": valid,
            "proposed_label": proposed_label,
            "description": description,
        }
    )


def _finding_set(
    *rubric_ids: str,
    case_id: str = "case-1",
    revision: str = "",
    trace: bool = True,
    instruction: str = "",
) -> dict[str, Any]:
    """Build a serialized `FindingSet` containing failed rubrics for testing.

    Args:
        *rubric_ids: Rubric identifiers to mark as failed.
        case_id: Evaluated case identifier.
        revision: Optional agent revision string.
        trace: Whether to include conversation turns in the dataset.
        instruction: Optional system instruction for the agent configuration.

    Returns:
        Serialized FindingSet dictionary.
    """
    verdicts = [
        RubricVerdict(
            evaluated_rubric=Rubric(
                rubric_id=rubric_id,
                content=RubricContent(
                    property=RubricContentProperty(
                        description=f"must do {rubric_id}"
                    )
                ),
            ),
            verdict=False,
            reasoning=f"the agent did not do {rubric_id}",
        )
        for rubric_id in rubric_ids
    ]
    agent_data: dict[str, Any] = {
        "turns": [
            {
                "turn_index": 0,
                "events": [
                    {
                        "author": "user",
                        "content": {
                            "role": "user",
                            "parts": [{"text": "printer jammed"}],
                        },
                    }
                ],
            }
        ]
    }
    if instruction:
        agent_data["agents"] = {
            "root_agent": {"agent_id": "root_agent", "instruction": instruction}
        }
    page = EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=0,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={
                            "multi_turn_task_success_v1": EvalCaseMetricResult(
                                score=0.0, rubric_verdicts=verdicts
                            )
                        },
                    )
                ],
            )
        ],
        evaluation_dataset=[
            EvaluationDataset(
                eval_cases=[
                    EvalCase.model_validate(
                        {"eval_case_id": case_id}
                        | ({"agent_data": agent_data} if trace else {})
                    )
                ]
            )
        ],
    ).model_dump(mode="json")
    revisions = {case_id: revision} if revision else None
    return eval_results_to_finding_set([page], revisions).model_dump(
        mode="json"
    )


def _state(**overrides: Any) -> dict[str, Any]:
    """Builds a seeded sweep state with verification enabled and enforced.

    Args:
        **overrides: State field overrides.

    Returns:
        Workflow state dictionary.
    """
    state: dict[str, Any] = {
        "observed_agent_name": "root_agent",
        "run_id": "run-1",
        "insights_auto_resolve_days": 14,
        "insights_verification_enabled": True,
        "insights_verification_enforced": True,
        "finding_sets": [],
    }
    state.update(overrides)
    return state


@pytest.fixture
def fake_call(monkeypatch: pytest.MonkeyPatch) -> FakeInsightModelCall:
    """Swap the clustering factory for one returning a recording fake."""
    call = FakeInsightModelCall()
    monkeypatch.setattr(_common, "model_call_factory", lambda ctx: call)
    return call


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeInsightStore:
    """Swap the store factory for a recording fake (no BigQuery)."""
    store = FakeInsightStore()
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    return store


@pytest.fixture(autouse=True)
def fake_verify(monkeypatch: pytest.MonkeyPatch) -> FakeInsightModelCall:
    """Swap the verification factory for a recording fake, confirming every candidate.

    Autouse: the pass judges every cluster these tests build, so one that forgot
    the seam would call the model for real.
    """
    call = FakeInsightModelCall(_verdict())
    monkeypatch.setattr(_common, "verification_call_factory", lambda ctx: call)
    return call


@pytest.fixture(autouse=True)
def fake_merge(monkeypatch: pytest.MonkeyPatch) -> FakeInsightModelCall:
    """Swap the merge factory for a recording fake that folds nothing.

    Autouse: every sweep here that builds two or more candidates reaches the
    merge call, so one that forgot the seam would call the model for real. An
    empty group list leaves each candidate in a group of its own, so a test
    asserts on exactly the candidates its clustering answer produced.
    """
    call = FakeInsightModelCall('{"groups": []}')
    monkeypatch.setattr(_common, "merge_call_factory", lambda ctx: call)
    return call


# --- correlation --------------------------------------------------------------


def test_unmatched_candidate_mints_one_new_insight(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Failures from both eval scopes become one grounded, unmatched cluster,
    persisted as a single NEW insight with one occurrence."""
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0", "1"]}
            ]
        }
    )
    state = _state(
        finding_sets=[_finding_set("r-1"), _finding_set("r-2")],
    )

    event = insight_correlation_node._func(_ctx(state))

    new_insights, matched_ids, occurrences = fake_store.saved[0]
    assert [i.status for i in new_insights] == [InsightStatus.NEW]
    assert new_insights[0].label == "made no tool call"
    assert new_insights[0].agent_name == "root_agent"
    assert matched_ids == []
    assert len(occurrences) == 1
    assert occurrences[0].insight_id == new_insights[0].insight_id
    assert occurrences[0].run_id == "run-1"
    assert occurrences[0].item_count == 2
    # No revision reached the findings, so the sighting is the single unnamed one.
    assert occurrences[0].agent_revision == ""
    # The node leaves nothing behind in state; the store owns persistence.
    assert "insight_clusters" not in state
    assert event.content is not None
    text = event.content.parts[0].text
    assert "2 failed rubric(s)" in text
    assert "1 candidate issue(s): 1 new, 0 recurring" in text
    assert "_made no tool call_ (new): 2 rubric(s)" in text


def test_matched_candidate_appends_occurrence_not_insight(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate the judge matches appends an occurrence to the existing
    insight and mints nothing new."""
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    store = FakeInsightStore(matches={0: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    state = _state(finding_sets=[_finding_set("r-1")])

    event = insight_correlation_node._func(_ctx(state))

    new_insights, matched_ids, occurrences = store.saved[0]
    assert new_insights == []
    assert matched_ids == ["ins-existing"]
    assert [o.insight_id for o in occurrences] == ["ins-existing"]
    assert event.content is not None
    text = event.content.parts[0].text
    assert "1 candidate issue(s): 0 new, 1 recurring" in text
    assert "_made no tool call_ (recurring): 1 rubric(s)" in text


def test_counts_the_clusters_and_the_insights_they_became(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's findings are counted the way it recorded them: one per
    cluster, split by whether the cluster was a new issue or an old one."""
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0"]},
                {"label": "invented a ticket id", "finding_ids": ["1"]},
            ]
        }
    )
    store = FakeInsightStore(matches={1: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1"),
            _finding_set("r-2", case_id="case-2"),
        ]
    )

    insight_correlation_node._func(_ctx(state))

    assert state["counters"] == {
        "clusters_created": 2,
        "clusters_verified": 2,
        "clusters_rejected": 0,
        "clusters_verify_skipped": 0,
        "clusters_verify_failed": 0,
        "insights_created": 1,
        "insights_recurring": 1,
        "rubrics_errored": 0,
        "rubrics_unclustered": 0,
    }


def test_counts_the_rubrics_that_reached_no_insight(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """The skipped-rubric counts are recorded, not only printed.

    The node's summary line says a sweep came out short, but only while someone
    is reading that run's events; a shrunken sweep is exactly the thing worth
    seeing across runs, so the same numbers go into the run's counters and the
    deployment's totals.
    """
    # Two rubrics fail and the model clusters one, leaving the other out.
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    state = _state(
        finding_sets=[_finding_set("r-1"), _finding_set("r-2")],
    )

    insight_correlation_node._func(_ctx(state))

    assert state["counters"]["rubrics_unclustered"] == 1
    assert state["counters"]["rubrics_errored"] == 0


def test_an_occurrence_counts_the_traces_behind_its_cluster(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Two rubrics from one conversation are one trace; the third, from
    another, makes two -- the spread of the issue, not its rubric count."""
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0", "1", "2"]}
            ]
        }
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", "r-2", case_id="case-1"),
            _finding_set("r-3", case_id="case-2"),
        ]
    )

    event = insight_correlation_node._func(_ctx(state))

    _new_insights, _matched, occurrences = fake_store.saved[0]
    assert occurrences[0].item_count == 3
    assert occurrences[0].trace_count == 2
    assert event.content is not None
    assert "3 rubric(s) across 2 trace(s)" in event.content.parts[0].text


def test_an_occurrence_names_the_trajectories_behind_its_cluster(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """The ids as well as their number, so an insight resolves to the
    conversations it was found in rather than only to how many there were."""
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0", "1", "2"]}
            ]
        }
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", "r-2", case_id="case-1"),
            _finding_set("r-3", case_id="case-2"),
        ]
    )

    insight_correlation_node._func(_ctx(state))

    _new_insights, _matched, occurrences = fake_store.saved[0]
    assert occurrences[0].trajectory_ids == ["case-1", "case-2"]
    assert occurrences[0].trace_count == len(occurrences[0].trajectory_ids)


def test_event_names_the_source_cases_of_each_candidate(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """The event carries the eval cases behind a cluster, so a finding is
    traceable back to the conversations that produced it -- in the run, not logs."""
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    state = _state(finding_sets=[_finding_set("r-1")])

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    assert "cases: case-1" in event.content.parts[0].text


def test_summary_reports_rubrics_the_clustering_left_out(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Two rubrics fail but the model clusters only one; the other is surfaced as
    a clustering skip against the sweep's total, not silently dropped."""
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    state = _state(
        finding_sets=[_finding_set("r-1"), _finding_set("r-2")],
    )

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    text = event.content.parts[0].text
    assert "skipped rubric(s):** 0 errored, 1 unclustered (of 2 total)" in text


# --- verification -------------------------------------------------------------


def _one_cluster_state(
    fake_call: FakeInsightModelCall, **kwargs: Any
) -> dict[str, Any]:
    """Configures a sweep of one failed rubric grouped into one candidate.

    Args:
        fake_call: Mock model call recording clustering requests.
        **kwargs: Overrides forwarded to `_finding_set`.

    Returns:
        Workflow state dictionary.
    """
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    return _state(finding_sets=[_finding_set("r-1", **kwargs)])


def _rendered_inputs(prompt: str) -> str:
    """Extracts the JSON payload section from a verification prompt.

    Args:
        prompt: Full prompt string.

    Returns:
        JSON substring following the input section header.
    """
    return prompt.rsplit("### THE INSIGHT TO DIAGNOSE\n", maxsplit=1)[-1]


def test_candidates_are_judged_before_any_of_them_is_matched(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching is the expensive question and a rejected candidate has no use
    for the answer, so the pass runs first and only survivors are matched.

    The order is also what makes the rename available to the judge: the label a
    candidate is matched on is the one it will be stored under.
    """
    order: list[str] = []
    find_existing = fake_store.find_existing_insights

    def find(candidates: Any) -> dict[int, str | None]:
        order.append("find")
        return find_existing(candidates)

    def verify(prompt: str) -> str:
        order.append("verify")
        return _verdict()

    monkeypatch.setattr(fake_store, "find_existing_insights", find)
    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    state = _one_cluster_state(fake_call)

    insight_correlation_node._func(_ctx(state))

    assert order == ["verify", "find"]


def test_a_candidate_is_matched_on_the_label_it_will_be_stored_under(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An insight stores the verdict's rename, so the judge has to be given it.

    Matching the raw clustering label against a stored refined one compares two
    different kinds of description -- what the sweep observed, and what
    verification concluded after reading the trajectory. The judge then answers
    about a question nobody asked, and a recurrence mints a duplicate.
    """
    monkeypatch.setattr(
        _common,
        "verification_call_factory",
        lambda ctx: (
            lambda prompt: _verdict(
                proposed_label="`create_ticket` declares no parameters"
            )
        ),
    )

    insight_correlation_node._func(_ctx(_one_cluster_state(fake_call)))

    assert fake_store.candidate_labels == [
        "`create_ticket` declares no parameters"
    ]
    (new_insights, _matched, occurrences) = fake_store.saved[0]
    # The insight carries the same label the judge compared.
    assert [i.label for i in new_insights] == [
        "`create_ticket` declares no parameters"
    ]
    # The occurrence still records what the sweep observed.
    assert [o.label for o in occurrences] == ["made no tool call"]


def test_a_candidate_without_a_rename_is_matched_on_the_observed_label(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification can be off, or can propose nothing. Then the observed label
    is what gets stored, so it is also what the judge should compare."""
    monkeypatch.setattr(
        _common,
        "verification_call_factory",
        lambda ctx: lambda prompt: _verdict(),
    )

    insight_correlation_node._func(_ctx(_one_cluster_state(fake_call)))

    assert fake_store.candidate_labels == ["made no tool call"]


def test_a_verified_candidate_records_its_verdict_and_mints_its_insight(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """A confirmed candidate is persisted as it always was, with the verdict
    filed under the step that reached it."""
    state = _one_cluster_state(fake_call)

    insight_correlation_node._func(_ctx(state))

    new_insights, _matched, occurrences = fake_store.saved[0]
    assert [i.status for i in new_insights] == [InsightStatus.NEW]
    assert occurrences[0].insight_id == new_insights[0].insight_id
    verdict = occurrences[0].analyses["verification"]
    assert verdict.valid is True
    assert verdict.explanation == "described"
    assert state["counters"]["clusters_verified"] == 1
    assert state["counters"]["clusters_rejected"] == 0


def test_the_pass_is_shown_the_configuration_the_sessions_ran_under(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """The `agent_definition` verdict faults the configuration, so a sweep that
    withheld it could only ever return `agent_behavior`."""
    state = _one_cluster_state(fake_call, instruction="Always be polite")

    insight_correlation_node._func(_ctx(state))

    inputs = _rendered_inputs(fake_verify.prompts[0])
    assert "agent_definition" in inputs
    assert "Always be polite" in inputs


def test_a_sweep_without_a_configuration_still_reaches_a_verdict(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """A source recording no agent configuration leaves the pass reasoning from
    the trajectories alone, which is a verdict rather than a skip."""
    state = _one_cluster_state(fake_call)

    insight_correlation_node._func(_ctx(state))

    assert "agent_definition" not in _rendered_inputs(fake_verify.prompts[0])
    _new, _matched, occurrences = fake_store.saved[0]
    assert occurrences[0].analyses["verification"].valid is True


def test_a_rejected_candidate_mints_no_insight_and_records_why_when_enforced(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """The candidate is dropped as an issue and kept as evidence: its occurrence
    names no insight and carries the verdict that threw it out."""
    fake_verify.response = _verdict(valid=False, description="")
    state = _one_cluster_state(fake_call)

    insight_correlation_node._func(_ctx(state))

    new_insights, matched_ids, occurrences = fake_store.saved[0]
    assert new_insights == []
    assert matched_ids == []
    assert len(occurrences) == 1
    assert occurrences[0].insight_id is None
    assert occurrences[0].analyses["verification"].valid is False
    assert state["counters"]["clusters_rejected"] == 1
    assert state["counters"]["insights_created"] == 0
    # A rejection is a judgement on a candidate, not the absence of one.
    assert state["counters"]["clusters_created"] == 1


def test_a_rejected_candidate_is_never_put_to_the_matching_judge_when_enforced(
    fake_call: FakeInsightModelCall,
    fake_verify: FakeInsightModelCall,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retired candidate tracks no insight, so which one it resurfaces is a
    question with no consumer -- and one the sweep pays a judge to answer.

    The insight it would have matched therefore records no sighting this sweep,
    is not dated to it either, and ages toward RESOLVED. That is the intent: a
    rejection is evidence the issue is not real, and the window exists for an
    issue nothing evidences. It is the one thing that separates a rejection from
    an unjudged candidate, which is matched and dated precisely because no
    verdict is no such evidence.
    """
    store = FakeInsightStore(matches={0: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    fake_verify.response = _verdict(valid=False, description="")
    state = _one_cluster_state(fake_call)

    insight_correlation_node._func(_ctx(state))

    assert store.candidate_labels == []
    assert store.seen == []
    new_insights, matched_ids, occurrences = store.saved[0]
    assert (new_insights, matched_ids) == ([], [])
    assert occurrences[0].insight_id is None
    assert store.calls == ["cleanup", "find", "save", "resolve"]


def test_a_survivor_keeps_its_own_insight_when_a_candidate_is_dropped(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store keys its answer by position in the list it was handed, and
    dropping the rejected candidate renumbers that list: the survivor is
    position 0 there and candidate 1 here. A mapping that lost the difference
    would file this sighting against another candidate's insight.
    """
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0"]},
                {"label": "invented a ticket id", "finding_ids": ["1"]},
            ]
        }
    )
    store = FakeInsightStore(matches={0: "ins-ticket"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)

    def verify(prompt: str) -> str:
        if "made no tool call" in prompt:
            return _verdict(valid=False, description="")
        return _verdict()

    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1"),
            _finding_set("r-2", case_id="case-2"),
        ]
    )

    insight_correlation_node._func(_ctx(state))

    assert store.candidate_labels == ["invented a ticket id"]
    new_insights, matched_ids, occurrences = store.saved[0]
    assert new_insights == []
    assert matched_ids == ["ins-ticket"]
    assert [(o.label, o.insight_id) for o in occurrences] == [
        ("made no tool call", None),
        ("invented a ticket id", "ins-ticket"),
    ]


def test_the_matching_positions_still_invert_across_a_mixed_sweep(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the rejected candidate is held back, so the store's positions
    are neither the candidate indexes nor a fixed offset from them: candidate 2
    is position 1 in the list it was handed. Read back wrong, the unjudged
    candidate's insight is dated never or dated against the wrong cluster --
    and the verified candidate claims the other one's insight.
    """
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0"]},
                {"label": "invented a ticket id", "finding_ids": ["1"]},
                {
                    "label": "answered in the wrong language",
                    "finding_ids": ["2"],
                },
            ]
        }
    )
    store = FakeInsightStore(matches={0: "ins-tool", 1: "ins-language"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)

    def verify(prompt: str) -> str:
        if "invented a ticket id" in prompt:
            return _verdict(valid=False, description="")
        return _verdict()

    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1"),
            _finding_set("r-2", case_id="case-2"),
            _finding_set("r-3", case_id="case-3", trace=False),
        ]
    )

    insight_correlation_node._func(_ctx(state))

    assert store.candidate_labels == [
        "made no tool call",
        "answered in the wrong language",
    ]
    new_insights, matched_ids, occurrences = store.saved[0]
    assert new_insights == []
    assert matched_ids == ["ins-tool"]
    assert store.seen == ["ins-tool", "ins-language"]
    assert [(o.label, o.insight_id) for o in occurrences] == [
        ("made no tool call", "ins-tool"),
        ("invented a ticket id", None),
        ("answered in the wrong language", None),
    ]


def test_the_summary_reports_what_the_pass_threw_out(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """A sweep that rejected everything must not read as a sweep that found
    nothing: the header counts no issue either way, so the outcome line is the
    only thing that says a verdict is why."""
    fake_verify.response = _verdict(valid=False, description="")
    state = _one_cluster_state(fake_call)

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    text = event.content.parts[0].text
    assert "1 candidate issue(s): 0 new, 0 recurring" in text
    assert "verification:** 0 verified, 1 rejected" in text
    assert "_made no tool call_ (rejected): 1 rubric(s)" in text


def test_the_summary_omits_the_outcome_line_when_everything_verified(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    state = _one_cluster_state(fake_call)

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    assert "verification:**" not in event.content.parts[0].text


def test_a_candidate_the_pass_could_not_read_mints_no_insight(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """A cluster whose sessions carry no trace is never judged, and an unjudged
    candidate is withheld exactly as a rejected one is.

    Withheld, not discarded -- the occurrence is written either way, so the
    sighting stays on record for anyone reading the sweep rather than the
    insights.

    It is still put to the matching judge, because an insight it matched would
    have to be kept alive; this one matches nothing, so the sweep mints none
    and dates none.
    """
    state = _one_cluster_state(fake_call, trace=False)

    insight_correlation_node._func(_ctx(state))

    assert fake_verify.prompts == []
    assert fake_store.candidate_labels == ["made no tool call"]
    assert fake_store.seen == []
    new_insights, matched_ids, occurrences = fake_store.saved[0]
    assert (new_insights, matched_ids) == ([], [])
    assert occurrences[0].insight_id is None
    assert occurrences[0].analyses == {}
    assert state["counters"]["clusters_verify_skipped"] == 1
    assert state["counters"]["clusters_verified"] == 0
    assert state["counters"]["insights_created"] == 0


def test_a_candidate_the_pass_failed_on_mints_no_insight(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost call costs the insight, not the sighting behind it -- and costs
    the candidate judged alongside it nothing."""

    def verify(prompt: str) -> str:
        if "invented a ticket id" in prompt:
            raise RuntimeError("quota exceeded")
        return _verdict()

    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0"]},
                {"label": "invented a ticket id", "finding_ids": ["1"]},
            ]
        }
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1"),
            _finding_set("r-2", case_id="case-2"),
        ]
    )

    insight_correlation_node._func(_ctx(state))

    assert fake_store.candidate_labels == [
        "made no tool call",
        "invented a ticket id",
    ]
    new_insights, _matched, occurrences = fake_store.saved[0]
    assert [i.label for i in new_insights] == ["made no tool call"]
    lost = next(o for o in occurrences if o.label == "invented a ticket id")
    assert lost.insight_id is None
    assert lost.analyses == {}
    assert state["counters"]["clusters_verify_failed"] == 1
    assert state["counters"]["clusters_verified"] == 1
    assert state["counters"]["insights_created"] == 1


def test_a_wholesale_verification_outage_still_writes_the_sweep(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify_clusters` raises when every call it made failed, and in a sweep
    holding one judgeable cluster that is one transient error. Letting it escape
    the node would discard the clustering and merge spend and write no audit row
    at all, when UNJUDGED already records what happened.
    """

    def verify(prompt: str) -> str:
        raise RuntimeError("503 backend unavailable")

    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    state = _state(finding_sets=[_finding_set("r-1", case_id="case-1")])

    insight_correlation_node._func(_ctx(state))

    new_insights, _matched, occurrences = fake_store.saved[0]
    assert new_insights == []
    assert [o.occurrence_state for o in occurrences] == [
        OccurrenceState.UNJUDGED
    ]
    assert state["counters"]["clusters_verify_failed"] == 1


def test_an_unjudged_candidate_keeps_the_insight_it_matched_alive(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The insight is dated to this sweep without a recurrence, which is the
    only thing standing between it and permanent resolution.

    Its cluster records no occurrence against it, `resolve_stale_insights`
    reads exactly that absence, and RESOLVED is terminal -- the matching judge
    never sees a resolved insight again, so the next sweep mints a duplicate
    and splits the history. A cluster beyond the cap or without traces goes
    unjudged for the same reason every sweep, so nothing would ever lift that.
    No verdict is not evidence the defect is gone, so the insight stays alive;
    a rejection, which is such evidence, does not get this.
    """
    store = FakeInsightStore(matches={0: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    state = _one_cluster_state(fake_call, trace=False)

    insight_correlation_node._func(_ctx(state))

    assert store.candidate_labels == ["made no tool call"]
    assert store.seen == ["ins-existing"]
    new_insights, matched_ids, occurrences = store.saved[0]
    # Neither a new insight nor a recurrence of the matched one: the sweep
    # asserts nothing about a defect it never checked.
    assert (new_insights, matched_ids) == ([], [])
    assert occurrences[0].insight_id is None
    # One write path: the sighting travels with the save, not beside it.
    assert store.calls == ["cleanup", "find", "save", "resolve"]


def test_a_matched_insight_takes_the_verdicts_rename(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clustering names a candidate from the finding tuples alone, and that name
    is the recurrence key every later sweep is judged against. Verification reads
    the trajectories the name was guessed from, so its rename is the better key,
    and the insight keeps its id while taking it -- a rename is not a new defect.
    """
    store = FakeInsightStore(matches={0: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    monkeypatch.setattr(
        _common,
        "verification_call_factory",
        lambda ctx: (
            lambda prompt: _verdict(
                proposed_label="called the tool with no city"
            )
        ),
    )

    insight_correlation_node._func(_ctx(_one_cluster_state(fake_call)))

    assert store.relabelled == {"ins-existing": "called the tool with no city"}


def test_a_matched_insight_keeps_its_name_when_no_rename_is_proposed(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict that leaves `proposed_label` empty is judging the defect, not
    the wording. Rewriting the key on every sighting would churn what the next
    sweep matches against for no gain."""
    store = FakeInsightStore(matches={0: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    monkeypatch.setattr(
        _common,
        "verification_call_factory",
        lambda ctx: lambda prompt: _verdict(),
    )

    insight_correlation_node._func(_ctx(_one_cluster_state(fake_call)))

    assert store.relabelled == {}


def test_a_minted_insight_is_named_by_the_verdict_not_the_cluster(
    fake_call: FakeInsightModelCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first sighting is where the key is set for good, so a rename matters
    most here: mint under the clustering guess and every later sweep matches
    against it."""
    store = FakeInsightStore()
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    monkeypatch.setattr(
        _common,
        "verification_call_factory",
        lambda ctx: (
            lambda prompt: _verdict(
                proposed_label="called the tool with no city"
            )
        ),
    )

    insight_correlation_node._func(_ctx(_one_cluster_state(fake_call)))

    new_insights, _recurring, occurrences = store.saved[0]
    assert [i.label for i in new_insights] == ["called the tool with no city"]
    # The sighting still records what this sweep observed, which is what the
    # next sweep's candidate is matched on.
    assert occurrences[0].label == "made no tool call"


def test_the_summary_counts_a_sweep_that_mixed_all_three_outcomes(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header's arithmetic has to close, or it claims insights nobody minted.

    Every candidate lands in exactly one verdict bucket, and only a verified one
    is counted new or recurring -- so ``new + recurring`` is the verified count
    and the four verdicts sum to the candidates.
    """
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0"]},
                {"label": "invented a ticket id", "finding_ids": ["1"]},
                {
                    "label": "answered in the wrong language",
                    "finding_ids": ["2"],
                },
            ]
        }
    )

    def verify(prompt: str) -> str:
        if "invented a ticket id" in prompt:
            return _verdict(valid=False, description="")
        return _verdict()

    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1"),
            _finding_set("r-2", case_id="case-2"),
            _finding_set("r-3", case_id="case-3", trace=False),
        ]
    )

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    text = event.content.parts[0].text
    assert "3 candidate issue(s): 1 new, 0 recurring" in text
    assert "verification:** 1 verified, 1 rejected, 1 skipped, 0 failed" in text
    assert "_made no tool call_ (new):" in text
    assert "_invented a ticket id_ (rejected):" in text
    assert "_answered in the wrong language_ (unjudged):" in text
    assert state["counters"]["insights_created"] == 1
    assert state["counters"]["insights_recurring"] == 0


def test_only_the_examples_the_pass_read_are_marked_examined(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """The mark says the model saw this trajectory, so it names exactly the
    cases the pass sampled -- a mark on an example it never read is worse than
    no mark at all."""
    fake_call.response = json.dumps(
        {
            "clusters": [
                {
                    "label": "made no tool call",
                    "finding_ids": ["0", "1", "2", "3"],
                }
            ]
        }
    )
    state = _state(
        finding_sets=[
            _finding_set(f"r-{i}", case_id=f"case-{i}") for i in range(1, 5)
        ]
    )

    insight_correlation_node._func(_ctx(state))

    _new, _matched, occurrences = fake_store.saved[0]
    examined = {e.eval_case_id for e in occurrences[0].rubrics if e.examined}
    # Three traces per prompt, taken in eval-case order; case-4 never reached it.
    assert examined == {"case-1", "case-2", "case-3"}


def test_the_verdict_reads_back_through_the_reader(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Verifies the verification verdict end-to-end data flow:
    1. Node names the step.
    2. Store serializes the payload.
    3. Reader rehydrates the model.
    """
    state = _one_cluster_state(fake_call)
    insight_correlation_node._func(_ctx(state))
    _new, _matched, occurrences = fake_store.saved[0]

    writer = mock.MagicMock()
    BigQueryInsightStore(
        client=writer,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).save_investigation_result([], [], [], occurrences, _NOW)
    (row,) = writer.load_table_from_json.call_args.args[0]

    reads = mock.MagicMock()
    reads.query.side_effect = [
        mock.MagicMock(**{"result.return_value": [{"total": 1}]}),
        mock.MagicMock(**{"result.return_value": [row]}),
    ]
    stored, _total = BigQueryInsightReader(
        client=reads,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).list_occurrences(
        insight_id=str(occurrences[0].insight_id), limit=1, offset=0
    )

    verdict = stored[0].analyses["verification"]
    assert verdict.valid is True
    assert verdict.explanation == "described"
    assert [e.examined for e in stored[0].rubrics] == [True]


def test_every_occurrence_says_why_it_names_the_insight_it_names(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies all three occurrence states end-to-end:
    1. Cluster creation with verification outcome.
    2. Store serialization to BigQuery row format.
    3. Reader deserialization into occurrence objects.
    """
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["0"]},
                {"label": "invented a ticket id", "finding_ids": ["1"]},
                {
                    "label": "answered in the wrong language",
                    "finding_ids": ["2"],
                },
            ]
        }
    )

    def verify(prompt: str) -> str:
        if "invented a ticket id" in prompt:
            return _verdict(valid=False, description="")
        return _verdict()

    monkeypatch.setattr(
        _common, "verification_call_factory", lambda ctx: verify
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1"),
            _finding_set("r-2", case_id="case-2"),
            _finding_set("r-3", case_id="case-3", trace=False),
        ]
    )

    insight_correlation_node._func(_ctx(state))
    _new, _matched, occurrences = fake_store.saved[0]

    writer = mock.MagicMock()
    BigQueryInsightStore(
        client=writer,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).save_investigation_result([], [], [], occurrences, _NOW)
    rows = writer.load_table_from_json.call_args.args[0]

    reads = mock.MagicMock()
    reads.query.side_effect = [
        mock.MagicMock(**{"result.return_value": [{"total": len(rows)}]}),
        mock.MagicMock(**{"result.return_value": rows}),
    ]
    stored, _total = BigQueryInsightReader(
        client=reads,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).list_occurrences(insight_id="ins-1", limit=3, offset=0)

    assert [row["occurrence_state"] for row in rows] == [
        "TRACKED",
        "REJECTED",
        "UNJUDGED",
    ]
    assert [
        (o.label, o.insight_id is None, o.occurrence_state) for o in stored
    ] == [
        ("made no tool call", False, OccurrenceState.TRACKED),
        ("invented a ticket id", True, OccurrenceState.REJECTED),
        ("answered in the wrong language", True, OccurrenceState.UNJUDGED),
    ]


# --- what switches the pass on ------------------------------------------------


@pytest.mark.parametrize(("setting", "runs"), [(None, True), (False, False)])
def test_the_mode_the_run_carries_decides_the_pass(
    monkeypatch: pytest.MonkeyPatch,
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
    setting: bool | None,
    runs: bool,
) -> None:
    """The mode a sweep runs under is the one its own state carries, not the
    one the environment holds by the time it executes.

    A run snapshots its config when it is scheduled, so an attach or a redeploy
    in between leaves the two disagreeing. Left unset, the flag has to follow
    the mode the sweep actually runs -- and an explicitly disabled pass has to
    stay off across the same disagreement.
    """
    monkeypatch.setattr(
        effective_config,
        "env_config",
        dataclasses.replace(
            effective_config.env_config,
            quality_analysis_mode="session_review",
            insights_verification_enabled=setting,
        ),
    )
    snapshot = dataclasses.replace(
        effective_config.load({}), quality_analysis_mode="eval_service"
    )
    # Seeded as the durable run seeds it (`job_execution` spreads the whole
    # `asdict(config)`): flag and mode reach the node side by side, unresolved.
    state = _one_cluster_state(fake_call) | {
        "insights_verification_enabled": snapshot.insights_verification_enabled,
        "quality_analysis_mode": snapshot.quality_analysis_mode,
    }

    insight_correlation_node._func(_ctx(state))

    assert (fake_verify.prompts != []) is runs


# --- the pass switched off ----------------------------------------------------


def test_the_pass_costs_nothing_and_withholds_nothing_when_it_is_off(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """When verification is disabled, candidates bypass checking without model calls.

    Every candidate is minted or matched, occurrences carry no analysis,
    and verification counters remain zero.
    """
    state = _one_cluster_state(fake_call)
    state["insights_verification_enabled"] = False

    insight_correlation_node._func(_ctx(state))

    assert fake_verify.prompts == []
    new_insights, matched_ids, occurrences = fake_store.saved[0]
    assert [i.label for i in new_insights] == ["made no tool call"]
    assert matched_ids == []
    assert occurrences[0].insight_id == new_insights[0].insight_id
    assert occurrences[0].occurrence_state is OccurrenceState.TRACKED
    assert occurrences[0].analyses == {}
    assert state["counters"]["clusters_verified"] == 0
    assert state["counters"]["clusters_rejected"] == 0
    assert state["counters"]["clusters_verify_skipped"] == 0
    assert state["counters"]["clusters_verify_failed"] == 0
    assert state["counters"]["insights_created"] == 1


def test_a_candidate_the_pass_could_not_have_read_still_mints_when_it_is_off(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Nothing is withheld for want of a verdict nobody was going to reach.

    A cluster whose sessions carry no trace is what the running pass leaves
    unjudged, and an unjudged candidate mints no insight. Off, there is no such
    thing: the candidate is minted like any other, or the flag would withhold
    findings rather than only the checking of them.
    """
    state = _one_cluster_state(fake_call, trace=False)
    state["insights_verification_enabled"] = False

    insight_correlation_node._func(_ctx(state))

    new_insights, _matched, occurrences = fake_store.saved[0]
    assert [i.label for i in new_insights] == ["made no tool call"]
    assert occurrences[0].insight_id == new_insights[0].insight_id
    assert fake_store.seen == []


def test_the_summary_says_nothing_of_a_pass_that_did_not_run(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """A sweep that never verified must not read as one that verified nothing:
    the outcome line is absent rather than four zeros, and the candidate is
    reported as the new issue it became rather than as unjudged.

    The candidate carries no trace, which is what the running pass reports as
    skipped and leaves unjudged -- so the summary of this sweep is the one the
    flag has to change.
    """
    state = _one_cluster_state(fake_call, trace=False)
    state["insights_verification_enabled"] = False

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    text = event.content.parts[0].text
    assert text is not None
    assert "verification:**" not in text
    assert "1 candidate issue(s): 1 new, 0 recurring" in text
    assert "_made no tool call_ (new):" in text


# --- the verdict unenforced ---------------------------------------------------


def test_a_flagged_candidate_still_mints_under_the_refined_label_when_unenforced(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """Unenforced, the verdict informs rather than gates: the candidate still
    mints its insight, under verification's refined label because that reading
    of the trajectory is the better one whichever way `valid` went, and the
    verdict itself rides along on the occurrence for a triager to weigh."""
    fake_verify.response = _verdict(
        valid=False,
        proposed_label="dropped the ticket id",
        description="described",
    )
    state = _one_cluster_state(fake_call)
    state["insights_verification_enforced"] = False

    insight_correlation_node._func(_ctx(state))

    new_insights, matched_ids, occurrences = fake_store.saved[0]
    assert [i.label for i in new_insights] == ["dropped the ticket id"]
    assert matched_ids == []
    assert occurrences[0].insight_id == new_insights[0].insight_id
    assert occurrences[0].occurrence_state is OccurrenceState.TRACKED
    verdict = occurrences[0].analyses["verification"]
    assert verdict.valid is False
    assert verdict.explanation == "described"
    # The counters audit the verdict itself, not what the sweep did with it.
    assert state["counters"]["clusters_rejected"] == 1
    assert state["counters"]["insights_created"] == 1


def test_a_flagged_candidate_is_matched_against_stored_insights_when_unenforced(
    fake_call: FakeInsightModelCall,
    fake_verify: FakeInsightModelCall,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kept candidate is a tracked one, so it owes the recurrence judge the
    same question every other tracked candidate asks. Skipping the judge here
    would mint a duplicate of an insight already on file, and the label it is
    matched and relabelled under is the refined one an insight stores."""
    store = FakeInsightStore(matches={0: "ins-existing"})
    monkeypatch.setattr(_common, "insight_store_factory", lambda ctx: store)
    fake_verify.response = _verdict(
        valid=False, proposed_label="dropped the ticket id"
    )
    state = _one_cluster_state(fake_call)
    state["insights_verification_enforced"] = False

    insight_correlation_node._func(_ctx(state))

    assert store.candidate_labels == ["dropped the ticket id"]
    new_insights, matched_ids, occurrences = store.saved[0]
    assert new_insights == []
    assert matched_ids == ["ins-existing"]
    assert store.relabelled == {"ins-existing": "dropped the ticket id"}
    assert occurrences[0].insight_id == "ins-existing"


def test_an_unjudged_candidate_stays_unjudged_when_unenforced(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """The flag says what to do with a verdict, and an unjudged candidate has
    none: no trace means the pass never ran on it, which is not a doubt to
    overrule."""
    state = _one_cluster_state(fake_call, trace=False)
    state["insights_verification_enforced"] = False

    insight_correlation_node._func(_ctx(state))

    new_insights, _matched, occurrences = fake_store.saved[0]
    assert new_insights == []
    assert occurrences[0].insight_id is None
    assert occurrences[0].occurrence_state is OccurrenceState.UNJUDGED


def test_the_summary_marks_a_kept_candidate_the_model_doubted(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """A kept candidate the pass doubted must read as exactly that. It is
    counted as the new issue it became, marked flagged so the doubt is not lost,
    and the outcome line says which way the verdict was taken."""
    fake_verify.response = _verdict(valid=False)
    state = _one_cluster_state(fake_call)
    state["insights_verification_enforced"] = False

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    text = event.content.parts[0].text
    assert text is not None
    assert "1 candidate issue(s): 1 new, 0 recurring" in text
    assert "_made no tool call_ (new, flagged):" in text
    # The header counted this candidate as new, so the outcome line names the
    # same one flagged; "rejected" would read as thrown out and minted at once.
    assert "verification:** 0 verified, 1 flagged, 0 skipped, 0 failed" in text
    assert "rejected" not in text
    assert "a flagged candidate is recorded and kept" in text


def test_the_summary_still_says_rejected_when_the_verdict_is_enforced(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """Enforced, the candidate really is thrown out and the header counts no
    issue for it, so the outcome line keeps the word that closes that
    arithmetic: only the unenforced wording softens."""
    fake_verify.response = _verdict(valid=False)
    state = _one_cluster_state(fake_call)

    event = insight_correlation_node._func(_ctx(state))

    assert event.content is not None
    text = event.content.parts[0].text
    assert text is not None
    assert "1 candidate issue(s): 0 new, 0 recurring" in text
    assert "verification:** 0 verified, 1 rejected, 0 skipped, 0 failed" in text
    assert "only a verified candidate becomes an insight" in text


def test_a_negative_verdict_gates_nothing_by_default(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    fake_verify: FakeInsightModelCall,
) -> None:
    """A state that never mentions the flag is the shipped default, and it must
    keep the candidate: the gate costs a real issue whenever a trace is thin, so
    nothing may switch it on by omission."""
    fake_verify.response = _verdict(valid=False)
    state = _one_cluster_state(fake_call)
    del state["insights_verification_enforced"]

    insight_correlation_node._func(_ctx(state))

    new_insights, _matched, occurrences = fake_store.saved[0]
    assert len(new_insights) == 1
    assert occurrences[0].occurrence_state is OccurrenceState.TRACKED


# --- retry-safety and resolution ----------------------------------------------


def test_cleanup_runs_before_any_other_store_call(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """A crash-retry must erase its prior attempt before rewriting rows, so
    cleanup is the first thing the node asks the store to do."""
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    state = _state(finding_sets=[_finding_set("r-1")])

    insight_correlation_node._func(_ctx(state))

    assert fake_store.calls[0] == "cleanup"
    assert fake_store.cleanup_run_ids == ["run-1"]
    assert fake_store.calls == ["cleanup", "find", "save", "resolve"]
    assert fake_store.resolve_calls == [(fake_store.resolve_calls[0][0], 14)]


def test_no_failed_rubrics_cleans_and_resolves_without_clustering(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """A clean sweep pays for no clustering call and writes no occurrences, but
    still cleans its prior attempt and ages out stale insights (time-based)."""
    state = _state()

    event = insight_correlation_node._func(_ctx(state))

    assert fake_call.prompts == []
    assert fake_store.calls == ["cleanup", "resolve"]
    assert fake_store.saved == []
    assert event.content is not None
    assert "no failed rubrics" in event.content.parts[0].text


def test_hallucinated_cluster_is_rejected(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """A cluster citing a rubric that was never sent is not evidence of anything.
    The sweep's one rubric was sent as id "0", so "r-invented" is not one the
    model was given."""
    fake_call.response = json.dumps(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["r-invented"]}
            ]
        }
    )
    state = _state(finding_sets=[_finding_set("r-1")])

    event = insight_correlation_node._func(_ctx(state))

    new_insights, matched_ids, occurrences = fake_store.saved[0]
    assert (new_insights, matched_ids, occurrences) == ([], [], [])
    assert event.content is not None
    text = event.content.parts[0].text
    assert "0 candidate issue(s): 0 new, 0 recurring" in text
    assert "made no tool call" not in text
    # The dropped cluster's rubric is surfaced as a clustering skip, not lost.
    assert "skipped rubric(s):** 0 errored, 1 unclustered (of 1 total)" in text


# --- fail loud ----------------------------------------------------------------


def test_a_custom_run_resolves_nothing(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Verify custom runs correlate findings but skip resolving stale insights."""
    state = _one_cluster_state(fake_call)
    state["trigger_type"] = CUSTOM_TRIGGER_TYPE

    insight_correlation_node._func(_ctx(state))

    assert fake_store.calls == ["cleanup", "find", "save"]
    assert fake_store.resolve_calls == []
    new_insights, _matched, occurrences = fake_store.saved[0]
    assert len(new_insights) == 1
    assert len(occurrences) == 1


def test_a_custom_run_with_nothing_to_correlate_resolves_nothing(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Verify custom runs skip auto-resolution even when no defect findings are present."""
    state = _state(trigger_type=CUSTOM_TRIGGER_TYPE)

    insight_correlation_node._func(_ctx(state))

    assert fake_store.calls == ["cleanup"]
    assert fake_store.resolve_calls == []


@pytest.mark.parametrize(
    "state_overrides",
    [
        pytest.param({}, id="unset"),
        pytest.param({"trigger_type": "scheduled"}, id="scheduled"),
    ],
)
def test_an_ambient_run_still_resolves_stale_insights(
    fake_call: FakeInsightModelCall,
    fake_store: FakeInsightStore,
    state_overrides: dict[str, Any],
) -> None:
    """Verify ambient and scheduled runs execute stale insight auto-resolution."""
    state = _one_cluster_state(fake_call)
    state.update(state_overrides)

    insight_correlation_node._func(_ctx(state))

    assert fake_store.calls == ["cleanup", "find", "save", "resolve"]
    assert [window for _now, window in fake_store.resolve_calls] == [14]


def test_a_zero_window_ambient_run_leaves_the_switch_to_the_store(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """Verify ambient runs forward a zero-day window to the store to handle resolution disabling."""
    state = _one_cluster_state(fake_call)
    state["insights_auto_resolve_days"] = 0

    insight_correlation_node._func(_ctx(state))

    assert [window for _now, window in fake_store.resolve_calls] == [0]


def test_clustering_failure_fails_the_sweep(
    fake_store: FakeInsightStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep whose every clustering chunk failed is broken, not degraded: it
    fails so the orchestrator retries, rather than silently recording no
    insights. A sweep that lost only some chunks keeps what the others found --
    see `clustering.cluster_and_label`."""

    def _boom(prompt: str) -> str:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(_common, "model_call_factory", lambda ctx: _boom)
    state = _state(finding_sets=[_finding_set("r-1")])

    with pytest.raises(RuntimeError, match="model unavailable"):
        insight_correlation_node._func(_ctx(state))

    # Nothing was persisted; cleanup ran first, so the retry starts clean.
    assert fake_store.saved == []
    assert fake_store.calls[0] == "cleanup"


def test_state_carries_the_counts_the_alert_selects_on(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """The key names are the contract with the run summary and the filter.

    Spelled wrong at either end, the alert reads a missing field, the
    comparison is false, and every finding goes unreported with nothing
    failing anywhere. The node writes these once, as counters; `_summarize`
    lifts the same two numbers under the names the policy extracts.
    """
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
    )
    state = _state(finding_sets=[_finding_set("r-1")])

    insight_correlation_node._func(_ctx(state))

    assert state["counters"]["insights_created"] == 1
    assert state["counters"]["insights_recurring"] == 0


def test_occurrence_records_the_newest_revision_it_was_seen_on(
    fake_call: FakeInsightModelCall, fake_store: FakeInsightStore
) -> None:
    """One defect seen on two builds is dated to the newer one, so a triager
    reads the occurrence as "this is still broken in revision 10"."""
    fake_call.response = json.dumps(
        {"clusters": [{"label": "made no tool call", "finding_ids": [0, 1]}]}
    )
    state = _state(
        finding_sets=[
            _finding_set("r-1", case_id="case-1", revision="2"),
            _finding_set("r-2", case_id="case-2", revision="10"),
        ],
    )

    insight_correlation_node._func(_ctx(state))

    _, _, occurrences = fake_store.saved[0]
    assert [o.agent_revision for o in occurrences] == ["10"]
