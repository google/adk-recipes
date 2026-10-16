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
    """Verify agent.py imports and defines the expected globals."""
    # set INTEGRATION_TEST so helpers imported by agent.py take their mock path —
    # the setup must happen before the import.
    os.environ.setdefault("INTEGRATION_TEST", "TRUE")

    import app.agent

    assert app.agent.root_agent is not None
    assert app.agent.app is not None
