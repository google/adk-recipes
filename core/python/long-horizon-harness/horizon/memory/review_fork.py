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

"""Background self-improvement fork — judges what to save after every turn.

After each parent turn, ``review_fork_callback`` hands a restricted-toolset
agent (memory + skills only) to the ``SiblingAgentPlugin``. The fork
receives the turn as a ``<CONVERSATION>...</CONVERSATION>`` snapshot
followed by one of three review prompts (``horizon.memory.review_prompts``)
and is asked: "given what just happened, what's worth saving?".

Fire-and-forget: the parent's response goes out immediately; memory and
skill writes land before the next turn's ``PreloadMemoryTool`` query.

Recursion guard is structural — the fork's ``Agent`` has no
``after_agent_callback`` chain, and the whitelist gates writes to
``.agents/skills/<name>/...`` paths only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import partial
from typing import Any

from google.adk.agents import Agent
from google.adk.models import Gemini

from horizon.memory._fork import (
    format_conversation_snapshot,
    log_successful_writes,
    make_whitelist_callback,
)
from horizon.memory._throttle import try_claim
from horizon.memory.add_memory_tool import memory
from horizon.memory.review_prompts import (
    COMBINED_REVIEW_PROMPT,
    MEMORY_REVIEW_PROMPT,
)
from horizon.memory.skill_telemetry import (
    SKILL_TELEMETRY_STATE_KEY,
    skill_name_from_path,
)
from horizon.models.registry import ROBUST_RETRY_OPTIONS
from horizon.tools import names
from horizon.tools.file_ops import edit, write

REVIEW_FORK_ENABLED_ENV = "LHA_REVIEW_FORK"

# The review fork is restricted to memory curation + skill curation. File
# ops are allowed only for writes under ``.agents/skills/`` so the fork cannot
# touch arbitrary workspace files. load_skill covers both reading a skill
# and action='reload' (the former standalone reload tool folded into it).
_SKILL_READ_TOOL_NAMES: frozenset[str] = frozenset({names.LOAD_SKILL})
_SKILL_MUTATE_TOOL_NAMES: frozenset[str] = frozenset({names.WRITE, names.EDIT})
_REVIEW_TOOL_NAMES: frozenset[str] = (
    frozenset({names.MEMORY})
    | _SKILL_READ_TOOL_NAMES
    | _SKILL_MUTATE_TOOL_NAMES
)

_REVIEW_AGENT_NAME = "review_fork"
_REVIEW_AGENT_MODEL = "gemini-3.7-flash"
_FORK_INSTRUCTION = (
    "You are a self-improvement curator. The parent agent has just "
    "finished a turn with the user; the conversation is provided to "
    "you as a <CONVERSATION>...</CONVERSATION> block in the user "
    "message, followed by a review prompt. Follow the review prompt's "
    "instructions exactly. Your only tools are: memory; load_skill "
    "(read a skill's instructions, or pass resource='references/...' for "
    "a bundled file — the catalog is auto-injected as <available_skills>); "
    "write and edit "
    "(restricted to paths under '.agents/skills/<name>/...'); and "
    "load_skill(action='reload') (call after any write so the parent picks "
    "up the change on its next turn). When you have either saved what's "
    "worth saving or determined nothing needs saving, stop."
)


def _pick_prompt(telemetry: Mapping[str, Mapping[str, Any]] | None) -> str:
    """Combined when the session touched skills, memory-only otherwise."""
    if not telemetry:
        return MEMORY_REVIEW_PROMPT
    for rec in telemetry.values():
        if not isinstance(rec, Mapping):
            continue
        try:
            views = int(rec.get("views", 0) or 0)
            manages = int(rec.get("manages", 0) or 0)
        except (TypeError, ValueError):
            continue
        if views > 0 or manages > 0:
            return COMBINED_REVIEW_PROMPT
    return MEMORY_REVIEW_PROMPT


def _is_skills_path(args: Any) -> bool:
    if not isinstance(args, Mapping):
        return False
    return skill_name_from_path(args.get("path")) is not None


def _skills_path_check(name: str, args: Any) -> dict[str, Any] | None:
    if name in _SKILL_MUTATE_TOOL_NAMES and not _is_skills_path(args):
        return {
            "error": (
                f"Tool {name!r} in the review fork may only write to "
                f"'.agents/skills/<name>/...' paths."
            )
        }
    return None


# Monkeypatch hook — tests rebind these via this module's namespace.
_format_snapshot = format_conversation_snapshot
_whitelist_tools_callback = make_whitelist_callback(
    _REVIEW_TOOL_NAMES, fork_name="review", extra_check=_skills_path_check
)
_log_action = partial(log_successful_writes, log_prefix="review_fork")


def _build_review_agent() -> Agent:
    from horizon.agent import _SKILL_TOOLSET

    return Agent(
        name=_REVIEW_AGENT_NAME,
        model=Gemini(
            model=_REVIEW_AGENT_MODEL,
            retry_options=ROBUST_RETRY_OPTIONS,
        ),
        instruction=_FORK_INSTRUCTION,
        tools=[memory, _SKILL_TOOLSET, write, edit],
        before_tool_callback=_whitelist_tools_callback,
    )


_review_agent: Agent | None = None


def _get_review_agent() -> Agent:
    global _review_agent
    if _review_agent is None:
        _review_agent = _build_review_agent()
    return _review_agent


async def review_fork_callback(callback_context: Any) -> None:
    """``after_agent_callback`` — hand the review run to ``SiblingAgentPlugin``.

    Throttled per session — see :mod:`horizon.memory._throttle`.
    """
    if os.environ.get(REVIEW_FORK_ENABLED_ENV, "1") == "0":
        return None

    invocation_context = callback_context._invocation_context
    memory_service = getattr(invocation_context, "memory_service", None)
    if memory_service is None:
        return None

    if not try_claim(callback_context.state, "review_fork"):
        return None

    telemetry = callback_context.state.get(SKILL_TELEMETRY_STATE_KEY)
    prompt = _pick_prompt(telemetry)

    parent_session = getattr(invocation_context, "session", None)
    events = list(getattr(parent_session, "events", []) or [])
    snapshot_text = _format_snapshot(events)

    # Local import — horizon.agent imports from this module (via
    # ``from horizon.memory.review_fork import review_fork_callback``), so a
    # top-level import would cycle.
    from horizon.agent import SIBLING_AGENT_PLUGIN

    SIBLING_AGENT_PLUGIN.spawn_sibling(
        agent=_get_review_agent(),
        prompt=f"{snapshot_text}\n\n{prompt}",
        app_name=invocation_context.app_name,
        user_id=invocation_context.user_id,
        memory_service=memory_service,
        on_event=_log_action,
        log_prefix="review_fork",
    )
    return None
