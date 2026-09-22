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

"""Build the root agent's tool declarations on ADK's legacy schema path.

ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL selects that path process-wide, and
agents-cli imports this package to reach root_agent, so setting it would
re-render the declarations of every other ADK agent in the process. Scope
it to one build instead. Child agents stay on the pydantic path rather than
pay a call site each.

docs/context-budget.md has what the legacy path is worth.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from google.adk.features._feature_registry import (
    FeatureName,
    temporary_feature_override,
)
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool

__all__ = ["compact_tool_declarations"]

# temporary_feature_override mutates process-global state, so builds are
# serialised; the guarded section is synchronous, so only threads can race.
_BUILD = threading.Lock()


def _compact(tool: BaseTool) -> BaseTool:
    inner = tool._get_declaration

    def scoped() -> Any:
        with (
            _BUILD,
            temporary_feature_override(
                FeatureName.JSON_SCHEMA_FOR_FUNC_DECL, False
            ),
        ):
            return inner()

    # copy.copy, never a BaseTool subclass: an override returning None would
    # delete the tool from the model's view in silence. Rebuilt per call
    # rather than pinned, so a session-varying declaration keeps varying.
    clone = copy.copy(tool)
    object.__setattr__(clone, "_get_declaration", scoped)
    return clone


def compact_tool_declarations(tools: list[Any]) -> list[Any]:
    """Compact our own declarations, leaving the rest of the process alone."""
    out: list[Any] = []
    for tool in tools:
        if isinstance(tool, BaseTool):
            out.append(_compact(tool))
        elif callable(tool):
            out.append(_compact(FunctionTool(tool)))  # what ADK does anyway
        else:
            out.append(tool)  # a BaseToolset builds its own declarations
    return out
