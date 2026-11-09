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

"""Data models for insight correlation, occurrences, and root-cause diagnoses.

Each investigation produces sample findings (`FindingExample`, wrapping a
`Finding`) and correlates them into durable `Insight` and `InsightOccurrence`
records.

These models hold state without persistence logic, which lives in
`InsightStore` and `InsightReader`. They subclass `BaseModel` to serialize
cleanly across workflow state and API boundaries.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from ambient_quality_agent.tools.insights.findings import Finding
from pydantic import AliasChoices, BaseModel, Field


class InsightStatus(enum.StrEnum):
    """Lifecycle states of a durable insight.

    Subclasses ``StrEnum`` to serialize as the plain string stored in the
    ``status`` column.
    """

    NEW = "NEW"
    RECURRING = "RECURRING"

    RESOLVED = "RESOLVED"
    """Terminal state. Matching queries skip resolved insights, so a later
    recurrence creates a new insight rather than reopening a resolved one."""


class OccurrenceState(enum.StrEnum):
    """Whether an occurrence is a sighting of a tracked insight, or why it was withheld.

    Subclasses ``StrEnum`` to serialize as the plain string stored in the
    ``occurrence_state`` column. Unlike `InsightStatus`, which tracks a durable
    insight's lifecycle across investigations, this describes a single
    investigation's candidate cluster.

    Distinguishes rejected candidates from unjudged ones directly on the row,
    since both leave `InsightOccurrence.insight_id` as ``None``.
    """

    TRACKED = "TRACKED"
    """Sighting of the insight named by `InsightOccurrence.insight_id`. When
    the verification pass ran, it did not reject the candidate."""

    REJECTED = "REJECTED"
    """Judged not to be a defect by the verification pass (`analyses`), so it
    names no insight."""

    UNJUDGED = "UNJUDGED"
    """Skipped or failed during the verification pass, so no verdict confirmed
    it and it names no insight."""


class FindingExample(BaseModel):
    """A sample `Finding` paired with the conversation turns where it occurred.

    Provides both the failure description and the conversation context for
    verification, root-cause analysis, and the dashboard. Stored as JSON on
    `InsightOccurrence.examples` (in the BigQuery ``rubrics`` column).
    """

    finding: Finding = Field(
        validation_alias=AliasChoices("finding", "rubric"),
        serialization_alias="rubric",
    )
    """The finding. Stored in BigQuery under the key ``rubric``."""

    trajectory_id: str = Field(
        default="",
        validation_alias=AliasChoices("trajectory_id", "eval_case_id"),
        serialization_alias="eval_case_id",
    )
    """Trajectory where the finding occurred (`Finding.trajectory_id`). Stored
    in BigQuery under the key ``eval_case_id``."""

    turns: list[dict] = Field(
        default_factory=list,
        validation_alias=AliasChoices("turns", "trace"),
        serialization_alias="trace",
    )
    """Conversation turns as JSON-serializable dicts. Empty when the
    trajectory's turns were unavailable. Stored in BigQuery under ``trace``."""

    examined: bool = False
    """True when the verification pass inspected this example's trajectory."""


class Insight(BaseModel):
    """A durable, deduplicated quality issue across investigations.

    Maps to the BigQuery ``insights`` table, which stores `observed_agent_key`
    in the ``agent_name`` column (`FIELD_TO_COLUMN`).
    """

    insight_id: str
    observed_agent_key: str
    """Key of the observed agent (`Config.resolve_agent_key`); insights are
    never matched or queried across agents."""

    agent_id: str = ""
    """Root agent or sub-agent the defect is attributed to (`Cluster.agent_id`).
    Candidate matching compares only insights attributed to the same agent.
    Empty on unattributed rows, which default to `observed_agent_key`."""

    label: str

    status: InsightStatus
    created_at: dt.datetime
    """Timestamp of the first sighting."""

    updated_at: dt.datetime
    """Timestamp of the most recent sighting; used as the cutoff for
    auto-resolution."""

    resolved_at: dt.datetime | None = None
    """Timestamp when auto-resolution marked the insight resolved, or ``None``
    while open."""

    dismissed_at: dt.datetime | None = None
    """Timestamp when an operator dismissed the insight, or ``None`` if active.

    Tracked separately from `status` so dismissed insights remain eligible for
    candidate matching during correlation: a recurrence attaches to the
    dismissed insight and stays hidden from list views rather than minting a
    new visible insight."""

    merged_into_insight_id: str | None = None
    """Target insight ID when an operator marked this insight as a duplicate,
    or ``None`` if canonical.

    Always references a row whose own `merged_into_insight_id` is ``None``.
    `InsightStore.merge_insight` flattens transitive merges on write so read
    queries can resolve ownership with a single ``COALESCE``."""

    merged_at: dt.datetime | None = None
    """Timestamp of the most recent merge into `merged_into_insight_id`, or
    ``None`` if not merged."""


