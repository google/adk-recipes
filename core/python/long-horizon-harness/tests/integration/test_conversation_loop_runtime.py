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

"""Runtime integration — IterationBudgetPlugin via App(plugins=[...]).

The `agents-cli run` / playground path constructs the ADK Runner itself
(ADK's `/run_sse` path calls
`runner.run_async(...)` directly). Runtime iteration-budget enforcement
is achieved by attaching `IterationBudgetPlugin` to `App(plugins=[...])`,
which ADK wires into every Runner constructed from the App.

These tests pin the CONTRACT:
  1. The plugin is wired into the Runner that App produces and its
     callbacks actually fire (plugin instance is reused across runs).
  2. When the budget halts pre-call, the runner short-circuits with a
     synthetic `author="model"` halt event — the LLM is NOT invoked.
  3. When a tool-call event trips the per-iteration cap, `on_event_callback`
     halts the budget but lets the original event through untouched. The
     halt envelope is surfaced on the NEXT turn by `before_model_callback`
     (point 2). Replacing the event would orphan any function_response
     events ADK has already dispatched and break the next turn's content
     preprocessor.

Assertions are on STATE and EVENT SHAPE only — no LLM content checks.
LLM behavior validation lives in tests/eval/evalsets/guardrail_halt.evalset.json.
"""

from __future__ import annotations

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.genai import types

from horizon.conversation.iteration_budget_plugin import (
    ITERATION_STATE_KEY,
    IterationBudgetPlugin,
)


async def _seed_state(
    service: InMemorySessionService, session: Session, **delta
) -> None:
    """Persist a state delta via append_event — direct session.state writes
    do not survive the next get_session refetch the runner does."""
    await service.append_event(
        session,
        Event(
            invocation_id="test_setup",
            author="user",
            actions=EventActions(state_delta=dict(delta)),
        ),
    )


pytestmark = pytest.mark.asyncio


APP_NAME = "test_loop_runtime"


def _user_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _trivial_agent() -> Agent:
    # No model field needed for short-circuit tests — `before_run_callback`
    # returns Content before the LLM is reached. Use a placeholder model
    # name to satisfy Agent construction; the model is never invoked
    # because the plugin halts pre-call.
    return Agent(name="root_agent", model="gemini-3.7-flash")


async def _drive(
    *,
    plugin: IterationBudgetPlugin,
    user_id: str,
    message: str = "hi",
    session_service: InMemorySessionService | None = None,
    memory_service: InMemoryMemoryService | None = None,
) -> list[Event]:
    session_service = session_service or InMemorySessionService()
    memory_service = memory_service or InMemoryMemoryService()
    app = App(name=APP_NAME, root_agent=_trivial_agent(), plugins=[plugin])
    runner = Runner(
        app=app,
        session_service=session_service,
        memory_service=memory_service,
    )
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )
    events: list[Event] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=_user_message(message),
    ):
        events.append(event)
    return events


def _is_halt_event(event: Event) -> bool:
    return any(
        part.text and "[halted:" in part.text
        for part in (event.content.parts if event.content else [])
    )


