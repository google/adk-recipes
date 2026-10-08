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

"""Preview functionality for agent-authored SQL selectors before launching sweeps.

Wiring::

    chat_agent.preview_custom_investigation()
          │        chat_agent.start_custom_investigation(),
          │        chat_agent._validate_selector_after_attempts_spent()
          ▼                                              ▼
    preview_selector()                           validate_selector()
          ├──► selector.find_lexical_violation() ◄───────┤
          ├──► _resolve_window() ◄───────────────────────┤
          │      └──► derive_window_and_budget()         │
          ▼ worker thread                                ▼ worker thread
    _preview_on_this_thread()                    _precheck_on_this_thread()
     │           │ fetcher_factory()                     │ fetcher_factory()
     │           ▼                                       │
     │     BaseFetcher ◄─────────────────────────────────┘
     │       precheck_selector(), count_scanned(),
     │       submit_query(), fetch_page() ──► BigQuery
     │           │ add()
     │           ▼
     │     _ArchiveCollector ──► build_archive()
     ▼ store_factory()
    TrajectoryStore.record_payloads() ──► AQuA dataset, or in process

Runs the multi-turn ingestion pipeline with a limit of five conversations to
estimate matched conversation counts and archive sample payloads for inspection
without executing a full investigation sweep.

Key design constraints:
- **Sample vs. Examples**: Ingestion queries order by ``RAND()``, so preview and
  full runs draw independent random samples. Previewed conversations illustrate
  matching criteria rather than the exact set evaluated during a full run,
  unless the total matches fit within `EXAMPLE_COUNT` and the full run budget.
- **Payloads Only**: Archives raw trajectory payloads without creating
  `Trajectory` index records, ensuring preview runs do not populate run-level
  investigation metrics or outcome dashboards.
- **Locking**: Does not acquire the investigation agent lock, allowing
  concurrent preview checks without blocking subsequent run execution.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent import providers
from ambient_quality_agent.core.investigation.job_scheduling import (
    derive_window_and_budget,
    iso_instant_to_utc,
)
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.session_state import DetachedContext
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from ambient_quality_agent.tools.trajectories.payloads import build_archive
from ambient_quality_agent.tools.trajectories.reader import (
    build_trace_console_url,
)
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentplatform._genai.types import EvalCase
    from ambient_quality_agent.config import Config
    from ambient_quality_agent.core.session_state import LaunchContext
    from ambient_quality_agent.tools.ingestion.base import BaseFetcher
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )
    from ambient_quality_agent.tools.trajectories.store import TrajectoryStore

logger = logging.getLogger(__name__)

SESSION_REVIEW_MODE = "session_review"
"""Analysis mode that supports SQL selectors."""

EXAMPLE_COUNT = 5
"""Number of sample conversations fetched and archived for preview."""

DEADLINE_SECONDS = 180.0
"""Overall wall-clock timeout in seconds for preview execution."""

JOB_TIMEOUT_MS = 240_000
"""Server-side execution timeout in milliseconds for BigQuery query jobs.

Set higher than `DEADLINE_SECONDS` so client-side timeout handling fires first
and returns a structured `timed_out` response, while ensuring abandoned
background query jobs terminate server-side.
"""

FIRST_MESSAGE_CHARS = 280
"""Maximum characters retained from the opening user message for preview summaries."""

PREVIEW_RUN_SEGMENT = "preview"
"""Placeholder run identifier used in `CASE_VIEW_PATH` for preview links."""

CASE_VIEW_PATH = "/investigations/{run}/cases/{trajectory_id}"
"""Dashboard URL template for inspecting an archived conversation."""

EXAMPLES_NOTE = (
    "These are examples of what the selector matched, not the conversations "
    "the investigation will review. The preview and the run each draw their "
    "own random sample, so they will almost certainly be different "
    "conversations."
)
"""User note displayed when preview examples represent a subset of matched conversations."""

WHOLE_MATCH_NOTE = (
    "These are every conversation the selector matched, so the investigation "
    "will review exactly these."
)
"""User note displayed when all matched conversations fit within preview and budget limits."""

NO_MATCH_NOTE = (
    "The selector matched no conversations in this window. Widen the window or "
    "loosen the selector, then preview again."
)
"""User note displayed when the selector query matches zero conversations."""

TIMEOUT_NOTE = (
    "The preview ran out of time and returned nothing. Nothing is known about "
    "how many conversations the selector matches; narrow it and try again."
)

REJECTED_NOTE = "The selector was refused and nothing was read. Rewrite it and preview again."

VALIDATION_EVENT = "aqa_selector_validated"
"""``event`` field of the structured entry written for every validation outcome.

