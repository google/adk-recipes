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

"""Strip source indentation from tool descriptions before they ship.

ADK's non-pydantic declaration path (selected via
ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL for a smaller schema) passes
``func.__doc__`` through verbatim, four-space source indentation included.
Tool-agnostic on purpose, so it also normalizes declarations ADK itself
contributes.
"""

from __future__ import annotations

import inspect

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse


def normalize_tool_descriptions(llm_request: LlmRequest) -> int:
    """Cleandoc every function declaration in place. Returns chars saved."""
    saved = 0
    for tool in getattr(llm_request.config, "tools", None) or []:
        for decl in getattr(tool, "function_declarations", None) or []:
            text = getattr(decl, "description", None)
            if not text:
                continue
            cleaned = inspect.cleandoc(text)
            if cleaned != text:
                saved += len(text) - len(cleaned)
                decl.description = cleaned
    return saved


async def normalize_tool_schemas_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    normalize_tool_descriptions(llm_request)
    return None


__all__ = [
    "normalize_tool_descriptions",
    "normalize_tool_schemas_callback",
]
