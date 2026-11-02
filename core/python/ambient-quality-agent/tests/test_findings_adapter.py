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

"""Tests for the eval-to-findings adapter (offline, no GCP).

The adapter holds the ``EvaluationResult`` shape so clustering does not.
Covers what it reduces a sweep's pages to: one finding per failed rubric, the
sessions those findings came from, and the configurations they ran under -- plus
that a stored example carrying the ``rubric_id`` key still validates.
"""

from __future__ import annotations

from typing import Any

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
from ambient_quality_agent.tools.evaluation.findings_adapter import (
    eval_results_to_finding_set,
)
from ambient_quality_agent.tools.insights.models import RubricExample
from pydantic import ValidationError

# --- builders -----------------------------------------------------------------


def _verdict(
    rubric_id: str, description: str, passed: bool | None
) -> RubricVerdict:
    """Constructs a single rubric verdict.

    Args:
        rubric_id: Evaluated rubric identifier.
        description: Rubric property description.
        passed: Three-state evaluation verdict from SDK (True, False, or None).

    Returns:
        RubricVerdict with the specified rubric and verdict.
    """
    return RubricVerdict(
        evaluated_rubric=Rubric(
            rubric_id=rubric_id,
            content=RubricContent(
                property=RubricContentProperty(description=description)
            ),
        ),
        verdict=passed,
        reasoning=f"reasoning for {rubric_id}",
    )


def _traced_case(
    text: str, case_id: str = "case-abc", agents: dict[str, dict] | None = None
) -> EvalCase:
    """Constructs an evaluation case carrying one user turn.

    Built from a dictionary because the SDK does not publicly re-export
    ``AgentData`` or ``ConversationTurn``.

    Args:
        text: User turn text.
        case_id: Evaluated case identifier.
        agents: Optional mapping of agent configurations.

    Returns:
        Validated EvalCase instance.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "turns": [
                    {
                        "turn_index": 0,
                        "events": [
                            {
                                "author": "user",
                                "content": {
                                    "role": "user",
                                    "parts": [{"text": text}],
                                },
                            }
                        ],
                    }
                ],
            }
            | ({"agents": agents} if agents else {}),
        }
    )


def _page(
    verdicts: list[RubricVerdict],
    *,
    metric: str = "multi_turn_task_success_v1",
    score: float = 0.0,
    with_trace: bool = False,
    case_index: int | None = 0,
    trace_text: str = "the printer is jammed",
    case_id: str = "case-abc",
    agents: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Constructs a single-case evaluation page in dictionary form.

    Args:
        verdicts: Rubric verdicts for the evaluation candidate.
        metric: Metric identifier evaluated on the candidate.
        score: Score assigned to the metric result.
        with_trace: Whether to include an evaluation dataset trace.
        case_index: Optional index of the evaluation case.
        trace_text: Content text for the user turn in the trace.
        case_id: Case identifier for the evaluation dataset.
        agents: Optional mapping of agent configurations.

    Returns:
        EvaluationResult serialized as a JSON dictionary.
    """
    dataset = None
    if with_trace:
        dataset = [
            EvaluationDataset(
                eval_cases=[_traced_case(trace_text, case_id, agents)]
            )
        ]
    return EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=case_index,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={
                            metric: EvalCaseMetricResult(
                                score=score, rubric_verdicts=verdicts
                            )
                        },
                    )
                ],
            )
        ],
        evaluation_dataset=dataset,
    ).model_dump(mode="json")


# --- failed-rubric selection --------------------------------------------------


def test_keeps_failures_and_drops_passes() -> None:
    """On a failing metric, ``False`` and score-failing ``None`` both survive;
    only the ``True`` (PASS) is dropped.

    The metric scores 0.0, so the ``None`` counts as a failure. The PASS never
    does.
    """
    page = _page(
        [
            _verdict("r-fail", "must call the ticket tool", False),
            _verdict("r-pass", "must confirm the ticket", True),
            _verdict("r-na", "must set a priority", None),
        ]
    )

    findings = eval_results_to_finding_set([page]).findings

    assert [f.expected_behavior for f in findings] == [
        "must call the ticket tool",
        "must set a priority",
    ]
    assert findings[0].actual_behavior == "reasoning for r-fail"


def test_none_verdict_on_a_passing_metric_is_not_a_failure() -> None:
    """A ``None`` that cost the agent no score is genuinely undecided, so it
    produces no finding."""
    page = _page(
        [_verdict("r-pass", "d", True), _verdict("r-na", "d", None)], score=1.0
    )

    assert eval_results_to_finding_set([page]).findings == []


