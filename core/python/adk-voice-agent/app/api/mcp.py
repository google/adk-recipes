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

"""Authenticated MCP bridge (`/api/v1/mcp`) serving internal tools and proxying `.mcp.json` upstreams with per-thread tokens."""

from __future__ import annotations

import inspect
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api import mcp_gateway
from app.app_utils.user_context import warm_user_context
from app.auth import identity
from app.auth.identity import acting_user_id, dev_user_id, run_as
from app.auth.providers import is_remote_deployment
from app.context_cards import CARD_KINDS
from app.threads import documents
from app.threads.describe import home_path
from app.threads.model import (
    FINISHED_STATES,
    GATE_STATES,
    RUNNING_STATES,
    ThreadHandle,
    ThreadState,
)
from app.threads.registry import ThreadNotFound, get_thread_registry
from app.tools import grounding_tools, memory_tools
from app.workers.workspace import is_code

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_INFO = {"name": "adk-voice-agent-mcp", "version": "1.0.0"}
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8000"

# MCP endpoints authenticate via per-thread Bearer tokens rather than browser IAP headers.
MCP_PUBLIC_PATHS = frozenset(
    {
        "/api/v1/mcp",
        "/api/v1/mcp/",
        "/api/v1/mcp/tools",
        "/api/v1/mcp/call",
    }
)
identity.PUBLIC_PATHS = identity.PUBLIC_PATHS | MCP_PUBLIC_PATHS

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])
mcp_router = router


@dataclass(frozen=True)
class McpTokenGrant:
    token: str
    thread_id: str
    user_id: str
    created_at: float = field(default_factory=time.time)


_tokens: dict[str, McpTokenGrant] = {}
_token_by_thread: dict[str, str] = {}


def _norm_thread_id(thread_id: str) -> str:
    return thread_id.strip().lower()


def mint_mcp_token(thread_id: str, user_id: str) -> str:
    """Mint a per-thread Bearer token, replacing any existing token for `thread_id`."""
    clean_thread = _norm_thread_id(thread_id)
    clean_user = user_id.strip()
    if not clean_thread:
        raise ValueError("mint_mcp_token requires a non-empty thread_id")
    if not clean_user:
        raise ValueError("mint_mcp_token requires a non-empty user_id")

    previous = _token_by_thread.pop(clean_thread, None)
    if previous:
        _tokens.pop(previous, None)

    token = f"voice_agent_mcp_{secrets.token_urlsafe(32)}"
    grant = McpTokenGrant(
        token=token,
        thread_id=clean_thread,
        user_id=clean_user,
    )
    _tokens[token] = grant
    _token_by_thread[clean_thread] = token
    return token


def revoke_mcp_token(thread_id: str) -> None:
    clean_thread = _norm_thread_id(thread_id)
    token = _token_by_thread.pop(clean_thread, None)
    if token:
        _tokens.pop(token, None)
    for tok, grant in list(_tokens.items()):
        if grant.thread_id == clean_thread:
            _tokens.pop(tok, None)


def clear_mcp_tokens() -> None:
    _tokens.clear()
    _token_by_thread.clear()


def verify_mcp_token(
    raw_token: str, *, thread_id_hint: str = ""
) -> McpTokenGrant | None:
    candidate = raw_token.strip()
    if not candidate:
        return None
    for stored_token, grant in list(_tokens.items()):
        if secrets.compare_digest(candidate, stored_token):
            return grant

    dev_token = (os.getenv("VOICE_AGENT_MCP_DEV_TOKEN") or "").strip()
    if (
        dev_token
        and not is_remote_deployment()
        and secrets.compare_digest(candidate, dev_token)
    ):
        resolved_thread = _norm_thread_id(thread_id_hint or "dev") or "dev"
        return McpTokenGrant(
            token=dev_token,
            thread_id=resolved_thread,
            user_id=dev_user_id(),
        )
    return None


