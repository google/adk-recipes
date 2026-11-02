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

"""Provides full conversation history for root-cause analysis of insights.

Joins insight trajectory IDs with archived conversation payloads to deliver
complete, turn-by-turn conversation logs. Distinguishes unarchived, expired,
and lossy (partial/truncated) conversations, and supports paginated responses.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import TYPE_CHECKING, Any, NamedTuple

# Runtime import, not TYPE_CHECKING: ADK resolves a tool's annotations with
# `typing.get_type_hints` when it builds the function declaration, so a deferred
# name raises NameError and takes the whole agent down.
from ambient_quality_agent.core.session_state import ADKStateLike, LaunchContext
from ambient_quality_agent.tools.orchestrator import (
    insight_tools,
    trajectory_tools,
)
from ambient_quality_agent.tools.orchestrator.paging import (
    decode_page_token,
    encode_page_token,
)
from ambient_quality_agent.tools.trajectories import payloads
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ambient_quality_agent.tools.insights.models import InsightOccurrence
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )

logger = logging.getLogger(__name__)

MAX_OCCURRENCES = 10
"""Maximum sightings read when analyzing an entire insight.

Bounds the read across evaluation sweeps for long-lived insights. The response
reports the actual count covered, keeping this limit out of the
``get_full_trajectories`` docstring.
"""

MAX_TRAJECTORIES_PER_PAGE = 10
"""Maximum number of full conversations returned per page to limit response size."""

PAGE_BYTE_BUDGET = 4 * 1024 * 1024
"""Maximum serialized byte budget per page.