def test_an_errored_metric_contributes_nothing() -> None:
    """A null score means the metric errored, so its verdicts were never judged;
    treating them as agent failures would invent findings from an outage."""
    errored = _page([_verdict("r-1", "d", None)], score=0.0)
    errored["eval_case_results"][0]["response_candidate_results"][0][
        "metric_results"
    ]["multi_turn_task_success_v1"]["score"] = None

    assert eval_results_to_finding_set([errored]).findings == []


def test_findings_span_pages_and_metrics_in_order() -> None:
    """Failures collect across pages and metrics alike, in traversal order.

    One defect fails rubrics under several metrics; keeping the metric would
    split it into several issues.
    """
    pages = [
        _page(
            [_verdict("r-1", "d1", False)], metric="multi_turn_task_success_v1"
        ),
        _page(
            [_verdict("r-2", "d2", False)],
            metric="multi_turn_tool_use_quality_v1",
        ),
    ]

    findings = eval_results_to_finding_set(pages).findings

    assert [f.actual_behavior for f in findings] == [
        "reasoning for r-1",
        "reasoning for r-2",
    ]


def test_raises_on_an_unparseable_page() -> None:
    """Pages are dumped and re-validated within one graph run on one SDK version,
    so a page that will not validate is an impossible state that must surface
    loudly rather than being silently dropped."""
    with pytest.raises(ValidationError):
        eval_results_to_finding_set(
            ["not-a-page", _page([_verdict("r-1", "d1", False)])]
        )


def test_returns_nothing_when_all_rubrics_pass() -> None:
    """The healthy sweep: no failures, so no findings and no sessions."""
    finding_set = eval_results_to_finding_set(
        [_page([_verdict("r-pass", "d", True)], score=1.0)]
    )

    assert finding_set.findings == []
    assert finding_set.sessions == {}


# --- expected / actual extraction ---------------------------------------------


def test_expected_behavior_falls_back_when_a_rubric_has_no_description() -> (
    None
):
    """A rubric arriving without a property description still yields a usable
    finding rather than an empty one."""
    page = _page(
        [RubricVerdict(evaluated_rubric=None, verdict=False, reasoning="oops")]
    )

    (finding,) = eval_results_to_finding_set([page]).findings

    assert finding.expected_behavior == ""
    assert finding.actual_behavior == "oops"


def test_the_eval_adapter_leaves_agent_id_empty() -> None:
    """A rubric verdict is not attributed to an agent, so the finding carries no
    agent name."""
    page = _page([_verdict("r-1", "d", False)])

    (finding,) = eval_results_to_finding_set([page]).findings

    assert finding.agent_id == ""


# --- sessions -----------------------------------------------------------------


def test_a_finding_carries_its_session_and_the_session_carries_its_turns() -> (
    None
):
    """The finding names its session; the session map holds that session's
    conversation turns."""
    page = _page(
        [_verdict("r-1", "must call the tool", False)], with_trace=True
    )

    finding_set = eval_results_to_finding_set([page])

    (finding,) = finding_set.findings
    assert finding.session_id == "case-abc"
    assert "case-abc" in finding_set.sessions
    assert "printer is jammed" in str(finding_set.sessions["case-abc"])


def test_two_findings_in_one_session_share_one_trace() -> None:
    """Several findings in one conversation key one session, materialized once."""
    page = _page(
        [_verdict("r-1", "d1", False), _verdict("r-2", "d2", False)],
        with_trace=True,
        case_id="case-1",
    )

    finding_set = eval_results_to_finding_set([page])

    assert [f.session_id for f in finding_set.findings] == ["case-1", "case-1"]
    assert list(finding_set.sessions) == ["case-1"]


def test_only_sessions_that_produced_a_finding_are_materialized() -> None:
    """A session whose rubrics all passed is not carried: `sessions` is the
    evidence behind failures, not every conversation the sweep saw."""
    pages = [
        _page(
            [_verdict("r-1", "d", False)],
            with_trace=True,
            case_id="failed-case",
        ),
        _page(
            [_verdict("r-2", "d", True)],
            score=1.0,
            with_trace=True,
            case_id="passed-case",
        ),
    ]

    finding_set = eval_results_to_finding_set(pages)

    assert list(finding_set.sessions) == ["failed-case"]


