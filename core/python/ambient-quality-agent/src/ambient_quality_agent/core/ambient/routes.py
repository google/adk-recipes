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

"""HTTP routes for ambient triggers.

Agent Runtime forwards paths under `{engine}/api/` to the container with the
`/api` prefix stripped. This module lives apart from `fast_api_app` so tests can
drive it without building the ADK app.

The platform passthrough rewrites container 5xx responses to 400. Any non-2xx
makes Pub/Sub nack and Cloud Tasks retry.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from ambient_quality_agent.core.ambient import handler
from ambient_quality_agent.core.session_state import RouteContext
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 1 << 20

router = APIRouter(prefix="/ambient")

SCHEDULE_TIME_HEADER = "X-CloudScheduler-ScheduleTime"


@router.post("/trigger")
async def receive_trigger(request: Request) -> dict[str, str]:
    """Handles Cloud Scheduler ticks and delayed Cloud Task fires.

    Args:
        request: FastAPI HTTP request.

    Returns:
        Dictionary containing execution status note.
    """
    parsed = _validate_trigger_or_reject(await _read_json_body(request))
    anchor = _parse_schedule_time(request.headers.get(SCHEDULE_TIME_HEADER))
    note = await handler.dispatch_ambient_trigger(
        RouteContext(), parsed, anchor=anchor
    )
    return {"note": note}


@router.post("/observed-update")
async def receive_observed_update(request: Request) -> dict[str, str]:
    """Defers an investigation when the observed agent updates.

    The Pub/Sub subscription uses `no_wrapper`, so the body is an audit
    `LogEntry`.

    Args:
        request: FastAPI HTTP request containing the LogEntry JSON payload.

    Returns:
        Dictionary containing execution status note.
    """
    parsed = _validate_trigger_or_reject(
        _build_update_payload(await _read_json_body(request))
    )
    return {
        "note": await handler.dispatch_ambient_trigger(RouteContext(), parsed)
    }


def _parse_schedule_time(schedule_time: str | None) -> dt.datetime | None:
    """Parses the instant Cloud Scheduler meant this fire for.

    Returns None when the header is missing or unparsable, which leaves the
    dedup key to the receiver's clock.

    Args:
        schedule_time: Raw schedule time string from request headers.

    Returns:
        Parsed datetime in UTC timezone, or None if missing/invalid.
    """
    if not schedule_time:
        logger.warning(
            "No %s on a trigger; keying the run on this container's clock.",
            SCHEDULE_TIME_HEADER,
        )
        return None
    try:
        instant = dt.datetime.fromisoformat(schedule_time)
    except ValueError:
        logger.warning(
            "Ignoring %s %r: not an RFC 3339 instant.",
            SCHEDULE_TIME_HEADER,
            schedule_time,
        )
        return None
    if instant.tzinfo is None:
        # An instant with no offset would key the run on the container's zone.
        return instant.replace(tzinfo=dt.UTC)
    return instant


def _build_update_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "update",
        "resource_name": _extract_nested_dict(entry, "protoPayload").get(
            "resourceName"
        ),
        # `operation.id` carries the project number, so no engine name is in it.
        "dedup_id": _extract_nested_dict(entry, "operation").get("id"),
    }


def _extract_nested_dict(entry: dict[str, Any], key: str) -> dict[str, Any]:
    value = entry.get(key)
    return value if isinstance(value, dict) else {}


async def _read_capped(request: Request) -> bytes:
    """Reads the request body, refusing anything over `_MAX_BODY_BYTES`.

    Reads the stream rather than trusting `content-length`, which a chunked
    sender need not send and an unfriendly one can understate.

    Args:
        request: FastAPI request to read.

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
    """Reads the body from raw bytes, ignoring incoming `Content-Type`.

    Args:
        request: FastAPI request to read.

    Returns:
        Decoded JSON dictionary.

    Raises:
        HTTPException: If the body is not valid JSON or not a JSON object.
    """
    try:
        payload = json.loads(await _read_capped(request))
    except json.JSONDecodeError as exc:
        raise _build_rejection(f"body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _build_rejection(f"body is not a JSON object: {payload!r}")
    return payload


def _validate_trigger_or_reject(
    payload: dict[str, Any],
) -> handler.AmbientTrigger:
    try:
        return handler.validate_trigger_payload(payload)
    except handler.AmbientTriggerParseError as exc:
        raise _build_rejection(str(exc)) from exc


def _build_rejection(detail: str) -> HTTPException:
    logger.warning("Rejecting an ambient request: %s", detail)
    return HTTPException(status_code=400, detail=detail)
