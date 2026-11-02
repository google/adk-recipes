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

"""Shared request building: run defaults, deadlines and input checks."""

import functools
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce import requests, tools
from genmedia4commerce.core import clients, gcs, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.gen import VideoParams
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import outfit
from tests.conftest import HEIC
from tests.core.test_gen import image_response, text_response
from tests.mcp.test_tools import COMPLETE, PERSON, PRODUCT, b64, events
from tests.workflows.tryon.test_pipeline import _classified, _img

INLINE_LIMIT = 7 * 2**20  # the image model's limit per inline image
async_test = pytest.mark.asyncio


def encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def load(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@functools.cache
def phone_photo() -> bytes:
    """A 4000x3000 JPEG with sensor-like noise (3.9 MB; 32 MB as PNG)."""
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:3000, 0:4000]
    base = np.stack([x * 255 // 4000, y * 255 // 3000, (x + y) // 28], -1)
    noise = rng.normal(0, 12, base.shape)
    pixels = np.clip(base + noise, 0, 255).astype(np.uint8)
    return encode(Image.fromarray(pixels), "JPEG", quality=90)


# --- run defaults (R5) and deadlines (R11) ----------------------------------


@pytest.fixture
def deployment_defaults(monkeypatch):
    monkeypatch.setenv("GENMEDIA_NUM_VARIATIONS", "1")
    monkeypatch.setenv("GENMEDIA_RUN_MODE", "sequential")
    get_settings.cache_clear()


BUILDS = {
    "tryon": lambda **kw: requests.tryon_request([PRODUCT], PERSON, **kw),
    "video": lambda **kw: requests.video_tryon_request(
        tryon_image=PERSON, **kw
    ),
    "spin": lambda **kw: requests.spin_request([PRODUCT], **kw),
}


@pytest.mark.parametrize("kind", BUILDS)
def test_unset_options_follow_the_deployment(deployment_defaults, kind):
    policy = BUILDS[kind]().policy

    assert (policy.num_variations, policy.mode) == (1, "sequential")


@pytest.mark.parametrize("kind", BUILDS)
def test_request_options_override_the_deployment(deployment_defaults, kind):
    policy = BUILDS[kind](num_variations=4, mode="parallel").policy

    assert (policy.num_variations, policy.mode) == (4, "parallel")


@async_test
async def test_tools_follow_the_deployment(deployment_defaults, monkeypatch):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)

    await tools.try_on([b64(PRODUCT)], b64(PERSON))

    policy = run.requests[0].policy
    assert (policy.num_variations, policy.mode) == (1, "sequential")


def test_video_runs_have_time_to_evaluate_after_the_video():
    """The video call, asked again once when it stalls, then evaluation."""
    video_budget = 2 * VideoParams().deadline_s + requests.EVAL_BUDGET_S

    assert BUILDS["video"]().policy.deadline_s == video_budget == 780
    assert BUILDS["spin"]().policy.deadline_s == video_budget
    assert BUILDS["tryon"]().policy.deadline_s == 900


def test_video_motion_defaults_to_auto():
    assert BUILDS["video"]().motion == "auto"
    assert BUILDS["video"](motion="").motion == "auto"


# --- input checks before any upload or run event (R16) -----------------------


@async_test
@pytest.mark.parametrize(
    ("tool", "workflow", "kwargs", "message"),
    [
        (
            tools.try_on,
            "run_tryon",
            {"product_images_base64": [b64(b"%PDF-1.7 not an image")]},
            "not a PNG, JPEG, WebP, HEIC or AVIF image",
        ),
        (
            tools.try_on,
            "run_tryon",
            {"product_images_base64": [b64(PRODUCT)] * 21},
            "product_images: 1 to 20 images, got 21",
        ),
        (
            tools.spin_360,
            "run_spin",
            {"product_images_base64": [b64(PRODUCT)] * 9},
            "product_images: 1 to 8 images, got 9",
        ),
        (
            tools.spin_360,
            "run_spin",
            {"product_images_base64": [b64(PRODUCT[:40])]},
            "unreadable image",
        ),
    ],
)
async def test_bad_images_fail_before_anything_is_saved(
    monkeypatch, storage, tool, workflow, kwargs, message
):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, workflow, run)
    defaults = {
        tools.try_on: {
            "product_images_base64": [b64(PRODUCT)],
            "person_image_base64": b64(PERSON),
        },
        tools.video_try_on: {"tryon_image_base64": b64(PERSON)},
        tools.spin_360: {},
    }[tool]

    result = await tool(**{**defaults, **kwargs}, input="gcs")

    assert set(result) == {"error", "retryable"}  # no run was started
    assert message in result["error"]
    assert storage.uploads == {}
    assert run.requests == []


