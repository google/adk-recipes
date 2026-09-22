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

"""Pre-compaction memory flush — extract durable user facts before they vanish.

Fires once when ``HorizonSummarizer`` is about to compact events (ADK's
``EventsCompactionConfig`` summarizer hook). A narrowly-scoped sibling
agent (``memory`` only) walks the soon-to-be-compacted events and
saves anything worth keeping. Mirrors the structure of
``horizon/memory/review_fork.py`` but with a sharper toolset and a flush-
focused prompt.

Gated by ``LHA_PRE_COMPRESS_FLUSH`` (default on). Set to ``0`` in eval
harnesses where the extra LLM cost would distort baselines.

Recursion guard is structural — the fork's ``Agent`` has no
``after_agent_callback`` chain, and the whitelist blocks every tool except
``memory``.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import Agent
from google.adk.events.event import Event
from google.adk.models import Gemini

from horizon.memory._fork import (
    format_conversation_snapshot,
    make_whitelist_callback,
)
from horizon.memory.add_memory_tool import memory
from horizon.models.registry import ROBUST_RETRY_OPTIONS
from horizon.tools import names

FLUSH_FORK_ENABLED_ENV = "LHA_PRE_COMPRESS_FLUSH"

_FLUSH_TOOL_NAMES: frozenset[str] = frozenset({names.MEMORY})

_FLUSH_AGENT_NAME = "flush_fork"
_FLUSH_AGENT_MODEL = "gemini-3.7-flash"
_FLUSH_INSTRUCTION = (
    "You are a memory flush curator. The conversation provided in the "
    "<CONVERSATION>...</CONVERSATION> block is about to be discarded by "
    "context compression. Your only job: surface durable user facts that "
    "would be lost. Save them via memory — nothing else is available "
    "to you. When you have saved what's worth saving (or determined "
    "nothing qualifies), stop."
)

_FLUSH_PROMPT = (
    "Review the conversation above. The next step will compress it into a "
    "short summary and discard the original events, so any durable user "
    "fact you don't save now is lost.\n\n"
    "Save with memory ONLY if the fact is:\n"
    "  - About the user (preference, role, environment, recurring "
    "constraint) — scope='user'.\n"
    "  - Stable: still true a week from now.\n"
    "  - Not already obvious from the project files.\n\n"
    "Skip task-progress notes, PR numbers, transient errors, and "
    "anything that will be stale soon. If nothing qualifies, say "
    "'Nothing to flush.' and stop."
)


# Monkeypatch hook — tests rebind these via this module's namespace.
_format_snapshot = format_conversation_snapshot
_whitelist_tools_callback = make_whitelist_callback(
    _FLUSH_TOOL_NAMES, fork_name="flush"
)


def _build_flush_agent() -> Agent:
    return Agent(
        name=_FLUSH_AGENT_NAME,
        model=Gemini(
            model=_FLUSH_AGENT_MODEL,
            retry_options=ROBUST_RETRY_OPTIONS,
        ),
        instruction=_FLUSH_INSTRUCTION,
        tools=[memory],
        before_tool_callback=_whitelist_tools_callback,
    )


_flush_agent: Agent | None = None


def _get_flush_agent() -> Agent:
    global _flush_agent
    if _flush_agent is None:
        _flush_agent = _build_flush_agent()
    return _flush_agent


def spawn_flush_fork(
    *,
    parent_memory_service: Any,
    parent_app_name: str,
    parent_user_id: str,
    events: list[Event],
    sibling_agent_plugin: Any = None,
) -> bool:
    """Hand the flush run to ``SiblingAgentPlugin`` fire-and-forget.

    Returns True iff a sibling was scheduled. Called by
    ``HorizonSummarizer.maybe_summarize_events`` right before ADK folds the
    events into a compaction summary. Returns False (no-op) when the env
    gate is off, the parent has no memory service, there are no events to
    flush, or the plugin handle is missing.
    """
    if os.environ.get(FLUSH_FORK_ENABLED_ENV, "1") == "0":
        return False
    if parent_memory_service is None:
        return False
    if not events:
        return False

    plugin = sibling_agent_plugin
    if plugin is None:
        # Fall back to the module-level handle — keeps the API callable
        # from places that don't have a CompactionContext (e.g. ad-hoc
        # scripts) while letting HorizonSummarizer pass the per-session plugin
        # explicitly.
        from horizon.agent import SIBLING_AGENT_PLUGIN

        plugin = SIBLING_AGENT_PLUGIN

    snapshot_text = _format_snapshot(events)
    plugin.spawn_sibling(
        agent=_get_flush_agent(),
        prompt=f"{snapshot_text}\n\n{_FLUSH_PROMPT}",
        app_name=parent_app_name,
        user_id=parent_user_id,
        memory_service=parent_memory_service,
        log_prefix="flush_fork",
    )
    return True


__all__ = [
    "FLUSH_FORK_ENABLED_ENV",
    "spawn_flush_fork",
]
