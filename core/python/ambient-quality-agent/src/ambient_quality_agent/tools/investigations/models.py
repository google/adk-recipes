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

"""An investigation run and the progress events it emits, as stored.

Two records for two tables, because they stand in different relations to a run:

* `InvestigationRecord` is the run. It is stored as whole snapshots -- one row
  per lifecycle step, each carrying every field as it stood -- so the newest
  snapshot of a run simply is the run.
* `InvestigationEvent` is one progress event. A run has many, they only ever
  accumulate, and the node emitting one holds nothing but the event -- so they
  are rows in a table of their own rather than a field every later snapshot
  would have to copy forward.

Each model's fields *are* its table's columns, one for one:
``terraform/modules/aqa/schemas/{investigations,investigation_events}.json``
declare them, `list_snapshot_columns` / `list_event_columns` read them back off the
models, and `tests/test_investigations_schema.py` holds the two sides level. The
validators below are everything a BigQuery row needs to become a record; no
column table restates any of it.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


def _format_utc_now_iso() -> str:
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


class RunStatus(enum.StrEnum):
    """Lifecycle states of an investigation run.

    ``StrEnum`` to serialize as the bare string the ``status`` column stores.
    """

    SCHEDULED = "scheduled"
    """An observed-agent update arrived and a delayed trigger will start the
    run at ``due_at``. Kept apart from ``PENDING`` because the delay can outlast
    the pending lease, and nothing is running or queued to run yet."""
    PENDING = "pending"
    """Durable job submitted; the work runs in a separate LRO invocation."""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    """A duplicate trigger: its idempotency marker already existed, so the run
    exited before the graph (see `job_execution.execute_investigation`). Also a
    scheduled run whose trigger fired while a sweep was already running."""


TERMINAL_STATUSES = frozenset(
    {RunStatus.DONE, RunStatus.FAILED, RunStatus.SKIPPED}
)
"""Statuses that end a run; reaching one stamps ``finished_at``."""

IN_FLIGHT_STATUSES = frozenset({RunStatus.PENDING, RunStatus.RUNNING})
"""Statuses of a run that has been handed work. A ``SCHEDULED`` run is neither
finished nor in flight: nothing runs for it until its trigger fires."""

MANUAL_TRIGGER_TYPE = "manual"
"""``trigger_type`` of a run a user or the CLI asked for directly."""

CUSTOM_TRIGGER_TYPE = "custom"
"""``trigger_type`` for one-off investigations targeting user-selected conversations."""

NON_AMBIENT_TRIGGER_TYPES = frozenset(
    {MANUAL_TRIGGER_TYPE, CUSTOM_TRIGGER_TYPE}
)
"""Trigger types excluded from ambient scheduling progress tracking.

