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

"""Workflow state schema for the AQA investigation pipeline."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from ambient_quality_agent import providers
from ambient_quality_agent.config import (
    DEFAULT_INSIGHTS_MATCH_MODEL,
    DEFAULT_QUALITY_ANALYSIS_MODE,
    DEFAULT_SELECTOR_AI_MODEL,
    Config,
    Model,
)
from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.tools.insights.findings import FindingSet
from ambient_quality_agent.tools.investigations.models import (
    InvestigationCounters,
)
from google.adk.events.event import Event
from google.adk.sessions.state import State
from google.genai import types as genai_types
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# State key under which `record_progress_event` accumulates emitted snippets.
PROGRESS_LOG_KEY = "progress_log"

# State key under which `add_counts` accumulates the run's stage counters.
COUNTERS_KEY = "counters"


def add_counts(state: ADKStateLike, **deltas: int) -> None:
    """Adds `deltas` to the run's counters in `state`.

    Stages accumulate counts independently and never read running totals back,
    ensuring counters remain accurate even if earlier stages were skipped or
    multiple scopes contribute. Counter names must match fields in
    `InvestigationCounters`.

    Args:
        state: ADK session state mapping.
        **deltas: Keyword arguments mapping counter names to positive increments.

    Raises:
        ValueError: If any counter name is not a field in InvestigationCounters.
    """
    unknown = set(deltas) - set(InvestigationCounters.model_fields)
    if unknown:
        raise ValueError(f"unknown investigation counter(s): {sorted(unknown)}")
    counters = dict(state.get(COUNTERS_KEY) or {})
    for name, delta in deltas.items():
        counters[name] = counters.get(name, 0) + delta
    state[COUNTERS_KEY] = counters


event_writer: Callable[[ADKStateLike, str, str], None] = (
    providers.build_unbound_provider("event_writer")
)
"""Swappable seam; tests replace it so a node never reaches BigQuery."""


class WorkflowState(BaseModel):
    """Shared, typed state for one investigation run.

    This is the object mutated in place by the ADK 2.0 workflow nodes.
    It carries the process configuration inputs (copied from
    `Config`) plus values derived during the run.
    """

    # --- Configuration inputs (copied from Config) ---

    observed_agent_name: str
    """Name of the watched agent under evaluation."""

    project_id: str
    """Google Cloud project that hosts the watched agent."""

    location: str
    """Google Cloud region used for the eval client (and chat LLM/engine)."""

    quality_analysis_mode: str = DEFAULT_QUALITY_ANALYSIS_MODE
    """Quality analysis strategy (``session_review`` or ``eval_service``);
    also emitted by `init` to route to the appropriate producer branch."""

    metrics_gcs_bucket: str = ""
    """GCS bucket holding the code-metric library.

    Empty, or holding no metric, leaves session review running on its own.
    """

    jobs_gcs_bucket: str = ""
    """GCS bucket holding the developer's goal (`goal.md`), which the session
    review reads once per run."""

    telemetry_ingestion_source: str = "big_query"
    """Where telemetry is pulled from."""

    observed_project_id: str = ""
    """Project the observed agent runs in, which owns `telemetry_dataset`;
    empty means `project_id`."""

    telemetry_dataset: str = "agent_analytics"
    """BigQuery dataset holding the telemetry `telemetry_ingestion_source`
    reads."""

    telemetry_table: str = "agent_events"
    """BigQuery table or view within `telemetry_dataset`."""

    telemetry_location: str = "us-central1"
    """BigQuery location of `telemetry_dataset`, and the location ingestion
    jobs run in. May differ from `location` (which drives the eval
    client)."""

    data_evaluation_cap: int = 1000
    """Maximum number of records to pull into a single evaluation run."""

    multi_turn_metrics: list[str] = Field(
        default_factory=lambda: [
            "task_success",
            "tool_use_quality",
            "trajectory_quality",
        ]
    )
    """Metrics evaluated over a multi-turn conversation."""

    single_turn_metrics: list[str] = Field(default_factory=list)
    """Metrics evaluated over a single turn."""

    data_lookback_window: int = 7
    """How many days of telemetry to consider."""

    base_model: Model = Field(default_factory=Model)
    """Base model for the orchestrator (name + serving location)."""

    insights_match_model: str = DEFAULT_INSIGHTS_MATCH_MODEL
    """Model name for the same-issue judge in insight matching."""

    selector_ai_model: str = DEFAULT_SELECTOR_AI_MODEL
    """Model invoked by `AI.IF` in conversation selectors, executed under the
    caller's credentials (`roles/aiplatform.user`)."""

    aqa_dataset: str = "aqua_insights"
    """BigQuery dataset the agent owns for its own tables; its location is
    `aqa_dataset_location`."""

    aqa_dataset_location: str = "us-central1"
    """BigQuery location of `aqa_dataset`."""

    insights_auto_resolve_days: int = 14
    """Days without a sighting before an insight auto-resolves; `0` disables it."""

    insights_verification_enabled: bool | None = None
    """Whether `insight_correlation` reads each candidate's own traces to judge,
    name and describe it. ``None`` carries "unset" to
    `config.is_verification_enabled`, which holds the default."""

    insights_verification_enforced: bool | None = None
    """Whether a negative verdict withholds the candidate it judged. ``None``
    carries "unset" to `config.is_verification_enforced`, which holds the
    default."""

    standalone: bool = False
    """Whether AQA runs on its own, with nothing of it deployed (see
    `Config.standalone`). A standalone sweep fails on a window that holds no
    traces, where a deployed one reports it and completes."""

    # --- Calculated during the run ---

    run_id: str = ""
    """Identifier of the sweep this graph run belongs to, seeded from the
    orchestrator's `InvestigationRecord.run_id` (or generated for local runs)."""

    budget_per_metric: int = 0
    """Per-metric evaluation budget, derived from `data_evaluation_cap`
    and the total active metric count across all metric types."""

    window_start: dt.datetime | None = None
    """Inclusive start of the telemetry time window."""

    window_end: dt.datetime | None = None
    """Inclusive end of the telemetry time window (typically "now")."""

    trigger_type: str = ""
    """Trigger category identifying run origin (e.g. manual, custom, scheduled)."""

    selector_sql: str = ""
    """Custom SQL query specifying target conversations, replacing the random sample.

    Empty for ambient runs.
    """

    session_review_focus: str = ""
    """Additional focus instructions appended to the reviewer prompt.

    Empty for ambient runs.
    """

    finding_sets: list[FindingSet] = Field(default_factory=list)
    """Finding sets collected from evaluated pages across all evaluation scopes.

    Each entry contains failed rubrics, their session traces and the agent
    configurations those sessions ran under, pruned of passing cases to
    minimize memory overhead. Stored as serialized dicts to support durable
    state persistence across workflow transitions.
    """

    multi_turn_pages_evaluated: int = 0
    """Count of evaluated multi-turn pages, recorded for run summarization."""

    single_turn_pages_evaluated: int = 0
    """Count of evaluated single-turn pages, recorded for run summarization."""

    eval_outcome_counts: dict[str, int] = Field(
        default_factory=lambda: {"passed": 0, "failed": 0, "errored": 0}
    )
    """Per-investigation pass/fail/error tally over every ``(case, metric)``
    outcome, accumulated by both `eval` nodes. ``N/A`` outcomes (no
    rubric verdicts and no numeric score) are counted as neither."""

    metrics_by_name: dict[str, dict[str, int]] = Field(default_factory=dict)
    """Per-metric pass, fail, and error counts across evaluated trajectories."""

    metrics_detail: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-metric execution kind, expected behavior, threshold, and skip
    reason."""

    goal: dict[str, str | None] = Field(default_factory=dict)
    """The developer's goal the session review ran with: its ``version``, its
    ``source`` (see `InvestigationGoal.source`) and the ``prompt_sha256`` of
    the text it added to every review prompt. Empty when no review ran."""

    counters: dict[str, int] = Field(default_factory=dict)
    """The run's stage counters, added to by the nodes via `add_counts` and
    stored on the run record as `InvestigationCounters`. A plain mapping here
    rather than that model: it crosses the durable JSON state boundary, and
    every writer only ever adds to one name."""

    progress_log: list[str] = Field(default_factory=list)
    """Markdown progress/diagnostic snippets emitted by nodes during the
    run (via `record_progress_event`), accumulated in emission order. These are
    surfaced to the orchestrator (folded into the run summary) and printed
    by the local runners; they may carry useful detail about errors or
    skipped steps."""

    @classmethod
    def from_config(cls, config: Config | None = None) -> WorkflowState:
        """Seeds a fresh workflow state from a `Config` instance.

        Copies configuration parameters; calculated run fields retain defaults.

        Args:
            config: Optional Config instance; defaults to global config.

        Returns:
            Populated WorkflowState instance.
        """
        if config is None:
            from ambient_quality_agent.config import config as _config

            cfg = _config
        else:
            cfg = config
        return cls(
            observed_agent_name=cfg.observed_agent_name,
            project_id=cfg.project_id,
            location=cfg.location,
            quality_analysis_mode=cfg.quality_analysis_mode,
            metrics_gcs_bucket=cfg.metrics_gcs_bucket,
            jobs_gcs_bucket=cfg.jobs_gcs_bucket,
            telemetry_ingestion_source=cfg.telemetry_ingestion_source,
            observed_project_id=cfg.observed_project_id,
            telemetry_dataset=cfg.telemetry_dataset,
            telemetry_table=cfg.telemetry_table,
            telemetry_location=cfg.telemetry_location,
            data_evaluation_cap=cfg.data_evaluation_cap,
            multi_turn_metrics=list(cfg.multi_turn_metrics),
            single_turn_metrics=list(cfg.single_turn_metrics),
            data_lookback_window=cfg.data_lookback_window,
            base_model=cfg.base_model,
            insights_match_model=cfg.insights_match_model,
            selector_ai_model=cfg.selector_ai_model,
            aqa_dataset=cfg.aqa_dataset,
            aqa_dataset_location=cfg.aqa_dataset_location,
            insights_auto_resolve_days=cfg.insights_auto_resolve_days,
            insights_verification_enabled=cfg.insights_verification_enabled,
            insights_verification_enforced=cfg.insights_verification_enforced,
            standalone=cfg.standalone,
        )

    @staticmethod
    def build_model_event(text: str) -> Event:
        """Wraps `text` in a model-role `Event` for the chat stream.

        Args:
            text: Message string to send.

        Returns:
            ADK Event with role "model" and output text.
        """
        return Event(
            output=text,
            content=genai_types.Content(
                role="model", parts=[genai_types.Part(text=text)]
            ),
        )

    @staticmethod
    def record_progress_event(
        state: State, text: str, source: str = ""
    ) -> Event:
        """Records `text` in the run's progress log and returns it as an event.

        Every emitted snippet is recorded three ways:
        1. Appended to ``state[PROGRESS_LOG_KEY]`` for orchestrator and local runner display.
        2. Written as an event row to BigQuery for real-time progress tracking.
        3. Returned as a model-role `Event` so chat clients render it in the stream.

        Persisting the event is best-effort: losing a progress row does not fail
        the investigation.

        Args:
            state: Live session-backed State object.
            text: Markdown progress or diagnostic snippet to emit.
            source: Emitting node name stamped on the stored row.

        Returns:
            Model-role Event containing the emitted text.
        """
        log = list(state.get(PROGRESS_LOG_KEY) or [])
        log.append(text)
        state[PROGRESS_LOG_KEY] = log
        if state.get("run_id"):
            try:
                event_writer(state, text, source)
            except Exception as exc:
                logger.warning(
                    "investigations: could not record event row: %s", exc
                )
        return WorkflowState.build_model_event(text)
