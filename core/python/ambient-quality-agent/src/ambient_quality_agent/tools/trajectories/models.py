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

"""Data models for investigation trajectories, trajectory manifests, and steps.

Two distinct grains are modeled here:
* `InvestigationTrajectory` records that one investigation sampled one
  trajectory (`(investigation_id, trajectory_id)`), along with its ingestion
  status and source trace IDs.
* `TrajectoryManifest` and `TrajectoryStep` store the trajectory itself
  (`(observed_agent_key, trajectory_id)`), shared across investigations.

Persistence, recording, and querying live in `TrajectoryStore`,
`TrajectoryRecorder`, and `TrajectoryReader`.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from pydantic import BaseModel, Field


class IngestStatus(enum.StrEnum):
    """Ingestion outcome for a trajectory sampled by an investigation.

    Subclasses ``StrEnum`` to serialize as the plain string stored in the
    ``ingest_status`` column. Describes ingestion only, independent of
    downstream evaluation or clustering outcomes.
    """

    INGESTED = "ingested"
    """Assembled with complete telemetry."""

    PARTIAL = "partial"
    """Assembled from incomplete telemetry, such as an unreadable GCS payload
    or a trace that yielded no conversation turn."""

    NOT_INGESTED = "not_ingested"
    """Sampled, but yielded no usable conversation content. Recorded so sample
    counts match the investigation's budget and source trace IDs remain
    inspectable."""


class SamplingSignals(BaseModel):
    """The risk properties weighted sampling scored one trajectory on.

    Each value is relative to the investigation's window or selection, so the
    same trajectory can score differently in another investigation. See
    `ambient_quality_agent.tools.ingestion.sampling` for the definitions.
    """

    has_error: bool = False
    """Whether the trajectory recorded an error."""

    has_abnormal_finish: bool = False
    """Whether a model call stopped for a reason other than a normal stop."""

    has_tool_reported_error: bool = False
    """Whether a tool returned a result that reports a failure."""

    busy_turn_calls: int | None = None
    """The most calls to one tool in any one turn (on Cloud Logging, the most
    model calls in any one turn), or None without any call."""

    total_tokens: int | None = None
    """The trajectory's total tokens, or None when no model call recorded its
    usage."""

    slowest_turn_ms: int | None = None
    """Duration of the trajectory's slowest turn in milliseconds, or None when
    the source recorded none."""

    busy_turn_percentile: float | None = None
    """Percentile, from 0 to 1, of `busy_turn_calls` among the candidates, or
    None where it is None."""

    cost_percentile: float | None = None
    """Percentile, from 0 to 1, of `total_tokens` among the candidates, or None
    where it is None."""

    latency_percentile: float | None = None
    """Percentile, from 0 to 1, of `slowest_turn_ms` among the candidates, or
    None where it is None."""

    busy_turn_tail: float = 0.0
    """Tail score, from 0 to 1, of `busy_turn_percentile`: the share of the
    busy-turn coefficient the weight added."""

    cost_tail: float = 0.0
    """Tail score, from 0 to 1, of `cost_percentile`: the share of the cost
    coefficient the weight added."""

    latency_tail: float = 0.0
    """Tail score, from 0 to 1, of `latency_percentile`: the share of the
    latency coefficient the weight added."""


class InvestigationTrajectory(BaseModel):
    """Association recording that an investigation sampled a trajectory.

    Holds ingestion provenance and trace IDs without conversation content. A
    trajectory is the unit evaluated together (a session for multi-turn review,
    or an invocation turn for single-turn scoring) and comprises one or more
    Cloud Trace traces (`source_trace_ids`).

    Append-only, keyed by ``(investigation_id, trajectory_id)``.
    `TrajectoryStore.delete_investigation_trajectories` removes prior rows on
    investigation retry so the pair remains unique within an investigation.

    Maps to the BigQuery ``trajectories`` table, which stores `investigation_id`
    in ``run_id``, `observed_agent_key` in ``agent_name``, and
    `ingestion_source` in ``source``.
    """

    investigation_id: str
    """Investigation that sampled this trajectory; used by retry cleanup to
    delete prior attempts' rows."""

    observed_agent_key: str
    """Key of the observed agent (`Config.resolve_agent_key`), denormalized to
    query trajectories without joining ``investigations``."""

    trajectory_id: str
    """Identifier of the sampled trajectory (a session ID for multi-turn
    evaluation, or an invocation/trace ID for single-turn evaluation). Matches
    `Finding.trajectory_id` and `InsightOccurrence.trajectory_ids`."""

    created_at: dt.datetime
    """Timestamp when the trajectory was recorded; serves as the table's
    partition column."""

    ingestion_source: str | None = None
    """Telemetry source: ``big_query``, ``cloud_ops``, or ``cloud_logging``.
    Stored in BigQuery in the ``source`` column."""

    source_session_id: str | None = None
    """Parent session ID for single-turn trajectories, linking the turn to its
    surrounding conversation."""

    source_trace_ids: list[str] = Field(default_factory=list)
    """Chronologically ordered Cloud Trace IDs comprising the trajectory (one
    per turn for a session, or one for a single turn). Console deep links use
    the first trace ID. Empty for ``big_query`` telemetry, which records no
    trace IDs."""

    ingest_status: IngestStatus
    """Outcome of ingesting this trajectory during the investigation. Required
    so uningestible rows cannot default to ``INGESTED``."""

    sampling_weight: float | None = None
    """Weight weighted sampling gave the trajectory (see
    `ambient_quality_agent.tools.ingestion.sampling`). ``None`` under uniform
    sampling, and on a record without sampling recorded."""

    inclusion_probability: float | None = Field(default=None, gt=0, le=1)
    """Probability that the investigation picked this trajectory, given the
    rest of its sample. A figure for all traffic counts the trajectory as
    ``1 / inclusion_probability`` trajectories. ``None`` on a record without
    sampling recorded."""

    sampling_signals: SamplingSignals | None = None
    """The risk properties behind `sampling_weight`; ``None`` where it is."""


