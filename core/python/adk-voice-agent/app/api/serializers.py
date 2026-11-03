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

"""Explicit REST serializers for threads so internal state never leaks."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from app.auth.github.api import parse_github_remote
from app.threads.model import (
    FINISHED_STATES,
    GATE_STATES,
    RUNNING_STATES,
    ThreadHandle,
    Turn,
)
from app.threads.summaries import fallback_summary
from app.workers.manifest import repo_source
from app.workers.workspace import is_code, repo_of

STALE_FINISHED_S = 600
_EVENT_TAIL = 50
OUTPUT_KINDS = frozenset(
    {"pr", "branch", "doc", "sheet", "slides", "file", "event", "draft"}
)


def _serialize_turn(handle: ThreadHandle, turn: Turn) -> dict[str, Any]:
    turn_events = [
        e.to_dict() for e in handle.events if e.iteration == turn.seq
    ]
    return {
        "seq": turn.seq,
        "prompt": turn.prompt,
        "outcome": turn.outcome,
        "answer": turn.answer,
        "summary": turn.summary,
        "error": turn.error,
        "cards": list(turn.cards),
        "links": output_links(handle, turn.outputs, pushed=turn.pushed),
        "started_at": turn.started_at,
        "ended_at": turn.ended_at,
        "elapsed_seconds": turn.elapsed_seconds,
        "events": turn_events,
    }


def thread_summary(handle: ThreadHandle) -> dict[str, Any]:
    handle.sync_active_turn()
    return {
        "thread_id": handle.thread_id,
        "title": handle.title,
        "goal": handle.goal,
        "project": handle.project,
        "harness": handle.harness,
        # Only code threads have a branch of their own; other projects commit on `main`.
        "branch": handle.branch if is_code(handle.project) else "",
        "state": handle.state.value,
        "status": handle.status,
        "outcome": handle.outcome,
        "is_running": handle.state in RUNNING_STATES,
        "is_gate": handle.state in GATE_STATES,
        "is_finished": handle.state in FINISHED_STATES,
        "is_stale": is_stale(handle),
        "turn_count": len(handle.turns),
        "latest_update": handle.latest_update,
        "result_text": handle.result_text,
        "summary": handle.summary or fallback_summary(handle.result_text),
        "error": handle.error,
        "permission": {
            "action_class": handle.permission.action_class,
            "command": handle.permission.command,
            "key": handle.permission.key,
            "kind": handle.permission.kind.value,
            "arguments": handle.permission.arguments,
        }
        if handle.permission
        else None,
        "links": output_links(handle, handle.outputs, pushed=handle.pushed),
        "cards": handle.cards,
        "created_at": handle.created_at,
        "updated_at": handle.updated_at,
        "ended_at": handle.ended_at,
        "elapsed_seconds": handle.elapsed_seconds,
        "last_seq": handle.last_seq,
    }


def thread_detail(handle: ThreadHandle) -> dict[str, Any]:
    return {
        **thread_summary(handle),
        "turns": [_serialize_turn(handle, t) for t in handle.turns],
        "events": [e.to_dict() for e in handle.events[-_EVENT_TAIL:]],
    }


def output_links(
    handle: ThreadHandle, outputs: list[dict[str, str]], *, pushed: bool
) -> list[dict[str, str]]:
    """Links to deliverables: PR or pushed branch first, then Drive and connected-app items."""
    links: list[dict[str, str]] = [
        {
            "kind": kind
            if (kind := o.get("kind")) and kind in OUTPUT_KINDS
            else "file",
            "title": o.get("title") or "",
            "url": o["url"],
        }
        for o in outputs
    ]
    has_pr = any(link["kind"] == "pr" for link in links)
    if pushed and not has_pr and (branch_url := github_branch_url(handle)):
        links.insert(
            0, {"kind": "branch", "title": handle.branch, "url": branch_url}
        )
    code = [link for link in links if link["kind"] in ("pr", "branch")]
    return code + [link for link in links if link not in code]


def github_branch_url(handle: ThreadHandle) -> str:
    if not (handle.pushed and handle.branch and is_code(handle.project)):
        return ""
    remote = parse_github_remote(repo_source(repo_of(handle.project))[0])
    if remote is None:
        return ""
    owner, name = remote
    return f"https://github.com/{owner}/{name}/tree/{quote(handle.branch)}"


def is_stale(handle: ThreadHandle) -> bool:
    return handle.dismissed or (
        handle.ended_at is not None
        and time.time() - handle.ended_at > STALE_FINISHED_S
    )