The feature has no flag to switch off and no bytes ceiling, so this entry (or a
log-based metric built on it) is how a selector that scans the dataset or a model
stuck rewriting rejected SQL becomes visible before the bill does.
"""

PREVIEW_SURFACE = "preview"
"""``surface`` of a validation the user asked for and will read."""

START_SURFACE = "start"
"""``surface`` of the single validation that gates launching a custom run."""


@dataclasses.dataclass(frozen=True)
class PreviewedConversation:
    """Preview summary of a single matching conversation."""

    trajectory_id: str
    """Unique trajectory identifier and archive storage key."""

    case_view_path: str
    """Dashboard URL path for replaying the archived conversation."""

    turn_count: int
    """Number of conversation turns in the archived payload."""

    first_user_message: str
    """Opening user message truncated to `FIRST_MESSAGE_CHARS`, or empty if unavailable."""

    trace_url: str | None = None
    """Cloud Trace URL, or None if the telemetry source does not provide trace IDs."""

    def to_payload(self) -> dict[str, Any]:
        """Serialize the conversation preview to a JSON-compatible dictionary.

        Returns:
            Dictionary containing conversation summary fields.
        """
        payload = {
            "trajectory_id": self.trajectory_id,
            "case_view_path": self.case_view_path,
            "turn_count": self.turn_count,
            "first_user_message": self.first_user_message,
        }
        if self.trace_url:
            payload["trace_url"] = self.trace_url
        return payload


@dataclasses.dataclass(frozen=True)
class SelectorPreview:
    """Results of a selector preview execution, including validation or timeout status."""

    rejection: selector.Rejection | None = None
    """Rejection details if validation failed, or None if accepted."""

    matched: int = 0
    """Total number of matching conversations across the window before sampling."""

    would_review: int = 0
    """Estimated number of conversations a full run would review given the evaluation cap."""

    examples: tuple[PreviewedConversation, ...] = ()
    """Sample matching conversations (up to `EXAMPLE_COUNT`)."""

    timed_out: bool = False
    """Whether preview execution exceeded `DEADLINE_SECONDS`."""

    @property
    def is_valid(self) -> bool:
        """Return True if the selector passed validation."""
        return self.rejection is None

    def to_payload(self) -> dict[str, Any]:
        """Serialize the preview result to a JSON-compatible dictionary.

        Returns:
            Dictionary containing preview status, counts, examples, and user note.
        """
        return {
            "rejected": not self.is_valid,
            "reason": self.rejection.reason.value if self.rejection else None,
            "explanation": self.rejection.explanation
            if self.rejection
            else None,
            "timed_out": self.timed_out,
            "matched": self.matched,
            "would_review": self.would_review,
            "examples": [example.to_payload() for example in self.examples],
            "note": self._resolve_note(),
        }

    def _resolve_note(self) -> str:
        """Determines the appropriate explanatory note for the preview payload.

        Returns:
            Predefined note string describing matching and sampling status.
        """
        if not self.is_valid:
            return REJECTED_NOTE
        if self.timed_out:
            return TIMEOUT_NOTE
        if not self.examples:
            return NO_MATCH_NOTE
        # Warn about sampling when preview or budget limits truncate the match set.
        if (
            self.matched > len(self.examples)
            or self.would_review < self.matched
        ):
            return EXAMPLES_NOTE
        return WHOLE_MATCH_NOTE


async def preview_selector(
    context: LaunchContext,
    *,
    selector_sql: str,
    window_start: str = "",
    window_end: str = "",
) -> SelectorPreview:
    """Validate, sample, archive, and describe matching conversations for a selector.

    Performs lexical validation and a BigQuery dry run. Validation failures are
    returned as rejection details in `SelectorPreview` rather than raised.
    Synchronous BigQuery and GCS operations run in a worker thread bounded by
    `DEADLINE_SECONDS`.

    Args:
        context: Launch context supplying configuration overrides.
        selector_sql: Unwrapped agent-authored SQL selector query.
        window_start: Optional ISO-8601 start timestamp with UTC offset. When
            omitted along with `window_end`, defaults to the configured lookback.
        window_end: Optional ISO-8601 end timestamp with UTC offset.

    Returns:
        A `SelectorPreview` instance containing validation status, counts, and
        examples. When `DEADLINE_SECONDS` expires first, `timed_out` is set and
        every other field keeps its default: no counts, no examples, and
        `is_valid` true, since the selector was accepted rather than refused.
        The abandoned worker thread is not interrupted; its query jobs stop
        server-side after `JOB_TIMEOUT_MS`.

    Raises:
        ValueError: If the quality analysis mode is not ``session_review``, the
            selector SQL is empty, or only one window boundary is provided.
    """
    config = _load_selector_config(context, selector_sql)
    # Perform static lexical validation before executing BigQuery dry runs.
    violation = selector.find_lexical_violation(selector_sql)
    if violation is not None:
        _log_validation_outcome(
            config, PREVIEW_SURFACE, violation, estimated_bytes=0
        )
        return SelectorPreview(rejection=violation)
    start, end = _resolve_window(config, window_start, window_end)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _preview_on_this_thread, config, selector_sql, start, end
            ),
            timeout=DEADLINE_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Preview of a selector for %r gave up after %.0fs.",
            config.observed_agent_name,
            DEADLINE_SECONDS,
        )
        return SelectorPreview(timed_out=True)


async def validate_selector(
    context: LaunchContext,
    *,
    selector_sql: str,
    window_start: str = "",
    window_end: str = "",
    surface: str = START_SURFACE,
) -> selector.PrecheckResult:
    """Validate whether a selector is permitted to run without querying conversations.

    Executes lexical checks and BigQuery dry-run validation without data
    sampling or payload archiving. Gates investigation launches to prevent
    creating run records for invalid selectors.

    Args:
        context: Launch context supplying configuration overrides.
        selector_sql: Unwrapped agent-authored SQL selector query.
        window_start: Optional ISO-8601 start timestamp with UTC offset. When
            omitted along with `window_end`, defaults to the configured lookback.
        window_end: Optional ISO-8601 end timestamp with UTC offset.
        surface: Which caller the logged outcome is attributed to,
            `START_SURFACE` or `PREVIEW_SURFACE`.

    Returns:
        A `selector.PrecheckResult` whose `rejection` is None when the selector
        may run, and whose `estimated_bytes` reports what the dry run costed the
        query at.

    Raises:
        ValueError: If the quality analysis mode is not ``session_review``, the
            selector SQL is empty, or only one window boundary is provided.
    """
    config = _load_selector_config(context, selector_sql)
    violation = selector.find_lexical_violation(selector_sql)
    if violation is not None:
        _log_validation_outcome(config, surface, violation, estimated_bytes=0)
        return selector.PrecheckResult(rejection=violation)
    start, end = _resolve_window(config, window_start, window_end)
    result = await asyncio.to_thread(
        _precheck_on_this_thread, config, selector_sql, start, end
    )
    _log_validation_outcome(
        config,
        surface,
        result.rejection,
        estimated_bytes=result.estimated_bytes,
    )
    return result


def _precheck_on_this_thread(
    config: Config, selector_sql: str, start: dt.datetime, end: dt.datetime
) -> selector.PrecheckResult:
    """Dry-run the selector synchronously, discarding anything it would ingest.

    Args:
        config: Agent configuration.
        selector_sql: Raw selector SQL query.
        start: Window start timestamp.
        end: Window end timestamp.

    Returns:
        The fetcher's `selector.PrecheckResult`.
    """
    fetcher = fetcher_factory(
        config, selector_sql, recorder=NullTrajectoryRecorder()
    )
    return fetcher.precheck_selector(
        config.observed_agent_name, start, end, limit=EXAMPLE_COUNT
    )


def _load_selector_config(context: LaunchContext, selector_sql: str) -> Config:
    """Resolve the effective config for a selector request and reject caller mistakes.

    Args:
        context: Launch context supplying configuration overrides.
        selector_sql: Unwrapped agent-authored SQL selector query.

    Returns:
        The effective `Config`.

    Raises:
        ValueError: If the quality analysis mode is not ``session_review`` or
            the selector SQL is empty.
    """
    config = effective_config.load(context.state)
    if config.quality_analysis_mode != SESSION_REVIEW_MODE:
        raise ValueError(
            f"selectors are offered only in {SESSION_REVIEW_MODE!r} mode; this "
            f"deployment runs {config.quality_analysis_mode!r}"
        )
    if not selector_sql.strip():
        raise ValueError("Cannot preview an empty selector.")
    return config


def _log_validation_outcome(
    config: Config,
    surface: str,
    rejection: selector.Rejection | None,
    *,
    estimated_bytes: int,
) -> None:
    """Write the one structured entry a validation outcome produces.

    `estimated_bytes` is carried although nothing gates on it: the dry run's
    figure is the only advance warning that an accepted selector is about to
    scan the whole dataset.

    Args:
        config: Effective configuration the selector was validated under.
        surface: `PREVIEW_SURFACE` or `START_SURFACE`.
        rejection: Why the selector was refused, or None when it was accepted.
        estimated_bytes: Bytes the dry run expects the selector to scan; zero
            when a lexical check refused it before BigQuery saw it.
    """
    logger.info(
        json.dumps(
            {
                "event": VALIDATION_EVENT,
                "agent": config.observed_agent_name,
                "surface": surface,
                "rejected": rejection is not None,
                "reason": rejection.reason.value if rejection else None,
                "estimated_bytes": estimated_bytes,
            },
            default=str,
        )
    )


def _resolve_window(
    config: Config, window_start: str, window_end: str
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve preview window timestamps, defaulting to the configured lookback window.

    Args:
        config: Agent configuration containing lookback settings.
        window_start: ISO-8601 timestamp string with an explicit offset, or empty.
        window_end: ISO-8601 timestamp string with an explicit offset, or empty.

    Returns:
        Tuple of (start, end) UTC datetime instances.

    Raises:
        ValueError: If only one boundary is provided, UTC offsets are missing,
            or start does not precede end.
    """
    if not window_start and not window_end:
        derived_start, derived_end, _ = derive_window_and_budget(config)
        return dt.datetime.fromisoformat(
            derived_start
        ), dt.datetime.fromisoformat(derived_end)
    if not window_start or not window_end:
        raise ValueError(
            "a preview window needs both window_start and window_end; "
            f"got start={window_start!r}, end={window_end!r}"
        )
    start = dt.datetime.fromisoformat(
        iso_instant_to_utc(window_start, label="window_start")
    )
    end = dt.datetime.fromisoformat(
        iso_instant_to_utc(window_end, label="window_end")
    )
    if start >= end:
        raise ValueError(
            f"window_start {start} must fall before window_end {end}"
        )
    return start, end


