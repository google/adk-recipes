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

import io
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.testing import mp4_stub, png
from tests.conftest import HEIC


def encode(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def load(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def gradient(w=128, h=96) -> bytes:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    arr = np.tile(x, (h, 1))
    arr[: h // 2, : w // 2] = 255 - arr[: h // 2, : w // 2]
    return encode(Image.fromarray(arr).convert("RGB"))


# ------------------------------------------------------------------ sniffing


def test_sniff_mime():
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    assert media.sniff_mime(encode(img, "PNG")) == "image/png"
    assert media.sniff_mime(encode(img, "JPEG")) == "image/jpeg"
    assert media.sniff_mime(encode(img, "WEBP")) == "image/webp"
    assert media.sniff_mime(mp4_stub(frames=2)) == "video/mp4"
    assert media.sniff_mime(b"\x00\x01garbage") == "application/octet-stream"
    assert media.sniff_mime(b"") == "application/octet-stream"


def test_to_png():
    jpeg = encode(Image.new("RGB", (10, 6), (9, 9, 9)), "JPEG")
    out = media.to_png(jpeg)
    assert media.sniff_mime(out) == "image/png"
    assert load(out).size == (10, 6)
    rgba = encode(Image.new("RGBA", (4, 4), (1, 2, 3, 0)))
    assert load(media.to_png(rgba)).mode == "RGBA"


def test_resize_max():
    big = png(400, 200)
    out = load(media.resize_max(big, 100))
    assert out.size == (100, 50)
    small = png(50, 20)
    assert media.resize_max(small, 100) == small


# ----------------------------------------------------------------- cropping


def test_crop_box_and_pad():
    img = png(100, 100)
    assert load(media.crop_box(img, (10, 20, 50, 60))).size == (40, 40)
    # 25% pad each side -> 10 px; clamped at the image border
    assert load(media.crop_box(img, (10, 20, 50, 60), pad=0.25)).size == (
        60,
        60,
    )
    assert load(media.crop_box(img, (0, 0, 100, 100), pad=0.5)).size == (
        100,
        100,
    )
    with pytest.raises(InputError):
        media.crop_box(img, (50, 50, 50, 80))


def test_crop_fraction():
    arr = np.zeros((100, 20, 3), dtype=np.uint8)
    arr[:50] = 255  # top half white, bottom half black
    img = encode(Image.fromarray(arr))
    top = load(media.crop_fraction(img, top=0.0, bottom=0.5))
    bottom = load(media.crop_fraction(img, top=0.5, bottom=1.0))
    assert top.size == bottom.size == (20, 50)
    assert np.asarray(top).min() == 255
    assert np.asarray(bottom).max() == 0
    assert load(media.crop_fraction(img, top=0.7)).size == (20, 30)
    for bad in ({"top": 0.6, "bottom": 0.4}, {"top": -0.1}, {"bottom": 1.2}):
        with pytest.raises(InputError):
            media.crop_fraction(img, **bad)


def _vertex(x, y):
    return SimpleNamespace(x=x, y=y)


def _face(x0, y0, x1, y1):
    vertices = [
        _vertex(x0, y0),
        _vertex(x1, y0),
        _vertex(x1, y1),
        _vertex(x0, y1),
    ]
    return SimpleNamespace(bounding_poly=SimpleNamespace(vertices=vertices))


class FakeVision:
    instances: ClassVar[list] = []
    faces: ClassVar[list] = []

    def __init__(self, client_options=None):
        self.client_options = client_options
        self.requests = []
        self.timeouts = []
        FakeVision.instances.append(self)

    def face_detection(self, image, timeout=None):
        self.requests.append(image)
        self.timeouts.append(timeout)
        return SimpleNamespace(face_annotations=FakeVision.faces)


@pytest.fixture
def fake_vision(monkeypatch):
    FakeVision.instances = []
    FakeVision.faces = []
    media._vision.cache_clear()
    monkeypatch.setattr(media.vision, "ImageAnnotatorClient", FakeVision)
    yield FakeVision
    media._vision.cache_clear()


def test_crop_face_picks_largest_face_with_margin(fake_vision):
    fake_vision.faces = [_face(0, 0, 10, 10), _face(40, 40, 60, 60)]
    out = media.crop_face(png(100, 100), margin=0.5)
    assert load(out).size == (40, 40)  # 20 px face + 10 px each side
    client = fake_vision.instances[0]
    assert client.client_options == {"quota_project_id": "test-project"}
    assert load(client.requests[0].content).size == (100, 100)


def test_crop_face_default_margin(fake_vision):
    fake_vision.faces = [_face(40, 40, 60, 60)]
    assert load(media.crop_face(png(100, 100))).size == (44, 44)


def test_crop_face_detects_on_2mp_and_crops_the_original(fake_vision):
    """Vision gets a copy of at most 1.92 MP; the crop keeps full detail."""
    big = png(4000, 3000)
    fake_vision.faces = [_face(400, 300, 500, 400)]  # in the 1600x1200 copy
    out = media.crop_face(big, margin=0.0)
    sent = load(fake_vision.instances[0].requests[0].content)
    assert sent.size == (1600, 1200)
    assert sent.format == "JPEG"
    assert load(out).size == (250, 250)  # the 100 px face, x2.5


def test_crop_face_none_when_no_face(fake_vision):
    assert media.crop_face(png()) is None
    assert media.face_box(png()) is None


def test_crop_face_reuses_one_client_with_a_timeout(fake_vision):
    for _ in range(3):
        media.crop_face(png())
    [client] = fake_vision.instances
    assert client.timeouts == [30, 30, 30]


# Eyes 20 px apart at y=50, chin at y=90 (the blur: x 31-69, y 39-90).
POINTS = {
    media._MARK.LEFT_EYE: (40, 50),
    media._MARK.RIGHT_EYE: (60, 50),
    media._MARK.NOSE_TIP: (50, 62),
    media._MARK.MOUTH_LEFT: (43, 74),
    media._MARK.MOUTH_RIGHT: (57, 74),
    media._MARK.CHIN_GNATHION: (50, 90),
    media._MARK.LEFT_EAR_TRAGION: (25, 60),
}


def _marked(box, points=POINTS):
    """A Vision face with its box and landmarks."""
    face = _face(*box)
    face.landmarks = [
        SimpleNamespace(type_=mark, position=_vertex(x, y))
        for mark, (x, y) in points.items()
    ]
    return face


def striped(w=100, h=120) -> bytes:
    """Vertical 1 px stripes: any blur changes them."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(0, w, 2):
        img.paste((0, 0, 0), (x, 0, x + 1, h))
    return encode(img)


def _pixel(data: bytes, xy) -> tuple:
    return load(data).convert("RGB").getpixel(xy)


def test_blur_face_blurs_the_largest_face_and_keeps_the_ears(fake_vision):
    small = {mark: (x + 10, y - 40) for mark, (x, y) in POINTS.items()}
    fake_vision.faces = [
        _marked((5, 5, 15, 15), small),
        _marked((25, 25, 75, 92)),
    ]
    data = striped()
    out = media.blur_face(data)
    assert load(out).format == "PNG"
    assert _pixel(out, (50, 62)) != _pixel(data, (50, 62))  # nose
    assert _pixel(out, (40, 50)) != _pixel(data, (40, 50))  # eye
    assert _pixel(out, (50, 86)) != _pixel(data, (50, 86))  # chin
    assert _pixel(out, (20, 60)) == _pixel(data, (20, 60))  # ear side
    assert _pixel(out, (50, 30)) == _pixel(data, (50, 30))  # forehead
    assert _pixel(out, (50, 100)) == _pixel(data, (50, 100))  # below the chin
    assert _pixel(out, (60, 14)) == _pixel(data, (60, 14))  # the small face


def test_blur_face_can_keep_the_eyes(fake_vision):
    fake_vision.faces = [_marked((25, 25, 75, 92))]
    data = striped()
    out = media.blur_face(data, keep_eyes=True)
    assert _pixel(out, (40, 46)) == _pixel(data, (40, 46))  # eye
    assert _pixel(out, (50, 74)) != _pixel(data, (50, 74))  # mouth


def test_blur_face_places_the_landmarks_on_the_original(fake_vision):
    """Vision sees a 1600x1200 copy of a 2000x1500 photo: its landmarks
    are scaled by 1.25 (eyes 100 px apart, the blur from x 955 to 1145)."""
    copy = {mark: (x * 4 + 640, y * 4 + 400) for mark, (x, y) in POINTS.items()}
    fake_vision.faces = [_marked((700, 500, 900, 768), copy)]
    data = striped(2000, 1500)
    out = media.blur_face(data)
    assert load(fake_vision.instances[0].requests[0].content).size == (
        1600,
        1200,
    )
    assert _pixel(out, (1050, 810)) != _pixel(data, (1050, 810))  # nose
    assert _pixel(out, (840, 660)) == _pixel(data, (840, 660))  # unscaled


@pytest.mark.parametrize("faces", [[], [_face(25, 25, 75, 92)]])
def test_blur_face_without_a_face_or_landmarks_returns_the_original(
    fake_vision, faces
):
    """No face, or a face without the landmarks the blur needs."""
    for face in faces:
        face.landmarks = []
    fake_vision.faces = faces
    data = striped()
    assert media.blur_face(data) is data


def test_decompression_bomb_is_an_input_error():
    huge = encode(Image.new("1", (11000, 10000)))  # 110 MP
    with pytest.raises(InputError, match="too large"):
        media.resize_max(huge, 2560)


# --------------------------------------------------------------------- ssim


def test_ssim_identical_and_inverted():
    a = gradient()
    inverted = encode(Image.eval(load(a), lambda v: 255 - v))
    assert media.ssim(a, a) == pytest.approx(1.0)
    assert media.ssim(a, inverted) < 0.5


def test_ssim_different_sizes():
    a = gradient(128, 96)
    b = encode(load(a).resize((256, 192)))
    assert media.ssim(a, b) > 0.9


# ------------------------------------------------------------------ upscale


def test_upscale_plumbing(monkeypatch):
    calls = []

    def fake_generate_image(parts, *, system=None, params=None):
        calls.append((parts, params or ImageParams()))
        return b"UP"

    monkeypatch.setattr(media.gen, "generate_image", fake_generate_image)
    src = png()
    assert media.upscale(src) == b"UP"
    parts, params = calls[0]
    assert parts == [media.UPSCALE_PROMPT, src]
    assert params.image_size == "4K" and params.aspect_ratio == "auto"
    media.upscale(
        src, image_size="2K", params=ImageParams(model="m", temperature=0.5)
    )
    _, params = calls[1]
    assert (params.image_size, params.model, params.temperature) == (
        "2K",
        "m",
        0.5,
    )
    assert params.aspect_ratio == "3:4"  # caller's params are respected
    media.upscale(src, prompt=media.FACE_UPSCALE_PROMPT)
    assert calls[2][0] == [media.FACE_UPSCALE_PROMPT, src]


# ------------------------------------------------------------ HEIC and AVIF


def _encode_as(fmt: str) -> bytes:
    """A photo in ``fmt``: the HEIC fixture for HEIF, else a small one."""
    if fmt == "HEIF":
        return HEIC
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, format=fmt, quality=90)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("fmt", "mime"), [("HEIF", "image/heic"), ("AVIF", "image/avif")]
)
def test_sniff_mime_knows_heic_and_avif(fmt, mime):
    data = _encode_as(fmt)
    assert data[4:8] == b"ftyp"
    assert media.sniff_mime(data) == mime


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF"])
def test_normalise_image_converts_heic_and_avif_to_jpeg(fmt):
    data = _encode_as(fmt)
    out = media.normalise_image(data)

    assert media.sniff_mime(out) == "image/jpeg"
    assert load(out).size == load(data).size


def test_normalise_image_leaves_png_jpeg_webp_alone():
    for fmt in ("PNG", "JPEG", "WEBP"):
        data = _encode_as(fmt)
        assert media.normalise_image(data) is data


def test_sniff_mime_still_sees_mp4():
    assert (
        media.sniff_mime(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00")
        == "video/mp4"
    )
