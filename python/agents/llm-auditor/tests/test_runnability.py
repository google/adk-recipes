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
from unittest.mock import MagicMock, patch


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines the expected globals."""
    # provide a dummy GCP project and patch google.auth.default() so import-time
    # credential lookups don't need ADC — the setup must happen before the import.
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
    os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

    with patch(
        "google.auth.default", return_value=(MagicMock(), "test-project")
    ):
        import llm_auditor.agent

    assert llm_auditor.agent.root_agent is not None