def _preview_on_this_thread(
    config: Config, selector_sql: str, start: dt.datetime, end: dt.datetime
) -> SelectorPreview:
    """Execute preview validation, count query, sampling, and archiving synchronously.

    Runs blocking BigQuery and GCS operations on a dedicated worker thread to
    avoid blocking the asyncio event loop.

    Args:
        config: Agent configuration.
        selector_sql: Raw selector SQL query.
        start: Window start timestamp.
        end: Window end timestamp.

    Returns:
        A `SelectorPreview` instance.
    """
    collector = _ArchiveCollector(agent_name=config.observed_agent_name)
    fetcher = fetcher_factory(config, selector_sql, recorder=collector)

    precheck = fetcher.precheck_selector(
        config.observed_agent_name, start, end, limit=EXAMPLE_COUNT
    )
    _log_validation_outcome(
        config,
        PREVIEW_SURFACE,
        precheck.rejection,
        estimated_bytes=precheck.estimated_bytes,
    )
    if not precheck.is_valid:
        return SelectorPreview(rejection=precheck.rejection)

    matched = fetcher.count_scanned(
        config.observed_agent_name, MetricType.MULTI_TURN, start, end
    )
    execution_id = fetcher.submit_query(
        agent_name=config.observed_agent_name,
        metric_type=MetricType.MULTI_TURN,
        start=start,
        end=end,
        limit=EXAMPLE_COUNT,
    )
    fetcher.fetch_page(
        execution_id=execution_id, metric_type=MetricType.MULTI_TURN
    )

    sightings = collector.sightings
    if sightings:
        store_factory(config).record_payloads(
            [sighting.archive for sighting in sightings]
        )
    return SelectorPreview(
        matched=matched,
        would_review=min(matched, config.data_evaluation_cap),
        examples=tuple(
            _build_previewed_conversation(
                sighting, config.resolve_observed_project_id()
            )
            for sighting in sightings
        ),
    )


