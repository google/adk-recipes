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

"""The insight-correlation vocabulary: candidates in, durable issues out.

Per-sweep candidates (`RubricExample`, wrapping a `Finding`) are produced fresh
every run; `Insight` and `InsightOccurrence` are the durable BigQuery records
they correlate into.

Plain records, no behavior -- matching and persistence are `InsightStore`'s
job. Pydantic so they cross the durable JSON state boundary as
``.model_dump()`` dicts.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from ambient_quality_agent.tools.insights.findings import Finding
from pydantic import BaseModel, Field


class InsightStatus(enum.StrEnum):
    """Lifecycle of a durable insight. ``StrEnum`` to serialize as the bare
    string the BigQuery ``status`` column stores."""

    NEW = "NEW"
    RECURRING = "RECURRING"

    RESOLVED = "RESOLVED"
    """Terminal: the match query skips resolved insights, so a later recurrence
    mints a new one rather than reopening this."""


class OccurrenceState(enum.StrEnum):
    """Whether an occurrence is a sighting of an insight, and if not, why not.

    ``StrEnum`` to serialize as the bare string the BigQuery
    ``occurrence_state`` column stores. Distinct from `InsightStatus`, which is
    the lifecycle of the durable insight rather than anything about one sweep.

    Three states in one column, because `InsightOccurrence.insight_id` tells
    only the first of them from the other two, and a reader left to work out
    which of the others it was had to go inspecting `analyses`.
    """

    TRACKED = "TRACKED"
    """A sighting of the insight `InsightOccurrence.insight_id` names."""

    REJECTED = "REJECTED"
    """The verification pass judged the candidate no defect, so it named no
    insight; the verdict that threw it out is the one under `analyses`."""

    UNJUDGED = "UNJUDGED"
    """The verification pass reached no verdict -- it skipped the candidate or
    failed on it -- so nothing vouches for the candidate and it named no
    insight. What the sweep saw is on the row regardless."""


class RubricExample(BaseModel):
    """A `Finding` plus the conversation it was found in.

    The label says what went wrong, the trace shows where; the downstream
    root-cause harness needs both. Stored verbatim as JSON on the occurrence, so
    its shape is a wire contract with `reader` and the dashboard.
    """

    rubric: Finding
    """The finding, kept under the field name ``rubric`` because that is the key
    the stored payload and the dashboard read."""

    eval_case_id: str = ""
    """The session the finding was found in (`Finding.session_id`), kept under
    this name because that is what the stored payload and the dashboard read."""

    trace: list[dict] = Field(default_factory=list)
    """Conversation turns as JSON-safe dicts. Empty when the session's trace was
    unavailable -- degraded evidence, not an error."""

    examined: bool = False
    """True when the verification pass was actually shown this example's trajectory.
    Says the model read it, not that the example was validated."""


class Insight(BaseModel):
    """A durable, deduplicated production issue.

    Mirrors ``terraform/modules/aqa/schemas/insights.json``, which is the only
    definition of that table. Change one and change the other.

    ``insight_id`` is minted once and never changes, so occurrences accumulate
    against it across sweeps.
    """

    insight_id: str
    agent_name: str
    """Scope pre-filter for matching: insights are never compared across
    agents."""

    label: str

    status: InsightStatus
    created_at: dt.datetime
    """First sighting."""

    updated_at: dt.datetime
    """Most recent sighting; drives auto-resolution."""

    resolved_at: dt.datetime | None = None
    """When a sweep resolved it; ``None`` while it is still open.

    A ``None`` value indicates the timestamp is unrecorded rather than unresolved;
    `status` indicates whether it is resolved."""

    dismissed_at: dt.datetime | None = None
    """When an operator dismissed it; ``None`` means nobody has.

    Its own column rather than a fourth `InsightStatus`: `status` is what the
    pipeline decided about the defect, dismissal is what a human decided about
    the row. A dismissed insight stays matched during clustering, so a
    recurrence lands on it and stays hidden rather than being minted afresh."""

    merged_into_insight_id: str | None = None
    """The insight an operator judged this one a duplicate of; ``None`` when it
    is not a duplicate of anything.

    Always names a row whose own pointer is ``None``. `InsightStore.merge_insight`
    moves the source *and everything already pointing at it* in one statement, so
    a chain flattens as it is written and the depth is never more than one --
    which is what lets a read resolve ownership with a single ``COALESCE``."""

    merged_at: dt.datetime | None = None
    """When that judgement was made; ``None`` alongside a ``None``
    `merged_into_insight_id`. Records the current judgement, not its history: a
    re-pointed row is restamped."""


VERIFICATION_ANALYSIS_KEY = "verification"
"""`InsightOccurrence.analyses` key for the cluster-verification result.

