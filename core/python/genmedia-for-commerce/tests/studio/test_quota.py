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

"""The cost guards of Studio generations (``studio.quota``) and what a job
tells its clients when it fails (``studio.jobs``)."""

from __future__ import annotations

import asyncio
import collections
import threading
import time
from typing import Any

import pytest

from genmedia4commerce import tools
from genmedia4commerce.core.errors import (
    SERVER_FAILED,
    BusyError,
    ConfigError,
    InputError,
    StorageError,
)
from genmedia4commerce.core.events import ProgressEvent
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import jobs, quota, service, store
from genmedia4commerce.studio import state as ss


@pytest.fixture(autouse=True)
def _bucket(storage, monkeypatch: pytest.MonkeyPatch):
    """Studios and jobs in the fake bucket, no local jobs, no notes, no
    limits from the environment."""
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(jobs, "_LOCAL", {})
    monkeypatch.setattr(jobs, "_RUNS", {})
    monkeypatch.setattr(jobs, "_FINAL_BACKOFF_S", 0)
    monkeypatch.setattr(quota, "_STARTS", collections.deque())
    monkeypatch.setattr(sh, "note", lambda studio_id, text: True)
    monkeypatch.delenv("GENMEDIA_MAX_JOBS_PER_HOUR", raising=False)
    monkeypatch.delenv("GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR", raising=False)
    return storage


def _studio(studio_id: str, **state: Any) -> None:
    """A studio the app saved."""
    doc = {"state": {"capability": "spin", **state}, "updated_at": 1.0}
    store.write(store.state_path(studio_id), doc)


def _state(studio_id: str) -> dict[str, Any]:
    return store.read(store.state_path(studio_id))["state"]


def _job(job_id: str, status: str, started: float, seen: float) -> None:
    """A job document as a job saves it."""
    doc = {"status": status, "started": started, "updated_at": seen}
    store.write(store.job_path(job_id), doc)


def _spin(studio_id: str) -> dict[str, Any]:
    """A spin of one upload of the studio."""
    bucket = get_settings().media_bucket
    run = f"runs/2026-01-02/studio-{studio_id}/0123456789ab"
    ref = f"gs://{bucket}/{run}/inputs/upload-a.png"
    return {"studio_id": studio_id, "capability": "spin", "spin": [ref]}


@pytest.fixture
def blocked(monkeypatch: pytest.MonkeyPatch):
    """Jobs whose stream waits for the returned event."""
    gate = asyncio.Event()

    async def events():
        yield ProgressEvent("preprocessed", "Generating")
        await gate.wait()

    async def fake_stream(tool: str, **kwargs: Any):
        del tool, kwargs
        return events()

    monkeypatch.setattr(tools, "stream", fake_stream)
    yield gate
    gate.set()


async def _until(check, tries: int = 300) -> None:
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


# ---- one job at a time ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_start_is_refused_while_the_first_runs(blocked):
    _studio("s1")
    first = await service.job_start(_spin("s1"))
    # The studio follows the job at once, before any client says so.
    assert _state("s1")["job"] == {
        "job_id": first["job_id"],
        "capability": "spin",
    }
    with pytest.raises(InputError, match="still running"):
        await service.job_start(_spin("s1"))
    _studio("s2")  # another studio is not blocked
    assert (await service.job_start(_spin("s2")))["status"] == "running"
    blocked.set()
    await _until(lambda: not jobs._RUNS)
    # Ended (and recorded by the job): the studio can start again.
    assert _state("s1")["job"] is None
    assert (await service.job_start(_spin("s1")))["status"] == "running"
    assert len(jobs._LOCAL) == 3


def test_concurrent_claims_start_one_job() -> None:
    _studio("s1")
    barrier, outcomes = threading.Barrier(8), []

    def claim(n: int) -> None:
        barrier.wait()
        try:
            quota.claim("s1", f"j{n}", "spin")
            outcomes.append(f"j{n}")
        except InputError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [o for o in outcomes if o.startswith("j")]
    assert len(won) == 1 and outcomes.count(quota.RUNNING) == 7
    assert _state("s1")["job"]["job_id"] == won[0]
    assert len(_state("s1")["job_starts"]) == 1