@dataclasses.dataclass(frozen=True)
class _Sighting:
    """Internal container for an ingested conversation and associated metadata."""

    archive: TrajectoryArchive
    trace_ids: tuple[str, ...]
    case: EvalCase


class _ArchiveCollector(NullTrajectoryRecorder):
    """Recorder implementation collecting payload archives in memory during preview.

    Subclasses `NullTrajectoryRecorder`, the recorder base used during ingestion,
    and writes no trajectory index rows, so preview operations do not register
    as sweep runs.
    """

    def __init__(self, *, agent_name: str) -> None:
        self._agent_name = agent_name
        self.sightings: list[_Sighting] = []

    def add(
        self,
        *,
        trajectory_id: str,
        session_id: str | None,
        trace_ids: Sequence[str],
        status: IngestStatus,
        case: EvalCase | None = None,
        agent_revision: str = "",
    ) -> None:
        """Record an ingested conversation payload in memory.

        Args:
            trajectory_id: Unique trajectory identifier.
            session_id: Session identifier (unused; index rows are not recorded).
            trace_ids: Associated Cloud Trace IDs.
            status: Ingestion status of the conversation.
            case: Assembled evaluation case, or None if ingestion failed.
            agent_revision: Observed revision of the target agent.
        """
        del session_id  # Unused: preview records payloads without index rows.
        if case is None:
            return
        self.sightings.append(
            _Sighting(
                archive=build_archive(
                    case,
                    agent_name=self._agent_name,
                    trajectory_id=trajectory_id,
                    created_at=dt.datetime.now(dt.UTC),
                    partial=status is IngestStatus.PARTIAL,
                    agent_revision=agent_revision,
                ),
                trace_ids=tuple(trace_ids),
                case=case,
            )
        )


