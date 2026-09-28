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

"""Runnability test for the retail-virtual-tryon recipe."""


def test_agent_runnability() -> None:
    """Verify scripts/tryon_agent.py imports and defines root_agent."""
    import importlib

    module = importlib.import_module("scripts.tryon_agent")

    assert getattr(module, "root_agent", None) is not None
