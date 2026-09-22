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

"""State-isolating runner for delegated child agents.

The dispatch layer can't use ``AgentTool`` because ADK's ``AgentTool``
forwards child ``session.state`` mutations back to the parent
(``adk.dev/agents/multi-agents/index.md`` §1.3c). The whole point of
delegation is isolation, so we wrap the child in a fresh
``InMemorySessionService`` + ``InMemoryMemoryService`` per call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.agents import Agent
from google.adk.events.event import Event
from google.genai.types import Content, Part

pytestmark = pytest.mark.asyncio


def _text_event(text: str, *, author: str = "delegate_x") -> Event:
    return Event(
        author=author,
        content=Content(role="model", parts=[Part(text=text)]),
        invocation_id="inv-1",
    )


def _make_fake_runner(events: list[Event]) -> Any:
    """Build a stand-in for ADK's Runner that yields the supplied events."""

    async def fake_run_async(**_kwargs: Any) -> AsyncIterator[Event]:
        for event in events:
            yield event

    runner = MagicMock()
    runner.run_async = fake_run_async
    return runner


@pytest.fixture()
def stub_child() -> Agent:
    """Cheap child stand-in — never actually executed; the fake Runner
    yields canned events. We only need a name on it."""
    agent = MagicMock(spec=Agent)
    agent.name = "delegate_stub"
    return agent


async def test_run_delegate_returns_completed_envelope(
    monkeypatch, stub_child: Agent
) -> None:
    from horizon.subagents import delegate_runner

    fake = _make_fake_runner(
        [_text_event("Done. Found 3 files.", author="delegate_stub")]
    )
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: fake)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="enumerate"
    )

    assert result["status"] == "completed"
    assert "Done. Found 3 files." in result["summary"]
    assert "telemetry" in result
    assert result["telemetry"]["iterations"] >= 1
    assert result["telemetry"]["duration_ms"] >= 0


async def test_run_delegate_concatenates_text_parts(
    monkeypatch, stub_child: Agent
) -> None:
    from horizon.subagents import delegate_runner

    fake = _make_fake_runner(
        [
            _text_event("Step 1 done. "),
            _text_event("Step 2 done. "),
            _text_event("Final: 5 results."),
        ]
    )
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: fake)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x"
    )

    assert "Final: 5 results." in result["summary"]
    assert result["telemetry"]["iterations"] == 3


async def test_run_delegate_timeout_returns_timeout_envelope(
    monkeypatch, stub_child: Agent
) -> None:
    from horizon.subagents import delegate_runner

    async def slow_run_async(**_kwargs: Any) -> AsyncIterator[Event]:
        await asyncio.sleep(5)  # longer than our timeout
        yield _text_event("never reached")

    runner = MagicMock()
    runner.run_async = slow_run_async
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: runner)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x", timeout_s=0.2
    )

    assert result["status"] == "timeout"
    assert result["telemetry"]["duration_ms"] >= 200


async def test_timeout_summary_says_the_work_is_incomplete(
    monkeypatch, stub_child: Agent
) -> None:
    """A timed-out child's partial narration reads like a finished report, so
    the incompleteness has to be IN the summary, not only in a sibling
    status field. A parent once relayed 'the test suite has been added' from
    a run that never finished."""
    from horizon.subagents import delegate_runner

    async def chatty_then_hang(**_kwargs: Any) -> AsyncIterator[Event]:
        yield _text_event("Added tests/unit/test_regression.py. All passing.")
        await asyncio.sleep(5)

    runner = MagicMock()
    runner.run_async = chatty_then_hang
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: runner)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x", timeout_s=0.2
    )

    assert result["status"] == "timeout"
    summary = result["summary"]
    assert "INCOMPLETE" in summary, summary
    # the partial work is still relayed, just not as a completed result
    assert "test_regression.py" in summary, summary
    # machine-readable too: the repo signals failure with an `error` key
    # (49 sites), and spawn.py's _result_summary already reads one.
    assert result["error"], result
    assert "timed out" in result["error"].lower(), result["error"]


