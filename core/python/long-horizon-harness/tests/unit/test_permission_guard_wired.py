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

"""permission_guard is registered last in the before_tool_callback chain."""

from __future__ import annotations

from app.agent import root_agent


def test_permission_guard_is_last_before_tool_callback():
    callbacks = root_agent.before_tool_callback
    names = [getattr(cb, "__name__", "") for cb in callbacks]
    assert "permission_guard" in names
    assert names[-1] == "permission_guard"
    assert names.index("permission_guard") > names.index("policies_guard")
