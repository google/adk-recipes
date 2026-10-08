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

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ambient_quality_agent.core.investigation import job_scheduling, model
from ambient_quality_agent.core.session_state import RouteContext
from ambient_quality_agent.tools.observed_agent_config import (
    commands as agent_config_commands,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.orchestrator import (
    config_tools,
    document_tools,
    health_tools,
    insight_tools,
    source_tools,
    trajectory_tools,
)
from ambient_quality_agent.tools.source_code import snapshot
from ambient_quality_agent.tools.source_code import upload as source_upload
from ambient_quality_shared.protocol import (
    AGENTS_APPLIED_ROUTE,
    AGENTS_ATTACH_ROUTE,
    AGENTS_DETACH_ROUTE,
    AGENTS_LIST_ROUTE,
    CONFIG_ROUTE,
    GOAL_GET_ROUTE,
    GOAL_SET_ROUTE,
    GOAL_VERSIONS_ROUTE,
    HEALTH_ROUTE,
    INSIGHTS_DISMISS_ROUTE,
    INSIGHTS_GET_ROUTE,
    INSIGHTS_LIST_ROUTE,
    INSIGHTS_MERGE_ROUTE,
    INVESTIGATIONS_DAILY_ROUTE,
    INVESTIGATIONS_GET_ROUTE,
    INVESTIGATIONS_LIST_ROUTE,
    INVESTIGATIONS_SCHEDULE_ROUTE,
    INVESTIGATIONS_STATS_ROUTE,
    MEMORIES_DELETE_ROUTE,
    MEMORIES_LIST_ROUTE,
    SOURCE_COMMIT_ROUTE,
    SOURCE_ROUTE,
    SOURCE_UPLOAD_ROUTE,
    TRAJECTORIES_CASE_ROUTE,
    TRAJECTORIES_LIST_ROUTE,
    TRAJECTORIES_OUTCOMES_ROUTE,
)
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 1 << 20

router = APIRouter()


@router.post(f"/{INVESTIGATIONS_LIST_ROUTE}")
async def list_investigations(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await model.list_investigations(
        RouteContext(),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{INVESTIGATIONS_GET_ROUTE}")
async def get_investigation(request: Request) -> dict[str, Any]:
    run_id = _extract_required_field(await _read_json_body(request), "run_id")
    return await model.get_investigation(run_id, RouteContext())


@router.post(f"/{INVESTIGATIONS_STATS_ROUTE}")
async def get_investigation_stats(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await model.get_investigation_stats(
        RouteContext(),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{INVESTIGATIONS_DAILY_ROUTE}")
async def get_daily_trends(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await model.get_daily_trends(
        RouteContext(),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{INVESTIGATIONS_SCHEDULE_ROUTE}")
async def schedule_investigation() -> dict[str, Any]:
    return await job_scheduling.schedule_investigation(RouteContext())


# The goal and the memories. Served here rather than read from GCS by the
# dashboard, so that the object layout, the id derivation and the write path
# have one definition, shared with the chat's goal and memory tools.
@router.post(f"/{GOAL_GET_ROUTE}")
async def get_goal() -> dict[str, Any]:
    return await document_tools.get_goal(RouteContext())


@router.post(f"/{GOAL_SET_ROUTE}")
async def set_goal(request: Request) -> dict[str, Any]:
    payload = await _read_json_body(request)
    return await document_tools.set_goal(
        RouteContext(), goal=str(payload.get("goal", ""))
    )


@router.post(f"/{GOAL_VERSIONS_ROUTE}")
async def list_goal_versions() -> dict[str, Any]:
    return await document_tools.list_goal_versions(RouteContext())


@router.post(f"/{MEMORIES_LIST_ROUTE}")
async def list_memories() -> dict[str, Any]:
    return await document_tools.list_memories(RouteContext())


@router.post(f"/{MEMORIES_DELETE_ROUTE}")
async def delete_memory(request: Request) -> dict[str, Any]:
    payload = await _read_json_body(request)
    return await document_tools.delete_memory(
        RouteContext(), memory_id=str(payload.get("memory_id", ""))
    )


@router.post(f"/{INSIGHTS_LIST_ROUTE}")
async def list_insights(request: Request) -> dict[str, Any]:
    filters = await _read_json_body(request)
    return await insight_tools.list_insights(
        RouteContext(),
        status=str(filters.get("status", "")),
        run_id=str(filters.get("run_id", "")),
        has_root_cause=_encode_tri_state(filters.get("has_root_cause")),
        page_token=str(filters.get("page_token", "")),
        window_start=str(filters.get("window_start", "")),
        window_end=str(filters.get("window_end", "")),
        order_by=str(filters.get("order_by", "")),
        page_size=_parse_count(filters.get("page_size")),
        day=str(filters.get("day", "")),
    )


@router.post(f"/{INSIGHTS_GET_ROUTE}")
async def get_insight(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await insight_tools.get_insight(
        RouteContext(),
        insight_id=_extract_required_field(args, "insight_id"),
        run_id=str(args.get("run_id", "")),
        include_traces=bool(args.get("include_traces", True)),
        include_trajectories=(
            None
            if args.get("include_trajectories") is None
            else bool(args["include_trajectories"])
        ),
        include_edits=bool(args.get("include_edits", True)),
        page_token=str(args.get("page_token", "")),
    )


# The two judgements an operator makes on the insight list. Command routes
# rather than tools the chat agent holds: they are BigQuery DML, so the
# dashboard cannot serve them itself, and a click should not depend on an LLM
# choosing to call the right function.
@router.post(f"/{INSIGHTS_DISMISS_ROUTE}")
async def dismiss_insight(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await insight_tools.dismiss_insight(
        RouteContext(), insight_id=_extract_required_field(args, "insight_id")
    )


@router.post(f"/{INSIGHTS_MERGE_ROUTE}")
async def merge_insight(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await insight_tools.merge_insight(
        RouteContext(),
        insight_id=_extract_required_field(args, "insight_id"),
        target_insight_id=_extract_required_field(args, "target_insight_id"),
    )


@router.post(f"/{TRAJECTORIES_OUTCOMES_ROUTE}")
async def count_trajectory_outcomes(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await trajectory_tools.count_trajectory_outcomes(
        RouteContext(),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{TRAJECTORIES_LIST_ROUTE}")
async def list_trajectories(request: Request) -> dict[str, Any]:
    filters = await _read_json_body(request)
    return await trajectory_tools.list_trajectories(
        RouteContext(),
        run_id=str(filters.get("run_id", "")),
        outcome=str(filters.get("outcome", "")),
        window_start=str(filters.get("window_start", "")),
        window_end=str(filters.get("window_end", "")),
        page_token=str(filters.get("page_token", "")),
    )


@router.post(f"/{TRAJECTORIES_CASE_ROUTE}")
async def get_case_conversation(request: Request) -> dict[str, Any]:
    """Retrieves one archived conversation for the dashboard's case view.

    Args:
        request: FastAPI HTTP request containing trajectory_id and run_id.

    Returns:
        Archived conversation trajectory record.
    """
    args = await _read_json_body(request)
    return await trajectory_tools.get_case_conversation(
        RouteContext(),
        trajectory_id=_extract_required_field(args, "trajectory_id"),
        run_id=str(args.get("run_id", "")),
    )


@router.post(f"/{HEALTH_ROUTE}")
async def get_ambient_health() -> dict[str, Any]:
    """Checks whether the ambient loop is active.

    Returns:
        Status dictionary for ambient health.
    """
    return await health_tools.get_ambient_health(RouteContext())


@router.post(f"/{SOURCE_ROUTE}")
async def get_source_snapshot(request: Request) -> dict[str, Any]:
    """Checks whether an agent's source code snapshot is readable.

    Args:
        request: Request whose body may carry `agent_name`, for an agent other
            than the watched one, and `revision`, for a revision other than
            the newest complete one.

    Returns:
        Status dictionary for source snapshot accessibility.

    Raises:
        HTTPException: 400 if `agent_name` or `revision` is not a valid
            snapshot key.
    """
    args = await _read_json_body(request)
    agent_name = str(args.get("agent_name") or "")
    revision = str(args.get("revision") or "")
    try:
        if agent_name:
            snapshot.validate_agent_name(agent_name)
        if revision:
            snapshot.validate_key_segment(revision, "revision")
    except ValueError as exc:
        raise _build_rejection(str(exc)) from exc
    return await source_tools.get_source_snapshot(
        RouteContext(), agent_name=agent_name, revision=revision
    )


# A source snapshot, uploaded by `aqua publish-source` in batches that each fit
# the body cap and written with the engine's credentials, so the CLI needs no
# grant on the source bucket. The manifest is committed last.
@router.post(f"/{SOURCE_UPLOAD_ROUTE}")
async def upload_source_files(request: Request) -> dict[str, Any]:
    """Writes one batch of a source snapshot's files.

    Args:
        request: Request whose body carries `agent_name` (empty for the watched
            agent), `revision` and `files`, each `{"path", "content"}` with the
            content gzipped and base64-encoded.

    Returns:
        How many files were written, or an error envelope.

    Raises:
        HTTPException: 400 if `revision` is missing or `files` is not a list.
    """
    args = await _read_json_body(request)
    revision = _extract_required_field(args, "revision")
    files = args.get("files")
    if not isinstance(files, list):
        raise _build_rejection(f"files is not a JSON list: {files!r}")
    # In a thread, as resolving the watched agent may read the jobs store.
    return await asyncio.to_thread(
        lambda: source_upload.write_files(
            agent_name=_resolve_source_agent(args),
            revision=revision,
            files=files,
        )
    )


@router.post(f"/{SOURCE_COMMIT_ROUTE}")
async def commit_source_snapshot(request: Request) -> dict[str, Any]:
    """Completes a source snapshot by writing its manifest.

    Args:
        request: Request whose body carries `agent_name` (empty for the watched
            agent), `revision` and `manifest`.

    Returns:
        A summary of the stored snapshot, or an error envelope.

    Raises:
        HTTPException: 400 if `revision` is missing or `manifest` is not an
            object.
    """
    args = await _read_json_body(request)
    revision = _extract_required_field(args, "revision")
    manifest = args.get("manifest")
    if not isinstance(manifest, dict):
        raise _build_rejection(f"manifest is not a JSON object: {manifest!r}")
    return await asyncio.to_thread(
        lambda: source_upload.commit(
            agent_name=_resolve_source_agent(args),
            revision=revision,
            manifest=manifest,
        )
    )


def _resolve_source_agent(args: dict[str, Any]) -> str:
    """Returns the agent a snapshot request names, or the watched one.

    Args:
        args: Request body.

    Returns:
        The agent name: the one given, else the one the reader would read.
    """
    return str(args.get("agent_name") or "").strip() or (
        effective_config.load(RouteContext().state).observed_agent_name
    )


@router.post(f"/{CONFIG_ROUTE}")
async def show_config() -> dict[str, Any]:
    """Returns the configuration this deployment is running under.

    AQuA's own settings from the environment, with the observed agent's taken
    from whatever `attach` stored. Read-only, and takes no arguments.

    Returns:
        Configuration dictionary for the current deployment.
    """
    return config_tools.show_config(RouteContext())


# How an agent is observed. `aqua attach` is the only writer and it writes
# through these, so the CLI needs no grant on AQuA's storage and the stored
# object is validated by the process that reads it.
@router.post(f"/{AGENTS_LIST_ROUTE}")
async def list_agents() -> dict[str, Any]:
    return await agent_config_commands.list_agents()


@router.post(f"/{AGENTS_ATTACH_ROUTE}")
async def attach_agent(request: Request) -> dict[str, Any]:
    """Merges `fields` over how the agent is observed now; a dry run only plans.

    `agent_name` may be omitted, meaning the agent the deployment's environment
    names.

    `check_access` asks a dry run to also try, as AQuA's service account, the
    reads an investigation of the result would make.

    Args:
        request: Request whose body carries `agent_name`, `fields`, `dry_run`
            and `check_access`.

    Returns:
        The plan, and whether the object was written, or an error envelope.

    Raises:
        HTTPException: 400 if `fields` is not an object, `dry_run` or
            `check_access` is not a boolean, or `check_access` is set without
            `dry_run`.
    """
    args = await _read_json_body(request)
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        raise _build_rejection(f"fields is not a JSON object: {fields!r}")
    dry_run = args.get("dry_run", False)
    if not isinstance(dry_run, bool):
        # The string "false" is truthy, so only a JSON boolean says which of
        # plan and write the caller meant.
        raise _build_rejection(f"dry_run is not a boolean: {dry_run!r}")
    check_access = args.get("check_access", False)
    if not isinstance(check_access, bool):
        raise _build_rejection(
            f"check_access is not a boolean: {check_access!r}"
        )
    if check_access and not dry_run:
        raise _build_rejection("check_access is answered only by a dry run")
    return await agent_config_commands.attach_agent(
        agent_name=str(args.get("agent_name") or ""),
        fields=fields,
        dry_run=dry_run,
        check_access=check_access,
    )


@router.post(f"/{AGENTS_DETACH_ROUTE}")
async def detach_agent(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await agent_config_commands.detach_agent(
        agent_name=_extract_required_field(args, "agent_name")
    )


@router.post(f"/{AGENTS_APPLIED_ROUTE}")
async def record_applied(request: Request) -> dict[str, Any]:
    """Records what `attach --apply` applied, and what `detach --apply` revoked.

    Args:
        request: Request whose body carries `agent_name` and at least one of
            `applied`, every Terraform-backed field as applied; `granted`, the
            grants just made; and `revoked`, the grants just revoked.

    Returns:
        The agent, when the triggers were last applied (`applied_at`), the
        fields still not in effect and the grants now recorded, or an error
        envelope.

    Raises:
        HTTPException: 400 if `agent_name` is missing, `applied` is given but
            is not an object, or `granted` or `revoked` is given but is not a
            list.
    """
    args = await _read_json_body(request)
    agent_name = _extract_required_field(args, "agent_name")
    applied = args.get("applied")
    if applied is not None and not isinstance(applied, dict):
        raise _build_rejection(f"applied is not a JSON object: {applied!r}")
    granted = args.get("granted")
    revoked = args.get("revoked")
    for key, value in (("granted", granted), ("revoked", revoked)):
        if value is not None and not isinstance(value, list):
            raise _build_rejection(f"{key} is not a JSON list: {value!r}")
    return await agent_config_commands.record_applied(
        agent_name=agent_name,
        applied=applied,
        granted=granted,
        revoked=revoked,
    )


async def _read_capped(request: Request) -> bytes:
    """Reads the request body and raises HTTP 400 if it exceeds `_MAX_BODY_BYTES`.

    Counts bytes as chunks arrive instead of relying on `Content-Length`.

    Args:
        request: FastAPI HTTP request.

    Returns:
        Raw request body bytes.

    Raises:
        HTTPException: If the body exceeds `_MAX_BODY_BYTES`.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise _build_rejection(
                f"body is larger than {_MAX_BODY_BYTES} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Reads the request body as a JSON object, ignoring `Content-Type`.

    Args:
        request: FastAPI HTTP request.

    Returns:
        Decoded JSON dictionary, or an empty dictionary if the body is empty.

    Raises:
        HTTPException: If the body is not valid JSON or not an object.
    """
    raw = await _read_capped(request)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _build_rejection(f"body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _build_rejection(f"body is not a JSON object: {payload!r}")
    return payload


def _extract_required_field(payload: dict[str, Any], key: str) -> str:
    """Returns the non-blank string value for `key`.

    Args:
        payload: JSON dictionary.
        key: Required field name.

    Returns:
        Non-empty string value for `key`.

    Raises:
        HTTPException: If `key` is missing, not a string, or blank.
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _build_rejection(f"missing required {key!r}: {payload!r}")
    return value


def _encode_tri_state(value: Any) -> str:
    """Converts a value to a ``"true"``, ``"false"``, or ``""`` filter sentinel.

    Maps ``None`` or empty string to ``""`` (no filter) rather than ``"None"``.

    Duplicated by ``ui.app._encode_tri_state`` and
    ``tools.orchestrator.insight_tools._parse_tri_state``: the UI image and the
    agent ship as separate deployables with no shared import path, so each end
    of the sentinel contract carries its own copy. Change all three together.

    Args:
        value: Input value (boolean, None, or string).

    Returns:
        Sentinel string expected by downstream tool filters.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_count(value: Any) -> int:
    """Reads a count off a JSON payload, defaulting to 0 when unreadable.

    Accepts numeric strings. Anything unreadable returns 0 (indicating no
    preference).

    Args:
        value: Input value to parse.

    Returns:
        Parsed integer count, or 0.
    """
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "ignoring unreadable integer argument %r; using 0.", value
        )
        return 0


def _build_rejection(detail: str) -> HTTPException:
    logger.warning("Rejecting a command request: %s", detail)
    return HTTPException(status_code=400, detail=detail)
