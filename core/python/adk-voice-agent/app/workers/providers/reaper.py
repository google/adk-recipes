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

"""Idle computer shutdown (`reap_once`), warm-hours startup (`warm_once`), and voice session pre-warming."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

from app.app_utils.iso_time import parse_iso8601
from app.store.user_settings import get_user_settings_store
from app.user_settings import TIMEZONE
from app.voice.preferences import resolve_timezone
from app.workers.providers.base import (
    ComputerProvider,
    ComputerState,
    IdleStoppable,
)
from app.workers.providers.common import idle_stop_minutes, owner_id
from app.workers.providers.last_known import (
    ensure_recorded,
    last_known_status,
    record_probe,
    record_status,
)

logger = logging.getLogger(__name__)

REAP_INTERVAL_S = 60.0
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WARM_HOURS = "08:00-18:00"
DEFAULT_WARM_DAYS = "mon-fri"
_OFF = frozenset({"off", "none", "0", "false", "no"})


@dataclass(frozen=True)
class WarmSchedule:
    start: clock
    end: clock
    days: frozenset[int]  # Monday = 0

    def opened_on(self, now: datetime, tz: ZoneInfo) -> date | None:
        """Return the local date the active warm window opened on, or None outside it."""
        local = now.astimezone(tz)
        at = local.time()
        if self.start < self.end:
            inside = self.start <= at < self.end
            opened = local.date()
        elif at >= self.start:
            inside, opened = True, local.date()
        else:
            inside, opened = at < self.end, local.date() - timedelta(days=1)
        return opened if inside and opened.weekday() in self.days else None


def _parse_days(spec: str) -> frozenset[int]:
    days: set[int] = set()
    for part in spec.lower().replace(" ", "").split(","):
        first, _, last = part.partition("-")
        lo, hi = _DAYS.index(first), _DAYS.index(last or first)
        days.update(d % 7 for d in range(lo, hi + 1 if hi >= lo else hi + 8))
    return frozenset(days)


def warm_schedule() -> WarmSchedule | None:
    spec = (os.getenv("COMPUTER_WARM_HOURS") or DEFAULT_WARM_HOURS).strip()
    if spec.lower() in _OFF:
        return None
    try:
        first, last = (clock.fromisoformat(p.strip()) for p in spec.split("-"))
        if first == last:
            raise ValueError("the window is empty")
        days = _parse_days(os.getenv("COMPUTER_WARM_DAYS") or DEFAULT_WARM_DAYS)
    except ValueError:
        logger.warning(
            "Ignoring the computer warm hours: COMPUTER_WARM_HOURS=%r, "
            "COMPUTER_WARM_DAYS=%r",
            spec,
            os.getenv("COMPUTER_WARM_DAYS"),
            exc_info=True,
        )
        return None
    return WarmSchedule(first, last, days)


class WarmZones:
    """Per-owner timezones resolved from saved user settings or `COMPUTER_TIMEZONE`."""

    def __init__(self, saved: Mapping[str, object]) -> None:
        self._saved = {owner_id(user): zone for user, zone in saved.items()}

    @classmethod
    async def load(cls) -> WarmZones:
        try:
            saved = await get_user_settings_store().load_key(TIMEZONE)
        except Exception:
            logger.warning(
                "Couldn't read users' timezones for the warm hours",
                exc_info=True,
            )
            saved = {}
        return cls(saved)

    def zone(self, owner: str) -> ZoneInfo:
        return resolve_timezone(
            self._saved.get(owner_id(owner)), os.getenv("COMPUTER_TIMEZONE")
        )[1]


def _window_opened(
    schedule: WarmSchedule | None, zones: WarmZones, owner: str, now: datetime
) -> date | None:
    return schedule.opened_on(now, zones.zone(owner)) if schedule else None


class LiveSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, set[asyncio.Task[object]]] = {}

    def open(self, user_id: str, session: asyncio.Task[object]) -> None:
        owner = owner_id(user_id)
        sessions = self._sessions.setdefault(owner, set())
        sessions.add(session)
        session.add_done_callback(lambda done: self._close(owner, done))

    def _close(self, owner: str, session: asyncio.Task[object]) -> None:
        sessions = self._sessions.get(owner)
        if sessions is not None:
            sessions.discard(session)
            if not sessions:
                self._sessions.pop(owner, None)

    def has_live(self, user_id: str) -> bool:
        return bool(self._sessions.get(owner_id(user_id)))


_live_sessions = LiveSessions()
_prewarms: set[asyncio.Task[None]] = set()


async def _prewarm() -> None:
    from app.workers.factory import resolve_environment

    try:
        await resolve_environment()
    except Exception:
        logger.warning("Pre-warming the computer failed", exc_info=True)


def voice_session_opened(
    user_id: str, session: asyncio.Task[object] | None
) -> None:
    """Record a live voice session and pre-warm the user's computer in the background."""
    try:
        if session is not None:
            _live_sessions.open(user_id, session)
        task = asyncio.create_task(_prewarm(), name="computer-prewarm")
        _prewarms.add(task)
        task.add_done_callback(_prewarms.discard)
    except Exception:
        logger.warning(
            "Could not schedule the computer pre-warm", exc_info=True
        )


