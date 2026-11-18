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

1. Disable ``load_dotenv()`` with ``PYTHON_DOTENV_DISABLED``, then read only
   the non-secret model/backend settings from ``.env.example`` using
   ``dotenv_values()``. Set a synthetic project for the tests. A developer's
   local ``.env`` is never loaded, whether or not it exists.
2. Register an autouse fixture that patches ``Gemini.generate_content_async``
   with a canned response, so ``test_agents.py::test_happy_path`` — which
   otherwise makes a **live Gemini call** — completes without touching
   the network or needing valid credentials.
"""

# --- Env bootstrap (runs before test files import safety_plugins) ----------
# pytest loads conftest.py before collecting sibling test modules, so this
# module-level code runs *before* test_agents.py's top-level
# ``from safety_plugins.agent import root_agent`` triggers
# ``safety_plugins/__init__.py`` (which calls ``load_dotenv()``).
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from dotenv import dotenv_values
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
_example = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
for _key in (
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "MODEL_NAME_GENERATED_1",
    "MODEL_NAME_GENERATED_2",
):
    _value = _example[_key]
    if _value is None:
        raise ValueError(f"Set {_key} in .env.example")
    os.environ[_key] = _value


async def _fake_generate_content_async(
    self: BaseLlm,
    llm_request: LlmRequest,
    stream: bool = False,
) -> AsyncGenerator[LlmResponse, None]:
    """Yield one canned model turn — no network, no credentials needed.

    ``test_happy_path`` only asserts that the runner produces a non-empty
    text response, so a single non-partial text turn with no
    ``function_call`` parts is enough: the agent's tool loop stays quiet
    and the run ends after this one turn.
    """
    yield LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text="Sure — running the short sum tool.")],
        ),
        partial=False,
        turn_complete=True,
    )


@pytest.fixture(autouse=True)
def mock_gemini_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every code path ADK might use to reach the Gemini API.

    ``LlmAgent(model="gemini-...")`` resolves lazily via
    ``LLMRegistry.new_llm``, which builds a fresh ``Gemini`` instance per
    ``canonical_model`` access. Patching the method on the class (not an
    instance) intercepts every future instance. ``BaseLlm`` is patched
    belt-and-suspenders in case any codepath dispatches through the
    abstract base.
    """
    monkeypatch.setattr(
        Gemini, "generate_content_async", _fake_generate_content_async
    )
    monkeypatch.setattr(
        BaseLlm, "generate_content_async", _fake_generate_content_async
    )
