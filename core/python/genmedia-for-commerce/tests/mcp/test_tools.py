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

"""The four tool functions: input decoding, request building, result shape."""

import base64

import pytest

from genmedia4commerce import requests, tools
from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import (
    ConfigError,
    GenerationError,
    InputError,
    StorageError,
)
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Verdict

pytestmark = pytest.mark.asyncio

PRODUCT = png(10, 10, (1, 2, 3))
PERSON = png(12, 12, (4, 5, 6))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def events(*items):
    """Fake workflow: records its request and yields ``items``."""
    requests = []

    async def run(req):
        requests.append(req)
        for item in items:
            if isinstance(item, BaseException):
                raise item
            yield item

    run.requests = requests
    return run


VARIATION = VariationEvent(
    index=0,
    status="ready",
    media=PRODUCT,
    verdict=Verdict(publish=True, description="ok", details={"score": 90}),
    score=90.0,
    extra={"view": "front"},
)
VIDEO = VariationEvent(
    index=0,
    status="ready",
    media=b"\x00\x00\x00\x18ftypmp42clip",
    verdict=Verdict(publish=True),
    score=90.0,
    extra={"stage": "video"},
)
COMPLETE = CompleteEvent(ranked_indices=[0], best_index=0)


async def test_try_on_returns_contract_shape(monkeypatch):
    run = events(
        ClassifiedEvent(payload={"mode": "single"}),
        ProgressEvent("preprocessed", "Generating the front view"),
        VARIATION,
        COMPLETE,
    )
    monkeypatch.setattr(tools, "run_tryon", run)

    result = await tools.try_on([b64(PRODUCT)], b64(PERSON))

    assert result["classification"] == {"mode": "single"}
    assert result["best_index"] == 0
    assert result["ranked_indices"] == [0]
    (variation,) = result["variations"]
    assert variation["index"] == 0
    assert variation["status"] == "ready"
    assert variation["media_base64"] == b64(PRODUCT)
    assert variation["media_mime"] == "image/png"
    assert variation["verdict"]["publish"] is True
    assert variation["score"] == 90.0
    assert variation["attempt"] == 1
    assert variation["extra"] == {"view": "front"}


async def test_try_on_builds_request(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)

    await tools.try_on(
        [b64(PRODUCT), b64(PERSON)],
        b64(PERSON),
        reference_model_image_base64=b64(PRODUCT),
        style_guidelines="jackets closed",
        generate_back=False,
        num_variations=2,
        mode="sequential",
        aspect_ratio="1:1",
        image_size="1K",
    )

    (req,) = run.requests
    assert req.product_images == [PRODUCT, PERSON]
    assert req.person_image == PERSON
    assert req.reference_model_image == PRODUCT
    assert req.style_guidelines == "jackets closed"
    assert req.scenario is None
    assert req.generate_back is False
    assert (req.policy.mode, req.policy.num_variations) == ("sequential", 2)
    assert req.image == ImageParams(aspect_ratio="1:1", image_size="1K")


async def test_empty_strings_mean_absent(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)

    await tools.try_on([b64(PRODUCT)], b64(PERSON))

    (req,) = run.requests
    assert req.reference_model_image is None
    assert req.style_guidelines is None


async def test_gs_uri_inputs_are_resolved(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)
    uris = {"gs://b/p.png": PRODUCT}
    monkeypatch.setattr(gcs, "resolve_image", lambda ref: uris.get(ref, ref))

    await tools.try_on(["gs://b/p.png"], b64(PERSON))

    assert run.requests[0].product_images == [PRODUCT]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"product_images_base64": ["not base64!"]},
        {"person_image_base64": ""},
        {"mode": "fast"},
        {"num_variations": 0},
        {"num_variations": requests.MAX_VARIATIONS + 1},
        {"aspect_ratio": "7:3"},
        {"image_size": "8K"},
    ],
)
async def test_try_on_bad_input_returns_error(monkeypatch, kwargs):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)
    args = {
        "product_images_base64": [b64(PRODUCT)],
        "person_image_base64": b64(PERSON),
    }

    result = await tools.try_on(**{**args, **kwargs})

    assert set(result) == {"error", "retryable"}
    assert result["retryable"] is False
    assert run.requests == []