@async_test
async def test_images_over_the_size_limit_are_refused(monkeypatch, storage):
    monkeypatch.setattr(tools, "run_spin", events(COMPLETE))
    huge = b"\x89PNG\r\n\x1a\n" + bytes(gcs.MAX_IMAGE_BYTES)

    result = await tools.spin_360([b64(huge)], input="gcs")

    assert "at most 20 MB" in result["error"]
    assert storage.uploads == {}


# --- inputs are kept as sent; gen sizes them per model ----------------------


@async_test
async def test_inputs_reach_the_workflow_untouched(monkeypatch, storage):
    """No resizing at the entrance: crops are cut from the originals. Only
    a photo with an EXIF turn is re-encoded, upright."""
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)
    photo = phone_photo()
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees to display
    rotated = encode(Image.new("RGB", (3000, 2000)), "JPEG", exif=exif)
    cutout = encode(Image.new("RGBA", (3000, 1000), (9, 9, 9, 0)), "PNG")

    result = await tools.try_on(
        product_images_base64=[b64(photo), b64(cutout)],
        person_image_base64=b64(rotated),
        reference_model_image_base64=b64(photo),
        input="gcs",
    )

    assert "error" not in result, result
    (req,) = run.requests
    assert req.product_images == [photo, cutout]
    assert req.reference_model_image == photo
    upright = req.person_image
    assert load(upright).size == (2000, 3000)
    assert load(upright).getexif().get(0x0112, 1) == 1
    assert sorted(storage.uploads.values(), key=len) == sorted(
        [photo, photo, cutout, upright], key=len
    )


@async_test
async def test_images_over_100_megapixels_are_refused(monkeypatch, storage):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_spin", run)
    huge = encode(Image.new("1", (11000, 10000)), "PNG")  # 110 MP, small file

    result = await tools.spin_360([b64(huge)], input="gcs")

    assert "product_images: image too large" in result["error"]
    assert storage.uploads == {}
    assert run.requests == []


class ModelStub:
    """The genai client's ``models``: canned answers, every request kept."""

    def __init__(self, classified: dict):
        self.classified = classified
        self.calls: list[tuple] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append((config, contents[0].parts))
        if config.response_modalities == ["IMAGE"]:
            return image_response(PRODUCT)
        system = config.system_instruction or ""
        assert "classifier" in system, f"unexpected text call: {system[:80]}"
        return text_response(json.dumps(self.classified))


@async_test
async def test_phone_photos_reach_the_models_at_their_size(monkeypatch):
    """The real ``core.gen`` sends every image at 5.16 MP at most, sides % 16."""
    monkeypatch.setattr(media, "face_box", lambda data: None)
    monkeypatch.setattr(
        outfit,
        "evaluate_outfit",
        lambda products, target, **_k: Verdict(True, details={"score": 90}),
    )
    stub = ModelStub(_classified(("A", "top", [_img(0)])))
    monkeypatch.setattr(
        clients, "get_client", lambda **kw: SimpleNamespace(models=stub)
    )

    result = await tools.try_on(
        [b64(phone_photo())], b64(phone_photo()), num_variations=1
    )

    assert result["best_index"] == 0, result
    sent = [
        part.inline_data.data
        for config, parts in stub.calls
        for part in parts
        if part.inline_data
    ]
    image_call = next(
        parts for config, parts in stub.calls if config.response_modalities
    )
    assert len([p for p in image_call if p.inline_data]) == 2
    assert sent and all(len(image) <= INLINE_LIMIT for image in sent)
    for image in sent:
        w, h = load(image).size
        assert w * h <= 5_160_960 and w % 16 == h % 16 == 0
    # The 4000x3000 photos arrive at 2608x1952.
    assert (2608, 1952) in {load(image).size for image in sent}


@async_test
async def test_heic_and_avif_inputs_are_converted_to_jpeg(monkeypatch):
    """iPhone photos (HEIC) and shop images (AVIF) are accepted everywhere."""
    buf = io.BytesIO()
    Image.new("RGB", (80, 60), (10, 200, 10)).save(buf, format="AVIF")
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_spin", run)
    for data in (HEIC, buf.getvalue()):
        result = await tools.spin_360([b64(data)])
        assert "error" not in result, result
        sent = run.requests[-1].product_images[0]
        assert media.sniff_mime(sent) == "image/jpeg"
        assert load(sent).size == load(data).size


def test_a_spin_picks_its_aspect_ratio_unless_one_is_chosen():
    assert requests.spin_request([PRODUCT]).video.aspect_ratio == "auto"
    chosen = requests.spin_request([PRODUCT], aspect_ratio="9:16")
    assert chosen.video.aspect_ratio == "9:16"
    with pytest.raises(InputError, match="aspect_ratio"):
        requests.spin_request([PRODUCT], aspect_ratio="4:3")
