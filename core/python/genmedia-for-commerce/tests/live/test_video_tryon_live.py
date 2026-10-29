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

"""Live Video Try-On runs (real models; GENMEDIA_LIVE=1).

Shortcut (animate a try-on image) and end to end (products + person), for
the automatic animation and a custom request. Media and verdicts are saved under
/tmp/genmedia_live/video_tryon/<case>/. The number of variations follows
GENMEDIA_NUM_VARIATIONS (default 3).
"""

import json
from pathlib import Path

import pytest

from genmedia4commerce.core import catalog
from genmedia4commerce.core.events import (
    CompleteEvent,
    Event,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon
from genmedia4commerce.workflows.video_tryon.types import VideoTryOnRequest

pytestmark = pytest.mark.live

OUT = Path("/tmp/genmedia_live/video_tryon")
#: A dressed model of the catalogue, as the image to animate.
DRESSED = "m_sofia"


async def _run(case: str, req: VideoTryOnRequest) -> list[Event]:
    events = [event async for event in run_video_tryon(req)]
    out = OUT / case
    out.mkdir(parents=True, exist_ok=True)
    record = []
    for event in events:
        data = event.to_dict()
        if isinstance(event, VariationEvent) and event.media:
            stage = event.extra.get("stage", "video")
            view = event.extra.get("view", "")
            ext = "mp4" if stage == "video" else "png"
            path = out / f"{stage}{view}_{event.index}_{event.status}.{ext}"
            path.write_bytes(event.media)
            data["media_base64"] = str(path)
        record.append(data)
    (out / "events.json").write_text(json.dumps(record, indent=2))
    assert isinstance(events[-1], CompleteEvent), record[-1]
    assert any(
        isinstance(e, VariationEvent) and e.extra.get("stage") == "video"
        for e in events
    ), f"no video variation, see {out}/events.json"
    return events


CUSTOM = "walks towards the camera, then a close shot of the shirt"


@pytest.mark.asyncio
@pytest.mark.parametrize("motion", ["auto", "custom"])
async def test_video_tryon_shortcut_live(motion):
    prompt = CUSTOM if motion == "custom" else None
    req = VideoTryOnRequest(
        tryon_image=catalog.image(DRESSED), motion=motion, prompt=prompt
    )
    await _run(f"shortcut_{motion}", req)


@pytest.mark.asyncio
@pytest.mark.parametrize("motion", ["auto", "custom"])
async def test_video_tryon_end_to_end_live(motion):
    req = VideoTryOnRequest(
        product_images=[
            catalog.image("s_genmedia_tshirt", 0),
            catalog.image("s_genmedia_tshirt", 1),
        ],
        person_image=catalog.image("m_james"),
        motion=motion,
        prompt=CUSTOM if motion == "custom" else None,
    )
    events = await _run(f"end_to_end_{motion}", req)
    (selected,) = [
        e
        for e in events
        if isinstance(e, ProgressEvent) and e.stage == "image_selected"
    ]
    assert isinstance(selected.data["front_index"], int)
    assert any(
        isinstance(e, VariationEvent) and e.extra.get("stage") == "image"
        for e in events
    )
