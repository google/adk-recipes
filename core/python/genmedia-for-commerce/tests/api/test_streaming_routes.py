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

"""POST /api/tryon, /api/video-tryon, /api/spin: validation and SSE."""

import json

import pytest

from genmedia4commerce import requests
from genmedia4commerce.api import spin, sse, tryon, video_tryon
from genmedia4commerce.core.errors import SERVER_FAILED
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from tests.api.conftest import IMAGE, fake_workflow, frames


def files(field: str, n: int, data: bytes = IMAGE) -> list[tuple]:
    return [(field, (f"{field}{i}.png", data, "image/png")) for i in range(n)]


@pytest.fixture
def run_tryon(monkeypatch):
    run = fake_workflow()
    monkeypatch.setattr(tryon, "run_tryon", run)
    return run


@pytest.fixture
def run_video(monkeypatch):
    run = fake_workflow()
    monkeypatch.setattr(video_tryon, "run_video_tryon", run)
    return run


@pytest.fixture
def run_spin(monkeypatch):
    run = fake_workflow()
    monkeypatch.setattr(spin, "run_spin", run)
    return run


def test_tryon_streams_events_with_sse_headers(client, run_tryon):
    response = client.post(
        "/api/tryon",
        files=files("product_images", 2) + files("person_image", 1),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    events = frames(response.text)
    assert [e["type"] for e in events] == ["progress", "variation", "complete"]
    assert events[0]["stage"] == "run"
    assert events[1]["media_mime"] == "image/png"


def test_tryon_builds_request_from_form(client, run_tryon):
    client.post(
        "/api/tryon",
        files=files("product_images", 1)
        + files("person_image", 1)
        + files("reference_model_image", 1),
        data={
            "style_guidelines": "tucked",
            "scenario": "beach",
            "generate_back": "false",
            "complete_look": "false",
            "upscale_crops": "true",
            "num_variations": "2",
            "mode": "sequential",
            "aspect_ratio": "1:1",
            "image_size": "4K",
        },
    )

    (req,) = run_tryon.requests
    assert req.product_images == [IMAGE]
    assert req.person_image == IMAGE
    assert req.reference_model_image == IMAGE
    assert (req.style_guidelines, req.scenario) == ("tucked", "beach")
    assert req.generate_back is False
    assert req.complete_look is False
    assert req.upscale_crops is True
    assert (req.policy.mode, req.policy.num_variations) == ("sequential", 2)
    assert req.image == ImageParams(aspect_ratio="1:1", image_size="4K")


def test_tryon_defaults(client, run_tryon):
    client.post(
        "/api/tryon",
        files=files("product_images", 1) + files("person_image", 1),
    )

    (req,) = run_tryon.requests
    assert req.generate_back is True
    assert req.complete_look is True
    assert req.upscale_crops is False
    assert req.reference_model_image is None
    assert req.image == ImageParams()


@pytest.mark.parametrize(
    ("extra_files", "data"),
    [
        (files("product_images", 0) + files("person_image", 1), {}),
        (files("product_images", 9) + files("person_image", 1), {}),
        (files("product_images", 1), {}),
        (files("product_images", 1) + files("person_image", 1, b""), {}),
        (
            files("product_images", 1) + files("person_image", 1),
            {"aspect_ratio": "7:2"},
        ),
        (
            files("product_images", 1) + files("person_image", 1),
            {"mode": "turbo"},
        ),
    ],
)
def test_tryon_rejects_bad_input_with_400(client, run_tryon, extra_files, data):
    response = client.post("/api/tryon", files=extra_files, data=data)

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    assert run_tryon.requests == []


def test_file_over_size_limit_is_400(client, run_tryon, monkeypatch):
    monkeypatch.setattr(sse, "MAX_IMAGE_BYTES", len(IMAGE) - 1)

    response = client.post(
        "/api/tryon",
        files=files("product_images", 1) + files("person_image", 1),
    )

    assert response.status_code == 400
    assert "MB" in response.json()["detail"]


def test_total_over_size_limit_is_400(client, run_tryon, monkeypatch):
    monkeypatch.setattr(sse, "MAX_TOTAL_BYTES", 2 * len(IMAGE))

    response = client.post(
        "/api/tryon",
        files=files("product_images", 2) + files("person_image", 1),
    )

    assert response.status_code == 400


def test_video_tryon_builds_request(client, run_video):
    classification = {
        "mode": "single",
        "framing": "footwear",
        "products": [
            {"id": "A", "category": "footwear", "description": "shoe"}
        ],
    }
    response = client.post(
        "/api/video-tryon",
        files=files("tryon_image", 1)
        + files("back_image", 1)
        + files("product_images", 2),
        data={
            "classification": json.dumps(classification),
            "motion": "custom",
            "prompt": "walks, then the bag",
            "complete_look": "false",
            "upscale_crops": "true",
            "num_variations": "1",
            "aspect_ratio": "16:9",
            "resolution": "1080p",
        },
    )

    assert response.status_code == 200
    (req,) = run_video.requests
    assert req.tryon_image == IMAGE
    assert req.back_image == IMAGE
    assert len(req.product_images) == 2
    assert req.classification.framing == "footwear"
    assert req.classification.products[0].category == "footwear"
    assert (req.motion, req.prompt) == ("custom", "walks, then the bag")
    assert req.complete_look is False
    assert req.upscale_crops is True
    assert req.video == VideoParams(aspect_ratio="16:9", resolution="1080p")
    assert req.videos_per_image == 1  # an image: num_variations videos


def test_video_tryon_takes_the_source_run(client, run_video, monkeypatch):
    seen = []

    def load_record(source_run):
        seen.append(source_run)  # a run that kept no record

    monkeypatch.setattr(requests, "load_record", load_record)
    uri = "gs://bucket/runs/2026-01-02/s/0123456789ab/manifest.json"
    response = client.post(
        "/api/video-tryon",
        files=files("tryon_image", 1) + files("product_images", 1),
        data={"source_run": uri},
    )

    assert response.status_code == 200
    assert seen == [uri]
    (req,) = run_video.requests
    assert len(req.product_images) == 1 and req.closeups is None


def test_video_tryon_defaults(client, run_video):
    client.post("/api/video-tryon", files=files("tryon_image", 1))

    (req,) = run_video.requests
    assert req.classification is None
    assert req.person_image is None
    assert (req.motion, req.prompt) == ("auto", None)
    assert req.video == VideoParams(aspect_ratio="9:16")


def test_video_tryon_motion_defaults_to_auto_with_a_back_view(
    client, run_video
):
    client.post(
        "/api/video-tryon",
        files=files("tryon_image", 1) + files("back_image", 1),
    )

    assert run_video.requests[0].motion == "auto"


@pytest.mark.parametrize("motion", ["catwalk", "turnaround", "dance"])
def test_video_tryon_old_or_unknown_motions_are_rejected(
    client, run_video, motion
):
    response = client.post(
        "/api/video-tryon",
        files=files("tryon_image", 1),
        data={"motion": motion},
    )

    assert response.status_code == 400
    assert run_video.requests == []


def test_unset_run_options_follow_the_deployment(
    client, run_tryon, monkeypatch
):
    monkeypatch.setenv("GENMEDIA_NUM_VARIATIONS", "2")
    monkeypatch.setenv("GENMEDIA_RUN_MODE", "sequential")
    get_settings.cache_clear()

    client.post(
        "/api/tryon",
        files=files("product_images", 1) + files("person_image", 1),
    )

    policy = run_tryon.requests[0].policy
    assert (policy.mode, policy.num_variations) == ("sequential", 2)


def test_video_tryon_end_to_end_builds_request(client, run_video):
    person, reference = png(9, 9, (1, 1, 1)), png(7, 7, (2, 2, 2))
    response = client.post(
        "/api/video-tryon",
        files=files("product_images", 2)
        + files("person_image", 1, person)
        + files("reference_model_image", 1, reference),
        data={
            "style_guidelines": "tucked",
            "scenario": "beach",
            "image_size": "1K",
            "num_variations": "2",
            "mode": "sequential",
        },
    )

    assert response.status_code == 200
    assert [e["type"] for e in frames(response.text)] == [
        "progress",
        "variation",
        "complete",
    ]
    (req,) = run_video.requests
    assert req.tryon_image is None
    assert req.product_images == [IMAGE, IMAGE]
    assert req.person_image == person
    assert req.reference_model_image == reference
    assert (req.style_guidelines, req.scenario) == ("tucked", "beach")
    assert req.image == ImageParams(aspect_ratio="9:16", image_size="1K")
    assert req.motion == "auto"
    assert (req.policy.mode, req.policy.num_variations) == ("sequential", 2)
    assert req.video == VideoParams(aspect_ratio="9:16", resolution="720p")


def test_video_tryon_end_to_end_default_image_size(client, run_video):
    client.post(
        "/api/video-tryon",
        files=files("product_images", 1) + files("person_image", 1),
    )

    (req,) = run_video.requests
    assert req.image == ImageParams(aspect_ratio="9:16", image_size="2K")
    assert req.reference_model_image is None
    assert (req.style_guidelines, req.scenario) == (None, None)


@pytest.mark.parametrize(
    "extra_files",
    [
        [],
        files("person_image", 1),
        files("product_images", 2),
        files("back_image", 1),
    ],
)
def test_video_tryon_needs_tryon_image_or_person_and_products(
    client, run_video, extra_files
):
    response = client.post("/api/video-tryon", files=extra_files or None)

    assert response.status_code == 400
    assert "tryon_image" in response.json()["detail"]
    assert run_video.requests == []


@pytest.mark.parametrize(
    ("extra_files", "data"),
    [
        ([], {}),
        (files("tryon_image", 1), {"motion": "custom"}),
        (files("tryon_image", 1) + files("product_images", 9), {}),
        (files("tryon_image", 1), {"classification": "{not json"}),
        (files("tryon_image", 1), {"classification": '{"framing": "knees"}'}),
        (files("tryon_image", 1), {"aspect_ratio": "3:4"}),
        (files("tryon_image", 1), {"image_size": "8K"}),
        (files("tryon_image", 1), {"resolution": "4K"}),
        (files("tryon_image", 1) + files("person_image", 1, b""), {}),
    ],
)
def test_video_tryon_rejects_bad_input(client, run_video, extra_files, data):
    response = client.post("/api/video-tryon", files=extra_files, data=data)

    assert response.status_code == 400
    assert run_video.requests == []


def test_spin_builds_request(client, run_spin):
    response = client.post(
        "/api/spin",
        files=files("product_images", 3),
        data={
            "num_variations": "2",
            "mode": "sequential",
            "aspect_ratio": "9:16",
        },
    )

    assert [e["type"] for e in frames(response.text)] == [
        "progress",
        "variation",
        "complete",
    ]
    (req,) = run_spin.requests
    assert len(req.product_images) == 3
    assert (req.policy.mode, req.policy.num_variations) == ("sequential", 2)
    assert req.video == VideoParams(aspect_ratio="9:16", resolution="720p")


def test_spin_rejects_bad_resolution(client, run_spin):
    response = client.post(
        "/api/spin",
        files=files("product_images", 1),
        data={"resolution": "720"},
    )

    assert response.status_code == 400
    assert "resolution" in response.json()["detail"]
    assert run_spin.requests == []


@pytest.mark.parametrize("n", [0, 9])
def test_spin_rejects_file_count(client, run_spin, n):
    response = client.post(
        "/api/spin", files=files("product_images", n) or None
    )

    assert response.status_code == 400
    assert run_spin.requests == []


PDF = b"%PDF-1.4 not an image"


@pytest.mark.parametrize(
    ("route", "upload"),
    [
        (
            "/api/tryon",
            files("product_images", 1, PDF) + files("person_image", 1),
        ),
        ("/api/video-tryon", files("tryon_image", 1, PDF)),
        (
            "/api/spin",
            files("product_images", 2) + files("product_images", 1, PDF),
        ),
    ],
)
def test_non_image_upload_is_400(
    client, run_tryon, run_video, run_spin, route, upload
):
    response = client.post(route, files=upload)

    assert response.status_code == 400
    assert (
        "not a PNG, JPEG, WebP, HEIC or AVIF image" in response.json()["detail"]
    )
    assert run_tryon.requests == run_video.requests == run_spin.requests == []


def test_workflow_exception_streams_error_event(client, monkeypatch):
    async def failing(_req):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(spin, "run_spin", failing)

    response = client.post("/api/spin", files=files("product_images", 1))

    run, error = frames(response.text)
    assert run["stage"] == "run"
    assert error == {
        "type": "error",
        "message": SERVER_FAILED,  # not the exception's text
        "retryable": False,
    }
