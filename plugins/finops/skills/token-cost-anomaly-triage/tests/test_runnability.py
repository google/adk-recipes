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

"""Runnability test for the token-cost-anomaly-triage skill."""

import importlib
import sys
from pathlib import Path

# Ensure skill directory is on path
SKILL_DIR = Path(__file__).parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


def test_triage_script_runnability() -> None:
    """Verify scripts/triage.py imports and defines main entrypoint."""
    module = importlib.import_module("scripts.triage")
    assert getattr(module, "main", None) is not None