def test_a_page_without_its_dataset_still_yields_a_finding() -> None:
    """Results can arrive without the source dataset; the finding then names no
    session and the session map holds nothing for it."""
    page = _page(
        [_verdict("r-1", "must call the tool", False)], with_trace=False
    )

    finding_set = eval_results_to_finding_set([page])

    (finding,) = finding_set.findings
    assert finding.session_id == ""
    assert finding_set.sessions == {}


def test_a_missing_case_index_leaves_a_finding_without_a_session() -> None:
    """``eval_case_index`` is optional in the SDK; a finding whose result index
    is absent cannot be joined to a case, so it carries no session."""
    page = _page(
        [_verdict("r-1", "d", False)], with_trace=True, case_index=None
    )

    finding_set = eval_results_to_finding_set([page])

    (finding,) = finding_set.findings
    assert finding.session_id == ""
    assert finding_set.sessions == {}


# --- agent configurations -----------------------------------------------------


def test_a_failing_session_carries_the_configuration_it_ran_under() -> None:
    """The instruction lives in the eval case's own ``agent_data``, so the eval
    path can name the build a defect appeared under."""
    page = _page(
        [_verdict("r-1", "must call the tool", False)],
        with_trace=True,
        case_id="sess-1",
        agents={
            "root": {"agent_id": "root", "instruction": "Always be polite"}
        },
    )

    ((session_id, (revision,)),) = eval_results_to_finding_set(
        [page], {"sess-1": "4"}
    ).agent_revisions.items()

    assert session_id == "sess-1"
    assert (revision.agent_id, revision.revision_id) == ("root", "4")
    assert revision.instruction == "Always be polite"


def test_only_sessions_that_produced_a_finding_carry_a_configuration() -> None:
    """`agent_revisions` is the configuration behind the failures, not every
    configuration the sweep saw: the passing session's is never carried."""
    pages = [
        _page(
            [_verdict("r-1", "d", False)],
            with_trace=True,
            case_id="failed-case",
            agents={"root": {"agent_id": "root", "instruction": "be helpful"}},
        ),
        _page(
            [_verdict("r-2", "d", True)],
            score=1.0,
            with_trace=True,
            case_id="passed-case",
            agents={
                "quiet": {"agent_id": "quiet", "instruction": "say little"}
            },
        ),
    ]

    assert {
        session_id: [r.agent_id for r in revisions]
        for session_id, revisions in eval_results_to_finding_set(
            pages
        ).agent_revisions.items()
    } == {"failed-case": ["root"]}


def test_no_configuration_without_agent_telemetry() -> None:
    """A source recording conversations but no agent configuration leaves the
    verification pass to reason from the trajectories alone."""
    page = _page([_verdict("r-1", "d", False)], with_trace=True)

    assert eval_results_to_finding_set([page]).agent_revisions == {}


# --- stored wire shape --------------------------------------------------------


def test_a_stored_example_with_the_old_rubric_id_key_validates() -> None:
    """Serialized examples carrying unexpected keys (such as `rubric_id`) validate without errors."""
    stored = {
        "rubric": {
            "rubric_id": "r-1",
            "expected_behavior": "call create_ticket",
            "actual_behavior": "no tool call was made",
        },
        "eval_case_id": "case-1",
        "trace": [{"role": "user"}],
    }

    example = RubricExample.model_validate(stored)

    assert example.rubric.expected_behavior == "call create_ticket"
    assert example.rubric.actual_behavior == "no tool call was made"
    assert example.eval_case_id == "case-1"
    assert not hasattr(example.rubric, "rubric_id")


# --- deployment revision ------------------------------------------------------


def test_findings_carry_their_session_s_revision() -> None:
    """The revision the fetcher read off the telemetry reaches every finding
    from that session."""
    page = _page(
        [_verdict("r-1", "d1", False), _verdict("r-2", "d2", False)],
        with_trace=True,
        case_id="sess-1",
    )

    finding_set = eval_results_to_finding_set([page], {"sess-1": "4"})

    assert [f.agent_revision for f in finding_set.findings] == ["4", "4"]


def test_findings_are_unattributed_without_a_revision_map() -> None:
    """Findings default to an empty revision when no revision map is provided."""
    page = _page(
        [_verdict("r-1", "d1", False)], with_trace=True, case_id="sess-1"
    )

    unmapped = eval_results_to_finding_set([page])
    unrelated = eval_results_to_finding_set([page], {"other-session": "4"})

    assert [f.agent_revision for f in unmapped.findings] == [""]
    assert unrelated == unmapped
