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

"""Harness-facing prompts and shared context blocks.

The system prompt carries only the thread's workspace facts and user profile;
how to work on the computer lives in `<home>/AGENTS.md` and skills on disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.memory.profile import render_profile
from app.threads.model import (
    Directive,
    InstructionKind,
    MessageKind,
    ThreadHandle,
)
from app.workers.workspace import (
    ProjectKind,
    is_code,
    project_kind,
    project_path,
)

_PROJECT_KINDS = {
    ProjectKind.CODE: "a code repository",
    ProjectKind.PROJECT: "a long-running project folder",
    ProjectKind.SCRATCH: "a scratch folder for one-off work",
}


def system_prompt(
    *,
    project: str,
    cwd: str,
    home: str,
    branch: str,
    profile: Mapping[str, Any] | None = None,
    project_name: str = "",
) -> str:
    """Build the thread's workspace context and user profile block."""
    root = project_path(home, project)
    where = f"Working directory: {cwd}"
    if cwd.rstrip("/") != root:
        where += f" (a git worktree of {root}, merged back when you finish)"
    lines = [
        "THREAD CONTEXT",
        f"Project: {project_name} ({project}, {_PROJECT_KINDS[project_kind(project)]})"
        if project_name
        else f"Project: {project} ({_PROJECT_KINDS[project_kind(project)]})",
        where,
    ]
    if branch:
        lines.append(f"Branch: {branch}")
    lines.append(
        f"This computer's guide and skills: read {home}/AGENTS.md and the SKILL.md of "
        f"each skill in {home}/.agents/skills that fits the thread, in one command "
        f"(`cat {home}/AGENTS.md {home}/.agents/skills/<name>/SKILL.md ...`)."
    )
    if profile_block := render_profile(profile or {}):
        lines += ["", profile_block.rstrip()]
    return "\n".join(lines)


def build_message(
    directive: Directive, handle: ThreadHandle, *, handoff: bool
) -> str:
    """Build the user message for one segment (`handoff=True` prepends prior turn context)."""
    kind = directive.kind
    instruction = (directive.text or "").strip()
    command = (
        directive.permission.command
        if directive.permission
        else "the blocked command"
    )
    if kind is MessageKind.START:
        body = instruction or handle.goal
    elif kind is MessageKind.FOLLOWUP:
        body = instruction
    elif kind is MessageKind.REDIRECT:
        body = f"The user interrupted with new direction. Adjust and continue:\n{instruction}"
    elif kind is MessageKind.PERMISSION_GRANTED:
        body = f"Approved: `{command}`. Go ahead with it now (exactly once) and continue."
    elif kind is MessageKind.PERMISSION_DENIED:
        reason = instruction or "no reason given"
        body = f"The user denied `{command}` ({reason}). Do not run it. Continue without it, or explain what is blocked."
    else:
        raise ValueError(f"Unknown message kind {kind!r}")
    if directive.context and not (
        handoff
        and any(i.context == directive.context for i in handle.instructions)
    ):
        body = f"{directive.context}\n\n{body}"
    return f"{handoff_summary(handle)}\n\n{body}" if handoff else body


def handoff_summary(handle: ThreadHandle) -> str:
    """Context for a harness that has no session history for this thread."""
    where = "clone" if is_code(handle.project) else "workspace"
    lines = [
        "[CONTEXT FROM EARLIER WORK ON THIS THREAD]",
        *_context_lines(handle),
    ]
    lines.extend(i.context for i in handle.instructions if i.context)
    lines.append(
        f"Your {where} contains all earlier changes. Continue from that state."
    )
    return "\n".join(lines)


def share_context_block(source: ThreadHandle) -> str:
    """Context shared explicitly from `source` into another thread (`share_context`)."""
    lines = [
        f'[SHARED CONTEXT from thread {source.thread_id} "{source.title}" in {source.project}]',
        *_context_lines(source),
    ]
    if source.branch:
        lines.append(f"Source branch: {source.branch}")
    return "\n".join(lines)


def _context_lines(handle: ThreadHandle) -> list[str]:
    lines = [f"Original goal: {handle.goal}"]
    followups = [
        i.text
        for i in handle.instructions
        if i.kind is not InstructionKind.GOAL
    ]
    if followups:
        lines.append("Later messages from the user, oldest first:")
        lines.extend(f"- {t}" for t in followups[-8:])
    if handle.result_text:
        lines.append(f"Most recent outcome: {handle.result_text[:1500]}")
    if handle.files_changed:
        lines.append(
            f"Files changed in the last pass: {', '.join(handle.files_changed[:20])}"
        )
    if outputs := handle.outputs:
        items = [
            f"{o['title'] or o['kind']} ({o['url']})" for o in outputs[-8:]
        ]
        lines.append(f"Made for the user: {'; '.join(items)}")
    return lines
