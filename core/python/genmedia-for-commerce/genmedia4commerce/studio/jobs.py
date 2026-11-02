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

"""Background generation jobs for the Studio MCP app.

A generation takes minutes, so the app (or the agent) starts a job, through
the cost guards of ``studio.quota``, and the app polls its status with
short ``tools/call`` round trips. The job runs the workflow's event stream
(``tools.stream``) on the instance that started it and rewrites its
document in the media bucket (``studio.store``) as events arrive: the
progress line, each finished variation, and for a video try-on from photos
the try-on images (``image_results``) and those it animates (``images``,
each with ``passed`` and its back view); each result carries the URI of
its run record (``run``) and each video the image it was made from
(``source``). A "Make video" job (``tryon_image``) animates one of them
again. A job with ``refresh_cache: true`` (the "Try again" of a failed
one) recomputes the preprocessing the cache keeps (``core.cache``). Any
instance can answer a status poll. Each job writes its inputs when it starts
and its outputs when it ends to the studio's history, with their ``gs://``
URIs, so the agent can reuse them.

Every save stamps ``updated_at``, and a running job saves at least every
minute, so a job whose instance stopped (a deploy, a scale-in) is reported
as interrupted instead of running forever. A failure the user cannot act on
is logged and reported with a generic message (``errors.user_message``),
never with its own text.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from genmedia4commerce import tools
from genmedia4commerce.core import cache
from genmedia4commerce.core.errors import (
    USER_ERRORS,
    StorageError,
    user_message,
)
from genmedia4commerce.core.events import (
    CompleteEvent,
    ErrorEvent,
    Event,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio import store
from genmedia4commerce.studio.events import ended_note, started_note
from genmedia4commerce.studio.job_call import refresh_cache, tool_call

__all__ = [
    "CAPABILITIES",
    "INTERRUPTED",
    "JOB_STALE_S",
    "interrupt_local",
    "start",
    "status",
]

logger = logging.getLogger(__name__)

CAPABILITIES = ("tryon", "video_tryon", "spin")
#: A running job saves at least this often, even without events.
_HEARTBEAT_S = 60.0
#: Progress-only changes are saved at most this often.
_PROGRESS_S = 1.0
#: A stored running job not saved for this long has lost its instance.
JOB_STALE_S = 300.0
INTERRUPTED = (
    "interrupted: the server running this generation stopped; start it again"
)
#: Tries of a job's last write, and the wait before the second.
_FINAL_TRIES = 3
_FINAL_BACKOFF_S = 0.5
_KEEP_S = 6 * 3600


@dataclass
class _Run:
    """A job running on this instance: its live document and its saves."""

    job_id: str
    job: dict[str, Any]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    saved_at: float = 0.0
    dirty: bool = False  # changed since the last save


#: Jobs started on this instance (the store has every job): a copy of
#: each, replaced on every change, so a status poll (in a worker thread)
#: never reads a document being changed.
_LOCAL: dict[str, dict[str, Any]] = {}
_RUNS: dict[str, _Run] = {}
_TASKS: set[asyncio.Task] = set()


def _result(v: dict[str, Any], video: bool) -> dict[str, Any] | None:
    """One variation for the app and the history; ``None`` without media."""
    uri = v.get("media_uri") or ""
    if not uri:
        return None
    return {
        "index": v.get("index"),
        "status": v.get("status"),
        "score": v.get("score"),
        "uri": uri,
        "url": sm.preview_url(uri, video=video),
        "note": (v.get("verdict") or {}).get("description", ""),
        "view": (v.get("extra") or {}).get("view") or "front",
        "fixed": bool(v.get("fixed")),
    }


def _ranked(results: list[dict], order: list[int], best: Any) -> list[dict]:
    """``results`` in the judge's order (unranked ones keep theirs, last),
    the back views, drawn from the best front, right after it: the judge
    ranks the fronts only, and fronts and back views are numbered apart."""
    rank = {i: n for n, i in enumerate(order)}
    backs = [r for r in results if r.get("view") == "back"]
    fronts = sorted(
        (r for r in results if r.get("view") != "back"),
        key=lambda r: rank.get(r["index"], len(rank)),
    )
    at = next(
        (n + 1 for n, r in enumerate(fronts) if r["index"] == best),
        len(fronts),
    )
    return [*fronts[:at], *backs, *fronts[at:]]


def _views(job: dict[str, Any], view: str) -> dict[Any, dict[str, Any]]:
    """The try-on images of one view by index (fronts and back views are
    numbered apart)."""
    return {
        r["index"]: r
        for r in job["image_results"]
        if (r.get("view") == "back") == (view == "back")
    }


def _animated(job: dict[str, Any], data: dict[str, Any]) -> None:
    """Record a try-on image a video try-on animates (``image_selected``):
    in ``images`` (``passed``: it passed its checks; ``back``: its back
    view)."""
    front = _views(job, "front").get(data.get("front_index"))
    back = _views(job, "back").get(data.get("back_index")) or {}
    if front is None:
        return
    passed = front.get("status") == "ready"
    image = {**front, "passed": passed, "back": back.get("uri") or ""}
    image.pop("view", None)
    job.setdefault("images", []).append(image)


def _source(job: dict[str, Any], index: Any) -> str:
    """The try-on image a video was made from: a "Make video"'s image, or
    the front of ``extra.source_index``."""
    if job.get("tryon_image"):
        return str(job["tryon_image"])
    return (_views(job, "front").get(index) or {}).get("uri", "")


async def _apply(job: dict[str, Any], event: Event, video: bool) -> bool:
    """Fold one event into the job; whether the job changed."""
    if isinstance(event, ProgressEvent) and event.stage == "record":
        # The run record: each result names it (``run``), for the video of
        # a try-on image to reuse (a "Make video": ``job_call``).
        job["run"] = (event.data or {}).get("manifest") or ""
        return False
    if isinstance(event, ProgressEvent):
        job["progress"] = event.message
        # A video try-on animates every passing image; each video names
        # its own (``source``).
        if event.stage == "image_selected":
            _animated(job, event.data or {})
        return True
    if isinstance(event, VariationEvent):
        image_stage = event.extra.get("stage") == "image"
        key = "image_results" if image_stage else "results"
        # Signing the preview URL is a network call: off the event loop.
        item = await asyncio.to_thread(
            _result, event.to_dict(), video and not image_stage
        )
        source = _source(job, event.extra.get("source_index"))
        if item and not image_stage and source:
            item["source"] = source
        if item and job.get("run"):
            item["run"] = job["run"]
        if item:
            job[key].append(item)
        return item is not None
    if isinstance(event, CompleteEvent):
        job["results"] = _ranked(
            job["results"], event.ranked_indices, event.best_index
        )
        return True
    if isinstance(event, ErrorEvent):
        job["error"] = event.message
        return True
    return False


def _publish(run: _Run) -> dict[str, Any]:
    """Replace this instance's copy of the job; return the new copy."""
    snapshot = _LOCAL[run.job_id] = copy.deepcopy(run.job)
    return snapshot


