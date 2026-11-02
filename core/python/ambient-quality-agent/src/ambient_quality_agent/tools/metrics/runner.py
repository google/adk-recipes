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

"""Evaluate session cases against code metrics to produce structured findings.

Executes metrics directly in-process to bypass SDK rate limiters and nested
result structures.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import TYPE_CHECKING, Any

from agentplatform._genai.types import EvalCaseMetricResult
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    PASS_SCORE,
    PASSED,
    build_outcome_keys,
    list_failed_rubric_verdicts,
)
from ambient_quality_agent.tools.insights.findings import Finding, FindingSet
from ambient_quality_agent.tools.review import session_review
from google.genai import types as genai_types

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ambient_quality_agent.tools.ingestion.models import Page
    from ambient_quality_agent.tools.metrics.library import CodeMetric

logger = logging.getLogger(__name__)

UNEXPLAINED = "The metric failed but returned no explanation."
"""What a finding says when a metric failed and gave no reason.

The remote code path substitutes its own sentence, because the eval service
never returns an explanation for a code metric; a judged metric keeps this one.
"""


@dataclasses.dataclass
class Tally:
    """Aggregate counters accumulated during a code-metric evaluation sweep."""

    cases: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    """Cases where any metric errored, matching `traces_eval_errored`."""

    findings: int = 0
    scored_metrics: set[str] = dataclasses.field(default_factory=set)
    """Metrics that returned a verdict at least once.

    Tracks successfully executed metrics. Sessions that fail before evaluation
    still increment `cases`, which would otherwise hide unexecuted metrics.
    """

    first_error: BaseException | None = None

    outcomes: dict[str, str] = dataclasses.field(default_factory=dict)
    """Verdict per session, which the sweep totals across evaluations."""

    by_metric: dict[str, dict[str, int]] = dataclasses.field(
        default_factory=dict
    )
    """Per-metric pass, fail, and error counts accumulated during the sweep."""

    scores: dict[str, dict[str, float]] = dataclasses.field(
        default_factory=dict
    )
    """Score summary (count, mean, min, max) per scores-only metric."""


SCORED = "scored"
"""Outcome of a metric that reports a score with no pass mark, as agents-cli
reports a judge: it neither passes nor fails a session."""


def build_instance(eval_case: Any) -> dict[str, Any]:
    """Shape an eval case into the dictionary format expected by metrics.

    Fetchers construct cases from `agent_data` without populating `responses`
    or `prompt`. Both are derived from the turns so metrics receive what was
    actually said.

    Args:
        eval_case: Evaluation case containing turn events and scenario data.

    Returns:
        Dictionary representation of the case populated with extracted response
        and prompt fields.
    """
    instance = eval_case.model_dump(
        exclude={"responses"}, mode="json", exclude_none=True
    )
    instance["response"] = _extract_response(eval_case)
    prompt = extract_prompt_content(eval_case)
    if prompt is not None:
        instance["prompt"] = prompt.model_dump(mode="json", exclude_none=True)
    return instance


def score_page(
    page: Page, library: Mapping[str, CodeMetric], tally: Tally
) -> FindingSet:
    """Score evaluation cases in a page against a metric library.

    Args:
        page: Page of ingested evaluation cases and associated revisions.
        library: Mapping of metric names to CodeMetric evaluators.
        tally: Mutable counters tracking overall evaluation outcomes.

    Returns:
        FindingSet containing all detected findings and turn histories.
    """
    findings: list[Finding] = []
    sessions: dict[str, list[dict]] = {}

    eval_cases = page.dataset.eval_cases or []
    keys = build_outcome_keys(eval_cases, tally.cases)
    for position, eval_case in enumerate(eval_cases):
        session_id = str(getattr(eval_case, "eval_case_id", "") or "")
        case_findings, errored = _run_case(
            eval_case, library, session_id, tally
        )
        tally.cases += 1
        outcome_key = keys[position]
        # Errors take precedence over failures to match traces_eval_errored alerting.
        if errored:
            tally.errored += 1
            tally.outcomes[outcome_key] = "errored"
        elif case_findings:
            tally.failed += 1
            tally.outcomes[outcome_key] = "failed"
        else:
            tally.passed += 1
            tally.outcomes[outcome_key] = "passed"
        if case_findings:
            revision = page.revisions.get(session_id, "")
            findings.extend(
                f.model_copy(update={"agent_revision": revision})
                if revision
                else f
                for f in case_findings
            )
            if session_id and session_id not in sessions:
                sessions[session_id] = session_review.extract_session_turns(
                    eval_case
                )

    tally.findings += len(findings)
    return FindingSet(findings=findings, sessions=sessions)


def merge_breakdown(
    state: Any, by_metric: dict[str, dict[str, int]], *, kind: str
) -> None:
    """Merge per-metric outcome counts and execution kind into state.

    Args:
        state: State dictionary holding pipeline evaluation metrics.
        by_metric: Mapping of metric names to outcome count dictionaries.
        kind: Execution category for the metrics.
    """
    breakdown = dict(state.get("metrics_by_name") or {})
    for name, counts in by_metric.items():
        running = breakdown.setdefault(name, {})
        for outcome, n in counts.items():
            running[outcome] = running.get(outcome, 0) + n
    state["metrics_by_name"] = breakdown
    merge_metrics_detail(state, {name: {"kind": kind} for name in by_metric})


def merge_metrics_detail(state: Any, rows: dict[str, dict[str, Any]]) -> None:
    """Merge per-metric metadata rows into state['metrics_detail'].

    Preserves existing keys so earlier writes remain intact.

    Args:
        state: State dictionary holding pipeline evaluation metrics.
        rows: Mapping of metric names to metadata dictionaries.
    """
    merged = dict(state.get("metrics_detail") or {})
    for name, row in rows.items():
        merged.setdefault(name, {}).update(row)
    state["metrics_detail"] = merged


def record_metric(tally: Tally, name: str, outcome: str) -> None:
    """Increment the outcome count for a metric in tally.by_metric.

    Seeds passed, failed, and errored counts to 0 so every metric exposes
    all outcome keys.

    Args:
        tally: Evaluation counters to update.
        name: Name of the metric.
        outcome: Outcome category (passed, failed, or errored).
    """
    counts = tally.by_metric.setdefault(
        name, {PASSED: 0, FAILED: 0, ERRORED: 0}
    )
    counts[outcome] = counts.get(outcome, 0) + 1


def record_score(tally: Tally, name: str, score: float) -> None:
    """Fold one score into a scores-only metric's summary.

    Args:
        tally: Evaluation counters to update.
        name: Name of the metric.
        score: The score the metric returned for one session.
    """
    summary = tally.scores.get(name)
    if summary is None:
        tally.scores[name] = {
            "count": 1,
            "mean": score,
            "min": score,
            "max": score,
        }
        return
    count = summary["count"] + 1
    summary["mean"] += (score - summary["mean"]) / count
    summary["count"] = count
    summary["min"] = min(summary["min"], score)
    summary["max"] = max(summary["max"], score)


def _run_case(
    eval_case: Any,
    library: Mapping[str, CodeMetric],
    session_id: str,
    tally: Tally,
) -> tuple[list[Finding], bool]:
    """Run all metrics against a single session case.

    Args:
        eval_case: Evaluation case containing turn events and scenario data.
        library: Mapping of metric names to CodeMetric evaluators.
        session_id: Session identifier for the case.
        tally: Evaluation counters to update.

    Returns:
        A tuple of (findings, errored), where findings lists detected issues
        and errored indicates if metric evaluation failed with an exception.
    """
    try:
        instance = build_instance(eval_case)
    except (Exception, SystemExit) as exc:
        record_first_error(tally, exc)
        logger.warning(
            "Could not shape session %s for metrics: %s", session_id, exc
        )
        # Record an error for each library metric so unshapeable trajectories
        # are tracked in per-metric error counts, matching remote.mark_page_errored.
        for metric_name in library:
            record_metric(tally, metric_name, ERRORED)
        return [], True

    findings: list[Finding] = []
    errored = False
    for metric in library.values():
        try:
            actual = _extract_failure_text(
                metric.evaluate(instance), metric.threshold
            )
        # Catch SystemExit in case metric scripts invoke sys.exit or argparse.
        except (Exception, SystemExit) as exc:
            record_first_error(tally, exc)
            errored = True
            record_metric(tally, metric.name, ERRORED)
            logger.warning(
                "Metric %r failed on session %s: %s",
                metric.name,
                session_id,
                exc,
            )
            continue
        tally.scored_metrics.add(metric.name)
        if actual is None:
            record_metric(tally, metric.name, PASSED)
            continue
        record_metric(tally, metric.name, FAILED)
        findings.append(
            Finding(
                expected_behavior=metric.expected,
                actual_behavior=actual,
                session_id=session_id,
            )
        )
    return findings, errored


def _extract_failure_text(
    outcome: Any, threshold: float = PASS_SCORE
) -> str | None:
    """Extract failure explanation from a metric result.

    Args:
        outcome: Result returned by a metric evaluator.
        threshold: Score threshold required to pass.

    Returns:
        Failure explanation text, or None if the metric passed.

    Raises:
        ValueError: If outcome is not a recognized type, lacks a 'score' key,
            or contains a non-numeric or non-finite score.
    """
    if isinstance(outcome, EvalCaseMetricResult):
        return extract_verdict_text(outcome, threshold)
    if isinstance(outcome, dict):
        if "score" not in outcome:
            raise ValueError("returned a dict with no 'score' key")
        score, explanation = outcome["score"], outcome.get("explanation")
    elif isinstance(outcome, int | float):
        score, explanation = outcome, None
    else:
        raise ValueError(
            f"returned {type(outcome).__name__}, want a dict or number"
        )

    if not isinstance(score, int | float):
        raise ValueError(f"'score' is {type(score).__name__}, want a number")
    if not math.isfinite(score):
        raise ValueError(f"'score' is {score}, want a finite number")
    if score >= threshold:
        return None

    text = str(explanation or "").strip()
    if not text:
        logger.warning("A metric failed with no explanation.")
    return text or UNEXPLAINED


def extract_verdict_text(
    result: EvalCaseMetricResult,
    threshold: float = PASS_SCORE,
    unexplained: str = UNEXPLAINED,
) -> str | None:
    """Extract failure reasoning from an `EvalCaseMetricResult`.

    Evaluates the verdict using this sequence:
    1. Check for SDK error messages.
    2. Extract reasoning from failed rubric verdicts.
    3. Pass if any rubric was decided without failures.
    4. Reject unexecuted shapes where rubrics are undecided and scores are missing.
    5. Check whether the numeric score meets the pass threshold.

    Shared with the remote runner to ensure consistent verdict parsing.

    Args:
        result: Evaluation metric result from the SDK.
        threshold: Score threshold required to pass.
        unexplained: Fallback message when failure reasoning is empty.

    Returns:
        Failure reasoning text, or None if the result passed.

    Raises:
        ValueError: If the result contains an error message, lacks both decisive
            rubrics and numeric scores, or has a non-finite score.
    """
    if result.error_message:
        raise ValueError(f"returned an errored result: {result.error_message}")
    failed = list_failed_rubric_verdicts(result)
    if failed:
        reasons = [str(v.reasoning or "").strip() for v in failed]
        return "; ".join(r for r in reasons if r) or unexplained
    verdicts = result.rubric_verdicts or []
    if verdicts and any(v.verdict is not None for v in verdicts):
        return None  # At least one rubric was decided, and none failed.
    if verdicts and not isinstance(result.score, int | float):
        # The SDK returns empty verdicts with no score when a metric raises.
        # Because list_failed_rubric_verdicts reads scoreless results as passing,
        # reject this shape to prevent unexecuted metrics from reporting clean.
        raise ValueError("returned rubric verdicts that decided nothing")
    score = result.score
    if not isinstance(score, int | float) or not math.isfinite(score):
        raise ValueError(
            "returned neither a rubric verdict nor a numeric score"
        )
    if score >= threshold:
        return None
    return str(result.explanation or "").strip() or unexplained


def extract_score(result: EvalCaseMetricResult) -> float:
    """Extract the numeric score from an `EvalCaseMetricResult`.

    Args:
        result: Evaluation metric result from the SDK.

    Returns:
        The score.

    Raises:
        ValueError: If the result carries an error or no finite numeric score.
    """
    if result.error_message:
        raise ValueError(f"returned an errored result: {result.error_message}")
    score = result.score
    if not isinstance(score, int | float) or not math.isfinite(score):
        raise ValueError("returned no numeric score")
    return float(score)


def record_first_error(tally: Tally, exc: BaseException) -> None:
    """Record the first exception encountered during evaluation.

    Shared with the remote runner to maintain a single error-recording invariant.

    Args:
        tally: Counters tracking evaluation errors.
        exc: Exception to record if none has been set yet.
    """
    if tally.first_error is None:
        tally.first_error = exc


def _extract_response(eval_case: Any) -> dict[str, Any]:
    """Extract the final agent response as a model Content dict, excluding thoughts.

    Args:
        eval_case: Evaluation case containing candidate responses or turn history.

    Returns:
        Serialized Content dictionary representing the final response, or an empty
        dict if no response was found.
    """
    for candidate in getattr(eval_case, "responses", None) or []:
        response = getattr(candidate, "response", None)
        if response is not None:
            return response.model_dump(mode="json", exclude_none=True)

    content = extract_response_content(eval_case)
    return content.model_dump(mode="json", exclude_none=True) if content else {}


def extract_response_content(eval_case: Any) -> genai_types.Content | None:
    """Extract the final agent utterance as a `Content` object.

    The SDK reads responses only from `responses`, which fetchers do not
    populate. Deriving content consistently ensures local and remote execution
    see the same data.

    Args:
        eval_case: Evaluation case containing agent conversation history.

    Returns:
        Model `Content` containing the final utterance, or None if the agent
        produced no text.
    """
    text = _extract_final_agent_text(getattr(eval_case, "agent_data", None))
    return (
        genai_types.Content(role="model", parts=[genai_types.Part(text=text)])
        if text
        else None
    )


def _extract_final_agent_text(agent_data: Any) -> str:
    """Extract the agent's final text after the user's last message.

    What agents-cli reads as a case's response: the agent's final text for the
    last prompt, looking past trailing tool calls and thoughts. A user message
    the agent never answered leaves it empty, so the silence is judged rather
    than an earlier exchange.

    Args:
        agent_data: Session data containing conversation turns and events.

    Returns:
        Text of the final agent utterance, or an empty string if none found.
    """
    return _split_last_exchange(agent_data)[1]


def extract_prompt_content(eval_case: Any) -> genai_types.Content | None:
    """Extract the prompt agents-cli would carry for this session as a case.

    A case's own `prompt`, then its scenario's `starting_prompt`, take
    precedence, as in the SDK. Fetchers set neither, so for a real session this
    is its user message when the session is single-turn -- it opens with its
    only user message -- and None otherwise, because agents-cli sets a prompt
    only on single-turn cases.

    Args:
        eval_case: Evaluation case containing prompt, scenario, or turn data.

    Returns:
        The prompt as `Content`, or None for a multi-turn session.
    """
    prompt = getattr(eval_case, "prompt", None)
    if prompt is not None:
        return prompt
    scenario = getattr(eval_case, "user_scenario", None)
    starting = getattr(scenario, "starting_prompt", None) if scenario else None
    if starting:
        return genai_types.Content(parts=[genai_types.Part(text=starting)])
    text = _split_last_exchange(getattr(eval_case, "agent_data", None))[0]
    return (
        genai_types.Content(role="user", parts=[genai_types.Part(text=text)])
        if text is not None
        else None
    )


def _split_last_exchange(agent_data: Any) -> tuple[str | None, str]:
    """Split a session into its prompt and the agent's final reply.

    Args:
        agent_data: Session data containing conversation turns and events.

    Returns:
        `(prompt, response)`. The prompt is the session's user message if the
        session opens with its only one, as a single-turn agents-cli case does,
        and None otherwise. The response is the agent's final text after the
        user's last message, or after nothing when the user never spoke; empty
        if none.
    """
    events = _list_utterances(agent_data)
    users = [i for i, (by_agent, _text) in enumerate(events) if not by_agent]
    prompt = events[0][1] if users == [0] else None
    after = events[users[-1] + 1 :] if users else events
    response = next((text for _by_agent, text in reversed(after) if text), "")
    return prompt, response


def _list_utterances(agent_data: Any) -> list[tuple[bool, str]]:
    """List the user's messages and the agent's actions, in order.

    An agent event is kept with its text, which is empty for a tool call or a
    thought: it still makes the session more than a single user message. A user
    event is kept only when it carries text, because ADK records a tool's
    result as a `user`-role event, and that is not the user speaking.

    Args:
        agent_data: Session data containing conversation turns and events.

    Returns:
        `(by_agent, text)` pairs, one per kept event.
    """
    utterances = []
    for turn in getattr(agent_data, "turns", None) or []:
        for event in getattr(turn, "events", None) or []:
            parts = (
                getattr(getattr(event, "content", None), "parts", None) or []
            )
            text = "".join(
                part.text
                for part in parts
                if getattr(part, "text", None)
                and not getattr(part, "thought", False)
            )
            by_agent = _is_agent(event)
            if by_agent or text:
                utterances.append((by_agent, text))
    return utterances


def _is_agent(event: Any) -> bool:
    """Check whether a conversation event originated from the agent.

    Args:
        event: Conversation event to check.

    Returns:
        True if the event was generated by the agent; False if generated by the user.
    """
    role = getattr(getattr(event, "content", None), "role", None)
    return role != "user" and getattr(event, "author", None) != "user"
