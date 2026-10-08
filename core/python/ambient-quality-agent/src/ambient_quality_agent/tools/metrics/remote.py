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

"""Evaluate sessions with custom metrics the remote evaluation service runs or judges.

Metric functions are sent as source code in `CodeExecutionMetric` instances to the
evaluation service, which executes them remotely; judged metrics are sent as
`LLMMetric` prompt templates the service's model scores. Either way the service
returns `EvalCaseMetricResult` structures matching `runner`'s local evaluation
shapes.

The evaluation service supplies code metric input cases structured as::

    {"response": {"contents": {"gemini_contents": [{"role": ..., "parts": ...}]}},
     "agent_eval_data": {"turns": [...]}}

A judged metric's template sees the same response through `{response}`, and the
user message it answers through `{prompt}`.

Remote execution isolates untrusted metric code from local credentials at the cost
of network round trips per case and metric during SDK fan-out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agentplatform._genai.types import (
    CodeExecutionMetric,
    EvaluationDataset,
    Metric,
    ResponseCandidate,
)
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    PASSED,
    build_outcome_keys,
    walk_metric_results,
)
from ambient_quality_agent.tools.insights.findings import Finding, FindingSet
from ambient_quality_agent.tools.metrics import runner
from ambient_quality_agent.tools.metrics.library import (
    REMOTE_KIND,
    RemoteJudgeMetric,
)
from ambient_quality_agent.tools.review import session_review
from google.genai import types as genai_types

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import Any

    from ambient_quality_agent.tools.ingestion.models import Page
    from ambient_quality_agent.tools.metrics.library import RemoteMetric

logger = logging.getLogger(__name__)

_SILENT_RESPONSE = genai_types.Content(
    role="model", parts=[genai_types.Part(text="")]
)
"""Fallback model response ensuring the SDK populates the 'response' key when the agent never spoke."""

_NO_REASON = (
    "The eval service does not return an explanation for a code metric, so this "
    "finding is the metric's expected behavior and the score alone."
)
"""Explanation text assigned to remote code findings.

