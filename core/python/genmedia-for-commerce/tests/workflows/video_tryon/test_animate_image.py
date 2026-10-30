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

"""Animating a given image: how many videos, and where its products come
from (the try-on's run record, the classifier, or none)."""

import pytest

from genmedia4commerce.core.events import ClassifiedEvent, VariationEvent
from genmedia4commerce.core.types import Classification, Product, ProductRef
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import reference
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    Shot,
    VideoTryOnRequest,
)
from tests.workflows.video_tryon.conftest import (
    CLIP,
    FACE,
    TRYON,
    classification,
    product_png,
)

ZOOM = Detail("logo", "", "front", "logo on the flap", b"zoom")
BAG_CLOSEUP = Plan([Shot("walk", 4.0), Shot("detail", 4.0, "bag")])


async def collect(req):
    return [event async for event in run_video_tryon(req)]


def video_calls(fake_gen):
    return [c for c in fake_gen.calls if c["fn"] == "generate_video"]


@pytest.mark.asyncio
async def test_num_variations_sets_the_videos_of_an_image(
    fake_gen, fake_eval, videos_per_image
):
    videos_per_image(2)
    fake_gen.video(CLIP)
    await collect(VideoTryOnRequest(tryon_image=TRYON))
    assert len(video_calls(fake_gen)) == 2  # the deployment's number

    fake_gen.calls.clear()
    req = VideoTryOnRequest(tryon_image=TRYON, videos_per_image=1)
    events = await collect(req)
    assert events[0].data["videos"] == 1
    assert len(video_calls(fake_gen)) == 1
    assert len([e for e in events if isinstance(e, VariationEvent)]) == 1


@pytest.mark.asyncio
async def test_photos_are_classified_then_the_chosen_one_goes(
    fake_gen, fake_eval, videos_per_image, zoom, monkeypatch
):
    """An uploaded image with product photos: classified as the try-on
    does, the photo it would choose per product (its hero, first) sent,
    its details searched before the plan needs them."""
    videos_per_image(1)
    fake_gen.video(CLIP)
    flat, worn = product_png(1), product_png(2)
    bag = Product(
        "A",
        "bag",
        "a bag",
        [ProductRef(worn, "front", 2, "worn"), ProductRef(flat, "front")],
    )
    classified = Classification("single", [bag], "full_body")
    calls = []

    async def classify(photos, reference_model, complete_look, **kw):
        calls.append(list(photos))
        return classified, {}

    monkeypatch.setattr(reference, "classify", classify)
    events = await collect(
        VideoTryOnRequest(tryon_image=TRYON, product_images=[flat, worn])
    )
    assert calls == [[flat, worn]]
    assert isinstance(events[0], ClassifiedEvent)
    assert events[0].payload["products"][0]["category"] == "bag"
    (call,) = video_calls(fake_gen)
    assert call["raw_parts"] == [TRYON, FACE, worn]  # the worn upload
    # searched with its product's name
    assert [(i, name) for i, _, name in zoom.calls] == [([worn], "a bag")]


@pytest.mark.asyncio
async def test_the_run_records_details_are_used_without_a_search(
    fake_gen, fake_eval, videos_per_image, zoom, planner
):
    videos_per_image(1)
    fake_gen.video(CLIP)
    planner.plan = BAG_CLOSEUP
    req = VideoTryOnRequest(
        tryon_image=TRYON,
        product_images=[product_png(1)],
        classification=classification("full_body", "bag"),
        closeups=[[ZOOM]],
    )
    events = await collect(req)
    assert not any(isinstance(e, ClassifiedEvent) for e in events)
    assert zoom.calls == []  # no new search
    assert zoom.upscaled == [[ZOOM]]  # only the plan's close-up
    (call,) = video_calls(fake_gen)
    assert call["raw_parts"] == [TRYON, FACE, product_png(1), b"zoom 2K"]


@pytest.mark.asyncio
async def test_without_products_the_outfit_is_judged_against_the_image(
    fake_gen, fake_eval, videos_per_image, zoom
):
    videos_per_image(1)
    fake_gen.video(CLIP)
    await collect(VideoTryOnRequest(tryon_image=TRYON))
    (call,) = video_calls(fake_gen)
    assert call["raw_parts"] == [TRYON, FACE]  # no product reference
    assert zoom.calls == []
    assert {c["product_filter"] for c in fake_eval.judge_calls} == {None}
    assert all(c["references"] == [TRYON] for c in fake_eval.judge_calls)
