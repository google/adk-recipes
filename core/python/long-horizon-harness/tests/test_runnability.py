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

"""Runnability test: the agent module imports and exposes a root agent."""


def test_agent_runnability() -> None:
    import app.agent

    assert app.agent.root_agent is not None


def test_app_is_built() -> None:
    """`app.agent` also owns the ADK ``App`` the Runner/FastAPI surface serves."""
    import app.agent

    assert app.agent.app is not None