async def _save(run: _Run) -> bool:
    """Write the job as it is now, after any save in progress."""
    async with run.lock:
        run.job["updated_at"] = run.saved_at = time.time()
        run.dirty = False
        doc = _publish(run)
        path = store.job_path(run.job_id)
        return await asyncio.to_thread(store.write, path, doc)


async def _save_final(run: _Run) -> None:
    """The job's last write, tried ``_FINAL_TRIES`` times."""
    for attempt in range(_FINAL_TRIES):
        if await _save(run):
            return
        if attempt + 1 < _FINAL_TRIES:
            await asyncio.sleep(_FINAL_BACKOFF_S * (attempt + 1))
    logger.error("Studio job %s: its final state was not saved", run.job_id)


async def _keep_saved(run: _Run) -> None:
    """Save throttled progress within a second, and every minute anyway."""
    while True:
        await asyncio.sleep(_PROGRESS_S)
        if run.dirty or time.time() - run.saved_at >= _HEARTBEAT_S:
            await _save(run)


async def _generate(run: _Run, spec: dict[str, Any]) -> None:
    """Run the workflow, saving the job as its events arrive: progress at
    most once a second, results and the end at once."""
    job, video = run.job, spec.get("capability") != "tryon"
    name, kwargs = await asyncio.to_thread(tool_call, spec)
    with cache.refreshing(refresh_cache(spec)):
        events = await tools.stream(name, **kwargs)
        async with contextlib.aclosing(events):
            async for event in events:
                if not await _apply(job, event, video):
                    continue
                if isinstance(event, ProgressEvent):
                    run.dirty = True
                    _publish(run)
                else:
                    await _save(run)
    if job["status"] != "running":  # interrupted meanwhile
        return
    if job.get("error"):
        job["status"] = "error"
    elif job["results"]:
        job["status"] = "done"
    else:
        job.update(status="error", error="No result came out. Try again.")


