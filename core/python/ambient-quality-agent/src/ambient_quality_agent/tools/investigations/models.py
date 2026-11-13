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

"""Stored investigation snapshots and progress events.

`Investigation` and `InvestigationEvent` map to separate tables because they
have different write patterns:

* `Investigation` represents an investigation's full state. Each lifecycle
  transition appends a complete snapshot row, so the row with the latest
  ``updated_at`` is the current state.
* `InvestigationEvent` represents a single progress message emitted by a
  workflow node. Events are stored in a separate table so emitting nodes do not
  need to read and copy prior events into every investigation snapshot.

Model fields map to table columns by name unless mapped in `FIELD_TO_COLUMN`.
`list_snapshot_columns` and `list_event_columns` derive the column lists for
the ``investigations`` and ``investigation_events`` tables, and
`tests/test_investigations_schema.py` verifies that the models match the
Terraform schemas.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import uuid
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    field_validator,
)

logger = logging.getLogger(__name__)


def _format_utc_now_iso() -> str:
    """Returns the current UTC time as an ISO 8601 string.

    Returns:
        Current UTC timestamp formatted as an ISO 8601 string.
    """
    return dt.datetime.now(tz=dt.UTC).isoformat()


def _to_iso(value: Any) -> Any:
    """Converts a datetime value to an ISO 8601 string.

    Args:
        value: Datetime instance, string, or arbitrary value.

    Returns:
        ISO 8601 string if value is a datetime, otherwise original value.
    """
    return value.isoformat() if isinstance(value, dt.datetime) else value


def _to_json_obj(value: Any) -> Any:
    """Decodes JSON column values into Python objects.

    Decodes up to two levels to handle double-encoded string columns from BigQuery.
    Returns None on parsing error to allow fields to fall back to defaults.

    Args:
        value: Raw column value.

    Returns:
        Parsed Python object, or None if decoding fails.
    """
    for _ in range(2):
        if not isinstance(value, str):
            return value
        try:
            value = json.loads(value)
        except ValueError as exc:
            logger.warning(
                "investigations: unparseable stored JSON column: %s", exc
            )
            return None
    return value if not isinstance(value, str) else None


class InvestigationStatus(enum.StrEnum):
    """Lifecycle states of an investigation.

    Subclasses ``StrEnum`` to serialize as the plain string stored in the
    ``status`` column.
    """

    SCHEDULED = "scheduled"
    """Waiting for a delayed trigger at ``due_at`` after an observed-agent
    update. Kept separate from ``PENDING`` because the trigger delay can exceed
    the pending lease, and no job is queued or running while waiting."""
    PENDING = "pending"
    """Durable job submitted to run in a separate long-running invocation."""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Exited before running the workflow because its idempotency marker was
    already claimed (`job_execution.execute_investigation`), or because its
    delayed trigger fired while another investigation was already running."""


TERMINAL_STATUSES = frozenset(
    {
        InvestigationStatus.DONE,
        InvestigationStatus.FAILED,
        InvestigationStatus.SKIPPED,
    }
)
"""Statuses that end an investigation and stamp ``finished_at``."""

IN_FLIGHT_STATUSES = frozenset(
    {InvestigationStatus.PENDING, InvestigationStatus.RUNNING}
)
"""Statuses of an investigation with active or queued work. Excludes
``SCHEDULED`` because nothing runs until its delayed trigger fires."""

MANUAL_TRIGGER_TYPE = "manual"
"""``trigger_type`` for an investigation requested by a user or the CLI."""

CUSTOM_TRIGGER_TYPE = "custom"
"""``trigger_type`` for a one-off investigation with a custom selector or
review focus."""

NON_AMBIENT_TRIGGER_TYPES = frozenset(
    {MANUAL_TRIGGER_TYPE, CUSTOM_TRIGGER_TYPE}
)
"""Trigger types excluded from ambient watermark and health calculations.

Excluded from `InvestigationStore.get_last_finished_window_end` and health
checks because manual and custom investigations can target arbitrary time
windows.
"""


