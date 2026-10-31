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

"""Thread registry: dispatches, steers, approves, cancels, and persists per-user threads.

Every state change and event goes through `transition` / `emit`. Reads and actions
are scoped to `acting_user_id()`, and each thread's driver runs as its owner.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from app.auth.identity import acting_user_id, run_as
from app.context_cards import MAX_CARD_TEXT, card_items
from app.memory import episodes
from app.store.thread_store import ThreadStore
from app.threads import documents
from app.threads.describe import action_label, output_line
from app.threads.driver import drive, publish_branch, stop_orphaned_runs
from app.threads.model import (
    FINISHED_STATES,
    MILESTONE_KINDS,
    NARRATION_KINDS,
    RUNNING_STATES,
    TRANSITIONS,
    Directive,
    EventKind,
    Instruction,
    InstructionKind,
    InvalidTransition,
    MessageKind,
    PermissionRequest,
    ThreadEvent,
    ThreadHandle,
    ThreadState,
    Turn,
    new_thread_id,
)
from app.threads.prompts import share_context_block
from app.threads.titles import thread_title
from app.user_settings import load_user_settings, user_default_harness
from app.workers.harnesses.base import CodingHarness
from app.workers.harnesses.registry import get_harness_registry
from app.workers.workspace import is_code, resolve_project

logger = logging.getLogger(__name__)

Listener = Callable[[ThreadHandle, ThreadEvent], None]

MAX_RUNNING_THREADS = 20
DEFAULT_MAX_RUNNING_THREADS_PER_USER = 3
DUPLICATE_WINDOW_S = 120.0
_STOP_TIMEOUT_S = 15.0
_SETTLE_TIMEOUT_S = 5.0
_BRIEFING_KINDS = sorted(
    k.value for k in MILESTONE_KINDS - {EventKind.CANCELLED}
)
_ROUND_END_KINDS = frozenset(
    {EventKind.COMPLETED, EventKind.ERROR, EventKind.CANCELLED}
)

MAX_THREAD_CARDS = 3


class ThreadError(ValueError):
    """A user-facing error for a request the thread API cannot honor."""


class ThreadNotFound(ThreadError):
    pass


class ThreadInputError(ThreadError):
    """Invalid request input (empty goal, unknown harness or project)."""


class ThreadRegistry:
    def __init__(self, store: ThreadStore | None = None) -> None:
        self._store = store
        self._threads: dict[str, ThreadHandle] = {}
        self._listeners: list[tuple[str | None, Listener]] = []
        self._lock = asyncio.Lock()
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        self.orphan_cleanup: asyncio.Task[None] | None = None
        self._grants: dict[str, set[str]] = {}

    # ---- thread API -------------------------------------------------------

    def dispatch_lock(self) -> asyncio.Lock:
        """The acting user's lock around a duplicate check and the dispatch it guards."""
        return self._dispatch_locks.setdefault(acting_user_id(), asyncio.Lock())

    async def dispatch(
        self,
        goal: str,
        project: str = "",
        *,
        harness: str | None = None,
        title: str = "",
        origin_session_id: str | None = None,
        context: str = "",
    ) -> ThreadHandle:
        """Start a new thread in `project` and execute `goal` right away."""
        goal = goal.strip()
        if not goal:
            raise ThreadInputError("A thread needs a goal.")
        if harness is None:
            # The dispatching user's default harness, else the deployment's.
            await load_user_settings()
            harness_impl = user_default_harness()
        else:
            harness_impl = _resolve_harness(harness)
        project = _resolve_project(project, thread_title(title, goal))
        owner = acting_user_id()
        async with self._lock:
            running = [t for t in self._threads.values() if t.is_running]
            mine = sum(1 for t in running if t.owner == owner)
            per_user = max_running_threads_per_user()
            if mine >= per_user:
                raise ThreadError(
                    f"You already have {_threads(mine)} running, the most one person can run at once. "
                    "Cancel one or wait for one to finish."
                )
            if len(running) >= MAX_RUNNING_THREADS:
                raise ThreadError(
                    f"{len(running)} threads are already running. Cancel one or wait for one to finish."
                )
            thread_id = new_thread_id()
            while thread_id in self._threads or (
                self._store is not None
                and await self._store.load_thread(thread_id) is not None
            ):
                thread_id = new_thread_id()
            handle = ThreadHandle(
                thread_id=thread_id,
                goal=goal,
                title=title,
                project=project,
                harness=harness_impl.name,
                branch=f"agent/{thread_id}",
                owner=owner,
                state=ThreadState.EXECUTING,
                instructions=[
                    Instruction(
                        InstructionKind.GOAL,
                        goal,
                        1,
                        session_id=origin_session_id,
                        context=context,
                    )
                ],
                origin_session_id=origin_session_id,
                turns=[Turn(seq=1, prompt=goal)],
            )
            self._threads[thread_id] = handle
        await self.emit(
            handle,
            EventKind.STATE_CHANGED,
            f"created → {handle.state.value}",
            {"to": handle.state.value},
        )
        self._start(handle, Directive(MessageKind.START, context=context))
        return handle

    async def steer(
        self,
        thread_id: str,
        instruction: str,
        *,
        harness: str | None = None,
        session_id: str | None = None,
        context: str = "",
    ) -> ThreadHandle:
        """Send a message or redirect to an existing thread."""
        instruction = instruction.strip()
        if not instruction:
            raise ThreadInputError("Steering needs an instruction.")
        target = _resolve_harness(harness) if harness else None
        handle = await self.require(thread_id)
        async with handle.lock:
            await self._settle(handle)
            # Stop the current run first: a driver that outlives the stop timeout would
            # otherwise share the clone with the one this steer starts.
            if handle.state in RUNNING_STATES and not await self._stop(handle):
                raise ThreadError(
                    f"Still stopping the previous run of {thread_id}; try again in a moment."
                )
            if target is not None and target.name != handle.harness:
                previous = handle.harness
                handle.harness = target.name
                await self.emit(
                    handle,
                    EventKind.PROGRESS,
                    f"Switched from {previous} to {target.display_name}.",
                )
            kind, directive = self._steer_directive(handle, instruction)
            directive.context = context
            handle.instructions.append(
                Instruction(
                    kind,
                    instruction,
                    handle.iteration,
                    session_id=session_id,
                    context=context,
                )
            )
            handle.dismissed = False
            await self._resume(handle, directive)
        return handle

    async def share_context(
        self,
        from_thread_id: str,
        to_target: str = "",
        instruction: str = "",
        *,
        origin_session_id: str | None = None,
    ) -> ThreadHandle:
        """Explicitly share context from `from_thread_id` into another thread or project."""
        source = await self.require(from_thread_id)
        target_str = to_target.strip() or source.project
        if not target_str:
            raise ThreadInputError("Specify a target thread ID or project.")
        target_handle = await self.get(target_str)
        if target_handle is not None:
            if target_handle.thread_id == source.thread_id:
                raise ThreadInputError(
                    "Choose a different target thread to share context into."
                )
            context = share_context_block(source)
            prompt = (
                instruction.strip()
                or f'Continue using the shared context from "{source.title}".'
            )
            return await self.steer(
                target_handle.thread_id,
                prompt,
                session_id=origin_session_id,
                context=context,
            )
        if target_str.lower().startswith("thread-"):
            raise ThreadNotFound(f"No thread with ID {target_str}.")
        context = share_context_block(source)
        prompt = (
            instruction.strip()
            or f'Continue from "{source.title}": {source.goal}'
        )
        return await self.dispatch(
            prompt,
            project=target_str,
            origin_session_id=origin_session_id,
            context=context,
        )

    async def approve(
        self,
        thread_id: str,
        *,
        permission_key: str | None = None,
    ) -> ThreadHandle:
        """Approve the thread's pending permission request."""
        handle = await self.require(thread_id)
        async with handle.lock:
            await self._settle(handle)
            state = handle.state
            if permission_key is not None and (
                state is not ThreadState.AWAITING_PERMISSION
                or handle.permission is None
                or handle.permission.key != permission_key
            ):
                raise ThreadError(
                    f"What {handle.thread_id} is waiting on changed; review it again."
                )
            if (
                state is ThreadState.AWAITING_PERMISSION
                and handle.permission is None
            ):
                raise ThreadError(
                    f"{handle.thread_id} has no request open to approve; "
                    "check what happened, then steer or cancel it."
                )
            if (
                state is not ThreadState.AWAITING_PERMISSION
                or handle.permission is None
            ):
                raise ThreadError(
                    f"{handle.thread_id} has nothing waiting for approval (it is {state.value})."
                )
            directive = Directive(
                MessageKind.PERMISSION_GRANTED,
                permission=handle.permission,
            )
            handle.permission = None
            await self.save(handle)
            await self._resume(handle, directive)
        return handle

    async def request_permission(
        self, thread_id: str, permission: PermissionRequest
    ) -> bool:
        handle = await self.require(thread_id)
        async with handle.lock:
            grants = self._grants.get(handle.thread_id, set())
            if permission.key in grants:
                grants.discard(permission.key)
                return True
            if handle.state is ThreadState.EXECUTING:
                await self.pause_for_permission(handle, permission)
            return False

    def grant_once(self, thread_id: str, key: str) -> None:
        self._grants.setdefault(thread_id.strip().lower(), set()).add(key)

    async def pause_for_permission(
        self, handle: ThreadHandle, permission: PermissionRequest
    ) -> None:
        if handle.state is ThreadState.CANCELLED:
            return
        handle.permission = permission
        await self.transition(handle, ThreadState.AWAITING_PERMISSION)
        await self.emit(
            handle,
            EventKind.PERMISSION_REQUESTED,
            f"Wants to run `{permission.command}` ({action_label(permission.action_class)}).",
            permission.to_dict(),
        )

    async def record_finding(self, thread_id: str, text: str) -> None:
        handle = await self._running(thread_id)
        await self.emit(handle, EventKind.PROGRESS, text, {"finding": True})

    async def save_to_drive(
        self, thread_id: str, path: str, *, title: str = ""
    ) -> dict[str, str]:
        handle = await self._running(thread_id)
        saved = await documents.save_to_drive(handle, path, title=title)
        await self._add_output(handle, saved)
        return saved

    async def record_output(
        self, thread_id: str, output: dict[str, str]
    ) -> None:
        await self._add_output(await self._running(thread_id), output)

    async def _add_output(
        self, handle: ThreadHandle, output: dict[str, str]
    ) -> None:
        await self.emit(
            handle, EventKind.PROGRESS, output_line(output), {"output": output}
        )
        handle.sync_active_turn()
        await self.save(handle)

    async def present_card(
        self, thread_id: str, kind: str, title: str, items: Any
    ) -> str:
        """Attach an answer card (`events`, `threads`, or `files`) to a running thread's turn."""
        handle = await self._running(thread_id)
        normalized = card_items(kind, items)
        if not normalized:
            raise ValueError(
                f"No {kind} to show: pass the items exactly as the Google tools "
                "returned them, each with its id."
            )
        async with handle.lock:
            if len(handle.cards) >= MAX_THREAD_CARDS:
                raise ValueError(
                    f"This thread already shows {MAX_THREAD_CARDS} cards, the most it can."
                )
            handle.cards.append(
                {
                    "kind": kind,
                    "title": title[:MAX_CARD_TEXT],
                    "items": normalized,
                }
            )
            handle.sync_active_turn()
        await self.save(handle)
        noun = kind if len(normalized) != 1 else kind.removesuffix("s")
        shown = f"Card shown: {len(normalized)} {noun}."
        await self.emit(handle, EventKind.PROGRESS, shown, {"card": kind})
        return shown

    async def _running(self, thread_id: str) -> ThreadHandle:
        handle = await self.require(thread_id)
        if handle.state not in RUNNING_STATES:
            raise ThreadError(
                f"{handle.thread_id} is {handle.state.value}, not running."
            )
        return handle

    # ---- publishing a code thread's work -------------------------------------

    async def publish(self, thread_id: str) -> ThreadHandle:
        """Push a code thread's branch to `origin` (the user asked to). Raises `ThreadError`
        when the thread has no branch, is still running, or the push failed."""
        handle = await self.require(thread_id)
        if not is_code(handle.project):
            raise ThreadInputError(
                f"{handle.thread_id} works in {handle.project}, which has no branch to push."
            )
        async with handle.lock:
            if handle.state in RUNNING_STATES:
                raise ThreadError(
                    f"{handle.thread_id} is still {handle.state.value}; "
                    "push its branch once it stops."
                )
            pushed, problem = await publish_branch(handle)
            if not pushed:
                raise ThreadError(problem or "The push failed.")
            handle.pushed = True
            handle.sync_active_turn()
            await self.emit(
                handle,
                EventKind.PROGRESS,
                f"Pushed {handle.branch} to origin.",
                {"pushed": True, "branch": handle.branch},
            )
            await self.save(handle)
        return handle

    async def cancel(self, thread_id: str) -> ThreadHandle:
        """Stop any running harness and mark the thread cancelled. Its files and branch are kept."""
        handle = await self.require(thread_id)
        async with handle.lock:
            if handle.state in FINISHED_STATES:
                raise ThreadError(
                    f"{handle.thread_id} already {handle.state.value}."
                )
            if not await self._stop(handle):
                raise ThreadError(
                    f"Still stopping the previous run of {handle.thread_id}; try again in a moment."
                )
            handle.permission = None
            await self.transition(handle, ThreadState.CANCELLED)
            handle.sync_active_turn()
            await self.emit(
                handle,
                EventKind.CANCELLED,
                "Cancelled. Its files and branch are kept.",
            )
        return handle

    async def dismiss(self, thread_id: str) -> ThreadHandle:
        handle = await self.require(thread_id)
        handle.dismissed = True
        await self.save(handle)
        return handle

    async def get(self, thread_id: str) -> ThreadHandle | None:
        """The acting user's thread with this ID; None if there is none or it is someone else's."""
        handle = await self._load(thread_id)
        if handle is None or handle.owner != acting_user_id():
            return None
        return handle

    async def _load(self, thread_id: str) -> ThreadHandle | None:
        """The thread with this ID, whoever owns it (from memory, else the store)."""
        key = thread_id.strip().lower()
        if key in self._threads:
            return self._threads[key]
        if self._store is None:
            return None
        loaded = await self._store.load_thread(key)
        if loaded is None:
            return None
        snapshot, events = loaded
        try:
            handle = ThreadHandle.from_dict(
                snapshot, [ThreadEvent.from_dict(e) for e in events]
            )
        except (KeyError, TypeError, ValueError):
            # One unreadable record (e.g. from an older schema) must not break listing.
            logger.warning(
                "Skipping unreadable stored thread %s", key, exc_info=True
            )
            return None
        if handle.events:
            handle.last_seq = max(handle.last_seq, handle.events[-1].seq)
        async with self._lock:
            return self._threads.setdefault(key, handle)

    async def require(self, thread_id: str) -> ThreadHandle:
        handle = await self.get(thread_id)
        if handle is None:
            raise ThreadNotFound(f"No thread with ID {thread_id}.")
        return handle

    async def list_all(self, limit: int = 50) -> list[ThreadHandle]:
        """The acting user's recent threads, newest first, including ones from earlier server runs."""
        if self._store is not None:
            for thread_id in await self._store.recent_thread_ids(
                acting_user_id(), limit
            ):
                if thread_id not in self._threads:
                    await self._load(thread_id)
        return self.snapshot()[:limit]

    async def query(
        self,
        *,
        q: str = "",
        before: str | None = None,
        limit: int = 30,
        states: list[ThreadState] | None = None,
        origin_session_id: str | None = None,
    ) -> tuple[list[ThreadHandle], str | None]:
        """A page of the acting user's thread history from the store, most recently
        ended (or updated) first, with the cursor for the next page."""
        names = [s.value for s in states] if states else None
        if self._store is None:
            in_memory = self.snapshot()
            if origin_session_id:
                in_memory = [
                    t
                    for t in in_memory
                    if t.origin_session_id == origin_session_id
                ]
            return _query_in_memory(in_memory, q, before, limit, names)
        try:
            snapshots, next_before = await self._store.query_threads(
                acting_user_id(),
                q=q,
                before=before,
                limit=limit,
                states=names,
                origin_session_id=origin_session_id,
            )
        except ValueError as exc:
            raise ThreadInputError(str(exc)) from exc
        handles: list[ThreadHandle] = []
        for snapshot in snapshots:
            live = self._threads.get(snapshot["thread_id"])
            if live is not None:
                handles.append(live)
                continue
            try:
                handles.append(ThreadHandle.from_dict(snapshot))
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Skipping unreadable stored thread %s",
                    snapshot.get("thread_id"),
                    exc_info=True,
                )
        return handles, next_before

    async def purge_finished_before(self, cutoff: float) -> int:
        """Delete every user's threads that finished before `cutoff` (epoch seconds),
        with their history: the retention cleanup. Returns how many were deleted."""
        if self._store is None:
            expired = [
                (t.thread_id, t.owner)
                for t in self._threads.values()
                if t.state in FINISHED_STATES
                and t.ended_at is not None
                and t.ended_at < cutoff
            ]
        else:
            expired = await self._store.delete_finished_before(
                cutoff, [s.value for s in FINISHED_STATES]
            )
        async with self._lock:
            for thread_id, _ in expired:
                handle = self._threads.get(thread_id)
                # Steered back to life since the query: keep it in memory.
                if handle is not None and handle.state in FINISHED_STATES:
                    del self._threads[thread_id]
        by_owner: dict[str, list[str]] = {}
        for thread_id, owner in expired:
            by_owner.setdefault(owner, []).append(thread_id)
        for owner, thread_ids in by_owner.items():
            # What ADK Voice Agent remembers of a thread goes with it.
            episodes.forget_threads(owner, thread_ids)
        return len(expired)

    def snapshot(self) -> list[ThreadHandle]:
        """The acting user's threads already in memory, newest first (no store access)."""
        owner = acting_user_id()
        return sorted(
            (t for t in self._threads.values() if t.owner == owner),
            key=lambda t: t.created_at,
            reverse=True,
        )

    def recent_duplicate(
        self, goal: str, project: str | None
    ) -> ThreadHandle | None:
        """The acting user's unfinished thread started in the last `DUPLICATE_WINDOW_S` for
        the same goal (case and spacing aside) and project (any project for None)."""
        words = " ".join(goal.lower().split())
        cutoff = time.time() - DUPLICATE_WINDOW_S
        return next(
            (
                h
                for h in self.snapshot()
                if h.created_at >= cutoff
                and h.state not in FINISHED_STATES
                and " ".join(h.goal.lower().split()) == words
                and project in (None, h.project)
            ),
            None,
        )

    async def recover_interrupted(self) -> int:
        """Call once at startup: threads a previous process left mid-run can't be resumed, so mark them failed."""
        if self._store is None:
            return 0
        interrupted = await self._store.fail_interrupted_threads(
            [s.value for s in RUNNING_STATES]
        )
        if interrupted:
            by_owner: dict[str, list[str]] = {}
            for thread_id, owner in interrupted:
                by_owner.setdefault(owner, []).append(thread_id)
            self.orphan_cleanup = asyncio.create_task(
                _stop_orphans(by_owner), name="stop-orphaned-runs"
            )
        return len(interrupted)

    async def unseen_milestones(self) -> list[dict[str, Any]]:
        """The acting user's milestones not yet delivered to them; marks them seen."""
        if self._store is None:
            return []
        owner = acting_user_id()
        events = await self._store.unseen_events(_BRIEFING_KINDS, owner)
        if events:
            await self._store.advance_cursor(
                max(e["id"] for e in events), owner
            )
        return events

    async def mark_seen(self, delivered: dict[str, int] | None = None) -> None:
        if self._store is not None:
            await self._store.mark_all_seen(acting_user_id(), delivered)

    async def delete(self, thread_id: str) -> None:
        """Stop the acting user's thread if it is still running, then delete it and its
        history. Raises `ThreadError` when they have no such thread."""
        handle = await self.require(thread_id)
        async with self._lock:
            self._threads.pop(handle.thread_id, None)
        await self._stop(handle)
        if self._store is not None:
            await self._store.delete_thread(handle.thread_id, handle.owner)
        episodes.forget_threads(handle.owner, [handle.thread_id])

    async def delete_threads(self, thread_ids: Sequence[str]) -> int:
        """Stop and delete multiple threads for the acting user."""
        count = 0
        for thread_id in thread_ids:
            await self.delete(thread_id)
            count += 1
        return count

    async def clear_all(self) -> int:
        """Stop and delete the acting user's threads and ADK sessions; other users' are untouched."""
        from app.app_utils.services import clear_user_sessions

        owner = acting_user_id()
        async with self._lock:
            handles = [t for t in self._threads.values() if t.owner == owner]
            for handle in handles:
                del self._threads[handle.thread_id]
        for handle in handles:
            await self._stop(handle)
        episodes.forget_all_threads(owner)
        try:
            await clear_user_sessions(owner)
        except Exception:
            logger.warning(
                "Could not clear ADK sessions for %s", owner, exc_info=True
            )
        return (
            await self._store.clear_threads(owner)
            if self._store is not None
            else len(handles)
        )

    def add_listener(
        self, listener: Listener, *, owner: str | None = None
    ) -> None:
        """Call `listener` on every event of `owner`'s threads (every user's when None)."""
        if (owner, listener) not in self._listeners:
            self._listeners.append((owner, listener))

    def remove_listener(self, listener: Listener) -> None:
        self._listeners = [
            entry for entry in self._listeners if entry[1] is not listener
        ]

    # ---- state + events (used by the driver) ------------------------------

    async def transition(
        self, handle: ThreadHandle, state: ThreadState
    ) -> None:
        previous = handle.state
        if previous is state:
            return
        if state not in TRANSITIONS[previous]:
            raise InvalidTransition(
                f"{handle.thread_id}: {previous.value} → {state.value}"
            )
        handle.state = state
        handle.ended_at = time.time() if state in FINISHED_STATES else None
        handle.sync_active_turn()
        await self.emit(
            handle,
            EventKind.STATE_CHANGED,
            f"{previous.value} → {state.value}",
            {"from": previous.value, "to": state.value},
        )

    async def emit(
        self,
        handle: ThreadHandle,
        kind: EventKind,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> ThreadEvent:
        event = handle.record(kind, message, data)
        if self._store is not None:
            try:
                await self._store.append_event(
                    handle.thread_id, event.to_dict()
                )
                if kind not in NARRATION_KINDS:
                    await self._store.save_thread(handle.to_dict())
            except Exception:
                # The in-memory handle stays authoritative; a lost write must not stop the thread.
                logger.exception(
                    "Could not persist event %s for %s", kind, handle.thread_id
                )
        async with handle.changed:
            handle.changed.notify_all()
        for owner, listener in list(self._listeners):
            if owner is not None and owner != handle.owner:
                continue
            try:
                listener(handle, event)
            except Exception:
                logger.exception("Thread listener failed")
        if kind in _ROUND_END_KINDS:
            # The round is over: what ADK Voice Agent remembers of the thread is rewritten.
            episodes.remember_thread(handle)
        return event

    async def save(self, handle: ThreadHandle) -> None:
        handle.updated_at = time.time()
        if self._store is not None:
            try:
                await self._store.save_thread(handle.to_dict())
            except Exception:
                logger.exception("Could not persist %s", handle.thread_id)

    async def fail(self, handle: ThreadHandle, message: str) -> None:
        if handle.state is ThreadState.CANCELLED:
            return
        handle.error = message
        await self.transition(handle, ThreadState.FAILED)
        handle.sync_active_turn()
        await self.emit(handle, EventKind.ERROR, message)

    async def complete(
        self, handle: ThreadHandle, data: dict[str, Any]
    ) -> None:
        if handle.state is ThreadState.CANCELLED:
            return
        handle.error = None
        await self.transition(handle, ThreadState.COMPLETED)
        handle.sync_active_turn()
        message = (handle.result_text or "").strip()[
            :600
        ] or f"Finished {handle.title}."
        await self.emit(
            handle,
            EventKind.COMPLETED,
            message,
            {**data, "summary": handle.summary} if handle.summary else data,
        )

    # ---- internals -------------------------------------------------------

    async def latest_origin_session(
        self, *, exclude: str | None = None
    ) -> str | None:
        """The voice conversation the acting user last dispatched a thread from, other than
        `exclude`; None if there is none."""
        owner = acting_user_id()
        if self._store is not None:
            return await self._store.latest_origin_session(
                owner, exclude=exclude
            )
        return next(
            (
                t.origin_session_id
                for t in self.snapshot()
                if t.origin_session_id and t.origin_session_id != exclude
            ),
            None,
        )

    @staticmethod
    def _steer_directive(
        handle: ThreadHandle, instruction: str
    ) -> tuple[InstructionKind, Directive]:
        state = handle.state
        if state in RUNNING_STATES:
            if handle.turns and handle.turns[-1].outcome == "running":
                handle.turns[-1].outcome = "cancelled"
                handle.turns[-1].ended_at = time.time()
            handle.iteration = len(handle.turns) + 1
            handle.result_text = ""
            handle.summary = ""
            handle.error = None
            handle.cards = []
            handle.turns.append(Turn(seq=handle.iteration, prompt=instruction))
            return InstructionKind.REDIRECT, Directive(
                MessageKind.REDIRECT, instruction
            )
        if state is ThreadState.AWAITING_PERMISSION:
            directive = Directive(
                MessageKind.PERMISSION_DENIED,
                instruction,
                permission=handle.permission,
            )
            handle.permission = None
            return InstructionKind.DENIAL, directive
        # Finished: the instruction starts the next turn.
        handle.iteration = max(handle.iteration + 1, len(handle.turns) + 1)
        handle.permission = None
        handle.error = None
        handle.result_text = ""
        handle.summary = ""
        handle.cards = []
        handle.files_changed = []
        handle.diff_summary = ""
        handle.raw_diff = ""
        for t in handle.turns:
            t.raw_diff = ""
        handle.artifacts = []
        handle.turns.append(Turn(seq=handle.iteration, prompt=instruction))
        return InstructionKind.FOLLOWUP, Directive(
            MessageKind.FOLLOWUP, instruction
        )

    async def _resume(self, handle: ThreadHandle, directive: Directive) -> None:
        if not await self._stop(handle):
            raise ThreadError(
                f"Still stopping the previous run of {handle.thread_id}; try again in a moment."
            )
        await self.transition(handle, ThreadState.EXECUTING)
        self._start(handle, directive)

    def _start(self, handle: ThreadHandle, directive: Directive) -> None:
        # The driver, and every task and thread it starts, runs as the thread's owner,
        # whoever acted on the thread, so credential and sandbox lookups are theirs.
        with run_as(handle.owner):
            handle.driver = asyncio.create_task(
                drive(self, handle, directive),
                name=f"thread-driver-{handle.thread_id}",
            )

    async def _settle(self, handle: ThreadHandle) -> None:
        """Let a driver that just opened a gate finish announcing it before the gate is acted on."""
        driver = handle.driver
        if (
            handle.state not in RUNNING_STATES
            and driver is not None
            and not driver.done()
        ):
            await asyncio.wait({driver}, timeout=_SETTLE_TIMEOUT_S)

    async def _stop(self, handle: ThreadHandle) -> bool:
        """Cancel the thread's driver; False if it is still running after the timeout."""
        driver = handle.driver
        handle.driver = None
        if driver is None or driver.done():
            return True
        driver.cancel()
        done, _ = await asyncio.wait({driver}, timeout=_STOP_TIMEOUT_S)
        if not done:
            # Keep the reference so a later stop can wait on it again.
            handle.driver = driver
            logger.warning(
                "Driver for %s did not stop within %ss",
                handle.thread_id,
                _STOP_TIMEOUT_S,
            )
        return bool(done)


def _query_in_memory(
    handles: list[ThreadHandle],
    q: str,
    before: str | None,
    limit: int,
    states: list[str] | None,
) -> tuple[list[ThreadHandle], str | None]:
    """`ThreadRegistry.query` over in-memory threads, for a registry with no store."""
    from app.store.thread_store import decode_cursor, encode_cursor

    def sort_key(t: ThreadHandle) -> tuple[float, str]:
        return (t.ended_at or t.updated_at, t.thread_id)

    needle = q.strip().lower()
    rows = sorted(
        (
            t
            for t in handles
            if (not states or t.state.value in states)
            and (not needle or needle in f"{t.title}\n{t.goal}".lower())
        ),
        key=sort_key,
        reverse=True,
    )
    if before:
        try:
            cursor = decode_cursor(before)
        except ValueError as exc:
            raise ThreadInputError(str(exc)) from exc
        rows = [t for t in rows if sort_key(t) < cursor]
    page = rows[:limit]
    more = len(rows) > limit and bool(page)
    return page, encode_cursor(*sort_key(page[-1])) if more else None


async def _stop_orphans(by_owner: dict[str, list[str]]) -> None:
    """Stop interrupted threads' runs, each owner's in their own environment."""
    for owner, thread_ids in by_owner.items():
        with run_as(owner):
            await stop_orphaned_runs(thread_ids)


def max_running_threads_per_user() -> int:
    """`MAX_RUNNING_THREADS_PER_USER`: how many threads one user can have running at once."""
    raw = (os.getenv("MAX_RUNNING_THREADS_PER_USER") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_RUNNING_THREADS_PER_USER
    except ValueError:
        logger.warning(
            "MAX_RUNNING_THREADS_PER_USER=%r is not a number; using %d",
            raw,
            DEFAULT_MAX_RUNNING_THREADS_PER_USER,
        )
        value = DEFAULT_MAX_RUNNING_THREADS_PER_USER
    return max(1, value)


def _threads(count: int) -> str:
    return "1 thread" if count == 1 else f"{count} threads"


def _resolve_harness(name: str | None) -> CodingHarness:
    try:
        return get_harness_registry().get(name)
    except ValueError as exc:
        raise ThreadInputError(str(exc)) from exc


def _resolve_project(name: str, title: str) -> str:
    try:
        return resolve_project(name, title=title)
    except ValueError as exc:
        raise ThreadInputError(str(exc)) from exc


_registry: ThreadRegistry | None = None


def get_thread_registry() -> ThreadRegistry:
    global _registry
    if _registry is None:
        from app.store.thread_store import get_thread_store

        _registry = ThreadRegistry(get_thread_store())
    return _registry


def set_thread_registry(registry: ThreadRegistry | None) -> None:
    """Replace the process-wide registry (tests and scripts); `None` resets to the default."""
    global _registry
    _registry = registry
