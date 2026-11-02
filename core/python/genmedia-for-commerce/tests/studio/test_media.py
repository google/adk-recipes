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

"""Studio previews, uploads and tool errors: only our refs are ever served."""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
from typing import Any

import pytest
from PIL import Image

from genmedia4commerce.core import gcs, media, video
from genmedia4commerce.core.errors import ConfigError, InputError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import mp4_stub
from genmedia4commerce.mcp import studio
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import picks, store
from tests.conftest import HEIC


def _media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def _png(size: tuple[int, int] = (8, 8), fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format=fmt)
    return buf.getvalue()


OUTPUT = "runs/2026-01-02/studio-s1/0123456789ab/outputs/front-0.png"
NAMES = ("video-0.mp4", "video-0.webp")  # a video try-on's, a spin's
FORBIDDEN = [
    "gs://other-bucket/x",
    "gs://other-bucket/runs/x.png",
    "sessions/studio-s1/history.json",  # in the media bucket
    "runs/../sessions/studio-s1/state.json",  # in the media bucket
    "https://example.com/a.png",
    "data:image/png;base64,AAAA",
    "gallery:models/w1/a.png",
    "catalog:pa1",  # previews of catalogue refs are thumbnails instead
]


def _ref(name: str) -> str:
    return name if ":" in name else _media(name)


@pytest.fixture
def signing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``gcs.signed_url`` that records what it signs."""
    signed: list[str] = []

    def sign(uri: str, **_kwargs) -> str:
        signed.append(uri)
        return f"https://signed/{uri}"

    monkeypatch.setattr(gcs, "signed_url", sign)
    monkeypatch.setattr(sm, "_warned_unsigned", False)
    return signed


@pytest.fixture
def no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    def cannot_sign(uri: str, **_kwargs) -> str:
        raise ConfigError("these credentials cannot sign URLs")

    monkeypatch.setattr(gcs, "signed_url", cannot_sign)
    monkeypatch.setattr(sm, "_warned_unsigned", False)


@pytest.fixture
def downloads(storage, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every object in the fake bucket, and a log of what is downloaded."""
    seen: list[str] = []
    for name in [OUTPUT, *FORBIDDEN]:
        storage.objects[_ref(name)] = _png()

    def download(uri: str) -> bytes:
        seen.append(uri)
        return storage.download(uri)

    monkeypatch.setattr(gcs, "download", download)
    return seen


# ---- preview_url -------------------------------------------------------------


@pytest.mark.parametrize("name", FORBIDDEN)
def test_preview_never_signs_other_refs(name, signing, downloads) -> None:
    assert sm.preview_url(_ref(name)) == ""
    assert sm.preview_url(_ref(name), video=True) == ""
    assert signing == [] and downloads == []


@pytest.mark.parametrize("name", FORBIDDEN)
def test_preview_never_inlines_other_refs(name, no_signing, downloads) -> None:
    assert sm.preview_url(_ref(name)) == ""
    assert downloads == []


def test_preview_signs_run_outputs(signing, downloads) -> None:
    uri = _media(OUTPUT)
    assert sm.preview_url(uri) == f"https://signed/{uri}"
    assert signing == [uri] and downloads == []


def test_previews_of_catalogue_refs_are_thumbnails(
    signing, downloads, catalog_file
) -> None:
    refs = ["catalog:pa1", "catalog:s_shoe/2", "catalog:nope", "gallery:x"]
    inline = sm.previews(refs)
    assert set(inline) == {"catalog:pa1", "catalog:s_shoe/2"}
    assert inline["catalog:pa1"].startswith("data:image/webp;base64,")
    assert sm.previews(refs, inline=False) == {
        "catalog:pa1": "/api/catalog/image/pa1/0?w=256",
        "catalog:s_shoe/2": "/api/catalog/image/s_shoe/2?w=256",
    }
    assert signing == [] and downloads == []


def test_preview_inlines_run_outputs_when_it_cannot_sign(
    no_signing, downloads, storage, caplog
) -> None:
    uri = _media(OUTPUT)
    clip, spin = (_media(OUTPUT.replace("front-0.png", n)) for n in NAMES)
    webp = video.encode_frames([_png(), _png()], fps=24)
    storage.objects[clip], storage.objects[spin] = mp4_stub(frames=2), webp
    with caplog.at_level(logging.DEBUG, logger=sm.__name__):
        assert sm.preview_url(uri).startswith("data:image/jpeg;base64,")
        # A video or a spin goes whole, in its own format.
        assert sm.preview_url(clip, video=True).startswith("data:video/mp4;")
        assert sm.preview_url(spin, video=True) == (
            "data:image/webp;base64," + base64.b64encode(webp).decode()
        )
    assert downloads == [uri, clip, spin]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # once per process, not per call


def test_refresh_urls_handles_each_result_on_its_own(
    no_signing, downloads
) -> None:
    gone = _media("runs/2026-01-02/s/r/outputs/gone.png")
    results: list[Any] = [
        {"uri": _media("sessions/studio-s1/history.json"), "url": "x"},
        {"uri": gone},
        None,
        "junk",
        {"uri": _media(OUTPUT), "url": "old"},
    ]
    sm.refresh_urls(results, video=False)
    assert results[0]["url"] == "" and results[1]["url"] == ""
    assert results[4]["url"].startswith("data:image/jpeg;base64,")


# ---- studio_event and studio_state -----------------------------------------


