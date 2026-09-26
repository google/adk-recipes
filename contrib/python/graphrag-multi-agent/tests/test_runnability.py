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

import os


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines root_agent.

    The package __init__ validates that a credential is present, so a dummy
    API key is supplied here. No NEO4J_* or MCP_TOOLBOX_URL values are needed:
    the Neo4j driver connects lazily on first tool use, and the MCP Toolbox is
    skipped when unconfigured, so importing the agent performs no network I/O.
    """
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "0")
    os.environ.setdefault("GOOGLE_API_KEY", "test-key")
    os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

    import app.agent

    assert app.agent.root_agent is not None