class TrajectoryProcessingState(enum.StrEnum):
    """Processing outcome of a sampled trajectory across ingestion and correlation.

    Derived at read time from `IngestStatus` and whether an `InsightOccurrence`
    from the same investigation references the trajectory, keeping both storage
    tables append-only.

    Subclasses ``StrEnum`` to serialize as the JSON string used by dashboard
    charts and filters.
    """

    NOT_INGESTED = "not_ingested"
    """Failed ingestion and never reached evaluation."""

    NO_INSIGHT = "no_insight"
    """Ingested and evaluated without producing a tracked insight occurrence
    (either passed all checks or had findings that did not form a tracked
    insight)."""

    IN_AN_INSIGHT = "in_an_insight"
    """Referenced by a tracked `InsightOccurrence` from the same
    investigation."""


class InvestigationTrajectoryView(InvestigationTrajectory):
    """Read view of an `InvestigationTrajectory` with its derived `outcome`.

    Console URLs are attached at the tool/serialization layer rather than here
    because they require the observed project ID.
    """

    outcome: TrajectoryProcessingState
    """Processing state derived at read time (`TrajectoryProcessingState`)."""


class DailyOutcome(BaseModel):
    """Count of sampled trajectories for one UTC day and processing outcome.

    A trajectory sampled by multiple investigations on different days is counted
    on each day it was sampled, matching `InsightView.trajectory_count`.
    """

    day: dt.date
    """UTC calendar date of `InvestigationTrajectory.created_at`."""

    outcome: TrajectoryProcessingState
    trajectories: int
    """Sampled trajectories with this outcome on this day."""

    estimated_trajectories: float | None = None
    """Trajectories of all traffic that `trajectories` stands for: the sum of
    ``1 / InvestigationTrajectory.inclusion_probability`` over them, without
    the scaling an investigation's own estimates get. ``None`` when any of
    them was recorded without a probability."""


class TrajectoryStatus(enum.StrEnum):
    """Completeness status of a stored trajectory bundle.

    Unlike `IngestStatus`, which records what one investigation's ingestion made
    of a trajectory, this describes the stored trajectory bundle itself and
    includes `TRUNCATED` when an oversized step or agent config was omitted by
    the write cap.
    """

    INGESTED = "ingested"
    """Complete trajectory with all steps and agent configurations stored."""

    PARTIAL = "partial"
    """Assembled from incomplete source telemetry. Replaced if a later
    investigation ingests a complete copy."""

    TRUNCATED = "truncated"
    """Cut at write time because a step or agent configuration exceeded
    `bundles.MAX_JSON_BYTES`. Distinct from `PARTIAL` because re-ingesting the
    same telemetry will not repair an oversized payload."""


class TrajectoryManifest(BaseModel):
    """Metadata header for a stored trajectory, without its conversation steps.

    Keyed by ``(observed_agent_key, trajectory_id)`` without `investigation_id`,
    so a trajectory sampled by multiple investigations is stored once. Kept
    separate from `TrajectoryStep` rows so trajectory-level metadata (`agents`,
    `agent_revision`, `status`, `step_count`) is stored once per trajectory.

    Maps to the BigQuery ``trajectory_payloads`` table, which stores
    `observed_agent_key` in ``agent_name`` and `step_count` in ``turn_count``.
    """

    observed_agent_key: str
    """Key of the observed agent; first part of the manifest key."""

    trajectory_id: str
    """Trajectory identifier; second part of the manifest key, matching
    `InvestigationTrajectory.trajectory_id` and
    `InsightOccurrence.trajectory_ids`."""

    created_at: dt.datetime
    """Timestamp when this copy was stored; serves as the partition column and
    ties the manifest to its `TrajectoryStep` rows."""

    status: TrajectoryStatus
    """Completeness status of this stored copy."""

    step_count: int = Field(ge=0)
    """Number of `TrajectoryStep` rows belonging to this manifest, allowing
    readers to detect expired or missing steps."""

    agents: dict[str, Any] | None = None
    """Static agent configurations (`AgentData.agents`, including system
    instructions and tool declarations) stored as untyped JSON dicts so stored
    manifests remain readable across SDK schema changes."""

    agent_revision: str | None = None
    """Deployment revision of the agent that served the trajectory (the latest
    revision if a multi-turn conversation spanned a redeploy, or ``None`` when
    the telemetry source reports no revision)."""


class TrajectoryStep(BaseModel):
    """One stored step of a trajectory (`ConversationTurn`).

    Keyed by ``(observed_agent_key, trajectory_id, step_index)``. Splitting a
    trajectory into per-step rows keeps each row within database size limits.

    Maps to the BigQuery ``trajectory_payload_turns`` table, which stores
    `observed_agent_key` in ``agent_name``, `step_index` in ``turn_index``, and
    `step` in ``turn``.
    """

    observed_agent_key: str
    """Key of the observed agent; first part of the step key."""

    trajectory_id: str
    """Trajectory this step belongs to; second part of the step key."""

    step_index: int = Field(ge=0)
    """Zero-based step position within the trajectory; third part of the step
    key and the sort order for reassembling the trajectory."""

    created_at: dt.datetime
    """Timestamp when this step was stored, matching its
    `TrajectoryManifest.created_at` so both expire together."""

    step: dict[str, Any]
    """Serialized ``ConversationTurn`` stored as an untyped JSON dict."""
