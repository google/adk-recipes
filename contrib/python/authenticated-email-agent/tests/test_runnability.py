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
"""Runnability tests for the recipe."""

import importlib
import os

os.environ.setdefault("E2A_API_KEY", "synthetic-agent-scoped-key")
os.environ.setdefault("GOOGLE_API_KEY", "synthetic-google-key")
os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

agent = importlib.import_module("app.agent")


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines the expected globals."""
    assert agent.root_agent is not None
