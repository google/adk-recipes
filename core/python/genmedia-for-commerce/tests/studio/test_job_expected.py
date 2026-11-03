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

"""A job says how many front variations are coming and how many failed."""

from typing import Any

import pytest

from genmedia4commerce.core.events import ProgressEvent, VariationEvent
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import jobs, store

ZERO = {"image_results": 0, "results": 0}


def _variation(index: int, uri: str = "", **extra) -> VariationEvent:
    event = VariationEvent(
        index=index,
        status="ready" if uri else "error",
        media=None,
        verdict=None,
        score=90.0,
    )
    event.extra = extra
    event.media_uri = uri or None
    return event


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs.sm, "preview_url", lambda uri, video=False: uri)
    monkeypatch.setattr(jobs, "_LOCAL", {})
    monkeypatch.setattr(jobs, "_RUNS", {})

    async def no_run(run: Any, spec: dict) -> None:
        del run, spec

    monkeypatch.setattr(jobs, "_run", no_run)


async def _started(spec: dict[str, Any]) -> dict[str, Any]:
    jobs.start({"studio_id": "s1", **spec}, "j1")
    return jobs.status("j1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"capability": "tryon", "settings": {"num_variations": "3"}}, 3),
        ({"capability": "spin", "settings": {"num_variations": 2}}, 2),
        ({"capability": "spin", "settings": {"num_variations": "x"}}, 1),
        ({"capability": "tryon", "settings": {"num_variations": 0}}, 1),
        ({"capability": "tryon"}, 1),
        (  # a "Make video": for_studio set its number of videos
            {
                "capability": "video_tryon",
                "tryon_image": "gs://b/front.png",
                "settings": {"num_variations": 2},
            },
            2,
        ),
    ],
)
async def test_a_job_expects_its_variations_as_results(spec, expected):
    job = await _started(spec)
    assert job["expected"] == {"image_results": 0, "results": expected}
    assert job["failed"] == ZERO


@pytest.mark.asyncio
async def test_a_video_try_on_expects_videos_per_animated_image(
    monkeypatch,
):
    monkeypatch.setenv("GENMEDIA_VIDEOS_PER_IMAGE", "3")
    get_settings.cache_clear()
    spec = {"capability": "video_tryon", "settings": {"num_variations": "2"}}
    job = await _started(spec)
    assert job["expected"] == {"image_results": 2, "results": 0}
    live = jobs._LOCAL["j1"] = jobs._RUNS["j1"].job
    for i in (0, 1):
        img = _variation(i, f"gs://b/runs/x/front-{i}.png", stage="image")
        await jobs._apply(live, img, video=True)
    for front in (0, 7, 1):  # 7: no such image, nothing is animated
        data = {"front_index": front, "back_index": None}
        event = ProgressEvent(stage="image_selected", message="", data=data)
        await jobs._apply(live, event, video=True)
        if front == 0:
            assert jobs.status("j1")["expected"]["results"] == 3
    assert jobs.status("j1")["expected"] == {"image_results": 2, "results": 6}


@pytest.mark.asyncio
async def test_failed_front_variations_are_counted_per_list():
    job: dict[str, Any] = {"results": [], "image_results": []}
    events = [
        _variation(0, stage="image", view="front"),  # a try-on image
        _variation(0, stage="image", view="back"),  # an extra: not counted
        _variation(1, "gs://b/runs/x/front-1.png", stage="image"),
        _variation(0),  # a video
        _variation(1, view="back"),
        _variation(2, "gs://b/runs/x/video-2.mp4"),
    ]
    changed = [await jobs._apply(job, e, video=True) for e in events]
    assert changed == [True, False, True, True, False, True]
    assert job["failed"] == {"image_results": 1, "results": 1}
    assert [r["index"] for r in job["image_results"]] == [1]
    assert [r["index"] for r in job["results"]] == [2]


def test_an_old_job_document_has_no_counts(storage) -> None:
    del storage
    doc = {"status": "running", "studio_id": "s1", "results": []}
    store.write(store.job_path("old1"), {**doc, "updated_at": 4e9})
    job = jobs.status("old1")
    assert job["status"] == "running"
    assert "expected" not in job and "failed" not in job  # read as zeros
