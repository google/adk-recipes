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

"""Images are sized for each model when sent: ``media.fit`` and ``core.gen``."""

import base64
import functools
import io

import numpy as np
import pytest
from google.genai import types
from PIL import Image, JpegImagePlugin

from genmedia4commerce.core import clients, gen, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.testing import png
from tests.core.test_gen import (
    StubClient,
    _interaction,
    image_response,
    text_response,
)

CONTENT = {"max_pixels": 5_160_960, "multiple": 16}
BACKGROUND = (200, 180, 160)


def encode(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def load(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@functools.cache
def photo_24mp() -> bytes:
    """A 6000x4000 JPEG (24 MP)."""
    return encode(Image.new("RGB", (6000, 4000), BACKGROUND), "JPEG")


def portrait(w: int = 900, h: int = 1200) -> bytes:
    """A 3:4 image: a dark product on a plain background."""
    img = Image.new("RGB", (w, h), BACKGROUND)
    img.paste((20, 20, 20), (w // 4, h // 4, 3 * w // 4, 3 * h // 4))
    return encode(img)


def near(pixel, color, tolerance: int = 4) -> bool:
    return all(
        abs(a - b) <= tolerance for a, b in zip(pixel, color, strict=True)
    )


# ---------------------------------------------------------------- media.fit


def test_images_within_the_limits_are_returned_unchanged():
    small = png(640, 640)
    assert media.fit(small, **CONTENT) is small
    assert media.fit(small, box=(1280, 720)) is small
    exact = png(720, 1280)
    assert media.fit(exact, box=(720, 1280), pad=True) is exact


def test_max_pixels_scales_down_to_multiples_of_16_as_jpeg():
    out = media.fit(photo_24mp(), **CONTENT)
    img = load(out)
    w, h = img.size
    assert w * h <= 5_160_960
    assert w % 16 == h % 16 == 0
    assert (w, h) == (2768, 1840)  # 6000x4000 scaled by 0.4637, rounded down
    assert media.sniff_mime(out) == "image/jpeg"
    assert JpegImagePlugin.get_sampling(img) == 0  # 4:4:4


def test_transparent_images_stay_png():
    cutout = encode(Image.new("RGBA", (3000, 2000), (9, 9, 9, 0)))
    out = load(media.fit(cutout, **CONTENT))
    assert out.format == "PNG"
    assert out.mode == "RGBA"
    assert out.size == (2768, 1840)


def test_exif_orientation_is_applied_even_within_the_limits():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees to display
    rotated = encode(Image.new("RGB", (300, 200)), "JPEG", exif=exif)
    out = load(media.fit(rotated, **CONTENT))
    assert out.size == (200, 300)
    assert out.getexif().get(0x0112) is None


def test_box_fits_inside_without_padding():
    assert load(media.fit(png(2560, 1440), box=(1280, 720))).size == (
        1280,
        720,
    )
    assert load(media.fit(png(1000, 2000), box=(1280, 720))).size == (360, 720)


def test_pad_centres_on_the_images_own_background():
    out = load(media.fit(portrait(), box=(720, 1280), pad=True))
    assert out.size == (720, 1280)
    pixels = np.asarray(out.convert("RGB"))
    # 900x1200 scales to 720x960: 160 rows of padding above and below.
    for y in (0, 80, 1279):
        assert near(pixels[y, 360], BACKGROUND), y
    assert near(pixels[640, 360], (20, 20, 20))


def test_pad_scales_small_images_up_to_fill_the_box():
    out = load(media.fit(png(64, 64, (10, 200, 10)), box=(720, 1280), pad=True))
    pixels = np.asarray(out.convert("RGB"))
    assert out.size == (720, 1280)
    # The square fills the full width (720x720), centred vertically.
    assert near(pixels[640, 2], (10, 200, 10))
    assert near(pixels[640, 717], (10, 200, 10))


def test_too_large_and_unreadable_images_are_input_errors():
    huge = encode(Image.new("1", (11000, 10000)))  # 110 MP
    with pytest.raises(InputError, match="too large"):
        media.fit(huge, **CONTENT)
    with pytest.raises(InputError, match="too large"):
        media.image_size(huge)
    with pytest.raises(InputError, match="unreadable"):
        media.image_size(png()[:40])


# ---------------------------------------------------------------- core.gen


@pytest.fixture
def client(monkeypatch):
    stub = StubClient()
    monkeypatch.setattr(clients, "get_client", lambda **_kw: stub)
    return stub


def sent_images(call) -> list[bytes]:
    return [p.inline_data.data for p in call["contents"][0].parts[1:]]


@pytest.mark.parametrize("fn", ["image", "json"])
def test_a_24mp_part_reaches_generate_content_at_5mp(client, fn):
    if fn == "image":
        client.models.responses = [image_response(png())]
        gen.generate_image(["make it", photo_24mp()])
    else:
        client.models.responses = [text_response("{}")]
        gen.generate_json(["judge it", photo_24mp()])
    (call,) = client.models.calls
    parts = call["contents"][0].parts
    assert parts[0].text in ("make it", "judge it")
    (image,) = sent_images(call)
    w, h = load(image).size
    assert w * h <= 5_160_960
    assert w % 16 == h % 16 == 0
    assert parts[1].inline_data.mime_type == "image/jpeg"


def test_small_and_transparent_parts_keep_their_format(client):
    client.models.responses = [text_response("ok")]
    small = png(640, 640)
    cutout = encode(Image.new("RGBA", (3000, 2000), (9, 9, 9, 0)))
    clip = (b"\x00\x00\x00\x18ftypmp42clip", {"fps": 2})
    gen.generate_text(["look", small, cutout, clip])
    parts = client.models.calls[0]["contents"][0].parts
    assert parts[1].inline_data.data == small  # byte-identical
    assert parts[2].inline_data.mime_type == "image/png"
    assert load(parts[2].inline_data.data).mode == "RGBA"
    assert parts[3].inline_data.data == clip[0]  # video untouched
    assert parts[3].video_metadata.fps == 2


def test_a_3_4_reference_reaches_the_video_model_as_720x1280(client):
    client.interactions.responses = [_interaction(b"VIDEO")]
    params = gen.VideoParams(aspect_ratio="9:16")
    gen.generate_video([portrait()], "walk", params=params)
    sent = client.interactions.calls[0]["input"][1]
    img = load(base64.b64decode(sent["data"]))
    assert img.size == (720, 1280)
    assert sent["mime_type"] == "image/png"  # lossless for the video model
    pixels = np.asarray(img.convert("RGB"))
    assert near(pixels[10, 360], BACKGROUND)  # not black padding
    assert near(pixels[1270, 360], BACKGROUND)


def test_landscape_videos_use_the_1280x720_box(client):
    client.interactions.responses = [_interaction(b"VIDEO")]
    gen.generate_video([portrait()], "spin")  # default 16:9
    sent = client.interactions.calls[0]["input"][1]
    assert load(base64.b64decode(sent["data"])).size == (1280, 720)


def test_embeddings_get_their_images_unchanged(client):
    values = types.ContentEmbedding(values=[1.0, 0.0])
    client.models.responses = [types.EmbedContentResponse(embeddings=[values])]
    gen.embed([photo_24mp()], model="emb")
    part = client.models.calls[0]["contents"][0].parts[0]
    assert part.inline_data.data == photo_24mp()


def test_lossless_is_png_where_the_default_is_jpeg():
    noise = np.random.default_rng(0).integers(0, 256, (300, 400, 3), np.uint8)
    data = encode(Image.fromarray(noise))
    lossless = media.fit(data, box=(1280, 720), pad=True, lossless=True)
    lossy = media.fit(data, box=(1280, 720), pad=True)
    assert media.sniff_mime(lossless) == "image/png"
    assert media.sniff_mime(lossy) == "image/jpeg"
    a, b = (
        np.asarray(load(x).convert("RGB")).astype(int)
        for x in (lossless, lossy)
    )
    assert a.shape == b.shape == (720, 1280, 3)
    assert np.abs(a - b).mean() > 0  # JPEG only approximates the resize