async def test_error_event_becomes_error_dict(monkeypatch):
    monkeypatch.setattr(
        tools, "run_tryon", events(ErrorEvent(message="quota", retryable=True))
    )

    result = await tools.try_on([b64(PRODUCT)], b64(PERSON))

    assert result["error"] == "quota"
    assert result["retryable"] is True
    assert result["image_variations"] == []
    assert result["run"]["session_id"] == "no-session"


async def test_error_keeps_the_try_on_images_made_before_it(monkeypatch):
    image = VariationEvent(
        index=0,
        status="rejected",
        media=PRODUCT,
        verdict=Verdict(publish=False, description="sleeve differs"),
        score=40.0,
        extra={"view": "front", "stage": "image"},
    )
    no_image = ErrorEvent(message="no publishable image", retryable=True)
    monkeypatch.setattr(tools, "run_video_tryon", events(image, no_image))

    result = await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)], person_image_base64=b64(PERSON)
    )

    assert (result["error"], result["retryable"]) == (
        "no publishable image",
        True,
    )
    (kept,) = result["image_variations"]
    assert kept["media_base64"] == b64(PRODUCT)
    assert kept["verdict"]["description"] == "sleeve differs"
    assert set(result["run"]) == {
        "run_id",
        "session_id",
        "inputs",
        "outputs",
        "manifest",
    }


async def test_workflow_exception_becomes_error_dict(monkeypatch):
    monkeypatch.setattr(
        tools, "run_video_tryon", events(InputError("bad motion"))
    )

    result = await tools.video_try_on(tryon_image_base64=b64(PERSON))

    assert result == {"error": "bad motion", "retryable": False}


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("403 gs://secret-bucket for sa@x.iam"),
        StorageError("could not read gs://secret-bucket/x.json"),
        ConfigError("The catalogue file is missing: /srv/secret-bucket"),
    ],
)
async def test_internal_errors_are_hidden_from_the_caller(
    monkeypatch, caplog, error
):
    monkeypatch.setattr(tools, "run_video_tryon", events(error))

    result = await tools.video_try_on(tryon_image_base64=b64(PERSON))

    assert result == {"error": tools.TOOL_FAILED, "retryable": False}
    assert "secret-bucket" in caplog.text  # logged with its traceback


async def test_generation_errors_keep_their_message(monkeypatch):
    error = GenerationError("429 quota exceeded", retryable=True)
    monkeypatch.setattr(tools, "run_video_tryon", events(error))

    result = await tools.video_try_on(tryon_image_base64=b64(PERSON))

    assert result == {"error": "429 quota exceeded", "retryable": True}


async def test_video_try_on_builds_request(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    await tools.video_try_on(
        tryon_image_base64=b64(PERSON),
        back_image_base64=b64(PRODUCT),
        product_images_base64=[b64(PRODUCT)],
        motion="custom",
        prompt="spin slowly",
        num_variations=1,
        aspect_ratio="16:9",
    )

    (req,) = run.requests
    assert req.tryon_image == PERSON
    assert req.back_image == PRODUCT
    assert req.product_images == [PRODUCT]
    assert (req.motion, req.prompt) == ("custom", "spin slowly")
    assert req.policy.num_variations == 1
    assert req.video == VideoParams(aspect_ratio="16:9", resolution="720p")
    assert req.classification is None
    assert req.person_image is None


async def test_video_try_on_defaults(monkeypatch):
    run = events(VIDEO, COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(tryon_image_base64=b64(PERSON))

    (req,) = run.requests
    assert req.back_image is None
    assert req.product_images == []
    assert (req.motion, req.prompt) == ("auto", None)
    assert req.policy.deadline_s == 780  # the video call twice, then eval
    assert req.video == VideoParams(aspect_ratio="9:16", resolution="720p")
    assert result["classification"] is None
    assert "image_variations" not in result
    assert "selected" not in result
    assert len(result["variations"]) == 1


@pytest.mark.parametrize(
    ("motion", "expected"),
    [("", "auto"), ("auto", "auto"), ("custom", "custom")],
)
async def test_a_back_image_keeps_the_motion_asked_for(
    monkeypatch, motion, expected
):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    await tools.video_try_on(
        tryon_image_base64=b64(PERSON),
        back_image_base64=b64(PRODUCT),
        motion=motion,
        prompt="turn slowly",
    )

    assert run.requests[0].motion == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"motion": "dance"},
        {"motion": "catwalk"},
        {"motion": "turnaround"},
        {"motion": "custom"},
        {"motion": "custom", "prompt": "   "},
        {"aspect_ratio": "3:4"},
        {"image_size": "8K"},
        {"resolution": "4k"},
    ],
)
async def test_video_try_on_bad_input(monkeypatch, kwargs):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(tryon_image_base64=b64(PERSON), **kwargs)

    assert "error" in result
    assert run.requests == []