VERIFICATION_ANALYSIS_KEY = "verification"
"""Key in `InsightOccurrence.analyses` for the cluster verification result.

Shared by the verification pass and the dashboard server (`ui/app.py`), which
projects the verification explanation as `diagnosis`. Because the UI container
ships without the agent package, `ui/app.py` defines its own copy and
`tests/test_ui_investigations.py` verifies they match.
"""


class ClusterVerification(BaseModel):
    """Verification verdict for a candidate cluster, stored in `InsightOccurrence.analyses`."""

    model: str = ""
    """Name of the model that produced the verdict."""

    valid: bool = True
    """Whether the trajectory evidence supports the defect claim as one coherent
    issue. When `config.is_verification_enforced` is true, a false verdict
    withholds the candidate from creating or matching an insight."""

    explanation: str = ""
    """Symptom summary explaining what went wrong and why."""

    refined_label: str = ""
    """Replacement label proposed by the verification model, or empty if the
    cluster's original `label` was kept."""

    created_at: dt.datetime | None = None
    """Timestamp when the verification completed."""


class InsightOccurrence(BaseModel):
    """One investigation's sighting of a candidate issue cluster.

    Append-only: occurrences are never updated in place, so ordering them by
    `created_at` reconstructs an insight's sighting history. Maps to the
    BigQuery ``insight_occurrences`` table, which stores `investigation_id` in
    ``run_id``, `observed_agent_key` in ``agent_name``, `finding_count` in
    ``item_count``, `trajectory_count` in ``trace_count``, and `examples` in
    ``rubrics`` (`FIELD_TO_COLUMN`).
    """

    occurrence_id: str

    insight_id: str | None
    """Insight this occurrence belongs to, or ``None`` when withheld
    (`occurrence_state` is ``REJECTED`` or ``UNJUDGED``). Unlinked occurrences
    are retained for audit and excluded from `InsightReader` queries."""

    occurrence_state: OccurrenceState
    """Whether this occurrence is tracked under an insight, rejected, or
    unjudged. Required so callers must explicitly state why `insight_id` is set
    or ``None``."""

    investigation_id: str
    """Investigation that produced this occurrence; used by retry cleanup to
    delete prior attempts' occurrences."""

    created_at: dt.datetime
    observed_agent_key: str
    """Key of the observed agent, denormalized to filter occurrences without
    joining ``insights``."""

    agent_revision: str = ""
    """Deployment revision attributed to this sighting (the latest revision
    when findings span multiple revisions, or empty when the telemetry source
    reports no revision)."""

    analysis_mode: str = ""
    """Quality analysis mode that produced this sighting
    (``quality_analysis_mode``), denormalized for filtering without joining
    ``investigations``."""

    label: str
    """Cluster label produced during clustering."""

    finding_count: int = 0
    """Total findings in the cluster. Stored in BigQuery in ``item_count``."""

    trajectory_count: int = 0
    """Distinct trajectories covered by all findings in the cluster (not capped
    to `examples`). Stored in BigQuery in ``trace_count``."""

    trajectory_ids: list[str] = Field(default_factory=list)
    """All distinct trajectory IDs in the cluster, matching
    `InvestigationTrajectory.trajectory_id` so readers can resolve source traces
    and full trajectory bundles."""

    analyses: dict[str, ClusterVerification] = Field(default_factory=dict)
    """Analysis results keyed by step name (e.g. `VERIFICATION_ANALYSIS_KEY`).
    Empty when no analysis step ran on the cluster."""

    examples: list[FindingExample] = Field(default_factory=list)
    """Sampled findings with conversation turns, capped at `MAX_EXAMPLES`.
    Stored in BigQuery in the ``rubrics`` column."""


