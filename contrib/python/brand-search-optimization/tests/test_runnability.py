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

"""Runnability tests for brand search optimization recipe."""

import os
from unittest.mock import MagicMock, patch


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines the expected globals."""
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
    os.environ.setdefault("DISABLE_WEB_DRIVER", "1")

    with patch(
        "google.auth.default", return_value=(MagicMock(), "test-project")
    ):
        import brand_search_optimization.agent

    agent = brand_search_optimization.agent.root_agent
    assert agent is not None
    assert agent.name == "brand_search_optimization"
    assert len(agent.sub_agents) == 3
    sub_agent_names = {sub.name for sub in agent.sub_agents}
    assert "keyword_finding_agent" in sub_agent_names
    assert "search_results_agent" in sub_agent_names
    assert "comparison_root_agent" in sub_agent_names
