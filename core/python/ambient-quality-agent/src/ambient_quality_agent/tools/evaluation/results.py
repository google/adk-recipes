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

"""Interprets ``EvaluationResult`` objects.

Centralizes SDK response traversal and outcome conventions so clustering
and investigation tallies handle pass, fail, and error results consistently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentplatform._genai.types import EvaluationResult

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

PASS_SCORE = 1.0
"""Minimum score for a metric to count as passed, matching SDK conventions
for predefined metrics."""

PASSED = "passed"
FAILED = "failed"
ERRORED = "errored"


def walk_metric_results(
    pages: Iterable[EvaluationResult],
) -> Iterator[tuple[Any, str, Any]]:
    """Flatten and traverse metric results across all evaluation pages.

    Traverses the nested SDK structure:
    ``eval_case_results[].response_candidate_results[].metric_results[name]``.

    Args:
        pages: Evaluation result pages from the eval service.

    Yields:
        Tuples of ``(case_index, metric_name, metric_result)``.
    """
    for page in pages:
        for case in page.eval_case_results or []:
            for candidate in case.response_candidate_results or []:
                for name, metric_result in (
                    candidate.metric_results or {}
                ).items():
                    yield case.eval_case_index, name, metric_result


def list_failed_rubric_verdicts(metric_result: Any) -> list[Any]:
    """Identify failed rubric verdicts within a metric result.

    A rubric verdict counts as failed when:
    1. The verdict is explicitly ``False``.
    2. The verdict is ``None`` and the parent metric did not pass (evaluation
       outputs omit an explicit ``False`` verdict on rubric failures).

    Args:
        metric_result: Evaluated metric result object.

    Returns:
        List of rubric verdicts that represent failures.
    """
    score = getattr(metric_result, "score", None)
    metric_passed = score is None or score >= PASS_SCORE
    return [
        v
        for v in getattr(metric_result, "rubric_verdicts", None) or []
        if v.verdict is False or (v.verdict is None and not metric_passed)
    ]


def compute_metric_status(metric_result: Any) -> str | None:
    """Classify a metric result outcome as passed, failed, or errored.

    Determines status using the following order of precedence:
    1. Returns ``ERRORED`` if an error message is present.
    2. Returns ``FAILED`` if any rubric verdict failed according to
       `list_failed_rubric_verdicts`.
    3. Returns ``PASSED`` if rubric verdicts are present and none failed.
    4. Evaluates numeric score against `PASS_SCORE` (>= passes, < fails).
    5. Returns ``None`` (N/A) if neither verdicts nor a numeric score exist.

    Args:
        metric_result: Metric result to evaluate.

    Returns:
        Status string ('passed', 'failed', 'errored'), or ``None`` if N/A.
    """
    if getattr(metric_result, "error_message", None):
        return ERRORED
    if list_failed_rubric_verdicts(metric_result):
        return FAILED
    if getattr(metric_result, "rubric_verdicts", None):
        return PASSED  # Rubric verdicts were present and none failed.
    score = getattr(metric_result, "score", None)
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return PASSED if score >= PASS_SCORE else FAILED
    return None


def count_outcomes(pages: Iterable[EvaluationResult]) -> dict[str, int]:
    """Tally passed, failed, and errored outcomes across all evaluation pages.

    Excludes N/A outcomes that lack both rubric verdicts and numeric scores.

    Args:
        pages: Evaluation result pages from the eval service.

    Returns:
        Mapping from outcome status ('passed', 'failed', 'errored') to counts.
    """
    counts = {PASSED: 0, FAILED: 0, ERRORED: 0}
    for _case_index, _name, metric_result in walk_metric_results(pages):
        status = compute_metric_status(metric_result)
        if status is not None:
            counts[status] += 1
    return counts


def build_outcome_keys(
    eval_cases: Sequence[Any], seen_before: int
) -> list[str]:
    """Generate consistent outcome keys for evaluation cases in a page.

    Centralizes key generation so different analyzers judging the same
    trajectory produce identical keys, preventing duplicate counting during
    aggregation. Keys use ``eval_case_id``, falling back to a sequential offset
    when an ID is absent (such as in tests) to avoid key collisions.

    Args:
        eval_cases: Evaluation cases in the current page.
        seen_before: Count of cases evaluated in preceding pages.

    Returns:
        List of outcome keys in page order.
    """
    return [
        str(getattr(case, "eval_case_id", "") or "")
        or f"#{seen_before + offset + 1}"
        for offset, case in enumerate(eval_cases)
    ]


OUTCOME_RANK = {PASSED: 0, FAILED: 1, ERRORED: 2}
"""Precedence ranking for collapsing multiple outcomes on a single trajectory.

Centralizes ordering (errored > failed > passed) so collapsing multiple
evaluations across metrics or analyzers resolves consistently.
"""


def compute_case_outcomes(pages: Iterable[EvaluationResult]) -> dict[str, str]:
    """Resolve the final outcome status for each trajectory across all metrics.

    Collapses multiple evaluations for a case to its highest-severity status
    defined by ``OUTCOME_RANK`` (errored > failed > passed).

    Args:
        pages: Evaluation result pages from the eval service.

    Returns:
        Mapping from case index (as string) to its resolved outcome status.
    """
    worst: dict[str, str] = {}
    for page in pages:
        for case_index, _name, metric_result in walk_metric_results([page]):
            status = compute_metric_status(metric_result)
            if status is None:
                continue
            key = str(case_index)
            if (
                key not in worst
                or OUTCOME_RANK[status] > OUTCOME_RANK[worst[key]]
            ):
                worst[key] = status
    return worst


def count_rubrics(pages: Iterable[EvaluationResult]) -> int:
    """Count total rubric verdicts produced across all evaluation pages.

    Establishes the total baseline of evaluated evidence against which failed
    rubrics are isolated for clustering.

    Args:
        pages: Evaluation result pages from the eval service.

    Returns:
        Total count of rubric verdicts evaluated.
    """
    return sum(
        len(getattr(metric_result, "rubric_verdicts", None) or [])
        for _case_index, _name, metric_result in walk_metric_results(pages)
    )
