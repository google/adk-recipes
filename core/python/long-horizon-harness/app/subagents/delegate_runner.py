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

"""State-isolating wrapper around ADK's ``Runner`` for delegated children.

``AgentTool`` would forward child ``session.state`` mutations back to the
parent (see ADK's multi-agent docs at https://adk.dev/agents/multi-agents/).
The whole
point of a delegate is isolation, so this module builds a fresh
``InMemorySessionService`` + ``InMemoryMemoryService`` for every call,
runs the child, captures the final text, and discards everything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai.types import Content, Part

DELEGATE_APP_NAME = "delegate_child"
DELEGATE_USER_ID = "delegate_caller"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildPending:
    """The child paused on a tool that needs approval; surface this upward."""

    confirmation_id: str
    hint: str
    payload: Any


@dataclass
class ChildDriveResult:
    status: str  # "completed" | "timeout" | "halted" | "pending"
    summary: str
    telemetry: dict[str, Any] = field(default_factory=dict)
    pending: ChildPending | None = None


def build_resumable_child_runner(
    child: Agent, session_service: BaseSessionService
) -> Runner:
    """A resumable child runner over a durable session service so the child can
    pause for approval and resume in place. Memory stays in-memory (a delegated
    child has no cross-session memory by design)."""
    app = App(
        name=DELEGATE_APP_NAME,
        root_agent=child,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    return Runner(
        app=app,
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )


def _pending_from_event(event: Any) -> ChildPending | None:
    for fc in event.get_function_calls() or []:
        if fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
            tc = (fc.args or {}).get("toolConfirmation", {}) or {}
            return ChildPending(
                confirmation_id=fc.id or "",
                hint=str(tc.get("hint", "")),
                payload=tc.get("payload"),
            )
    return None


async def drive_child(
    runner: Runner,
    *,
    user_id: str,
    session_id: str,
    new_message: Content,
    timeout_s: float = 120.0,
    max_iterations: int | None = None,
) -> ChildDriveResult:
    """Drain one child runner pass, capturing text and detecting a HITL pause.

    Returns ``status="pending"`` with a ``ChildPending`` when the child emits an
    ``adk_request_confirmation`` (it needs user approval). The caller resumes by
    invoking ``drive_child`` again with the confirmation ``FunctionResponse``."""
    chunks: list[str] = []
    iterations = 0
    pending: ChildPending | None = None
    status = "completed"
    summary_override: str | None = None
    start = time.monotonic()

    async def _drain() -> None:
        nonlocal iterations, pending
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=new_message
        ):
            iterations += 1
            text = _event_text(event)
            if text:
                chunks.append(text)
            found = _pending_from_event(event)
            if found is not None:
                pending = found
            if max_iterations is not None and iterations >= max_iterations:
                raise _MaxIterationsExceeded()

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    except TimeoutError:
        status = "timeout"
        summary_override = _unfinished_summary(
            f"the child hit the {timeout_s}s timeout and did not finish",
            chunks,
        )
    except _MaxIterationsExceeded:
        status = "halted"
        summary_override = _unfinished_summary(
            f"max_iterations ({max_iterations}) exceeded, child halted",
            chunks,
        )
    except Exception as exc:
        _logger.exception("delegate child raised %s", type(exc).__name__)
        status = "halted"
        summary_override = f"Child crashed with {type(exc).__name__}: {exc}"

    if pending is not None:
        status = "pending"
        # A paused child has not done the work; without this the summary is
        # its pre-approval narration, which reads like a finished report.
        summary_override = _unfinished_summary(
            "the child paused for approval and has not run the operation yet",
            chunks,
            label="PAUSED",
        )

    return ChildDriveResult(
        status=status,
        summary=summary_override
        if summary_override is not None
        else _reported_summary(chunks),
        telemetry={
            "iterations": iterations,
            "duration_ms": int((time.monotonic() - start) * 1000),
        },
        pending=pending,
    )


def _build_runner(
    *,
    agent: Agent,
    session_service: InMemorySessionService,
    memory_service: InMemoryMemoryService,
) -> Runner:
    return Runner(
        app_name=DELEGATE_APP_NAME,
        agent=agent,
        session_service=session_service,
        memory_service=memory_service,
    )


def _reported_summary(chunks: list[str]) -> str:
    """A success flag with an empty body is the same vacuum a timeout left,
    and the parent fills vacuums with plausible detail. Say it reported
    nothing rather than returning ""."""
    text = "".join(chunks)
    if text.strip():
        return text
    return (
        "The child finished but reported nothing. There is no result to "
        "relay; do not describe work it might have done."
    )


def _envelope_error(
    status: str, timeout_s: float | None, max_iterations: int | None
) -> str | None:
    """Machine-readable twin of the INCOMPLETE summary: the repo signals
    failure with an `error` key and spawn.py's _result_summary reads one."""
    if status == "timeout":
        return f"Child timed out after {timeout_s}s without finishing."
    if status == "halted":
        return f"Child halted (max_iterations={max_iterations}) or crashed."
    return None


