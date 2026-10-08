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

"""Pytest configuration for the tests in this directory."""

import os

# Keeps a developer's `src/ambient_quality_agent/.env` out of the tests, as in
# tests/conftest.py.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# Sets required configuration defaults for importing ambient_quality_agent
# during test collection, matching tests/conftest.py.
os.environ.setdefault("AQA_OBSERVED_AGENT_NAME", "test-observed-agent")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
