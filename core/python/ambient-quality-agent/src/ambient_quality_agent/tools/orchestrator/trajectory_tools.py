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

"""Read tools over the trajectory store, for the dashboard and the harness.

Three tools driven either by the LLM or over the ``investigation_trajectories/*``
and ``trajectories/bundle`` routes in `core.command_routes` for the UI or CLI:

* ``count_investigation_trajectory_outcomes`` -- per-day counts of what became
  of what a sweep sampled. The chart behind "how much of the window did we
  actually look at".
* ``list_investigation_trajectories`` -- the investigation trajectories behind
  one bar, paginated, each with the ids it was built from and a console link to
  the conversation.
* ``get_trajectory_bundle`` -- one stored trajectory in full, for the
  dashboard's trajectory view.

Both build an agent-scoped `TrajectoryReader` from the effective config, so the
observed-agent name, dataset and region come from deploy config and the caller
never names them. The console URL is attached here rather than in the reader,
because it needs the project id and because deriving links at serialization
time is what keeps the stored rows free of URLs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent import providers
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.orchestrator.paging import (
    decode_page_token,
    encode_page_token,
)
from ambient_quality_agent.tools.trajectories import bundles
from ambient_quality_agent.tools.trajectories.models import (
    TrajectoryProcessingState,
)
from ambient_quality_agent.tools.trajectories.reader import (
    build_trace_console_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ambient_quality_agent.core.session_state import (
        ADKStateLike,
        LaunchContext,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        InvestigationTrajectory,
    )
    from ambient_quality_agent.tools.trajectories.reader import TrajectoryReader

logger = logging.getLogger(__name__)

TRAJECTORIES_PAGE_SIZE = 100
"""Number of trajectories returned per `list_investigation_trajectories` page.