@pytest.mark.asyncio
async def test_concurrent_starts_start_one_job(blocked) -> None:
    del blocked
    _studio("s1")
    results = await asyncio.gather(
        *(service.job_start(_spin("s1")) for _ in range(4)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    refused = [r for r in results if isinstance(r, InputError)]
    assert len(refused) == 3 and len(jobs._RUNS) == 1


@pytest.mark.parametrize(
    ("job", "running"),
    [
        (None, False),  # the job of a start an hour ago was never saved
        ("done", False),
        ("error", False),
        ("fresh", True),
        ("stale", False),  # its instance stopped saving it
        ("hung", False),  # saved but older than any request may run
    ],
)
def test_a_followed_job_blocks_only_while_it_runs(job, running) -> None:
    now = time.time()
    docs = {
        "done": ("done", now - 60, now - 10),
        "error": ("error", now - 60, now - 10),
        "fresh": ("running", now - 60, now - 10),
        "stale": ("running", now - 900, now - jobs.JOB_STALE_S - 1),
        "hung": ("running", now - quota.MAX_JOB_S - 1, now - 10),
    }
    if job:
        _job("old", *docs[job])
    _studio("s1", job={"job_id": "old", "capability": "spin"}, job_starts=[])
    if running:
        with pytest.raises(InputError, match="still running"):
            quota.claim("s1", "new", "spin")
    else:
        quota.claim("s1", "new", "spin")
        assert _state("s1")["job"]["job_id"] == "new"


def test_a_job_claimed_but_not_saved_yet_blocks_for_a_while() -> None:
    now = time.time()
    job = {"job_id": "old", "capability": "spin"}
    _studio("s1", job=job, job_starts=[now - 5])
    with pytest.raises(InputError, match="still running"):
        quota.claim("s1", "new", "spin")
    _studio("s1", job=job, job_starts=[now - jobs.JOB_STALE_S - 1])
    quota.claim("s1", "new", "spin")


# ---- the studio must exist -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_studio_never_saved_cannot_start_a_job(storage) -> None:
    for studio_id in ("nobody", ""):
        with pytest.raises(InputError, match="add its inputs"):
            await service.job_start(_spin(studio_id))
    assert not jobs._RUNS and not quota._STARTS
    assert store.read(store.state_path("nobody")) is None
    with pytest.raises(InputError, match="capability"):
        await service.job_start({"studio_id": "s1", "capability": "x"})
    del storage


def test_a_state_that_cannot_be_read_is_not_overwritten(storage) -> None:
    _studio("s1")
    uri = storage.media(store.state_path("s1"))
    storage.objects[uri] = b"not json"
    # A message for the client (``BusyError``), with no path.
    with pytest.raises(BusyError, match=quota.NOT_STARTED):
        quota.claim("s1", "j1", "spin")
    assert storage.objects[uri] == b"not json"


# ---- rate limits ---------------------------------------------------------------


def test_a_studio_starts_at_most_n_jobs_an_hour(monkeypatch) -> None:
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_HOUR", "2")
    now = time.time()
    _studio("s1", job_starts=[now - quota._HOUR_S - 1, now - 600])
    quota.claim("s1", "j1", "spin")
    _job("j1", "done", now, now)
    with pytest.raises(InputError, match="2 generations in the last hour"):
        quota.claim("s1", "j2", "spin")
    starts = _state("s1")["job_starts"]
    assert len(starts) == 2 and min(starts) == now - 600  # old ones dropped
    _studio("s2")  # per studio
    quota.claim("s2", "j3", "spin")


def test_a_bad_limit_falls_back_to_the_default(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_HOUR", "lots")
    assert quota._limit(quota._PER_STUDIO) == 20
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_HOUR", "0")
    assert quota._limit(quota._PER_STUDIO) == 20
    assert "GENMEDIA_MAX_JOBS_PER_HOUR" in caplog.text
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_HOUR", "5")
    assert quota._limit(quota._PER_STUDIO) == 5


@pytest.mark.asyncio
async def test_an_instance_starts_at_most_n_jobs_an_hour(
    blocked, monkeypatch
) -> None:
    del blocked
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR", "2")
    for sid in ("s1", "s2", "s3"):
        _studio(sid)
    await service.job_start(_spin("s1"))
    # A refused start does not count.
    with pytest.raises(InputError, match="still running"):
        await service.job_start(_spin("s1"))
    await service.job_start(_spin("s2"))
    with pytest.raises(InputError, match="busy"):
        await service.job_start(_spin("s3"))
    assert "job" not in _state("s3")
    quota._STARTS[0] -= quota._HOUR_S  # an hour later
    await service.job_start(_spin("s3"))


# ---- what a failed job tells its clients ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (RuntimeError("key=secret at /srv/app.py"), SERVER_FAILED),
        (StorageError("could not read sessions/x/state.json"), SERVER_FAILED),
        (
            ConfigError("The catalogue file is missing: /srv/c.json"),
            SERVER_FAILED,
        ),
        (InputError("person_image is required"), "person_image is required"),
    ],
)
async def test_a_failed_job_shows_only_messages_meant_for_the_user(
    monkeypatch, caplog, error, shown
) -> None:
    async def failing(tool: str, **kwargs: Any):
        del tool, kwargs
        raise error

    monkeypatch.setattr(tools, "stream", failing)
    monkeypatch.setattr(ss, "finish_job", lambda *a: {})
    _studio("s1")
    job_id = (await service.job_start(_spin("s1")))["job_id"]
    await _until(lambda: not jobs._RUNS)
    status = service.job_status(job_id)
    assert (status["status"], status["error"]) == ("error", shown)
    assert store.read(store.job_path(job_id))["error"] == shown
    if shown == SERVER_FAILED:  # the details are in the log only
        assert str(error) in caplog.text
