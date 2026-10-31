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

"""Run slow tools without stalling the Gemini Live audio stream.

Native Live sessions use async-generator tools with tool-wide `SILENT` scheduling
(so ADK's initial placeholder does not trigger a second spoken turn) and yield
real results with `WHEN_IDLE`. Cascaded sessions receive a plain twin that returns
immediately and delivers results via `ThreadUpdates`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from google.adk.models import LlmRequest
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from app.app_utils.timing import log_latency
from app.threads.model import ThreadHandle
from app.threads.registry import get_thread_registry
from app.tools.thread_tools import SINCE_SEQ_KEY, SINCE_SEQS_KEY
from app.voice.background import spawn
from app.voice.thread_updates import (
    BACKGROUND_UPDATE,
    TOOL_RESULT,
    format_approach,
    format_milestone,
    format_milestones,
    format_still_working,
    get_thread_updates,
    mark_delivered_seen,
    tool_result_update,
)
from app.voice.voice_mode import is_cascaded
from app.workers.workspace import is_code

logger = logging.getLogger(__name__)

_WHEN_IDLE = types.FunctionResponseScheduling.WHEN_IDLE
_SILENT = types.FunctionResponseScheduling.SILENT

AsyncTool = Callable[..., Awaitable[Any]]

STILL_WORKING_AFTER_S = 8.0

PENDING = {
    "status": "pending",
    "note": f"The result comes as a {TOOL_RESULT} message. Don't guess it.",
}


def non_blocking_tool(
    func: AsyncTool, scheduling: types.FunctionResponseScheduling = _WHEN_IDLE
) -> FunctionTool:
    """Run `func` in the background and speak its result when the conversation pauses."""

    async def run(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[types.FunctionResponse]:
        with log_latency("tool", tool=func.__name__):
            result = await func(*args, **kwargs)
        yield _response(func, result, scheduling)

    async def run_cascaded(*args: Any, **kwargs: Any) -> dict[str, str]:
        spawn(_report(func, args, kwargs), name=f"tool-{func.__name__}")
        return PENDING

    return _voice_tool(func, run, run_cascaded)


def thread_tool(func: AsyncTool) -> FunctionTool:
    """Record a thread tool's confirmation silently, then yield its approach line and next milestone."""

    async def run(
        *args: Any, **kwargs: Any
    ) -> AsyncIterator[types.FunctionResponse]:
        result = await func(*args, **kwargs)
        if (several := result.pop(SINCE_SEQS_KEY, None)) is not None:
            async for response in _several(func, result, several):
                yield response
            return
        since = result.pop(SINCE_SEQ_KEY, None)
        handle = (
            await get_thread_registry().get(result["thread_id"])
            if since is not None
            else None
        )
        if handle is None:
            yield _response(func, result, _WHEN_IDLE)
            return
        # The thread ID goes into the model's context without a second spoken turn.
        yield _response(func, result, _SILENT)
        updates = get_thread_updates()
        waiting = asyncio.ensure_future(updates.wait(handle, since))
        approach = asyncio.ensure_future(updates.wait_approach(handle, since))
        try:
            # Before the milestone, at most: the thread's approach line, and one
            # still-working line if a quick thread has said nothing yet.
            first = {waiting, approach}
            done, _ = await asyncio.wait(
                first,
                timeout=STILL_WORKING_AFTER_S if _is_quick(handle) else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                yield _response(func, format_still_working(handle), _WHEN_IDLE)
                done, _ = await asyncio.wait(
                    first, return_when=asyncio.FIRST_COMPLETED
                )
            if approach in done and (line := approach.result()) is not None:
                yield _response(func, format_approach(handle, line), _WHEN_IDLE)
            event = await waiting
        finally:
            for future in (waiting, approach):
                if not future.done():
                    future.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await future
        if event is None:
            return
        await mark_delivered_seen()
        yield _response(func, format_milestone(handle, event), _WHEN_IDLE)

    async def run_cascaded(*args: Any, **kwargs: Any) -> Any:
        # Nothing waits on the thread, so `ThreadUpdates` sends its milestones; its
        # approach line is sent the same way.
        result = await func(*args, **kwargs)
        since = result.pop(SINCE_SEQ_KEY, None)
        result.pop(SINCE_SEQS_KEY, None)
        if since is not None and (
            handle := await get_thread_registry().get(result["thread_id"])
        ):
            spawn(_send_approach(handle, since), name="thread-approach")
        return result

    return _voice_tool(func, run, run_cascaded)


async def _send_approach(handle: ThreadHandle, since: int) -> None:
    updates = get_thread_updates()
    line = await updates.wait_approach(handle, since)
    if line is not None and handle.milestone_after(line.seq) is None:
        updates.send(f"{BACKGROUND_UPDATE} {format_approach(handle, line)}")


def _is_quick(handle: ThreadHandle) -> bool:
    return not is_code(handle.project)


async def _several(
    func: AsyncTool, result: dict[str, Any], since_seqs: dict[str, int]
) -> AsyncIterator[types.FunctionResponse]:
    registry = get_thread_registry()
    pairs = [
        (handle, since)
        for thread_id, since in since_seqs.items()
        if (handle := await registry.get(thread_id)) is not None
    ]
    if not pairs:
        yield _response(func, result, _WHEN_IDLE)
        return
    yield _response(func, result, _SILENT)
    found = await get_thread_updates().wait_together(pairs)
    if not found:
        return
    await mark_delivered_seen()
    yield _response(func, format_milestones(found), _WHEN_IDLE)


async def _report(
    func: AsyncTool, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    with log_latency("tool", tool=func.__name__):
        try:
            result = await func(*args, **kwargs)
        except Exception:
            logger.warning("%s failed", func.__name__, exc_info=True)
            result = "That tool hit an unexpected error."
    get_thread_updates().send(tool_result_update(func.__name__, result))


class _VoiceTool(FunctionTool):
    def __init__(
        self, func: Callable[..., Any], cascaded: FunctionTool
    ) -> None:
        super().__init__(func)
        self.response_scheduling = _SILENT
        self.cascaded = cascaded

    async def process_llm_request(
        self, *, tool_context: ToolContext, llm_request: LlmRequest
    ) -> None:
        # In cascaded mode, ADK drops later partial tool results, so results come back
        # as background updates: the session gets the twin, which returns at once.
        if is_cascaded(tool_context):
            await self.cascaded.process_llm_request(
                tool_context=tool_context, llm_request=llm_request
            )
        else:
            await super().process_llm_request(
                tool_context=tool_context, llm_request=llm_request
            )


def _voice_tool(
    func: AsyncTool,
    run: Callable[..., AsyncIterator[types.FunctionResponse]],
    run_cascaded: AsyncTool,
) -> FunctionTool:
    return _VoiceTool(
        _declared_as(func, run),
        cascaded=FunctionTool(_declared_as(func, run_cascaded)),
    )


def _declared_as(
    func: AsyncTool, impl: Callable[..., Any]
) -> Callable[..., Any]:
    wrapper = functools.wraps(func)(impl)
    # ADK builds the tool declaration from the signature and docstring of `func`.
    wrapper.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
    return wrapper


def _response(
    func: AsyncTool, result: Any, scheduling: types.FunctionResponseScheduling
) -> types.FunctionResponse:
    return types.FunctionResponse(
        name=func.__name__, response={"result": result}, scheduling=scheduling
    )