Excluded from `InvestigationStore.get_last_finished_window_end` and health checks
because they are ad-hoc and specify arbitrary time ranges.
"""


class CustomOverrides(BaseModel):
    """Configuration overrides for custom investigations.

    Stored as a model on the record so ambient runs leave the database column NULL.
    """

    selector_sql: str = ""
    """SQL query selecting specific conversations, replacing the random sample.

    Stored and spliced as written, so whoever supplies it is trusted with the
    telemetry dataset as directly as someone holding a BigQuery console.
    """

    session_review_focus: str = ""
    """Focus guidance appended to the reviewer prompt to narrow findings."""

    def is_custom(self) -> bool:
        """Check whether any custom override is configured.

        Returns:
            True if selector_sql or session_review_focus is set, False otherwise.
        """
        return bool(self.selector_sql or self.session_review_focus)


class InvestigationCounters(BaseModel):
    """How much a run saw at each stage, from telemetry to insights.

    One flat mapping of non-negative integers, accumulated in workflow state as
    the nodes run (`core.state.add_counts`) and stored as the run's ``counters``
    JSON column. Flat and integer-only on purpose: `BigQueryInvestigationStore.sum_counters`
    sums every field of this model across runs in one statement, so a
    counter added here is aggregated and surfaced without touching that query.

    The unit throughout is the *sampling unit* of the scope being evaluated --
    a session for ``MULTI_TURN``, a trace/invocation for ``SINGLE_TURN`` -- and
    both scopes of a run add into the same counters. "Trace" below means that
    unit.
    """

    traces_scanned: int = 0
    """Traces in the run's window, before sampling: what the ingestion query
    would have matched with no ``LIMIT`` (see `BaseFetcher.count_scanned`). The
    denominator the other trace counts are read against."""

    traces_ingested: int = 0
    """Sampled traces turned into a complete evaluation case."""

    traces_ingested_partial: int = 0
    """Sampled traces turned into a case that lost some telemetry on the way --
    an unreadable payload offloaded to GCS, or a trace that contributed no
    conversation turn. Evaluated, but on less than the agent produced."""

    traces_ingested_failed: int = 0
    """Sampled traces dropped before evaluation: unparseable telemetry, or no
    conversation content at all."""

    traces_evaluated: int = 0
    """Evaluation cases sent to the eval service."""

    traces_eval_passed: int = 0
    """Evaluated traces where every metric passed."""

    traces_eval_failed: int = 0
    """Evaluated traces where at least one metric failed (and none errored)."""

    traces_eval_errored: int = 0
    """Evaluated traces where at least one metric errored -- an eval API
    failure, not an agent defect. A trace with neither verdicts nor a score
    counts in none of the three."""

    rubrics_generated: int = 0
    """Rubric verdicts the evaluators produced across every ``(trace, metric)``
    outcome, passed and failed alike."""

    findings_generated: int = 0
    """Total failed findings submitted to clustering across all producers."""

    rubrics_errored: int = 0
    """Failed rubrics that reached no insight because their clustering call
    raised (`ClusteringInfo.errored_rubrics`). Findings the sweep looked for and
    lost, as opposed to findings it did not have."""

    rubrics_unclustered: int = 0
    """Failed rubrics the clustering result left out -- the model omitted them,
    or their cluster was dropped as ungrounded
    (`ClusteringInfo.unclustered_rubrics`)."""

    clusters_created: int = 0
    """Candidate issues the sweep's failed rubrics clustered into; each becomes
    one insight occurrence. Counted before the verification pass judges them, so
    the series means the same thing on either side of the deploy that added that
    pass; the four counters below say what became of them, and stay at zero on a
    sweep whose mode or flag left the pass off."""

    clusters_verified: int = 0
    """Candidates the verification pass confirmed as real defects."""

    clusters_rejected: int = 0
    """Candidates the pass judged no defect at all. Each records its occurrence
    and verdict for audit. Under `config.is_verification_enforced` it mints no
    insight either, so it counts in neither `insights_created` nor
    `insights_recurring`; unenforced it still counts in one of them."""

    clusters_verify_skipped: int = 0
    """Candidates the pass never evaluated -- no trace to read, or beyond the
    calls it spends per sweep. Withheld like a rejection: each records its
    occurrence for audit and counts in neither `insights_created` nor
    `insights_recurring`."""

    clusters_verify_failed: int = 0
    """Candidates whose verification call failed or answered unreadably.
    Withheld for the same reason, and counted apart because a rising number
    here is an outage rather than a quiet sweep."""

    insights_created: int = 0
    """Clusters that matched no existing insight and minted a new one."""

    insights_recurring: int = 0
    """Clusters that matched an insight an earlier sweep recorded and were
    marked recurring against it. An unjudged candidate that matched is none of
    them -- its insight is dated to this sweep, but nothing counts as a
    recurrence of it."""


def list_counter_names() -> tuple[str, ...]:
    """Returns counter field names in `InvestigationCounters` order.

    Returns:
        Tuple of counter field names.
    """
    return tuple(InvestigationCounters.model_fields)


class InvestigationEvent(BaseModel):
    """One progress event a workflow node emitted, from its own row."""

    run_id: str = ""
    """Which run emitted it. Left empty on the copies nested in an
    `InvestigationRecord`, which are already under their run."""

    created_at: str = Field(default_factory=_format_utc_now_iso)
    """When the emitting node wrote it, and the order they are read back in."""

    text: str = ""
    """The markdown snippet the node emitted."""

    source: str | None = None
    """Workflow node that emitted it, when the caller named one."""

    _iso = field_validator("created_at", mode="before")(_to_iso)


class InvestigationRecord(BaseModel):
    """One investigation: the columns of its newest snapshot, plus its events.

    Fields are split into what the scheduler knows up front (inputs) and what
    the run produces (outputs). `events` is the one exception to
    field-per-column: it is read from the events table, and the list view
    leaves it empty.
    """

    # --- Identity -------------------------------------------------------- #

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])

    created_at: str = Field(default_factory=_format_utc_now_iso)
    """When the run was scheduled. Every snapshot of it repeats this."""

    updated_at: str = Field(default_factory=_format_utc_now_iso)
    """When this snapshot was written -- restamped on every append, so a run's
    newest snapshot is the one with the highest ``updated_at``."""

    # --- Inputs, fixed at submit time -------------------------------------- #

    observed_agent_name: str = ""
    trigger_type: str = ""
    """Origin of the run: ``manual`` (direct request), ``custom`` (user-selected
    conversations), or ambient triggers (``scheduled``, ``task_fire``, ``update``)."""

    window_start: str | None = None
    window_end: str | None = None
    budget_per_metric: int = 0
    """Per-metric evaluation budget, derived from the config at submit time."""

    metrics: dict[str, list[str]] = Field(default_factory=dict)
    """Snapshot of the metrics evaluated, keyed by metric type."""

    dry_run: bool = False

    custom_overrides: CustomOverrides | None = None
    """Custom overrides for this run, or None for ambient runs.

    Stored as None so the underlying database column remains NULL when unset.
    """

    idempotency_key: str | None = None
    """Dedup key for an ambient trigger; `execute_investigation` claims a GCS
    marker on it so a redelivered trigger yields one investigation."""

    effective_config: dict[str, Any] = Field(default_factory=dict)
    """Config snapshot taken at submit time; the durable LRO invocation reads
    this to run the graph with the same settings."""

    due_at: str | None = None
    """When the delayed trigger of a ``SCHEDULED`` run is due to start it. None
    for a run that started when it was asked for."""

    # --- Outputs ----------------------------------------------------------- #

    status: RunStatus = RunStatus.PENDING
    job_name: str | None = None
    """Resource name of the durable long-running query job (LRO), if any."""

    finished_at: str | None = None
    error: str | None = None

    summary: dict[str, Any] | None = None
    """Terminal summary of the run, without its ``events`` key: those are rows
    in the events table, and `format_run` puts the two back together."""

    counters: InvestigationCounters = Field(
        default_factory=InvestigationCounters
    )
    """What the run saw at each stage, written once when it reaches a terminal
    status. Zero throughout for a run that never got that far."""

    # --- Not a column ------------------------------------------------------- #

    events: list[InvestigationEvent] = Field(default_factory=list)
    """Progress events in emission order, read from the events table. Empty in
    the list view, which does not fetch them."""

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
"""The one `InvestigationRecord` field the investigations table does not hold."""


def list_snapshot_columns() -> tuple[str, ...]:
    """Returns column names of the investigations table.

    Returns:
        Tuple of column names excluding virtual fields.
    """
    return tuple(
        f for f in InvestigationRecord.model_fields if f != EVENTS_FIELD
    )


def list_event_columns() -> tuple[str, ...]:
    """Returns column names of the investigation_events table.

    Returns:
        Tuple of column names.
    """
    return tuple(InvestigationEvent.model_fields)
