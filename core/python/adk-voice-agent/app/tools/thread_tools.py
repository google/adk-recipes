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

"""Voice-facing thread tools. They speak the thread contract and never branch on the harness.

The docstrings are the tool declarations the live model reads.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

from app.threads.describe import (
    action_label,
    first_line,
    next_step,
    permission_phrase,
    short,
    state_phrase,
    thread_name,
    thread_ref,
)
from app.threads.model import (
    GATE_STATES,
    RUNNING_STATES,
    InstructionKind,
    ThreadHandle,
    ThreadState,
)
from app.threads.registry import ThreadError, get_thread_registry
from app.threads.titles import thread_title
from app.voice.focus import get_focus
from app.workers.environment import Environment, FileEntry
from app.workers.factory import resolve_environment
from app.workers.harnesses.registry import get_harness_registry
from app.workers.workspace import ProjectKind, is_code, resolve_project

logger = logging.getLogger(__name__)

# Key a thread tool's result carries so `app.voice.non_blocking.thread_tool` can wait
# for the thread's next milestone after it.
SINCE_SEQ_KEY = "_since_seq"
# The same for a call that acted on several threads: thread ID -> sequence number.
SINCE_SEQS_KEY = "_since_seqs"

_VOICE_TEXT_BUDGET = 600
_VOICE_DIFF_BUDGET = 1200
_RECENT_LIMIT = 8
# list_projects: the folders it lists, how it labels them, and how many of each.
_PROJECT_KINDS = {
    ProjectKind.CODE: "Repositories",
    ProjectKind.PROJECT: "Projects",
    ProjectKind.SCRATCH: "Recent scratch folders",
}
_PROJECTS_PER_KIND = 10

_STEER_REPLIES = {
    InstructionKind.DENIAL: "Declined that for {name}. It is continuing without it.",
    InstructionKind.REDIRECT: "Interrupted {name} and redirected it.",
    InstructionKind.FOLLOWUP: "Started round {round} of {name}.",
}


def _resolve_target_id(raw_id: str, *, require_gate: bool = False) -> str:
    """Resolve `'current'` / `'this'` / empty to the thread open on screen or the sole active thread."""
    cleaned = (raw_id or "").strip()
    if cleaned.lower() not in ("", "current", "this", "active"):
        return cleaned

    registry = get_thread_registry()
    focus = get_focus()
    if focus.get("view") == "thread" and focus.get("thread_id"):
        return focus["thread_id"]

    handles = registry.snapshot()
    if focus.get("view") == "project" and focus.get("project"):
        in_project = [h for h in handles if h.project == focus["project"]]
        if require_gate:
            gated = [h for h in in_project if h.state in GATE_STATES]
            if gated:
                return gated[0].thread_id
        if in_project:
            return in_project[0].thread_id

    if require_gate:
        gated = [h for h in handles if h.state in GATE_STATES]
        if len(gated) == 1:
            return gated[0].thread_id

    active = [h for h in handles if h.state in (RUNNING_STATES | GATE_STATES)]
    if len(active) == 1:
        return active[0].thread_id
    if len(handles) == 1:
        return handles[0].thread_id
    return cleaned or "current"


async def start_thread(
    goal: str,
    project: str = "",
    title: str = "",
    harness: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Start a thread on the user's computer in a folder of their home (its project).

    Args:
        goal: What the thread should accomplish, in the user's words plus any context they gave.
        project: Target folder: empty for a new one-off (scratch), 'code/<repo>',
            'projects/<name>' for an ongoing project, or 'scratch/<name>' from
            list_threads/list_projects.
        title: 2 to 6 words naming the deliverable, no repo names (e.g. 'Portland trip plan').
        harness: Optional harness name; defaults to the user's default.
    """
    registry = get_thread_registry()
    try:
        resolved = resolve_project(project, title=thread_title(title, goal))
    except ValueError as exc:
        return {"message": f"Could not start the thread: {exc}"}
    session = _session(tool_context)
    # The live model occasionally emits the same dispatch twice in one turn.
    async with registry.dispatch_lock():
        if duplicate := registry.recent_duplicate(goal, resolved):
            return _reply(
                duplicate,
                f"{thread_name(duplicate)} ({duplicate.thread_id}) was just started for this; not starting another.",
                since_seq=duplicate.last_seq,
            )
        try:
            handle = await registry.dispatch(
                goal,
                resolved or "",
                harness=harness,
                title=title,
                origin_session_id=session.id if session is not None else None,
            )
        except ThreadError as exc:
            return {"message": f"Could not start the thread: {exc}"}
    return _reply(
        handle,
        f"Started {thread_name(handle)} ({handle.thread_id}){_where(handle)} with "
        f"{_harness_name(handle)}. It is working on it now.",
        since_seq=0,
    )