Set to 100 to reduce round trips when plotting hundreds of daily trajectories.
Investigation trajectories contain lightweight IDs rather than full evaluation
rubrics, making larger pages efficient.
"""


# Initialized by `backends.configure_providers`. Unbound providers raise to catch missing backend setup early.
reader_factory: Callable[[ADKStateLike], TrajectoryReader] = (
    providers.build_unbound_provider("trajectory reader_factory")
)


def _parse_outcome(
    outcome: str,
) -> tuple[TrajectoryProcessingState | None, str | None]:
    """Parses an outcome filter string into a TrajectoryProcessingState.

    Args:
        outcome: Outcome filter string (e.g. "not_ingested", "no_insight",
            "in_an_insight", or "all"). An empty string or "all" matches any
            outcome.

    Returns:
        A tuple of (state, error_message). On success, error_message is None;
        on failure, state is None and error_message details the invalid value.
    """
    normalized = (outcome or "").strip()
    if normalized.lower() in ("", "all"):
        return None, None
    try:
        return TrajectoryProcessingState(normalized.lower()), None
    except ValueError:
        allowed = ", ".join(o.value for o in TrajectoryProcessingState)
        return None, f"invalid outcome {outcome!r}; expected one of: {allowed}."


async def count_investigation_trajectory_outcomes(
    tool_context: LaunchContext,
    *,
    window_start: str = "",
    window_end: str = "",
) -> dict[str, Any]:
    """Aggregates daily counts of sampled trajectories by processing outcome.

    Args:
        tool_context: Launch context providing access to session state.
        window_start: ISO instant lower bound for recorded trajectories; empty
            for no lower bound.
        window_end: ISO instant upper bound for recorded trajectories; empty
            for no upper bound.

    Returns:
        Dictionary mapping "days" to a list of daily outcome counts sorted
        oldest first ({"days": [{"day": ..., "outcome": ..., "trajectories": N}]}),
        or {"error": ...} on failure. Outcomes are "not_ingested", "no_insight",
        or "in_an_insight". Days without trajectories are omitted. Note that
        "no_insight" includes both passing trajectories and unclustered failures
        because the store retains only cluster-level results rather than
        per-trajectory evaluations.
    """
    state = tool_context.state
    try:
        # Offload synchronous BigQuery I/O to avoid blocking the ADK event loop.
        days = await asyncio.to_thread(
            lambda: reader_factory(state).count_daily_outcomes(
                window_start=window_start or None,
                window_end=window_end or None,
            )
        )
    except Exception as exc:
        logger.warning(
            "trajectories: count_investigation_trajectory_outcomes failed: %s",
            exc,
        )
        return {"error": f"failed to read trajectory outcomes: {exc}"}
    return {"days": [d.model_dump(mode="json") for d in days]}


async def list_investigation_trajectories(
    tool_context: LaunchContext,
    *,
    investigation_id: str = "",
    outcome: str = "",
    window_start: str = "",
    window_end: str = "",
    page_token: str = "",
) -> dict[str, Any]:
    """Lists investigation trajectories newest first, with pagination.

    Enables drill-down inspection for `count_investigation_trajectory_outcomes`
    by passing the same time window and outcome segment.

    Args:
        tool_context: Launch context providing access to session state.
        investigation_id: Sweep investigation ID to filter by; empty returns all sweeps.
        outcome: Outcome category filter ("not_ingested", "no_insight", or
            "in_an_insight"); empty returns all outcomes.
        window_start: ISO instant lower bound for recorded trajectories.
        window_end: ISO instant upper bound for recorded trajectories.
        page_token: Pagination token from a previous call's `next_page_token`;
            omit for the first page.

    Returns:
        Dictionary with "investigation_trajectories", "total" count, and "next_page_token"
        (or None if on the final page), or {"error": ...} on failure. Each
        trajectory includes conversation ID, ingestion status, source trace IDs,
        and a Cloud Trace console URL (or empty string if no trace IDs exist).
    """
    parsed, error = _parse_outcome(outcome)
    if error:
        return {"error": error}
    offset = decode_page_token(page_token)
    state = tool_context.state
    try:
        reader = reader_factory(state)
        rows, total = await asyncio.to_thread(
            lambda: reader.list_investigation_trajectories(
                investigation_id=investigation_id or None,
                outcome=parsed,
                window_start=window_start or None,
                window_end=window_end or None,
                limit=TRAJECTORIES_PAGE_SIZE,
                offset=offset,
            )
        )
    except Exception as exc:
        logger.warning(
            "trajectories: list_investigation_trajectories failed: %s", exc
        )
        return {"error": f"failed to list trajectories: {exc}"}
    # Traces are kept in the observed agent's project, not AQuA's.
    project_id = effective_config.load(state).resolve_observed_project_id()
    next_offset = offset + len(rows)
    return {
        "investigation_trajectories": [
            format_linked_trajectory(row, project_id) for row in rows
        ],
        "total": total,
        "next_page_token": (
            encode_page_token(next_offset) if next_offset < total else None
        ),
    }


def format_linked_trajectory(
    trajectory: InvestigationTrajectory, project_id: str
) -> dict[str, Any]:
    """Serializes a trajectory and attaches a Cloud Trace console link.

    Generates the same schema used by `insight_tools` for occurrences to ensure
    consistent representations across chart items and insight conversation lists.

    Args:
        trajectory: InvestigationTrajectory record to serialize.
        project_id: GCP project ID used to build trace console URLs.

    Returns:
        Serialized trajectory dictionary containing all trajectory fields plus
        a "console_url" key.
    """
    return {
        **trajectory.model_dump(mode="json"),
        "console_url": build_trace_console_url(
            project_id, trajectory.source_trace_ids
        ),
    }


async def get_trajectory_bundle(
    tool_context: LaunchContext,
    *,
    trajectory_id: str = "",
    investigation_id: str = "",
) -> dict[str, Any]:
    """Retrieves a trajectory bundle for the dashboard's trajectory view.

    Args:
        tool_context: Launch context providing access to session state.
        trajectory_id: Target trajectory ID (matches the trajectory ID in the
            URL, projected from occurrence trajectory IDs).
        investigation_id: Optional investigation ID used to attach trace links and
            investigation metadata. Omitted from the bundle lookup because
            trajectory bundles are stored independently of investigations.

    Returns:
        Dictionary mapping "trajectory_bundle" to the bundle, or
        {"error": ...} on failure. The bundle includes "status", "step_count", and
        "steps_returned" to distinguish unarchived, expired, and empty
        conversations.
    """
    if not trajectory_id:
        return {"error": "trajectory_id is required"}
    state = tool_context.state
    try:
        # Offload synchronous BigQuery I/O to avoid blocking the ADK event loop.
        found = await asyncio.to_thread(
            lambda: reader_factory(state).get_trajectory_bundles(
                [trajectory_id]
            )
        )
    except Exception as exc:
        logger.warning("trajectories: get_trajectory_bundle failed: %s", exc)
        return {"error": f"failed to read the conversation: {exc}"}

    bundle = bundles.format_bundle(trajectory_id, found.get(trajectory_id))
    trajectory = await _get_linked_trajectory(
        state, investigation_id, trajectory_id
    )
    if trajectory is not None:
        bundle["trajectory"] = trajectory
    return {"trajectory_bundle": bundle}


async def _get_linked_trajectory(
    state: ADKStateLike, investigation_id: str, trajectory_id: str
) -> dict[str, Any] | None:
    """Fetches sweep index metadata and console link for a conversation.

    Lookup is best-effort and non-fatal: it provides the console link and
    ingestion status for the trajectory view, but the conversation remains useful
    without them. Missing rows occur when the investigation/trajectory pair does not exist
    or when a URL is manually modified.

    Args:
        state: Session state used to initialize the trajectory reader and config.
        investigation_id: Sweep investigation ID to look up.
        trajectory_id: Target trajectory ID.

    Returns:
        Serialized trajectory dictionary with console link, or None if investigation_id
        is empty, the row is not found, or lookup fails.
    """
    if not investigation_id:
        return None
    try:
        rows = await asyncio.to_thread(
            lambda: reader_factory(state).get_investigation_trajectories(
                [(investigation_id, trajectory_id)]
            )
        )
    except Exception as exc:
        logger.warning(
            "trajectories: resolving %s failed: %s", trajectory_id, exc
        )
        return None
    row = rows.get((investigation_id, trajectory_id))
    if row is None:
        return None
    return format_linked_trajectory(
        row, effective_config.load(state).resolve_observed_project_id()
    )
