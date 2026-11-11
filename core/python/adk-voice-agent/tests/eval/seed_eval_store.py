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

"""Seeds the thread database (threads and sessions) before a voice eval case from `voice/history.yaml`."""

from __future__ import annotations

import asyncio
import os
import sys

# `agents-cli eval generate` opens sessions as `eval-cli-user`; match `sitecustomize.py`.
os.environ.setdefault("VOICE_AGENT_DEV_USER_ID", "eval-cli-user")

from history import current, seconds_ago

from app.auth.identity import dev_user_id
from app.threads.model import (
    EventKind,
    Instruction,
    InstructionKind,
    PermissionRequest,
    ThreadHandle,
    ThreadState,
)


def thread_from_fixture(spec: dict) -> ThreadHandle:
    goal = spec["goal"]
    created = seconds_ago(spec.get("created"))
    ended = spec.get("ended")
    instructions = [Instruction(InstructionKind.GOAL, goal, 1)]
    instructions += [
        Instruction.from_dict(
            {
                "kind": kind,
                "text": text,
                "iteration": int(spec.get("iteration", 1)),
            }
        )
        for kind, text in spec.get("followups") or []
    ]
    state = ThreadState(str(spec["state"]))
    permission = PermissionRequest.from_dict(spec.get("permission"))
    result_text = spec.get("result_text") or spec.get("summary") or ""
    return ThreadHandle(
        thread_id=spec["thread_id"],
        owner=dev_user_id(),
        title=spec.get("title", ""),
        goal=goal,
        project=spec.get("project") or f"scratch/{spec['thread_id']}",
        harness=spec.get("harness", "claude"),
        branch=f"agent/{spec['thread_id']}",
        state=state,
        iteration=int(spec.get("iteration", 1)),
        permission=permission,
        result_text=result_text,
        error=spec.get("error"),
        files_changed=list(spec.get("files_changed") or []),
        instructions=instructions,
        origin_session_id=spec.get("origin_session_id"),
        created_at=created,
        updated_at=seconds_ago(ended) if ended is not None else created,
        ended_at=seconds_ago(ended) if ended is not None else None,
    )


async def seed_sessions(sessions: list[dict]) -> None:
    from google.adk.events.event import Event
    from google.genai import types

    from app.app_utils.services import get_session_service

    service, user = get_session_service(), dev_user_id()
    for spec in sessions:
        session = await service.create_session(
            app_name="app", user_id=user, session_id=spec["id"]
        )
        ended, turns = seconds_ago(spec.get("ended")), spec.get("turns") or []
        for i, (role, text) in enumerate(turns):
            said = types.Transcription(text=text)
            await service.append_event(
                session,
                Event(
                    author="user" if role == "user" else "voice_agent",
                    input_transcription=said if role == "user" else None,
                    output_transcription=None if role == "user" else said,
                    timestamp=ended - (len(turns) - 1 - i) * 10,
                ),
            )


async def seed() -> None:
    # This drops every thread table, so it only runs against an explicitly chosen database.
    if not os.getenv("THREAD_DB_URL"):
        sys.exit(
            "Set THREAD_DB_URL to a throwaway database; seeding drops all thread tables."
        )
    from app.store.engine import close_db, get_async_engine
    from app.store.models import Base
    from app.store.thread_store import ThreadStore
    from app.threads.registry import ThreadRegistry
    from app.voice.focus import set_focus

    history = current()
    if history.focus:
        set_focus(
            view=history.focus.get("view", "home"),
            project=history.focus.get("project", ""),
            thread_id=history.focus.get("thread_id", ""),
            user_id=dev_user_id(),
        )
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    store = ThreadStore(engine)
    registry = ThreadRegistry(store)
    specs = history.threads
    heard = [s for s in specs if not s.get("announce")]
    for spec in heard:
        await store.save_thread(thread_from_fixture(spec).to_dict())
    await store.mark_all_seen(dev_user_id())
    for spec in specs:
        if spec.get("announce"):
            handle = thread_from_fixture(spec)
            kind = EventKind(str(spec["announce"]))
            await registry.emit(
                handle,
                kind,
                handle.summary or handle.result_text,
                handle.permission.to_dict() if handle.permission else None,
            )
    await seed_sessions(history.sessions)
    await close_db()
    print(
        f"Seeded {len(specs)} thread(s): {', '.join(s['thread_id'] for s in specs) or 'none'}."
    )


if __name__ == "__main__":
    asyncio.run(seed())
