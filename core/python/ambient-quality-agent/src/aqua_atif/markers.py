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

"""Missing markers: entries in `extra.aqua.missing` that say data is missing.

A marker sits on the object that lacks the data, with a `path` relative to that
object, a `reason` from a closed list and an optional short `detail`. Markers
record absent optional fields, unreadable external references, and `v1.9+`
unknown items on documents that passed `validate_document`; a document missing
a required ATIF field is rejected by `validate_document` and is `not_ingested`.
Producers may declare unrecorded fields in `extra.aqua.missing` as field names
(expanded to `not_recorded` on ingest); only AQuA writes ingestion reasons
(`read_error`, `invalid_content`, `path_traversal`, `too_large`,
`unsupported_version`).
The session reviewer reads the paths so that it does not report a gap as agent
failure; AQuA's code reads the reasons for the ingest status and for the
coverage line, which names the review checks that markers switch off.

A run whose producer recorded no tool definitions, no result for `call_2`, and
an image that could not be read (403)::

    {"agent": {"name": "checkout-fixer", "version": "2.3"},
     "steps": [
       {"step_id": 1, "source": "user", "message": [...],
        "extra": {"aqua": {"missing": [              # on the step: a content
          {"path": "message[1]",                     # part has no `extra`
           "reason": "read_error", "detail": "403"}]}}},
       {"step_id": 2, "source": "agent",
        "tool_calls": [
          {"tool_call_id": "call_1", ...},
          {"tool_call_id": "call_2", ...,
           "extra": {"aqua": {"missing": [           # on the tool call
             {"path": "result", "reason": "not_recorded"}]}}}]}],
     "extra": {"aqua": {"missing": [                 # on the root
       {"path": "agent.tool_definitions", "reason": "not_recorded"}]}}}

Where each marker comes from::

    parsing, validation      -> `PendingMissingMarker`s passed to
    and the source              `annotate_document` (a repaired string, a
                                field newer than v1.8, an unread file)
    `find_missing_markers`   -> data the document does not record
                                (no tool definitions, a call with no result,
                                a sentence in `extra.losses`)

What the markers decide::

    any reason of read_error, invalid_content,
    path_traversal, too_large               -> IngestStatus.PARTIAL
    otherwise                               -> IngestStatus.INGESTED
    {"path": "agent.tool_definitions", ...} -> the unlisted-tool check is off
                                               (`resolve_disabled_check`)
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from aqua_atif.errors import records_tool_errors
from aqua_atif.paths import (
    RECORDS_TOOL_ERRORS,
    Holder,
    HolderKind,
    JsonPath,
    get_aqua,
    get_value,
    list_holders,
)
from pydantic import TypeAdapter, ValidationError

_LOGGER = logging.getLogger(__name__)

# Coerces `llm_call_count` as `Step` does: its type is `int | None`, and its
# `ge=0` bound already held when the document was validated.
_LLM_CALL_COUNT: TypeAdapter[int | None] = TypeAdapter(int | None)


class Reason(enum.StrEnum):
    """Why data is missing: the closed list of marker reasons."""

    NOT_RECORDED = "not_recorded"
    READ_ERROR = "read_error"
    INVALID_CONTENT = "invalid_content"
    PATH_TRAVERSAL = "path_traversal"
    TOO_LARGE = "too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    UNSUPPORTED_VERSION = "unsupported_version"
    OUT_OF_SCOPE = "out_of_scope"


_REASON_VALUES = frozenset(reason.value for reason in Reason)

# Reasons that make a trajectory `partial` rather than `ingested`.
PARTIAL_REASONS: frozenset[Reason] = frozenset(
    {
        Reason.READ_ERROR,
        Reason.INVALID_CONTENT,
        Reason.PATH_TRAVERSAL,
        Reason.TOO_LARGE,
    }
)


class IngestStatus(enum.StrEnum):
    """The outcome of ingesting one document."""

    INGESTED = "ingested"
    PARTIAL = "partial"
    NOT_INGESTED = "not_ingested"


# MissingMarker paths. `system_prompt`, `tool_errors` and `continued_trajectory` are
# names, not field paths.
TOOL_DEFINITIONS = "agent.tool_definitions"
AGENT_VERSION = "agent.version"
SYSTEM_PROMPT = "system_prompt"
STEP_TIMESTAMPS = "steps[].timestamp"
STEP_MODEL_NAMES = "steps[].model_name"
STEP_REASONING = "steps[].reasoning_content"
STEP_METRICS = "steps[].metrics"
STEP_MESSAGES = "steps[].message"
TOOL_ERRORS = "tool_errors"
FINAL_METRICS = "final_metrics"
CONTINUED_TRAJECTORY = "continued_trajectory"
TOOL_CALL_RESULT = "result"
SUBAGENT_TRAJECTORY = "trajectory"

_PART_PATH_RE = re.compile(r"(?:extra\.aqua\.)?(?:message|content)\[\d+\]")


@dataclasses.dataclass(frozen=True)
class MissingMarker:
    """One entry in `extra.aqua.missing`.

    Attributes:
        path: What is missing, relative to the object that holds the marker.
        reason: Why it is missing.
        detail: A short qualifier, such as an HTTP status; never trajectory
            content.
    """

    path: str
    reason: Reason
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Formats the marker as stored in `extra.aqua.missing`.

        Returns:
            `{"path", "reason"}`, with `detail` when set.
        """
        payload = {"path": self.path, "reason": str(self.reason)}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> MissingMarker | None:
        """Parses a stored marker.

        Args:
            raw: One entry of `extra.aqua.missing`.

        Returns:
            The marker, or None when `path` is not a non-empty string, `reason`
            is not a known code, or `detail` is neither a string nor absent.
        """
        if not isinstance(raw, Mapping):
            return None
        path = raw.get("path")
        reason = raw.get("reason")
        detail = raw.get("detail")
        if not isinstance(path, str) or not path:
            return None
        if not isinstance(reason, str) or reason not in _REASON_VALUES:
            return None
        if detail is not None and not isinstance(detail, str):
            return None
        return cls(path=path, reason=Reason(reason), detail=detail)