class CustomOverrides(BaseModel):
    """Configuration overrides for a custom investigation.

    Kept in an optional model so ambient investigations leave the
    ``custom_overrides`` column NULL.
    """

    selector_sql: str = ""
    """SQL subquery selecting target conversations for review.

    Spliced into the ingestion query after lexical and dry-run validation.
    """

    review_focus: str = Field(
        default="",
        validation_alias=AliasChoices("review_focus", "session_review_focus"),
        serialization_alias="session_review_focus",
    )
    """Focus guidance appended to the reviewer prompt. Stored in BigQuery under
    ``session_review_focus``."""

    def is_custom(self) -> bool:
        """Checks whether any custom override is set.

        Returns:
            True if `selector_sql` or `review_focus` is non-empty.
        """
        return bool(self.selector_sql or self.review_focus)


class InvestigationCounters(BaseModel):
    """Per-stage counts recorded by an investigation, from ingestion to insights.

    Accumulated in workflow state via `core.state.add_counts` and stored in the
    ``counters`` JSON column. Uses a flat mapping of non-negative integers so
    `BigQueryInvestigationStore.sum_counters` can sum every field across
    investigations in one SQL query.

    Each trajectory counter counts the evaluation unit of its scope (a session
    for ``MULTI_TURN``, an invocation trace for ``SINGLE_TURN``), summed across
    both scopes when both run.

    In BigQuery, the ``trajectories_*`` fields are stored under ``traces_*``
    keys, and ``findings_errored`` and ``findings_unclustered`` under
    ``rubrics_errored`` and ``rubrics_unclustered``; `counter_to_key` returns
    each field's stored key.
    """

    trajectories_scanned: int = Field(
        default=0,
        validation_alias=AliasChoices("trajectories_scanned", "traces_scanned"),
        serialization_alias="traces_scanned",
    )
    """Trajectories matching the investigation's window before sampling
    (`BaseFetcher.count_scanned`). Serves as the denominator for sampling and
    ingestion rates."""

    trajectories_ingested: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_ingested", "traces_ingested"
        ),
        serialization_alias="traces_ingested",
    )
    """Sampled trajectories assembled with complete telemetry."""

    trajectories_ingested_partial: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_ingested_partial", "traces_ingested_partial"
        ),
        serialization_alias="traces_ingested_partial",
    )
    """Sampled trajectories assembled with missing telemetry, such as an
    unreadable GCS payload or a trace with no conversation turn."""

    trajectories_ingested_failed: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_ingested_failed", "traces_ingested_failed"
        ),
        serialization_alias="traces_ingested_failed",
    )
    """Sampled trajectories dropped before evaluation because their telemetry
    could not be parsed or had no conversation content."""

    trajectories_evaluated: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_evaluated", "traces_evaluated"
        ),
        serialization_alias="traces_evaluated",
    )
    """Ingested trajectories submitted for evaluation."""

    trajectories_eval_passed: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_eval_passed", "traces_eval_passed"
        ),
        serialization_alias="traces_eval_passed",
    )
    """Evaluated trajectories where every metric passed."""

    trajectories_eval_failed: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_eval_failed", "traces_eval_failed"
        ),
        serialization_alias="traces_eval_failed",
    )
    """Evaluated trajectories where at least one metric failed and none
    errored."""

    trajectories_eval_errored: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "trajectories_eval_errored", "traces_eval_errored"
        ),
        serialization_alias="traces_eval_errored",
    )
    """Evaluated trajectories where at least one metric call errored. A
    trajectory that produced neither verdicts nor a score is not counted in
    passed, failed, or errored."""

    rubrics_generated: int = 0
    """Total rubric verdicts produced across all ``(trajectory, metric)``
    evaluations, both passed and failed."""

    findings_generated: int = 0
    """Findings submitted to clustering across all evaluations."""

    findings_errored: int = Field(
        default=0,
        validation_alias=AliasChoices("findings_errored", "rubrics_errored"),
        serialization_alias="rubrics_errored",
    )
    """Findings dropped because their clustering chunk call failed
    (`ClusteringInfo.errored_findings`)."""

    findings_unclustered: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "findings_unclustered", "rubrics_unclustered"
        ),
        serialization_alias="rubrics_unclustered",
    )
    """Findings omitted by the clustering model or belonging to clusters
    dropped during validation (`ClusteringInfo.unclustered_findings`)."""

    clusters_created: int = 0
    """Candidate clusters produced from the investigation's findings before
    verification; each becomes one `InsightOccurrence`."""

    clusters_verified: int = 0
    """Candidate clusters confirmed as defects by the verification pass."""

    clusters_rejected: int = 0
    """Candidate clusters judged not to be defects by the verification pass.
    When `config.is_verification_enforced` is true, rejected candidates do not
    create or match insights."""

    clusters_verify_skipped: int = 0
    """Candidate clusters skipped by verification because they lacked trajectory
    turns or exceeded `MAX_VERIFIED_CLUSTERS`. Withheld from creating or
    matching insights."""

    clusters_verify_failed: int = 0
    """Candidate clusters whose verification call failed or returned invalid
    JSON. Withheld from creating or matching insights and tracked separately to
    surface model outages."""

    insights_created: int = 0
    """Tracked candidates (not rejected or left unjudged by the verification
    pass) that did not match an existing insight and created a new one."""

    insights_recurring: int = 0
    """Tracked candidates (not rejected or left unjudged by the verification
    pass) that matched an existing insight and marked it recurring. Unjudged
    candidates that match an insight update its timestamp without counting as
    recurrences."""


