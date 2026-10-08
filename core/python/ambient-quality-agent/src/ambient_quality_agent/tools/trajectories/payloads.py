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

"""Turning an assembled `EvalCase` into the rows that archive it.

One conversation becomes one manifest row and a row per turn, bundled as a
`TrajectoryArchive` so the manifest's `turn_count` cannot disagree with the
turns beside it -- the two are produced together, here, and nowhere else.

The only judgement this module makes is the **write cap**: a turn too large for
a load job is left out and the copy is marked `truncated`. Everything else is
kept, because deciding how much of a conversation is worth reading belongs to
the node that reads it, where the model's context is known.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.trajectories.models import (
    PayloadStatus,
    TrajectoryPayload,
    TrajectoryPayloadTurn,
)

if TYPE_CHECKING:
    import datetime as dt

    from agentplatform._genai.types import EvalCase

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 10 * 1024 * 1024
"""Largest JSON value stored in one column, in bytes.

A guard against the load job's hard ceiling, which is **100 MB per
newline-delimited JSON row**
(https://docs.cloud.google.com/bigquery/quotas#load_job_limits). Ten megabytes
leaves room for the rest of the row and for JSON escaping, and is two hundred
times a whole typical trajectory (~50 KB), so it fires only for a single
pathological turn -- which is in any case far past what the model that would
read it can accept.

Not configurable, unlike the *read* budget: this one exists to keep a write
from failing, and that ceiling is BigQuery's rather than ours.
"""


@dataclasses.dataclass(frozen=True)
class TrajectoryArchive:
    """One conversation's stored copy: the manifest and its turns together.

    A bundle rather than two loose lists, because `turn_count` is a promise
    about the rows beside it and a reader uses it to tell a short conversation
    from a partly-written one. Built in one place so the promise cannot be made
    by one caller and broken by another.
    """

    payload: TrajectoryPayload
    """The manifest row."""

    turns: tuple[TrajectoryPayloadTurn, ...]
    """Its turns, in `turn_index` order, and exactly `payload.turn_count` of
    them."""


def build_archive(
    case: EvalCase,
    *,
    agent_name: str,
    trajectory_id: str,
    created_at: dt.datetime,
    partial: bool,
    agent_revision: str = "",
) -> TrajectoryArchive:
    """Build the archive of one assembled case.

    Args:
        case: The case ingestion built. Its ``agent_data`` is what is stored;
            the rest of the case is derived from telemetry the store already
            holds, or is the evaluator's own input.
        agent_name: Observed agent, the first part of the key.
        trajectory_id: The conversation, the second part. Taken from the
            caller rather than from ``case.eval_case_id`` so one value keys
            the payload and the `Trajectory` row that points at it.
        created_at: When this copy is being stored; shared by the manifest and
            every turn, so a conversation lands in one partition and expires in
            one piece.
        partial: Whether ingestion assembled the case from lossy telemetry.
            Carried through to `PayloadStatus.PARTIAL`, which is what makes a
            later, better copy an upgrade rather than a duplicate.
        agent_revision: The deployment that served the conversation, stored
            beside it so the copy says which build produced what it holds.
            Empty is stored as ``None``, the source's single unnamed revision.

    Returns:
        The manifest and its turns. A case with no turns still yields a
        manifest, with ``turn_count`` zero: the conversation was sampled and
        assembled, and an empty one is a fact about the telemetry rather than a
        reason to store nothing.
    """
    agent_data = case.agent_data
    agents, agents_truncated = _apply_write_cap(
        _dump_to_json_dict(agent_data.agents)
        if agent_data and agent_data.agents
        else None,
        what=f"agents of {trajectory_id}",
    )

    turns: list[TrajectoryPayloadTurn] = []
    turns_truncated = False
    for index, turn in enumerate(agent_data.turns or [] if agent_data else []):
        # `ConversationTurn.turn_index` is optional in the SDK and the column
        # is not: an unindexed turn has no place in the key and none in the
        # read order. The turn's position in the conversation is the answer
        # that is always available and always right.
        dumped, truncated = _apply_write_cap(
            _dump_to_json_dict(turn), what=f"turn {index} of {trajectory_id}"
        )
        turns_truncated = turns_truncated or truncated
        if dumped is None:
            continue
        turns.append(
            TrajectoryPayloadTurn(
                agent_name=agent_name,
                trajectory_id=trajectory_id,
                turn_index=index,
                created_at=created_at,
                turn=dumped,
            )
        )

    return TrajectoryArchive(
        payload=TrajectoryPayload(
            agent_name=agent_name,
            trajectory_id=trajectory_id,
            created_at=created_at,
            status=_resolve_payload_status(
                partial=partial, truncated=agents_truncated or turns_truncated
            ),
            turn_count=len(turns),
            agents=agents,
            agent_revision=agent_revision or None,
        ),
        turns=tuple(turns),
    )


def _resolve_payload_status(*, partial: bool, truncated: bool) -> PayloadStatus:
    """Resolves payload status, prioritizing lossy telemetry over write cap truncation.

    Partial telemetry can be repaired in future sweeps, whereas write cap truncation
    cannot be repaired by re-ingesting.

    Args:
        partial: Whether source telemetry was incomplete.
        truncated: Whether conversation content exceeded write caps.

    Returns:
        The resolved PayloadStatus enum value.
    """
    if partial:
        return PayloadStatus.PARTIAL
    return PayloadStatus.TRUNCATED if truncated else PayloadStatus.INGESTED


def _dump_to_json_dict(value: Any) -> dict[str, Any]:
    """Converts a Pydantic model or mapping to a JSON-compatible dictionary.

    Omits unset fields to minimize storage size.

    Args:
        value: Model instance or dictionary to serialize.

    Returns:
        JSON-compatible dictionary.
    """
    if isinstance(value, dict):
        return {key: _dump_to_json_dict(item) for key, item in value.items()}
    return value.model_dump(mode="json", exclude_none=True)


def _apply_write_cap(
    value: dict[str, Any] | None, *, what: str
) -> tuple[dict[str, Any] | None, bool]:
    """Drops JSON objects exceeding the per-column write cap MAX_JSON_BYTES.

    Oversized objects are dropped completely rather than trimmed to avoid
    corrupting structured turn events.

    Args:
        value: JSON dictionary to check.
        what: Context description for logging when the cap is exceeded.

    Returns:
        Tuple of (retained dictionary or None, whether value was dropped).
    """
    if value is None:
        return None, False
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size <= MAX_JSON_BYTES:
        return value, False
    logger.warning(
        "Dropping %s from the archive: %d bytes exceeds the %d-byte write cap.",
        what,
        size,
        MAX_JSON_BYTES,
    )
    return None, True


NOT_ARCHIVED = "not_archived"
"""Status for a conversation id with no manifest at all.

Deliberately outside `PayloadStatus`: that enum describes a copy that exists
and how complete it is, and "there is no copy" is a different kind of answer.
Reported rather than omitted so a caller can tell it apart from a conversation
that was archived and turned out to be empty.
"""


def format_archive(
    trajectory_id: str, archive: TrajectoryArchive | None
) -> dict[str, Any]:
    """Formats an archived conversation into the wire structure shared by readers.

    Args:
        trajectory_id: Conversation identifier.
        archive: Assembled TrajectoryArchive instance, or None if unarchived.

    Returns:
        Dictionary containing trajectory status, turn counts, turns, and metadata.
    """
    if archive is None:
        return {
            "trajectory_id": trajectory_id,
            "status": NOT_ARCHIVED,
            "turn_count": 0,
            "turns_returned": 0,
            "turns": [],
            "agents": None,
            "recorded_at": None,
        }
    payload = archive.payload
    return {
        "trajectory_id": trajectory_id,
        "status": payload.status.value,
        "turn_count": payload.turn_count,
        "turns_returned": len(archive.turns),
        "turns": [turn.turn for turn in archive.turns],
        "agents": payload.agents,
        "recorded_at": payload.created_at.isoformat(),
    }
