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

"""Unit tests for the Agent initialization."""

from google.adk.agents import LlmAgent

from app.agent import root_agent


def test_agent_initialization():
    """Validates that the agent initializes properly without validation errors."""
    assert root_agent is not None
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.model is not None
    assert "gemini" in root_agent.model
