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

"""The ``output`` form field of the three streaming routes."""

import re

import pytest

from genmedia4commerce.api import spin, tryon, video_tryon
from genmedia4commerce.core import gcs
from genmedia4commerce.core.runs import media_url
from genmedia4commerce.core.settings import get_settings
from tests.api.conftest import fake_workflow, frames
from tests.api.test_streaming_routes import files

BUCKET = "test-project-genmedia-for-commerce-media-payloads"

ROUTES = {
    "tryon": (
        tryon,
        "run_tryon",
        files("product_images", 1) + files("person_image", 1),
    ),
    "video-tryon": (
        video_tryon,
        "run_video_tryon",
        files("tryon_image", 1),
    ),
    "spin": (spin, "run_spin", files("product_images", 1)),
}


@pytest.fixture
def uploads(monkeypatch) -> list[str]:
    paths: list[str] = []

    def fake_upload(data, *, bucket, path, content_type):
        paths.append(path)
        return f"gs://{bucket}/{path}"

    monkeypatch.setattr(gcs, "upload", fake_upload)
    return paths


def post(client, monkeypatch, route: str, data: dict):
    module, name, upload_files = ROUTES[route]
    run = fake_workflow()
    monkeypatch.setattr(module, name, run)
    return client.post(f"/api/{route}", files=upload_files, data=data), run


@pytest.mark.parametrize("route", ROUTES)
def test_output_gcs_streams_media_urls(client, monkeypatch, uploads, route):
    response, _ = post(client, monkeypatch, route, {"output": "gcs"})

    run, variation, complete = frames(response.text)
    (path,) = uploads
    assert re.fullmatch(
        r"runs/\d{4}-\d{2}-\d{2}/no-session/[0-9a-f]{12}/outputs/0\.png", path
    )
    assert path.startswith(
        run["data"]["outputs"].removeprefix(f"gs://{BUCKET}/")
    )
    uri = f"gs://{BUCKET}/{path}"
    assert variation["media_base64"] is None
    assert variation["media_uri"] == uri
    assert variation["media_url"] == media_url(uri)
    assert variation["media_mime"] == "image/png"
    assert complete["type"] == "complete"


@pytest.mark.parametrize("route", ROUTES)
def test_output_defaults_to_inline(client, monkeypatch, uploads, route):
    response, _ = post(client, monkeypatch, route, {})

    _, variation, _ = frames(response.text)
    assert variation["media_base64"]
    assert variation["media_uri"] is None and variation["media_url"] is None
    assert uploads == []


def test_absent_output_uses_the_deployment_default(
    client, monkeypatch, uploads
):
    monkeypatch.setenv("GENMEDIA_OUTPUT", "gcs")
    get_settings.cache_clear()

    response, _ = post(client, monkeypatch, "spin", {})

    assert frames(response.text)[1]["media_uri"].startswith(f"gs://{BUCKET}/")
    assert len(uploads) == 1


def test_output_inline_overrides_a_gcs_default(client, monkeypatch, uploads):
    monkeypatch.setenv("GENMEDIA_OUTPUT", "gcs")
    get_settings.cache_clear()

    response, _ = post(client, monkeypatch, "tryon", {"output": "inline"})

    assert frames(response.text)[1]["media_base64"]
    assert uploads == []


@pytest.mark.parametrize("route", ROUTES)
def test_invalid_output_is_400(client, monkeypatch, uploads, route):
    response, run = post(client, monkeypatch, route, {"output": "s3"})

    assert response.status_code == 400
    assert "output" in response.json()["detail"]
    assert run.requests == []