async def message_thread(
    thread_id: str = "current",
    instruction: str = "",
    harness: str | None = None,
    also_thread_ids: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Send a follow-up, answer, permission denial, or redirect to an existing thread.

    Args:
        thread_id: Thread ID or 'current' for the thread on screen.
        instruction: The user's message, answer, or new direction.
        harness: Optional harness to switch this thread onto.
        also_thread_ids: Additional thread IDs to send the same instruction to.
    """
    registry = get_thread_registry()
    session = _session(tool_context)
    resolved_first = _resolve_target_id(thread_id)
    targets = list(dict.fromkeys([resolved_first, *(also_thread_ids or [])]))
    steered: list[tuple[ThreadHandle, int]] = []
    failures: list[str] = []
    for target in targets:
        try:
            since = (await registry.require(target)).last_seq
            handle = await registry.steer(
                target,
                instruction,
                harness=harness,
                session_id=session.id if session is not None else None,
            )
        except ThreadError as exc:
            failures.append(str(exc))
            continue
        steered.append((handle, since))
    if not steered:
        return {"message": " ".join(failures)}
    if len(targets) == 1:
        handle, since = steered[0]
        return _reply(handle, _steer_reply(handle), since)
    return {
        "thread_ids": [h.thread_id for h, _ in steered],
        "message": " ".join([_steer_reply(h) for h, _ in steered] + failures),
        SINCE_SEQS_KEY: {h.thread_id: since for h, since in steered},
    }


def _steer_reply(handle: ThreadHandle) -> str:
    kind = handle.instructions[-1].kind
    template = _STEER_REPLIES.get(kind, "Started round {round} of {name}.")
    return template.format(name=thread_name(handle), round=handle.iteration)


async def approve(
    thread_id: str = "current",
    approved: bool = True,
    reason: str = "",
) -> dict[str, Any]:
    """Approve or decline a pending permission request after describing it in an earlier turn.

    Args:
        thread_id: Thread ID or 'current'.
        approved: True if approved, False if declined.
        reason: Optional reason when declined.
    """
    registry = get_thread_registry()
    target = _resolve_target_id(thread_id, require_gate=True)
    try:
        before = await registry.require(target)
        if (
            before.state is not ThreadState.AWAITING_PERMISSION
            or not before.permission
        ):
            if not approved:
                return {
                    "ok": False,
                    "message": (
                        f"Nothing to decline: {before.thread_id} is {state_phrase(before)}, "
                        f"not waiting on a permission request. Next: {next_step(before)}"
                    ),
                }
            return {
                "ok": False,
                "message": (
                    f"Nothing approved: {before.thread_id} is {state_phrase(before)}, "
                    f"not waiting on a permission request. Next: {next_step(before)}"
                ),
            }
        if not approved:
            return await message_thread(
                target,
                reason.strip()
                or "Decline that action and continue without it.",
            )
        since, permission = before.last_seq, before.permission
        handle = await registry.approve(target)
    except ThreadError as exc:
        return {"ok": False, "message": str(exc)}
    message = (
        f"Approved `{permission.command}`. {thread_name(handle)} is continuing."
    )
    return _reply(handle, message, since)


async def cancel_thread(thread_id: str = "current") -> str:
    """Stop a thread entirely (keeps its files and branch; use message_thread to decline one step).

    Args:
        thread_id: Thread ID or 'current'.
    """
    target = _resolve_target_id(thread_id)
    try:
        handle = await get_thread_registry().cancel(target)
    except ThreadError as exc:
        return str(exc)
    if not is_code(handle.project):
        return f"Cancelled {thread_name(handle)}. Its work so far is kept."
    return (
        f"Cancelled {thread_name(handle)}. Its branch {handle.branch} is kept."
    )


async def read_thread(thread_id: str = "current") -> str:
    """Read a thread's state, pending permission request, progress, result, or changed files.

    Args:
        thread_id: Thread ID or 'current'.
    """
    target = _resolve_target_id(thread_id)
    try:
        handle = await get_thread_registry().require(target)
    except ThreadError as exc:
        return str(exc)
    branch = f", branch {handle.branch}" if is_code(handle.project) else ""
    parts = [
        f"{thread_ref(handle)}, {_harness_name(handle)}{branch}, "
        f"round {handle.iteration}: {state_phrase(handle)}."
    ]
    if handle.state in RUNNING_STATES:
        if updates := handle.recent_updates():
            parts.append(
                f"Latest: {updates[-1]}"
                if len(updates) == 1
                else f"Preliminary progress so far (still {state_phrase(handle)}): "
                + "; ".join(updates)
                + f". Latest: {updates[-1]}"
            )
    if handle.permission:
        parts.append(
            f"Waiting for {permission_phrase(handle.permission)} "
            f"({action_label(handle.permission.action_class)})."
        )
    if handle.state is ThreadState.COMPLETED:
        parts.extend(_outcome_parts(handle))
    if handle.error:
        parts.append(f"Error: {handle.error}")
    parts.append(f"Next: {next_step(handle)}")
    return " ".join(p for p in parts if p)


async def share_context(
    from_thread_id: str,
    to_target: str = "",
    instruction: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Hand over a thread's prompts, answers, changed files, and branch into another thread or folder.

    Args:
        from_thread_id: Source thread ID.
        to_target: Target thread ID to message, or folder ('code/<repo>', 'projects/<name>', or empty for source's folder) to start a new thread in.
        instruction: What the target thread should do with the shared context.
    """
    registry = get_thread_registry()
    session = _session(tool_context)
    source_id = _resolve_target_id(from_thread_id)
    try:
        target = to_target.strip()
        existing = await registry.get(target) if target else None
        since = existing.last_seq if existing is not None else 0
        handle = await registry.share_context(
            source_id,
            to_target,
            instruction,
            origin_session_id=session.id if session is not None else None,
        )
    except ThreadError as exc:
        return {"message": f"Could not share context: {exc}"}
    return _reply(
        handle,
        f"Shared context from {source_id} into {thread_name(handle)} ({handle.thread_id}){_where(handle)}. "
        "It is working on it now.",
        since_seq=since,
    )


async def publish_thread(thread_id: str = "current") -> str:
    """Push a code thread's branch to origin on GitHub. Ask first; call only in a later turn, after the user says yes.

    Args:
        thread_id: Thread ID or 'current'.
    """
    target = _resolve_target_id(thread_id)
    try:
        handle = await get_thread_registry().publish(target)
    except ThreadError as exc:
        return f"Nothing pushed: {exc}"
    return f"Pushed {thread_name(handle)}'s branch {handle.branch} to origin."


async def name_project(project: str, name: str) -> str:
    """Keep a 'scratch/<name>' one-off as a 'projects/<slug>' project, or rename a project, when the user asks.

    Args:
        project: 'scratch/<name>' to keep a one-off, or 'projects/<name>' / 'code/<repo>' to rename.
        name: Short display name, 2 to 4 words, in the user's words ('Europe trip').
    """
    from app.projects import name_project as name_it

    try:
        named = await name_it(project, name)
    except ValueError as exc:
        return f"Not named: {exc}"
    if named != project:
        return f"Kept as the project {name!r}, now in {named}. Use {named} for its threads."
    if named.startswith("scratch/"):
        return (
            f"Kept as the project {name!r}. A thread is still working in {named}, so its "
            f"folder moves once that finishes; until then, use {named} for its threads."
        )
    return f"Named {named} {name!r}."


async def list_threads(
    query: str = "",
    conversation: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """List threads with their folder and state, optionally filtered by keywords or conversation.

    Args:
        query: Optional search keywords matching title, project, goal, or result.
        conversation: Optional: 'this' for this session, 'previous' for the prior session.
    """
    words = [w for w in query.lower().split() if len(w) > 3]
    registry = get_thread_registry()
    by_conversation = conversation.strip().lower() in ("this", "previous")
    if by_conversation:
        session = _session(tool_context)
        current = session.id if session is not None else None
        origin = (
            current
            if conversation.strip().lower() == "this"
            else await registry.latest_origin_session(exclude=current)
        )
        candidates = (
            (await registry.query(origin_session_id=origin, limit=50))[0]
            if origin
            else []
        )
    else:
        candidates = await registry.list_all(limit=200 if words else 20)
    handles = [
        h
        for h in candidates
        if not words
        or any(
            w in f"{h.title} {h.project} {h.goal} {h.result_text}".lower()
            for w in words
        )
    ]
    if not handles and by_conversation:
        return "No threads were started in that conversation."
    if not handles:
        return (
            f"No threads match '{query}'." if words else "There are no threads."
        )
    waiting = [h for h in handles if h.state in GATE_STATES]
    running = [h for h in handles if h.state in RUNNING_STATES]
    finished = [h for h in handles if h not in waiting and h not in running]
    groups = [
        ("Waiting on you", waiting, state_phrase),
        (
            "Running",
            running,
            lambda h: h.latest_update or state_phrase(h),
        ),
        (
            "Recent",
            finished[:_RECENT_LIMIT],
            lambda h: (
                f"{h.state.value}: {first_line(h.result_text or h.error or '')}"
            ),
        ),
    ]
    return " ".join(
        f"{title}: "
        + "; ".join(f"{thread_ref(h)}, {short(detail(h), 120)}" for h in items)
        + "."
        for title, items, detail in groups
        if items
    )


async def list_projects() -> str:
    """List repositories ('code/<repo>'), projects ('projects/<name>'), and recent scratch folders."""
    try:
        env = (await resolve_environment()).env
        listed = [
            (kind, label, await project_folders(env, kind))
            for kind, label in _PROJECT_KINDS.items()
        ]
    except RuntimeError:
        logger.warning("Could not list project folders", exc_info=True)
        return "Couldn't read the folders on the user's computer right now."
    groups = [
        f"{label}: "
        + ", ".join(
            f"{kind}/{e.name} ({e.mtime[:10]})"
            if e.mtime
            else f"{kind}/{e.name}"
            for e in entries
        )
        + "."
        for kind, label, entries in listed
        if entries
    ]
    return " ".join(groups) or "There are no project folders yet."


async def project_folders(
    env: Environment, kind: str, limit: int | None = _PROJECTS_PER_KIND
) -> list[FileEntry]:
    try:
        entries = await env.list_files(kind)
    except FileNotFoundError:
        return []
    folders = [
        e for e in entries if e.type == "dir" and not e.name.startswith(".")
    ]
    folders.sort(key=lambda e: (e.mtime, e.name), reverse=True)
    return folders[:limit]


def _reply(
    handle: ThreadHandle, message: str, since_seq: int
) -> dict[str, Any]:
    return {
        "ok": True,
        "message": message,
        "thread_id": handle.thread_id,
        "state": handle.state.value,
        "status": handle.status,
        SINCE_SEQ_KEY: since_seq,
    }


def _session(tool_context: ToolContext | None) -> Any:
    try:
        return tool_context.session if tool_context is not None else None
    except (AttributeError, ValueError):
        return None


def _where(handle: ThreadHandle) -> str:
    return f" in {handle.project}"


def _harness_name(handle: ThreadHandle) -> str:
    return get_harness_registry().display_name(handle.harness)


def _outcome_parts(handle: ThreadHandle) -> list[str]:
    parts = [f"Outcome: {_clip(handle.result_text or '', _VOICE_TEXT_BUDGET)}"]
    if handle.files_changed:
        if is_code(handle.project):
            pushed = (
                f"pushed to {handle.branch}"
                if handle.pushed
                else f"on branch {handle.branch}, not pushed"
            )
            parts.append(f"{handle.diff_summary} ({pushed}).")
        else:
            parts.append(f"{handle.diff_summary}.")
        parts.append(
            f"Files, in the folder {handle.project}: "
            f"{', '.join(handle.files_changed[:10])}."
        )
        if handle.raw_diff:
            parts.append(
                f"Diff excerpt:\n{_clip(handle.raw_diff, _VOICE_DIFF_BUDGET)}"
            )
    else:
        parts.append("No files changed.")
    if handle.artifacts:
        titles = ", ".join(a["title"] for a in handle.artifacts)
        parts.append(f"Documents on screen: {titles}.")
    return parts


def _clip(text: str, budget: int) -> str:
    # Diffs keep their line structure; prose is collapsed to one line.
    text = " ".join(text.split()) if "\n@@" not in text else text.strip()
    if len(text) <= budget:
        return text
    return (
        text[:budget].rstrip()
        + f"... [{len(text) - budget} more characters on screen]"
    )