@dataclasses.dataclass(frozen=True)
class PendingMissingMarker:
    """A marker not yet written into the document, and the object it goes on.

    Stages that find missing data before the document is annotated, such as
    parsing, validation and the source, return their markers in this form, and
    `add_markers` writes them.

    Attributes:
        holder: Path from the document root of the object that holds the
            marker; it must be an object that can hold `extra`.
        marker: The marker.
    """

    holder: JsonPath
    marker: MissingMarker


def extract_markers(holder: Mapping[str, Any]) -> list[MissingMarker]:
    """Extracts the markers of one object.

    Args:
        holder: An object that can hold `extra`.

    Returns:
        The valid entries of its `extra.aqua.missing`, in order.
    """
    extra = holder.get("extra")
    aqua = extra.get("aqua") if isinstance(extra, Mapping) else None
    missing = aqua.get("missing") if isinstance(aqua, Mapping) else None
    if not isinstance(missing, list):
        return []
    return [
        marker
        for item in missing
        if (marker := MissingMarker.from_dict(item)) is not None
    ]


def list_placed_markers(
    document: Mapping[str, Any],
) -> list[PendingMissingMarker]:
    """Lists every marker in a document, embedded subagents included.

    Args:
        document: Root trajectory of the document.

    Returns:
        The markers with the paths of the objects that hold them.
    """
    return [
        PendingMissingMarker(holder=holder.path, marker=marker)
        for holder in list_holders(document)
        for marker in extract_markers(holder.value)
    ]


def add_markers(
    document: dict[str, Any], markers: Iterable[PendingMissingMarker]
) -> None:
    """Merges markers into the `extra.aqua.missing` lists of a document.

    A marker equal to one already on the object (same path, reason and detail)
    is not added twice. A marker whose holder does not exist is dropped and
    logged.

    Args:
        document: Root trajectory of the document, changed in place.
        markers: The markers to add.
    """
    stored: dict[int, set[MissingMarker]] = {}
    for placed in markers:
        holder = get_value(document, placed.holder)
        if not isinstance(holder, dict):
            _LOGGER.warning(
                "Dropped a %s marker whose holder does not exist.",
                placed.marker.reason,
            )
            continue
        aqua = get_aqua(holder, create=True)
        if aqua is None:
            continue
        missing = aqua.get("missing")
        if not isinstance(missing, list):
            missing = []
            aqua["missing"] = missing
        if id(missing) not in stored:
            stored[id(missing)] = set(extract_markers(holder))
        if placed.marker not in stored[id(missing)]:
            stored[id(missing)].add(placed.marker)
            missing.append(placed.marker.to_dict())