async def test_spin_360_builds_request(monkeypatch):
    run = events(VARIATION, COMPLETE)
    monkeypatch.setattr(tools, "run_spin", run)

    result = await tools.spin_360(
        [b64(PRODUCT)],
        num_variations=2,
        aspect_ratio="9:16",
        resolution="1080p",
    )

    (req,) = run.requests
    assert req.product_images == [PRODUCT]
    assert req.policy.num_variations == 2
    assert req.video == VideoParams(aspect_ratio="9:16", resolution="1080p")
    assert len(result["variations"]) == 1


async def test_spin_360_bad_resolution(monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_spin", run)

    result = await tools.spin_360([b64(PRODUCT)], resolution="480p")

    assert "resolution must be one of 720p, 1080p" in result["error"]
    assert run.requests == []


async def test_catalog_search(catalog_file, fake_gen):
    fake_gen.embed(GenerationError("down"))  # keyword-only
    result = await tools.catalog_search(
        "black jacket", k=1, audience="men, unisex"
    )
    (item,) = result["items"]
    assert (item["id"], item["ref"], item["title"]) == (
        "pb2",
        "catalog:pb2",
        "Black leather jacket",
    )
    assert await tools.catalog_search("black jacket", audience="women") == {
        "items": []
    }


async def test_catalog_search_without_a_query_lists_the_featured(
    catalog_file,
):
    result = await tools.catalog_search("", k=5)
    assert [i["ref"] for i in result["items"]] == [
        "catalog:pa1",
        "catalog:p_glasses_4",
    ]


async def test_catalog_search_error(catalog_file):
    assert await tools.catalog_search("x", k=0) == {
        "error": "k must be between 1 and 50",
        "retryable": False,
    }


async def test_catalogue_refs_are_image_inputs(catalog_file):
    assert tools._ref("catalog:pa1", "person_image_base64") == "catalog:pa1"


async def test_stream_yields_the_workflow_events_with_the_tool_request(
    monkeypatch,
):
    run = events(
        ProgressEvent("preprocessed", "Generating"), VARIATION, COMPLETE
    )
    monkeypatch.setattr(tools, "run_tryon", run)

    stream = await tools.stream(
        "try_on",
        product_images_base64=[b64(PRODUCT)],
        person_image_base64=b64(PERSON),
        num_variations=2,
    )
    kinds = [type(e).__name__ async for e in stream]

    assert kinds[-3:] == ["ProgressEvent", "VariationEvent", "CompleteEvent"]
    (req,) = run.requests
    assert req.policy.num_variations == 2
    assert req.image.aspect_ratio == "3:4"  # the tool's default


async def test_stream_raises_on_an_invalid_request():
    with pytest.raises(InputError):
        await tools.stream("catalog_search", query="x")
    with pytest.raises(TypeError):
        await tools.stream("spin_360", product_images_base64=[], colour="red")
    with pytest.raises(InputError):
        await tools.stream(
            "video_try_on",
            product_images_base64=[b64(PRODUCT)],
            person_image_base64=b64(PERSON),
            motion="catwalk",
        )
