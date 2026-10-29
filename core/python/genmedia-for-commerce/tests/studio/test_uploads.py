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

"""Studio uploads are stored as sent (upright), named and typed by their
format, and every reader of uploads takes the .jpg and .webp names."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from genmedia4commerce.core import gcs, media, runs
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import media as sm

FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def photo(fmt: str, orientation: int | None = None) -> bytes:
    img = Image.new("RGB", (30, 20), (200, 40, 40))
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format=fmt, exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture
def types_sent(storage, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``{uri: content type}`` of every object written."""
    sent: dict[str, str] = {}

    def upload(
        data: bytes, *, bucket: str, path: str, content_type: str
    ) -> str:
        uri = storage.upload(
            data, bucket=bucket, path=path, content_type=content_type
        )
        sent[uri] = content_type
        return uri

    monkeypatch.setattr(gcs, "upload", upload)
    return sent


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_png_jpeg_webp_are_stored_as_sent(
    storage, fake_gen, types_sent, fmt
) -> None:
    fake_gen.text("a red square")
    data = photo(fmt)

    up = sm.save_image(data, "photo", "s1")

    ref = up["ref"]
    assert storage.uploads[ref] == data
    assert ref.endswith(f".{FORMATS[fmt]}")
    assert types_sent[ref] == media.sniff_mime(data) == sm.mime_of(ref)


def test_a_turned_jpeg_is_stored_upright(storage, fake_gen) -> None:
    fake_gen.text("a red square")

    up = sm.save_image(photo("JPEG", orientation=6), "phone.jpg", "s1")

    saved = storage.uploads[up["ref"]]
    assert up["ref"].endswith(".jpg")
    assert Image.open(io.BytesIO(saved)).size == (20, 30)


def test_the_same_photo_is_stored_as_the_same_bytes(storage, fake_gen) -> None:
    fake_gen.text("a red square")
    data = photo("JPEG", orientation=3)

    first = sm.save_image(data, "a.jpg", "s1")["ref"]
    second = sm.save_image(data, "a.jpg", "s1")["ref"]

    assert first != second
    assert storage.uploads[first] == storage.uploads[second]


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_readers_take_every_stored_format(storage, fake_gen, fmt) -> None:
    fake_gen.text("a red square")
    ref = sm.save_image(photo(fmt), "", "s1")["ref"]
    path = ref.removeprefix(f"gs://{get_settings().media_bucket}/")

    assert runs.is_media_file(path)
    assert sm.allowed_ref(ref, "s1") and not sm.allowed_ref(ref, "s2")
    assert sm.upload_descriptions([ref]) == {ref: "a red square"}
    assert gcs.resolve_image(ref) == storage.uploads[ref]


@pytest.mark.parametrize(
    ("ref", "mime"),
    [
        ("gs://b/runs/x/inputs/upload-1.jpg", "image/jpeg"),
        ("gs://b/runs/x/inputs/upload-1.JPEG", "image/jpeg"),
        ("gs://b/runs/studio_catalog/p/0.webp", "image/webp"),
        ("gs://b/runs/x/outputs/front-0.png", "image/png"),
        ("gs://b/runs/x/outputs/video-0.mp4?sig=1", "video/mp4"),
        ("gs://b/runs/x/outputs/noext", "image/png"),
    ],
)
def test_mime_of_knows_the_stored_extensions(ref, mime) -> None:
    assert sm.mime_of(ref) == mime
