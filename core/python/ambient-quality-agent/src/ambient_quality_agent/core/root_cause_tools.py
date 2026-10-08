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

"""Chat tool for persisting root-cause diagnoses and proposed code edits.

`record_root_cause` saves an RCA summary and complete edit set against an
insight occurrence in AQuA's BigQuery dataset. Each call replaces previous
records for that occurrence to avoid partial state or merge conflicts.

Key guarantees:
* Server-side anchoring: The `before` text for each edit is populated directly
  from the source snapshot at the given revision, ensuring exact code matches.
* Atomic validation: All proposed edits must anchor successfully; if any edit
  fails validation, the entire record is rejected.
* Read-only repository access: The tool reads published snapshots and never
  modifies the target agent repository.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ambient_quality_agent import providers

# Imported at runtime because ADK evaluates tool annotations via
# typing.get_type_hints when constructing function declarations.
from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.insights.models import (
    ProposedEdit,
    RootCause,
)
from ambient_quality_agent.tools.orchestrator import insight_tools
from ambient_quality_agent.tools.source_code import snapshot
from ambient_quality_agent.tools.source_code.agent_source_browsing_tools import (
    build_absence_reason,
    resolve_snapshot,
)
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ambient_quality_agent.core.session_state import ADKStateLike
    from ambient_quality_agent.tools.insights.models import InsightOccurrence
    from ambient_quality_agent.tools.insights.root_cause_store import (
        RootCauseWriter,
    )
    from ambient_quality_agent.tools.source_code.reader import FileSlice

logger = logging.getLogger(__name__)


class RejectedEdit(BaseModel):
    """Proposed edit that could not be anchored against the source snapshot."""

    path: str
    start_line: int
    end_line: int

    reason: str
    """Reason for rejection, such as a missing file, invalid line range,
    exclusion pattern, or overlapping edit."""


class RecordRootCauseResult(BaseModel):
    """Result returned by `record_root_cause` to the model and chat UI.

    Includes the stored `record` with populated `before` code snippets so the UI
    can render the diagnosis without secondary lookups."""

    recorded: bool
    root_cause_id: str = ""

    record: RootCause | None = None
    """Stored record with anchored `before` code, or ``None`` when nothing was
    stored."""

    edits_recorded: int = 0

    anchored: list[ProposedEdit] = Field(default_factory=list)
    """Successfully anchored edits returned on partial rejection so the caller
    can resend them without recomputing."""

    rejected: list[RejectedEdit] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)
    """Non-fatal diagnostic observations encountered during execution."""


store_factory: Callable[[ADKStateLike], RootCauseWriter] = (
    providers.build_unbound_provider("root_cause store_factory")
)

# Uses insight_tools.reader_factory directly to share a single test seam.


async def record_root_cause(
    tool_context: LaunchContext,
    *,
    insight_id: str,
    occurrence_id: str,
    revision: str,
    summary: str,
    edits: list[ProposedEdit],
) -> dict[str, Any]:
    """Records the root-cause diagnosis of an insight occurrence with proposed code edits.

    Call this once the defect mechanism is identified in source code. Each call
    supersedes previous records for this occurrence; include the complete set
    of edits you want applied.

    The server populates each edit's `before` text from the source snapshot at
    `revision`. Leave `before` empty.

    Validation is atomic: if any edit cannot be anchored, the entire record is
    rejected. The response will list rejected edits with reasons alongside
    successfully anchored edits to assist corrections.

    Args:
        tool_context: ADK launch context providing session state.
        insight_id: Identifier of the insight being diagnosed.
        occurrence_id: Sighting identifier from the insight's occurrences list.
        revision: Deployment revision the edits target (matching agent_revision).
        summary: One to three sentences naming the mechanism: what in the code
            produces the observed behavior. The dashboard shows this next to
            the insight, so it has to stand on its own.
        edits: Proposed code replacements. Each edit specifies a path, 1-based
            inclusive start_line and end_line, replacement text in after, and
            a rationale. Overlapping line ranges in the same file are rejected.
            Pass an empty list when the diagnosis proposes no code change.

    Returns:
        Dictionary containing execution result:
        - On success: {"recorded": True, "root_cause_id": ..., "record": {...},
          "edits_recorded": N, "warnings": [...]}
        - On validation failure: {"recorded": False, "rejected": [...], "anchored": [...]}
        - On error: {"error": ...}
        `warnings` are advisory: relay them to the user rather than retrying.
    """
    if not insight_id:
        return {"error": "insight_id is required"}
    if not occurrence_id:
        return {"error": "occurrence_id is required"}
    if not summary.strip():
        return {
            "error": "summary is required: state the mechanism in one paragraph."
        }

    # Coerce and validate payload fields (such as float line numbers) into
    # declared types before range checks, anchoring, and storage.
    try:
        edits = [ProposedEdit.model_validate(edit) for edit in edits]
    except ValidationError as exc:
        return {"error": f"an edit does not match the expected shape: {exc}"}

    state = tool_context.state
    try:
        reader = insight_tools.reader_factory(state)
        # Offload synchronous BigQuery I/O to a worker thread to keep the
        # event loop responsive.
        found, _ = await asyncio.to_thread(
            lambda: reader.list_occurrences(
                occurrence_id=occurrence_id, limit=1, offset=0
            )
        )
    except Exception as exc:
        logger.warning(
            "root cause: reading occurrence %s failed: %s", occurrence_id, exc
        )
        return {"error": f"failed to read occurrence {occurrence_id}: {exc}"}
    if not found:
        return {
            "error": (
                f"No occurrence {occurrence_id!r} exists for this agent. Take it "
                "from get_insight's occurrences[].occurrence_id."
            )
        }
    occurrence = found[0]
    if occurrence.insight_id != insight_id:
        return {
            "error": (
                f"Occurrence {occurrence_id!r} belongs to insight "
                f"{occurrence.insight_id!r}, not {insight_id!r}."
            )
        }

    warnings = _compute_revision_warnings(revision, occurrence)
    anchored, rejected = await _anchor_all(state, revision, edits)
    if rejected:
        return RecordRootCauseResult(
            recorded=False,
            anchored=anchored,
            rejected=rejected,
            warnings=warnings,
        ).model_dump(mode="json")

    warnings += await _compute_withdrawal_warnings(
        reader, insight_id, occurrence_id, anchored
    )

    record = RootCause(
        root_cause_id=uuid.uuid4().hex,
        insight_id=insight_id,
        occurrence_id=occurrence_id,
        agent_revision=revision,
        summary=summary,
        edits=anchored,
        created_at=dt.datetime.now(dt.UTC),
    )
    try:
        await asyncio.to_thread(lambda: store_factory(state).save(record))
    except Exception as exc:
        logger.warning(
            "root cause: saving %s failed: %s", record.root_cause_id, exc
        )
        return {"error": f"failed to record the root cause: {exc}"}
    return RecordRootCauseResult(
        recorded=True,
        root_cause_id=record.root_cause_id,
        record=record,
        edits_recorded=len(record.edits),
        warnings=warnings,
    ).model_dump(mode="json")


def _compute_revision_warnings(
    revision: str, occurrence: InsightOccurrence
) -> list[str]:
    """Warns when proposed edits target a revision different from the occurrence.

    Only checks when both revisions are specified, as telemetry may omit revision
    metadata, triggering fallback to the latest snapshot.

    Args:
        revision: Revision the edits are anchored against.
        occurrence: Occurrence the diagnosis is written against.

    Returns:
        List containing a warning message if revisions differ, else empty.
    """
    if not revision or not occurrence.agent_revision:
        return []
    if revision == occurrence.agent_revision:
        return []
    return [
        f"The edits are anchored at revision {revision!r}, but occurrence "
        f"{occurrence.occurrence_id} was seen on {occurrence.agent_revision!r}."
    ]


async def _compute_withdrawal_warnings(
    reader: Any,
    insight_id: str,
    occurrence_id: str,
    edits: list[ProposedEdit],
) -> list[str]:
    """Warns if edits present in an earlier record are omitted in the new call.

    Args:
        reader: Configured InsightReader instance.
        insight_id: Identifier of the target insight.
        occurrence_id: Occurrence identifier to compare against previous records.
        edits: Anchored edits being saved.

    Returns:
        List of warning strings for each dropped edit range.
    """
    try:
        records = await asyncio.to_thread(
            lambda: reader.list_root_causes(insight_id)
        )
    except Exception as exc:
        logger.warning(
            "root cause: reading the previous record failed: %s", exc
        )
        return []
    previous = next(
        (r for r in records if r.occurrence_id == occurrence_id), None
    )
    if previous is None:
        return []
    kept = {_extract_range_key(edit) for edit in edits}
    return [
        f"{edit.path}:{edit.start_line}-{edit.end_line} was in the previous "
        "record for this occurrence and is not in this one."
        for edit in previous.edits
        if _extract_range_key(edit) not in kept
    ]


def _extract_range_key(edit: ProposedEdit) -> tuple[str, int, int]:
    """Extracts the (path, start_line, end_line) identity tuple for an edit.

    The path is canonicalized so that two spellings of the same file compare
    equal.

    Args:
        edit: Proposed edit to inspect.

    Returns:
        Tuple of (normalized path, start_line, end_line).
    """
    return (snapshot.normalize_path(edit.path), edit.start_line, edit.end_line)


async def _anchor_all(
    state: ADKStateLike, revision: str, edits: list[ProposedEdit]
) -> tuple[list[ProposedEdit], list[RejectedEdit]]:
    """Populates each edit's `before` text from the source snapshot at `revision`.

    Validates line ranges and checks for out-of-bounds line numbers against the
    actual file length in the snapshot.

    Args:
        state: ADK session state used to resolve the snapshot reader.
        revision: Revision to anchor against, or empty string for latest snapshot.
        edits: Proposed edits to anchor.

    Returns:
        Tuple of (anchored_edits, rejected_edits).
    """
    if not edits:
        return [], []

    structural = _reject_unusable_ranges(edits)
    if len(structural) == len(edits):
        # Skip snapshot resolution if all edits failed structural validation.
        return [], list(structural.values())
    resolved, refusal = await resolve_snapshot(state, revision)
    if resolved is None:
        reason = (refusal or {}).get(
            "reason", "The source snapshot could not be read."
        )
        return [], [
            structural.get(position) or _build_rejected_edit(edit, reason)
            for position, edit in enumerate(edits)
        ]

    anchored: list[ProposedEdit] = []
    rejected: list[RejectedEdit] = list(structural.values())
    for position, edit in enumerate(edits):
        if position in structural:
            continue
        if resolved.manifest.get_entry(edit.path) is None:
            rejected.append(
                _build_rejected_edit(
                    edit,
                    build_absence_reason(resolved.manifest, edit.path)[
                        "reason"
                    ],
                )
            )
            continue
        try:
            # Offload synchronous GCS reads to a worker thread.
            found: FileSlice | None = await asyncio.to_thread(
                resolved.reader.read_file,
                resolved.revision,
                edit.path,
                offset=edit.start_line,
                limit=edit.end_line - edit.start_line + 1,
            )
        except (
            api_exceptions.GoogleAPIError,
            auth_exceptions.GoogleAuthError,
        ) as exc:
            # Catch known GCS/auth errors as rejections; allow unexpected
            # exceptions to surface.
            logger.warning("root cause: reading %s failed: %s", edit.path, exc)
            rejected.append(
                _build_rejected_edit(edit, f"failed to read {edit.path}: {exc}")
            )
            continue
        if found is None:
            rejected.append(
                _build_rejected_edit(
                    edit,
                    f"{edit.path} is listed in the manifest for revision "
                    f"{resolved.revision} but its body is missing from the "
                    "snapshot, so the upload was incomplete.",
                )
            )
            continue
        wanted = edit.end_line - edit.start_line + 1
        if len(found.lines) != wanted:
            rejected.append(
                _build_rejected_edit(
                    edit,
                    f"lines {edit.start_line}-{edit.end_line} run past the end "
                    f"of {edit.path}, which has {found.total_lines} lines at "
                    f"revision {resolved.revision}.",
                )
            )
            continue
        # Store the canonical path so a coding harness can anchor the edit
        # against HEAD without re-deriving the spelling the model used.
        anchored.append(
            edit.model_copy(
                update={
                    "path": snapshot.normalize_path(edit.path),
                    "before": "\n".join(found.lines),
                }
            )
        )
    return anchored, rejected


def _reject_unusable_ranges(
    edits: list[ProposedEdit],
) -> dict[int, RejectedEdit]:
    """Validates basic line range constraints and detects overlapping ranges.

    Rejects non-positive start lines, inverted line ranges, and edits that
    overlap another edit in the same file.

    Args:
        edits: Proposed edits to validate.

    Returns:
        Dictionary mapping invalid edit list indices to RejectedEdit instances.
    """
    rejected: dict[int, RejectedEdit] = {}
    accepted: list[ProposedEdit] = []
    for position, edit in enumerate(edits):
        if not edit.path:
            rejected[position] = _build_rejected_edit(
                edit, "path is required; name it as list_source_files does."
            )
            continue
        if edit.start_line < 1:
            rejected[position] = _build_rejected_edit(
                edit,
                f"start_line must be 1 or greater; got {edit.start_line}.",
            )
            continue
        if edit.end_line < edit.start_line:
            rejected[position] = _build_rejected_edit(
                edit,
                f"end_line {edit.end_line} is before start_line {edit.start_line}.",
            )
            continue
        clash = next((e for e in accepted if _overlaps(e, edit)), None)
        if clash is not None:
            rejected[position] = _build_rejected_edit(
                edit,
                f"overlaps the edit to {clash.path} at lines {clash.start_line}-"
                f"{clash.end_line} in the same call; send one edit per range.",
            )
            continue
        accepted.append(edit)
    return rejected


def _overlaps(one: ProposedEdit, other: ProposedEdit) -> bool:
    """Checks if two edits target the same file with overlapping line ranges.

    Args:
        one: First proposed edit.
        other: Second proposed edit.

    Returns:
        True if both edits share a path and overlap in line numbers.
    """
    return (
        snapshot.normalize_path(one.path) == snapshot.normalize_path(other.path)
        and one.start_line <= other.end_line
        and other.start_line <= one.end_line
    )


def _build_rejected_edit(edit: ProposedEdit, reason: str) -> RejectedEdit:
    """Constructs a RejectedEdit instance for an invalid or unresolvable edit.

    Args:
        edit: The rejected proposed edit.
        reason: Diagnostic explanation of why anchoring failed.

    Returns:
        A RejectedEdit with path, line range, and failure reason.
    """
    return RejectedEdit(
        path=edit.path,
        start_line=edit.start_line,
        end_line=edit.end_line,
        reason=reason,
    )
