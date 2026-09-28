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

"""Run a routine ONCE synchronously, on demand — for testing before scheduling.

Mirrors ``subagents/delegate_runner.run_delegate`` but over the live ``App`` (so
the run is faithful: same agent, callbacks, plugins) under ``routine_isolation``
(isolated sandbox + headless + scoped secrets). Ephemeral in-memory services —
nothing is persisted; the returned ``output`` is the test result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

from google.genai.types import Content, Part

from app.routines.isolation import HEADLESS_PREAMBLE, routine_isolation

logger = logging.getLogger(__name__)

# The cron fire path imposes no timeout, so this blocking dry-run only bounds how
# long the user waits — keep it generous enough for real work (research + drafts).
_DEFAULT_TIMEOUT_S = 300.0


def _build_runner() -> Any:
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.memory import InMemoryMemoryService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from app.agent import app

    return Runner(
        app=app,
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        artifact_service=InMemoryArtifactService(),
    )


def _event_text(event: Any) -> str:
    content = getattr(event, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(p.text for p in parts if getattr(p, "text", None))


async def run_routine_once(
    *,
    routine_id: str,
    owner: str,
    task: str,
    secrets: Iterable[str] = (),
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    runner: Any = None,
) -> dict[str, Any]:
    """Run ``task`` once under the routine's full isolation and return an envelope:
    ``{success, status: completed|timeout|error, output, iterations, duration_ms}``.
    """
    if runner is None:
        runner = _build_runner()
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=owner
    )
    message = Content(
        role="user",
        parts=[
            Part(text=HEADLESS_PREAMBLE.format(routine_id=routine_id) + task)
        ],
    )

    chunks: list[str] = []
    iterations = 0
    status = "completed"
    error: str | None = None
    start = time.monotonic()

    async def _drain() -> None:
        nonlocal iterations
        async for event in runner.run_async(
            user_id=owner, session_id=session.id, new_message=message
        ):
            iterations += 1
            text = _event_text(event)
            if text:
                chunks.append(text)

    with routine_isolation(routine_id, owner=owner, secrets=secrets):
        try:
            await asyncio.wait_for(_drain(), timeout=timeout_s)
        except TimeoutError:
            status = "timeout"
        except Exception as exc:
            logger.exception("routine test run failed for %s", routine_id)
            status = "error"
            error = f"{type(exc).__name__}: {exc}"

    return {
        "success": status == "completed",
        "status": status,
        "output": error if error is not None else "".join(chunks),
        "iterations": iterations,
        "duration_ms": int((time.monotonic() - start) * 1000),
    }


__all__ = ["run_routine_once"]
