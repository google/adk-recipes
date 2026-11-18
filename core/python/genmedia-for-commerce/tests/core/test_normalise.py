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

"""``media.normalise_image``: PNG, JPEG and WebP kept byte for byte unless
their EXIF orientation needs applying; HEIC and AVIF as JPEG; deterministic."""

import io

import pytest
from PIL import Image, ImageCms

from genmedia4commerce.core import media
from tests.conftest import HEIC

ORIENTATION = 0x0112
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def photo(
    fmt: str, *, orientation: int | None = None, icc: bool = False
) -> bytes:
    """A 60x40 photo, red on its left half, in ``fmt``; with an EXIF
    ``orientation`` and an sRGB colour profile when asked."""
    img = Image.new("RGB", (60, 40), (20, 20, 220))
    img.paste((220, 20, 20), (0, 0, 30, 40))
    extra: dict = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[ORIENTATION] = orientation
        extra["exif"] = exif.tobytes()
    if icc:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        extra["icc_profile"] = profile.tobytes()
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=90, **extra)
    return buf.getvalue()


def load(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@pytest.mark.parametrize("fmt", list(FORMATS))
@pytest.mark.parametrize("orientation", [None, 1])
def test_upright_png_jpeg_webp_are_kept_byte_for_byte(fmt, orientation):
    data = photo(fmt, orientation=orientation)

    assert media.normalise_image(data) is data


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_a_turned_photo_is_straightened_in_its_own_format(fmt):
    # Orientation 6: the camera was turned; viewers turn it 90° clockwise.
    data = photo(fmt, orientation=6, icc=True)

    out = media.normalise_image(data)

    assert media.sniff_mime(out) == FORMATS[fmt]
    img = load(out)
    assert img.size == (40, 60)
    assert img.getexif().get(ORIENTATION, 1) == 1  # never turned twice
    assert img.info.get("icc_profile") == load(data).info["icc_profile"]
    # The left half (red) is now the top.
    r, _, b = img.convert("RGB").getpixel((20, 5))
    assert r > 150 and b < 100


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_the_same_photo_always_gives_the_same_bytes(fmt):
    data = photo(fmt, orientation=8)

    assert media.normalise_image(data) == media.normalise_image(bytes(data))


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF"])
def test_heic_and_avif_become_the_same_jpeg_every_time(fmt):
    # Both are red on their left half, blue on their right.
    data = HEIC if fmt == "HEIF" else photo(fmt)

    out = media.normalise_image(data)

    assert media.sniff_mime(out) == "image/jpeg"
    img = load(out).convert("RGB")
    assert img.size == load(data).size
    w, h = img.size
    assert img.getpixel((w // 8, h // 2))[0] > 150  # red
    assert img.getpixel((w * 7 // 8, h // 2))[2] > 150  # blue
    assert media.normalise_image(data) == out


def test_unreadable_png_jpeg_webp_come_back_unchanged():
    fake = b"\xff\xd8\xff\xe0" + b"j" * 20  # a JPEG signature, no image

    assert media.normalise_image(fake) is fake