Caps token/payload size when conversations contain large turns, while still
allowing at least one conversation per page to ensure forward progress.
"""


async def get_full_trajectories(
    tool_context: LaunchContext,
    *,
    insight_id: str = "",
    occurrence_id: str = "",
    page_token: str = "",
) -> dict[str, Any]:
    """Retrieves full conversations behind an insight or a specific sighting.

    Use during root-cause analysis to inspect complete conversation trajectories
    rather than capped rubric samples from ``get_insight``. Call ``get_insight``
    first to obtain an ``insight_id`` and sighting ``occurrence_id`` values.

    Scope follows ``occurrence_id``: provide one to read that single sighting,
    or omit it to read all sightings for ``insight_id`` (newest first). At least
    one ID is required.

    Args:
        tool_context: Context holding active session state.
        insight_id: Insight to inspect. Optional when ``occurrence_id`` is provided.
        occurrence_id: Sighting to read. Omit to read all sightings for the insight.
        page_token: Pagination token from a prior ``next_page_token``. Omit for
            the initial page.

    Returns:
        A dictionary containing:
        * ``trajectories``: List of trajectory objects, each containing:
          - ``trajectory_id``: Unique conversation identifier.
          - ``status``: Archive status (``ingested``, ``partial``,
            ``truncated``, or ``not_archived``).
          - ``turns``: Ordered list of turn event objects.
          - ``turn_count``: Total number of recorded turns.
          - ``turns_returned``: Number of returned turns (fewer than
            ``turn_count`` indicates expired turns).
          - ``agents``: Agent configuration captured at archive time.
          - ``recorded_at``: ISO timestamp of archiving, or None if unarchived.
          - ``occurrence_id`` and ``agent_revision``: Present only under
            ``insight`` scope to identify the sighting and build for this
            specific conversation. Pass this revision when inspecting source.
        * ``next_page_token``: Token for fetching the next page, or None if done.
        * ``insight_id``: The insight the conversations belong to.
        * ``occurrence_id``: Sighting the conversations were read from; None
          under ``insight`` scope.
        * ``scope``: ``occurrence`` for a single sighting, ``insight`` for all
          sightings.
        * ``agent_revision``: Deployment revision for the sighting. Pass to
          source-browsing tools to inspect the matching source build. Empty
          string indicates an unnamed revision; None under ``insight`` scope
          where sightings may span multiple builds (use the per-trajectory
          ``agent_revision`` instead).
        * ``total_trajectories``: Count of conversations in scope, across pages.
        * ``occurrences_read``: Number of sightings whose conversations are in
          scope; 1 under ``occurrence`` scope.
        * ``total_occurrences``: Total sightings for the insight; None under
          ``occurrence`` scope, which reads only the named sighting.
        * ``occurrences_truncated``: True when ``total_occurrences`` exceeds
          ``occurrences_read``, indicating conversations cover only recent
          sightings. Never state how often the issue happened from this
          response; use ``get_insight`` for complete occurrence totals.
        * ``error``: Error message if the insight or occurrence cannot be found
          or loaded.

    Note:
        Do not treat ``partial``, ``truncated``, or ``not_archived``
        trajectories as complete evidence; explicitly identify missing data.
    """
    state = tool_context.state
    wanted_insight = insight_id.strip()
    wanted_occurrence = occurrence_id.strip()
    if not wanted_insight and not wanted_occurrence:
        return {
            "error": (
                "name what to read: an insight_id for every conversation behind"
                " the insight, or an occurrence_id for a single sighting."
                " get_insight returns both."
            )
        }
    offset = decode_page_token(page_token)
    try:
        # Offload synchronous BigQuery I/O to a worker thread to avoid blocking
        # the ADK async event loop.
        read = await asyncio.to_thread(
            _list_occurrences_in_scope,
            state,
            insight_id=wanted_insight,
            occurrence_id=wanted_occurrence,
        )
    # Catch expected BigQuery API and authentication errors; unexpected
    # exceptions indicate bugs and should propagate.
    except (
        api_exceptions.GoogleAPIError,
        auth_exceptions.GoogleAuthError,
    ) as exc:
        logger.warning(
            "rca: reading %s failed: %s", wanted_insight, exc, exc_info=True
        )
        return {"error": f"failed to read insight {wanted_insight!r}: {exc}"}
    if read is None:
        return {"error": f"No insight found with id {wanted_insight!r}."}
    occurrences, total_occurrences = read
    if wanted_occurrence and not occurrences:
        return {
            "error": _build_missing_occurrence_error(
                wanted_occurrence, wanted_insight
            )
        }

    scoped = occurrences[0] if wanted_occurrence else None
    resolved_id = str(
        wanted_insight or (scoped.insight_id if scoped else "") or ""
    )
    conversation_ids = _extract_conversation_ids(occurrences)
    # Single-sighting reads omit total_occurrences to avoid misrepresenting overall insight frequency.
    counts = _OccurrenceCounts(
        read=len(occurrences), total=None if scoped else total_occurrences
    )
    page = functools.partial(
        _build_page,
        insight_id=resolved_id,
        occurrence=scoped,
        counts=counts,
        offset=offset,
        total=len(conversation_ids),
    )
    page_ids = conversation_ids[offset : offset + MAX_TRAJECTORIES_PER_PAGE]
    if not page_ids:
        return page([])
    try:
        archives = await asyncio.to_thread(
            lambda: trajectory_tools.reader_factory(state).get_archives(
                page_ids
            )
        )
    except (
        api_exceptions.GoogleAPIError,
        auth_exceptions.GoogleAuthError,
    ) as exc:
        logger.warning(
            "rca: reading the conversations of insight %s failed: %s",
            resolved_id,
            exc,
            exc_info=True,
        )
        return {"error": f"failed to read the conversations: {exc}"}

    # Single-sighting responses provide occurrence and revision at the top level.
    sources = None if scoped else _build_trajectory_sources(occurrences)
    entries = _trim_to_byte_budget(
        _format_trajectory_entry(
            trajectory_id,
            archives.get(trajectory_id),
            source=sources.get(trajectory_id) if sources else None,
        )
        for trajectory_id in page_ids
    )
    return page(entries)


class _OccurrenceCounts(NamedTuple):
    """Sightings read count versus total sightings available for an insight.

    ``total`` is None for single-sighting reads where total counts are not queried.
    """

    read: int
    total: int | None


class _TrajectorySource(NamedTuple):
    """Sighting ID and deployment revision associated with a conversation."""

    occurrence_id: str
    agent_revision: str


def _build_missing_occurrence_error(occurrence_id: str, insight_id: str) -> str:
    """Builds the error message for an unmatched ``occurrence_id``.

    Distinguishes missing sightings from empty results so invalid IDs are obvious.

    Args:
        occurrence_id: Sighting ID that matched nothing.
        insight_id: Optional insight ID filter.

    Returns:
        Formatted error message directing callers to ``get_insight``.
    """
    on_insight = f" on insight {insight_id!r}" if insight_id else ""
    return (
        f"No occurrence {occurrence_id!r}{on_insight}; get_insight lists the"
        " occurrence_id of each sighting."
    )


def _list_occurrences_in_scope(
    state: ADKStateLike,
    *,
    insight_id: str,
    occurrence_id: str,
) -> tuple[list[InsightOccurrence], int] | None:
    """Reads occurrences in scope, ordered newest first.

    Globally unique ``occurrence_id`` values resolve directly without verifying
    the parent insight.

    Args:
        state: Session state used to construct the insight reader.
        insight_id: Insight identifier to read, or empty if ``occurrence_id`` is provided.
        occurrence_id: Sighting ID to read, or empty for all sightings of the insight.

    Returns:
        Tuple of ``(occurrences, total_occurrences)``, or None if the insight does
        not exist. ``total_occurrences`` includes sightings beyond ``MAX_OCCURRENCES``.
    """
    reader = insight_tools.reader_factory(state)
    if occurrence_id:
        return reader.list_occurrences(
            insight_id=insight_id or None,
            occurrence_id=occurrence_id,
            limit=1,
            offset=0,
        )
    if reader.get_insight(insight_id) is None:
        return None
    return reader.list_occurrences(
        insight_id=insight_id, limit=MAX_OCCURRENCES, offset=0
    )


def _build_trajectory_sources(
    occurrences: Sequence[InsightOccurrence],
) -> dict[str, _TrajectorySource]:
    """Maps each trajectory ID to its most recent sighting and deployment revision.

    Matches the deduplication order in ``_extract_conversation_ids`` by attributing recurring
    conversations to their latest occurrence.

    Args:
        occurrences: Insight occurrences ordered newest first.

    Returns:
        Dictionary mapping trajectory IDs to their ``_TrajectorySource``.
    """
    sources: dict[str, _TrajectorySource] = {}
    for occurrence in occurrences:
        source = _TrajectorySource(
            occurrence.occurrence_id, occurrence.agent_revision
        )
        for trajectory_id in occurrence.trajectory_ids:
            sources.setdefault(trajectory_id, source)
    return sources


def _extract_conversation_ids(
    occurrences: Sequence[InsightOccurrence],
) -> list[str]:
    """Extracts unique trajectory IDs from occurrences, preserving first-seen order.

    Order preservation ensures stable offset pagination across consecutive requests.

    Args:
        occurrences: Insight occurrences ordered newest first.

    Returns:
        Deduplicated list of trajectory IDs.
    """
    return list(
        dict.fromkeys(
            trajectory_id
            for occurrence in occurrences
            for trajectory_id in occurrence.trajectory_ids
        )
    )


def _format_trajectory_entry(
    trajectory_id: str,
    archive: TrajectoryArchive | None,
    *,
    source: _TrajectorySource | None,
) -> dict[str, Any]:
    """Formats a TrajectoryArchive into a structured dictionary.

    The body is `payloads.format_archive`, which the dashboard's case route returns
    too, so the agent and the dashboard cannot come to describe the same
    conversation differently. What is added here is the sighting it came from,
    which only a question about an insight has to hand.

    Args:
        trajectory_id: Unique trajectory identifier.
        archive: Archive payload containing manifest and turns, or None if unarchived.
        source: Sighting and revision for this conversation, or None if handled
            at the response level.

    Returns:
        Dictionary representing the trajectory and its archive status.
    """
    entry = payloads.format_archive(trajectory_id, archive)
    if source is None:
        return entry
    # Ahead of the archive fields, as it was when this dict was built by hand:
    # attribution answers "whose evidence is this" and reads first.
    return {
        "trajectory_id": entry["trajectory_id"],
        "occurrence_id": source.occurrence_id,
        "agent_revision": source.agent_revision,
        **{k: v for k, v in entry.items() if k != "trajectory_id"},
    }


def _trim_to_byte_budget(
    entries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filters entries to fit within `PAGE_BYTE_BUDGET`, always retaining at least one.

    Protects model context window limits against oversized conversations while
    guaranteeing page progress.

    Args:
        entries: Candidate trajectory entries for the page.

    Returns:
        List of entries within the byte budget.
    """
    kept: list[dict[str, Any]] = []
    used = 0
    for entry in entries:
        size = len(json.dumps(entry, default=str).encode("utf-8"))
        if kept and used + size > PAGE_BYTE_BUDGET:
            break
        kept.append(entry)
        used += size
    return kept


