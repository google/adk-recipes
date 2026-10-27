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

"""Converts eval result pages into a source-neutral `FindingSet`.

Isolates nested ``EvaluationResult`` SDK structures from clustering logic. Maps
failed rubrics to `Finding` records, extracts conversation traces for affected
sessions, and captures the agent configurations under evaluation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agentplatform._genai.types import EvaluationResult
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    AgentRevisionCache,
)
from ambient_quality_agent.tools.evaluation.results import (
    list_failed_rubric_verdicts,
    walk_metric_results,
)
from ambient_quality_agent.tools.insights.findings import Finding, FindingSet

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)


def eval_results_to_finding_set(
    raw_pages: Iterable[Any], revisions: Mapping[str, str] | None = None
) -> FindingSet:
    """Transform evaluation pages into a structured `FindingSet` for clustering.

    Generates one `Finding` per failed rubric verdict, preserving page traversal
    order to maintain deterministic finding identifiers. Retains conversation
    turn history and agent configuration revisions only for sessions with at
    least one finding, discarding passing cases and raw datasets to reduce memory
    usage.

    Args:
        raw_pages: Evaluation result objects or serialized dictionaries.
        revisions: Mapping of session IDs to deployment revisions. Sessions
            omitted from the mapping use an empty string representing the
            default unnamed revision.

    Returns:
        FindingSet containing findings, session traces, and agent revisions.
    """
    pages = list(raw_pages)
    revisions = revisions or {}
    revision_cache = AgentRevisionCache()
    findings: list[Finding] = []
    sessions: dict[str, list[dict]] = {}
    agent_revisions: dict[str, list[AgentRevision]] = {}
    for rubric, verdict, page, case_index in _walk_failed_rubrics(pages):
        eval_case = _get_eval_case(page, case_index)
        session_id = _extract_eval_case_id(eval_case)
        findings.append(
            Finding(
                expected_behavior=_extract_expected_behavior(rubric),
                actual_behavior=(verdict.reasoning or "").strip(),
                session_id=session_id,
                agent_revision=revisions.get(session_id, ""),
            )
        )
        # Extract traces and configurations only once per session to avoid duplicate serialization.
        if session_id and session_id not in sessions:
            sessions[session_id] = _extract_session_turns(eval_case)
            if case_revisions := revision_cache.build_revisions(
                eval_case, revisions.get(session_id, "")
            ):
                agent_revisions[session_id] = case_revisions
    return FindingSet(
        findings=findings, sessions=sessions, agent_revisions=agent_revisions
    )


def _walk_failed_rubrics(
    raw_pages: Iterable[Any],
) -> Iterable[tuple[Any, Any, EvaluationResult, Any]]:
    """Yield failed rubric verdicts and their evaluation contexts across pages.

    Args:
        raw_pages: Evaluation result objects or serialized dictionaries.

    Yields:
        Tuples of ``(rubric, verdict, page, case_index)`` for each failed rubric.
    """
    for raw in raw_pages:
        page = EvaluationResult.model_validate(raw)
        for case_index, _metric_name, metric_result in walk_metric_results(
            [page]
        ):
            for verdict in list_failed_rubric_verdicts(metric_result):
                yield verdict.evaluated_rubric, verdict, page, case_index


def _extract_expected_behavior(rubric: Any) -> str:
    """Extract the expected behavior description from a rubric.

    Falls back to rubric type or identifier when property description is absent
    so incomplete rubrics still yield usable findings.

    Args:
        rubric: Rubric object to inspect.

    Returns:
        Extracted behavior description, or an empty string if rubric is None.
    """
    if rubric is None:
        return ""
    content = getattr(rubric, "content", None)
    prop = getattr(content, "property", None) if content else None
    description = getattr(prop, "description", None) if prop else None
    return str(description or rubric.type or rubric.rubric_id or "").strip()


def _get_eval_case(page: EvaluationResult, case_index: Any) -> Any:
    """Look up the source evaluation case matching a result index.

    Joins evaluation results with dataset cases positionally. Returns ``None``
    if the page arrived without its dataset or the index is out of bounds.

    Args:
        page: Evaluation result page containing dataset cases.
        case_index: Positional index of the evaluation case.

    Returns:
        Evaluation case object, or ``None`` if not found.
    """
    datasets = page.evaluation_dataset or []
    if not datasets or not isinstance(case_index, int):
        return None
    cases = datasets[0].eval_cases or []
    if not 0 <= case_index < len(cases):
        return None
    return cases[case_index]


def _extract_eval_case_id(eval_case: Any) -> str:
    return (
        str(getattr(eval_case, "eval_case_id", "") or "") if eval_case else ""
    )


def _extract_session_turns(eval_case: Any) -> list[dict]:
    """Extract an evaluation case's conversation turns as JSON-safe dictionaries.

    Returns an empty list if agent data is missing or serialization fails, as
    trace evidence extraction is best-effort and must not fail the sweep.

    Args:
        eval_case: Evaluation case containing agent conversation data.

    Returns:
        List of serialized conversation turn dictionaries.
    """
    agent_data = getattr(eval_case, "agent_data", None) if eval_case else None
    turns = getattr(agent_data, "turns", None) if agent_data else None
    if not turns:
        return []
    try:
        return [
            turn.model_dump(mode="json", exclude_none=True) for turn in turns
        ]
    except Exception as exc:
        logger.warning(
            "insights: could not serialize a conversation trace: %s", exc
        )
        return []
