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

"""Canonical tool-name registry. The only module allowed to contain
tool-name string literals; every fail-closed set elsewhere in horizon
imports its members from here instead of hardcoding a name.

Each constant holds today's registered tool name. A future rename (or
merge, handled by its own task) only ever edits the value on the right
of one of these assignments — every consumer, and TOOL_NAME_ALIASES
below, updates automatically because they hold a reference to the
constant, not a copy of the string.
"""

from __future__ import annotations

READ = "read"
WRITE = "write"
EDIT = "edit"
BASH = "bash"
PROCESS = "process"
SEARCH_FILES = "search_files"
SUBAGENT = "subagent"
# Covers both add and search (former session_search) actions.
MEMORY = "memory"
LOAD_SKILL = "load_skill"
WEB_RESEARCH = "web_research"
ARTIFACT = "artifact"
ROUTINE = "routine"
CLARIFY = "clarify"
PRELOAD_MEMORY = "preload_memory"

# Every currently registered tool name, exactly. Consistency test 2
# asserts this equals {t.name for t in root_agent.canonical_tools()}.
ALL: frozenset[str] = frozenset(
    {
        READ,
        WRITE,
        EDIT,
        BASH,
        PROCESS,
        SEARCH_FILES,
        SUBAGENT,
        MEMORY,
        LOAD_SKILL,
        WEB_RESEARCH,
        ARTIFACT,
        ROUTINE,
        CLARIFY,
        PRELOAD_MEMORY,
    }
)

# Legacy name -> current constant. A no-op today (every legacy name already
# equals its constant's value); a later rename changes only the constant
# above, and every key here keeps resolving to the live name because the
# dict holds a reference to the constant, not a snapshotted string.
TOOL_NAME_ALIASES: dict[str, str] = {
    "read_file": READ,
    # view_file was merged into read (Task 3); a persisted permission rule
    # keyed on the old tool name must still resolve to the tool that now
    # handles that capability.
    "view_file": READ,
    "write_file": WRITE,
    "patch": EDIT,
    "terminal": BASH,
    "process": PROCESS,
    "search_files": SEARCH_FILES,
    # delegate and agent were merged into subagent (Task 4); a persisted
    # permission rule keyed on either old tool name must still resolve to
    # the tool that now handles that capability.
    "delegate": SUBAGENT,
    "agent": SUBAGENT,
    "add_memory": MEMORY,
    # session_search (and, before Task 11's rename, recall_past_sessions) was
    # folded into memory(action="search") — a persisted rule keyed on either
    # old tool name must still resolve to the tool that now handles it.
    "recall_past_sessions": MEMORY,
    "session_search": MEMORY,
    "load_skill": LOAD_SKILL,
    # reload was folded into load_skill(action="reload") — a persisted rule
    # keyed on the old tool name must still resolve to the tool that now
    # handles that capability. The /reload SLASH COMMAND is a separate
    # surface (horizon/commands/__init__.py) and is unaffected.
    "reload": LOAD_SKILL,
    "web_research": WEB_RESEARCH,
    "artifact": ARTIFACT,
    "routine": ROUTINE,
    "clarify": CLARIFY,
    "preload_memory": PRELOAD_MEMORY,
    # Deleted tools (write_todos, report_to_maintainers, repo_overview,
    # set_workspace_window, load_skill_resource, run_skill_script, reminder)
    # are deliberately absent, not aliased, so a persisted rule naming one
    # stays inert instead of silently rebinding to an unrelated live tool.
    # reminder has no successor: routine is a different capability (headless,
    # unattended, cannot prompt the user), not a rebind target.
}


def apply_tool_aliases(name: str) -> str:
    """Map a possibly-legacy tool name to its current registered name.

    A name with no entry (never existed, or named a tool deleted outright
    with no successor) passes through unchanged, so it matches no live tool.
    """
    return TOOL_NAME_ALIASES.get(name, name)


__all__ = [
    "ALL",
    "ARTIFACT",
    "BASH",
    "CLARIFY",
    "EDIT",
    "LOAD_SKILL",
    "MEMORY",
    "PRELOAD_MEMORY",
    "PROCESS",
    "READ",
    "ROUTINE",
    "SEARCH_FILES",
    "SUBAGENT",
    "TOOL_NAME_ALIASES",
    "WEB_RESEARCH",
    "WRITE",
    "apply_tool_aliases",
]