The evaluation service returns an empty explanation for code execution metrics.
This static text indicates the finding relies solely on the score and expected
behavior. A judged metric does get an explanation back, so it keeps the local
wording for the rare case where one is missing.
"""


def build_eval_metrics(metrics: Iterable[RemoteMetric]) -> list[Metric]:
    """Build the SDK metrics the evaluation service runs or judges.

    A judged metric is sent as the `LLMMetric` the library validated at load;
    a code metric is wrapped here. Both use `service_name` to ensure metric
    names sent to the service match the names returned in results for
    correlation in `score_page_results`.

    Args:
        metrics: Remote code and judged metrics to convert.

    Returns:
        SDK metrics, one per input metric, configured with service names.
    """
    return [
        metric.metric
        if isinstance(metric, RemoteJudgeMetric)
        else CodeExecutionMetric(
            name=metric.service_name, custom_function=metric.source
        )
        for metric in metrics
    ]


def build_request_dataset(dataset: EvaluationDataset) -> EvaluationDataset:
    """Create a copy of `dataset` with each case's agent response and prompt populated.

    Fetchers store conversation turns under `agent_data` without populating
    `responses` or `prompt`. Because the SDK evaluation service reads candidate
    responses from `responses` and renders a judge's `{prompt}` only from
    `EvalCase.prompt`, this derives both from the turns the same way `runner`
    does locally. A response or prompt a case already carries is left alone.
    Returns a copy to avoid mutating the shared dataset used by other
    evaluators in the sweep.

    Args:
        dataset: Original evaluation dataset.

    Returns:
        New evaluation dataset containing cases populated with candidate
        responses and prompts.
    """
    cases = []
    for eval_case in dataset.eval_cases or []:
        update: dict[str, Any] = {}
        if not eval_case.responses:
            # Provide a silent response so the SDK populates the 'response' key.
            # Omitting it causes metrics accessing the response dictionary to raise KeyError.
            response = (
                runner.extract_response_content(eval_case) or _SILENT_RESPONSE
            )
            update["responses"] = [ResponseCandidate(response=response)]
        if eval_case.prompt is None and (
            prompt := runner.extract_prompt_content(eval_case)
        ):
            update["prompt"] = prompt
        cases.append(
            eval_case.model_copy(update=update) if update else eval_case
        )
    return dataset.model_copy(update={"eval_cases": cases})


def score_page_results(
    page: Page,
    result: Any,
    metrics: Mapping[str, RemoteMetric],
    tally: runner.Tally,
) -> FindingSet:
    """Turn evaluation service results for a page into findings and update tally.

    Processes results through the following steps:
    1. Match returned metric results to page cases by positional index.
    2. Extract verdicts and score each case against metric thresholds.
    3. Mark cases missing responses from any requested metric as errored.
    4. Tally overall outcomes and return aggregated findings.

    Args:
        page: Page containing the original evaluation cases and revisions.
        result: Raw result payload returned by the evaluation service.
        metrics: Mapping of service metric names to remote code and judged metrics.
        tally: Accumulator updated with case verdicts and error counts.

    Returns:
        FindingSet containing generated findings and session turns.
    """
    cases = list(page.dataset.eval_cases or [])
    keys = build_outcome_keys(cases, tally.cases)
    findings: list[Finding] = []
    sessions: dict[str, list[dict]] = {}
    # Map case position to combined status across all evaluating metrics.
    statuses: dict[int, str] = {}
    # Track which metrics evaluated each case to ensure all requested metrics answered.
    judged: dict[int, set[str]] = {}

    for case_index, metric_name, metric_result in walk_metric_results([result]):
        metric = metrics.get(metric_name)
        if metric is None or not isinstance(case_index, int):
            continue
        if not 0 <= case_index < len(cases):
            logger.warning(
                "The eval service returned case %r, which this page does not "
                "hold; ignoring it.",
                case_index,
            )
            continue
        eval_case = cases[case_index]
        session_id = str(getattr(eval_case, "eval_case_id", "") or "")
        # Record evaluation before parsing to prevent double-counting malformed verdicts as silent.
        judged.setdefault(case_index, set()).add(metric.service_name)
        try:
            if metric.threshold is None:
                score = runner.extract_score(metric_result)
            else:
                unexplained = (
                    _NO_REASON
                    if metric.kind == REMOTE_KIND
                    else runner.UNEXPLAINED
                )
                actual = runner.extract_verdict_text(
                    metric_result, metric.threshold, unexplained
                )
        except Exception as exc:
            runner.record_first_error(tally, exc)
            statuses[case_index] = ERRORED
            runner.record_metric(tally, metric.name, ERRORED)
            logger.warning(
                "Remote metric %r failed on session %s: %s",
                metric.name,
                session_id,
                exc,
            )
            continue
        tally.scored_metrics.add(metric.name)
        if metric.threshold is None:
            # Scores only, as agents-cli reports a judge: no pass mark, so the
            # session is not a defect and no finding is filed.
            statuses.setdefault(case_index, PASSED)
            runner.record_metric(tally, metric.name, runner.SCORED)
            runner.record_score(tally, metric.name, score)
            continue
        if actual is None:
            statuses.setdefault(case_index, PASSED)
            runner.record_metric(tally, metric.name, PASSED)
            continue
        runner.record_metric(tally, metric.name, FAILED)
        # Preserve ERRORED status since execution errors take precedence over failures.
        if statuses.get(case_index) != ERRORED:
            statuses[case_index] = FAILED
        findings.append(
            Finding(
                expected_behavior=metric.expected,
                actual_behavior=actual,
                session_id=session_id,
                agent_revision=page.revisions.get(session_id, ""),
            )
        )
        if session_id and session_id not in sessions:
            sessions[session_id] = session_review.extract_session_turns(
                eval_case
            )

    # Check every case in the page to identify cases omitted by the evaluation service.
    for position in range(len(cases)):
        silent = set(metrics) - judged.get(position, set())
        if statuses.get(position) == PASSED and silent:
            statuses[position] = ERRORED
        for service_name in silent:
            runner.record_metric(tally, metrics[service_name].name, ERRORED)
    _tally_cases(tally, keys, statuses)
    tally.findings += len(findings)
    return FindingSet(findings=findings, sessions=sessions)


def _tally_cases(
    tally: runner.Tally, keys: list[str], statuses: Mapping[int, str]
) -> None:
    """Record a verdict for every case in the page, preserving order.

    Cases omitted by the evaluation service default to ERRORED to prevent
    unanswered evaluations from being treated as passing.

    Args:
        tally: Accumulator updated with case verdicts and totals.
        keys: Outcome keys identifying each case position.
        statuses: Mapping of case positions to resolved outcome statuses.
    """
    for position, key in enumerate(keys):
        status = statuses.get(position, ERRORED)
        tally.outcomes[key] = status
        setattr(tally, status, getattr(tally, status) + 1)
    tally.cases += len(keys)


def mark_page_errored(
    page: Page,
    tally: runner.Tally,
    exc: BaseException,
    metrics: Iterable[RemoteMetric] = (),
) -> None:
    """Record every case and metric in a page as errored after a failed service call.

    Accounts for all page cases in the sweep totals when a service failure occurs,
    preventing unexecuted evaluations from being omitted or counted as passed.

    Args:
        page: Page whose cases could not be evaluated.
        tally: Accumulator updated with error counts and outcomes.
        exc: Exception raised during the evaluation service call.
        metrics: Remote code metrics that were scheduled to run on the page.
    """
    runner.record_first_error(tally, exc)
    cases = list(page.dataset.eval_cases or [])
    for metric in metrics:
        for _case in cases:
            runner.record_metric(tally, metric.name, ERRORED)
    _tally_cases(tally, build_outcome_keys(cases, tally.cases), {})
