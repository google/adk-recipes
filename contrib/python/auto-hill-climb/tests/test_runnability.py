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
"""Runnability test for the recipe."""

from unittest.mock import patch

from google.adk.agents import Agent
from google.adk.apps import App


def test_agent_runnability() -> None:
    """agent.py imports and builds the agent without calling a model."""
    test_env = {
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "GOOGLE_CLOUD_LOCATION": "global",
        "MODEL_NAME": "gemini-3.8-flash",
    }
    with patch.dict("os.environ", test_env):
        import app.agent

    assert isinstance(app.agent.root_agent, Agent)
    assert app.agent.root_agent.name == "support_agent"
    assert isinstance(app.agent.app, App)
