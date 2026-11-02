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
adapter is one producer; an LLM trace reviewer is another, and both hand
clustering the same shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.agent_revision import AgentRevision
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable


class Finding(BaseModel):
    """One quality problem found in one session, as clustering consumes it.

    Source-neutral: a failed eval rubric and an LLM reviewer's observation both
    reduce to the same pair.
    """

    expected_behavior: str
    """What the agent should have done."""

    actual_behavior: str
    """What it did instead."""

    session_id: str = ""
    """The conversation it was found in."""

    agent_id: str = ""
    """Identifier or name of the failing agent (root or subagent), or empty
    string if the issue cannot be attributed to a specific agent."""

    agent_revision: str = ""
    """Deployment revision the session ran on, so a triager can say which build
    a defect appeared in. Empty for telemetry sources that report no revision,
    which denotes their single unnamed revision."""


class FindingSet(BaseModel):
    """One producer's contribution to a sweep: its findings and their context."""

    findings: list[Finding] = Field(default_factory=list)
    """The quality problems, in traversal order. A finding's id is its position
    here -- clustering, validation and evidence all address findings by index."""

    sessions: dict[str, list[dict]] = Field(default_factory=dict)
    """Map of session ID to serialized conversation turns. Indexed by session
    to avoid duplicating trace payloads across multiple findings."""

    agent_revisions: dict[str, list[AgentRevision]] = Field(
        default_factory=dict
    )
    """The configurations each failing session ran under, keyed by session id.

    Keyed by session rather than by ``(agent_id, revision_id)``: filtered tools,
    per-tenant subsets and request-scoped toolsets all vary within one
    deployment revision, so a session judged against another's configuration is
    judged against something it never ran. A session with no entry is judged on
    its trajectory alone. `AgentRevisionCache` interns identical configurations,
    so sessions that share one cost a reference."""


def merge_finding_sets(
    finding_sets: Iterable[FindingSet | dict[str, Any]],
) -> FindingSet:
    """Merge multiple `FindingSet` instances or dicts into a unified set.

    Preserves input finding order so sequential indices remain stable IDs for
    clustering and validation.

    Deduplicates session turn traces by session ID (first occurrence wins) to
    prevent redundant payload storage, and the configurations behind them the
    same way, so two producers reporting one session carry it across once.

    Args:
        finding_sets: Iterable of FindingSet instances or raw dict equivalents.

    Returns:
        Unified FindingSet containing all findings, sessions, and revisions.
    """
    findings: list[Finding] = []
    sessions: dict[str, list[dict]] = {}
    agent_revisions: dict[str, list[AgentRevision]] = {}
    for raw in finding_sets:
        finding_set = FindingSet.model_validate(raw)
        findings.extend(finding_set.findings)
        for session_id, turns in finding_set.sessions.items():
            sessions.setdefault(session_id, turns)
        for session_id, revisions in finding_set.agent_revisions.items():
            agent_revisions.setdefault(session_id, revisions)
    return FindingSet(
        findings=findings, sessions=sessions, agent_revisions=agent_revisions
    )
