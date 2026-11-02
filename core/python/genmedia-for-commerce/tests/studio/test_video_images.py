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

"""Video Try-On's try-on images (``state.images.video_tryon``), the videos
that name them (``source``) and "Make video", which makes one image's
videos again."""

from __future__ import annotations

import collections
import time
from typing import Any, Literal

import pytest

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import (
    ErrorEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import job_call, jobs, quota, store
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss

_RUN = "runs/2026-01-02/studio-s1/0123456789ab"
_Status = Literal["ready", "rejected", "error"]


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def out(name: str) -> str:
    return media(f"{_RUN}/outputs/{name}")


def manifest() -> str:
    return media(f"{_RUN}/manifest.json")


@pytest.fixture(autouse=True)
def _offline(storage, monkeypatch: pytest.MonkeyPatch) -> None:
    del storage  # the state lives in the fake bucket
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(ss, "_JOB_WAIT_S", 0)
    monkeypatch.setattr(sh, "note", lambda studio_id, text: True)
    monkeypatch.setattr(
        sm, "preview_url", lambda uri, video=False: f"https://p/{uri}"
    )
    monkeypatch.setattr(quota, "_STARTS", collections.deque())


def _variation(index: int, uri: str, status: _Status = "ready", **extra):
    event = VariationEvent(
        index=index, status=status, media=None, verdict=None, score=50.0 + index
    )
    event.extra, event.media_uri = extra, uri
    return event


def _selected(front: int, back: int | None = None) -> ProgressEvent:
    data = {"front_index": front, "back_index": back}
    return ProgressEvent("image_selected", "Animating", data=data)


async def _job(events: list, tryon_image: str = "") -> dict[str, Any]:
    """A video try-on job document with ``events`` folded in."""
    job = {
        "studio_id": "s1",
        "capability": "video_tryon",
        "status": "running",
        "results": [],
        "image_results": [],
        "images": [],
        "tryon_image": tryon_image,
    }
    for event in events:
        await jobs._apply(job, event, video=True)
    return job


def _full_run_events(status_1: _Status = "ready") -> list:
    """Two passing fronts (and one rejected), a back view of the first
    (numbered apart from the fronts), each animated; three videos."""
    return [
        ProgressEvent("record", "Saved", data={"manifest": manifest()}),
        _variation(0, out("front-0.png"), stage="image", view="front"),
        _variation(
            1, out("front-1.png"), status_1, stage="image", view="front"
        ),
        _variation(2, out("front-2.png"), "rejected", stage="image"),
        _variation(0, out("back-0.png"), stage="image", view="back"),
        _selected(0, back=0),
        _selected(1),
        _variation(0, out("video-0.mp4"), source_index=0, stage="video"),
        _variation(1, out("video-1.mp4"), source_index=0, stage="video"),
        _variation(2, out("video-2.mp4"), source_index=1, stage="video"),
    ]


def _follow(job_id: str, cap: str = "video_tryon") -> None:
    now = time.time()
    doc = {"studio_id": "s1", "status": "running", "started": now}
    store.write(store.job_path(job_id), {**doc, "updated_at": now})
    ss.apply_action(
        "s1", {"job_started": {"job_id": job_id, "capability": cap}}
    )


def _state() -> dict[str, Any]:
    return ss.get_state("s1")


# ---- the live job ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_job_lists_the_animated_images_with_their_back_views():
    job = await _job(_full_run_events())
    assert job["images"] == [
        {
            "index": 0,
            "status": "ready",
            "score": 50.0,
            "uri": out("front-0.png"),
            "url": f"https://p/{out('front-0.png')}",
            "note": "",
            "fixed": False,
            "run": manifest(),
            "passed": True,
            "back": out("back-0.png"),
        },
        {
            "index": 1,
            "status": "ready",
            "score": 51.0,
            "uri": out("front-1.png"),
            "url": f"https://p/{out('front-1.png')}",
            "note": "",
            "fixed": False,
            "run": manifest(),
            "passed": True,
            "back": "",
        },
    ]
    # The back view (index 0 too) never stands for the front of index 0.
    assert "selected" not in job
    assert [r["source"] for r in job["results"]] == [
        out("front-0.png"),
        out("front-0.png"),
        out("front-1.png"),
    ]


@pytest.mark.asyncio
async def test_an_image_animated_though_none_passed_is_marked_so():
    events = [
        _variation(0, out("front-0.png"), "rejected", stage="image"),
        _variation(1, out("front-1.png"), "rejected", stage="image"),
        _selected(1),
    ]
    job = await _job(events)
    assert [(i["uri"], i["passed"]) for i in job["images"]] == [
        (out("front-1.png"), False)
    ]


@pytest.mark.asyncio
async def test_a_make_video_job_names_its_image_on_each_video():
    image = out("front-1.png")
    job = await _job([_variation(0, out("video-9.mp4"))], tryon_image=image)
    assert job["results"][0]["source"] == image
    assert job["images"] == []


@pytest.mark.asyncio
async def test_a_started_job_document_carries_its_image(monkeypatch):
    async def not_run(run, spec) -> None:
        del run, spec

    monkeypatch.setattr(jobs, "_RUNS", {})
    monkeypatch.setattr(jobs, "_LOCAL", {})
    monkeypatch.setattr(jobs, "_run", not_run)
    spec = {"studio_id": "s1", "capability": "video_tryon"}
    jobs.start({**spec, "tryon_image": out("front-0.png")}, "j9")
    doc = jobs.status("j9")
    assert (doc["tryon_image"], doc["images"]) == (out("front-0.png"), [])
    jobs.start(spec, "j10")  # a full run
    assert jobs.status("j10")["tryon_image"] == ""


# ---- the end of a full run ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_finished_run_saves_its_images_and_videos_with_their_source():
    _follow("j1")
    job = {**await _job(_full_run_events()), "status": "done"}
    st = ss.finish_job("s1", "j1", job)["state"]
    images = st["images"]["video_tryon"]
    assert [(i["uri"], i["passed"], i["back"]) for i in images] == [
        (out("front-0.png"), True, out("back-0.png")),
        (out("front-1.png"), True, ""),
    ]
    assert "url" not in images[0]  # signed again on every read
    videos = st["results"]["video_tryon"]
    assert [(v["index"], v["source"]) for v in videos] == [
        (0, out("front-0.png")),
        (1, out("front-0.png")),
        (2, out("front-1.png")),
    ]
    assert "source" not in st
    read = ss.saved_state("s1")["state"]
    assert read["images"]["video_tryon"][1]["url"].startswith("https://p/")


@pytest.mark.asyncio
async def test_a_run_whose_videos_failed_still_saves_its_images():
    _follow("j1")
    events = [*_full_run_events()[:7], ErrorEvent("No video came out")]
    job = {**await _job(events), "status": "error"}
    st = ss.finish_job("s1", "j1", job)["state"]
    assert len(st["images"]["video_tryon"]) == 2
    assert st["results"]["video_tryon"] == []
    assert st["job"] is None


@pytest.mark.asyncio
async def test_a_run_that_animated_nothing_changes_nothing():
    _follow("j1")
    done = {**await _job(_full_run_events()), "status": "done"}
    ss.finish_job("s1", "j1", done)
    _follow("j2")  # a new full run clears the page
    assert _state()["images"]["video_tryon"] == []
    failed = await _job([ErrorEvent("No try-on image came out")])
    st = ss.finish_job("s1", "j2", {**failed, "status": "error"})["state"]
    assert st["images"]["video_tryon"] == []
    assert st["results"]["video_tryon"] == []


# ---- "Make video" --------------------------------------------------------------


def _studio_with_images() -> None:
    """A studio whose full run made images 0 and 1 with their videos."""
    images = [
        {"index": 0, "uri": out("front-0.png"), "run": manifest()}
        | {"back": out("back-0.png"), "passed": True},
        {"index": 1, "uri": out("front-1.png"), "run": manifest()}
        | {"back": "", "passed": True},
    ]
    videos = [
        {"index": 0, "uri": out("video-0.mp4"), "source": out("front-0.png")},
        {"index": 4, "uri": out("video-1.mp4"), "source": out("front-1.png")},
        {"index": 2, "uri": out("video-2.mp4"), "source": out("front-0.png")},
    ]
    state = {
        "capability": "video_tryon",
        "images": {"video_tryon": images},
        "results": {"video_tryon": videos},
    }
    store.write(store.state_path("s1"), {"state": state, "updated_at": 1.0})


@pytest.fixture
def started(monkeypatch) -> list[dict[str, Any]]:
    """The specs ``quota.start`` passes on, the jobs not run."""
    specs: list[dict[str, Any]] = []

    def start(spec: dict[str, Any], job_id: str) -> dict[str, Any]:
        specs.append(spec)
        return {"job_id": job_id, "status": "running"}

    monkeypatch.setattr(jobs, "start", start)
    return specs


@pytest.mark.asyncio
async def test_make_video_derives_its_run_back_view_and_count(started):
    _studio_with_images()
    spec = {
        "studio_id": "s1",
        "capability": "video_tryon",
        "tryon_image": out("front-0.png"),
        "back_image": out("front-1.png"),  # the client's: ignored
        "source_run": "gs://elsewhere/manifest.json",
        "settings": {"num_variations": "5", "motion": "auto"},
    }
    job_id = (await quota.start(spec))["job_id"]
    (sent,) = started
    assert sent["back_image"] == out("back-0.png")
    assert sent["source_run"] == manifest()
    assert sent["settings"] == {
        "num_variations": get_settings().videos_per_image,
        "motion": "auto",
    }
    # Its start clears nothing: the other images' videos stay on show.
    st = _state()
    assert st["job"] == {
        "job_id": job_id,
        "capability": "video_tryon",
        "tryon_image": out("front-0.png"),
    }
    assert len(st["results"]["video_tryon"]) == 3
    assert len(st["images"]["video_tryon"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    [
        out("front-2.png"),  # a file of the studio, not one of its images
        out("video-0.mp4"),
        media("runs/2026-01-02/studio-s2/0123456789ab/outputs/front-0.png"),
        7,
    ],
)
async def test_make_video_refuses_an_image_the_studio_did_not_animate(
    started, image
):
    _studio_with_images()
    spec = {
        "studio_id": "s1",
        "capability": "video_tryon",
        "tryon_image": image,
    }
    with pytest.raises(InputError, match=job_call.NOT_AN_IMAGE):
        await quota.start(spec)
    assert not started
    assert _state().get("job") is None


@pytest.mark.asyncio
async def test_make_video_refuses_an_image_a_full_run_replaced_meanwhile(
    started, monkeypatch
):
    _studio_with_images()

    def full_run_meanwhile(spec: dict[str, Any], studio_id: str) -> None:
        del spec, studio_id  # after the image was checked, before the claim
        _follow("j0")
        ss.finish_job("s1", "j0", {"status": "error", "error": "x"})

    monkeypatch.setattr(quota, "check_refs", full_run_meanwhile)
    spec = {"studio_id": "s1", "capability": "video_tryon"}
    with pytest.raises(InputError, match=job_call.NOT_AN_IMAGE):
        await quota.start({**spec, "tryon_image": out("front-0.png")})
    assert not started
    assert _state()["images"]["video_tryon"] == []
    assert _state().get("job") is None
    assert not quota._STARTS  # its slot given back


@pytest.mark.asyncio
async def test_a_full_run_drops_what_only_the_server_sets(started):
    _studio_with_images()
    spec = {
        "studio_id": "s1",
        "capability": "tryon",
        "tryon_image": out("front-0.png"),  # only Video Try-On's
        "back_image": out("back-0.png"),
        "source_run": manifest(),
    }
    await quota.start(spec)
    assert started == [{"studio_id": "s1", "capability": "tryon"}]


@pytest.mark.asyncio
async def test_make_video_replaces_only_its_image_videos(started):
    _studio_with_images()
    image = out("front-0.png")
    spec = {"studio_id": "s1", "capability": "video_tryon"}
    job_id = (await quota.start({**spec, "tryon_image": image}))["job_id"]
    events = [
        _variation(1, out("video-8.mp4")),
        _variation(0, out("video-9.mp4")),
    ]
    job = {**await _job(events, tryon_image=image), "status": "done"}
    st = ss.finish_job("s1", job_id, job)["state"]
    videos = st["results"]["video_tryon"]
    assert [(v["uri"], v["index"], v["source"]) for v in videos] == [
        (out("video-1.mp4"), 4, out("front-1.png")),
        (out("video-8.mp4"), 5, image),
        (out("video-9.mp4"), 6, image),
    ]
    assert len({v["index"] for v in videos}) == len(videos)
    assert len(st["images"]["video_tryon"]) == 2


@pytest.mark.asyncio
async def test_a_failed_make_video_leaves_its_image_without_video(started):
    _studio_with_images()
    image = out("front-1.png")
    spec = {"studio_id": "s1", "capability": "video_tryon"}
    job_id = (await quota.start({**spec, "tryon_image": image}))["job_id"]
    job = {**await _job([ErrorEvent("no video")], image), "status": "error"}
    st = ss.finish_job("s1", job_id, job)["state"]
    assert [v["source"] for v in st["results"]["video_tryon"]] == [
        out("front-0.png"),
        out("front-0.png"),
    ]
    assert len(st["images"]["video_tryon"]) == 2


# ---- studios saved before ----------------------------------------------------


def test_an_old_studio_gets_its_source_as_its_one_image():
    source = {"index": 1, "status": "ready", "score": 80, "uri": out("f.png")}
    old = {
        "capability": "video_tryon",
        "video_start": "image",
        "tryon_image": {"label": "r", "ref": out("f.png"), "run": manifest()},
        "source": {"video_tryon": {**source, "view": "front"}},
        "results": {
            "video_tryon": [
                {"index": 0, "uri": out("v.mp4")},
                {"index": 1, "uri": out("w.mp4"), "source": out("g.png")},
            ]
        },
    }
    store.write(store.state_path("s1"), {"state": old, "updated_at": 1.0})
    st = ss.saved_state("s1")["state"]
    assert st["images"]["video_tryon"] == [
        {**source, "passed": True, "url": f"https://p/{out('f.png')}"}
    ]
    for key in ("video_start", "tryon_image", "source"):
        assert key not in st
    # Its videos are that image's (a video naming its own image keeps it).
    assert [(v["uri"], v["source"]) for v in st["results"]["video_tryon"]] == [
        (out("v.mp4"), out("f.png")),
        (out("w.mp4"), out("g.png")),
    ]
    # Written back on the next change, and Make video takes that image.
    ss.apply_action("s1", {"capability": "tryon"})
    assert _state()["images"]["video_tryon"][0]["uri"] == out("f.png")


@pytest.mark.asyncio
async def test_make_video_takes_an_old_studio_image_before_it_is_rewritten(
    started,
):
    source = {"index": 1, "status": "ready", "uri": out("f.png")}
    old = {"capability": "video_tryon", "source": {"video_tryon": source}}
    store.write(store.state_path("s1"), {"state": old, "updated_at": 1.0})
    spec = {"studio_id": "s1", "capability": "video_tryon"}
    await quota.start({**spec, "tryon_image": out("f.png")})
    assert len(started) == 1
    assert _state()["job"]["tryon_image"] == out("f.png")


def test_an_old_source_never_replaces_images():
    images = {"video_tryon": [{"uri": out("new.png"), "passed": False}]}
    old = {
        "images": images,
        "source": {"video_tryon": {"uri": out("old.png"), "status": "ready"}},
    }
    store.write(store.state_path("s1"), {"state": old, "updated_at": 1.0})
    assert _state()["images"] == images
    empty = {"source": {"video_tryon": None}, "video_start": "photos"}
    store.write(store.state_path("s1"), {"state": empty, "updated_at": 2.0})
    assert _state() == {}
