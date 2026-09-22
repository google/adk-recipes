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

"""Per-turn rewrite of the ``subagent`` tool's description.

ADK derives a ``FunctionDeclaration`` from each tool's docstring once, at
import — so the model never sees session-scoped facts (which child
archetypes exist). This ``before_model_callback`` rewrites the live
declaration's ``description`` every turn, splicing in the profile registry.
The Python docstrings stay static; only the model's runtime view changes.

The skill catalog used to be spliced in here too, a second copy of the same
``<available_skills>`` XML block already sitting in the system prompt (cut A
of the prompt-minimalism plan's skills-surface work — the ``delegate``+
``agent`` merge into ``subagent`` had already removed the other copy). The
model already has the real catalog; this suffix just points at it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse

from horizon.subagents.profiles import render_profiles_block
from horizon.tools import names

_SUBAGENT_TOOL_NAMES = frozenset({names.SUBAGENT})
_DYNAMIC_MARKER = "\n\n<!-- lha:dynamic -->\n\n"

_SKILLS_POINTER = (
    "Pass skill names from your `<available_skills>` block (skills=[...])."
)


def _base_description(current: str | None) -> str:
    if not current:
        return ""
    return current.split(_DYNAMIC_MARKER, 1)[0]


def _build_suffix() -> str:
    return f"{_SKILLS_POINTER}\n\n{render_profiles_block()}"


def make_subagent_description_callback() -> Callable[
    [CallbackContext, LlmRequest], Awaitable[LlmResponse | None]
]:
    async def _callback(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        config = getattr(llm_request, "config", None)
        tools = getattr(config, "tools", None) if config is not None else None
        if not tools:
            return None

        suffix = _build_suffix()
        for tool in tools:
            fds = getattr(tool, "function_declarations", None) or []
            for fd in fds:
                if fd.name not in _SUBAGENT_TOOL_NAMES:
                    continue
                base = _base_description(fd.description)
                fd.description = f"{base}{_DYNAMIC_MARKER}{suffix}"
        return None

    return _callback


subagent_description_callback = make_subagent_description_callback()


__all__ = [
    "make_subagent_description_callback",
    "subagent_description_callback",
]
