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

"""Thread titles, store-backed history (search and paging), and 30-day retention."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.identity import run_as
from app.store.models import ThreadEventRecord
from app.store.thread_store import ThreadStore
from app.threads import retention
from app.threads.model import EventKind, ThreadHandle, ThreadState
from app.threads.registry import (
    ThreadInputError,
    ThreadRegistry,
    set_thread_registry,
)
from app.threads.retention import RETENTION_DAYS, purge_expired_threads
from app.threads.titles import (
    UNTITLED,
    clean_title,
    fallback_title,
    thread_title,
)
from app.tools.thread_tools import start_thread
from tests.fakes import GOAL, REPO, Lab, seed_thread

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
DAY = 86400.0


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ThreadStore]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
    yield ThreadStore(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def registry(store: ThreadStore) -> AsyncIterator[ThreadRegistry]:
    reg = ThreadRegistry(store)
    set_thread_registry(reg)
    yield reg
    set_thread_registry(None)


def _thread(
    thread_id: str,
    *,
    owner: str = "local",
    goal: str = "Plan a trip",
    title: str = "",
    state: ThreadState = ThreadState.COMPLETED,
    ended_at: float | None = None,
    updated_at: float | None = None,
) -> ThreadHandle:
    now = time.time()
    finished = state in (
        ThreadState.COMPLETED,
        ThreadState.FAILED,
        ThreadState.CANCELLED,
    )
    handle = ThreadHandle(
        thread_id=thread_id,
        goal=goal,
        title=title,
        project="scratch/2026-01-01-history",
        harness="claude",
        branch=f"agent/{thread_id}",
        owner=owner,
        state=state,
        created_at=(ended_at or now) - 60,
        updated_at=updated_at or ended_at or now,
        ended_at=(ended_at or now) if finished else None,
    )
    handle.record(EventKind.COMPLETED, "Done.")
    handle.updated_at = updated_at or ended_at or now
    return handle


async def _event_count(store: ThreadStore, thread_id: str) -> int:
    async with store._engine.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(ThreadEventRecord)
            .where(ThreadEventRecord.thread_id == thread_id)
        )
        return int(result.scalar() or 0)


# ---- titles ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "title"),
    [
        (
            "Can you fix the flaky login test in web-dashboard, it fails on CI",
            "Fix the flaky login test",
        ),
        (
            "Please plan a 3-day trip to Alaska. Include kayaking.",
            "Plan a 3-day trip to Alaska",
        ),
        ("research EV chargers; compare prices", "Research EV chargers"),
        (
            "I want you to write meal plans for the week",
            "Write meal plans for the week",
        ),
        ("Rotate refresh tokens", "Rotate refresh tokens"),
        ("   ", UNTITLED),
        ("", UNTITLED),
    ],
)
async def test_the_fallback_title_is_the_first_clause_in_a_few_words(
    goal: str, title: str
):
    assert fallback_title(goal) == title


async def test_given_titles_are_cleaned_up_and_capped():
    assert (
        clean_title('  "alaska   trip itinerary."  ') == "Alaska trip itinerary"
    )
    long = clean_title("word " * 40)
    assert len(long) <= 60 and not long.endswith(" ")
    assert thread_title("", "Plan a trip") == "Plan a trip"
    assert thread_title("  ", "Plan a trip") == "Plan a trip"
    assert thread_title("Alaska trip", "Plan a trip") == "Alaska trip"


async def test_a_thread_never_has_an_empty_title_and_old_rows_get_one():
    assert _thread("thread-1", goal="Write the Q3 board notes").title == (
        "Write the Q3 board notes"
    )
    snapshot = _thread("thread-2", title="Q3 notes").to_dict()
    assert snapshot["title"] == "Q3 notes"
    del snapshot["title"]  # a thread saved before titles existed
    assert ThreadHandle.from_dict(snapshot).title == "Plan a trip"


async def test_the_title_is_persisted_and_survives_a_restart(lab: Lab):
    handle = await lab.dispatch(title="Greeting helper")
    assert handle.title == "Greeting helper"
    lab.registry._threads.clear()
    reloaded = await lab.registry.require(handle.thread_id)
    assert reloaded.title == "Greeting helper"


async def test_start_thread_takes_a_title_and_says_it(lab: Lab):
    started = await start_thread(GOAL, REPO, title="greeting helper")
    handle = await lab.settle(started["thread_id"])
    assert handle.title == "Greeting helper"
    assert started["message"].startswith(
        f"Started “Greeting helper” ({handle.thread_id}) in code/{REPO} with"
    )


async def test_start_thread_without_a_title_falls_back_to_the_goal(lab: Lab):
    started = await start_thread(GOAL, REPO)
    handle = await lab.settle(started["thread_id"])
    assert handle.title == fallback_title(GOAL)
    assert handle.thread_id not in handle.title and REPO not in handle.title


# ---- history query --------------------------------------------------------------


async def test_history_pages_newest_first_with_a_cursor(
    store: ThreadStore, registry: ThreadRegistry
):
    now = time.time()
    # Two share an end time: the cursor must still neither skip nor repeat them.
    ends = [now - 10, now - 20, now - 20, now - 30, now - 40]
    for i, ended in enumerate(ends):
        await seed_thread(store, _thread(f"thread-{i}", ended_at=ended))

    seen: list[str] = []
    before = None
    pages = 0
    while True:
        handles, before = await registry.query(limit=2, before=before)
        seen += [h.thread_id for h in handles]
        pages += 1
        if before is None:
            break
    assert pages == 3
    assert seen[0] == "thread-0" and seen[-1] == "thread-4"
    assert sorted(seen) == [f"thread-{i}" for i in range(5)]
    assert len(seen) == len(set(seen))


async def test_history_search_matches_title_and_goal_case_insensitively(
    store: ThreadStore, registry: ThreadRegistry
):
    await seed_thread(
        store,
        _thread("thread-a", title="Alaska kayak weekend", goal="Plan a trip"),
    )
    await seed_thread(
        store,
        _thread(
            "thread-b", title="Board notes", goal="Summarize the ALASKA memo"
        ),
    )
    await seed_thread(
        store, _thread("thread-c", title="100% done", goal="Rotate tokens")
    )

    found, _ = await registry.query(q="alaska")
    assert {h.thread_id for h in found} == {"thread-a", "thread-b"}
    found, _ = await registry.query(q="KAYAK")
    assert [h.thread_id for h in found] == ["thread-a"]
    # LIKE wildcards in the query match literally.
    found, _ = await registry.query(q="100%")
    assert [h.thread_id for h in found] == ["thread-c"]
    found, _ = await registry.query(q="_")
    assert found == []


async def test_history_is_per_user(
    store: ThreadStore, registry: ThreadRegistry
):
    await seed_thread(
        store, _thread("thread-alice", owner=ALICE, title="Alice's trip")
    )
    await seed_thread(
        store, _thread("thread-bob", owner=BOB, title="Bob's trip")
    )
    with run_as(ALICE):
        assert [h.thread_id for h in (await registry.query(q="trip"))[0]] == [
            "thread-alice"
        ]
    with run_as(BOB):
        assert [h.thread_id for h in (await registry.query())[0]] == [
            "thread-bob"
        ]


async def test_history_filters_by_state_and_rejects_a_bad_cursor(
    store: ThreadStore, registry: ThreadRegistry
):
    await seed_thread(store, _thread("thread-done"))
    await seed_thread(
        store, _thread("thread-gate", state=ThreadState.AWAITING_PERMISSION)
    )
    found, _ = await registry.query(states=[ThreadState.COMPLETED])
    assert [h.thread_id for h in found] == ["thread-done"]
    with pytest.raises(ThreadInputError):
        await registry.query(before="not-a-cursor")


async def test_history_reads_older_threads_without_caching_them(
    store: ThreadStore, registry: ThreadRegistry
):
    await seed_thread(store, _thread("thread-old", title="Old trip"))
    found, _ = await registry.query()
    assert [h.title for h in found] == ["Old trip"]
    assert "thread-old" not in registry._threads


async def test_the_history_route_pages_and_searches(
    store: ThreadStore, registry: ThreadRegistry
):
    from app.fast_api_app import app

    now = time.time()
    for i in range(3):
        await seed_thread(
            store, _thread(f"thread-{i}", title=f"Trip {i}", ended_at=now - i)
        )
    await seed_thread(
        store,
        _thread("thread-live", title="Trip live", state=ThreadState.EXECUTING),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as api:
        first = (
            await api.get(
                "/api/v1/threads", params={"state": "finished", "limit": 2}
            )
        ).json()
        assert [t["title"] for t in first["threads"]] == ["Trip 0", "Trip 1"]
        assert first["next_before"]
        second = (
            await api.get(
                "/api/v1/threads",
                params={"state": "finished", "before": first["next_before"]},
            )
        ).json()
        assert [t["thread_id"] for t in second["threads"]] == ["thread-2"]
        assert second["next_before"] is None

        found = (
            await api.get("/api/v1/threads", params={"q": "trip live"})
        ).json()
        assert [t["thread_id"] for t in found["threads"]] == ["thread-live"]

        assert (
            await api.get("/api/v1/threads", params={"before": "x"})
        ).status_code == 400
        assert (
            await api.get("/api/v1/threads", params={"state": "nope"})
        ).status_code == 400


# ---- retention ------------------------------------------------------------------


async def test_finished_threads_are_deleted_30_days_after_they_end(
    store: ThreadStore, registry: ThreadRegistry
):
    now = time.time()
    expired = RETENTION_DAYS * DAY + 60
    await seed_thread(store, _thread("thread-expired", ended_at=now - expired))
    await seed_thread(
        store, _thread("thread-bob-expired", owner=BOB, ended_at=now - expired)
    )
    await seed_thread(store, _thread("thread-recent", ended_at=now - 29 * DAY))
    await seed_thread(
        store,
        _thread(
            "thread-waiting",
            state=ThreadState.AWAITING_PERMISSION,
            updated_at=now - expired,
        ),
    )
    # One of them is in memory too: the cleanup drops it there as well.
    assert await registry.get("thread-expired") is not None
    assert await _event_count(store, "thread-expired") == 1

    assert await purge_expired_threads(registry, now=now) == 2

    assert "thread-expired" not in registry._threads
    assert await registry.get("thread-expired") is None
    assert await store.load_thread("thread-bob-expired") is None
    assert await registry.get("thread-recent") is not None
    assert await registry.get("thread-waiting") is not None
    # Its events went with it.
    assert await _event_count(store, "thread-expired") == 0
    assert await purge_expired_threads(registry, now=now) == 0


async def test_expired_and_cleared_adk_sessions_are_purged(
    registry: ThreadRegistry,
):
    from app.app_utils.services import get_session_service

    service = get_session_service()
    s1 = await service.create_session(app_name="app", user_id=ALICE)
    s2 = await service.create_session(app_name="app", user_id=BOB)
    now = time.time()
    # Purging with a future clock deletes both expired sessions.
    await purge_expired_threads(registry, now=now + RETENTION_DAYS * DAY + 60)
    assert (
        await service.get_session(
            app_name="app", user_id=ALICE, session_id=s1.id
        )
        is None
    )
    assert (
        await service.get_session(app_name="app", user_id=BOB, session_id=s2.id)
        is None
    )

    # clear_all deletes only the acting user's sessions.
    s_alice = await service.create_session(app_name="app", user_id=ALICE)
    s_bob = await service.create_session(app_name="app", user_id=BOB)
    with run_as(ALICE):
        await registry.clear_all()
    assert (
        await service.get_session(
            app_name="app", user_id=ALICE, session_id=s_alice.id
        )
        is None
    )
    assert (
        await service.get_session(
            app_name="app", user_id=BOB, session_id=s_bob.id
        )
        is not None
    )


async def test_retention_loop_runs_purge_and_stops_cleanly(
    store: ThreadStore,
    registry: ThreadRegistry,
    monkeypatch: pytest.MonkeyPatch,
):
    ran = asyncio.Event()
    real_purge = retention.purge_expired_threads

    async def recording_purge(reg=None, *, now=None) -> int:
        count = await real_purge(reg, now=now)
        ran.set()
        return count

    monkeypatch.setattr(retention, "purge_expired_threads", recording_purge)
    task = asyncio.create_task(retention.retention_loop())
    try:
        await asyncio.wait_for(ran.wait(), 2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
