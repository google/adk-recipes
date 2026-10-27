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

"""The trajectory store's vocabulary: what ingestion made of one sampled
trajectory, the conversation itself, and what a reader makes of either.

Plain records, no behaviour -- writing is `TrajectoryStore`'s job, assembling is
`TrajectoryRecorder`'s and reading is `TrajectoryReader`'s.

Two grains live here, and telling them apart is the whole of the design.
`Trajectory` is one **sweep's ingestion** of a conversation, so the same
session ingested partial on Monday and whole on Friday is two rows.
`TrajectoryPayload` and `TrajectoryPayloadTurn` are the **conversation**, so it
is stored once however many sweeps sampled it.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from pydantic import BaseModel, Field


class IngestStatus(enum.StrEnum):
    """What ingestion made of one sampled trajectory. ``StrEnum`` to serialize
    as the bare string the BigQuery ``ingest_status`` column stores.

    Three states in one column rather than a nullable bool, because the third
    -- sampled and unusable -- is the one a reader most needs and the one a
    ``partial IS NULL`` encoding hid. Ingestion outcomes only: nothing here
    says what an analysis later concluded.
    """

    INGESTED = "ingested"
    """Assembled whole."""

    PARTIAL = "partial"
    """Built from incomplete telemetry -- an unresolved payload, a trace that
    yielded no turn. Usable, but weaker as evidence, and worth telling apart
    from one that arrived intact."""

    NOT_INGESTED = "not_ingested"
    """Sampled, and nothing usable came out of it: unparseable telemetry, or no
    conversation content at all. Still recorded, because it is what explains a
    sample smaller than the run's budget, and because its trace ids are the
    only handle anyone has on what went wrong."""


class Trajectory(BaseModel):
    """One trajectory a sweep sampled, as ingestion left it.

    A trajectory is what the sweep judged as one thing: a whole session where
    it reviewed the conversation, a single turn where it scored that. It is
    made of one or more **traces** -- one per turn -- which is why
    `source_trace_ids` is a list rather than a single id.

    Append-only, one row per ``(run_id, trajectory_id)``. That pair is unique
    because every ingestion query selects its unit with a ``GROUP BY``, and
    because the two scopes key their units differently -- a session id for a
    reviewed conversation, an invocation/trace id for a scored turn -- so they
    cannot collide inside one run. It is a natural key rather than an enforced
    one, and `TrajectoryStore.delete_run_trajectories` is what keeps it true across retries
    of the same run.

    **The row describes ingestion, not analysis.** It is the state at the end
    of the fetch step, before anything has judged the conversation: what was
    sampled, where it came from, and how much of it arrived. No outcome, no
    score, no rubric text. A sweep runs one analysis today and is expected to
    run several, so an outcome column here would have to mean "the outcome of
    whichever analysis happened to write last" -- and the per-analysis result,
    if it is ever stored per trajectory, belongs in a table of its own keyed
    back to this one. The run counters remain the aggregate.

    Mirrors ``terraform/modules/aqa/schemas/trajectories.json``, which is the
    only definition of that table. Change one and change the other.
    """

    run_id: str
    """The sweep that judged it, and the key its retry cleanup deletes by."""

    agent_name: str
    """Observed agent, denormalized so trajectories can be read without a join."""

    trajectory_id: str
    """The conversation this row is about: a session id where a whole session
    was judged, an invocation/trace id where one turn was. The same value the
    findings and their occurrences carry, which is what lets an insight resolve
    back to the trajectories behind it.

    Named for the conversation rather than for the evaluator that consumed it,
    because what is stored here outlives any one use of it: a console link, a
    chart, a future re-scoring all want the conversation, not an Eval-service
    input."""

    created_at: dt.datetime
    """When the sweep recorded it; the table's partition column."""

    source: str | None = None
    """Telemetry source it came from: ``big_query``, ``cloud_ops`` or
    ``cloud_logging``."""

    source_session_id: str | None = None
    """The session a single-turn trajectory belongs to, so a reader can walk
    from one turn to the conversation around it."""

    source_trace_ids: list[str] = Field(default_factory=list)
    """The Cloud Trace ids this trajectory is made of, **in chronological
    order**: one per turn for a whole session, one in total for a single turn.
    A console link uses the first, which the ingestion queries order to be the
    conversation's opening turn. Empty for ``big_query``, whose analytics
    schema carries no trace ids -- a property of that source, not a gap here."""

    ingest_status: IngestStatus
    """What ingestion made of it. Required and undefaulted: every row knows the
    answer at the moment it is written, and a default would let a trajectory
    nothing could be made of be recorded as one that arrived fine."""


