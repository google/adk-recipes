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

"""Per-session registry of background process handles.

Held in a module-level store keyed by the ADK session id so live handles
survive across conversation turns — ``temp:`` state is invocation-scoped
and wiped between turns, which would orphan ``proc_…`` handles even though
the OS/container process keeps running. The registry holds live
ProcessHandle instances which aren't JSON serializable, so it can't live
in the persisted state delta. Caps total entries (LRU evicts oldest
finished) and TTLs finished sessions so the model can still inspect recent
results without unbounded memory growth.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from app.environment.process import ProcessHandle

logger = logging.getLogger(__name__)

REGISTRY_STATE_KEY = "temp:process_registry"
DEFAULT_MAX_SESSIONS = 32
DEFAULT_FINISHED_TTL_S = 600.0  # 10 min
DEFAULT_MAX_SESSION_REGISTRIES = 128

# Keyed by ADK session id so handles survive turn boundaries. Mirrors the
# module-level _env_cache in app/conversation/session_start.py.
_SESSION_REGISTRIES: OrderedDict[str, ProcessRegistry] = OrderedDict()


def reset_session_registries() -> None:
    _SESSION_REGISTRIES.clear()


def session_key_from_context(tool_context: Any) -> str | None:
    inv = getattr(tool_context, "_invocation_context", None)
    session = getattr(inv, "session", None)
    session_id = getattr(session, "id", None)
    return str(session_id) if session_id is not None else None


def resolve_registry(tool_context: Any | None) -> ProcessRegistry:
    key = (
        session_key_from_context(tool_context)
        if tool_context is not None
        else None
    )
    if key is not None:
        return ProcessRegistry.for_session(key)
    # temp: state is wiped between turns; a real caller landing here would
    # silently orphan the spawned handle, so make the fallback observable.
    logger.debug(
        "processes: no session key; falling back to temp: state registry"
    )
    state = (
        getattr(tool_context, "state", None)
        if tool_context is not None
        else None
    )
    return ProcessRegistry.for_state(state if state is not None else {})


class ProcessRegistry:
    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        finished_ttl_s: float = DEFAULT_FINISHED_TTL_S,
    ) -> None:
        self._max = max_sessions
        self._ttl = finished_ttl_s
        # Insertion order = age for LRU eviction.
        self._handles: OrderedDict[str, ProcessHandle] = OrderedDict()

    @classmethod
    def for_state(cls, state: Any) -> ProcessRegistry:
        existing = state.get(REGISTRY_STATE_KEY) if state is not None else None
        if isinstance(existing, cls):
            return existing
        reg = cls()
        if state is not None:
            state[REGISTRY_STATE_KEY] = reg
        return reg

    @classmethod
    def for_session(cls, session_key: str) -> ProcessRegistry:
        # ADK serializes turns within a session, so a single writer touches a
        # given key at a time — no lock needed around this read-then-create.
        reg = _SESSION_REGISTRIES.get(session_key)
        if reg is None:
            reg = cls()
        _SESSION_REGISTRIES[session_key] = reg
        _SESSION_REGISTRIES.move_to_end(session_key)
        cls._prune_session_registries()
        return reg

    @staticmethod
    def _prune_session_registries() -> None:
        # Never drop a registry with a live handle, and never drop the
        # just-requested key (always most-recent) — evicting it would orphan a
        # handle the caller is about to register. Accept overage otherwise.
        while len(_SESSION_REGISTRIES) > DEFAULT_MAX_SESSION_REGISTRIES:
            protected = next(
                reversed(_SESSION_REGISTRIES)
            )  # most-recently moved
            evicted = False
            for key, reg in list(_SESSION_REGISTRIES.items()):
                if key == protected:
                    continue
                if not any(h.is_running for h in reg._handles.values()):
                    del _SESSION_REGISTRIES[key]
                    evicted = True
                    break
            if not evicted:
                return

    def register(self, handle: ProcessHandle) -> None:
        self._handles[handle.session_id] = handle
        self._enforce_cap()

    def get(self, session_id: str) -> ProcessHandle | None:
        self.gc()
        return self._handles.get(session_id)

    def list_running(self) -> list[ProcessHandle]:
        self.gc()
        return [h for h in self._handles.values() if h.is_running]

    def list_finished(self) -> list[ProcessHandle]:
        self.gc()
        return [h for h in self._handles.values() if not h.is_running]

    def all(self) -> list[ProcessHandle]:
        self.gc()
        return list(self._handles.values())

    def gc(self) -> None:
        now = time.time()
        expired: list[str] = []
        for sid, h in self._handles.items():
            if not h.is_running and h.finished_at is not None:
                if now - h.finished_at > self._ttl:
                    expired.append(sid)
        for sid in expired:
            self._handles.pop(sid, None)

    def _enforce_cap(self) -> None:
        if len(self._handles) <= self._max:
            return
        # Evict oldest finished first.
        for sid in list(self._handles.keys()):
            if len(self._handles) <= self._max:
                return
            if not self._handles[sid].is_running:
                self._handles.pop(sid, None)
        # If still over cap, accept the overage rather than killing live
        # processes — registry is advisory; we never SIGKILL behind the
        # model's back.


def all_handles() -> list[ProcessHandle]:
    """Every live handle across all session registries — the local-dev source
    of truth for the /lha/processes read (prod uses the sandbox shim)."""
    out: list[ProcessHandle] = []
    for reg in list(_SESSION_REGISTRIES.values()):
        out.extend(reg.all())
    return out


def find_handle(session_id: str) -> ProcessHandle | None:
    for reg in list(_SESSION_REGISTRIES.values()):
        h = reg.get(session_id)
        if h is not None:
            return h
    return None
