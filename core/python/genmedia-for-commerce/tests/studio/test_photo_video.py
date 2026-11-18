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

"""A Video Try-On job of one of the studio's Photo Try-On results: a
full-body photo is cut to 9:16 and animated, any other makes a 9:16 try-on
image first (``job_call.for_studio``)."""

from __future__ import annotations

import collections
import io
import json
from typing import Any

import pytest
from PIL import Image

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import ProgressEvent, VariationEvent
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import job_call, jobs, quota, store
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss

_RUN = "runs/2026-01-02/studio-s1/0123456789ab"


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def out(name: str, run: str = _RUN) -> str:
    return media(f"{run}/outputs/{name}")


def manifest() -> str:
    return media(f"{_RUN}/manifest.json")


@pytest.fixture(autouse=True)
def _offline(storage, catalog_file, monkeypatch: pytest.MonkeyPatch) -> None:
    del storage, catalog_file  # the state, record and photos: in the fake
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(sh, "note", lambda studio_id, text: True)
    monkeypatch.setattr(sh, "note_later", lambda studio_id, text: None)
    monkeypatch.setattr(
        sm, "preview_url", lambda uri, video=False: f"https://p/{uri}"
    )
    monkeypatch.setattr(quota, "_STARTS", collections.deque())


@pytest.fixture
def started(monkeypatch) -> list[dict[str, Any]]:
    """The specs ``quota.start`` passes on, the jobs not run."""
    specs: list[dict[str, Any]] = []

    def start(spec: dict[str, Any], job_id: str) -> dict[str, Any]:
        specs.append(spec)
        return {"job_id": job_id, "status": "running"}

    monkeypatch.setattr(jobs, "start", start)
    return specs


def _photo_studio(storage, framing: str | None, span=None) -> None:
    """A studio whose Photo Try-On made a 3:4 front, its back view and
    another front; its run record says ``framing`` (None: no record) and,
    with a ``span``, its single product's."""
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), "red").save(buf, format="PNG")
    storage.write(out("front-0.png"), buf.getvalue())
    if framing is not None:
        products = [{"span": span}] if span else []
        c = {"mode": "single", "framing": framing, "products": products}
        record = {"classification": c}
        storage.write(manifest(), json.dumps(record).encode())
    results = [
        {"index": 0, "uri": out("front-0.png"), "run": manifest()}
        | {"view": "front", "status": "ready", "score": 90},
        {"index": 0, "uri": out("back-0.png"), "run": manifest()}
        | {"view": "back", "status": "ready"},
        {"index": 1, "uri": out("front-1.png"), "run": manifest()},
    ]
    old = [{"index": 0, "uri": out("video-0.mp4"), "source": "x"}]
    state = {
        "capability": "video_tryon",
        "results": {"tryon": results, "video_tryon": old},
        "images": {"video_tryon": [{"uri": out("old.png")}]},
    }
    store.write(store.state_path("s1"), {"state": state, "updated_at": 1.0})


def _spec(uri: Any) -> dict[str, Any]:
    return {
        "studio_id": "s1",
        "capability": "video_tryon",
        "person": out("me.png"),
        "products": [out("tee.png")],
        "tryon_image": uri,
        "settings": {"num_variations": 3, "motion": "auto"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("span", [None, ["shoulders", "mid-thigh"]])
async def test_a_full_body_photo_is_cut_to_9_16_and_animated(
    started, storage, span
):
    _photo_studio(storage, "full_body", span)  # or head to knees
    job_id = (await quota.start(_spec(out("front-0.png"))))["job_id"]
    (sent,) = started
    frame = out("front-0-9x16.png")
    assert sent["tryon_image"] == frame
    with Image.open(io.BytesIO(storage.objects[frame])) as img:
        assert img.size == (225, 400)  # the full height, the sides cut
    assert sent["back_image"] == out("back-0.png")  # the same run's back
    assert sent["source_run"] == manifest()
    assert sent["settings"]["num_variations"] == (
        get_settings().videos_per_image
    )
    assert sent["progress"] == job_call.ANIMATING
    assert (sent["image"]["uri"], sent["image"]["back"]) == (
        frame,
        out("back-0.png"),
    )
    # Its start replaces Video Try-On's page, as a full run does.
    st = ss.get_state("s1")
    assert st["job"] == {"job_id": job_id, "capability": "video_tryon"}
    assert st["images"]["video_tryon"] == []
    assert st["results"]["video_tryon"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("framing", "span"),
    [("upper_body", None), (None, None), ("full_body", ["waist", "ankles"])],
)
async def test_another_photo_makes_a_9_16_image_first(
    started, storage, framing, span
):
    _photo_studio(storage, framing, span)
    await quota.start(_spec(out("front-0.png")))
    (sent,) = started
    for key in ("tryon_image", "back_image", "source_run", "image"):
        assert key not in sent
    assert sent["settings"] == {"num_variations": 3, "motion": "auto"}
    assert sent["person"] == out("me.png")
    assert sent["progress"] == job_call.NEW_IMAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        out("back-0.png"),  # a back view
        out("front-9.png"),  # not one of its results
        out("front-0.png", "runs/2026-01-02/studio-s2/0123456789ab"),
    ],
)
async def test_only_a_front_result_of_the_studio_is_animated(
    started, storage, uri
):
    _photo_studio(storage, "full_body")
    with pytest.raises(InputError, match=job_call.NOT_AN_IMAGE):
        await quota.start(_spec(uri))
    assert not started


def test_a_foreign_file_among_the_results_is_refused():
    foreign = out("front-0.png", "runs/2026-01-02/studio-s2/0123456789ab")
    st = {"results": {"tryon": [{"uri": foreign, "run": manifest()}]}}
    with pytest.raises(InputError, match=job_call.NOT_AN_IMAGE):
        job_call.for_studio(_spec(foreign), st)


@pytest.mark.asyncio
async def test_the_job_says_which_way_it_went_and_keeps_its_image(
    monkeypatch,
):
    async def not_run(run, spec) -> None:
        del run, spec

    monkeypatch.setattr(jobs, "_RUNS", {})
    monkeypatch.setattr(jobs, "_LOCAL", {})
    monkeypatch.setattr(jobs, "_run", not_run)
    image = {"index": 0, "uri": out("f-9x16.png"), "passed": True}
    spec = {**_spec(out("f-9x16.png")), "settings": {"num_variations": 2}}
    spec |= {"image": image, "progress": job_call.ANIMATING}
    started = jobs.start(spec, "j1")
    assert started["progress"] == job_call.ANIMATING
    job = jobs._LOCAL["j1"]
    assert (job["tryon_image"], job["images"]) == ("", [image])
    assert job["progress"] == job_call.ANIMATING
    await jobs._apply(job, ProgressEvent("plan", "Planning"), video=True)
    assert job["progress"] == f"{job_call.ANIMATING} · Planning"
    video = VariationEvent(
        index=0, status="ready", media=None, verdict=None, score=80.0
    )
    video.media_uri, video.extra = out("video-1.mp4"), {"stage": "video"}
    await jobs._apply(job, video, video=True)
    assert job["results"][0]["source"] == out("f-9x16.png")
