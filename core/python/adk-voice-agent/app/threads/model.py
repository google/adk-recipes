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

"""Thread state machine (`TRANSITIONS`), multi-turn history (`Turn`), events, and `ThreadHandle`."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.auth.identity import DEFAULT_DEV_USER_ID
from app.threads.titles import thread_title


def new_thread_id() -> str:
    return f"thread-{uuid.uuid4().hex[:16]}"


class ThreadState(StrEnum):
    EXECUTING = "executing"
    AWAITING_PERMISSION = "awaiting_permission"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUNNING_STATES = frozenset({ThreadState.EXECUTING})
GATE_STATES = frozenset({ThreadState.AWAITING_PERMISSION})
FINISHED_STATES = frozenset(
    {ThreadState.COMPLETED, ThreadState.FAILED, ThreadState.CANCELLED}
)

_STOPPED = frozenset({ThreadState.FAILED, ThreadState.CANCELLED})
TRANSITIONS: dict[ThreadState, frozenset[ThreadState]] = {
    ThreadState.EXECUTING: frozenset(
        {
            ThreadState.AWAITING_PERMISSION,
            ThreadState.COMPLETED,
        }
    )
    | _STOPPED,
    ThreadState.AWAITING_PERMISSION: frozenset(
        {
            ThreadState.EXECUTING,
            ThreadState.COMPLETED,
        }
    )
    | _STOPPED,
    ThreadState.COMPLETED: frozenset({ThreadState.EXECUTING}),
    ThreadState.FAILED: frozenset({ThreadState.EXECUTING}),
    ThreadState.CANCELLED: frozenset({ThreadState.EXECUTING}),
}


class InvalidTransition(RuntimeError):
    """A state change outside `TRANSITIONS`."""


class EventKind(StrEnum):
    STATE_CHANGED = "state_changed"
    PROGRESS = "progress"
    ACTIVITY = "activity"
    WARNING = "warning"
    PERMISSION_REQUESTED = "permission_requested"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


# High-frequency narration: logged, but not worth re-saving the snapshot for.
NARRATION_KINDS = frozenset({EventKind.PROGRESS, EventKind.ACTIVITY})
MILESTONE_KINDS = frozenset(
    {
        EventKind.PERMISSION_REQUESTED,
        EventKind.COMPLETED,
        EventKind.ERROR,
        EventKind.CANCELLED,
    }
)
_UPDATE_KINDS = (MILESTONE_KINDS - {EventKind.CANCELLED}) | {
    EventKind.PROGRESS,
    EventKind.ACTIVITY,
    EventKind.WARNING,
}


class MessageKind(StrEnum):
    START = "start"
    FOLLOWUP = "followup"
    REDIRECT = "redirect"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_DENIED = "permission_denied"


class InstructionKind(StrEnum):
    GOAL = "goal"
    REDIRECT = "redirect"
    DENIAL = "denial"
    FOLLOWUP = "followup"


@dataclass
class ThreadEvent:
    seq: int
    iteration: int
    kind: EventKind
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "iteration": self.iteration,
            "kind": self.kind.value,
            "message": self.message,
            "data": self.data,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadEvent:
        return cls(
            seq=int(data["seq"]),
            iteration=int(data["iteration"]),
            kind=EventKind(str(data["kind"])),
            message=str(data["message"]),
            data=dict(data.get("data") or {}),
            ts=float(data.get("ts") or time.time()),
        )


class PermissionKind(StrEnum):
    COMMAND = "command"  # a command waiting for user permission
    TOOL = "tool"  # a tool call (e.g. a gated connected-app call through the gateway)


@dataclass
class PermissionRequest:
    """Something waiting for the user's approval before it can run."""

    action_class: str
    command: str
    # Identifies the exact command; a grant for this key lets it run once.
    key: str
    kind: PermissionKind = PermissionKind.COMMAND
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_class": self.action_class,
            "command": self.command,
            "key": self.key,
            "kind": self.kind.value,
            "arguments": dict(self.arguments),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PermissionRequest | None:
        if not data or not data.get("key"):
            return None
        try:
            kind = PermissionKind(data.get("kind") or PermissionKind.COMMAND)
        except ValueError:
            kind = PermissionKind.COMMAND
        raw_args = data.get("arguments")
        return cls(
            action_class=str(data.get("action_class") or "unknown"),
            command=str(data.get("command") or ""),
            key=str(data["key"]),
            kind=kind,
            arguments=dict(raw_args) if isinstance(raw_args, dict) else {},
        )


