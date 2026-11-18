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

"""Uploads the panel puts straight into the bucket with a signed URL."""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest
from PIL import Image

from genmedia4commerce.core import gcs, media
from genmedia4commerce.core.errors import ConfigError
from genmedia4commerce.mcp import studio
from genmedia4commerce.studio import media as sm
from tests.conftest import HEIC


def _image(fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), (40, 90, 200)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``gcs.signed_put_url`` recording what it signs."""
    calls: list[dict[str, Any]] = []

    def sign(uri: str, **kwargs: Any) -> tuple[str, dict[str, str]]:
        calls.append({"uri": uri, **kwargs})
        return f"https://signed/{uri}", {"Content-Type": kwargs["content_type"]}

    monkeypatch.setattr(gcs, "signed_put_url", sign)
    return calls


async def _url(studio_id: str, content_type: str) -> dict[str, Any]:
    res = await studio.studio_upload_url(studio_id, content_type)
    return res.structuredContent or {}


async def _saved(studio_id: str, ref: str) -> dict[str, Any]:
    res = await studio.studio_upload_saved(studio_id, ref, "Brightwave tee.png")
    return res.structuredContent or {}


@pytest.mark.asyncio
async def test_the_url_is_for_a_new_upload_of_that_studio(
    storage, signed
) -> None:
    del storage
    got = await _url("s1", "image/png")

    assert got["url"] == f"https://signed/{got['ref']}"
    assert got["headers"] == {"Content-Type": "image/png"}
    assert sm.allowed_ref(got["ref"], "s1")
    assert not sm.allowed_ref(got["ref"], "s2")
    assert "/inputs/upload-" in got["ref"] and got["ref"].endswith(".png")
    (call,) = signed
    assert call["content_type"] == "image/png"
    assert call["max_bytes"] == gcs.MAX_IMAGE_BYTES
    assert call["minutes"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("studio_id", "content_type"),
    [
        ("s1", "text/html"),
        ("s1", ""),
        ("../s2", "image/png"),
        ("", "image/png"),
    ],
)
async def test_no_url_for_other_types_or_bad_ids(
    storage, signed, studio_id, content_type
) -> None:
    del storage
    assert set(await _url(studio_id, content_type)) == {"error"}
    assert signed == []


@pytest.mark.asyncio
async def test_no_url_when_the_credentials_cannot_sign(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    del storage

    def cannot_sign(uri: str, **_kwargs: Any) -> tuple[str, dict]:
        raise ConfigError("these credentials cannot sign URLs")

    monkeypatch.setattr(gcs, "signed_put_url", cannot_sign)
    res = await studio.studio_upload_url("s1", "image/jpeg")
    assert res.isError and set(res.structuredContent or {}) == {"error"}


@pytest.mark.asyncio
async def test_a_saved_upload_comes_back_like_studio_upload(
    storage, signed, fake_gen
) -> None:
    del signed
    fake_gen.text("a blue tee")
    data = _image()
    ref = (await _url("s1", "image/png"))["ref"]
    storage.objects[ref] = data  # the browser's PUT

    got = await _saved("s1", ref)

    base64_up = await studio.studio_upload(
        base64.b64encode(data).decode(), "Brightwave tee.png", "s1"
    )
    assert set(got) == set(base64_up.structuredContent or {})
    assert got["ref"] == ref and got["label"] == "Brightwave tee.png"
    assert got["description"] == "a blue tee"
    assert got["thumb"].startswith("data:image/jpeg;base64,")
    assert storage.objects[ref] == data  # kept as sent
    assert sm.upload_descriptions([ref]) == {ref: "a blue tee"}


@pytest.mark.asyncio
async def test_a_heic_upload_is_stored_as_jpeg_in_place(
    storage, signed, fake_gen
) -> None:
    del signed
    fake_gen.text("a phone photo")
    ref = (await _url("s1", "image/heic"))["ref"]
    storage.objects[ref] = HEIC

    assert (await _saved("s1", ref))["ref"] == ref
    assert ref.endswith(".jpg")
    assert media.sniff_mime(storage.uploads[ref]) == "image/jpeg"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "content_type"),
    [
        (b"<html>not an image</html>", "image/png"),
        (_image("JPEG"), "image/png"),  # not the type it was signed for
        (b"\x89PNG\r\n\x1a\n" + bytes(gcs.MAX_IMAGE_BYTES), "image/png"),
    ],
)
async def test_an_invalid_upload_is_refused_and_deleted(
    storage, signed, sent, content_type
) -> None:
    del signed
    ref = (await _url("s1", content_type))["ref"]
    storage.objects[ref] = sent

    assert set(await _saved("s1", ref)) == {"error"}
    assert ref not in storage.objects


@pytest.mark.asyncio
async def test_only_an_upload_of_that_studio_is_saved(storage, signed) -> None:
    del signed
    ref = (await _url("s2", "image/png"))["ref"]
    storage.objects[ref] = _image()
    output = ref.replace("/inputs/upload-", "/outputs/front-")
    storage.objects[output] = _image()

    assert set(await _saved("s1", ref)) == {"error"}  # another studio's
    assert set(await _saved("s2", output)) == {"error"}  # not an upload
    assert set(await _saved("s2", ref.replace(".png", "-x.png"))) == {"error"}
    assert storage.uploads == {}  # nothing written, nothing described
