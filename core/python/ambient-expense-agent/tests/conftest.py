# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared test fixtures and environment bootstrap.

This conftest deliberately does two things at module top so tests are
self-contained and never depend on a ``.env`` file existing in CI:

1. Populate the env vars that ``app.config`` reads at import
   time (``GOOGLE_API_KEY``, ``MODEL_NAME``). Setting ``GOOGLE_API_KEY``
   also steers ``config.py`` down the AI Studio branch so
   ``google.auth.default()`` is never called — no ADC required.
2. Register an autouse fixture that patches ``Gemini.generate_content_async``
   with a canned response, so integration tests that route through the
   LLM ``review_agent`` complete without touching the network or needing
   a valid API key.
"""

# --- Env bootstrap (runs before test files import app) -----------
# pytest loads conftest.py before collecting sibling test modules, so this
# module-level code runs *before* test_integration.py's top-level
# ``from app.fast_api_app import app`` triggers config.py.
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")
os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

# ADK 2.0 defaults to SQLite local storage (.adk/ directory), which persists
# sessions across test runs and causes test pollution. Force in-memory
# services so every pytest invocation starts with a clean session store.
os.environ.setdefault("ADK_DISABLE_LOCAL_STORAGE", "1")

# The .env.example ships placeholder values for APP_NAME and
# PUBSUB_SUBSCRIPTION. The CI workflow copies .env.example → .env and then
# config.py's load_dotenv() loads those placeholders into the environment
# BEFORE frontend/main.py captures them as module-level constants. Pre-seed
# the correct test values here (using setdefault so a real .env with
# meaningful values still wins) so the frontend queries the right ADK app
# and user_id when running tests.
os.environ.setdefault("APP_NAME", "app")
os.environ.setdefault("PUBSUB_SUBSCRIPTION", "test-sub")

# --- LLM mock (imports are safe now that env is set) -----------------------
from collections.abc import AsyncGenerator

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


async def _fake_generate_content_async(
    self: BaseLlm,
    llm_request: LlmRequest,
    stream: bool = False,
) -> AsyncGenerator[LlmResponse, None]:
    """Yield one canned model turn — no network, no API key needed.

    The review_agent uses ``mode="single_turn"``, so a single non-partial
    text response is enough to complete its turn and let the workflow
    advance to the HITL ``RequestInput`` pause. The response contains no
    ``function_call`` parts, so the agent's tool loop stays quiet.
    """
    yield LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text="Review complete.")],
        ),
        partial=False,
        turn_complete=True,
    )


@pytest.fixture(autouse=True)
def mock_gemini_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every code path ADK might use to reach the Gemini API.

    ``Agent(model="gemini-...")`` resolves lazily via ``LLMRegistry.new_llm``,
    which builds a fresh ``Gemini`` instance per ``canonical_model`` access.
    Patching on the class (not an instance) intercepts every future
    instance. ``BaseLlm`` is patched belt-and-suspenders in case any
    codepath dispatches through the abstract base.
    """
    monkeypatch.setattr(
        Gemini, "generate_content_async", _fake_generate_content_async
    )
    monkeypatch.setattr(
        BaseLlm, "generate_content_async", _fake_generate_content_async
    )
