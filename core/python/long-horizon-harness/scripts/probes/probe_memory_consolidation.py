#!/usr/bin/env python3
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

"""Empirically probe Memory Bank general-memory consolidation.

Gates the dream-pass consolidation build. Answers, against the live API:
  (a) does a single ``memories.generate`` over recent events (scope
      {app_name,user_id}, consolidation left on) reconcile PRE-EXISTING
      contradictory facts -- i.e. emit UPDATED/DELETED actions, not just CREATE?
  (b) does the user's memory count stop growing (or shrink) after the pass,
      rather than accumulating a third contradictory fact?

If (a)/(b) hold, the existing dream-review generate call already consolidates
general memories — confirming that count-surfacing + the ``LHA_MEMORY_CONSOLIDATION``
kill switch (already shipped in ``app/memory/dream_review.py``) is the whole
feature. If they do NOT, the consolidation story needs rework (e.g. an
``allowed_topics``/scope-differentiated generate) — investigate before relying on it.

Costs real money (hits Memory Bank). Uses a throwaway, timestamped user id to
stay isolated; no per-user purge is wired here.

Env contract:
    GOOGLE_CLOUD_PROJECT        required
    AGENT_ENGINE_RESOURCE_NAME  required (projects/.../reasoningEngines/ID)
    GOOGLE_GENAI_USE_VERTEXAI=True, GOOGLE_CLOUD_LOCATION set as for dev.

All diagnostics go to stderr; a JSON result object is written to stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

from google.adk.events.event import Event
from google.adk.memory import VertexAiMemoryBankService
from google.genai.types import Content, Part

from app.memory.add_memory_tool import add_memory_entry
from app.memory.dream_review import _run_dream_review_for_user
from app.memory.memory_list import list_memory_writes


def _log(msg: str) -> None:
    print(f"probe_memory_consolidation: {msg}", file=sys.stderr, flush=True)


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        _log(f"missing required env var {var}")
        sys.exit(2)
    return val


class _OneSession:
    """Session service returning a single session of the given events."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events

    async def list_sessions(self, *, app_name, user_id=None):
        del app_name
        return SimpleNamespace(
            sessions=[
                SimpleNamespace(
                    id="s1", user_id=user_id or "u", last_update_time=1.0
                )
            ]
        )

    async def get_session(self, *, app_name, user_id, session_id):
        del app_name, user_id, session_id
        return SimpleNamespace(id="s1", events=self._events)


async def _count(memory_service, *, app_name: str, user_id: str) -> int:
    res = await list_memory_writes(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        limit=500,
        offset=0,
    )
    return len(res.entries)


async def _run() -> dict[str, object]:
    project = _require("GOOGLE_CLOUD_PROJECT")
    resource = _require("AGENT_ENGINE_RESOURCE_NAME")
    del project  # surfaced via env to the client implicitly
    engine_id = resource.rsplit("/", 1)[-1]
    memory_service = VertexAiMemoryBankService(agent_engine_id=engine_id)

    app_name = "app"
    user_id = f"lha-probe-consolidate-{int(time.time())}"

    _log(f"seeding contradictory facts for {user_id} ...")
    for fact in (
        "prefers tabs for indentation",
        "prefers spaces for indentation",
    ):
        await add_memory_entry(
            content=fact,
            scope="user",
            memory_service=memory_service,
            app_name=app_name,
            user_id=user_id,
            session_id="seed",
        )

    before = await _count(memory_service, app_name=app_name, user_id=user_id)
    _log(f"memory count after seeding: {before}")

    transcript = (
        "Just to be clear about my setup: I always use spaces for indentation, "
        "never tabs. Please remember that."
    )
    events = [
        Event(
            author="user",
            invocation_id="probe",
            content=Content(role="user", parts=[Part(text=transcript)]),
        )
    ]
    _log("running dream pass over the corrective transcript ...")
    result = await _run_dream_review_for_user(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        session_service=_OneSession(events),
    )

    after = await _count(memory_service, app_name=app_name, user_id=user_id)
    _log(f"memory count after pass: {after}")

    return {
        "dream_result": result,
        "count_before": before,
        "count_after": after,
        "grew": after > before,
    }


def main() -> int:
    result = asyncio.run(_run())
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    consolidated = bool(
        result.get("dream_result", {}).get("success")
    ) and not result.get("grew")
    _log(
        "LIKELY ALREADY CONSOLIDATED (shipped feature is correct)"
        if consolidated
        else "NOT consolidated by existing call (consolidation story needs rework)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
