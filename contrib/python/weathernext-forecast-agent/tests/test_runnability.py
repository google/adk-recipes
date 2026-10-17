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
"""Runnability tests for the ADK agent recipe.

Verifies that the Python code compiles, resolves all dependencies, and that
the ADK entry points are correctly wired — matching what `adk run app` and
`uvicorn app.fast_api_app:app` require at startup.

These tests are intentionally agnostic to business logic (tools, instructions,
etc.). Tool-level correctness belongs in test_tools.py.
"""

from fastapi import FastAPI
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini

import app
import app.agent


def test_adk_run_runnability() -> None:
    """Verifies the entry point required by `adk run app` is correctly wired.

    `adk run app` imports the `app` package and expects `root_agent` to be
    an Agent instance exposed at the package level via app/__init__.py.
    """
    # app/__init__.py must re-export root_agent or app for `adk run app` to work
    assert hasattr(app, "root_agent") or hasattr(app, "app"), (
        "app/__init__.py must export either `root_agent` or `app` "
        "for `adk run app` to work."
    )

    # root_agent must be a valid Agent instance
    assert app.agent.root_agent is not None
    assert isinstance(app.agent.root_agent, Agent)
    assert isinstance(app.agent.root_agent.model, Gemini)

    # Agent must have at least one tool registered
    assert len(app.agent.root_agent.tools) > 0, (
        "root_agent has no tools registered."
    )

    # App wrapper must be present and valid
    assert app.agent.app is not None
    assert isinstance(app.agent.app, App)


def test_web_server_runnability() -> None:
    """Verifies entry point for `uvicorn app.fast_api_app:app`."""
    import app.fast_api_app

    assert app.fast_api_app.app is not None
    assert isinstance(app.fast_api_app.app, FastAPI)