def _build_previewed_conversation(
    sighting: _Sighting, project_id: str
) -> PreviewedConversation:
    """Construct a `PreviewedConversation` from an ingested sighting.

    Args:
        sighting: Ingested conversation and metadata.
        project_id: GCP project ID for building trace links.

    Returns:
        A populated `PreviewedConversation` instance.
    """
    trajectory_id = sighting.archive.payload.trajectory_id
    return PreviewedConversation(
        trajectory_id=trajectory_id,
        case_view_path=CASE_VIEW_PATH.format(
            run=PREVIEW_RUN_SEGMENT, trajectory_id=trajectory_id
        ),
        turn_count=sighting.archive.payload.turn_count,
        first_user_message=_extract_first_user_message(sighting.case),
        trace_url=build_trace_console_url(project_id, sighting.trace_ids)
        or None,
    )


def _extract_first_user_message(case: EvalCase) -> str:
    """Extract and truncate the initial user message from an evaluation case.

    Args:
        case: Evaluation case containing conversation turns.

    Returns:
        The truncated user message, or an empty string if no user message exists.
    """
    agent_data = case.agent_data
    for turn in (agent_data.turns or []) if agent_data else []:
        for event in turn.events or []:
            content = event.content
            if content is None or content.role != "user":
                continue
            text = " ".join(
                part.text.strip() for part in (content.parts or []) if part.text
            ).strip()
            if text:
                return _truncate_first_message(text)
    return ""


def _truncate_first_message(text: str) -> str:
    """Truncate text to `FIRST_MESSAGE_CHARS` with an ellipsis if truncated.

    Args:
        text: Input text string.

    Returns:
        Truncated text string.
    """
    if len(text) <= FIRST_MESSAGE_CHARS:
        return text
    return text[:FIRST_MESSAGE_CHARS].rstrip() + "..."


def _create_default_fetcher(
    config: Config, selector_sql: str, *, recorder: NullTrajectoryRecorder
) -> BaseFetcher:
    """Construct the configured telemetry fetcher for preview execution.

    Args:
        config: Agent configuration.
        selector_sql: Raw selector SQL query.
        recorder: Recorder instance to collect ingested payloads.

    Returns:
        A configured BaseFetcher instance.
    """
    state = WorkflowState.from_config(config).model_dump()
    state["selector_sql"] = selector_sql
    return _common.build_fetcher(
        DetachedContext(state=state),
        recorder=recorder,
        job_timeout_ms=JOB_TIMEOUT_MS,
    )


fetcher_factory = _create_default_fetcher
"""Factory function for creating preview fetchers; swappable for testing."""

store_factory: Callable[[Config], TrajectoryStore] = (
    providers.build_unbound_provider("preview store_factory")
)
"""Trajectory store where a preview archives the conversations it tried.

A storage provider bound by `backends.configure_providers`. Standalone mode binds
it to the in-process store; the BigQuery backend binds it to AQuA's dataset.
"""
