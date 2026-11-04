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

"""Builds and formats `TrajectoryBundle` objects from ingested evaluation cases.

One trajectory becomes one `TrajectoryManifest` and one `TrajectoryStep` per
conversation turn, paired in a `TrajectoryBundle` so `manifest.step_count`
always matches the length of `steps`.

Applies the per-column write cap (`MAX_JSON_BYTES`): any step or agent
configuration exceeding the cap is omitted and marks the manifest
`TrajectoryStatus.TRUNCATED`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.trajectories.models import (
    TrajectoryManifest,
    TrajectoryStatus,
    TrajectoryStep,
)

if TYPE_CHECKING:
    import datetime as dt

    from agentplatform._genai.types import EvalCase

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 10 * 1024 * 1024
"""Maximum size in bytes of a JSON value stored in one column.

Guards against BigQuery's 100 MB per-row load-job limit
(https://docs.cloud.google.com/bigquery/quotas#load_job_limits), leaving
headroom for the rest of the row and JSON escaping.
"""


@dataclasses.dataclass(frozen=True)
class TrajectoryBundle:
    """A stored trajectory's manifest and its ordered steps.

    Pairs `manifest` with `steps` so `manifest.step_count` is guaranteed to
    equal `len(steps)` at write time, allowing readers to distinguish short
    trajectories from ones with missing or expired steps.
    """

    manifest: TrajectoryManifest
    """Metadata header for the stored trajectory."""

    steps: tuple[TrajectoryStep, ...]
    """Conversation steps in `step_index` order."""


def build_bundle(
    case: EvalCase,
    *,
    observed_agent_key: str,
    trajectory_id: str,
    created_at: dt.datetime,
    partial: bool,
    agent_revision: str = "",
) -> TrajectoryBundle:
    """Builds a `TrajectoryBundle` from an ingested `EvalCase`.

    Args:
        case: Ingested evaluation case whose ``agent_data`` holds the agent
            configurations and conversation turns.
        observed_agent_key: Key of the observed agent.
        trajectory_id: Trajectory identifier, shared with the corresponding
            `InvestigationTrajectory`.
        created_at: Timestamp stamped on both the manifest and every step so
            they share a partition and expire together.
        partial: Whether the case was assembled from incomplete telemetry,
            which sets the manifest status to `TrajectoryStatus.PARTIAL`.
        agent_revision: Deployment revision that served the trajectory; empty
            string is stored as ``None``.

    Returns:
        The assembled `TrajectoryBundle`. A case with no turns produces a
        manifest with ``step_count=0`` and an empty `steps` tuple.
    """
    agent_data = case.agent_data
    agents, agents_truncated = _apply_write_cap(
        _dump_to_json_dict(agent_data.agents)
        if agent_data and agent_data.agents
        else None,
        what=f"agents of {trajectory_id}",
    )

    turns: list[TrajectoryStep] = []
    steps_truncated = False
    for index, turn in enumerate(agent_data.turns or [] if agent_data else []):
        # Use the turn's position in `agent_data.turns` as `step_index` because
        # `ConversationTurn.turn_index` is optional in the SDK.
        dumped, truncated = _apply_write_cap(
            _dump_to_json_dict(turn), what=f"turn {index} of {trajectory_id}"
        )
        steps_truncated = steps_truncated or truncated
        if dumped is None:
            continue
        turns.append(
            TrajectoryStep(
                observed_agent_key=observed_agent_key,
                trajectory_id=trajectory_id,
                step_index=index,
                created_at=created_at,
                step=dumped,
            )
        )

    return TrajectoryBundle(
        manifest=TrajectoryManifest(
            observed_agent_key=observed_agent_key,
            trajectory_id=trajectory_id,
            created_at=created_at,
            status=_resolve_trajectory_status(
                partial=partial, truncated=agents_truncated or steps_truncated
            ),
            step_count=len(turns),
            agents=agents,
            agent_revision=agent_revision or None,
        ),
        steps=tuple(turns),
    )


def _resolve_trajectory_status(
    *, partial: bool, truncated: bool
) -> TrajectoryStatus:
    """Resolves `TrajectoryStatus`, prioritizing lossy telemetry over write-cap truncation.

    Partial telemetry can be repaired by a later investigation, whereas
    write-cap truncation cannot be repaired by re-ingesting.

    Args:
        partial: Whether source telemetry was incomplete.
        truncated: Whether any step or agent configuration exceeded the write cap.

    Returns:
        Resolved `TrajectoryStatus`.
    """
    if partial:
        return TrajectoryStatus.PARTIAL
    return (
        TrajectoryStatus.TRUNCATED if truncated else TrajectoryStatus.INGESTED
    )


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
        "Dropping %s from the trajectory bundle: %d bytes exceeds the %d-byte write cap.",
        what,
        size,
        MAX_JSON_BYTES,
    )
    return None, True


NOT_ARCHIVED = "not_archived"
"""Status for a conversation id with no manifest at all.

Deliberately outside `TrajectoryStatus`: that enum describes a copy that exists
and how complete it is, and "there is no copy" is a different kind of answer.
Reported rather than omitted so a caller can tell it apart from a conversation
that was archived and turned out to be empty.
"""


def format_bundle(
    trajectory_id: str, bundle: TrajectoryBundle | None
) -> dict[str, Any]:
    """Formats a trajectory bundle into the wire structure shared by readers.

    Args:
        trajectory_id: Trajectory identifier.
        bundle: The stored trajectory bundle, or None if none is stored.

    Returns:
        Dictionary containing trajectory status, step counts, steps, and metadata.
    """
    if bundle is None:
        return {
            "trajectory_id": trajectory_id,
            "status": NOT_ARCHIVED,
            "step_count": 0,
            "steps_returned": 0,
            "steps": [],
            "agents": None,
            "recorded_at": None,
        }
    manifest = bundle.manifest
    return {
        "trajectory_id": trajectory_id,
        "status": manifest.status.value,
        "step_count": manifest.step_count,
        "steps_returned": len(bundle.steps),
        "steps": [step.step for step in bundle.steps],
        "agents": manifest.agents,
        "recorded_at": manifest.created_at.isoformat(),
    }
