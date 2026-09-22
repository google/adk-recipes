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
"""Do the widgets actually reach the client? Asked of a real ADK runner.

Everything else in the suite tests our code. This file tests the seam between
our code and ADK, using a scripted model so no network is involved. Two
behaviours are only observable here:

1. A widget rendered from an after-agent callback that writes no state is
   silently dropped. This is the trap the whole staging layer is shaped
   around, and ``test_render_without_a_state_write_is_silently_dropped``
   pins it down so a future ADK version changing it shows up as a failing
   test rather than as duplicated widgets in production.

2. ADK's duplicate-widget-id guard does not survive parallel tool calls, so
   deduplication has to happen somewhere that sees all the widgets at once.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.events.ui_widget import UiWidget
from google.adk.flows.llm_flows.functions import (
    merge_parallel_function_response_events,
)
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import flush_widgets, seed_profile
from app.catalog import validator
from app.tools import get_personalized_picks

APP_NAME = "staged-ui-widgets-test"
USER_ID = "test-shopper"


class ScriptedLlm(BaseLlm):
    """A model that replays a fixed list of responses.

    Enough to drive the real ``Runner`` -- including a tool call and the
    follow-up reply -- without a network round trip or an API key.
    """

    model: str = "scripted-test-model"
    # A pydantic field, not a shared mutable default: ``BaseLlm`` is a
    # ``BaseModel``, so each instance gets its own list.
    script: list[types.Content] = []  # noqa: RUF012
    turns: int = 0

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        index = min(self.turns, len(self.script) - 1)
        self.turns += 1
        yield LlmResponse(content=self.script[index])


def reply(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def tool_call(name: str, **args: Any) -> types.Content:
    return types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(name=name, args=dict(args))
            )
        ],
    )


def run_turn(agent: Agent, message: str) -> list[Event]:
    """One full turn through a real runner, in-memory throughout."""
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        app_name=APP_NAME, user_id=USER_ID
    )
    runner = Runner(
        agent=agent, session_service=session_service, app_name=APP_NAME
    )
    return list(
        runner.run(
            user_id=USER_ID,
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=message)]
            ),
        )
    )


def delivered_widgets(events: list[Event]) -> list[UiWidget]:
    """Widgets that reached the client, across every event in the turn."""
    return [
        widget
        for event in events
        for widget in (event.actions.render_ui_widgets or [])
    ]


# --- the whole path, end to end ---------------------------------------------


def test_a_staged_widget_reaches_the_client_as_valid_a2ui() -> None:
    """Model calls a tool, tool stages, callback flushes, event carries it.

    The one test that exercises every layer at once: ranking, converter,
    A2UI assembly, the staging gates, and ADK's event plumbing.
    """
    agent = Agent(
        name="root_agent",
        model=ScriptedLlm(
            script=[
                tool_call("get_personalized_picks", query="trail shoes"),
                reply("Three that fit your usual setup."),
            ]
        ),
        instruction="Test agent.",
        tools=[get_personalized_picks],
        before_agent_callback=seed_profile,
        after_agent_callback=flush_widgets,
    )

    widgets = delivered_widgets(run_turn(agent, "show me trail shoes"))

    assert [w.id for w in widgets] == ["ui-picks"]
    assert widgets[0].provider == "a2ui"
    # The payload a host receives is spec-valid A2UI, not just well-shaped
    # JSON that happened to survive the trip.
    validator().validate(widgets[0].payload["a2ui"])


def test_a_turn_with_no_tool_call_delivers_no_widgets() -> None:
    """A conversational turn stays conversational."""
    agent = Agent(
        name="root_agent",
        model=ScriptedLlm(script=[reply("Happy to help.")]),
        instruction="Test agent.",
        tools=[get_personalized_picks],
        before_agent_callback=seed_profile,
        after_agent_callback=flush_widgets,
    )
    assert delivered_widgets(run_turn(agent, "hello")) == []


# --- the trap ---------------------------------------------------------------


def render_only(callback_context: CallbackContext) -> None:
    """The intuitive callback, and the broken one: renders, writes nothing."""
    callback_context.render_ui_widget(
        UiWidget(id="ui-trap", provider="a2ui", payload={"a2ui": []})
    )


def render_and_write(callback_context: CallbackContext) -> None:
    """The same callback plus a state write -- which is what ships it."""
    callback_context.render_ui_widget(
        UiWidget(id="ui-trap", provider="a2ui", payload={"a2ui": []})
    )
    callback_context.state["ui:emitted:trap"] = True


@pytest.mark.parametrize(
    ("callback", "expected"),
    [(render_only, []), (render_and_write, ["ui-trap"])],
    ids=["render_only_is_dropped", "render_with_state_write_is_delivered"],
)
def test_render_without_a_state_write_is_silently_dropped(
    callback: Any, expected: list[str]
) -> None:
    """Two agents differing by one state write; only one delivers a widget.

    ``base_agent`` creates an event for an after-agent callback only if the
    callback returned content or ``state.has_delta()``
    (``agents/base_agent.py:564-582``). ``render_ui_widget`` mutates event
    *actions*, never state -- so the render-only callback produces no event,
    the widget vanishes, and nothing raises anywhere.

    If a future ADK release emits the event regardless, this test fails on the
    first case and the staging layer can drop ``mark_emitted``'s second job.
    """
    agent = Agent(
        name="root_agent",
        model=ScriptedLlm(script=[reply("Done.")]),
        instruction="Test agent.",
        after_agent_callback=callback,
    )
    widgets = delivered_widgets(run_turn(agent, "hello"))
    assert [w.id for w in widgets] == expected


# --- the parallel-tool-call hole -------------------------------------------


def function_response_event(author: str, widget: UiWidget) -> Event:
    actions = EventActions()
    actions.render_ui_widgets = [widget]
    return Event(
        invocation_id="inv-1",
        author=author,
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=f"fc-{author}", name=author, response={"ok": True}
                    )
                )
            ],
        ),
        actions=actions,
    )


def test_duplicate_widget_ids_survive_a_parallel_tool_call_merge() -> None:
    """Why deduplication cannot live in the tools.

    ``render_ui_widget`` rejects a duplicate id, but each function call gets
    its own ``ToolContext`` (``flows/llm_flows/functions.py:1228``), so the
    check only ever sees one call's widgets.
    ``merge_parallel_function_response_events`` (``functions.py:1526``) then
    concatenates the two lists into one without re-checking ids
    (``:1545-1562``). Two tools rendering the same id in parallel ship two
    identical widgets to the client.

    Flushing once, from one context, is where that check finally bites --
    which ``test_second_flush_in_the_same_turn_is_a_no_op`` covers.
    """

    def widget() -> UiWidget:
        return UiWidget(id="ui-picks", provider="a2ui", payload={"n": 1})

    merged = merge_parallel_function_response_events(
        [
            function_response_event("tool_a", widget()),
            function_response_event("tool_b", widget()),
        ]
    )

    ids = [w.id for w in merged.actions.render_ui_widgets]
    assert ids == ["ui-picks", "ui-picks"], (
        "if ADK gained cross-call dedupe, this test is the place to notice"
    )