def _build_page(
    entries: list[dict[str, Any]],
    *,
    insight_id: str,
    occurrence: InsightOccurrence | None,
    counts: _OccurrenceCounts,
    offset: int,
    total: int,
) -> dict[str, Any]:
    """Constructs a paginated response dictionary.

    Args:
        entries: Trajectory entries included in the current page.
        insight_id: Insight identifier being analyzed.
        occurrence: Sighting the conversations were read from, or None for
            insight-level scope.
        counts: Sightings read count and total available count.
        offset: Starting trajectory index for the current page.
        total: Total number of trajectories in scope.

    Returns:
        Dictionary with paginated results and optional next page token.
    """
    next_offset = offset + len(entries)
    return {
        "trajectories": entries,
        "next_page_token": (
            encode_page_token(next_offset) if next_offset < total else None
        ),
        "insight_id": insight_id,
        "occurrence_id": occurrence.occurrence_id if occurrence else None,
        "scope": "occurrence" if occurrence else "insight",
        # Multi-sighting responses omit top-level revision; each trajectory carries its own.
        "agent_revision": occurrence.agent_revision if occurrence else None,
        "total_trajectories": total,
        "occurrences_read": counts.read,
        "total_occurrences": counts.total,
        "occurrences_truncated": counts.total is not None
        and counts.total > counts.read,
    }
