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

"""POST /api/uploads: one input file into a run's ``inputs/`` folder."""

import datetime
import io
import re

import pytest
from PIL import Image

from genmedia4commerce.api import sse
from genmedia4commerce.core import gcs
from genmedia4commerce.core.runs import media_url
from genmedia4commerce.core.settings import get_settings
from tests.api.conftest import IMAGE
from tests.conftest import HEIC

BUCKET = "test-project-genmedia-for-commerce-media-payloads"
TODAY = datetime.datetime.now(datetime.UTC).date().isoformat()


def upload(client, data=IMAGE, name="shirt.png", **form):
    return client.post(
        "/api/uploads", files={"file": (name, data, "image/png")}, data=form
    )


def test_stores_the_file_under_the_run_inputs(client, storage):
    response = upload(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"run_id", "session_id", "uri", "url"}
    assert re.fullmatch(r"[0-9a-f]{12}", body["run_id"])
    assert body["session_id"] == "no-session"
    folder = f"gs://{BUCKET}/runs/{TODAY}/no-session/{body['run_id']}/inputs/"
    assert re.fullmatch(rf"{folder}upload-[0-9a-f]{{8}}\.png", body["uri"])
    assert body["url"] == media_url(body["uri"])
    assert storage.uploads == {body["uri"]: IMAGE}


def test_reuses_the_callers_run_and_session(client, storage):
    first = upload(client, session_id="tab-1").json()
    second = upload(
        client, run_id=first["run_id"], session_id="tab-1", name="b.jpg"
    ).json()

    assert second["run_id"] == first["run_id"]
    assert second["session_id"] == "tab-1"
    assert second["uri"] != first["uri"]
    folder = f"gs://{BUCKET}/runs/{TODAY}/tab-1/{first['run_id']}/inputs/"
    assert first["uri"].startswith(folder)
    assert second["uri"].startswith(folder)
    assert len(storage.uploads) == 2


def test_extension_follows_the_content(client, storage):
    jpeg = b"\xff\xd8\xff\xe0" + b"j" * 20

    body = upload(client, data=jpeg, name="photo.png").json()

    assert body["uri"].endswith(".jpg")


def test_a_turned_photo_is_stored_upright(client, storage):
    exif = Image.Exif()
    exif[0x0112] = 6  # turned 90 degrees on display
    buf = io.BytesIO()
    Image.new("RGB", (30, 20)).save(buf, format="WEBP", exif=exif.tobytes())

    body = upload(client, data=buf.getvalue(), name="phone.webp").json()

    assert body["uri"].endswith(".webp")
    assert Image.open(io.BytesIO(storage.uploads[body["uri"]])).size == (20, 30)


def test_uses_the_bucket_whatever_the_input_mode(client, storage, monkeypatch):
    monkeypatch.setenv("GENMEDIA_INPUT", "inline")
    get_settings.cache_clear()

    assert upload(client).status_code == 200
    assert len(storage.uploads) == 1


@pytest.mark.parametrize(
    ("form", "field"),
    [
        ({"run_id": "nothex123456"}, "run_id"),
        ({"run_id": "abc"}, "run_id"),
        ({"session_id": "a b"}, "session_id"),
        ({"session_id": "s" * 65}, "session_id"),
    ],
)
def test_invalid_ids_are_400(client, storage, form, field):
    response = upload(client, **form)

    assert response.status_code == 400
    assert field in response.json()["detail"]
    assert storage.uploads == {}


@pytest.mark.parametrize(
    "data", [b"", b"<html><script>alert(1)</script></html>"]
)
def test_empty_or_non_image_is_400(client, storage, data):
    response = upload(client, data=data)

    assert response.status_code == 400
    assert storage.uploads == {}


def test_missing_file_is_400(client, storage):
    response = client.post("/api/uploads", data={"session_id": "tab-1"})

    assert response.status_code == 400
    assert "file" in response.json()["detail"]


def test_file_over_size_limit_is_rejected(client, storage, monkeypatch):
    monkeypatch.setattr(sse, "MAX_IMAGE_BYTES", len(IMAGE) - 1)

    response = upload(client)

    assert response.status_code in (400, 413)
    assert "MB" in response.json()["detail"]
    assert storage.uploads == {}


def test_bucket_failure_is_503_without_its_details(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("bucket secret-b does not exist for sa@x.iam")

    monkeypatch.setattr(gcs, "upload", fail)

    response = upload(client)

    assert response.status_code == 503
    assert response.json()["detail"] == sse.BUCKET_UNAVAILABLE


def test_unconfigured_bucket_is_503(client, storage, monkeypatch):
    monkeypatch.delenv("PROJECT_ID")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("MEDIA_BUCKET", raising=False)
    get_settings.cache_clear()

    response = upload(client)

    assert response.status_code == 503
    assert storage.uploads == {}


# ------------------------------------------------------------ POST /api/preview


def test_preview_returns_a_small_drawable_copy_and_stores_nothing(
    client, storage
):
    response = client.post(
        "/api/preview", files={"file": ("IMG_8150.HEIC", HEIC, "image/heic")}
    )

    assert response.status_code == 200
    image = Image.open(io.BytesIO(response.content))
    assert image.size == (1024, 768)
    assert response.headers["content-type"] == f"image/{image.format.lower()}"
    assert storage.uploads == {}


def test_preview_rejects_a_file_that_is_not_an_image(client, storage):
    response = client.post(
        "/api/preview", files={"file": ("notes.txt", b"hello", "text/plain")}
    )

    assert response.status_code == 400
    assert storage.uploads == {}