def _unfinished_summary(
    reason: str, chunks: list[str], *, label: str = "INCOMPLETE"
) -> str:
    """Partial narration reads like a finished report, so the parent must see
    the state in the summary itself, not only in the status field."""
    text = (
        f"{label}: {reason}. Do not report this work as done; anything "
        "below is partial and unverified."
    )
    partial = "".join(chunks).strip()
    return f"{text}\n\nPartial output:\n{partial}" if partial else text


def _user_message(text: str) -> Content:
    return Content(role="user", parts=[Part(text=text)])


def _event_text(event: Any) -> str:
    content = getattr(event, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(part.text for part in parts if getattr(part, "text", None))


class _MaxIterationsExceeded(Exception):
    """Internal sentinel — raised from the drain loop to short-circuit
    asyncio.wait_for so the outer envelope can report a halted status."""


async def run_delegate(
    *,
    child: Agent,
    user_message: str,
    timeout_s: float = 120.0,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Run ``child`` to completion or timeout and return a result envelope.

    Returns:
        ``{"status": "completed"|"timeout"|"halted", "summary": str,
        "telemetry": {"iterations": int, "duration_ms": int}}``
    """
    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    runner = _build_runner(
        agent=child,
        session_service=session_service,
        memory_service=memory_service,
    )

    session = await session_service.create_session(
        app_name=DELEGATE_APP_NAME, user_id=DELEGATE_USER_ID
    )

    chunks: list[str] = []
    iterations = 0
    status = "completed"
    summary_override: str | None = None
    start = time.monotonic()

    async def _drain() -> None:
        nonlocal iterations
        async for event in runner.run_async(
            user_id=DELEGATE_USER_ID,
            session_id=session.id,
            new_message=_user_message(user_message),
        ):
            iterations += 1
            text = _event_text(event)
            if text:
                chunks.append(text)
            if max_iterations is not None and iterations >= max_iterations:
                raise _MaxIterationsExceeded()

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    except TimeoutError:
        status = "timeout"
        summary_override = _unfinished_summary(
            f"the child hit the {timeout_s}s timeout and did not finish",
            chunks,
        )
    except _MaxIterationsExceeded:
        status = "halted"
        summary_override = _unfinished_summary(
            f"max_iterations ({max_iterations}) exceeded, child halted",
            chunks,
        )
    except Exception as exc:
        _logger.exception("delegate child raised %s", type(exc).__name__)
        status = "halted"
        summary_override = f"Child crashed with {type(exc).__name__}: {exc}"

    duration_ms = int((time.monotonic() - start) * 1000)

    envelope: dict[str, Any] = {
        "status": status,
        "summary": summary_override
        if summary_override is not None
        else _reported_summary(chunks),
        "telemetry": {
            "iterations": iterations,
            "duration_ms": duration_ms,
        },
    }
    error = _envelope_error(status, timeout_s, max_iterations)
    if error is not None:
        envelope["error"] = error
    return envelope
