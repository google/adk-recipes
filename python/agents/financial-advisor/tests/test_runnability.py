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


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines the expected globals."""
    import financial_advisor.agent

    assert financial_advisor.agent.root_agent is not None
    assert financial_advisor.agent.app is not None


def test_fast_api_app_runnability() -> None:
    """Verify fast_api_app boots and serves /list-apps and A2A agent card."""
    from fastapi.testclient import TestClient

    from financial_advisor.fast_api_app import app

    with TestClient(app) as client:
        resp = client.get("/list-apps")
        assert resp.status_code == 200
        assert "financial_advisor" in resp.json()

        card_resp = client.get(
            "/a2a/financial_advisor/.well-known/agent-card.json"
        )
        assert card_resp.status_code == 200