@dataclass
class Instruction:
    kind: InstructionKind
    text: str
    iteration: int
    # The voice conversation (ADK session) the instruction came from; None from the REST API.
    session_id: str | None = None
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "text": self.text,
            "iteration": self.iteration,
        }
        if self.session_id:
            data["session_id"] = self.session_id
        if self.context:
            data["context"] = self.context
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Instruction:
        raw_kind = str(data.get("kind") or InstructionKind.FOLLOWUP)
        try:
            kind = InstructionKind(raw_kind)
        except ValueError:
            kind = InstructionKind.FOLLOWUP
        return cls(
            kind=kind,
            text=str(data["text"]),
            iteration=int(data["iteration"]),
            session_id=data.get("session_id") or None,
            context=str(data.get("context") or ""),
        )


@dataclass
class Directive:
    kind: MessageKind
    text: str | None = None
    permission: PermissionRequest | None = None
    context: str = ""


@dataclass
class Turn:
    seq: int
    prompt: str
    outcome: str = "running"  # "running" | "finished" | "failed" | "cancelled"
    answer: str = ""
    summary: str = ""
    error: str | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str = ""
    raw_diff: str = ""
    artifacts: list[dict[str, str]] = field(default_factory=list)
    outputs: list[dict[str, str]] = field(default_factory=list)
    commit_sha: str | None = None
    pushed: bool = False
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int((self.ended_at or time.time()) - self.started_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "prompt": self.prompt,
            "outcome": self.outcome,
            "answer": self.answer,
            "summary": self.summary,
            "error": self.error,
            "cards": list(self.cards),
            "files_changed": list(self.files_changed),
            "diff_summary": self.diff_summary,
            "artifacts": list(self.artifacts),
            "outputs": list(self.outputs),
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(
            seq=int(data.get("seq") or 1),
            prompt=str(data.get("prompt") or ""),
            outcome=str(data.get("outcome") or "finished"),
            answer=str(data.get("answer") or ""),
            summary=str(data.get("summary") or ""),
            error=data.get("error"),
            cards=[c for c in data.get("cards") or [] if isinstance(c, dict)],
            files_changed=list(data.get("files_changed") or []),
            diff_summary=str(data.get("diff_summary") or ""),
            raw_diff=str(data.get("raw_diff") or ""),
            artifacts=list(data.get("artifacts") or []),
            outputs=list(data.get("outputs") or []),
            commit_sha=data.get("commit_sha"),
            pushed=bool(data.get("pushed")),
            started_at=float(data.get("started_at") or time.time()),
            ended_at=data.get("ended_at"),
        )


def event_output(event: ThreadEvent) -> dict[str, str] | None:
    if event.kind is not EventKind.PROGRESS:
        return None
    made = event.data.get("output")
    return made if isinstance(made, dict) and made.get("url") else None


def _merge_outputs(
    saved: Iterable[dict[str, str]], events: Iterable[ThreadEvent]
) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    made = [o for o in saved if isinstance(o, dict) and o.get("url")]
    made += [o for e in events if (o := event_output(e))]
    for output in made:
        before = merged.get(output["url"], {})
        merged[output["url"]] = {
            **output,
            "title": output.get("title") or before.get("title", ""),
        }
    return list(merged.values())


EVENT_WINDOW = 200


@dataclass
class ThreadHandle:
    thread_id: str
    goal: str
    project: str
    harness: str
    branch: str
    owner: str = DEFAULT_DEV_USER_ID
    title: str = ""
    state: ThreadState = ThreadState.EXECUTING
    iteration: int = 1
    sessions: dict[str, str] = field(default_factory=dict)
    instructions: list[Instruction] = field(default_factory=list)
    permission: PermissionRequest | None = None
    result_text: str = ""
    summary: str = ""
    error: str | None = None
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str = ""
    raw_diff: str = ""
    artifacts: list[dict[str, str]] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)
    pushed: bool = False
    commit_sha: str | None = None
    base_sha: str | None = None
    workdir: str | None = None
    segment: int = 0
    dismissed: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    last_seq: int = 0
    events: list[ThreadEvent] = field(default_factory=list)
    origin_session_id: str | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)

    # Runtime only (never persisted).
    driver: asyncio.Task[None] | None = field(
        default=None, repr=False, compare=False
    )
    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )
    changed: asyncio.Condition = field(
        default_factory=asyncio.Condition, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.title = thread_title(self.title, self.goal)
        if not self.turns and self.goal:
            self.turns = [
                Turn(
                    seq=self.iteration,
                    prompt=self.goal,
                    started_at=self.created_at,
                )
            ]
            self.sync_active_turn()

    @property
    def status(self) -> str:
        if self.state is ThreadState.EXECUTING:
            return "running"
        if self.state is ThreadState.AWAITING_PERMISSION:
            return "awaiting_approval"
        return "idle"

    @property
    def outcome(self) -> str:
        if self.state is ThreadState.COMPLETED:
            return "finished"
        if self.state is ThreadState.FAILED:
            return "failed"
        if self.state is ThreadState.CANCELLED:
            return "cancelled"
        return ""

    @property
    def is_running(self) -> bool:
        return self.state in RUNNING_STATES

    @property
    def elapsed_seconds(self) -> int:
        return int((self.ended_at or time.time()) - self.created_at)

    @property
    def latest_update(self) -> str:
        return next(
            (
                e.message
                for e in reversed(self.events)
                if e.kind in _UPDATE_KINDS
            ),
            "",
        )

    def recent_updates(self, limit: int = 5) -> list[str]:
        """The latest distinct update lines from the current iteration, oldest first."""
        seen: list[str] = []
        for e in reversed(self.events):
            if (
                e.iteration == self.iteration
                and e.kind in _UPDATE_KINDS
                and e.message
                and e.message not in seen
            ):
                seen.append(e.message)
                if len(seen) >= limit:
                    break
        seen.reverse()
        return seen

    def outputs_for_iteration(self, iteration: int) -> list[dict[str, str]]:
        """What the thread made during `iteration`, in order first made."""
        return _merge_outputs(
            [], (e for e in self.events if e.iteration == iteration)
        )

    @property
    def outputs(self) -> list[dict[str, str]]:
        """What the thread made, once each (a later save of the same link updates it),
        in the order first made."""
        return _merge_outputs(
            [o for turn in self.turns for o in turn.outputs], self.events
        )

    def sync_active_turn(self) -> None:
        """Keep the current turn aligned with the handle's latest fields."""
        if not self.turns:
            self.turns.append(
                Turn(
                    seq=self.iteration,
                    prompt=self.goal,
                    started_at=self.created_at,
                )
            )
        turn = self.turns[-1]
        turn.outcome = self.outcome or "running"
        turn.answer = self.result_text or turn.answer
        turn.summary = self.summary or turn.summary
        turn.error = self.error or turn.error
        turn.cards = list(self.cards)
        turn.files_changed = list(self.files_changed)
        turn.diff_summary = self.diff_summary
        turn.raw_diff = self.raw_diff
        turn.artifacts = list(self.artifacts)
        if made := self.outputs_for_iteration(turn.seq):
            turn.outputs = made
        turn.commit_sha = self.commit_sha
        turn.pushed = self.pushed
        turn.ended_at = self.ended_at if self.state in FINISHED_STATES else None

    def record(
        self, kind: EventKind, message: str, data: dict[str, Any] | None = None
    ) -> ThreadEvent:
        self.last_seq += 1
        event = ThreadEvent(
            seq=self.last_seq,
            iteration=self.iteration,
            kind=kind,
            message=message,
            data=data or {},
        )
        self.events.append(event)
        if len(self.events) > EVENT_WINDOW:
            del self.events[: len(self.events) - EVENT_WINDOW]
        self.updated_at = event.ts
        return event

    def milestone_after(self, seq: int) -> ThreadEvent | None:
        return next(
            (
                e
                for e in self.events
                if e.seq > seq and e.kind in MILESTONE_KINDS
            ),
            None,
        )

    async def wait_for_milestone(
        self, after_seq: int, timeout_s: float
    ) -> ThreadEvent | None:
        """The first milestone event after `after_seq`, waiting up to `timeout_s`."""
        async with self.changed:
            try:
                await asyncio.wait_for(
                    self.changed.wait_for(
                        lambda: self.milestone_after(after_seq) is not None
                    ),
                    timeout_s,
                )
            except TimeoutError:
                return None
            return self.milestone_after(after_seq)

    def to_dict(self) -> dict[str, Any]:
        """The persisted snapshot (the API has its own serializer)."""
        self.sync_active_turn()
        data: dict[str, Any] = {
            "thread_id": self.thread_id,
            "owner": self.owner,
            "title": self.title,
            "goal": self.goal,
            "project": self.project,
            "harness": self.harness,
            "branch": self.branch,
            "state": self.state.value,
            "iteration": self.iteration,
            "sessions": self.sessions,
            "instructions": [i.to_dict() for i in self.instructions],
            "permission": self.permission.to_dict()
            if self.permission
            else None,
            "result_text": self.result_text,
            "summary": self.summary,
            "error": self.error,
            "files_changed": self.files_changed,
            "diff_summary": self.diff_summary,
            "raw_diff": self.raw_diff,
            "artifacts": self.artifacts,
            "profile": self.profile,
            "pushed": self.pushed,
            "commit_sha": self.commit_sha,
            "base_sha": self.base_sha,
            "workdir": self.workdir,
            "segment": self.segment,
            "dismissed": self.dismissed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "last_seq": self.last_seq,
            "turns": [t.to_dict() for t in self.turns],
        }
        if self.origin_session_id:
            data["origin_session_id"] = self.origin_session_id
        if self.cards:
            data["cards"] = self.cards
        return data

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], events: list[ThreadEvent] | None = None
    ) -> ThreadHandle:
        raw_state = str(data.get("state") or ThreadState.EXECUTING)
        state = ThreadState(raw_state)
        raw_turns = data.get("turns") or []
        turns = [Turn.from_dict(t) for t in raw_turns if isinstance(t, dict)]
        thread_id = str(data["thread_id"])
        handle = cls(
            thread_id=thread_id,
            goal=data["goal"],
            project=str(data.get("project") or f"scratch/{thread_id}"),
            harness=data["harness"],
            branch=data["branch"],
            owner=data.get("owner") or DEFAULT_DEV_USER_ID,
            title=str(data.get("title") or ""),
            state=state,
            iteration=int(data.get("iteration") or 1),
            sessions=dict(data.get("sessions") or {}),
            instructions=[
                Instruction.from_dict(i) for i in data.get("instructions") or []
            ],
            permission=PermissionRequest.from_dict(data.get("permission")),
            result_text=data.get("result_text") or "",
            summary=data.get("summary") or "",
            error=data.get("error"),
            files_changed=list(data.get("files_changed") or []),
            diff_summary=data.get("diff_summary") or "",
            raw_diff=data.get("raw_diff") or "",
            artifacts=list(data.get("artifacts") or []),
            profile=dict(data.get("profile") or {}),
            pushed=bool(data.get("pushed")),
            commit_sha=data.get("commit_sha"),
            base_sha=data.get("base_sha"),
            workdir=data.get("workdir"),
            segment=int(data.get("segment") or 0),
            dismissed=bool(data.get("dismissed")),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            ended_at=data.get("ended_at"),
            last_seq=int(data.get("last_seq") or 0),
            events=events or [],
            origin_session_id=data.get("origin_session_id") or None,
            cards=[c for c in data.get("cards") or [] if isinstance(c, dict)],
            turns=turns,
        )
        if not turns:
            handle.sync_active_turn()
        elif handle.raw_diff:
            turns[-1].raw_diff = handle.raw_diff
        return handle