# Subclasses Insight to inherit core fields while adding read-view aggregation metrics.
class InsightView(Insight):
    """Read view of an `Insight` with aggregated occurrence and root-cause stats."""

    occurrence_count: int = 0
    """Total confirmed occurrences attributed to this insight."""

    trajectory_count: int = 0
    """Sum of `InsightOccurrence.trajectory_count` across the insight's
    occurrences. A trajectory seen in multiple investigations is counted in
    each."""

    last_investigation_id: str | None = None
    """`investigation_id` of the most recent occurrence."""

    last_investigation_at: dt.datetime | None = None
    """`created_at` of the most recent occurrence, or ``None`` if it has no
    occurrences."""

    has_root_cause: bool = False
    """Whether any `RootCause` record exists for this insight, computed at read
    time for list views."""


class ProposedEdit(BaseModel):
    """Proposed source file replacement for the observed agent.

    Serves as an ADK tool-argument model where field descriptions become
    model-facing documentation in the generated schema."""

    path: str = Field(
        description="Repository-relative path, exactly as `list_source_files` names it."
    )
    start_line: int = Field(
        description="First line to replace, 1-based and inclusive."
    )
    end_line: int = Field(
        description="Last line to replace, 1-based and inclusive."
    )
    after: str = Field(
        description=(
            "Replacement text for start_line..end_line, without line numbers."
        )
    )
    rationale: str = Field(default="", description="One line: why this edit.")
    before: str = Field(
        default="",
        description=(
            "Leave empty. The server fills this with the code at "
            "start_line..end_line as it exists at the pinned revision; anything "
            "supplied here is discarded."
        ),
    )


class RootCause(BaseModel):
    """Diagnosis of an insight occurrence with proposed code changes.

    Mirrors the BigQuery table ``insight_root_causes``.

    Agent name and citations are omitted because deployment snapshots span the
    entire agent hierarchy (edits can target sub-agents while occurrences
    attribute to the root agent), and edit paths with line ranges provide the
    citations."""

    root_cause_id: str
    """UUID4 hex identifier minted per diagnosis."""

    insight_id: str
    occurrence_id: str
    """Insight occurrence ID this diagnosis was written against."""

    agent_revision: str
    """Agent snapshot revision used to resolve edit line ranges."""

    summary: str
    """Concise summary of the root cause mechanism."""

    edits: list[ProposedEdit] = Field(default_factory=list)
    """Proposed code replacements, or empty if no code changes are required."""

    created_at: dt.datetime


def mint_id() -> str:
    """Mint a fresh id for an insight or occurrence.

    Returns:
        A unique UUID4 hex string.
    """
    return uuid.uuid4().hex


FIELD_TO_COLUMN = {
    "investigation_id": "run_id",
    "observed_agent_key": "agent_name",
    "trajectory_count": "trace_count",
    "finding_count": "item_count",
    "examples": "rubrics",
}
"""The fields whose BigQuery column has a different name, mapped to the column.

BigQuery cannot rename a column of a deployed table, so the BigQuery store and
reader name these columns explicitly, and the schema check reads the tables
through this map."""


def field_to_column(field: str) -> str:
    """Returns the BigQuery column that holds an insight model field.

    Args:
        field: Field name of `Insight`, `InsightOccurrence` or `RootCause`.

    Returns:
        The column name, which is the field name unless `FIELD_TO_COLUMN`
        maps it.
    """
    return FIELD_TO_COLUMN.get(field, field)


def list_insight_columns() -> tuple[str, ...]:
    """Returns column names of the insights table.

    Returns:
        Tuple of the columns `Insight` is stored in.
    """
    return tuple(field_to_column(f) for f in Insight.model_fields)


def list_occurrence_columns() -> tuple[str, ...]:
    """Returns column names of the insight_occurrences table.

    Returns:
        Tuple of the columns `InsightOccurrence` is stored in.
    """
    return tuple(field_to_column(f) for f in InsightOccurrence.model_fields)