def find_missing_markers(
    document: Mapping[str, Any],
) -> list[PendingMissingMarker]:
    """Builds the markers for data a document does not record.

    Inspects the root trajectory and each embedded subagent for:

    - Missing trajectory-level fields (such as tool definitions, timestamps,
      model names, reasoning, metrics, and `tool_errors`).
    - Known `agents-cli` loss sentences in `extra.losses` (for example,
      `"per-step token usage unavailable"` and
      `"unsupported content parts dropped"`; see `losses_to_markers`).
    - `result` markers on tool calls with no matching observation result.
    - `trajectory` markers on unresolved subagent references that do not
      already have a `trajectory` marker.

    An agent step with `llm_call_count: 0` made no model call and does not
    count toward per-step markers.

    Args:
        document: Root trajectory of a validated document.

    Returns:
        The markers, in document order.
    """
    markers: list[PendingMissingMarker] = []
    records = records_tool_errors(document)
    subagents = build_subagent_index(document)
    for holder in list_holders(document):
        if holder.kind == HolderKind.TRAJECTORY:
            extra = holder.value.get("extra")
            losses = extra.get("losses") if isinstance(extra, Mapping) else None
            aqua = get_aqua(holder.value)
            trajectory_records = records or (
                aqua is not None and aqua.get(RECORDS_TOOL_ERRORS) is True
            )
            markers.extend(
                PendingMissingMarker(holder.path, marker)
                for marker in (
                    *losses_to_markers(losses),
                    *_build_trajectory_markers(
                        holder.value, trajectory_records
                    ),
                )
            )
        elif holder.kind == HolderKind.STEP:
            markers.extend(_build_tool_call_markers(holder))
        elif holder.kind == HolderKind.SUBAGENT_REF:
            resolved = resolve_subagent(subagents, holder.path, holder.value)
            if resolved is None and not any(
                marker.path == SUBAGENT_TRAJECTORY
                for marker in extract_markers(holder.value)
            ):
                markers.append(
                    PendingMissingMarker(
                        holder.path,
                        MissingMarker(SUBAGENT_TRAJECTORY, Reason.NOT_RECORDED),
                    )
                )
    return markers


def _build_trajectory_markers(
    trajectory: Mapping[str, Any], records_errors: bool
) -> list[MissingMarker]:
    """Builds the root markers of one trajectory.

    Args:
        trajectory: The root or an embedded subagent.
        records_errors: Whether its producer records tool failures.

    Returns:
        The markers for the trajectory's own root.
    """
    agent = trajectory.get("agent")
    agent = agent if isinstance(agent, Mapping) else {}
    steps = [
        step
        for step in trajectory.get("steps") or ()
        if isinstance(step, Mapping)
    ]
    model_steps = [
        step
        for step in steps
        if step.get("source") == "agent" and read_llm_call_count(step) != 0
    ]
    has_results = any(
        isinstance(step.get("observation"), Mapping)
        and step["observation"].get("results")
        for step in steps
    )
    version = agent.get("version")
    checks = (
        (agent.get("tool_definitions") is None, TOOL_DEFINITIONS),
        (not isinstance(version, str) or not version, AGENT_VERSION),
        (not _has_system_prompt(steps), SYSTEM_PROMPT),
        (all(step.get("timestamp") is None for step in steps), STEP_TIMESTAMPS),
        (
            bool(model_steps)
            and agent.get("model_name") is None
            and all(step.get("model_name") is None for step in model_steps),
            STEP_MODEL_NAMES,
        ),
        (
            bool(model_steps)
            and all(
                step.get("reasoning_content") is None for step in model_steps
            ),
            STEP_REASONING,
        ),
        (
            bool(model_steps)
            and all(step.get("metrics") is None for step in model_steps),
            STEP_METRICS,
        ),
        (
            bool(has_results) and not records_errors,
            TOOL_ERRORS,
        ),
        (trajectory.get("final_metrics") is None, FINAL_METRICS),
    )
    markers = [
        MissingMarker(path, Reason.NOT_RECORDED)
        for missing, path in checks
        if missing
    ]
    if trajectory.get("continued_trajectory_ref"):
        markers.append(MissingMarker(CONTINUED_TRAJECTORY, Reason.OUT_OF_SCOPE))
    return markers


def read_llm_call_count(step: Mapping[str, Any]) -> int | None:
    """Reads a step's `llm_call_count` as the models validated it.

    The models coerce the value in Pydantic's lax mode, so `"0"` is 0. Reading
    the raw JSON would treat such a step as one that called a model.

    Args:
        step: A step of a validated document.

    Returns:
        The count, or None when the step has none.
    """
    try:
        return _LLM_CALL_COUNT.validate_python(step.get("llm_call_count"))
    except ValidationError:
        return None


