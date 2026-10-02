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

"""Runnability tests for the retail-product-search recipe."""


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines root_agent."""
    import app.agent

    assert app.agent.root_agent is not None


def test_config_surfacing_flags(monkeypatch) -> None:
    """Verify boolean surfacing flags in app.config."""
    from app.config import config

    monkeypatch.delenv("DEPLOY_AGENT_ENGINE", raising=False)
    monkeypatch.delenv("PUBLISH_GEMINI_ENTERPRISE", raising=False)
    monkeypatch.delenv("PUBLISH_AGENT_GARDEN", raising=False)
    monkeypatch.delenv("DEPLOY_CLOUD_RUN", raising=False)
    monkeypatch.delenv("RUN_LOCAL_WEB", raising=False)

    assert config.DEPLOY_AGENT_ENGINE is True
    assert config.PUBLISH_GEMINI_ENTERPRISE is True
    assert config.PUBLISH_AGENT_GARDEN is True
    assert config.DEPLOY_CLOUD_RUN is False
    assert config.RUN_LOCAL_WEB is False

    monkeypatch.setenv("DEPLOY_CLOUD_RUN", "true")
    assert config.DEPLOY_CLOUD_RUN is True