A name rather than a literal because it is a wire contract with two readers
outside this package: whichever step writes the result, and the dashboard,
which projects the explanation into what it calls a `diagnosis`. `ui/` cannot
import this module -- the image ships without the agent -- so it hand-copies
the value, and `tests/test_ui_investigations.py` holds the two level.
"""


class ClusterVerification(BaseModel):
    """A reality check on one candidate cluster: is it a real defect, and whose.

    Named for what it does rather than for the map it lives in, because a later
    step will report something else entirely. The step's name is the key in
    `analyses`, so a reader knows which shape to expect."""

    model: str = ""
    """The model that produced this result."""

    valid: bool = True
    """Whether the evidence supports the claim and the cluster is one coherent
    issue. Under `config.is_verification_enforced` False retires the candidate: it
    mints no insight and matches none."""

    explanation: str = ""
    """Explains what the defect is and why, giving a triager immediate context
    without reading raw trajectories."""

    refined_label: str = ""
    """A sharper restatement of the cluster's `label`. Kept here rather than
    written over `label`, which is what the sweep itself observed."""

    created_at: dt.datetime | None = None
    """When the method ran, so a stale result is recognizable as one."""


class InsightOccurrence(BaseModel):
    """What one sweep saw of one candidate issue, and which insight, if any, it names.

    Append-only: never updated, so ordered occurrences reconstruct an issue's
    history without a transition table. Mirrors
    ``terraform/modules/aqa/schemas/insight_occurrences.json``, which is the
    only definition of that table. Change one and change the other.

    An analysis method that runs over a candidate cluster appends its result to
    `analyses` rather than editing the occurrence: what the sweep observed and what
    a method concluded about it stay separable, and a second method can be added
    without another column.
    """

    occurrence_id: str

    insight_id: str | None
    """The insight this is a sighting of, or ``None`` when it is a sighting of
    none and `occurrence_state` says why. Such a row records what the sweep saw
    for audit; it is no sighting of a tracked insight, so the read path leaves
    it out."""

    occurrence_state: OccurrenceState
    """Whether this is a sighting of a tracked insight, and when it is not, what
    stopped it being one -- what `insight_id` alone cannot say.

    Undefaulted, so a candidate nothing vouched for cannot be recorded as a
    sighting by omission, which is what reading the state off a null
    `insight_id` allowed."""

    run_id: str
    """Also the cleanup key: a retried sweep deletes its own occurrences by this
    before rewriting them."""

    created_at: dt.datetime
    agent_name: str
    """Denormalized so occurrences can be queried without a join."""

    agent_revision: str = ""
    """Deployment revision this sighting is attributed to, so a triager can say
    which build a defect appeared in. A sighting whose findings span a redeploy
    records the newest of their revisions. Empty when the telemetry source
    reports no revision, which denotes its single unnamed revision."""

    analysis_mode: str = ""
    """The analysis mode this sweep ran, from ``quality_analysis_mode``.

    A property of the sighting, not of the defect: the workflow routes a run to one
    producer, so an occurrence has exactly one mode. Denormalized here for the same
    reason as `agent_name` -- so occurrences can be filtered by it without a join."""

    label: str
    """Short issue description."""

    item_count: int = 0

    trace_count: int = 0
    """Distinct conversations this sighting appeared in. Evaluated across all
    findings in the cluster, independent of the capped `rubrics` sample."""

    trajectory_ids: list[str] = Field(default_factory=list)
    """Which conversations those were, as trajectory ids -- the value
    ``trajectories.trajectory_id`` holds, so a sighting resolves to the source
    traces behind it and to a console link.

    Every trajectory the cluster covers, not the capped `rubrics` sample, which
    is what turns the sighting from an illustration into an index. Kept
    alongside `trace_count` to ensure integer counts remain reliable even when
    trajectory lists are empty."""

    analyses: dict[str, ClusterVerification] = Field(default_factory=dict)
    """Analysis step to what that step concluded. Empty when none ran.

    Keyed by step -- verification today, another pass later -- not by
    `Insight.analysis_mode`, which is a different axis: a sighting belongs to one
    mode and may carry several steps' conclusions. Keyed rather than flattened so
    a step can be added without another column, and so "this step did not run" is
    the absence of a key instead of a field full of empty strings."""

    rubrics: list[RubricExample] = Field(default_factory=list)
    """Sampled findings with full conversation traces (capped at MAX_EXAMPLES)."""


# Subclasses Insight to inherit core fields while adding read-view aggregation metrics.
class InsightView(Insight):
    """A Read View of the Insight model."""

    occurrence_count: int = 0
    """How many sweeps have seen this issue."""

    trace_count: int = 0
    """Conversations this issue was seen in, summed over its occurrences. A
    trace one sweep saw and the next saw again counts in both: the number says
    how often the issue was met, not how many distinct conversations exist."""

    last_run_id: str | None = None
    """`run_id` of the most recent occurrence; the harness's entry point into
    `get_insight` for the freshest evidence."""

    last_run_at: dt.datetime | None = None
    """`created_at` of the most recent occurrence (``None`` when there are none)."""

    has_root_cause: bool = False
    """Indicates whether any root-cause record exists for this insight.

    Computed at read time to allow list views to flag diagnosed insights
    without fetching full root-cause records."""


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

    Mirrors the BigQuery table schema in
    ``terraform/modules/aqa/schemas/insight_root_causes.json``.

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