def _has_system_prompt(steps: list[Mapping[str, Any]]) -> bool:
    """Checks whether a `system` step comes before the first agent step.

    Args:
        steps: The trajectory's steps.

    Returns:
        True when a `system` step is found before any `agent` step.
    """
    for step in steps:
        if step.get("source") == "system":
            return True
        if step.get("source") == "agent":
            return False
    return False


def _build_tool_call_markers(step: Holder) -> list[PendingMissingMarker]:
    """Builds `result` markers for the tool calls of a step with no result.

    Args:
        step: A step `Holder` from `list_holders`.

    Returns:
        One marker per unanswered tool call, on that call.
    """
    observation = step.value.get("observation")
    results = (
        observation.get("results") if isinstance(observation, Mapping) else None
    )
    answered = {
        result.get("source_call_id")
        for result in (results if isinstance(results, list) else ())
        if isinstance(result, Mapping)
    }
    calls = step.value.get("tool_calls")
    return [
        PendingMissingMarker(
            (*step.path, "tool_calls", index),
            MissingMarker(TOOL_CALL_RESULT, Reason.NOT_RECORDED),
        )
        for index, call in enumerate(calls if isinstance(calls, list) else ())
        if isinstance(call, Mapping)
        and call.get("tool_call_id") not in answered
    ]


def find_embedded_subagent(
    document: Mapping[str, Any], ref_path: JsonPath
) -> dict[str, Any] | None:
    """Finds the embedded subagent that a subagent reference resolves to.

    A reference with a non-empty `trajectory_id` searches the
    `subagent_trajectories` of the holding trajectory and then each enclosing
    trajectory.

    Args:
        document: Root trajectory of the document.
        ref_path: Path of the reference from the document root.

    Returns:
        The embedded subagent, or None when the reference does not resolve.
    """
    ref = get_value(document, ref_path)
    if not isinstance(ref, Mapping):
        return None
    return resolve_subagent(build_subagent_index(document), ref_path, ref)


# For each trajectory path, its embedded subagents keyed by `trajectory_id`.
SubagentIndex = dict[JsonPath, dict[str, dict[str, Any]]]


def build_subagent_index(document: Mapping[str, Any]) -> SubagentIndex:
    """Indexes the embedded subagents of every trajectory in a document.

    Args:
        document: Root trajectory of the document.

    Returns:
        The index; the first subagent with a `trajectory_id` wins.
    """
    index: SubagentIndex = {}
    for holder in list_holders(document):
        if holder.kind != HolderKind.TRAJECTORY:
            continue
        by_id: dict[str, dict[str, Any]] = {}
        subagents = holder.value.get("subagent_trajectories")
        for subagent in subagents if isinstance(subagents, list) else ():
            if not isinstance(subagent, dict):
                continue
            trajectory_id = subagent.get("trajectory_id")
            if isinstance(trajectory_id, str):
                by_id.setdefault(trajectory_id, subagent)
        index[holder.path] = by_id
    return index


def has_subagent_id(
    index: SubagentIndex, trajectory_path: JsonPath, trajectory_id: str
) -> bool:
    """Checks whether a trajectory already embeds a subagent with an id.

    Args:
        index: A subagent index.
        trajectory_path: Path of the trajectory from the document root.
        trajectory_id: The id.

    Returns:
        True when the trajectory's `subagent_trajectories` holds that id.
    """
    return trajectory_id in index.get(trajectory_path, {})


def add_embedded_subagent(
    index: SubagentIndex,
    trajectory_path: JsonPath,
    subagent_path: JsonPath,
    subagent: dict[str, Any],
) -> None:
    """Updates `index` with a subagent that a source embedded from another file.

    Indexes the subagent under its holding trajectory by `trajectory_id`, along
    with any subagents already embedded inside it.

    Args:
        index: The index, changed in place.
        trajectory_path: Path of the trajectory that holds the subagent.
        subagent_path: Path of the subagent from the document root.
        subagent: The embedded subagent, with `trajectory_id` set.
    """
    by_id = index.setdefault(trajectory_path, {})
    trajectory_id = subagent.get("trajectory_id")
    if isinstance(trajectory_id, str):
        by_id.setdefault(trajectory_id, subagent)
    for path, entry in build_subagent_index(subagent).items():
        index[(*subagent_path, *path)] = entry


