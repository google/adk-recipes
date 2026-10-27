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

"""Tests for the shared ``EvaluationResult`` interpretation helpers.

`compute_metric_status` and `count_outcomes` classify and tally per-(case, metric)
outcomes; they must agree with `list_failed_rubric_verdicts` (tested against real
telemetry shapes in ``test_insight_clustering.py``) on what a failure is.
"""

from __future__ import annotations

from agentplatform._genai.types import (
    EvalCaseMetricResult,
    EvalCaseResult,
    EvaluationResult,
    ResponseCandidateResult,
    Rubric,
    RubricContent,
    RubricContentProperty,
    RubricVerdict,
)
from ambient_quality_agent.tools.evaluation import results

from .conftest import make_agent_case


def _verdict(passed: bool | None) -> RubricVerdict:
    return RubricVerdict(
        evaluated_rubric=Rubric(
            rubric_id="r",
            content=RubricContent(
                property=RubricContentProperty(description="d")
            ),
        ),
        verdict=passed,
        reasoning="because",
    )


def _metric(
    score: float | None = None,
    verdicts: list[bool | None] | None = None,
    error_message: str | None = None,
) -> EvalCaseMetricResult:
    return EvalCaseMetricResult(
        score=score,
        rubric_verdicts=[_verdict(v) for v in verdicts] if verdicts else None,
        error_message=error_message,
    )


# --- compute_metric_status ------------------------------------------------------------


def test_metric_status_errored_wins_over_everything() -> None:
    # An error_message means the metric never got judged; error beats any score.
    assert (
        results.compute_metric_status(_metric(score=0.0, error_message="boom"))
        == "errored"
    )


def test_metric_status_verdicts_are_authoritative() -> None:
    # A single failing verdict fails the metric regardless of score.
    assert (
        results.compute_metric_status(
            _metric(score=1.0, verdicts=[True, False])
        )
        == "failed"
    )
    # All-passing verdicts pass even without a score.
    assert (
        results.compute_metric_status(_metric(verdicts=[True, True]))
        == "passed"
    )


def test_metric_status_none_verdict_follows_the_score() -> None:
    # A None verdict on a non-passing metric is a failure (the observed shape);
    # on a passing metric it is undecided, so the metric passes.
    assert (
        results.compute_metric_status(_metric(score=0.0, verdicts=[None]))
        == "failed"
    )
    assert (
        results.compute_metric_status(_metric(score=1.0, verdicts=[True, None]))
        == "passed"
    )


def test_metric_status_score_fallback_without_verdicts() -> None:
    assert results.compute_metric_status(_metric(score=1.0)) == "passed"
    assert results.compute_metric_status(_metric(score=0.5)) == "failed"


def test_metric_status_na_when_no_verdicts_and_no_score() -> None:
    # Neither verdicts nor a numeric score -> uncounted (N/A).
    assert results.compute_metric_status(_metric()) is None


# --- count_outcomes -----------------------------------------------------------


def _page(*metrics: EvalCaseMetricResult) -> EvaluationResult:
    """Constructs an EvaluationResult page containing a single case with specified metrics.

    Args:
        *metrics: Metric results to attach to the candidate response.

    Returns:
        An EvaluationResult instance.
    """
    return EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=0,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={
                            f"m{i}": m for i, m in enumerate(metrics)
                        },
                    )
                ],
            )
        ]
    )


def test_count_outcomes_tallies_each_status() -> None:
    page = _page(
        _metric(score=1.0),  # pass
        _metric(score=0.0),  # fail
        _metric(error_message="boom"),  # error
        _metric(),  # N/A -> uncounted
    )

    assert results.count_outcomes([page]) == {
        "passed": 1,
        "failed": 1,
        "errored": 1,
    }


def test_count_outcomes_spans_multiple_pages() -> None:
    counts = results.count_outcomes(
        [
            _page(_metric(score=1.0)),
            _page(_metric(score=0.0), _metric(score=1.0)),
        ]
    )

    assert counts == {"passed": 2, "failed": 1, "errored": 0}


def test_count_outcomes_empty() -> None:
    assert results.count_outcomes([]) == {
        "passed": 0,
        "failed": 0,
        "errored": 0,
    }


# --- build_outcome_keys -------------------------------------------------------------


def test_outcome_keys_names_a_case_by_its_id() -> None:
    cases = [make_agent_case("s1"), make_agent_case("s2")]

    assert results.build_outcome_keys(cases, 0) == ["s1", "s2"]


def test_outcome_keys_numbers_a_case_that_has_no_id() -> None:
    """Two blank ids would be one key, merging two cases into a single verdict."""
    cases = [make_agent_case(""), make_agent_case("s2"), make_agent_case("")]

    assert results.build_outcome_keys(cases, 0) == ["#1", "s2", "#3"]


def test_outcome_keys_continues_the_numbering_across_pages() -> None:
    """`seen_before` is the caller's running count, so page two does not reuse
    page one's numbers and collapse a case from each into one."""
    page_one = results.build_outcome_keys([make_agent_case("")], 0)
    page_two = results.build_outcome_keys([make_agent_case("")], 1)

    assert page_one == ["#1"]
    assert page_two == ["#2"]


# --- count_rubrics ------------------------------------------------------------


def test_count_rubrics_counts_every_verdict_passed_or_failed() -> None:
    page = _page(
        _metric(score=0.0, verdicts=[True, False, None]),
        _metric(score=1.0, verdicts=[True]),
        _metric(score=1.0),  # no verdicts at all
    )

    assert results.count_rubrics([page]) == 4


def test_count_rubrics_empty() -> None:
    assert results.count_rubrics([]) == 0


# --- OUTCOME_RANK -------------------------------------------------------------


def test_outcome_rank_puts_errored_above_failed_above_passed() -> None:
    """An error hides whether the trajectory would have passed or failed, so it
    must outrank both wherever a collapse compares two outcomes."""
    assert (
        results.OUTCOME_RANK["errored"]
        > results.OUTCOME_RANK["failed"]
        > results.OUTCOME_RANK["passed"]
    )