async def test_completed_run_carries_no_error_key(
    monkeypatch, stub_child: Agent
) -> None:
    from horizon.subagents import delegate_runner

    async def quick(**_kwargs: Any) -> AsyncIterator[Event]:
        yield _text_event("done")

    runner = MagicMock()
    runner.run_async = quick
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: runner)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x", timeout_s=5
    )

    assert result["status"] == "completed"
    assert "error" not in result, result


async def test_run_delegate_isolates_from_parent_session(
    monkeypatch, stub_child: Agent
) -> None:
    """Verify the runner builds its own InMemorySessionService and never
    receives a parent service reference."""
    from google.adk.sessions import InMemorySessionService

    from horizon.subagents import delegate_runner

    captured: dict[str, Any] = {}

    def fake_build_runner(
        *, agent: Agent, session_service: Any, memory_service: Any
    ) -> Any:
        captured["session_service"] = session_service
        captured["memory_service"] = memory_service
        captured["agent"] = agent
        return _make_fake_runner([_text_event("ok")])

    monkeypatch.setattr(delegate_runner, "_build_runner", fake_build_runner)

    await delegate_runner.run_delegate(child=stub_child, user_message="x")

    assert isinstance(captured["session_service"], InMemorySessionService)
    # Fresh service, never the caller's.
    assert captured["agent"] is stub_child


async def test_run_delegate_empty_response_still_completes(
    monkeypatch, stub_child: Agent
) -> None:
    """Child that exits without emitting text still reached natural
    termination, so ``status='completed'``. The summary must SAY it reported
    nothing: a success flag with an empty body is the same vacuum that had a
    parent inventing a finished test suite after a timeout."""
    from horizon.subagents import delegate_runner

    fake = _make_fake_runner([])
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: fake)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x"
    )

    assert result["status"] == "completed"
    assert "nothing" in result["summary"].lower(), result["summary"]
    assert "error" not in result, result
    assert result["telemetry"]["iterations"] == 0


async def test_run_delegate_max_iterations_halts_at_cap(
    monkeypatch, stub_child: Agent
) -> None:
    """When the child emits more events than ``max_iterations`` allows,
    drain stops and the envelope is marked ``halted`` with a clear
    summary. This is the structured-axis counterpart to ``timeout_s``."""
    from horizon.subagents import delegate_runner

    events = [_text_event(f"step {i} ") for i in range(10)]
    fake = _make_fake_runner(events)
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: fake)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x", max_iterations=3
    )
    assert result["status"] == "halted"
    assert "iterations" in result["summary"].lower()
    assert result["telemetry"]["iterations"] == 3


async def test_run_delegate_max_iterations_unbounded_when_none(
    monkeypatch, stub_child: Agent
) -> None:
    """``max_iterations=None`` (the default) means no cap — all events
    drain through."""
    from horizon.subagents import delegate_runner

    events = [_text_event(f"step {i} ") for i in range(7)]
    fake = _make_fake_runner(events)
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: fake)

    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x"
    )
    assert result["status"] == "completed"
    assert result["telemetry"]["iterations"] == 7


async def test_run_delegate_records_duration(
    monkeypatch, stub_child: Agent
) -> None:
    from horizon.subagents import delegate_runner

    async def slightly_slow(**_kwargs: Any) -> AsyncIterator[Event]:
        await asyncio.sleep(0.05)
        yield _text_event("done")

    runner = MagicMock()
    runner.run_async = slightly_slow
    monkeypatch.setattr(delegate_runner, "_build_runner", lambda **_: runner)

    start = time.monotonic()
    result = await delegate_runner.run_delegate(
        child=stub_child, user_message="x"
    )
    elapsed = (time.monotonic() - start) * 1000

    assert result["status"] == "completed"
    # Sanity-bound the recorded duration to a real elapsed time.
    assert 40 <= result["telemetry"]["duration_ms"] <= elapsed + 50
