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
"""Environment bootstrap and the shared context stub.

``app/__init__.py`` builds the agent at import time, which needs
``MODEL_NAME``. pytest loads this conftest before it collects sibling test
modules, so seeding the environment here happens before any ``import app``.

The values below mirror ``.env.example``. ``setdefault`` throughout, so a
developer with a real ``.env`` already loaded keeps their own settings --
nothing in the unit suite reaches the network, so the model name only has to
be well-formed.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")
os.environ.setdefault("GEMINI_API_KEY", "test-key-never-used")

# ADK defaults to SQLite-backed local storage under .adk/, which would carry
# sessions between test runs. Force in-memory services so each run starts
# from an empty store.
os.environ.setdefault("ADK_DISABLE_LOCAL_STORAGE", "1")

import pytest
from google.adk.sessions.state import State


class StubContext:
    """Stand-in for the two members of ADK's ``Context`` this recipe uses.

    Real ``State``, so ``has_delta()`` behaves exactly as it does under a
    runner -- that is the assertion the lifecycle tests turn on. The widget
    sink reproduces the duplicate-id guard from ``agents/context.py:1010``.

    Every tool and the whole staging layer take a context this narrow, which
    is why the unit suite needs no runner, session service, or model.
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = State(value=dict(state or {}), delta={})
        self.widgets: list[Any] = []

    def render_ui_widget(self, ui_widget: Any) -> None:
        if any(w.id == ui_widget.id for w in self.widgets):
            raise ValueError(f"widget id {ui_widget.id} already rendered")
        self.widgets.append(ui_widget)

    def next_turn(self) -> StubContext:
        """A fresh context carrying only what ADK would carry forward.

        ``temp:`` keys are per-invocation: ``_apply_temp_state`` writes them
        onto the in-memory session only, and ``_trim_temp_delta_state`` strips
        them from the event before it is stored, so they reach neither the next
        turn nor storage (``sessions/base_session_service.py:169,182-210``).
        Dropping them here is what makes the cross-turn revival test honest.
        """
        return StubContext(
            {
                key: value
                for key, value in self.state.to_dict().items()
                if not key.startswith("temp:")
            }
        )

    @property
    def widget_ids(self) -> list[str]:
        return [w.id for w in self.widgets]


@pytest.fixture
def ctx() -> StubContext:
    """An empty context: no profile, nothing staged."""
    return StubContext()
