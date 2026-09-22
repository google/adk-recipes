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

"""Core tools for the Horizon agent.

Docstring policy for every tool registered on the root agent (enforced by
tests/unit/test_prompt_budget.py): line 1 states what the tool does; document
an arg only when its name and type don't already carry the meaning. No
"Use when" essays, no worked examples, no comparisons to other tools — a
cross-tool choice belongs in TOOL_ROUTING_GUIDANCE
(horizon/conversation/system_prompt.py), not in either tool's description.
"""

from horizon.tools.file_ops import edit, search_files, write

# bash/process live in horizon.tools.processes; import from there directly.
# read_file is a module-level helper for ReadTool's text branch, not a
# registered tool; import it from horizon.tools.file_ops.

__all__ = [
    "edit",
    "search_files",
    "write",
]
