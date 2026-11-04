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

"""The source-neutral input to insight clustering.

Clustering groups quality problems by root cause; it does not care where a
problem came from. `Finding` is that problem reduced to what clustering reads,
and `FindingSet` is one producer's whole contribution to a sweep. The eval
adapter is one producer; an LLM session reviewer is another, and both hand
clustering the same shape.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.agent_revision import AgentRevision
from pydantic import AliasChoices, BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

MAX_FINDINGS_PER_TRAJECTORY = 10
"""Maximum number of findings kept per trajectory to bound payload size. Read via
this module at call time so that a value assigned to it at run time applies."""


class Finding(BaseModel):
    """One quality problem found in one trajectory, as clustering consumes it.

    Source-neutral: a failed eval rubric and an LLM reviewer's observation both
    reduce to the same pair.
    """

    expected_behavior: str
    """What the agent should have done."""

    actual_behavior: str
    """What it did instead."""

    trajectory_id: str = Field(
        default="",
        validation_alias=AliasChoices("trajectory_id", "session_id"),
        serialization_alias="session_id",
    )
    """The trajectory it was found in. Stored in BigQuery under the key
    ``session_id``, inside each item of the ``rubrics`` column."""

    agent_id: str = ""
    """Identifier or name of the failing agent (root or subagent), or empty
    string if the issue cannot be attributed to a specific agent."""

    agent_revision: str = ""
    """Deployment revision the trajectory ran on, so a triager can say which
    build a defect appeared in. Empty for telemetry sources that report no
    revision, which denotes their single unnamed revision."""


class FindingSet(BaseModel):
    """One producer's contribution to an investigation: its findings and their context."""

    findings: list[Finding] = Field(default_factory=list)
    """The quality problems, in traversal order. A finding's id is its position
    here -- clustering, validation and evidence all address findings by index."""

    turns_by_trajectory: dict[str, list[dict]] = Field(default_factory=dict)
    """Map of trajectory ID to serialized conversation turns. Indexed by
    trajectory to avoid duplicating turn payloads across multiple findings."""

    agent_revisions: dict[str, list[AgentRevision]] = Field(
        default_factory=dict
    )
    """The configurations each failing trajectory ran under, keyed by
    trajectory ID.

    Keyed by trajectory rather than by ``(agent_id, revision_id)``: filtered
    tools, per-tenant subsets and request-scoped toolsets all vary within one
    deployment revision, so a trajectory judged against another's configuration
    is judged against something it never ran. A trajectory with no entry is
    judged on its turns alone. `AgentRevisionCache` interns identical
    configurations, so trajectories that share one cost a reference."""


def extract_trajectory_turns(eval_case: Any) -> list[dict]:
    """Serialize a trajectory's conversation turns as JSON-safe dictionaries.

    Fills `FindingSet.turns_by_trajectory` and the conversation in the session reviewer's
    prompt. Serialization is best effort so missing or malformed trace evidence
    does not fail the sweep.

    Args:
        eval_case: Evaluation case containing the conversation turns.

    Returns:
        JSON-safe turn dictionaries without None fields, or an empty list if
        turns are missing or fail to serialize.
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


def merge_finding_sets(
    finding_sets: Iterable[FindingSet | dict[str, Any]],
) -> FindingSet:
    """Merge multiple `FindingSet` instances or dicts into a unified set.

    Preserves input finding order so sequential indices remain stable IDs for
    clustering and validation.

    Deduplicates turns by trajectory ID (first occurrence wins) to prevent
    redundant payload storage, and the configurations behind them the same way,
    so two producers reporting one trajectory carry it across once.

    Args:
        finding_sets: Iterable of FindingSet instances or raw dict equivalents.

    Returns:
        Unified FindingSet containing all findings, turns, and revisions.
    """
    findings: list[Finding] = []
    turns_by_trajectory: dict[str, list[dict]] = {}
    agent_revisions: dict[str, list[AgentRevision]] = {}
    for raw in finding_sets:
        finding_set = FindingSet.model_validate(raw)
        findings.extend(finding_set.findings)
        for trajectory_id, turns in finding_set.turns_by_trajectory.items():
            turns_by_trajectory.setdefault(trajectory_id, turns)
        for session_id, revisions in finding_set.agent_revisions.items():
            agent_revisions.setdefault(session_id, revisions)
    return FindingSet(
        findings=findings,
        turns_by_trajectory=turns_by_trajectory,
        agent_revisions=agent_revisions,
    )