@pytest.mark.asyncio
async def test_saved_state_never_serves_other_objects(
    storage, signing, downloads
) -> None:
    state = {
        "capability": "tryon",
        "person": {"label": "me", "ref": _media(OUTPUT), "url": "https://x"},
        "results": {
            "tryon": [
                {"index": 0, "uri": "gs://other-bucket/x", "url": "https://x"},
                {"index": 1, "uri": _media("sessions/studio-s1/history.json")},
                {"index": 2, "uri": _media(OUTPUT), "url": "https://evil"},
            ]
        },
        "images": {"video_tryon": [{"uri": _media("sessions/x/state.json")}]},
    }
    # A state written with foreign refs (the app can no longer send states;
    # test_state.py checks that caller URLs are never stored).
    store.write(store.state_path("s1"), {"state": state, "updated_at": 1.0})

    res = await studio.studio_state("s1")
    restored = res.structuredContent["state"]
    urls = [r["url"] for r in restored["results"]["tryon"]]
    assert urls == ["", "", f"https://signed/{_media(OUTPUT)}"]
    assert restored["images"]["video_tryon"][0]["url"] == ""
    assert signing == [_media(OUTPUT)]
    forbidden = {
        "gs://other-bucket/x",
        _media("sessions/studio-s1/history.json"),
        _media("sessions/x/state.json"),
    }
    assert not forbidden & set(downloads)


# ---- uploads -------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_accepts_a_data_url(storage, fake_gen) -> None:
    fake_gen.text("a red square")
    up = sm.save_upload(
        "data:image/png;base64," + _b64(_png()), "red.png", "s1"
    )
    assert media.sniff_mime(storage.uploads[up["ref"]]) == "image/png"
    assert up["thumb"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF"])
def test_upload_converts_heic_and_avif(storage, fake_gen, fmt) -> None:
    fake_gen.text("a red square")
    data = HEIC if fmt == "HEIF" else _png((40, 30), fmt)
    up = sm.save_upload(_b64(data), "photo", "s1")
    saved = storage.uploads[up["ref"]]
    assert up["ref"].endswith(".jpg")
    assert media.sniff_mime(saved) == "image/jpeg"
    assert media.image_size(saved) == media.image_size(data)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not base64!", "base64"),
        (_b64(b"%PDF-1.4 not an image"), "not a PNG"),
        (_b64(b"\x89PNG\r\n\x1a\n" + b"\0" * 20), "unreadable"),
    ],
)
def test_upload_rejects_bad_images(storage, payload, match) -> None:
    with pytest.raises(InputError, match=match):
        sm.save_upload(payload, "x.png", "s1")
    assert storage.uploads == {}


def test_upload_rejects_large_files_and_images(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gcs, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(InputError, match="at most"):
        sm.save_upload(_b64(_png()), "x.png", "s1")
    monkeypatch.undo()
    # The 100 MP guard (twice PIL's limit), scaled down.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(InputError, match="too large"):
        sm.save_upload(_b64(_png()), "x.png", "s1")
    assert storage.uploads == {}


# ---- catalogue picks -----------------------------------------------------------


@pytest.fixture
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh copy cache, and any image description fails the test."""
    monkeypatch.setattr(sm, "_COPIES", {})

    def describe(_data: bytes) -> str:
        raise AssertionError("a catalogue pick must not call a model")

    monkeypatch.setattr(sm, "describe_image", describe)


def test_catalogue_copies_take_the_catalogue_description(
    storage, catalog_file, no_model
) -> None:
    uri, desc = sm.to_gcs("catalog:pb2")
    assert uri == _media("runs/studio_catalog/pb2/0.webp")
    assert desc == "men's black black leather jacket. A test item."
    assert storage.uploads[uri][:4] == b"RIFF"
    # Preset models and spin sets have only their name: no description.
    assert sm.to_gcs("catalog:m_emma") == (
        _media("runs/studio_catalog/m_emma/0.webp"),
        "",
    )
    assert sm.to_gcs("catalog:s_shoe/2")[1] == ""
    assert sm.to_gcs(_media(OUTPUT)) == (_media(OUTPUT), "")


def test_catalogue_copies_of_a_spin_set_upload_in_parallel(
    storage, catalog_file, no_model, monkeypatch: pytest.MonkeyPatch
) -> None:
    views = ["catalog:s_shoe", "catalog:s_shoe/1", "catalog:s_shoe/2"]
    # Each upload waits for the other two: one after the other would time out.
    together = threading.Barrier(len(views), timeout=5)
    upload = gcs.upload

    def slow_upload(data: bytes, **kwargs) -> str:
        together.wait()
        return upload(data, **kwargs)

    monkeypatch.setattr(gcs, "upload", slow_upload)
    copies = sm.catalog_copies([*views, _media(OUTPUT), views[0]])
    assert list(copies) == views  # each once, catalogue refs only
    assert [uri for uri, _ in copies.values()] == [
        _media(f"runs/studio_catalog/s_shoe/{n}.webp") for n in range(3)
    ]
    assert sm.catalog_copies([_media(OUTPUT)]) == {}


# ---- tool errors ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_input_errors_come_back_as_json(storage) -> None:
    res = await studio.studio_upload("not base64!", "x.png", "s1")
    assert res.isError
    assert res.structuredContent == {"error": "image_base64: not valid base64"}
    assert json.loads(res.content[0].text) == res.structuredContent


@pytest.mark.asyncio
async def test_tool_crashes_come_back_as_json_without_details(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    def broken(**_kwargs) -> dict:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(picks, "gallery", broken)
    res = await studio.studio_gallery()
    assert res.isError
    assert res.structuredContent == {
        "error": "studio_gallery failed; try again."
    }
    assert "secret" not in res.content[0].text
    assert "secret detail" in caplog.text  # logged with the traceback
