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

"""GET /api/media: the images and videos of runs' inputs and outputs and
the Studio's catalogue copies, nothing else of the media bucket."""

from typing import ClassVar

import pytest
from google.api_core.exceptions import NotFound

from genmedia4commerce.core import gcs
from genmedia4commerce.core.media import sniff_mime
from genmedia4commerce.core.runs import media_url
from genmedia4commerce.core.testing import mp4_stub
from tests.api.conftest import IMAGE

BUCKET = "test-project-genmedia-for-commerce-media-payloads"
RUN = f"gs://{BUCKET}/runs/2026-01-02/tab-1/0123456789ab"
URI = f"{RUN}/outputs/image-front-0.png"


class FakeBlob:
    """A media-bucket object; ``downloads`` records each (start, end) read."""

    downloads: ClassVar[list[tuple[int | None, int | None]]] = []

    def __init__(self, store: dict[str, bytes], uri: str):
        self.store, self.uri = store, uri
        self.size: int | None = None
        self.content_type: str | None = None

    def _data(self) -> bytes:
        if self.uri not in self.store:
            raise NotFound(self.uri)
        return self.store[self.uri]

    def reload(self) -> None:
        data = self._data()
        self.size, self.content_type = len(data), sniff_mime(data)

    def download_as_bytes(self, start=None, end=None) -> bytes:
        FakeBlob.downloads.append((start, end))
        data = self._data()
        return data if start is None else data[start : end + 1]


@pytest.fixture
def objects(monkeypatch) -> dict[str, bytes]:
    store: dict[str, bytes] = {}
    FakeBlob.downloads = []
    monkeypatch.setattr(gcs, "blob", lambda uri: FakeBlob(store, uri))
    return store


@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [
        ("outputs/image-front-0.png", IMAGE, "image/png"),
        ("outputs/video-1.mp4", mp4_stub(frames=2), "video/mp4"),
        ("inputs/upload-1a2b3c4d.png", IMAGE, "image/png"),
        ("inputs/product-0.jpg", IMAGE, "image/png"),
    ],
)
def test_serves_a_run_file_with_type_and_cache_header(
    client, objects, name, data, mime
):
    uri = f"{RUN}/{name}"
    objects[uri] = data

    response = client.get(media_url(uri))

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == mime
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize(
    "uri",
    [
        "gs://someone-elses-bucket/runs/2026-01-02/s/r/outputs/0.png",
        f"gs://{BUCKET}/sessions/g1/history.json",
        f"gs://{BUCKET}/outputs/2026-01-02/r1/0.png",
        f"gs://{BUCKET}/runs",
        f"gs://{BUCKET}/runs/../sessions/g1/history.json",
        f"gs://{BUCKET}/runs/2026-01-02/s/r/..",
        f"https://storage.googleapis.com/{BUCKET}/runs/x/0.png",
        f"{BUCKET}/runs/x/0.png",
        "",
        # Inside runs/ but not an input or output image or video.
        f"{RUN}/manifest.json",
        f"{RUN}/record/photo-0.png",
        f"{RUN}/inputs/upload-1a2b3c4d.png.json",  # an upload's description
        f"{RUN}/outputs/front-0.json",
        f"{RUN}/outputs/x/front-0.png",
        f"{RUN}/outputs/front-0.png?alt=media",
        f"gs://{BUCKET}/runs/2026-01-02/tab-1/0123456789ab",
        f"gs://{BUCKET}/runs/x/0.png",
        f"gs://{BUCKET}/runs/studio_catalog/pa1/0.json",
        "catalog:pa1",
    ],
)
def test_rejects_uris_outside_the_runs_folder(client, objects, uri):
    objects[uri] = IMAGE

    response = client.get("/api/media", params={"uri": uri})

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


def test_serves_a_studio_catalogue_copy(client, objects):
    uri = f"gs://{BUCKET}/runs/studio_catalog/pa1/0.webp"
    objects[uri] = IMAGE

    assert client.get(media_url(uri)).content == IMAGE


def test_missing_object_is_404(client, objects):
    response = client.get(media_url(URI))

    assert response.status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Range": "bytes=0-9"}])
def test_storage_failure_is_502(client, monkeypatch, headers):
    class Down(FakeBlob):
        def _data(self):
            raise RuntimeError("gcs down")

    monkeypatch.setattr(gcs, "blob", lambda uri: Down({}, uri))

    assert client.get(media_url(URI), headers=headers).status_code == 502


def test_missing_object_with_a_range_is_404(client, objects):
    response = client.get(media_url(URI), headers={"Range": "bytes=0-9"})

    assert response.status_code == 404


# --- Range requests (Safari, video seeking) ----------------------------------

DATA = bytes(range(100))


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=0-9", 0, 9),
        ("bytes=90-", 90, 99),
        ("bytes=-5", 95, 99),
        ("bytes=95-1000", 95, 99),
        ("bytes=0-0", 0, 0),
        ("bytes=-500", 0, 99),
    ],
)
def test_range_returns_206_with_the_slice(client, objects, header, start, end):
    objects[URI] = DATA

    response = client.get(media_url(URI), headers={"Range": header})

    assert response.status_code == 206
    assert response.content == DATA[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/100"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert FakeBlob.downloads == [(start, end)]  # only the asked bytes


def test_no_range_downloads_the_whole_object_once(client, objects):
    objects[URI] = DATA

    client.get(media_url(URI))

    assert FakeBlob.downloads == [(None, None)]


def test_range_keeps_the_type_of_the_whole_object(client, objects):
    clip = mp4_stub(frames=2)
    objects[URI] = clip

    response = client.get(media_url(URI), headers={"Range": "bytes=10-20"})

    assert response.status_code == 206
    assert response.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=500-600", "bytes=-0"])
def test_unsatisfiable_range_is_416(client, objects, header):
    objects[URI] = DATA

    response = client.get(media_url(URI), headers={"Range": header})

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */100"


@pytest.mark.parametrize(
    "header",
    ["bytes=5-2", "bytes=0-1,5-6", "items=0-5", "bytes=x-y", "bytes=-"],
)
def test_invalid_or_multi_range_is_ignored(client, objects, header):
    objects[URI] = DATA

    response = client.get(media_url(URI), headers={"Range": header})

    assert response.status_code == 200
    assert response.content == DATA
