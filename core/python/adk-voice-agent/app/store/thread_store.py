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

"""Async repository for threads, their event log, and briefing cursors.

Every thread row has an owner (`principal_id`, the user id); listings, clears, and
the briefing cursor are all per owner.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.store.engine import SqlStore
from app.store.models import (
    CursorRecord,
    ThreadEventRecord,
    ThreadRecord,
    thread_columns,
)
from app.threads.model import EventKind, ThreadState


class ThreadStore(SqlStore):
    async def save_thread(self, snapshot: dict[str, Any]) -> None:
        factory = await self._ready()
        async with factory() as session:
            record = await session.get(ThreadRecord, snapshot["thread_id"])
            if record is None:
                session.add(
                    ThreadRecord(
                        thread_id=snapshot["thread_id"],
                        principal_id=snapshot["owner"],
                        data=snapshot,
                        **thread_columns(snapshot),
                    )
                )
            else:
                record.data = snapshot
                for name, value in thread_columns(snapshot).items():
                    setattr(record, name, value)
            await session.commit()

    async def append_event(self, thread_id: str, event: dict[str, Any]) -> int:
        factory = await self._ready()
        async with factory() as session:
            record = ThreadEventRecord(
                thread_id=thread_id,
                seq=event["seq"],
                iteration=event["iteration"],
                kind=event["kind"],
                message=event["message"],
                data=event.get("data") or None,
                ts=event["ts"],
            )
            session.add(record)
            await session.commit()
            return record.id

    async def load_thread(
        self, thread_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Thread snapshot plus its most recent events (oldest first)."""
        factory = await self._ready()
        async with factory() as session:
            record = await session.get(ThreadRecord, thread_id)
            if record is None:
                return None
            events = await self._recent_events(session, thread_id)
            return {**record.data, "owner": record.principal_id}, events

    async def recent_thread_ids(self, owner: str, limit: int = 50) -> list[str]:
        """IDs of `owner`'s most recent threads, newest first."""
        factory = await self._ready()
        async with factory() as session:
            rows = await session.execute(
                select(ThreadRecord.thread_id)
                .where(ThreadRecord.principal_id == owner)
                .order_by(desc(ThreadRecord.created_at))
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def query_threads(
        self,
        owner: str,
        *,
        q: str = "",
        before: str | None = None,
        limit: int = 30,
        states: list[str] | None = None,
        origin_session_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a page of `owner`'s thread snapshots and the next cursor (`None` on last page)."""
        query = select(ThreadRecord).where(ThreadRecord.principal_id == owner)
        if states:
            query = query.where(ThreadRecord.state.in_(states))
        if origin_session_id:
            query = query.where(
                ThreadRecord.origin_session_id == origin_session_id
            )
        if words := q.strip():
            pattern = f"%{_escape_like(words)}%"
            query = query.where(
                or_(
                    ThreadRecord.title.ilike(pattern, escape="\\"),
                    ThreadRecord.goal.ilike(pattern, escape="\\"),
                )
            )
        if before:
            ts, thread_id = decode_cursor(before)
            query = query.where(
                or_(
                    ThreadRecord.activity_at < ts,
                    and_(
                        ThreadRecord.activity_at == ts,
                        ThreadRecord.thread_id < thread_id,
                    ),
                )
            )
        query = query.order_by(
            desc(ThreadRecord.activity_at), desc(ThreadRecord.thread_id)
        ).limit(limit + 1)
        factory = await self._ready()
        async with factory() as session:
            records = list((await session.execute(query)).scalars().all())
        page = records[:limit]
        more = len(records) > limit and bool(page)
        return (
            [{**r.data, "owner": r.principal_id} for r in page],
            encode_cursor(page[-1].activity_at or 0.0, page[-1].thread_id)
            if more
            else None,
        )

    async def delete_finished_before(
        self, cutoff: float, finished_states: list[str]
    ) -> list[tuple[str, str]]:
        """Delete every user's threads that finished before `cutoff` (epoch seconds),
        with their events. Returns the deleted `(thread_id, owner)` pairs."""
        factory = await self._ready()
        async with factory() as session:
            rows = await session.execute(
                select(ThreadRecord.thread_id, ThreadRecord.principal_id).where(
                    ThreadRecord.state.in_(finished_states),
                    ThreadRecord.ended_at.is_not(None),
                    ThreadRecord.ended_at < cutoff,
                )
            )
            expired = [(thread_id, owner) for thread_id, owner in rows.all()]
            ids = [thread_id for thread_id, _ in expired]
            for start in range(0, len(ids), _DELETE_BATCH):
                batch = ids[start : start + _DELETE_BATCH]
                await session.execute(
                    delete(ThreadEventRecord).where(
                        ThreadEventRecord.thread_id.in_(batch)
                    )
                )
                await session.execute(
                    delete(ThreadRecord).where(
                        ThreadRecord.thread_id.in_(batch)
                    )
                )
            await session.commit()
            return expired

    async def latest_origin_session(
        self, owner: str, *, exclude: str | None = None
    ) -> str | None:
        """The voice conversation `owner` most recently dispatched a thread from, other
        than `exclude` (the current one); None if there is none."""
        query = select(ThreadRecord.origin_session_id, ThreadRecord.data).where(
            ThreadRecord.principal_id == owner,
            ThreadRecord.origin_session_id.is_not(None),
        )
        if exclude:
            query = query.where(ThreadRecord.origin_session_id != exclude)
        factory = await self._ready()
        async with factory() as session:
            rows = (
                await session.execute(
                    query.order_by(desc(ThreadRecord.created_at)).limit(
                        _LATEST_WINDOW
                    )
                )
            ).all()
        # The row's `created_at` has one-second resolution on SQLite, so threads created in
        # the same second tie; the snapshot's creation time decides.
        latest = max(
            rows,
            key=lambda r: (r.data or {}).get("created_at") or 0.0,
            default=None,
        )
        return latest.origin_session_id if latest else None

    async def _recent_events(
        self, session: AsyncSession, thread_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        rows = await session.execute(
            select(ThreadEventRecord)
            .where(ThreadEventRecord.thread_id == thread_id)
            .order_by(desc(ThreadEventRecord.seq))
            .limit(limit)
        )
        return [_event_dict(e) for e in reversed(rows.scalars().all())]

    async def fail_interrupted_threads(
        self, running_states: list[str]
    ) -> list[tuple[str, str]]:
        """Threads that were mid-run when the server stopped can't be resumed; mark them failed.

        Each gets an `error` event, so the owner's next voice briefing mentions it.
        Returns their `(thread_id, owner)` pairs, across all owners.
        """
        factory = await self._ready()
        now = datetime.datetime.now(datetime.UTC).timestamp()
        message = "Interrupted by a server restart. Steer the thread to retry."
        async with factory() as session:
            rows = await session.execute(
                select(ThreadRecord).where(
                    ThreadRecord.state.in_(running_states)
                )
            )
            records = list(rows.scalars().all())
            for record in records:
                data = dict(record.data)
                max_seq = (
                    await session.execute(
                        select(func.max(ThreadEventRecord.seq)).where(
                            ThreadEventRecord.thread_id == record.thread_id
                        )
                    )
                ).scalar() or 0
                seq = max(int(data.get("last_seq") or 0), int(max_seq)) + 1
                data.update(
                    state=ThreadState.FAILED.value,
                    error=message,
                    ended_at=now,
                    updated_at=now,
                    last_seq=seq,
                )
                record.data = data
                for name, value in thread_columns(data).items():
                    setattr(record, name, value)
                session.add(
                    ThreadEventRecord(
                        thread_id=record.thread_id,
                        seq=seq,
                        iteration=int(data.get("iteration") or 1),
                        kind=EventKind.ERROR.value,
                        message=message,
                        data=None,
                        ts=now,
                    )
                )
            await session.commit()
            return [(r.thread_id, r.principal_id) for r in records]

    async def delete_thread(self, thread_id: str, owner: str) -> bool:
        """Delete one of `owner`'s threads and its events; False if they have no such thread."""
        factory = await self._ready()
        async with factory() as session:
            owned = (
                select(ThreadRecord.thread_id)
                .where(ThreadRecord.thread_id == thread_id)
                .where(ThreadRecord.principal_id == owner)
            )
            await session.execute(
                delete(ThreadEventRecord).where(
                    ThreadEventRecord.thread_id.in_(owned)
                )
            )
            result = await session.execute(
                delete(ThreadRecord)
                .where(ThreadRecord.thread_id == thread_id)
                .where(ThreadRecord.principal_id == owner)
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def delete_threads(self, thread_ids: list[str], owner: str) -> int:
        """Delete `thread_ids` owned by `owner` and their events; returns count deleted."""
        if not thread_ids:
            return 0
        factory = await self._ready()
        async with factory() as session:
            owned = (
                select(ThreadRecord.thread_id)
                .where(ThreadRecord.thread_id.in_(thread_ids))
                .where(ThreadRecord.principal_id == owner)
            )
            await session.execute(
                delete(ThreadEventRecord).where(
                    ThreadEventRecord.thread_id.in_(owned)
                )
            )
            result = await session.execute(
                delete(ThreadRecord)
                .where(ThreadRecord.thread_id.in_(thread_ids))
                .where(ThreadRecord.principal_id == owner)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def clear_threads(self, owner: str) -> int:
        """Delete `owner`'s threads and their events; other users' threads are untouched."""
        factory = await self._ready()
        async with factory() as session:
            owned = select(ThreadRecord.thread_id).where(
                ThreadRecord.principal_id == owner
            )
            await session.execute(
                delete(ThreadEventRecord).where(
                    ThreadEventRecord.thread_id.in_(owned)
                )
            )
            result = await session.execute(
                delete(ThreadRecord).where(ThreadRecord.principal_id == owner)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def delete_project_threads(
        self, project: str, owner: str
    ) -> list[str]:
        """Delete all of `owner`'s threads in `project` and return their IDs."""
        factory = await self._ready()
        async with factory() as session:
            rows = await session.execute(
                select(ThreadRecord.thread_id, ThreadRecord.data).where(
                    ThreadRecord.principal_id == owner
                )
            )
            matching = [
                tid
                for tid, data in rows.all()
                if (data or {}).get("project") == project
            ]
        if matching:
            await self.delete_threads(matching, owner)
        return matching

    async def unseen_events(
        self,
        kinds: list[str],
        principal_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Events of `kinds` after the user's cursor, from their threads that are not dismissed."""
        factory = await self._ready()
        async with factory() as session:
            last_id = (
                await session.execute(
                    select(CursorRecord.last_seen_event_id).where(
                        CursorRecord.principal_id == principal_id
                    )
                )
            ).scalar() or 0
            rows = await session.execute(
                select(ThreadEventRecord)
                .join(
                    ThreadRecord,
                    ThreadRecord.thread_id == ThreadEventRecord.thread_id,
                )
                .where(
                    ThreadRecord.principal_id == principal_id,
                    ThreadRecord.dismissed.is_(False),
                    ThreadEventRecord.id > last_id,
                    ThreadEventRecord.kind.in_(kinds),
                )
                .order_by(ThreadEventRecord.id)
                .limit(limit)
            )
            return [
                {
                    "id": e.id,
                    "thread_id": e.thread_id,
                    "seq": e.seq,
                    "kind": e.kind,
                    "message": e.message,
                }
                for e in rows.scalars().all()
            ]

    async def mark_all_seen(
        self, principal_id: str, delivered: dict[str, int] | None = None
    ) -> None:
        if delivered is not None and not delivered:
            return
        factory = await self._ready()
        async with factory() as session:
            stmt = (
                select(func.max(ThreadEventRecord.id))
                .join(
                    ThreadRecord,
                    ThreadRecord.thread_id == ThreadEventRecord.thread_id,
                )
                .where(ThreadRecord.principal_id == principal_id)
            )
            if delivered is not None:
                stmt = stmt.where(
                    or_(
                        *[
                            and_(
                                ThreadEventRecord.thread_id == tid,
                                ThreadEventRecord.seq <= seq,
                            )
                            for tid, seq in delivered.items()
                        ]
                    )
                )
            latest = (await session.execute(stmt)).scalar() or 0
        if latest:
            await self.advance_cursor(latest, principal_id)

    async def advance_cursor(self, event_id: int, principal_id: str) -> None:
        factory = await self._ready()
        async with factory() as session:
            cursor = await session.get(CursorRecord, principal_id)
            if cursor is None:
                session.add(
                    CursorRecord(
                        principal_id=principal_id, last_seen_event_id=event_id
                    )
                )
            elif event_id > cursor.last_seen_event_id:
                cursor.last_seen_event_id = event_id
            await session.commit()


_LATEST_WINDOW = 50
_DELETE_BATCH = 500


def encode_cursor(activity_at: float, thread_id: str) -> str:
    return f"{activity_at!r}~{thread_id}"


def decode_cursor(cursor: str) -> tuple[float, str]:
    ts, sep, thread_id = cursor.partition("~")
    if not sep or not thread_id:
        raise ValueError(f"Not a thread history cursor: {cursor!r}")
    return float(ts), thread_id


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _event_dict(record: ThreadEventRecord) -> dict[str, Any]:
    return {
        "seq": record.seq,
        "iteration": record.iteration,
        "kind": record.kind,
        "message": record.message,
        "data": record.data or {},
        "ts": record.ts,
    }


_store: ThreadStore | None = None


def get_thread_store() -> ThreadStore:
    global _store
    if _store is None:
        _store = ThreadStore()
    return _store


def set_thread_store(store: ThreadStore | None) -> None:
    global _store
    _store = store
