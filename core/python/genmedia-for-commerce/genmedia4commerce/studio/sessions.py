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

"""ADK sessions in the media bucket, shared by every Cloud Run instance.

A session lives in the folder of its studio (``store.session_id``):
``sessions/studio-<id>/adk/session.json`` holds its id, app, user, state and
creation time, and every event is one object under ``adk/events/``, named
by timestamp and event id so that the names sort in event order. Events are
written with ``if_generation_match=0`` (create only), so instances never
overwrite each other and appending never reads the history; the state is
merged into ``session.json`` with a generation precondition
(``store.update``).

An event object is never rewritten, so each process keeps the events it
has read (``studio.session_cache``, for the most recent sessions): a ``get_session``
lists the events and reads only the objects it has not seen. It also
remembers the sessions that exist, so ``ensure_session`` (every note)
writes nothing after the first time.

Session ids are studio ids (``store.check_id``), never rewritten: creating
a session with an invalid id raises ``ValueError``, which ADK's API answers
with 400, and there is no session with an invalid id (``get_session``
returns None, ADK's "not found"). ``studio.session_limits`` caps a user's
message and keeps only the latest ``[studio]`` notes.

The panel (``studio.agent.run_agent_turn``), the ADK API, ``adk web`` and
``studio.history.note`` all use it: ADK finds it under the ``studio://``
scheme (``register``, or ``services.yaml`` for the ADK command line).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google.adk.cli.service_registry import get_service_registry
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events import Event
from google.adk.sessions import BaseSessionService, Session, State
from google.adk.sessions.base_session_service import (
    GetSessionConfig,
    ListSessionsResponse,
)
from google.api_core.exceptions import NotFound, PreconditionFailed

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import InputError, StorageError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import session_limits as limits
from genmedia4commerce.studio import store
from genmedia4commerce.studio.session_cache import SessionCache

__all__ = [
    "APP_NAME",
    "PANEL_USER",
    "SCHEME",
    "StudioSessionService",
    "add_event",
    "ensure_session",
    "register",
]

logger = logging.getLogger(__name__)

APP_NAME = "genmedia4commerce"
#: The user of the sessions the panel and ``add_event`` create.
PANEL_USER = "studio"
SCHEME = "studio"
#: Event objects read at once.
_READERS = 16
#: The events and sessions this process has read.
_CACHE = SessionCache()


def _folder(session_id: str) -> str:
    return f"sessions/{store.session_id(session_id)}/adk/"


def _session_path(session_id: str) -> str:
    return _folder(session_id) + "session.json"


def _events_prefix(session_id: str) -> str:
    return _folder(session_id) + "events/"


def _uri(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def _micros(name: str) -> int:
    """The timestamp (microseconds) in an event object's name."""
    return int(name.rsplit("/", 1)[-1].split("-", 1)[0])