def mcp_env_for_thread(
    thread_id: str, user_id: str, base_url: str | None = None
) -> dict[str, str]:
    token = mint_mcp_token(thread_id, user_id)
    raw_base = (
        base_url
        or os.getenv("VOICE_AGENT_MCP_BASE_URL")
        or os.getenv("APP_URL")
        or DEFAULT_LOCAL_BASE_URL
    ).strip()
    trimmed = raw_base.rstrip("/")
    mcp_url = (
        trimmed if trimmed.endswith("/api/v1/mcp") else f"{trimmed}/api/v1/mcp"
    )
    return {
        "VOICE_AGENT_MCP_URL": mcp_url,
        "VOICE_AGENT_MCP_TOKEN": token,
        "VOICE_AGENT_THREAD_ID": _norm_thread_id(thread_id),
    }


def _extract_token(request: Request) -> str:
    auth_header = (request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (request.headers.get("x-voice-agent-mcp-token") or "").strip()


async def require_mcp_grant(request: Request) -> McpTokenGrant:
    raw_token = _extract_token(request)
    if not raw_token:
        raise HTTPException(
            status_code=401,
            detail="Missing ADK Voice Agent MCP token (provide Authorization: Bearer <token> or X-Voice-Agent-Mcp-Token).",
        )
    thread_hint = (request.headers.get("x-voice-agent-thread-id") or "").strip()
    grant = verify_mcp_token(raw_token, thread_id_hint=thread_hint)
    if grant is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked ADK Voice Agent MCP token.",
        )
    return grant


ToolHandler = Callable[[McpTokenGrant, dict[str, Any]], Awaitable[Any]]


def _always() -> bool:
    return True


@dataclass(frozen=True)
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    available: Callable[[], bool] = _always

    def to_mcp_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class _SessionStub:
    def __init__(self, user_id: str) -> None:
        self.app_name = "app"
        self.user_id = user_id


class _InvocationContextStub:
    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service


class _ToolContextStub:
    def __init__(self, user_id: str, session_service: Any) -> None:
        self.session = _SessionStub(user_id)
        self._invocation_context = _InvocationContextStub(session_service)


async def _call_search_web_grounded(
    _grant: McpTokenGrant, args: dict[str, Any]
) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError('"query" is required.')
    return await grounding_tools.search_web_grounded(query=query)


async def _call_search_maps_grounded(
    _grant: McpTokenGrant, args: dict[str, Any]
) -> str:
    query = str(args.get("query") or "").strip()
    near_location = str(args.get("near_location") or "").strip()
    if not query:
        raise ValueError('"query" is required.')
    if not near_location:
        raise ValueError('"near_location" is required.')
    return await grounding_tools.search_maps_grounded(
        query=query, near_location=near_location
    )