def list_counter_names() -> tuple[str, ...]:
    """Returns counter field names in `InvestigationCounters` definition order.

    Returns:
        Tuple of counter field names.
    """
    return tuple(InvestigationCounters.model_fields)


def counter_to_key(name: str) -> str:
    """Returns the JSON key that stores a counter in the ``counters`` column.

    Args:
        name: Counter field name on `InvestigationCounters`.

    Returns:
        The field's serialization alias, or `name` if it has no alias.
    """
    return InvestigationCounters.model_fields[name].serialization_alias or name


STAGE_EVENT_SOURCE = "stage"
"""`InvestigationEvent.source` of a row that says how far a running
investigation got: a `StageProgress`, as JSON. Only the newest one matters, so
readers that list an investigation's events for what it did leave these out."""


class StageProgress(BaseModel):
    """How far a running investigation got, as one stage row records it.

    Each count belongs to the stage that has it and is None for the others: a
    sweep counts trajectories against a total and the findings it filed,
    clustering the findings it groups, verification the clusters it checks.
    """

    stage: str
    """The stage the investigation is in, such as "Session review"."""

    evaluated: int | None = None
    """Trajectories the sweep has judged so far."""

    total: int | None = None
    """Most trajectories the sweep will read."""

    findings: int | None = None
    """Findings filed so far, or being clustered."""

    clusters: int | None = None
    """Clusters being verified."""


class InvestigationEvent(BaseModel):
    """One progress event emitted by a workflow node."""

    investigation_id: str = ""
    """Investigation that emitted the event. Empty when nested inside
    `Investigation.events`."""

    created_at: str = Field(default_factory=_format_utc_now_iso)
    """UTC ISO 8601 timestamp when the event was recorded; defines read order."""

    text: str = ""
    """Markdown progress message emitted by the node; for a stage row, a
    `StageProgress` in JSON."""

    source: str | None = None
    """Name of the workflow node that emitted the event, if specified, or
    `STAGE_EVENT_SOURCE` for a row saying how far a running investigation
    got."""

    _iso = field_validator("created_at", mode="before")(_to_iso)


