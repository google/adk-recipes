# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GET /lha/state — read-only session snapshot for the chat UI side panels.

Reads keys the agent already writes (halt_reason, skill_telemetry,
iteration counters) and returns a JSON shape consumable by the panels.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from google.adk.runners import Runner

from app.auth import current_user_id
from app.conversation.iteration_budget_plugin import (
    _HALT_REASON_STATE_KEY,
    _HALTED_STATE_KEY,
    _TOOL_CALLS_STATE_KEY,
    ITERATION_STATE_KEY,
)
from app.conversation.session_start import (
    SESSION_STARTED_AT_STATE_KEY,
    SESSION_STARTED_STATE_KEY,
    get_provisioning_status,
)
from app.guardrails.halt_consumer import HALT_REASON_STATE_KEY
from app.guardrails.repeated_failure import LAST_ERROR_STATE_KEY
from app.infrastructure.constants import APP_NAME
from app.memory.skill_telemetry import SKILL_TELEMETRY_STATE_KEY
from app.telemetry.ui import (
    DELEGATE_HISTORY_STATE_KEY,
    IN_FLIGHT_TOOL_CALL_STATE_KEY,
    MEMORY_WRITES_STATE_KEY,
    TOOL_CALL_LOG_STATE_KEY,
)
from app.tools.skill_reload import BOUND_SKILLS_STATE_KEY

logger = logging.getLogger(__name__)


def _latest_pending_confirmation(events: Any) -> dict[str, Any] | None:
    """Walk session events newest-first; return the most recent unresolved
    ``request_confirmation`` payload, or None if no confirmation is open.

    A confirmation is unresolved when no later event carries a
    function_response for the same ``function_call_id`` — i.e., the user
    has already approved/declined.
    """
    if not events:
        return None
    resolved: set[str] = set()
    for event in reversed(list(events)):
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            fr = getattr(part, "function_response", None)
            if fr is None:
                continue
            name = getattr(fr, "name", "") or ""
            if name != "adk_request_confirmation":
                continue
            fcid = getattr(fr, "id", None) or name
            if fcid:
                resolved.add(str(fcid))
        actions = getattr(event, "actions", None)
        requested = getattr(actions, "requested_tool_confirmations", None) or {}
        for fcid, confirmation in requested.items():
            if str(fcid) in resolved:
                continue
            payload = getattr(confirmation, "payload", None)
            hint = getattr(confirmation, "hint", None)
            return {
                "interrupt_id": str(fcid),
                "hint": hint,
                "payload": payload,
            }
    return None


def _latest_pending_credential(events: Any) -> dict[str, Any] | None:
    """Walk session events newest-first; return the most recent unresolved
    ``request_credential`` payload (``{interrupt_id, provider, auth_url,
    scopes}``), or None if no consent flow is open.

    A credential request is resolved when a later event carries a
    state-delta writing either ``oauth:<credential_key>`` (the Horizon OAuth
    callback's persistent marker) or ``temp:<credential_key>`` (ADK's
    standard auth_preprocessor stash, kept for interop).
    """
    if not events:
        return None
    resolved_keys: set[str] = set()
    for event in reversed(list(events)):
        actions = getattr(event, "actions", None)
        delta = getattr(actions, "state_delta", None) or {}
        for key in delta:
            if not isinstance(key, str):
                continue
            if key.startswith("oauth:"):
                resolved_keys.add(key[len("oauth:") :])
            elif key.startswith("temp:"):
                resolved_keys.add(key[len("temp:") :])
        requested = getattr(actions, "requested_auth_configs", None) or {}
        for fcid, config in requested.items():
            credential_key = getattr(config, "credential_key", None) or ""
            if credential_key and credential_key in resolved_keys:
                continue
            exchanged = getattr(config, "exchanged_auth_credential", None)
            oauth2 = getattr(exchanged, "oauth2", None) if exchanged else None
            auth_url = getattr(oauth2, "auth_uri", None) if oauth2 else None
            scheme = getattr(config, "auth_scheme", None)
            flows = getattr(scheme, "flows", None) if scheme else None
            ac_flow = (
                getattr(flows, "authorizationCode", None) if flows else None
            )
            scope_map = getattr(ac_flow, "scopes", None) if ac_flow else None
            scopes = (
                list(scope_map.keys()) if isinstance(scope_map, dict) else []
            )
            return {
                "interrupt_id": str(fcid),
                "provider": credential_key,
                "auth_url": auth_url,
                "scopes": scopes,
            }
    return None


def _slice_state(
    state: dict[str, Any], *, events: Any = None, user_id: str | None = None
) -> dict[str, Any]:
    return {
        "skill_telemetry": state.get(SKILL_TELEMETRY_STATE_KEY) or {},
        "bound_skills": state.get(BOUND_SKILLS_STATE_KEY) or [],
        "halt_reason": state.get(HALT_REASON_STATE_KEY),
        "last_error": state.get(LAST_ERROR_STATE_KEY),
        "iteration": state.get(ITERATION_STATE_KEY, 0),
        "tool_calls_this_iteration": state.get(_TOOL_CALLS_STATE_KEY, 0),
        "iteration_halted": bool(state.get(_HALTED_STATE_KEY, False)),
        "iteration_halt_reason": state.get(_HALT_REASON_STATE_KEY),
        "session_started": bool(state.get(SESSION_STARTED_STATE_KEY, False)),
        "session_started_at": state.get(SESSION_STARTED_AT_STATE_KEY),
        "pending_confirmation": _latest_pending_confirmation(events),
        "pending_credential": _latest_pending_credential(events),
        "tool_call_log": state.get(TOOL_CALL_LOG_STATE_KEY) or [],
        "in_flight_tool_call": state.get(IN_FLIGHT_TOOL_CALL_STATE_KEY) or None,
        "delegate_history": state.get(DELEGATE_HISTORY_STATE_KEY) or [],
        "memory_writes": state.get(MEMORY_WRITES_STATE_KEY) or [],
        "sandbox_status": get_provisioning_status(user_id) if user_id else None,
    }


def attach_state_routes(app: FastAPI, *, runner: Runner) -> None:
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.get("/state")
    async def get_state(
        context_id: str = Query(
            ..., description="A2A context id == ADK session id"
        ),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            session = await runner.session_service.get_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=context_id,
            )
        except Exception as err:
            logger.exception("lha_state: get_session failed")
            raise HTTPException(
                status_code=500, detail="session lookup failed"
            ) from err
        if session is None:
            return {
                "contextId": context_id,
                "exists": False,
                "state": _slice_state({}, events=None, user_id=user_id),
            }
        return {
            "contextId": context_id,
            "exists": True,
            "state": _slice_state(
                dict(session.state),
                events=getattr(session, "events", None),
                user_id=user_id,
            ),
        }

    app.include_router(router)