async def _call_recall(_grant: McpTokenGrant, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError('"query" is required.')
    return await memory_tools.recall(query=query)


async def _call_recall_conversation(
    grant: McpTokenGrant, args: dict[str, Any]
) -> str:
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        raise ValueError('"session_id" is required.')
    fn = memory_tools.recall_conversation
    params = inspect.signature(fn).parameters
    if "tool_context" in params:
        from app.app_utils.services import get_session_service

        ctx = _ToolContextStub(grant.user_id, get_session_service())
        return await fn(session_id=session_id, tool_context=ctx)  # type: ignore[arg-type]
    return await fn(session_id)


_STATE_FILTERS: dict[str, frozenset[ThreadState]] = {
    "active": RUNNING_STATES | GATE_STATES,
    "finished": FINISHED_STATES,
}


async def _call_list_threads(
    _grant: McpTokenGrant, args: dict[str, Any]
) -> dict[str, Any]:
    query = str(args.get("query") or "").strip().lower()
    state_filter = str(args.get("state") or "").strip().lower()
    limit = max(1, min(int(args.get("limit") or 20), 100))

    registry = get_thread_registry()
    handles = await registry.list_all(limit=max(limit, 50))

    allowed_states: frozenset[ThreadState] | None = None
    if state_filter:
        if state_filter in _STATE_FILTERS:
            allowed_states = _STATE_FILTERS[state_filter]
        else:
            try:
                allowed_states = frozenset({ThreadState(state_filter)})
            except ValueError as exc:
                raise ValueError(
                    f"Unknown thread state filter: {state_filter}"
                ) from exc

    words = [w for w in query.split() if w]
    matched: list[dict[str, Any]] = []
    for h in handles:
        if allowed_states is not None and h.state not in allowed_states:
            continue
        if words:
            haystack = f"{h.title} {h.project} {h.goal} {h.result_text}".lower()
            if not all(w in haystack for w in words):
                continue
        matched.append(
            {
                "id": h.thread_id,
                "thread_id": h.thread_id,
                "title": h.title,
                "goal": h.goal,
                "state": h.state.value,
                "status": h.status,
                "outcome": h.outcome,
                "harness": h.harness,
                **_where(h),
                "result_text": (h.result_text or "")[:400],
                "updated_at": h.updated_at,
            }
        )
        if len(matched) >= limit:
            break

    return {
        "threads": matched,
        "count": len(matched),
    }


def _where(h: ThreadHandle) -> dict[str, Any]:
    where: dict[str, Any] = {
        "project": h.project,
        "workdir": h.workdir,
        "paths": [home_path(h, f) for f in h.files_changed],
    }
    if is_code(h.project):
        where["branch"] = h.branch
    return where


async def _call_read_thread(
    _grant: McpTokenGrant, args: dict[str, Any]
) -> dict[str, Any]:
    thread_id = str(args.get("thread_id") or args.get("id") or "").strip()
    if not thread_id:
        raise ValueError('"thread_id" is required.')
    registry = get_thread_registry()
    handle = await registry.require(thread_id)
    return {
        "id": handle.thread_id,
        "thread_id": handle.thread_id,
        "title": handle.title,
        "goal": handle.goal,
        "state": handle.state.value,
        "status": handle.status,
        "outcome": handle.outcome,
        "harness": handle.harness,
        **_where(handle),
        "iteration": handle.iteration,
        "result_text": handle.result_text,
        "error": handle.error,
        "files_changed": list(handle.files_changed),
        "diff_summary": handle.diff_summary,
        "deliverables": [
            {"path": home_path(handle, a["name"]), "title": a.get("title", "")}
            for a in handle.artifacts
            if a.get("name")
        ],
        "artifacts": handle.artifacts,
        "outputs": handle.outputs,
        "turns": [t.to_dict() for t in handle.turns],
        "events": [e.to_dict() for e in handle.events[-50:]],
        "updated_at": handle.updated_at,
    }


async def _call_update_user(grant: McpTokenGrant, args: dict[str, Any]) -> str:
    text = str(args.get("text") or args.get("message") or "").strip()
    if not text:
        raise ValueError('"text" is required.')
    await get_thread_registry().record_finding(grant.thread_id, text)
    return "Recorded."


async def _call_save_to_drive(
    grant: McpTokenGrant, args: dict[str, Any]
) -> dict[str, str]:
    path = str(args.get("path") or "").strip()
    if not path:
        raise ValueError('"path" is required.')
    return await get_thread_registry().save_to_drive(
        grant.thread_id, path, title=str(args.get("title") or "").strip()
    )


async def _call_present_card(grant: McpTokenGrant, args: dict[str, Any]) -> str:
    return await get_thread_registry().present_card(
        grant.thread_id,
        str(args.get("kind") or "").strip(),
        str(args.get("title") or "").strip(),
        args.get("items"),
    )


def _drive_saving_available() -> bool:
    return documents.drive_saving().available


MCP_TOOLS: tuple[McpToolSpec, ...] = (
    McpToolSpec(
        name="list_threads",
        description=(
            "List the user's active and recent threads in the Shared Thread Journal "
            "(id, title, goal, state, status, outcome, harness, result_text, updated_at, "
            "project, workdir, changed paths, and branch)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words that must all match title, project, goal, or result_text.",
                    "default": "",
                },
                "state": {
                    "type": "string",
                    "description": "Filter: 'active', 'finished', or a ThreadState.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max threads to return (default 20).",
                    "default": 20,
                },
            },
        },
        handler=_call_list_threads,
    ),
    McpToolSpec(
        name="read_thread",
        description=(
            "Read one thread from the Shared Thread Journal: result_text, error, turns, "
            "recent events, outputs, project, workdir, changed paths, deliverables, and branch."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Thread ID (e.g. 'thread-1a2b3c4d').",
                },
            },
            "required": ["thread_id"],
        },
        handler=_call_read_thread,
    ),
    McpToolSpec(
        name="update_user",
        description=(
            "Tell the user how the thread is going in one short speakable sentence. "
            "Report concrete findings ('Cheapest refundable flight so far is $326'), not activity."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "One speakable sentence.",
                },
            },
            "required": ["text"],
        },
        handler=_call_update_user,
    ),
    McpToolSpec(
        name="save_to_drive",
        description=(
            "Save or update a final deliverable from your working directory to this project's "
            "Google Drive folder (md/txt/html -> Doc, csv/xlsx -> Sheet, pptx -> Slides, else as-is). "
            "Returns {title, url, kind}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to your working directory (e.g. 'itinerary.md').",
                },
                "title": {
                    "type": "string",
                    "description": "Optional name in Drive (default: filename).",
                    "default": "",
                },
            },
            "required": ["path"],
        },
        handler=_call_save_to_drive,
        available=_drive_saving_available,
    ),
    McpToolSpec(
        name="present_card",
        description=(
            "Call once at the end when your answer is a list of events, email threads, "
            "or files. Pass items exactly as the Google tools returned them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(CARD_KINDS),
                    "description": "What the items are.",
                },
                "title": {
                    "type": "string",
                    "description": "A short heading (e.g. 'Tomorrow's meetings').",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "The items, as the Google tools returned them.",
                },
            },
            "required": ["kind", "title", "items"],
        },
        handler=_call_present_card,
    ),
    McpToolSpec(
        name="recall",
        description=(
            "Search what ADK Voice Agent remembers of earlier conversations and threads with the user."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search in user memory.",
                },
            },
            "required": ["query"],
        },
        handler=_call_recall,
    ),
    McpToolSpec(
        name="recall_conversation",
        description=(
            "Read the transcript of an earlier conversation with the user by its session ID."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Conversation session ID from `recall`.",
                },
            },
            "required": ["session_id"],
        },
        handler=_call_recall_conversation,
    ),
    McpToolSpec(
        name="search_web_grounded",
        description=(
            "Search the live web using Google Search grounding for up-to-date facts and documentation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to look up.",
                },
            },
            "required": ["query"],
        },
        handler=_call_search_web_grounded,
    ),
    McpToolSpec(
        name="search_maps_grounded",
        description=(
            "Search places, businesses, and routes using Google Maps grounding."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Place or route query.",
                },
                "near_location": {
                    "type": "string",
                    "description": "City, neighborhood, or address.",
                },
            },
            "required": ["query", "near_location"],
        },
        handler=_call_search_maps_grounded,
    ),
)

