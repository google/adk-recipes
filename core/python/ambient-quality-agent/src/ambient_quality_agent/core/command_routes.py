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
from collections.abc import Callable
from typing import Any

from ambient_quality_agent.core.investigation import job_scheduling, model
from ambient_quality_agent.core.session_state import RouteContext
from ambient_quality_agent.tools.documents import agent_scope
from ambient_quality_agent.tools.metrics import publishing as metrics_publishing
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
    MAX_BODY_BYTES,
    MEMORIES_DELETE_ROUTE,
    MEMORIES_LIST_ROUTE,
    METRICS_DESTROY_ROUTE,
    METRICS_LIST_ROUTE,
    METRICS_PUBLISH_ROUTE,
    SOURCE_COMMIT_ROUTE,
    SOURCE_ROUTE,
    SOURCE_UPLOAD_ROUTE,
    TRAJECTORIES_CASE_ROUTE,
    TRAJECTORIES_LIST_ROUTE,
    TRAJECTORIES_OUTCOMES_ROUTE,
)
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(f"/{INVESTIGATIONS_LIST_ROUTE}")
async def list_investigations(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await model.list_investigations(
        await _build_route_context(window),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{INVESTIGATIONS_GET_ROUTE}")
async def get_investigation(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    run_id = _extract_required_field(args, "run_id")
    return await model.get_investigation(
        run_id, await _build_route_context(args)
    )


@router.post(f"/{INVESTIGATIONS_STATS_ROUTE}")
async def get_investigation_stats(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await model.get_investigation_stats(
        await _build_route_context(window),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{INVESTIGATIONS_DAILY_ROUTE}")
async def get_daily_trends(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await model.get_daily_trends(
        await _build_route_context(window),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{INVESTIGATIONS_SCHEDULE_ROUTE}")
async def schedule_investigation(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await job_scheduling.schedule_investigation(
        await _build_route_context(args)
    )


# The goal and the memories. Served here rather than read from GCS by the
# dashboard, so that the object layout, the id derivation and the write path
# have one definition, shared with the chat's goal and memory tools. Each agent
# has its own, and an `agent_key` in the body selects whose
# (`documents.agent_scope`).
@router.post(f"/{GOAL_GET_ROUTE}")
async def get_goal(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await document_tools.get_goal(await _build_route_context(args))


@router.post(f"/{GOAL_SET_ROUTE}")
async def set_goal(request: Request) -> dict[str, Any]:
    payload = await _read_json_body(request)
    return await document_tools.set_goal(
        await _build_route_context(payload), goal=str(payload.get("goal", ""))
    )


@router.post(f"/{GOAL_VERSIONS_ROUTE}")
async def list_goal_versions(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await document_tools.list_goal_versions(
        await _build_route_context(args)
    )


@router.post(f"/{MEMORIES_LIST_ROUTE}")
async def list_memories(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await document_tools.list_memories(await _build_route_context(args))


@router.post(f"/{MEMORIES_DELETE_ROUTE}")
async def delete_memory(request: Request) -> dict[str, Any]:
    payload = await _read_json_body(request)
    return await document_tools.delete_memory(
        await _build_route_context(payload),
        memory_id=str(payload.get("memory_id", "")),
    )


@router.post(f"/{INSIGHTS_LIST_ROUTE}")
async def list_insights(request: Request) -> dict[str, Any]:
    filters = await _read_json_body(request)
    return await insight_tools.list_insights(
        await _build_route_context(filters),
        status=str(filters.get("status", "")),
        run_id=str(filters.get("run_id", "")),
        has_root_cause=_encode_tri_state(filters.get("has_root_cause")),
        page_token=str(filters.get("page_token", "")),
        window_start=str(filters.get("window_start", "")),
        window_end=str(filters.get("window_end", "")),
        order_by=str(filters.get("order_by", "")),
        page_size=_parse_count(filters.get("page_size")),
        day=str(filters.get("day", "")),
        page=_parse_count(filters.get("page")),
    )


@router.post(f"/{INSIGHTS_GET_ROUTE}")
async def get_insight(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await insight_tools.get_insight(
        await _build_route_context(args),
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
        await _build_route_context(args),
        insight_id=_extract_required_field(args, "insight_id"),
    )


@router.post(f"/{INSIGHTS_MERGE_ROUTE}")
async def merge_insight(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await insight_tools.merge_insight(
        await _build_route_context(args),
        insight_id=_extract_required_field(args, "insight_id"),
        target_insight_id=_extract_required_field(args, "target_insight_id"),
    )


@router.post(f"/{TRAJECTORIES_OUTCOMES_ROUTE}")
async def count_trajectory_outcomes(request: Request) -> dict[str, Any]:
    window = await _read_json_body(request)
    return await trajectory_tools.count_trajectory_outcomes(
        await _build_route_context(window),
        window_start=str(window.get("window_start", "")),
        window_end=str(window.get("window_end", "")),
    )


@router.post(f"/{TRAJECTORIES_LIST_ROUTE}")
async def list_trajectories(request: Request) -> dict[str, Any]:
    filters = await _read_json_body(request)
    return await trajectory_tools.list_trajectories(
        await _build_route_context(filters),
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
        await _build_route_context(args),
        trajectory_id=_extract_required_field(args, "trajectory_id"),
        run_id=str(args.get("run_id", "")),
    )


@router.post(f"/{HEALTH_ROUTE}")
async def get_ambient_health(request: Request) -> dict[str, Any]:
    """Checks whether the ambient loop is active.

    Args:
        request: Request whose body may carry `agent_key`.

    Returns:
        Status dictionary for ambient health.
    """
    args = await _read_json_body(request)
    return await health_tools.get_ambient_health(
        await _build_route_context(args)
    )


@router.post(f"/{SOURCE_ROUTE}")
async def get_source_snapshot(request: Request) -> dict[str, Any]:
    """Checks whether an agent's source code snapshot is readable.

    Args:
        request: Request whose body may carry `agent_key` (or `agent_name`),
            for an agent other than the default one, and `revision`, for a
            revision other than the newest complete one.

    Returns:
        Status dictionary for source snapshot accessibility.

    Raises:
        HTTPException: 400 if the agent is not attached, or its key or
            `revision` is not a valid snapshot key.
    """
    args = await _read_json_body(request)
    context, _ = await _build_source_route_context(args)
    revision = str(args.get("revision") or "")
    try:
        if revision:
            snapshot.validate_key_segment(revision, "revision")
    except ValueError as exc:
        raise _build_rejection(str(exc)) from exc
    return await source_tools.get_source_snapshot(context, revision=revision)


# A source snapshot, uploaded by `aqua publish-source` in batches that each fit
# the body cap and written with the engine's credentials, so the CLI needs no
# grant on the source bucket. The manifest is committed last.
@router.post(f"/{SOURCE_UPLOAD_ROUTE}")
async def upload_source_files(request: Request) -> dict[str, Any]:
    """Writes one batch of a source snapshot's files.

    Args:
        request: Request whose body carries `agent_key` (or `agent_name`;
            neither for the default agent), `revision` and `files`, each
            `{"path", "content"}` with the content gzipped and base64-encoded.

    Returns:
        How many files were written, or an error envelope.

    Raises:
        HTTPException: 400 if the agent is not attached or its key is not a
            valid snapshot key, `revision` is missing, or `files` is not a
            list.
    """
    args = await _read_json_body(request)
    revision = _extract_required_field(args, "revision")
    files = args.get("files")
    if not isinstance(files, list):
        raise _build_rejection(f"files is not a JSON list: {files!r}")
    _, agent_key = await _build_source_route_context(args)
    return await asyncio.to_thread(
        lambda: source_upload.write_files(
            agent_key=agent_key, revision=revision, files=files
        )
    )


@router.post(f"/{SOURCE_COMMIT_ROUTE}")
async def commit_source_snapshot(request: Request) -> dict[str, Any]:
    """Completes a source snapshot by writing its manifest.

    Args:
        request: Request whose body carries `agent_key` (or `agent_name`;
            neither for the default agent), `revision` and `manifest`.

    Returns:
        A summary of the stored snapshot, or an error envelope.

    Raises:
        HTTPException: 400 if the agent is not attached, its key is not a
            valid snapshot key or it is attached for another engine than the
            manifest's, `revision` is missing, or `manifest` is not an object.
    """
    args = await _read_json_body(request)
    revision = _extract_required_field(args, "revision")
    manifest = args.get("manifest")
    if not isinstance(manifest, dict):
        raise _build_rejection(f"manifest is not a JSON object: {manifest!r}")
    context, agent_key = await _build_source_route_context(args)
    # In a thread, as reading the attachment may read the jobs store.
    await asyncio.to_thread(
        _validate_snapshot_engine, context, agent_key, manifest
    )
    return await asyncio.to_thread(
        lambda: source_upload.commit(
            agent_key=agent_key, revision=revision, manifest=manifest
        )
    )


async def _build_source_route_context(
    args: dict[str, Any],
) -> tuple[RouteContext, str]:
    """Builds a snapshot route's context and the key of the agent it selects.

    A CLI that sends no `agent_key` names the agent by `agent_name`, which is
    read as a key. A name that no attachment is stored under is refused,
    rather than its snapshot being stored where no reader looks.

    Args:
        args: Request body.

    Returns:
        A `RouteContext` whose state selects the agent, or selects none when
        the body names none, and the key the agent's snapshots are stored
        under; empty when there is no agent at all.

    Raises:
        HTTPException: 400 if the agent the body names is not attached, or
            its key cannot be a snapshot prefix.
    """
    if not args.get("agent_key") and args.get("agent_name"):
        args = {**args, "agent_key": args["agent_name"]}
    context = await _build_route_context(args)
    # In a thread, as resolving the agent may read the jobs store.
    agent_key = await asyncio.to_thread(
        lambda: effective_config.load(context.state).resolve_agent_key()
    )
    if agent_key:
        try:
            snapshot.validate_agent_key(agent_key)
        except ValueError as exc:
            raise _build_rejection(str(exc)) from exc
    return context, agent_key


def _validate_snapshot_engine(
    context: RouteContext, agent_key: str, manifest: dict[str, Any]
) -> None:
    """Refuses a snapshot of another engine than the one the agent observes.

    A CLI that sends no `agent_key` sends an agent's bare name, which can be
    the key of the same agent in AQuA's own project. Checked at the commit,
    which is what makes a snapshot readable, so that one agent's code does
    not become another's newest revision.

    Args:
        context: The request's context, which selects the agent.
        agent_key: Key of the agent the snapshot is stored under.
        manifest: The manifest the CLI sent, whose `engine` is the observed
            engine.

    Raises:
        HTTPException: 400 if both name an engine, and not the same one.
    """
    try:
        record = effective_config.load_selected_agent_record(context.state)
    except Exception:
        # An attachment that cannot be read names no engine to check against;
        # refusing would block every upload until the store recovers.
        logger.warning(
            "could not read %s's attachment; not checking the snapshot's "
            "engine",
            agent_key,
            exc_info=True,
        )
        record = None
    attached = record.observed_engine_id if record else ""
    sent = str(manifest.get("engine") or "")
    if (
        attached
        and sent
        and attached.rsplit("/", 1)[-1] != sent.rsplit("/", 1)[-1]
    ):
        raise _build_rejection(
            f"{agent_key} observes engine {attached}, not {sent}; publish the "
            "snapshot under the key of the agent that observes it, from "
            "`agents-cli aqua list-agents`"
        )


@router.post(f"/{CONFIG_ROUTE}")
async def show_config(request: Request) -> dict[str, Any]:
    """Returns the configuration this deployment is running under.

    AQuA's own settings from the environment, with the observed agent's taken
    from whatever `attach` stored. Read-only.

    Args:
        request: Request whose body may carry `agent_key`.

    Returns:
        Configuration dictionary for the current deployment.
    """
    args = await _read_json_body(request)
    context = await _build_route_context(args)
    # In a thread, as resolving the agent may read the jobs store.
    return await asyncio.to_thread(config_tools.show_config, context)


# The code-metric library, sent by `aqua metrics` and written with the engine's
# credentials, so the CLI needs no grant on the metrics bucket. The engine
# validates a library with the loader that runs it. Each agent has its own; an
# `agent_key` in the body selects whose.
@router.post(f"/{METRICS_LIST_ROUTE}")
async def list_metrics(request: Request) -> dict[str, Any]:
    """Lists an agent's code-metric library and validates it.

    Args:
        request: Request whose body may carry `agent_key`.

    Returns:
        The library's files and its report, or an error envelope.

    Raises:
        HTTPException: 400 if `agent_key` names no attached agent.
    """
    # Listing reads the store, and validating loads the metrics.
    return await _run_metrics_command(
        await _read_json_body(request),
        lambda agent_key: metrics_publishing.list_published(
            agent_key=agent_key
        ),
    )


@router.post(f"/{METRICS_PUBLISH_ROUTE}")
async def publish_metrics(request: Request) -> dict[str, Any]:
    """Validates a code-metric library and mirrors it into the metrics store.

    Args:
        request: Request whose body carries `files`, each `{"path",
            "content"}` with the content base64-encoded, and optionally
            `dry_run` and `agent_key`.

    Returns:
        The report and the mirror plan, and whether it was carried out, or an
        error envelope.

    Raises:
        HTTPException: 400 if `dry_run` is not a boolean, or `agent_key` names
            no attached agent.
    """
    args = await _read_json_body(request)
    dry_run = _extract_dry_run(args)
    return await _run_metrics_command(
        args,
        lambda agent_key: metrics_publishing.publish(
            args.get("files"), agent_key=agent_key, dry_run=dry_run
        ),
    )


@router.post(f"/{METRICS_DESTROY_ROUTE}")
async def destroy_metrics(request: Request) -> dict[str, Any]:
    """Deletes the published code-metric library.

    Args:
        request: Request whose body may carry `agent_key`, and `dry_run`,
            which defaults to True: only `dry_run: false` deletes.

    Returns:
        What was, or would be, deleted, or an error envelope.

    Raises:
        HTTPException: 400 if `dry_run` is not a boolean, or `agent_key` names
            no attached agent.
    """
    args = await _read_json_body(request)
    # Plans unless told otherwise, as `aqua metrics destroy` does.
    dry_run = _extract_dry_run(args, default=True)
    return await _run_metrics_command(
        args,
        lambda agent_key: metrics_publishing.destroy(
            agent_key=agent_key, dry_run=dry_run
        ),
    )


async def _run_metrics_command(
    args: dict[str, Any], command: Callable[[str], dict[str, Any]]
) -> dict[str, Any]:
    """Runs a metrics command on the library of the agent a request selects.

    Args:
        args: Request body; its optional `agent_key` selects the agent, else
            the default agent.
        command: The command, given the agent's key; empty when no agent is
            attached.

    Returns:
        The command's answer, or an error envelope when the request selects no
        agent and the attachments cannot be read to find the default one.

    Raises:
        HTTPException: 400 if `agent_key` names no attached agent.
    """
    context = await _build_route_context(args)

    def run() -> dict[str, Any]:
        try:
            agent_key = agent_scope.resolve_selected_agent_key(context.state)
        except Exception as exc:
            logger.warning(
                "metrics: could not resolve the default agent: %s", exc
            )
            return {
                "error": "Could not read the attached agents to find the "
                f"default one: {type(exc).__name__}: {exc}"
            }
        return command(agent_key)

    # In a thread: resolving the agent and the command both read storage.
    return await asyncio.to_thread(run)


def _extract_dry_run(args: dict[str, Any], *, default: bool = False) -> bool:
    """Extracts a request's `dry_run`.

    Args:
        args: Request body.
        default: What a body without `dry_run` asks for.

    Returns:
        Whether the request asks for a dry run.

    Raises:
        HTTPException: 400 if `dry_run` is not a boolean.
    """
    dry_run = args.get("dry_run", default)
    if not isinstance(dry_run, bool):
        # The string "false" is truthy, so only a JSON boolean says which of
        # plan and write the caller meant.
        raise _build_rejection(f"dry_run is not a boolean: {dry_run!r}")
    return dry_run


# How an agent is observed. `aqua attach` and the dashboard's schedule editor
# write only through these, so neither needs a grant on AQuA's storage and the
# stored object is validated by the process that reads it.
@router.post(f"/{AGENTS_LIST_ROUTE}")
async def list_agents() -> dict[str, Any]:
    return await agent_config_commands.list_agents()


@router.post(f"/{AGENTS_ATTACH_ROUTE}")
async def attach_agent(request: Request) -> dict[str, Any]:
    """Merges `fields` over how the agent is observed now; a dry run only plans.

    `agent_key`, when given, names the existing attachment to change;
    otherwise `agent_name` and the agent's project build the key.

    `check_access` asks a dry run to also try, as AQuA's service account, the
    reads an investigation of the result would make. `fill_from_table` asks
    such a dry run to first fill in what the telemetry table decides and
    `fields` leaves out.

    Args:
        request: Request whose body carries `agent_name` or `agent_key`,
            `fields`, `dry_run`, `check_access` and `fill_from_table`.

    Returns:
        The plan, and whether the object was written, or an error envelope.

    Raises:
        HTTPException: 400 if `fields` is not an object, `dry_run`,
            `check_access` or `fill_from_table` is not a boolean,
            `check_access` is set without `dry_run`, or `fill_from_table`
            without `check_access`.
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
    fill_from_table = args.get("fill_from_table", False)
    if not isinstance(fill_from_table, bool):
        raise _build_rejection(
            f"fill_from_table is not a boolean: {fill_from_table!r}"
        )
    if fill_from_table and not check_access:
        raise _build_rejection(
            "fill_from_table is answered only by a dry run with the access check"
        )
    return await agent_config_commands.attach_agent(
        agent_name=str(args.get("agent_name") or ""),
        fields=fields,
        dry_run=dry_run,
        check_access=check_access,
        fill_from_table=fill_from_table,
        agent_key=str(args.get("agent_key") or ""),
    )


@router.post(f"/{AGENTS_DETACH_ROUTE}")
async def detach_agent(request: Request) -> dict[str, Any]:
    args = await _read_json_body(request)
    return await agent_config_commands.detach_agent(
        agent_key=_extract_agent_key(args)
    )


@router.post(f"/{AGENTS_APPLIED_ROUTE}")
async def record_applied(request: Request) -> dict[str, Any]:
    """Records what `attach` wired and granted, and what `detach` revoked.

    Args:
        request: Request whose body carries `agent_key` (or `agent_name`,
            when it carries no `agent_key`) and at least one of
            `applied`, every update trigger field as wired; `granted`, the
            grants just made; and `revoked`, the grants just revoked.

    Returns:
        The agent, when the triggers were last applied (`applied_at`), the
        fields still not in effect and the grants now recorded, or an error
        envelope.

    Raises:
        HTTPException: 400 if `agent_key` is missing, `applied` is given but
            is not an object, or `granted` or `revoked` is given but is not a
            list.
    """
    args = await _read_json_body(request)
    agent_key = _extract_agent_key(args)
    applied = args.get("applied")
    if applied is not None and not isinstance(applied, dict):
        raise _build_rejection(f"applied is not a JSON object: {applied!r}")
    granted = args.get("granted")
    revoked = args.get("revoked")
    for key, value in (("granted", granted), ("revoked", revoked)):
        if value is not None and not isinstance(value, list):
            raise _build_rejection(f"{key} is not a JSON list: {value!r}")
    return await agent_config_commands.record_applied(
        agent_key=agent_key,
        applied=applied,
        granted=granted,
        revoked=revoked,
    )


async def _build_route_context(args: dict[str, Any]) -> RouteContext:
    """Builds the context a route runs under, with the agent the body selects.

    The key is checked here, once per request, so that every route answers an
    unknown agent with the same 400, whatever its tool would make of the
    error.

    Args:
        args: Request body; its optional `agent_key` selects the agent.

    Returns:
        A `RouteContext` whose state selects the agent, or selects none when
        the body names none.

    Raises:
        HTTPException: 400 if `agent_key` is not a string, or names no
            attached agent.
    """
    agent_key = args.get("agent_key")
    if agent_key is None:
        return RouteContext()
    if not isinstance(agent_key, str):
        raise _build_rejection(f"agent_key is not a string: {agent_key!r}")
    if not agent_key.strip():
        return RouteContext()
    state = {effective_config.AGENT_KEY_STATE: agent_key.strip()}
    try:
        # In a thread, as resolving the agent may read the jobs store.
        await asyncio.to_thread(effective_config.load, state)
    except effective_config.UnknownAgentError as exc:
        raise _build_rejection(str(exc)) from exc
    return RouteContext(state=state)


async def _read_capped(request: Request) -> bytes:
    """Reads the request body and raises HTTP 400 if it exceeds `MAX_BODY_BYTES`.

    Counts bytes as chunks arrive instead of relying on `Content-Length`.

    Args:
        request: FastAPI HTTP request.

    Returns:
        Raw request body bytes.

    Raises:
        HTTPException: If the body exceeds `MAX_BODY_BYTES`.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _build_rejection(
                f"body is larger than {MAX_BODY_BYTES} bytes"
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


def _extract_agent_key(payload: dict[str, Any]) -> str:
    """Returns the attachment a request names: `agent_key`, else `agent_name`.

    A CLI that sends no `agent_key` names the attachment by `agent_name`,
    which is what an agent that keeps its bare name is stored under.

    Args:
        payload: JSON dictionary.

    Returns:
        Non-empty key.

    Raises:
        HTTPException: If neither is a non-blank string.
    """
    if isinstance(payload.get("agent_name"), str) and not payload.get(
        "agent_key"
    ):
        return _extract_required_field(payload, "agent_name")
    return _extract_required_field(payload, "agent_key")


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
