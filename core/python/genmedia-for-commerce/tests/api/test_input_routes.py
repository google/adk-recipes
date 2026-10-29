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

"""Inputs of the streaming routes: URI fields, input mode, run folders."""

import datetime
import re

import pytest

from genmedia4commerce.api import spin, sse, tryon, video_tryon
from genmedia4commerce.core import catalog, gcs
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from tests.api.conftest import IMAGE, fake_workflow, frames
from tests.api.test_streaming_routes import files

BUCKET = "test-project-genmedia-for-commerce-media-payloads"
DAY = r"\d{4}-\d{2}-\d{2}"
RUN_ID = "0123456789ab"
UPLOADED = png(5, 5, (7, 7, 7))
CATALOGUE_REF = "catalog:pa1"


def run_folder(session="tab-1", run_id=RUN_ID) -> str:
    """The run folder of today (the date is the server's)."""
    day = datetime.datetime.now(datetime.UTC).date().isoformat()
    return f"runs/{day}/{session}/{run_id}"


def uploaded_uri(name="upload-1a2b3c4d.png", **kw) -> str:
    return f"gs://{BUCKET}/{run_folder(**kw)}/inputs/{name}"


@pytest.fixture
def storage(storage, catalog_file):
    del catalog_file  # CATALOGUE_REF is in the tiny catalogue
    storage.objects[uploaded_uri()] = UPLOADED
    return storage


def catalogue_image() -> bytes:
    return catalog.image("pa1")


def saved(storage) -> dict[str, bytes]:
    """Objects the request wrote (path relative to the media bucket)."""
    return {
        uri.removeprefix(f"gs://{BUCKET}/"): data
        for uri, data in storage.uploads.items()
    }


@pytest.fixture
def run_tryon(monkeypatch):
    run = fake_workflow()
    monkeypatch.setattr(tryon, "run_tryon", run)
    return run


@pytest.fixture
def run_video(monkeypatch):
    run = fake_workflow()
    monkeypatch.setattr(video_tryon, "run_video_tryon", run)
    return run


@pytest.fixture
def run_spin(monkeypatch):
    run = fake_workflow()
    monkeypatch.setattr(spin, "run_spin", run)
    return run


# --- URI fields -------------------------------------------------------------------


def test_tryon_resolves_uri_fields(client, storage, run_tryon):
    response = client.post(
        "/api/tryon",
        files=files("product_images", 1),
        data={
            "product_image_uris": [
                uploaded_uri(),
                CATALOGUE_REF,
            ],
            "person_image_uri": uploaded_uri(),
            "reference_model_image_uri": CATALOGUE_REF,
        },
    )

    assert response.status_code == 200
    (req,) = run_tryon.requests
    assert req.product_images == [IMAGE, UPLOADED, catalogue_image()]
    assert req.person_image == UPLOADED
    assert req.reference_model_image == catalogue_image()
    assert saved(storage) == {}  # input mode inline: nothing is written


def test_video_tryon_resolves_every_uri_field(client, storage, run_video):
    uri = uploaded_uri()
    response = client.post(
        "/api/video-tryon",
        data={
            "product_image_uris": [uri, uri],
            "person_image_uri": uri,
            "reference_model_image_uri": uri,
            "tryon_image_uri": uri,
            "back_image_uri": uri,
        },
    )

    assert response.status_code == 200
    (req,) = run_video.requests
    assert req.product_images == [UPLOADED, UPLOADED]
    assert req.person_image == req.reference_model_image == UPLOADED
    assert req.tryon_image == req.back_image == UPLOADED


def test_spin_from_uris_only(client, storage, run_spin):
    response = client.post(
        "/api/spin", data={"product_image_uris": [uploaded_uri()]}
    )

    assert response.status_code == 200
    assert run_spin.requests[0].product_images == [UPLOADED]