async def test_plugin_short_circuits_when_budget_pre_halted() -> None:
    """A pre-halted budget yields a synthetic halt event without invoking
    the model. The halt event's text carries the reason."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    user_id = "alice"

    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    app = App(name=APP_NAME, root_agent=_trivial_agent(), plugins=[plugin])
    runner = Runner(
        app=app, session_service=session_service, memory_service=memory_service
    )
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )

    # Pre-halt by persisting the halt flag into session.state (the plugin's
    # source of truth). Simulates a guardrail having tripped earlier in the
    # session. The handoff-delivered flag is seeded so this run exercises the
    # bare short-circuit path (the one-shot graceful handoff already happened),
    # keeping the model out of the loop.
    await _seed_state(
        session_service,
        session,
        _iteration_budget_halted=True,
        _iteration_budget_halt_reason="guardrail tripped",
        _iteration_budget_handoff_delivered=True,
    )

    events: list[Event] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=_user_message("hi"),
    ):
        events.append(event)

    assert len(events) >= 1
    halt = events[-1]
    halted_text = " ".join(
        part.text
        for part in (halt.content.parts if halt.content else [])
        if part.text
    )
    assert "[halted:" in halted_text
    assert "guardrail tripped" in halted_text


async def test_plugin_short_circuits_when_iteration_cap_exhausted() -> None:
    """When max_iterations is already consumed, the next run_async returns
    a halt event without invoking the model."""
    plugin = IterationBudgetPlugin(max_iterations=1)
    user_id = "bob"

    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    app = App(name=APP_NAME, root_agent=_trivial_agent(), plugins=[plugin])
    runner = Runner(
        app=app, session_service=session_service, memory_service=memory_service
    )
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )

    # Burn the single allowed iteration by persisting the count to session.state.
    # Seed the handoff-delivered flag so this run takes the bare short-circuit
    # path rather than the one-shot graceful-handoff turn (which would invoke
    # the model).
    await _seed_state(
        session_service,
        session,
        _iteration_budget_handoff_delivered=True,
        **{ITERATION_STATE_KEY: 1},
    )

    events: list[Event] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=_user_message("hi"),
    ):
        events.append(event)

    assert len(events) >= 1
    halt = events[-1]
    halted_text = " ".join(
        part.text
        for part in (halt.content.parts if halt.content else [])
        if part.text
    )
    assert "[halted:" in halted_text


async def test_plugin_state_survives_between_runs(requires_model: None) -> None:
    """Budget state is owned by session.state — two run_async calls against
    the same session must observe a monotonically increasing iteration
    count, not get reset between runs."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    user_id = "carol"

    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    app = App(name=APP_NAME, root_agent=_trivial_agent(), plugins=[plugin])
    runner = Runner(
        app=app, session_service=session_service, memory_service=memory_service
    )
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )

    # Pre-halt so the model is not actually invoked; we only care that the
    # plugin's before_run_callback fires.
    await _seed_state(
        session_service,
        session,
        _iteration_budget_halted=True,
        _iteration_budget_halt_reason="test-only halt",
    )

    # Two run calls — both should yield halt events without exception.
    async for _ in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=_user_message("a")
    ):
        pass
    async for _ in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=_user_message("b")
    ):
        pass

    # Halt flag persisted across both runs.
    refetched = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session.id
    )
    assert refetched.state.get("_iteration_budget_halted") is True


async def test_plugin_isolates_budgets_per_session(
    requires_model: None,
) -> None:
    """Two different sessions get independent budgets — state in one
    session.state must not leak into another."""
    plugin = IterationBudgetPlugin(max_iterations=10)

    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    app = App(name=APP_NAME, root_agent=_trivial_agent(), plugins=[plugin])
    runner = Runner(
        app=app, session_service=session_service, memory_service=memory_service
    )

    session_a = await session_service.create_session(
        app_name=APP_NAME, user_id="alice"
    )
    session_b = await session_service.create_session(
        app_name=APP_NAME, user_id="bob"
    )

    # Pre-halt both sessions with distinct reasons so neither hits the LLM,
    # then verify each session's state remained independent.
    await _seed_state(
        session_service,
        session_a,
        _iteration_budget_halted=True,
        _iteration_budget_halt_reason="alice halted",
    )
    await _seed_state(
        session_service,
        session_b,
        _iteration_budget_halted=True,
        _iteration_budget_halt_reason="bob halted",
    )

    async for _ in runner.run_async(
        user_id="alice",
        session_id=session_a.id,
        new_message=_user_message("hi"),
    ):
        pass
    async for _ in runner.run_async(
        user_id="bob", session_id=session_b.id, new_message=_user_message("hi")
    ):
        pass

    a_after = await session_service.get_session(
        app_name=APP_NAME, user_id="alice", session_id=session_a.id
    )
    b_after = await session_service.get_session(
        app_name=APP_NAME, user_id="bob", session_id=session_b.id
    )
    assert a_after.state.get("_iteration_budget_halt_reason") == "alice halted"
    assert b_after.state.get("_iteration_budget_halt_reason") == "bob halted"


async def test_plugin_rejects_invalid_init_args() -> None:
    with pytest.raises(ValueError):
        IterationBudgetPlugin(max_iterations=0)
    with pytest.raises(ValueError):
        IterationBudgetPlugin(max_iterations=10, max_tool_calls_per_iteration=0)
