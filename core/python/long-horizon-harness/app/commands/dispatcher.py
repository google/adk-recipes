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

"""Slash-command dispatcher for ``before_model_callback``.

Intercepts ``/<cmd>`` user turns. A built-in command short-circuits the
turn — its reply is returned as an ``LlmResponse`` and the model never
runs. A ``/<name>`` that matches a bound skill is rewritten in place and
passed through to the model (returns ``None``), which then runs the skill.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google.adk.models import LlmResponse
from google.genai.types import Content, Part

from app.commands import BUILTIN_COMMAND_REGISTRY
from app.tools.skill_reload import bound_skill_names

logger = logging.getLogger(__name__)


def _last_user_text(llm_request: Any) -> str | None:
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        if getattr(content, "role", None) != "user":
            continue
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                return text
    return None


def _rewrite_last_user_text(llm_request: Any, new_text: str) -> bool:
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        if getattr(content, "role", None) != "user":
            continue
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "text", None):
                part.text = new_text
                return True
    return False


def make_slash_command_dispatcher() -> Callable[..., Awaitable[Any | None]]:
    async def _dispatch(
        *, callback_context: Any, llm_request: Any
    ) -> Any | None:
        text = _last_user_text(llm_request)
        if not text or not text.startswith("/"):
            return None

        cmd, _, rest = text[1:].partition(" ")
        handler = BUILTIN_COMMAND_REGISTRY.get(cmd)
        if handler is None:
            # Skills run via the model (it calls load_skill then follows it),
            # so the bridge rewrites the turn instead of returning a response.
            if cmd in bound_skill_names():
                _rewrite_last_user_text(
                    llm_request, f"Use the {cmd} skill. {rest}".strip()
                )
            return None

        try:
            result = await handler(rest, callback_context)
        except Exception:
            logger.exception("slash command %r raised", cmd)
            result = f"[/{cmd} failed; see server logs]"
        return LlmResponse(
            content=Content(role="model", parts=[Part(text=str(result))]),
        )

    return _dispatch