class TrajectoryProcessingState(enum.StrEnum):
    """How far a sampled trajectory got, as far as the store can tell: what
    ingestion made of it, and whether an analysis turned it into an insight.

    Not a column: it is derived at read time from `IngestStatus` and from
    whether an insight occurrence names the trajectory. Deriving it keeps both
    tables append-only -- nothing is written back after correlation runs, and a
    deleted insight or a cleaned-up run cannot leave a stale flag behind.

    ``StrEnum`` because the value crosses to the dashboard as JSON and is what
    the chart keys its series on.
    """

    NOT_INGESTED = "not_ingested"
    """Sampled and unusable, so no analysis ever saw it. This is the bar that
    explains a sample smaller than the run's budget."""

    NO_INSIGHT = "no_insight"
    """Evaluated, and left no insight behind.

    Two different things at once, and the store cannot tell them apart: a
    trajectory that passed, and one that failed without its findings clustering
    into an insight. Separating them needs a per-trajectory analysis result,
    which is a table this store does not define (§4.6, §5.4 of the design)."""

    IN_AN_INSIGHT = "in_an_insight"
    """An insight occurrence of the same run names it -- the trajectory is
    evidence behind a defect."""


class TrajectoryView(Trajectory):
    """One trajectory as a reader returns it: the stored row plus its outcome.

    Subclasses `Trajectory` for the same reason `InsightView` subclasses
    `Insight` -- the read adds to the row rather than reshaping it, so the
    stored fields keep one definition. The console URL is **not** here: it needs
    the project id, which is the caller's, and deriving a link from stored ids
    at serialization time is what lets the link format change without rewriting
    rows (the `_build_gcs_console_url` precedent).
    """

    outcome: TrajectoryProcessingState
    """Derived by the query that returned it; see `TrajectoryProcessingState`."""


class DailyOutcome(BaseModel):
    """How many trajectories of one day fell into one outcome.

    One row of the per-day chart. A trajectory sampled by two runs a week apart
    counts on both days: the chart says how much evaluating happened, not how
    many distinct conversations exist -- the convention
    `InsightView.trace_count` already uses.
    """

    day: dt.date
    """The UTC day of `Trajectory.created_at`, the table's partition column."""

    outcome: TrajectoryProcessingState
    trajectories: int


class PayloadStatus(enum.StrEnum):
    """What a *stored copy* of a conversation is.

    Not `IngestStatus`, which is what one sweep made of one attempt. This says
    what is in the archive now, after however many sweeps have repaired it, and
    it has a state ingestion has no equivalent of: `TRUNCATED`.
    """

    INGESTED = "ingested"
    """The whole conversation is here."""

    PARTIAL = "partial"
    """Assembled from incomplete telemetry -- something was missing and could
    not be ingested. A later sweep that assembles the conversation whole
    replaces this copy; nothing ever downgrades one."""

    TRUNCATED = "truncated"
    """Nothing was wrong with the telemetry: we chose to cut it, because a turn
    exceeded the write cap. Deliberately distinct from `PARTIAL`, because a
    reader has to be able to tell "the source was lossy" from "we made a
    decision" -- only the first can be repaired by re-ingesting."""


