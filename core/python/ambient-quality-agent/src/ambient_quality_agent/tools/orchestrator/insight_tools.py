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

"""Tools for accessing the durable insight store for RCA, coding harness, and UI.

The correlation node writes deduplicated issues to BigQuery without surfacing
them to chat. Root-cause analysis workflows and the dashboard read stored
issues through these tools, accessible either via LLM tool calls or HTTP routes
in `core.command_routes`:

1. `list_insights`: Paginated triage summaries (label, status, occurrence
   statistics) omitting heavy rubric payloads.
2. `get_insight`: Detailed occurrence history for an issue, including full rubric
   examples and recorded root causes, or a single sweep's occurrence.
3. `dismiss_insight` / `merge_insight`: Operator actions from the dashboard.
   Reachable only via HTTP routes; not exposed to chat agents because mutations
   are restricted.

Readers instantiate an agent-scoped `InsightReader` and writers instantiate an
`InsightStore` using effective configuration, automatically resolving the
observed agent name, dataset, and region.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import TYPE_CHECKING, Any

from ambient_quality_agent import providers
from ambient_quality_agent.core.session_state import ADKStateLike, LaunchContext
from ambient_quality_agent.tools.insights.models import InsightStatus
from ambient_quality_agent.tools.insights.reader import InsightOrder
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.orchestrator import trajectory_tools
from ambient_quality_agent.tools.orchestrator.paging import (
    decode_page_token,
    encode_page_token,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ambient_quality_agent.tools.insights.reader import InsightReader
    from ambient_quality_agent.tools.insights.store import InsightStore

logger = logging.getLogger(__name__)

INSIGHTS_PAGE_SIZE = 30
"""Default page size for `list_insights` when unspecified."""

MAX_INSIGHTS_PAGE_SIZE = 100
"""Maximum page size allowed for `list_insights`.