_TOOLS_BY_NAME: dict[str, McpToolSpec] = {t.name: t for t in MCP_TOOLS}


def _format_tool_output(outcome: Any) -> dict[str, Any]:
    if isinstance(outcome, mcp_gateway.CallToolResult):
        return outcome
    if isinstance(outcome, str):
        return {
            "content": [{"type": "text", "text": outcome}],
            "isError": False,
        }
    text = json.dumps(outcome, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": outcome,
        "isError": False,
    }


async def _invoke_tool(
    grant: McpTokenGrant, tool_name: str, arguments: dict[str, Any]
) -> Any:
    spec = _TOOLS_BY_NAME.get(tool_name)
    if spec is None and mcp_gateway.split_gateway_tool(tool_name) is None:
        raise KeyError(f"Unknown MCP tool: {tool_name}")
    with run_as(grant.user_id):
        await warm_user_context(grant.user_id)
        if spec is None:
            return await mcp_gateway.call_gateway_tool(
                grant.thread_id, tool_name, arguments
            )
        if not spec.available():
            raise ValueError(f"{tool_name} isn't available to this thread.")
        return await spec.handler(grant, arguments)


def _is_known_tool(tool_name: str) -> bool:
    return (
        tool_name in _TOOLS_BY_NAME
        or mcp_gateway.split_gateway_tool(tool_name) is not None
    )


