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

"""Summarizes the source code snapshot for the dashboard and the CLI.

Source store reads execute in the backend service because the UI container
lacks access to the store and the agent configuration.

The summary exposes manifest counters (`truncated_files`, `omitted_files`) to
confirm that the full repository was published and is available for citations.
Without a revision, only the newest complete revision (containing a finalized
manifest) is reported, ensuring partial or incomplete uploads are ignored. The
CLI names a revision to ask whether that one is already published.

Reasons for unavailable snapshots are returned as user-facing copy for display
in the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.objects import store as objects_store
from ambient_quality_agent.tools.source_code import reader as source_reader

logger = logging.getLogger(__name__)

SOURCE_UNCONFIGURED = (
    "No source snapshot bucket is configured for this deployment, so the "
    "observed agent's code cannot be read and findings cite no source line."
)
"""Message returned when there is no source store: `AQA_SOURCE_GCS_BUCKET` is unset.

Contains no deployment-specific parameters, allowing test mocks to reuse it.
"""


def _render_unpublished_reason(location: str) -> str:
    """Explains why a source snapshot is missing from an existing store.

    Args:
        location: Where the agent's snapshots are published.

    Returns:
        User-facing message explaining that the snapshot has not been published.
    """
    return (
        f"No complete source snapshot has been published to {location} yet. "
        "Publishing runs when the observed agent is deployed or attached, or "
        "with `agents-cli aqua publish-source`."
    )


async def get_source_snapshot(
    tool_context: LaunchContext, *, agent_name: str = "", revision: str = ""
) -> dict[str, Any]:
    """Checks whether an observed agent's source code is available.

    Read failures are returned as unavailable states so the dashboard can
    display citation status gracefully.

    Args:
        tool_context: Launch context containing runtime state and configuration.
        agent_name: Agent whose snapshots to report; empty for the agent the
            deployment watches.
        revision: Revision to report; empty for the newest complete one.

    Returns:
        A dictionary containing ``{"available": True, ...}`` with the chosen
        snapshot's manifest summary, or ``{"available": False, "reason": ...}``
        when source code is inaccessible.
    """
    reader = (
        source_reader.reader_factory(tool_context.state)
        if not agent_name
        else _create_agent_reader(agent_name)
    )
    if reader is None:
        return {"available": False, "reason": SOURCE_UNCONFIGURED}

    try:
        # Offload synchronous store calls to a thread to avoid blocking the event loop.
        summary = await asyncio.to_thread(_summarize_snapshot, reader, revision)
    except source_reader.READ_ERRORS as exc:
        logger.warning("source snapshot: read failed: %s", exc)
        return {
            "available": False,
            "reason": f"Could not read {reader.location}: {exc}",
        }

    if summary is None:
        return {
            "available": False,
            "reason": _render_unpublished_reason(reader.location),
        }
    return summary


def _create_agent_reader(
    agent_name: str,
) -> source_reader.SourceSnapshotReader | None:
    """Creates a reader of a named agent's snapshots in the source store.

    Args:
        agent_name: Observed agent whose snapshots to read.

    Returns:
        The reader, or None if there is no source store.
    """
    store = objects_store.source_store_factory()
    if store is None:
        return None
    return source_reader.SourceSnapshotReader(
        store=store, agent_name=agent_name
    )


def _summarize_snapshot(
    reader: source_reader.SourceSnapshotReader, revision: str
) -> dict[str, Any] | None:
    """Summarizes one complete snapshot for the dashboard.

    Executes synchronously in a worker thread to batch store round trips into a
    single thread dispatch. Without a revision, the newest one that has a valid
    manifest.

    ``revision_count`` includes all published prefixes, complete or incomplete,
    to reflect the total retained history.

    Args:
        reader: Reader of the agent's snapshots.
        revision: Revision to summarize; empty for the newest complete one.

    Returns:
        A dictionary summarizing the snapshot, or None if it is not complete.
    """
    revisions = reader.list_revisions()
    chosen = revision or next(
        (r for r in revisions if reader.load_manifest(r)), None
    )
    if chosen is None:
        return None
    manifest = reader.load_manifest(chosen)
    if manifest is None:
        return None
    return {
        "available": True,
        "revision": chosen,
        "revision_count": len(revisions),
        "created_at": manifest.created_at,
        "agent_directory": manifest.agent_directory,
        "file_count": len(manifest.files),
        "total_bytes": manifest.total_bytes,
        "truncated_files": manifest.truncated_files,
        "omitted_files": manifest.omitted_files,
        "uri": reader.build_manifest_uri(chosen),
    }