@pytest.mark.parametrize(
    ("route", "upload_files", "data"),
    [
        (
            "tryon",
            files("product_images", 8) + files("person_image", 1),
            {"product_image_uris": [uploaded_uri()]},
        ),
        (
            "tryon",
            files("product_images", 1) + files("person_image", 1),
            {"person_image_uri": uploaded_uri()},
        ),
        (
            "tryon",
            files("product_images", 1)
            + files("person_image", 1)
            + files("reference_model_image", 1),
            {"reference_model_image_uri": uploaded_uri()},
        ),
        ("tryon", files("person_image", 1), {}),
        (
            "video-tryon",
            files("tryon_image", 1) + files("product_images", 8),
            {"product_image_uris": [uploaded_uri()]},
        ),
        (
            "video-tryon",
            files("tryon_image", 1),
            {"tryon_image_uri": uploaded_uri()},
        ),
        (
            "spin",
            files("product_images", 5),
            {"product_image_uris": [uploaded_uri()] * 4},
        ),
        ("spin", [], {}),
    ],
)
def test_count_limits_cover_files_and_uris(
    client, storage, monkeypatch, route, upload_files, data
):
    run = fake_workflow()
    module, name = {
        "tryon": (tryon, "run_tryon"),
        "video-tryon": (video_tryon, "run_video_tryon"),
        "spin": (spin, "run_spin"),
    }[route]
    monkeypatch.setattr(module, name, run)

    response = client.post(
        f"/api/{route}", files=upload_files or None, data=data
    )

    assert response.status_code == 400
    assert "images" in response.json()["detail"]
    assert run.requests == []


@pytest.mark.parametrize(
    "uri",
    [
        "gs://someone-elses-bucket/x.png",
        "https://evil.example.com/x.png",
        "file:///etc/passwd",
        f"gs://{BUCKET}/runs/missing.png",
    ],
)
def test_bad_or_foreign_uris_are_400(client, storage, run_spin, uri):
    response = client.post("/api/spin", data={"product_image_uris": [uri]})

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    assert run_spin.requests == []


def test_total_size_limit_counts_only_the_uploaded_files(
    client, storage, run_spin, monkeypatch
):
    monkeypatch.setattr(sse, "MAX_TOTAL_BYTES", len(IMAGE))

    response = client.post(
        "/api/spin",
        files=files("product_images", 1),
        data={"product_image_uris": [uploaded_uri()] * 3},
    )

    assert response.status_code == 200
    assert len(run_spin.requests[0].product_images) == 4


# --- run ids, input mode, run event -------------------------------------------------


def run_event(response) -> dict:
    first = frames(response.text)[0]
    assert (first["type"], first["stage"]) == ("progress", "run")
    return first["data"]


def test_run_event_comes_first_with_server_ids(client, storage, run_spin):
    response = client.post("/api/spin", files=files("product_images", 1))

    events = frames(response.text)
    assert [e["type"] for e in events] == ["progress", "variation", "complete"]
    data = run_event(response)
    assert re.fullmatch(r"[0-9a-f]{12}", data["run_id"])
    assert data["session_id"] == "no-session"
    assert data["inputs"] is None and data["outputs"] is None
    assert events[0]["message"] == f"Run {data['run_id']}"


def test_run_id_and_session_id_are_honoured(client, storage, run_spin):
    response = client.post(
        "/api/spin",
        files=files("product_images", 1),
        data={
            "run_id": RUN_ID,
            "session_id": "tab-1",
            "input": "gcs",
            "output": "gcs",
        },
    )

    data = run_event(response)
    assert (data["run_id"], data["session_id"]) == (RUN_ID, "tab-1")
    folder = f"gs://{BUCKET}/{run_folder()}"
    assert data["inputs"] == f"{folder}/inputs/"
    assert data["outputs"] == f"{folder}/outputs/"
    assert set(saved(storage)) == {
        f"{run_folder()}/inputs/product-0.png",
        f"{run_folder()}/outputs/0.png",
    }


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"run_id": "XYZ"}, "run_id"),
        ({"run_id": "0123456789abc"}, "run_id"),
        ({"session_id": "a/b"}, "session_id"),
        ({"session_id": "x" * 65}, "session_id"),
        ({"input": "s3"}, "input"),
        ({"output": "s3"}, "output"),
    ],
)
def test_invalid_run_options_are_400(client, storage, run_spin, data, field):
    response = client.post(
        "/api/spin", files=files("product_images", 1), data=data
    )

    assert response.status_code == 400
    assert field in response.json()["detail"]
    assert run_spin.requests == []
    assert saved(storage) == {}