Prevents single-query performance degradation by capping page size while
allowing callers to retrieve additional results using `next_page_token`.
"""

OCCURRENCES_PAGE_SIZE = 30
"""Default page size for occurrences returned by `get_insight`."""


# Bound at runtime by backends.configure_providers to ensure configured dependencies exist.
reader_factory: Callable[[ADKStateLike], InsightReader] = (
    providers.build_unbound_provider("insight reader_factory")
)
store_factory: Callable[[ADKStateLike], InsightStore] = (
    providers.build_unbound_provider("insight store_factory")
)


# Delegate trajectory reader access to trajectory_tools to maintain a single provider definition.


def _parse_statuses(
    status: str,
) -> tuple[list[InsightStatus] | None, str | None]:
    """Parses a status filter string into InsightStatus enums.

    An empty string or "all" applies no filter. Lifecycle names are accepted
    case-insensitively.

    Args:
        status: Lifecycle status name to filter by, "all", or an empty string.

    Returns:
        A tuple of (statuses, error_message). On success, error_message is None.
        When filtering is disabled, statuses is None.
    """
    normalized = (status or "").strip()
    if normalized.lower() in ("", "all"):
        return None, None
    try:
        return [InsightStatus(normalized.upper())], None
    except ValueError:
        allowed = ", ".join(s.value for s in InsightStatus)
        return None, f"invalid status {status!r}; expected one of: {allowed}."


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_day(
    day: str, statuses: list[InsightStatus] | None
) -> tuple[str | None, str | None]:
    """Parses a UTC day filter string into YYYY-MM-DD format.

    Requires strict YYYY-MM-DD formatting to prevent unhyphenated ISO dates
    from matching unexpected ranges. Malformed dates return an error to prevent
    unintended unfiltered queries. Combining RECURRING status with a day filter
    is rejected because recurrence is not timestamped to a single day.

    Args:
        day: Target date string formatted as YYYY-MM-DD, or an empty string.
        statuses: Active status filters to validate compatibility against.

    Returns:
        A tuple of (day_string, error_message). On success, error_message is None.
        When no date filter is requested, day_string is None.
    """
    normalized = (day or "").strip()
    if not normalized:
        return None, None
    try:
        valid = bool(_DAY.fullmatch(normalized)) and bool(
            dt.date.fromisoformat(normalized)
        )
    except ValueError:
        valid = False
    if not valid:
        return None, f"invalid day {day!r}; expected a UTC day as YYYY-MM-DD."
    if statuses and InsightStatus.RECURRING in statuses:
        return None, (
            "status RECURRING cannot be combined with a day: a day keeps the "
            "insights found (NEW) or resolved (RESOLVED) on it."
        )
    return normalized, None


def _parse_order(order_by: str) -> tuple[InsightOrder, str | None]:
    """Parses an ordering argument into an InsightOrder enum.

    Empty values resolve to the default order (RECENT). Unrecognized values
    return an error to prevent serving results with an unintended ranking.

    Args:
        order_by: Ordering strategy ("recent" or "impact"), or an empty string.

    Returns:
        A tuple of (order, error_message). On success, error_message is None.
    """
    normalized = (order_by or "").strip().lower()
    if not normalized:
        return InsightOrder.RECENT, None
    try:
        return InsightOrder(normalized), None
    except ValueError:
        allowed = ", ".join(o.value for o in InsightOrder)
        return (
            InsightOrder.RECENT,
            f"invalid order_by {order_by!r}; expected one of: {allowed}.",
        )


def _resolve_page_size(requested: int) -> int:
    """Resolves the page size for an insight query.

    Values less than or equal to zero use INSIGHTS_PAGE_SIZE. Values exceeding
    MAX_INSIGHTS_PAGE_SIZE are clamped to that maximum.

    Args:
        requested: Requested number of items per page.

    Returns:
        The resolved page size integer.
    """
    if requested <= 0:
        return INSIGHTS_PAGE_SIZE
    return min(requested, MAX_INSIGHTS_PAGE_SIZE)


def _parse_tri_state(value: str, name: str) -> tuple[bool | None, str | None]:
    """Parses a tri-state string argument into an optional boolean filter.

    Distinguishes True (matched), False (unmatched), and None (unfiltered).
    Empty strings represent unfiltered queries. Unrecognized values return an
    error to prevent returning unintended unfiltered supersets.

    Matches the encoding from ui.app._encode_tri_state and
    core.command_routes._encode_tri_state, duplicated here to keep UI and agent
    deployments decoupled without shared imports.

    Args:
        value: Input string to parse ('true', 'false', or '').
        name: Parameter name for formatting error messages.

    Returns:
        A tuple of (parsed_bool_or_none, error_message_or_none).
    """
    normalized = (value or "").strip().lower()
    if normalized == "":
        return None, None
    if normalized in ("true", "false"):
        return normalized == "true", None
    return None, f"invalid {name} {value!r}; expected 'true', 'false', or ''."


async def list_insights(
    tool_context: LaunchContext,
    *,
    status: str = "",
    run_id: str = "",
    has_root_cause: str = "",
    page_token: str = "",
    window_start: str = "",
    window_end: str = "",
    order_by: str = "",
    page_size: int = 0,
    day: str = "",
) -> dict[str, Any]:
    """List durable quality insights for the observed agent (paginated).

    Args:
        status: optional lifecycle filter -- ``NEW``, ``RECURRING``, or
            ``RESOLVED``; empty (the default) returns every status.
        run_id: keep only issues seen in that sweep (empty = every sweep).
        has_root_cause: ``"true"`` keeps only diagnosed issues, ``"false"`` only
            undiagnosed ones; empty (the default) returns both.
        page_token: opaque token from a previous call's ``next_page_token``;
            omit for the first page.
        window_start: ISO instant. Keep only issues last seen at or after it;
            empty for no lower bound.
        window_end: ISO instant. Keep only issues last seen at or before it;
            empty for no upper bound.
        order_by: how to rank the issues -- ``"recent"`` (the default) for last
            seen first, ``"impact"`` for most conversations affected first. The
            ranking is over everything that matches, so the first page is the
            top of the list rather than the top of one page of it.
        page_size: how many issues to return, up to `MAX_INSIGHTS_PAGE_SIZE`;
            0 (the default) takes `INSIGHTS_PAGE_SIZE`.
        day: a UTC day as ``YYYY-MM-DD``; keep only issues first found or
            resolved on it. With a day, ``status`` picks which of the two:
            ``NEW`` for found that day (whatever the status is now),
            ``RESOLVED`` for resolved that day; ``RECURRING`` is refused.
            Empty (the default) for no day.

    Returns ``{"insights": [...], "total": N, "conversations": M,
    "next_page_token": str | None}``; each insight carries its label, status,
    timestamps, occurrence stats and ``has_root_cause``, but not the rubric
    evidence (use `get_insight` for that).

    ``conversations`` is the distinct conversations behind every insight the
    filters match, not just the returned page, so a caller can say how much
    conversation the whole filtered set covers without summing what it was
    handed -- which would count one conversation once per sweep that saw it and
    once per insight it matches.
    """
    statuses, error = _parse_statuses(status)
    if error:
        return {"error": error}
    diagnosed, error = _parse_tri_state(has_root_cause, "has_root_cause")
    if error:
        return {"error": error}
    order, error = _parse_order(order_by)
    if error:
        return {"error": error}
    changed_on, error = _parse_day(day, statuses)
    if error:
        return {"error": error}
    offset = decode_page_token(page_token)
    state = tool_context.state
    filters: dict[str, Any] = {
        "statuses": statuses,
        "run_id": run_id or None,
        "window_start": window_start or None,
        "window_end": window_end or None,
        "has_root_cause": diagnosed,
        "day": changed_on,
    }
    try:
        # Offload synchronous BigQuery calls to a worker thread to keep the
        # asyncio event loop unblocked.
        summaries, total = await asyncio.to_thread(
            lambda: reader_factory(state).list_insights(
                limit=_resolve_page_size(page_size),
                offset=offset,
                order_by=order,
                **filters,
            )
        )
        conversations = await asyncio.to_thread(
            lambda: reader_factory(state).count_affected_conversations(
                **filters
            )
        )
    except Exception as exc:
        logger.warning("insights: list_insights failed: %s", exc)
        return {"error": f"failed to list insights: {exc}"}
    next_offset = offset + len(summaries)
    return {
        "insights": [s.model_dump(mode="json") for s in summaries],
        "total": total,
        "conversations": conversations,
        "next_page_token": (
            encode_page_token(next_offset) if next_offset < total else None
        ),
    }


async def get_insight(
    tool_context: LaunchContext,
    *,
    insight_id: str = "",
    run_id: str = "",
    include_traces: bool = True,
    include_trajectories: bool | None = None,
    include_edits: bool = True,
    page_token: str = "",
) -> dict[str, Any]:
    """Get one insight with its occurrences, rubric examples and root causes.

    Args:
        insight_id: the insight to fetch (required; from `list_insights`).
        run_id: return only that sweep's occurrence; empty returns the full
            occurrence history, newest first (paginated). The most recent run is
            already known from `list_insights`' ``last_run_id``, so there is
            no separate "latest" mode.
        include_traces: include each rubric's conversation trace (default True) --
            the large payloads a coding harness uses for root-cause analysis; set
            False to scan labels/reasoning cheaply without them.
        include_trajectories: resolve each occurrence's conversations to the
            traces they were built from and a console link into each. Defaults
            to following ``include_traces``, which is what a harness wants --
            the conversations and their provenance together, or neither.

            Separable because the two are different costs. A rubric's trace is
            a heavy payload already on the row and merely withheld; this is one
            extra query returning a handful of ids. A reader that wants a link
            to the conversation but not the conversation itself has to be able
            to ask for exactly that, and the dashboard is that reader.
        include_edits: include each proposed edit's ``before``/``after`` code
            (default True). False retains edit metadata without source bodies to
            reduce token consumption.
        page_token: opaque token from a previous call's ``next_page_token``; omit
            for the first page.

    Returns ``{"insight": {...}, "occurrences": [...], "root_causes": [...],
    "next_page_token": ...}``, or ``{"error": ...}`` when ``insight_id`` is
    missing or unknown.
    """
    if not insight_id:
        return {"error": "insight_id is required"}
    offset = decode_page_token(page_token)
    state = tool_context.state
    try:
        # Reuse a single reader to share the BigQuery client and cached table
        # existence probe across queries.
        reader = reader_factory(state)
        # Offload synchronous BigQuery calls to a worker thread to keep the
        # asyncio event loop unblocked.
        detail = await asyncio.to_thread(
            lambda: reader.get_insight_with_occurrences(
                insight_id=insight_id,
                run_id=run_id or None,
                limit=OCCURRENCES_PAGE_SIZE,
                offset=offset,
            )
        )
    except Exception as exc:
        logger.warning("insights: get_insight failed: %s", exc)
        return {"error": f"failed to get insight: {exc}"}
    if detail is None:
        return {"error": f"No insight found with id {insight_id!r}."}
    view, occurrences, total = detail
    resolve = (
        include_traces if include_trajectories is None else include_trajectories
    )
    trajectories = (
        await _get_trajectories(state, occurrences) if resolve else {}
    )
    root_causes = await _list_root_causes(reader, insight_id)
    # Traces are kept in the observed agent's project, not AQuA's.
    project_id = effective_config.load(state).resolve_observed_project_id()
    next_offset = offset + len(occurrences)
    return {
        "insight": view.model_dump(mode="json"),
        "occurrences": [
            _format_occurrence(
                o,
                include_traces=include_traces,
                trajectories=trajectories,
                project_id=project_id,
                root_causes=root_causes,
                include_edits=include_edits,
            )
            for o in occurrences
        ],
        # Return all insight root causes at the top level because sweep retries
        # generate new occurrence IDs, allowing diagnoses to outlive individual
        # sightings paged above.
        "root_causes": [
            _format_root_cause(r, include_edits) for r in root_causes
        ],
        "next_page_token": (
            encode_page_token(next_offset) if next_offset < total else None
        ),
    }


async def dismiss_insight(
    tool_context: LaunchContext, *, insight_id: str = ""
) -> dict[str, Any]:
    """Hides an insight from the triage list based on operator judgment.

    Args:
        tool_context: Execution context providing session and agent state.
        insight_id: Insight identifier to dismiss.

    Returns:
        Dictionary with {"dismissed": True} on success, or {"error": ...} if
        insight_id is missing or not found. Dismissing an already-dismissed
        insight succeeds as a no-op.
    """
    if not insight_id:
        return {"error": "insight_id is required"}
    state = tool_context.state
    try:
        # Offload synchronous BigQuery calls to a worker thread to keep the
        # asyncio event loop unblocked.
        found = await asyncio.to_thread(
            lambda: store_factory(state).dismiss_insight(insight_id)
        )
    except Exception as exc:
        logger.warning("insights: dismiss_insight failed: %s", exc)
        return {"error": f"failed to dismiss insight: {exc}"}
    if not found:
        return {"error": f"No insight found with id {insight_id!r}."}
    return {"dismissed": True}


async def merge_insight(
    tool_context: LaunchContext,
    *,
    insight_id: str = "",
    target_insight_id: str = "",
) -> dict[str, Any]:
    """Records an insight as a duplicate of another based on operator judgment.

    The duplicate insight retains its historical sightings but is hidden from
    triage lists. The target insight absorbs the duplicate's occurrence counts
    on read.

    Args:
        tool_context: Execution context providing session and agent state.
        insight_id: Identifier of the insight marked as duplicate.
        target_insight_id: Identifier of the primary insight being merged into.

    Returns:
        Dictionary with {"merged": True} on success, or {"error": ...} if an ID
        is missing, both IDs are identical, either ID is not found, or the target
        is already a duplicate (preventing pointer chains).
    """
    if not insight_id:
        return {"error": "insight_id is required"}
    if not target_insight_id:
        return {"error": "target_insight_id is required"}
    if insight_id == target_insight_id:
        # Prevent self-referential merge pointers that would hide the insight
        # without a valid target reference.
        return {"error": "an insight cannot be a duplicate of itself"}
    state = tool_context.state
    try:
        merged = await asyncio.to_thread(
            lambda: store_factory(state).merge_insight(
                insight_id=insight_id, target_insight_id=target_insight_id
            )
        )
    except Exception as exc:
        logger.warning("insights: merge_insight failed: %s", exc)
        return {"error": f"failed to merge insight: {exc}"}
    if not merged:
        return {
            "error": (
                f"Could not merge {insight_id!r} into {target_insight_id!r}: "
                "one of them does not exist, or the target is itself a duplicate."
            )
        }
    return {"merged": True}


async def _list_root_causes(reader: Any, insight_id: str) -> list[Any]:
    """Retrieves current root-cause records for an insight.

    Performs a best-effort read sharing the BigQuery client and table probe
    from the parent reader instance.

    Args:
        reader: Configured InsightReader instance.
        insight_id: Target insight identifier.

    Returns:
        List of root-cause records, or empty list on failure.
    """
    try:
        return await asyncio.to_thread(
            lambda: reader.list_root_causes(insight_id)
        )
    except Exception as exc:
        logger.warning(
            "insights: could not read an insight's root causes: %s", exc
        )
        return []


async def _get_trajectories(
    state: ADKStateLike, occurrences: Sequence[Any]
) -> dict[tuple[str, str], Any]:
    """Fetches trajectory rows for occurrences, keyed by run ID and trajectory ID.

    Retrieves trace IDs and console links for conversations where insights were
    found. Runs as a best-effort query: failures log a warning and return empty
    dictionaries so missing trajectory tables or records do not fail the primary
    insight request.

    Args:
        state: Session state used to instantiate the trajectory reader.
        occurrences: Sequence of occurrence records containing trajectory IDs.

    Returns:
        Mapping of (run_id, trajectory_id) tuples to trajectory records.
    """
    keys = [
        (occurrence.run_id, trajectory_id)
        for occurrence in occurrences
        for trajectory_id in occurrence.trajectory_ids
    ]
    if not keys:
        return {}
    try:
        return await asyncio.to_thread(
            lambda: trajectory_tools.reader_factory(state).get_trajectories(
                keys
            )
        )
    except Exception as exc:
        logger.warning(
            "insights: could not resolve an insight's trajectories: %s", exc
        )
        return {}


def _format_occurrence(
    occurrence: Any,
    *,
    include_traces: bool,
    trajectories: Mapping[tuple[str, str], Any],
    project_id: str,
    root_causes: Sequence[Any] = (),
    include_edits: bool = True,
) -> dict[str, Any]:
    """Serializes an occurrence, attaching resolved trajectories and matching root causes.

    Args:
        occurrence: InsightOccurrence model instance.
        include_traces: Whether to retain rubric conversation traces.
        trajectories: Mapping of (run_id, trajectory_id) to resolved trajectories.
        project_id: GCP project ID used to build console URLs.
        root_causes: Root-cause records for the parent insight; attached when
            matching the occurrence ID.
        include_edits: Whether to retain code diff snippets in root-cause edits.

    Returns:
        JSON-serializable dictionary representation of the occurrence.
    """
    data = occurrence.model_dump(mode="json")
    if not include_traces:
        for rubric in data.get("rubrics", []):
            rubric["trace"] = []
    resolved = [
        trajectories.get((occurrence.run_id, trajectory_id))
        for trajectory_id in occurrence.trajectory_ids
    ]
    data["trajectories"] = [
        trajectory_tools.format_linked_trajectory(found, project_id)
        for found in resolved
        if found
    ]
    data["root_causes"] = [
        _format_root_cause(record, include_edits)
        for record in root_causes
        if record.occurrence_id == occurrence.occurrence_id
    ]
    return data


def _format_root_cause(record: Any, include_edits: bool) -> dict[str, Any]:
    """Serializes a root-cause record to a dictionary.

    Args:
        record: RootCause model instance.
        include_edits: Whether to retain before/after code snippets in edits.

    Returns:
        JSON-serializable dictionary with an added edit_count field.
    """
    data = record.model_dump(mode="json")
    data["edit_count"] = len(record.edits)
    if not include_edits:
        data["edits"] = [
            {
                key: value
                for key, value in edit.items()
                if key not in ("before", "after")
            }
            for edit in data["edits"]
        ]
    return data
