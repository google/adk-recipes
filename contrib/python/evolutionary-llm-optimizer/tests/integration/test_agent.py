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

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import create_workflow


def test_workflow_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs the whole workflow against Gemini with a small budget.

    Checks that the loop ran, every evaluation was recorded, and the final
    report came from the verifier.
    """
    monkeypatch.setenv("MAX_EVALUATIONS", "2")
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id="test_user", app_name="test"
    )
    runner = Runner(
        node=create_workflow(),
        session_service=session_service,
        app_name="test",
    )
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text="Cut the bytes scanned by the station demand query."
            )
        ],
    )

    events = list(
        runner.run(
            new_message=message, user_id="test_user", session_id=session.id
        )
    )

    texts = [
        part.text
        for event in events
        if event.content
        for part in event.content.parts
        if part.text
    ]
    assert any(t.startswith("## SQL cost report: ") for t in texts)
    state = session_service.get_session_sync(
        app_name="test", user_id="test_user", session_id=session.id
    ).state
    assert state["evolve_evaluations"] == 2
    assert len(state["evolve_history"]) == 3
