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
"""Runnability tests for the ADK 2.0 workflow recipe.

Verifies that the code imports, and that the ADK entry points are wired the
way `adk run app`, `adk web`, `uvicorn app.fast_api_app:app`, the A2A routes
and the Agent Engine adapter expect. Node-level behavior is tested in
tests/unit/.
"""

from fastapi import FastAPI
from google.adk import Agent, Workflow
from google.adk.apps import App
from google.adk.models import Gemini

import app
import app.agent


def test_adk_run_runnability() -> None:
    """`adk run app` imports the package and needs `root_agent` or `app`."""
    assert hasattr(app, "root_agent") or hasattr(app, "app"), (
        "app/__init__.py must export either `root_agent` or `app`."
    )
    # The root is an ADK 2.0 graph workflow.
    assert isinstance(app.agent.root_agent, Workflow)
    assert isinstance(app.agent.app, App)
    assert app.agent.app.root_agent is app.agent.root_agent

    # Every LLM node uses Gemini with the model name from MODEL_NAME, and runs
    # in single_turn mode; there is at least one deterministic function node.
    nodes = {n.name: n for n in app.agent.root_agent.graph.nodes}
    llm_nodes = [n for n in nodes.values() if isinstance(n, Agent)]
    assert {n.name for n in llm_nodes} == {
        "parse_request",
        "explain_plan",
        "explain_no_window",
    }
    for node in llm_nodes:
        assert isinstance(node.model, Gemini)
        assert node.mode == "single_turn"
    assert {"check_request", "find_windows", "plan_crews"} <= set(nodes)


def test_web_server_runnability() -> None:
    """`uvicorn app.fast_api_app:app` builds a FastAPI app with A2A + adapter routes."""
    import app.fast_api_app

    server = app.fast_api_app.app
    assert isinstance(server, FastAPI)
    paths = {getattr(r, "path", "") for r in server.routes}
    assert "/api/reasoning_engine" in paths
    assert "/api/stream_reasoning_engine" in paths