async def _finish(run: _Run, sid: str, cap: str) -> None:
    """Save the ended job, then its results and a note in the history."""
    job = run.job
    job["finished"] = job.get("finished") or time.time()
    await _save_final(run)
    _RUNS.pop(run.job_id, None)
    # Its results go into the studio state (failed jobs stop being
    # followed); the note is written even if that fails.
    try:
        await asyncio.to_thread(ss.finish_job, sid, run.job_id, job)
    except Exception:
        logger.exception("Studio job %s: not recorded", run.job_id)
    await asyncio.to_thread(sh.note, sid, ended_note(cap, job))


async def _run(run: _Run, spec: dict[str, Any]) -> None:
    job, sid = run.job, str(spec.get("studio_id") or "")
    cap = str(spec.get("capability"))
    keeper = asyncio.create_task(_keep_saved(run))
    try:
        await _save(run)  # first, so a poll on another instance finds it
        await asyncio.to_thread(sh.note, sid, started_note(spec))
        await _generate(run, spec)
    except asyncio.CancelledError:
        job.update(status="error", error=INTERRUPTED)
        raise
    except Exception as exc:  # storage and config errors can name paths
        if not isinstance(exc, USER_ERRORS):
            logger.exception("Studio job %s failed", run.job_id)
        job.update(status="error", error=user_message(exc))
    finally:
        keeper.cancel()
        await _finish(run, sid, cap)


def start(spec: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Start a checked generation as job ``job_id`` at once (no I/O: the
    job is saved by its task). Only ``quota.start`` calls it, after the
    cost guards."""
    now = time.time()
    for jid, v in list(_LOCAL.items()):
        if jid not in _RUNS and now - v["started"] > _KEEP_S:
            _LOCAL.pop(jid, None)
    cap = spec.get("capability")
    run = _RUNS[job_id] = _Run(
        job_id,
        {
            "status": "running",
            "capability": cap,
            "studio_id": str(spec.get("studio_id") or ""),
            "started": now,
            "updated_at": now,
            "progress": "Starting",
            "results": [],
            "image_results": [],
            "images": [],
            "tryon_image": str(spec.get("tryon_image") or ""),
        },
    )
    _publish(run)
    task = asyncio.get_running_loop().create_task(_run(run, spec))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"job_id": job_id, "status": "running", "capability": cap}


async def interrupt_local() -> int:
    """Mark the jobs running on this instance as interrupted and save them,
    for the app's shutdown; return how many there were."""
    runs = [r for r in _RUNS.values() if r.job["status"] == "running"]
    for run in runs:
        run.job.update(status="error", error=INTERRUPTED, finished=time.time())
    await asyncio.gather(*(_save_final(run) for run in runs))
    return len(runs)


def _stored(job_id: str) -> dict[str, Any] | None:
    """The job as saved, reported interrupted when its instance stopped
    saving it."""
    job = store.read(store.job_path(job_id))
    if not isinstance(job, dict):
        return None
    seen = job.get("updated_at") or job.get("started") or 0
    if job.get("status") == "running" and time.time() - seen > JOB_STALE_S:
        job.update(status="error", error=INTERRUPTED, finished=seen)
    return job


def status(job_id: str) -> dict[str, Any]:
    """The job's status, progress, elapsed seconds and the results so far
    (a copy)."""
    job = copy.deepcopy(_LOCAL.get(job_id))
    if job is None:
        try:
            job = _stored(job_id)
        except StorageError as exc:  # maybe brief: the app polls again
            logger.warning("Studio job %s: %s", job_id, exc)
            return {"job_id": job_id, "status": "running"}
    if job is None:
        return {"job_id": job_id, "status": "unknown"}
    if job.get("finished"):  # read back later: its signed URLs may have expired
        video = job.get("capability") != "tryon"
        sm.refresh_urls(job.get("results") or [], video=video)
        sm.refresh_urls(job.get("image_results") or [], video=False)
        sm.refresh_urls(job.get("images") or [], video=False)
    end = job.get("finished") or time.time()
    return {
        "job_id": job_id,
        "elapsed_s": round(end - (job.get("started") or end)),
        **{k: v for k, v in job.items() if k not in ("started", "finished")},
    }