class TrajectoryPayload(BaseModel):
    """The manifest of one archived conversation: what it is, and how many
    turns of it were stored.

    Keyed ``(agent_name, trajectory_id)`` -- **no run_id**. `Trajectory` above
    records one sweep's ingestion and so is keyed by the run; this records the
    conversation, which is the same thing whichever sweep sampled it, and
    dropping the run is what deduplicates the bulky copy across sweeps.
    `agent_name` is in the key rather than merely on the row because trajectory
    ids being unique across agents is an assumption about the observed agent's
    telemetry, which AQA does not control.

    Separate from the turns because `agents` belongs to the conversation and
    not to any one turn of it: on a turn row it would either be repeated on
    every one with no rule for which copy wins, or hidden behind a sentinel
    ``turn_index`` every read then has to filter out.

    Mirrors ``terraform/modules/aqa/schemas/trajectory_payloads.json``, which is
    the only definition of that table. Change one and change the other.
    """

    agent_name: str
    """Observed agent; the first part of the key."""

    trajectory_id: str
    """The conversation this payload is of; the second part of the key, and the
    value `Trajectory.trajectory_id` and an occurrence's ``trajectory_ids``
    both carry."""

    created_at: dt.datetime
    """When this copy was stored; the partition column, and what the retention
    window expires on."""

    status: PayloadStatus
    """What this copy is. Required and undefaulted, so a cut conversation
    cannot be archived as a whole one."""

    turn_count: int = Field(ge=0)
    """How many `TrajectoryPayloadTurn` rows belong to this payload, so a
    reader can tell a conversation that was short from one whose turns were
    only partly written."""

    agents: dict[str, Any] | None = None
    """``AgentData.agents``: each agent's static config -- system instructions,
    tool definitions -- as the plain JSON the column holds.

    A mapping rather than the SDK's ``dict[str, AgentConfig]`` on purpose. This
    is an archive, and its job is to hand back what was stored even when the
    SDK model has since moved on; typing it would make a payload the current
    model cannot re-validate unreadable, which is the one failure an archive
    must not have."""

    agent_revision: str | None = None
    """The deployment that served the conversation, as the telemetry reported
    it.

    There could be multiple agent revisions in the traces of a long lasting
    multi-turn conversation and the last ingested revision is recorded here.

    ``None`` for a source that reports no revision, which denotes its single
    unnamed revision rather than an unknown one."""


class TrajectoryPayloadTurn(BaseModel):
    """One turn of an archived conversation.

    Keyed ``(agent_name, trajectory_id, turn_index)``, and split per **turn**
    rather than per trace: a multi-turn trajectory is one trace per turn, but
    the ``big_query`` source carries no trace ids at all, while every source
    has a turn index.

    Splitting the conversation across rows keeps a long session under the load
    job's per-row limit, and lets a reader size its batch to its model's
    context rather than to whole conversations -- which is what the planned
    `fetch`/`eval` split needs.

    Mirrors ``terraform/modules/aqa/schemas/trajectory_payload_turns.json``.
    """

    agent_name: str
    """Observed agent; the first part of the key, as on the manifest."""

    trajectory_id: str
    """The conversation this turn belongs to."""

    turn_index: int = Field(ge=0)
    """``ConversationTurn.turn_index``: the key's third part, and the order a
    reader reassembles the conversation in.

    Required here, where the SDK leaves it optional: an unindexed turn has no
    place in the key and no place in the order, so the writer has to settle on
    one -- the turn's position in ``AgentData.turns`` -- rather than storing a
    row nothing can address."""

    created_at: dt.datetime
    """When this copy was stored; the partition column, matching the manifest's
    so a conversation and its turns expire together."""

    turn: dict[str, Any]
    """One ``ConversationTurn`` as the plain JSON the column holds, for the
    reason `TrajectoryPayload.agents` is a mapping."""
