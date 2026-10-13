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
    """Verify that the recipe module imports and exports a root agent."""
    os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

    from amg_memory_guard_adk.agent import root_agent

    assert root_agent is not None
