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

"""Pytest configuration and environment fixtures for finops-zero-trust-governor."""

import os
import sys
from pathlib import Path

# Ensure recipe root is on sys.path
RECIPE_ROOT = Path(__file__).parent.parent
if str(RECIPE_ROOT) not in sys.path:
    sys.path.insert(0, str(RECIPE_ROOT))

# Set test environment flags
os.environ["INTEGRATION_TEST"] = "TRUE"
os.environ["GEMINI_MODEL"] = "gemini-3.7-flash"
os.environ["FINOPS_MAX_LLM_TURNS_PER_RUN"] = "10"
os.environ["GOOGLE_CLOUD_PROJECT"] = "test-finops-project"
