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
"""The recipe against a live model.

``tests/unit/test_event_delivery.py`` already drives this path with a scripted
model, so what is left for a live run is the half a stub cannot show: that the
instruction gets the tool called at all, and that the widget arrives *beside* a
reply rather than instead of one.

Needs live credentials, which is why ``pyproject.toml`` keeps this directory
out of the default pytest run -- ``uv run pytest tests/integration``.

Widget ids are asserted by membership rather than equality: a live model may
reach for a second tool in the same turn, and the exact emission set is already
pinned by the unit suite.
"""

from __future__ import annotations

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events.event import Event
from google.adk.events.ui_widget import UiWidget
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import create_agent
from app.catalog import validator
from app.staging import spec_for

APP_NAME = "staged-ui-widgets-integration"
USER_ID = "test-shopper"

PICKS_ID = spec_for("picks").widget_id
COMPARISON_ID = spec_for("comparison").widget_id


def start_session() -> tuple[Runner, str]:
    """A fresh agent over an empty in-memory session."""
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id=USER_ID, app_name=APP_NAME
    )
    runner = Runner(
        agent=create_agent(),
        session_service=session_service,
        app_name=APP_NAME,
    )
    return runner, session.id


def run_turn(runner: Runner, session_id: str, message: str) -> list[Event]:
    """One turn, streamed the way the FastAPI server streams it."""
    return list(
        runner.run(
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=message)]
            ),
            user_id=USER_ID,
            session_id=session_id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )


def delivered_widgets(events: list[Event]) -> list[UiWidget]:
    """Widgets that reached the client, across every event in the turn."""
    return [
        widget
        for event in events
        for widget in (event.actions.render_ui_widgets or [])
    ]


def reply_text(events: list[Event]) -> str:
    """What the model said this turn, streaming chunks excluded."""
    return " ".join(
        part.text
        for event in events
        if not event.partial and event.content
        for part in (event.content.parts or [])
        if part.text
    ).strip()


def test_a_picks_request_delivers_valid_a2ui_beside_the_reply() -> None:
    """Model calls a tool, the tool stages, the flush ships it.

    The pick carousel is the recipe's primary surface, so this is the turn
    that has to work: a widget on ADK's ``UiWidget`` channel whose payload is
    spec-valid A2UI, with words next to it.
    """
    runner, session_id = start_session()

    events = run_turn(runner, session_id, "I need new trail shoes")
    assert events, "Expected at least one event"

    widgets = delivered_widgets(events)
    ids = [w.id for w in widgets]
    assert PICKS_ID in ids, f"Expected a pick carousel; got {ids}"

    picks = next(w for w in widgets if w.id == PICKS_ID)
    assert picks.provider == "a2ui"
    # Spec-valid A2UI, not just JSON that survived the trip.
    validator().validate(picks.payload["a2ui"])

    # A widget with no words beside it reads as a bug, which is what
    # ``ensure_widget_companion`` in app/agent.py puts a floor under.
    assert reply_text(events), "Expected a reply alongside the widget"


def test_a_follow_up_compares_the_cards_already_on_screen() -> None:
    """The register outlives the turn, so "which is better" needs no ids.

    Turn two reaches ``compare_picks`` with no product ids: they come from the
    payload turn one left in session state. That cross-turn hand-off is the
    part inline rendering cannot express, so it is worth a live run.
    """
    runner, session_id = start_session()

    first = run_turn(runner, session_id, "I need new trail shoes")
    assert PICKS_ID in [w.id for w in delivered_widgets(first)], (
        "the comparison turn is only meaningful once picks are on screen"
    )

    second = run_turn(runner, session_id, "which of those is best value?")
    tables = [w for w in delivered_widgets(second) if w.id == COMPARISON_ID]
    assert tables, "Expected a comparison table built from the staged picks"
    validator().validate(tables[0].payload["a2ui"])
    assert reply_text(second), "Expected a reply alongside the table"
