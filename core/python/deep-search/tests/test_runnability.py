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
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_ai_studio_import_does_not_request_adc() -> None:
    """AI Studio startup must not require application-default credentials."""
    script = """
import os
import sys
from types import ModuleType
import google.auth

def unexpected_adc_lookup():
    raise AssertionError("AI Studio startup must not request ADC")

google.auth.default = unexpected_adc_lookup
agent = ModuleType("app.agent")
agent.root_agent = None
sys.modules["app.agent"] = agent
import app
assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "False"
"""
    env = os.environ.copy()
    for key in (
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ):
        env.pop(key, None)
    env["GOOGLE_API_KEY"] = "test-key"
    env["MODEL_NAME"] = "gemini-3.5-flash"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines the expected globals."""
    # provide a dummy GCP project and patch google.auth.default() so import-time
    # credential lookups don't need ADC — the setup must happen before the import.
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
    os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

    with patch(
        "google.auth.default", return_value=(MagicMock(), "test-project")
    ):
        import app.agent

    assert app.agent.root_agent is not None
    assert app.agent.app is not None
