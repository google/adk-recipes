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

"""``_slice_state`` echoes the bound skill catalog under ``bound_skills``.

The web Skills panel reads installed skills from the existing
``GET /lha/state`` poll; ``_slice_state`` maps ``session.state['bound_skills']``
onto the response key, defaulting to ``[]`` when absent.
"""

from __future__ import annotations

from app.api.state import _slice_state
from app.tools.skill_reload import BOUND_SKILLS_STATE_KEY


def test_bound_skills_round_trips() -> None:
    catalog = [
        {"name": "policy", "description": "rules", "source": "builtin"},
        {"name": "my-changelog", "description": "changelog", "source": "user"},
    ]
    sliced = _slice_state({BOUND_SKILLS_STATE_KEY: catalog})
    assert sliced["bound_skills"] == catalog


def test_bound_skills_defaults_to_empty_list() -> None:
    sliced = _slice_state({})
    assert sliced["bound_skills"] == []