def _create(path: str, doc: Any) -> bool:
    """Write ``doc`` at ``path`` only if nothing is there; whether it was
    written (``False``: the object exists)."""
    try:
        gcs.blob(_uri(path)).upload_from_string(
            json.dumps(doc).encode(),
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed:
        return False
    return True


def _write_event(session_id: str, event: Event, *, note: bool = False) -> None:
    """Store one event (a ``[studio]`` note: ``note``) as a new object;
    raises ``StorageError``."""
    micros = math.floor(event.timestamp * 1e6)
    end = limits.NOTE_SUFFIX if note else ".json"
    path = f"{_events_prefix(session_id)}{micros:017d}-{event.id}{end}"
    doc = json.loads(event.model_dump_json(exclude_none=True))
    try:
        created = _create(path, doc)
    except Exception as exc:
        raise StorageError(
            f"could not store an event of {session_id}: {exc}"
        ) from exc
    if created:  # as a later read would parse it
        _CACHE.put(_folder(session_id), path, Event.model_validate(doc))
    else:
        logger.warning(
            "Event %s of %s was already stored", event.id, session_id
        )


def _session_doc(
    session_id: str, app_name: str, user_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": session_id,
        "app_name": app_name,
        "user_id": user_id,
        "state": {
            k: v
            for k, v in state.items()
            if not k.startswith(State.TEMP_PREFIX)
        },
        "last_update_time": time.time(),
    }


def ensure_session(
    session_id: str, app_name: str = APP_NAME, user_id: str = PANEL_USER
) -> None:
    """Create the session unless it exists (one conditional write, none
    once this process knows it exists)."""
    path = _session_path(session_id)
    if not _CACHE.known(path):
        _create(path, _session_doc(session_id, app_name, user_id, {}))
        _CACHE.mark(path)


def add_event(session_id: str, event: Event) -> bool:
    """Append a ``[studio]`` note to the session (created if missing)
    without reading it; whether it was stored. Failures are logged. Only
    the latest notes are kept (``session_limits.trim_notes``)."""
    try:
        ensure_session(session_id)
        _write_event(session_id, event, note=True)
    except Exception as exc:
        logger.warning("Could not add an event to %s: %s", session_id, exc)
        return False
    return True


def _read_all(paths: list[str]) -> list[Any]:
    """The JSON documents at ``paths``, read in parallel, in order."""
    if not paths:
        return []
    with ThreadPoolExecutor(min(_READERS, len(paths))) as pool:
        return list(pool.map(store.read, paths))


def _filtered(names: list[str], config: GetSessionConfig | None) -> list[str]:
    """Event objects filtered by ``config`` on their names (the exact
    ``after_timestamp`` check is on the events)."""
    if config and config.after_timestamp is not None:
        floor = math.floor(config.after_timestamp * 1e6)
        names = [n for n in names if _micros(n) >= floor]
    if config and config.num_recent_events is not None:
        names = names[max(0, len(names) - config.num_recent_events) :]
    return names


def _drop(paths: list[str]) -> None:
    """Delete the notes ``trim_notes`` dropped (another instance may have
    deleted them already); failures are logged."""
    for path in paths:
        try:
            gcs.delete(_uri(path))
        except NotFound:
            pass
        except Exception as exc:
            logger.warning("Could not delete the old note %s: %s", path, exc)


def _events(session_id: str, config: GetSessionConfig | None) -> list[Event]:
    """The session's events in order (copies), filtered by ``config``:
    one listing, and a read of each object this process has not seen. The
    notes older than the latest ``session_limits.MAX_NOTES`` are dropped."""
    folder = _folder(session_id)
    bucket = get_settings().media_bucket
    listed = sorted(gcs.list_paths(bucket, _events_prefix(session_id)))
    names, old = limits.trim_notes(listed)
    _drop(old)
    cached = _CACHE.events(folder, names)
    names = _filtered(names, config)
    new = [n for n in names if n not in cached]
    for name, doc in zip(new, _read_all(new), strict=True):
        if doc is not None:  # else deleted meanwhile
            cached[name] = Event.model_validate(doc)
            _CACHE.put(folder, name, cached[name])
    return [cached[n].model_copy(deep=True) for n in names if n in cached]


def _owned(doc: Any, app_name: str, user_id: str | None) -> bool:
    return (
        isinstance(doc, dict)
        and doc.get("app_name") == app_name
        and user_id in (None, doc.get("user_id"))
    )


def _get(
    app_name: str,
    user_id: str,
    session_id: str,
    config: GetSessionConfig | None,
) -> Session | None:
    doc = store.read(_session_path(session_id))
    if not _owned(doc, app_name, user_id):
        return None
    events = _events(session_id, config)
    if config and config.after_timestamp is not None:
        events = [e for e in events if e.timestamp >= config.after_timestamp]
    last = events[-1].timestamp if events else 0.0
    return Session(
        id=session_id,
        app_name=app_name,
        user_id=user_id,
        state=dict(doc.get("state") or {}),
        events=events,
        last_update_time=max(last, doc.get("last_update_time") or 0.0),
    )


def _list(app_name: str, user_id: str | None) -> list[Session]:
    bucket = get_settings().media_bucket
    paths = gcs.list_paths(
        bucket, "sessions/studio-", "sessions/studio-*/adk/session.json"
    )
    found = [
        Session(
            id=doc["id"],
            app_name=app_name,
            user_id=doc["user_id"],
            last_update_time=doc.get("last_update_time") or 0.0,
        )
        for doc in _read_all(paths)
        if _owned(doc, app_name, user_id)
    ]
    return sorted(found, key=lambda s: s.last_update_time)


def _delete(app_name: str, user_id: str, session_id: str) -> None:
    """Delete the session's objects, ``session.json`` last."""
    if not _owned(store.read(_session_path(session_id)), app_name, user_id):
        return
    bucket = get_settings().media_bucket
    paths = gcs.list_paths(bucket, _folder(session_id))
    meta = _session_path(session_id)
    _CACHE.forget(_folder(session_id), meta)
    for path in sorted(paths, key=lambda p: p == meta):
        gcs.delete(_uri(path))


def _save_state(session_id: str, delta: dict[str, Any]) -> None:
    def merged(doc: Any) -> dict[str, Any]:
        if not isinstance(doc, dict):
            raise StorageError(f"no session {session_id}")
        return {**doc, "state": {**(doc.get("state") or {}), **delta}}

    if not store.update(_session_path(session_id), merged):
        raise StorageError(f"could not save the state of {session_id}")


class StudioSessionService(BaseSessionService):
    """ADK sessions in the media bucket (see the module docstring)."""

    def __init__(self, uri: str = f"{SCHEME}://", **_kwargs: Any):
        """``uri`` and ``kwargs`` are what ADK's service registry passes."""
        del uri

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        try:
            sid = store.check_id((session_id or "").strip() or uuid.uuid4().hex)
        except InputError as exc:  # ADK's API answers a ValueError with 400
            raise ValueError(str(exc)) from exc
        doc = _session_doc(sid, app_name, user_id, state or {})
        created = await asyncio.to_thread(_create, _session_path(sid), doc)
        _CACHE.mark(_session_path(sid))
        if not created:
            raise AlreadyExistsError(f"Session {sid} already exists.")
        return Session(
            id=sid,
            app_name=app_name,
            user_id=user_id,
            state=dict(doc["state"]),
            last_update_time=doc["last_update_time"],
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        try:
            store.check_id(session_id)
        except InputError:
            return None  # no studio has that id
        return await asyncio.to_thread(
            _get, app_name, user_id, session_id, config
        )

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        found = await asyncio.to_thread(_list, app_name, user_id)
        return ListSessionsResponse(sessions=found)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        await asyncio.to_thread(_delete, app_name, user_id, session_id)

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event
        event = await super().append_event(session, limits.capped(event))
        session.last_update_time = event.timestamp
        await asyncio.to_thread(_write_event, session.id, event)
        delta = event.actions.state_delta if event.actions else None
        if delta:
            await asyncio.to_thread(_save_state, session.id, dict(delta))
        return event


def register() -> None:
    """Make ``studio://`` session URIs use this service (ADK's registry)."""
    get_service_registry().register_session_service(
        SCHEME, StudioSessionService
    )
