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
from google.adk import Agent, Workflow
from google.adk.apps import App
from google.adk.models import Gemini

import app
import app.agent


def test_adk_run_runnability() -> None:
    """Verifies the entry point required by `adk run app` is correctly wired.

    `adk run app` imports the `app` package and expects `app` (an App) or
    `root_agent` at the package level via app/__init__.py. In this recipe the
    root is an ADK 2.0 graph Workflow.
    """
    assert hasattr(app, "root_agent") or hasattr(app, "app"), (
        "app/__init__.py must export either `root_agent` or `app` "
        "for `adk run app` to work."
    )

    root = app.agent.root_agent
    assert isinstance(root, Workflow)
    assert root.graph is not None

    # The App wrapper must serve the workflow.
    assert isinstance(app.agent.app, App)
    assert app.agent.app.root_agent is root

    # Every LLM step is a single_turn Agent on the env-configured model.
    nodes = {node.name: node for node in root.graph.nodes}
    for name in ("propose", "narrate"):
        agent = nodes[name]
        assert isinstance(agent, Agent)
        assert agent.mode == "single_turn"
        assert isinstance(agent.model, Gemini)
    assert nodes["propose"].output_schema is app.agent.Candidate
    assert {"init", "verify", "select", "report"} <= set(nodes)


def test_web_server_runnability() -> None:
    """Verifies entry point for `uvicorn app.fast_api_app:app`."""
    import app.fast_api_app

    assert app.fast_api_app.app is not None
    assert isinstance(app.fast_api_app.app, FastAPI)