async def _list_tools(grant: McpTokenGrant) -> list[dict[str, Any]]:
    with run_as(grant.user_id):
        await warm_user_context(grant.user_id)
        own = [spec.to_mcp_dict() for spec in MCP_TOOLS if spec.available()]
        proxied = await mcp_gateway.list_gateway_tools(grant.user_id)
    return own + proxied


class McpCallRequest(BaseModel):
    tool: str = ""
    name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.get("/tools")
async def mcp_list_tools(request: Request) -> dict[str, Any]:
    grant = await require_mcp_grant(request)
    tools = await _list_tools(grant)
    with run_as(grant.user_id):
        return {
            "thread_id": grant.thread_id,
            "user_id": acting_user_id(),
            "tools": tools,
        }


@router.post("/call")
async def mcp_call_tool(
    req: McpCallRequest,
    request: Request,
) -> dict[str, Any]:
    grant = await require_mcp_grant(request)
    tool_name = (req.tool or req.name).strip()
    if not tool_name:
        raise HTTPException(status_code=400, detail='"tool" is required.')
    if not _is_known_tool(tool_name):
        raise HTTPException(
            status_code=404, detail=f"Unknown MCP tool: {tool_name}"
        )
    try:
        outcome = await _invoke_tool(grant, tool_name, req.arguments or {})
    except ThreadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    formatted = _format_tool_output(outcome)
    return {
        "tool": tool_name,
        "thread_id": grant.thread_id,
        "result": outcome,
        **formatted,
    }


async def _handle_jsonrpc_message(
    grant: McpTokenGrant, payload: dict[str, Any]
) -> dict[str, Any]:
    req_id = payload.get("id")
    method = str(payload.get("method") or "").strip()
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32602,
                "message": "Invalid params: expected object.",
            },
        }

    if method == "initialize":
        requested_version = str(
            params.get("protocolVersion") or MCP_PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": MCP_SERVER_INFO,
            },
        }

    if method in {"notifications/initialized", "initialized"}:
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": await _list_tools(grant)},
        }

    if method == "tools/call":
        tool_name = str(params.get("name") or params.get("tool") or "").strip()
        arguments = params.get("arguments") or {}
        if not tool_name or not _is_known_tool(tool_name):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": f"Unknown MCP tool: {tool_name}",
                },
            }
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": "Tool arguments must be an object.",
                },
            }
        try:
            outcome = await _invoke_tool(grant, tool_name, arguments)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            logger.warning("MCP tool %s failed: %s", tool_name, exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _format_tool_output(outcome),
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


@router.post("")
@router.post("/", include_in_schema=False)
async def mcp_jsonrpc(request: Request) -> Any:
    grant = await require_mcp_grant(request)
    try:
        body = await request.json()
    except ValueError as exc:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": f"Parse error: {exc}"},
        }

    if isinstance(body, list):
        return [
            await _handle_jsonrpc_message(grant, item)
            for item in body
            if isinstance(item, dict)
        ]
    if not isinstance(body, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    return await _handle_jsonrpc_message(grant, body)


__all__ = [
    "MCP_TOOLS",
    "McpTokenGrant",
    "McpToolSpec",
    "clear_mcp_tokens",
    "mcp_env_for_thread",
    "mcp_router",
    "mint_mcp_token",
    "revoke_mcp_token",
    "router",
    "verify_mcp_token",
]