def test_input_gcs_saves_direct_files_once(client, storage, run_tryon):
    response = client.post(
        "/api/tryon",
        files=files("product_images", 2)
        + files("person_image", 1)
        + files("reference_model_image", 1),
        data={"input": "gcs"},
    )

    data = run_event(response)
    folder = run_folder(session="no-session", run_id=data["run_id"])
    assert saved(storage) == {
        f"{folder}/inputs/product-0.png": IMAGE,
        f"{folder}/inputs/product-1.png": IMAGE,
        f"{folder}/inputs/person.png": IMAGE,
        f"{folder}/inputs/reference-model.png": IMAGE,
    }
    assert data["inputs"] == f"gs://{BUCKET}/{folder}/inputs/"
    assert frames(response.text)[1]["media_base64"]  # output stays inline


def test_input_gcs_skips_files_already_in_this_runs_inputs(
    client, storage, run_tryon
):
    other_run = uploaded_uri(run_id="ba9876543210")
    storage.objects[other_run] = UPLOADED

    client.post(
        "/api/tryon",
        files=files("person_image", 1),
        data={
            "input": "gcs",
            "run_id": RUN_ID,
            "session_id": "tab-1",
            "product_image_uris": [
                uploaded_uri(),
                other_run,
                CATALOGUE_REF,
            ],
        },
    )

    inputs = f"{run_folder()}/inputs"
    assert saved(storage) == {
        f"{inputs}/product-1.png": UPLOADED,
        f"{inputs}/product-2.webp": catalogue_image(),
        f"{inputs}/person.png": IMAGE,
    }
    assert run_tryon.requests[0].product_images == [
        UPLOADED,
        UPLOADED,
        catalogue_image(),
    ]


def test_video_tryon_input_names(client, storage, run_video):
    response = client.post(
        "/api/video-tryon",
        files=files("product_images", 1)
        + files("person_image", 1)
        + files("reference_model_image", 1)
        + files("tryon_image", 1)
        + files("back_image", 1),
        data={"input": "gcs"},
    )

    run_id = run_event(response)["run_id"]
    folder = run_folder(session="no-session", run_id=run_id)
    names = {path.removeprefix(f"{folder}/inputs/") for path in saved(storage)}
    assert names == {
        "product-0.png",
        "person.png",
        "reference-model.png",
        "tryon.png",
        "back.png",
    }


@pytest.mark.parametrize(
    ("input_mode", "output_mode"),
    [
        ("inline", "inline"),
        ("gcs", "inline"),
        ("inline", "gcs"),
        ("gcs", "gcs"),
    ],
)
def test_input_and_output_modes_are_independent(
    client, storage, run_spin, input_mode, output_mode
):
    response = client.post(
        "/api/spin",
        files=files("product_images", 1),
        data={"input": input_mode, "output": output_mode},
    )

    data = run_event(response)
    variation = frames(response.text)[1]
    folder = run_folder(session="no-session", run_id=data["run_id"])
    paths = set(saved(storage))
    assert (f"{folder}/inputs/product-0.png" in paths) == (input_mode == "gcs")
    assert (f"{folder}/outputs/0.png" in paths) == (output_mode == "gcs")
    assert (data["inputs"] is not None) == (input_mode == "gcs")
    assert (data["outputs"] is not None) == (output_mode == "gcs")
    assert (variation["media_base64"] is None) == (output_mode == "gcs")
    assert (variation["media_uri"] is None) == (output_mode == "inline")


def test_absent_input_uses_the_deployment_default(
    client, storage, run_spin, monkeypatch
):
    monkeypatch.setenv("GENMEDIA_INPUT", "gcs")
    get_settings.cache_clear()

    response = client.post("/api/spin", files=files("product_images", 1))

    assert run_event(response)["inputs"].endswith("/inputs/")
    assert len(saved(storage)) == 1

    response = client.post(
        "/api/spin", files=files("product_images", 1), data={"input": "inline"}
    )

    assert run_event(response)["inputs"] is None
    assert len(saved(storage)) == 1


def test_bucket_failure_while_saving_inputs_is_503(
    client, storage, run_spin, monkeypatch
):
    def fail(*_args, **_kwargs):
        raise RuntimeError("bucket missing")

    monkeypatch.setattr(gcs, "upload", fail)

    response = client.post(
        "/api/spin", files=files("product_images", 1), data={"input": "gcs"}
    )

    assert response.status_code == 503
    assert response.json()["detail"] == sse.BUCKET_UNAVAILABLE  # no details
    assert run_spin.requests == []
