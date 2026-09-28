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

import asyncio
from types import SimpleNamespace

import pytest
from google.adk.sessions import InMemorySessionService

from app.routines.run_context import active_routine_run
from app.routines.run_once import run_routine_once

pytestmark = pytest.mark.asyncio


def _event(text):
    return SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text=text)])
    )


class _FakeRunner:
    app_name = "app"

    def __init__(self, events, *, record=None):
        self.session_service = InMemorySessionService()
        self._events = events
        self._record = record

    async def run_async(self, *, user_id, session_id, new_message):
        if self._record is not None:
            self._record["run"] = active_routine_run()
            self._record["message"] = new_message.parts[0].text
        for ev in self._events:
            yield ev


async def test_completed_envelope_and_isolation():
    record = {}
    runner = _FakeRunner([_event("hello "), _event("world")], record=record)
    res = await run_routine_once(
        routine_id="digest",
        owner="alice@x",
        task="do the thing",
        secrets=("STRIPE_KEY",),
        runner=runner,
    )
    assert res["success"] is True
    assert res["status"] == "completed"
    assert res["output"] == "hello world"
    assert res["iterations"] == 2
    # ContextVars set during the run (headless preamble + task in the message),
    # reset after.
    assert record["run"].routine_id == "digest"
    assert "digest" in record["message"] and "do the thing" in record["message"]
    assert active_routine_run() is None


async def test_error_envelope_on_raise():
    class _BoomRunner(_FakeRunner):
        async def run_async(self, *, user_id, session_id, new_message):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

    res = await run_routine_once(
        routine_id="d", owner="a", task="t", secrets=(), runner=_BoomRunner([])
    )
    assert res["success"] is False and res["status"] == "error"
    assert "kaboom" in res["output"]
    assert active_routine_run() is None


async def test_timeout_envelope_keeps_partial_output():
    class _SlowRunner(_FakeRunner):
        async def run_async(self, *, user_id, session_id, new_message):
            yield _event("partial ")
            await asyncio.sleep(5)
            yield _event("never")  # pragma: no cover

    res = await run_routine_once(
        routine_id="d",
        owner="a",
        task="t",
        secrets=(),
        runner=_SlowRunner([]),
        timeout_s=0.05,
    )
    assert res["success"] is False and res["status"] == "timeout"
    assert "partial" in res["output"]
    assert active_routine_run() is None