def resolve_subagent(
    index: SubagentIndex, ref_path: JsonPath, ref: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Resolves a subagent reference through a subagent index.

    Looks up a non-empty `trajectory_id` in the trajectory that holds the
    reference and then in each enclosing one.

    Args:
        index: `build_subagent_index` of the document.
        ref_path: Path of the reference from the document root.
        ref: The reference.

    Returns:
        The embedded subagent, or None when the reference does not resolve.
    """
    trajectory_id = ref.get("trajectory_id")
    if not isinstance(trajectory_id, str) or not trajectory_id:
        return None
    for scope in _list_enclosing_trajectories(ref_path):
        found = index.get(scope, {}).get(trajectory_id)
        if found is not None:
            return found
    return None


def _list_enclosing_trajectories(path: JsonPath) -> list[JsonPath]:
    """Lists the paths of the trajectories that contain a path, innermost first.

    Args:
        path: Path of a value from the document root.

    Returns:
        The enclosing trajectory paths, ending with the root's `()`.
    """
    scopes: list[JsonPath] = [()]
    for position, segment in enumerate(path[:-1]):
        if segment == "subagent_trajectories" and isinstance(
            path[position + 1], int
        ):
            scopes.append(path[: position + 2])
    return scopes[::-1]


def compute_ingest_status(document: Mapping[str, Any]) -> IngestStatus:
    """Computes the ingest status of a document that passed validation.

    Args:
        document: Root trajectory of the annotated document.

    Returns:
        `partial` when any marker, in the document or an embedded subagent, has
        the reason `read_error`, `invalid_content`, `path_traversal` or
        `too_large`; otherwise `ingested`.
    """
    if any(
        placed.marker.reason in PARTIAL_REASONS
        for placed in list_placed_markers(document)
    ):
        return IngestStatus.PARTIAL
    return IngestStatus.INGESTED


@dataclasses.dataclass(frozen=True)
class DisabledCheck:
    """A session-review check that a marker switches off.

    Attributes:
        check_id: Stable identifier, for counting.
        name: Name for the coverage line, such as `unlisted-tool check`.
        summary: What was not recorded, for the coverage line.
        prohibition: What the session reviewer must not do while the check is
            off.
    """

    check_id: str
    name: str
    summary: str
    prohibition: str


UNLISTED_TOOL_CHECK = DisabledCheck(
    "unlisted_tool",
    "unlisted-tool check",
    "tool definitions not recorded",
    "Report a call as not in the agent's tool list",
)
AGENT_REVISION_CHECK = DisabledCheck(
    "agent_revision",
    "agent-revision check",
    "agent version not recorded",
    "Make claims about the agent's revision",
)
INSTRUCTION_CHECK = DisabledCheck(
    "instructions",
    "instruction check",
    "system prompt not recorded",
    "Report that the agent broke its instructions",
)
TIMING_CHECK = DisabledCheck(
    "timing",
    "timing check",
    "step timestamps not recorded",
    "Make claims about time or latency",
)
MODEL_CHECK = DisabledCheck(
    "model",
    "model check",
    "model names not recorded",
    "Make claims about which model ran",
)
REASONING_CHECK = DisabledCheck(
    "reasoning",
    "reasoning check",
    "reasoning not recorded",
    "Report missing reasoning",
)
TOKEN_CHECK = DisabledCheck(
    "tokens",
    "token and cost check",
    "step metrics not recorded",
    "Make claims about tokens or cost",
)
TOOL_ERROR_CHECK = DisabledCheck(
    "tool_errors",
    "tool-error check",
    "tool failures not recorded",
    "Treat a result as a success or a failure, except from its text",
)
TOTALS_CHECK = DisabledCheck(
    "totals",
    "totals check",
    "final metrics not recorded",
    "Make claims about totals",
)
COMPLETION_CHECK = DisabledCheck(
    "completion",
    "completion check",
    "the run continues in another file",
    "Report that the agent stopped before it finished",
)
FAILED_CALL_CHECK = DisabledCheck(
    "failed_call",
    "failed-call check",
    "tool results not recorded",
    "Count the call as failed",
)
SUBAGENT_CHECK = DisabledCheck(
    "subagent",
    "subagent check",
    "subagent trajectory not read",
    "Judge what the subagent did",
)
MEDIA_CHECK = DisabledCheck(
    "media",
    "media check",
    "attachment not read",
    "Judge the image, audio or file",
)
DROPPED_CONTENT_CHECK = DisabledCheck(
    "dropped_content",
    "dropped-content check",
    "content dropped by the converter",
    "Judge content the converter dropped",
)
COMPLETENESS_CHECK = DisabledCheck(
    "completeness",
    "completeness check",
    "fields from a newer ATIF version",
    "Conclude from the visible fields alone that something on this object is "
    "absent or was not done",
)

_CHECKS_BY_PATH = {
    TOOL_DEFINITIONS: UNLISTED_TOOL_CHECK,
    AGENT_VERSION: AGENT_REVISION_CHECK,
    SYSTEM_PROMPT: INSTRUCTION_CHECK,
    STEP_TIMESTAMPS: TIMING_CHECK,
    STEP_MODEL_NAMES: MODEL_CHECK,
    STEP_REASONING: REASONING_CHECK,
    STEP_METRICS: TOKEN_CHECK,
    TOOL_ERRORS: TOOL_ERROR_CHECK,
    FINAL_METRICS: TOTALS_CHECK,
    CONTINUED_TRAJECTORY: COMPLETION_CHECK,
    TOOL_CALL_RESULT: FAILED_CALL_CHECK,
    SUBAGENT_TRAJECTORY: SUBAGENT_CHECK,
}


def resolve_disabled_check(marker: MissingMarker) -> DisabledCheck | None:
    """Resolves the review check that a marker switches off.

    Args:
        marker: The marker.

    Returns:
        The check, or None when the marker switches none off (for example, a
        field whose text had invalid characters replaced keeps the rest of its
        text).
    """
    if marker.reason == Reason.UNSUPPORTED_VERSION:
        return COMPLETENESS_CHECK
    if marker.path in _CHECKS_BY_PATH:
        return _CHECKS_BY_PATH[marker.path]
    if _PART_PATH_RE.fullmatch(marker.path):
        return MEDIA_CHECK
    if (
        marker.path == STEP_MESSAGES
        and marker.reason == Reason.UNSUPPORTED_TYPE
    ):
        return DROPPED_CONTENT_CHECK
    return None


@dataclasses.dataclass(frozen=True)
class DisabledCheckUse:
    """A check that markers switch off in one document.

    Attributes:
        check: The check.
        holders: Paths from the document root of the objects whose markers
            switch it off, in document order, so callers can tell whether a
            check is off for the root (`()`), an embedded subagent (its
            trajectory path), or a single step, tool call, result, or
            reference.
    """

    check: DisabledCheck
    holders: tuple[JsonPath, ...] = ()


def list_disabled_checks(document: Mapping[str, Any]) -> list[DisabledCheckUse]:
    """Lists the review checks that markers switch off in a document.

    Embedded subagents are included. Each check appears once.

    Args:
        document: Root trajectory of the annotated document.

    Returns:
        The checks, in the order their first marker appears, each with the
        objects whose markers switch it off.
    """
    checks: dict[str, DisabledCheck] = {}
    holders: dict[str, dict[JsonPath, None]] = {}
    for placed in list_placed_markers(document):
        check = resolve_disabled_check(placed.marker)
        if check is None:
            continue
        checks.setdefault(check.check_id, check)
        holders.setdefault(check.check_id, {})[placed.holder] = None
    return [
        DisabledCheckUse(check=check, holders=tuple(holders[check_id]))
        for check_id, check in checks.items()
    ]


# The sentences `agents-cli` writes in the root `extra.losses`, and the root
# marker each stands for.
_LOSSES = {
    "per-step token usage unavailable": MissingMarker(
        STEP_METRICS, Reason.NOT_RECORDED
    ),
    "unsupported content parts dropped": MissingMarker(
        STEP_MESSAGES, Reason.UNSUPPORTED_TYPE
    ),
}


def losses_to_markers(losses: Any) -> list[MissingMarker]:
    """Translates the sentences of an `agents-cli` root `extra.losses`.

    Unknown sentences are logged by count only, since they are file content.

    Args:
        losses: The value of the root's `extra.losses`.

    Returns:
        One root marker per known sentence.
    """
    if not isinstance(losses, list):
        return []
    markers: list[MissingMarker] = []
    unknown = 0
    for sentence in losses:
        marker = (
            _LOSSES.get(sentence.strip().lower())
            if isinstance(sentence, str)
            else None
        )
        if marker is None:
            unknown += 1
        elif marker not in markers:
            markers.append(marker)
    if unknown:
        _LOGGER.info("Ignored %d unknown sentence(s) in extra.losses.", unknown)
    return markers