class Investigation(BaseModel):
    """One investigation's latest snapshot and its progress events.

    Fields are grouped into submit-time inputs and execution outputs. `events`
    is populated from the ``investigation_events`` table on single-investigation
    reads and left empty in list reads.
    """

    # --- Identity -------------------------------------------------------- #

    investigation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    """Unique identifier across all agents. Update-scheduled investigations
    derive a deterministic ID from the trigger's dedup ID
    (`handler.compute_scheduled_investigation_id`)."""

    created_at: str = Field(default_factory=_format_utc_now_iso)
    """UTC ISO 8601 timestamp when the investigation was first recorded;
    preserved across all subsequent snapshots."""

    updated_at: str = Field(default_factory=_format_utc_now_iso)
    """UTC ISO 8601 timestamp when this snapshot was written; orders snapshots
    so the latest one wins."""

    # --- Inputs, fixed at submit time -------------------------------------- #

    observed_agent_key: str = ""
    """Key of the observed agent (`Config.resolve_agent_key`), which scopes
    investigation lists, counter totals, and concurrency locks."""

    trigger_type: str = ""
    """Trigger source: ``manual``, ``custom``, or an ambient trigger
    (``scheduled``, ``task_fire``, ``update``)."""

    window_start: str | None = None
    window_end: str | None = None
    budget_per_metric: int = 0
    """Maximum trajectories to sample per metric, derived at submit time."""

    metrics: dict[str, list[str]] = Field(default_factory=dict)
    """Configured metrics by scope (``multi_turn``, ``single_turn``)."""

    dry_run: bool = False

    custom_overrides: CustomOverrides | None = None
    """Selector and review-focus overrides for custom investigations, or None
    for ambient and manual investigations."""

    idempotency_key: str | None = None
    """Deduplication key for ambient triggers; `execute_investigation` claims a
    GCS marker for this key so redelivered triggers do not re-run the workflow."""

    effective_config: dict[str, Any] = Field(default_factory=dict)
    """Serialized `Config` snapshot captured at submit time for the durable job."""

    due_at: str | None = None
    """UTC ISO 8601 timestamp when a ``SCHEDULED`` investigation's delayed
    trigger is due to fire, or None if started immediately."""

    # --- Outputs ----------------------------------------------------------- #

    status: InvestigationStatus = InvestigationStatus.PENDING
    job_name: str | None = None
    """Resource name of the durable Agent Runtime query job, if submitted."""

    finished_at: str | None = None
    error: str | None = None

    summary: dict[str, Any] | None = None
    """Terminal execution summary excluding ``events``, which are stored in the
    events table and reattached by `format_investigation`."""

    counters: InvestigationCounters = Field(
        default_factory=InvestigationCounters
    )
    """Stage counters recorded when the investigation finishes; all zeros while
    in flight or if failed before completion."""

    # --- Not a column ------------------------------------------------------- #

    events: list[InvestigationEvent] = Field(default_factory=list)
    """Progress events in emission order from ``investigation_events``; empty in
    list queries."""

    _iso = field_validator(
        "created_at",
        "updated_at",
        "window_start",
        "window_end",
        "due_at",
        "finished_at",
        mode="before",
    )(_to_iso)
    _json = field_validator(
        "metrics",
        "effective_config",
        "summary",
        "custom_overrides",
        mode="before",
    )(_to_json_obj)

    @field_validator("counters", mode="before")
    @classmethod
    def _decode_counters(cls, value: Any) -> Any:
        """Decodes stored JSON counters into an InvestigationCounters model.

        Unparseable or absent payloads return an empty InvestigationCounters instance
        with all counters zeroed.

        Args:
            value: Raw counters field payload.

        Returns:
            Decoded payload or initialized default counters.
        """
        decoded = _to_json_obj(value)
        return InvestigationCounters() if decoded is None else decoded


EVENTS_FIELD = "events"
"""The one `Investigation` field the investigations table does not hold."""


FIELD_TO_COLUMN = {
    "investigation_id": "run_id",
    "observed_agent_key": "observed_agent_name",
}
"""The fields whose BigQuery column has a different name, mapped to the column.

BigQuery cannot rename a column of a deployed table, so the BigQuery store
translates between the field names and the column names of the investigations
and investigation_events tables."""


def field_to_column(field: str) -> str:
    """Returns the BigQuery column that holds a model field.

    Args:
        field: Field name of `Investigation` or `InvestigationEvent`.

    Returns:
        The column name, which is the field name unless `FIELD_TO_COLUMN`
        maps it.
    """
    return FIELD_TO_COLUMN.get(field, field)


def list_snapshot_fields() -> tuple[str, ...]:
    """Returns the `Investigation` fields the investigations table holds.

    Returns:
        Tuple of field names excluding virtual fields.
    """
    return tuple(f for f in Investigation.model_fields if f != EVENTS_FIELD)


def list_event_fields() -> tuple[str, ...]:
    """Returns the `InvestigationEvent` fields.

    Returns:
        Tuple of field names.
    """
    return tuple(InvestigationEvent.model_fields)


def list_snapshot_columns() -> tuple[str, ...]:
    """Returns column names of the investigations table.

    Returns:
        Tuple of column names excluding virtual fields.
    """
    return tuple(field_to_column(f) for f in list_snapshot_fields())


def list_event_columns() -> tuple[str, ...]:
    """Returns column names of the investigation_events table.

    Returns:
        Tuple of column names.
    """
    return tuple(field_to_column(f) for f in list_event_fields())
