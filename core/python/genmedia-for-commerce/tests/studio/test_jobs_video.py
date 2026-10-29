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

"""A video try-on that animates several try-on images."""

import pytest

from genmedia4commerce.core.events import ProgressEvent, VariationEvent
from genmedia4commerce.studio import jobs


def _variation(index, uri, **extra):
    event = VariationEvent(
        index=index, status="ready", media=None, verdict=None, score=90.0
    )
    event.extra = extra
    event.media_uri = uri
    return event


@pytest.fixture(autouse=True)
def _no_signing(monkeypatch):
    monkeypatch.setattr(jobs.sm, "preview_url", lambda uri, video=False: uri)


@pytest.mark.asyncio
async def test_videos_name_their_image_and_the_job_lists_the_animated():
    job = {"results": [], "image_results": []}
    for i in (0, 1):
        img = _variation(i, f"gs://b/runs/x/front-{i}.png", stage="image")
        await jobs._apply(job, img, video=True)
    for i in (1, 0):  # one image_selected per animated image
        data = {"front_index": i, "back_index": None}
        event = ProgressEvent(stage="image_selected", message="", data=data)
        await jobs._apply(job, event, video=True)
    for n, src in enumerate((0, 1, 1)):
        clip = _variation(n, f"gs://b/runs/x/video-{n}.mp4", source_index=src)
        await jobs._apply(job, clip, video=True)

    assert [i["uri"] for i in job["images"]] == [
        "gs://b/runs/x/front-1.png",
        "gs://b/runs/x/front-0.png",
    ]
    assert "selected" not in job
    assert [r["source"] for r in job["results"]] == [
        "gs://b/runs/x/front-0.png",
        "gs://b/runs/x/front-1.png",
        "gs://b/runs/x/front-1.png",
    ]


@pytest.mark.asyncio
async def test_a_result_says_whether_a_fix_pass_made_it():
    job = {"results": [], "image_results": []}
    fixed = _variation(0, "gs://b/f0.png")
    fixed.fixed = True
    await jobs._apply(job, fixed, video=False)
    await jobs._apply(job, _variation(1, "gs://b/f1.png"), video=False)
    assert [r["fixed"] for r in job["results"]] == [True, False]


@pytest.mark.asyncio
async def test_each_result_names_the_run_record():
    job = {"results": [], "image_results": [], "progress": "Drawing"}
    manifest = "gs://b/runs/d/s/0123456789ab/manifest.json"
    record = ProgressEvent("record", "Saved", data={"manifest": manifest})
    assert not await jobs._apply(job, record, video=False)
    assert job["progress"] == "Drawing"  # not a step for the user
    await jobs._apply(job, _variation(0, "gs://b/f.png"), video=False)
    assert job["results"][0]["run"] == manifest
