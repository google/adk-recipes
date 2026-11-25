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

"""AQuA's additions to a validated document: errors and missing-data markers.

The order in which a document goes through this package::

    parse_document ─► validate_document
        ─► (the caller resolves the id and copies attachments)
        ─► annotate_document ─► IngestStatus

Validation enforces the producer `extra.aqua` schema and rejects unknown keys
on input. This module expands producer `missing` field names and writes AQuA's
annotation keys under `extra.aqua` after validation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aqua_atif.errors import add_errors
from aqua_atif.markers import (
    IngestStatus,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    add_markers,
    compute_ingest_status,
    find_missing_markers,
)
from aqua_atif.paths import (
    get_aqua,
    list_holders,
)


def annotate_document(
    document: dict[str, Any],
    *,
    markers: Iterable[PendingMissingMarker] = (),
) -> IngestStatus:
    """Adds AQuA's errors and missing-data markers to a document, in place.

    1. Expands producer-written string entries in `extra.aqua.missing` into
       `not_recorded` marker objects.
    2. Writes `extra.aqua.error` on each failed result, step and trajectory.
    3. Adds `markers`: what earlier stages found, such as parsing,
       validation and the source's unread references.
    4. Adds the markers for data the document does not record.
    5. Computes the ingest status from all markers.

    Re-running on a stored document replaces errors and merges markers. Earlier
    markers stay, even when a new run would not derive them.

    Args:
        document: Root trajectory of a validated document, changed in place.
        markers: Markers found before this stage.

    Returns:
        `partial` or `ingested`.
    """
    _expand_producer_missing(document)
    add_errors(document)
    add_markers(document, markers)
    add_markers(document, find_missing_markers(document))
    return compute_ingest_status(document)


def _expand_producer_missing(document: dict[str, Any]) -> None:
    """Expands string entries in `extra.aqua.missing` into `not_recorded` dicts.

    Producers write field names such as `["message"]`; storage holds
    `[{"path": "message", "reason": "not_recorded"}]`. Already-expanded marker
    dicts stay untouched, and duplicates are dropped in order.

    Args:
        document: Root trajectory, changed in place.
    """
    for holder in list_holders(document):
        aqua = get_aqua(holder.value)
        if aqua is None:
            continue
        raw = aqua.get("missing")
        if not isinstance(raw, list) or not any(
            isinstance(item, str) for item in raw
        ):
            continue
        expanded: list[dict[str, str]] = []
        seen: set[MissingMarker] = set()
        for item in raw:
            marker = (
                MissingMarker(item, Reason.NOT_RECORDED)
                if isinstance(item, str) and item
                else MissingMarker.from_dict(item)
            )
            if marker is not None and marker not in seen:
                seen.add(marker)
                expanded.append(marker.to_dict())
        aqua["missing"] = expanded


def resolve_agent_revision(trajectory: Mapping[str, Any]) -> str | None:
    """Resolves the revision of the agent that ran one trajectory.

    The root and each embedded subagent follow the same rule, so a subagent's
    findings carry its own revision rather than the root's.

    Args:
        trajectory: A root or embedded trajectory.

    Returns:
        A non-empty `agent.version`, otherwise None.
    """
    agent = trajectory.get("agent")
    version = agent.get("version") if isinstance(agent, Mapping) else None
    return version if isinstance(version, str) and version else None
