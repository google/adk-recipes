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

"""``video_try_on`` end to end: request building and the two-stage result."""

import dataclasses

import pytest

from genmedia4commerce import tools
from genmedia4commerce.core import runs
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ProgressEvent,
    RecordEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Classification,
    Product,
    ProductRef,
    Verdict,
)
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import record
from tests.mcp.test_tools import COMPLETE, PERSON, PRODUCT, VIDEO, b64, events

pytestmark = pytest.mark.asyncio


async def test_video_try_on_end_to_end_builds_request(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)],
        person_image_base64=b64(PERSON),
        reference_model_image_base64=b64(PRODUCT),
        style_guidelines="jackets closed",
        scenario="a beach",
        num_variations=2,
        image_size="4K",
    )

    (req,) = run.requests
    assert req.tryon_image is None
    assert req.product_images == [PRODUCT]
    assert req.person_image == PERSON
    assert req.reference_model_image == PRODUCT
    assert (req.style_guidelines, req.scenario) == ("jackets closed", "a beach")
    assert req.motion == "auto"
    assert req.policy.num_variations == 2
    # A 9:16 video gets a 9:16 try-on image.
    assert req.image == ImageParams(aspect_ratio="9:16", image_size="4K")
    assert req.video == VideoParams(aspect_ratio="9:16", resolution="720p")


async def test_video_try_on_end_to_end_return_shape(monkeypatch):
    image = VariationEvent(
        index=0,
        status="ready",
        media=PRODUCT,
        verdict=Verdict(publish=True),
        score=80.0,
        extra={"view": "front", "stage": "image"},
    )
    selected = {"front_index": 0, "back_index": None}
    run = events(
        ClassifiedEvent(payload={"mode": "single", "framing": "full_body"}),
        ProgressEvent("preprocessed", "Generating the front view"),
        image,
        ProgressEvent("image_selected", "Animating 1", data=selected),
        ProgressEvent("references", "Animating the selected image"),
        VIDEO,
        COMPLETE,
    )
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)], person_image_base64=b64(PERSON)
    )

    assert set(result) == {
        "variations",
        "image_variations",
        "selected",
        "best_index",
        "ranked_indices",
        "classification",
        "run",
    }
    assert result["classification"]["framing"] == "full_body"
    (picture,) = result["image_variations"]
    assert picture["extra"] == {"view": "front", "stage": "image"}
    assert picture["media_mime"] == "image/png"
    (clip,) = result["variations"]
    assert clip["extra"] == {"stage": "video"}
    assert result["selected"] == [selected]
    assert (result["best_index"], result["ranked_indices"]) == (0, [0])


async def test_video_try_on_several_animated_images(monkeypatch):
    """Each animated image adds its selection; every video is returned."""

    def video(index, source):
        extra = {"stage": "video", "source_index": source}
        return dataclasses.replace(VIDEO, index=index, extra=extra)

    picks = [
        {"front_index": 0, "back_index": None},
        {"front_index": 2, "back_index": None},
    ]
    run = events(
        ProgressEvent("image_selected", "Animating 1", data=picks[0]),
        ProgressEvent("image_selected", "Animating 3", data=picks[1]),
        *(video(i, source) for i, source in enumerate([0, 2, 0, 2])),
        CompleteEvent(ranked_indices=[2, 0, 1, 3], best_index=2),
    )
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)], person_image_base64=b64(PERSON)
    )

    assert result["selected"] == picks
    assert [v["index"] for v in result["variations"]] == [0, 1, 2, 3]
    assert [v["extra"]["source_index"] for v in result["variations"]] == [
        0,
        2,
        0,
        2,
    ]
    assert (result["best_index"], result["ranked_indices"]) == (2, [2, 0, 1, 3])


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"person_image_base64": b64(PERSON)},
        {"product_images_base64": [b64(PRODUCT)]},
        {"product_images_base64": [], "person_image_base64": b64(PERSON)},
    ],
)
async def test_video_try_on_needs_tryon_image_or_person_and_products(
    monkeypatch, kwargs
):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(**kwargs)

    assert set(result) == {"error", "retryable"}
    assert "tryon_image" in result["error"]
    assert run.requests == []


async def test_num_variations_counts_the_videos_of_a_given_image(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    await tools.video_try_on(tryon_image_base64=b64(PERSON), num_variations=1)
    await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)],
        person_image_base64=b64(PERSON),
        num_variations=1,
    )

    animated, end_to_end = run.requests
    assert animated.videos_per_image == 1  # one video
    assert end_to_end.videos_per_image is None  # one try-on image
    assert end_to_end.policy.num_variations == 1


SHOES = png(14, 10, (7, 8, 9))
OTHER = png(16, 10, (9, 9, 9))


def _try_on_run() -> runs.RunRef:
    """A try-on run that kept its record: one pair of shoes, one close-up."""
    shoes = Product("A", "footwear", "boots", [ProductRef(SHOES, "front")])
    c = Classification("single", [shoes], "footwear")
    chosen = [(shoes, shoes.refs[0])]
    zoom = Detail("logo", "", "side", "logo", png(6, 6, (1, 1, 1)))
    classified = record.payload(c, chosen, [SHOES], {})
    kept = record.record(classified, chosen, [SHOES], {}, [("A", zoom)])
    run = runs.new_run(run_id="0123456789ab", session_id="studio-1")
    runs.save_record(run, kept)
    return run


async def test_source_run_brings_the_products_of_its_try_on(
    monkeypatch, storage
):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)
    bucket, folder = get_settings().media_bucket, _try_on_run().prefix()

    result = await tools.video_try_on(
        tryon_image_base64=b64(PERSON),
        product_images_base64=[b64(OTHER)],  # changed since: not used
        source_run=f"gs://{bucket}/{folder}/{runs.MANIFEST}",
    )

    assert "error" not in result
    (req,) = run.requests
    assert req.product_images == [SHOES]
    assert req.classification.framing == "footwear"
    assert [[d.where for d in found] for found in req.closeups] == [["logo"]]


async def test_a_run_without_a_record_keeps_the_products(monkeypatch, storage):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)
    folder = runs.folder_uri(runs.new_run(run_id="0123456789ab"), "outputs")

    await tools.video_try_on(
        tryon_image_base64=b64(PERSON),
        product_images_base64=[b64(OTHER)],
        source_run=folder,
    )

    (req,) = run.requests
    assert req.product_images == [OTHER]
    assert (req.classification, req.closeups) == (None, None)


async def test_source_run_needs_the_image_it_made(monkeypatch, storage):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)],
        person_image_base64=b64(PERSON),
        source_run=runs.folder_uri(_try_on_run(), "outputs"),
    )

    assert "tryon_image" in result["error"] and run.requests == []


async def test_a_try_on_result_names_its_run_record(monkeypatch, storage):
    kept = RecordEvent({"version": 1}, {"photo-0": SHOES})
    monkeypatch.setattr(tools, "run_tryon", events(kept, COMPLETE))

    result = await tools.try_on(
        [b64(PRODUCT)], b64(PERSON), output="gcs", run_id="0123456789ab"
    )

    assert result["run"]["manifest"].endswith("/0123456789ab/manifest.json")
    inline = await tools.try_on([b64(PRODUCT)], b64(PERSON), output="inline")
    assert inline["run"]["manifest"] is None
