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

"""The cost guards every Studio generation starts through (``start``).

A generation costs money and any MCP client can call the app's job tool,
so the panel, the web app and the agent all start jobs here. Every image
of the spec must be one the studio may use (``job_call.check_refs``:
catalogue images and copies, files of its own runs, its own try-on records
as ``source_run``), and a "Make video" one of its own video try-on images
(``job_call.for_studio``, which also sets what the server derives). The
checks below and the claim are one read-modify-write of
the studio's state (``store.update``, with a generation precondition), so
two starts on any instances never both pass:

- the studio must exist: its state was saved by an action or an agent turn;
- its job must have ended. The claim makes the studio follow the new job at
  once (a ``job_started`` patch). A followed job counts as running while its
  document says so and was saved within ``jobs.JOB_STALE_S`` (a running job
  saves every minute), for at most ``MAX_JOB_S``; before its first save, for
  ``jobs.JOB_STALE_S`` after it was claimed. A crashed instance's job never
  blocks the studio for longer;
- at most ``GENMEDIA_MAX_JOBS_PER_HOUR`` starts (default 20) per studio in a
  rolling hour, their times kept in the state (``job_starts``);
- a "Make video"'s image must still be one of the studio's video try-on
  images (a full run started meanwhile replaces them).

An instance also starts at most ``GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR``
(default 100) jobs per rolling hour, a ceiling for many studios at once.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import threading
import time
import uuid
from typing import Any

from genmedia4commerce.core.errors import BusyError, InputError
from genmedia4commerce.studio import jobs, store
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio.job_call import (
    NOT_AN_IMAGE,
    check_refs,
    for_studio,
    image_of,
)

__all__ = ["MAX_JOB_S", "NOT_STARTED", "RUNNING", "claim", "start"]

logger = logging.getLogger(__name__)

#: A job older than this has hung: Cloud Run ends any request at one hour.
MAX_JOB_S = 3600.0
_HOUR_S = 3600.0
_PER_STUDIO = ("GENMEDIA_MAX_JOBS_PER_HOUR", 20)
_PER_INSTANCE = ("GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR", 100)

RUNNING = (
    "A generation is still running in this studio: wait for it to finish "
    "before starting another one."
)
_NO_STUDIO = "Open the studio and add its inputs before generating."
_TOO_MANY = (
    "This studio has started {n} generations in the last hour, the most "
    "allowed: try again later."
)
_BUSY = "The server is busy with many generations: try again later."
#: A start that failed on the server, details logged (``BusyError``).
NOT_STARTED = "The generation could not be started; try again."

#: When this instance started its jobs of the last hour.
_STARTS: collections.deque[float] = collections.deque()
_STARTS_LOCK = threading.Lock()


class _Refused(Exception):
    """Ends a ``store.update`` without writing (``claim``)."""


def _limit(env: tuple[str, int]) -> int:
    """A positive whole number from the environment, else its default."""
    name, default = env
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "%s=%r is not a positive integer: using %s", name, raw, default
        )
        return default
    return value


def _running(st: dict[str, Any], starts: list[float], now: float) -> bool:
    """Whether the studio's followed job is still running (``starts``: the
    studio's recent start times)."""
    job_id = rules.job_id_of(st)
    if not (isinstance(job_id, str) and job_id):
        return False
    job = store.read(store.job_path(job_id))
    if not isinstance(job, dict):  # not saved yet: only just claimed
        return bool(starts) and now - max(starts) < jobs.JOB_STALE_S
    if job.get("status") != "running":
        return False
    seen = job.get("updated_at") or job.get("started") or 0
    begun = job.get("started") or seen
    return now - seen <= jobs.JOB_STALE_S and now - begun < MAX_JOB_S


def claim(
    studio_id: str, job_id: str, capability: str, tryon_image: str = ""
) -> None:
    """Make the saved studio follow a new job, after the checks above
    (``tryon_image``: the image of a "Make video", whose start clears no
    result).

    Raises ``InputError`` (with a message for the user) when the studio was
    never saved, its job is running, it started too many or ``tryon_image``
    is not one of its video try-on images; ``BusyError``
    (also with one) when its state or job could not be read or saved.
    """
    refused: list[str] = []
    limit = _limit(_PER_STUDIO)

    def change(doc: Any) -> dict[str, Any]:
        refused.clear()  # ``store.update`` may call it again
        st = doc.get("state") if isinstance(doc, dict) else None
        if not isinstance(st, dict):
            refused.append(_NO_STUDIO)
            raise _Refused(_NO_STUDIO)
        now = time.time()
        starts = [
            t
            for t in st.get("job_starts") or []
            if isinstance(t, int | float) and 0 <= now - t < _HOUR_S
        ]
        if _running(st, starts, now):
            refused.append(RUNNING)
        elif len(starts) >= limit:
            refused.append(_TOO_MANY.format(n=limit))
        elif tryon_image:
            rules.upgrade(st)  # an older studio's image, as on read
            if image_of(st, tryon_image) is None:
                refused.append(NOT_AN_IMAGE)
        if refused:
            raise _Refused(refused[0])
        job = {"job_id": job_id, "capability": capability}
        job |= {"tryon_image": tryon_image} if tryon_image else {}
        rules.apply_patch(st, {"job_started": job})
        st["job_starts"] = [*starts, now]
        return {"state": st, "updated_at": now}

    saved = store.update(store.state_path(studio_id), change)
    if refused:
        raise InputError(refused[0])
    if not saved:
        logger.warning("Studio %s: job %s not claimed", studio_id, job_id)
        raise BusyError(NOT_STARTED)


def _take_slot(now: float) -> None:
    """Count a start on this instance, or raise ``InputError`` at the
    limit."""
    limit = _limit(_PER_INSTANCE)
    with _STARTS_LOCK:
        while _STARTS and now - _STARTS[0] >= _HOUR_S:
            _STARTS.popleft()
        if len(_STARTS) >= limit:
            logger.warning("Studio: %s jobs started this hour", limit)
            raise InputError(_BUSY)
        _STARTS.append(now)


def _give_back_slot(now: float) -> None:
    """Uncount a start that was refused."""
    with _STARTS_LOCK, contextlib.suppress(ValueError):
        _STARTS.remove(now)


async def start(spec: dict[str, Any]) -> dict[str, Any]:
    """Start a generation after the cost guards: ``{job_id, status,
    capability}``. Raises ``InputError`` for a bad spec or a refused start,
    ``BusyError`` when the studio could not be read or saved, ``StorageError``
    when another stored document could not be read."""
    cap = spec.get("capability")
    if cap not in jobs.CAPABILITIES:
        raise InputError("capability must be tryon, video_tryon or spin")
    studio_id = str(spec.get("studio_id") or "")
    if not studio_id:
        raise InputError(_NO_STUDIO)
    remake = cap == "video_tryon" and bool(spec.get("tryon_image"))
    st = await asyncio.to_thread(ss.get_state, studio_id) if remake else {}
    spec = for_studio(spec, st)
    await asyncio.to_thread(check_refs, spec, studio_id)
    job_id, now = uuid.uuid4().hex[:10], time.time()
    image = str(spec.get("tryon_image") or "")
    _take_slot(now)
    try:
        await asyncio.to_thread(claim, studio_id, job_id, cap, image)
    except BaseException:
        _give_back_slot(now)
        raise
    return jobs.start(spec, job_id)