def _is_idle(last_activity: str, now: datetime, idle: timedelta) -> bool:
    when = parse_iso8601(last_activity)
    return when is None or now - when >= idle


_unreachable_since: dict[str, datetime] = {}


async def reap_once(
    provider: ComputerProvider,
    *,
    live: LiveSessions | None = None,
    now: datetime | None = None,
    zones: WarmZones | None = None,
    unreachable: dict[str, datetime] | None = None,
) -> list[str]:
    """Stop idle or persistently unreachable computers outside warm hours and live sessions."""
    if not isinstance(provider, IdleStoppable) or idle_stop_minutes() <= 0:
        return []
    from app.workers.factory import invalidate_environment

    live = live or _live_sessions
    now = now or datetime.now(UTC)
    unreachable = unreachable if unreachable is not None else _unreachable_since
    schedule = warm_schedule()
    if schedule and zones is None:
        zones = await WarmZones.load()
    idle = timedelta(minutes=idle_stop_minutes())
    stopped: list[str] = []
    for owner in await provider.known_users():
        if live.has_live(owner):
            continue
        if zones and _window_opened(schedule, zones, owner, now):
            continue
        try:
            env = await provider.running_environment(owner)
            if env is None:
                current = last_known_status(owner)
                if (
                    current is not None
                    and current.state is ComputerState.STARTING
                ):
                    first_seen = unreachable.setdefault(owner, now)
                    if now - first_seen >= idle:
                        logger.info(
                            "Stopping unreachable computer for %s", owner
                        )
                        await provider.stop(owner)
                        record_status(owner, ComputerState.STOPPED)
                        invalidate_environment(owner)
                        unreachable.pop(owner, None)
                        stopped.append(owner)
                else:
                    unreachable.pop(owner, None)
                    record_probe(owner, None)
                continue
            record_probe(owner, env)
            try:
                activity = await env.activity()
            except Exception:
                first_seen = unreachable.setdefault(owner, now)
                if now - first_seen >= idle:
                    logger.info("Stopping unreachable computer for %s", owner)
                    await provider.stop(owner)
                    record_status(owner, ComputerState.STOPPED)
                    invalidate_environment(owner)
                    unreachable.pop(owner, None)
                    stopped.append(owner)
                else:
                    logger.warning(
                        "Idle check for %s's computer failed",
                        owner,
                        exc_info=True,
                    )
                continue
            unreachable.pop(owner, None)
            if activity.active_runs or not _is_idle(
                activity.last_activity, now, idle
            ):
                continue
            logger.info("Stopping idle computer for %s", owner)
            await provider.stop(owner)
            record_status(owner, ComputerState.STOPPED)
            invalidate_environment(owner)
            stopped.append(owner)
        except Exception:
            logger.warning(
                "Idle check for %s's computer failed", owner, exc_info=True
            )
    return stopped


def reaper_enabled(provider: ComputerProvider) -> bool:
    return isinstance(provider, IdleStoppable) and (
        idle_stop_minutes() > 0 or warm_schedule() is not None
    )


_warmed: dict[str, date] = {}
_warmups: set[asyncio.Task[None]] = set()


async def _warm(provider: ComputerProvider, owner: str) -> None:
    try:
        await ensure_recorded(provider, owner)
    except Exception:
        logger.warning("Warming %s's computer failed", owner, exc_info=True)


async def warm_once(
    provider: ComputerProvider,
    *,
    now: datetime | None = None,
    warmed: dict[str, date] | None = None,
    zones: WarmZones | None = None,
) -> list[str]:
    """Start each stopped computer once per warm-hours window in the background."""
    schedule = warm_schedule()
    if not isinstance(provider, IdleStoppable) or schedule is None:
        return []
    now = now or datetime.now(UTC)
    zones = zones or await WarmZones.load()
    warmed = _warmed if warmed is None else warmed
    starting: list[str] = []
    for owner in await provider.known_users():
        opened = _window_opened(schedule, zones, owner, now)
        if opened is None or warmed.get(owner) == opened:
            continue
        try:
            running = await provider.running_environment(owner)
        except Exception:
            logger.warning(
                "Warm check for %s's computer failed", owner, exc_info=True
            )
            continue
        warmed[owner] = opened
        record_probe(owner, running)
        if running is not None:
            continue
        logger.info("Starting %s's computer for the warm hours", owner)
        task = asyncio.create_task(_warm(provider, owner), name="computer-warm")
        _warmups.add(task)
        task.add_done_callback(_warmups.discard)
        starting.append(owner)
    return starting


async def reaper_loop(
    provider: ComputerProvider, *, interval_s: float = REAP_INTERVAL_S
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            zones = await WarmZones.load() if warm_schedule() else None
            await reap_once(provider, zones=zones)
            await warm_once(provider, zones=zones)
        except Exception:
            logger.warning("The idle reaper failed", exc_info=True)
