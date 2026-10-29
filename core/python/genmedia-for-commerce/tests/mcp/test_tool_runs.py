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

"""The run parameters of the three generation tools: input, output,
run_id, session_id, and the ``run`` of the result."""

import datetime
import inspect
import re

import pytest

from genmedia4commerce import tools
from genmedia4commerce.core import gcs
from genmedia4commerce.core.events import ProgressEvent, VariationEvent
from genmedia4commerce.core.runs import media_url
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Verdict
from tests.mcp.test_tools import (
    COMPLETE,
    PERSON,
    PRODUCT,
    VARIATION,
    VIDEO,
    b64,
    events,
)

pytestmark = pytest.mark.asyncio

BUCKET = "test-project-genmedia-for-commerce-media-payloads"
DAY = r"\d{4}-\d{2}-\d{2}"
TODAY = datetime.datetime.now(datetime.UTC).date().isoformat()
IMAGE_STAGE = VariationEvent(
    index=1,
    status="ready",
    media=PRODUCT,
    verdict=Verdict(publish=True),
    score=80.0,
    extra={"view": "front", "stage": "image"},
)


@pytest.fixture
def uploads(monkeypatch) -> list[str]:
    paths: list[str] = []

    def fake_upload(data, *, bucket, path, content_type):
        paths.append(path)
        return f"gs://{bucket}/{path}"

    monkeypatch.setattr(gcs, "upload", fake_upload)
    return paths


def assert_saved(variation: dict, name: str) -> None:
    uri = variation["media_uri"]
    assert re.fullmatch(
        rf"gs://{BUCKET}/runs/{DAY}/no-session/[0-9a-f]{{12}}/outputs/{name}",
        uri,
    )
    assert variation["media_url"] == media_url(uri)
    assert variation["media_base64"] is None
    assert variation["media_mime"]


async def test_try_on_gcs(monkeypatch, uploads):
    monkeypatch.setattr(tools, "run_tryon", events(VARIATION, COMPLETE))

    result = await tools.try_on([b64(PRODUCT)], b64(PERSON), output="gcs")

    (variation,) = result["variations"]
    assert_saved(variation, "front-0.png")
    assert variation["media_mime"] == "image/png"
    assert result["best_index"] == 0


async def test_video_try_on_gcs_includes_image_variations(monkeypatch, uploads):
    selected = ProgressEvent(
        "image_selected", "Animating", data={"front_index": 1}
    )
    run = events(IMAGE_STAGE, selected, VIDEO, COMPLETE)
    monkeypatch.setattr(tools, "run_video_tryon", run)

    result = await tools.video_try_on(
        product_images_base64=[b64(PRODUCT)],
        person_image_base64=b64(PERSON),
        output="gcs",
    )

    (picture,) = result["image_variations"]
    assert_saved(picture, "image-front-1.png")
    (clip,) = result["variations"]
    assert_saved(clip, "video-0.mp4")
    assert clip["media_mime"] == "video/mp4"
    assert len({path.split("/")[3] for path in uploads}) == 1  # one run id
    assert result["selected"] == [{"front_index": 1}]


async def test_spin_360_gcs(monkeypatch, uploads):
    monkeypatch.setattr(tools, "run_spin", events(VIDEO, COMPLETE))

    result = await tools.spin_360([b64(PRODUCT)], output="gcs")

    assert_saved(result["variations"][0], "video-0.mp4")


async def test_default_output_follows_the_deployment(monkeypatch, uploads):
    monkeypatch.setattr(tools, "run_spin", events(VARIATION, COMPLETE))
    assert (await tools.spin_360([b64(PRODUCT)]))["variations"][0][
        "media_base64"
    ] == b64(PRODUCT)
    assert uploads == []

    monkeypatch.setenv("GENMEDIA_OUTPUT", "gcs")
    get_settings.cache_clear()
    result = await tools.spin_360([b64(PRODUCT)])

    assert result["variations"][0]["media_uri"]
    result = await tools.spin_360([b64(PRODUCT)], output="inline")
    assert result["variations"][0]["media_base64"] == b64(PRODUCT)


@pytest.mark.parametrize(
    ("tool", "workflow", "kwargs"),
    [
        (
            tools.try_on,
            "run_tryon",
            {
                "product_images_base64": [b64(PRODUCT)],
                "person_image_base64": b64(PERSON),
            },
        ),
        (
            tools.video_try_on,
            "run_video_tryon",
            {"tryon_image_base64": b64(PERSON)},
        ),
        (
            tools.spin_360,
            "run_spin",
            {"product_images_base64": [b64(PRODUCT)]},
        ),
    ],
)
async def test_invalid_output_is_an_error(monkeypatch, tool, workflow, kwargs):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, workflow, run)

    result = await tool(**kwargs, output="s3")

    assert set(result) == {"error", "retryable"}
    assert "output" in result["error"]
    assert run.requests == []


@pytest.mark.parametrize(
    "tool", [tools.try_on, tools.video_try_on, tools.spin_360]
)
@pytest.mark.parametrize("param", ["input", "output", "run_id", "session_id"])
async def test_run_params_are_documented_for_llm_callers(tool, param):
    assert inspect.signature(tool).parameters[param].default == ""
    assert f"{param}:" in tool.__doc__
    assert '"gcs"' in tool.__doc__


# --- input, run_id, session_id, run ------------------------------------------------

RUN_ID = "0123456789ab"
FOLDER = f"runs/{TODAY}/chat-7/{RUN_ID}"
UPLOADED = f"gs://{BUCKET}/{FOLDER}/inputs/upload-1a2b3c4d.png"


@pytest.fixture
def storage(storage):
    storage.objects[UPLOADED] = PERSON
    return storage


def saved(storage) -> set[str]:
    return {uri.removeprefix(f"gs://{BUCKET}/") for uri in storage.uploads}


async def test_result_carries_the_run(monkeypatch, storage):
    monkeypatch.setattr(tools, "run_spin", events(VARIATION, COMPLETE))

    result = await tools.spin_360([b64(PRODUCT)])

    run = result["run"]
    assert set(run) == {"run_id", "session_id", "inputs", "outputs", "manifest"}
    assert re.fullmatch(r"[0-9a-f]{12}", run["run_id"])
    assert run["session_id"] == "no-session"
    assert run["inputs"] is None and run["outputs"] is None
    assert storage.uploads == {}


async def test_input_gcs_saves_the_images_in_the_run_folder(
    monkeypatch, storage
):
    run = events(VARIATION, COMPLETE)
    monkeypatch.setattr(tools, "run_tryon", run)

    result = await tools.try_on(
        [b64(PRODUCT), UPLOADED],
        b64(PERSON),
        reference_model_image_base64=b64(PRODUCT),
        input="gcs",
        run_id=RUN_ID,
        session_id="chat-7",
    )

    assert result["run"] == {
        "run_id": RUN_ID,
        "session_id": "chat-7",
        "inputs": f"gs://{BUCKET}/{FOLDER}/inputs/",
        "outputs": None,
        "manifest": None,
    }
    assert saved(storage) == {  # the upload of this run is not saved again
        f"{FOLDER}/inputs/product-0.png",
        f"{FOLDER}/inputs/person.png",
        f"{FOLDER}/inputs/reference-model.png",
    }
    assert run.requests[0].product_images == [PRODUCT, PERSON]
    assert result["variations"][0]["media_base64"] == b64(PRODUCT)


async def test_upload_from_the_day_before_keeps_its_folder(
    monkeypatch, storage
):
    # Uploaded at 23:59 UTC, sent at 00:01: same session and run id.
    folder = f"runs/2020-01-01/chat-7/{RUN_ID}"
    yesterday = f"gs://{BUCKET}/{folder}/inputs/a.png"
    other_run = f"gs://{BUCKET}/runs/{TODAY}/chat-7/fedcba987654/inputs/b.png"
    storage.objects[yesterday] = storage.objects[other_run] = PRODUCT
    monkeypatch.setattr(tools, "run_spin", events(VARIATION, COMPLETE))

    result = await tools.spin_360(
        [other_run, yesterday],
        input="gcs",
        output="gcs",
        run_id=RUN_ID,
        session_id="chat-7",
    )

    # The run stays in the upload's folder: the run event names the folder
    # its inputs are in, and the new input and the result go there too.
    assert result["run"]["inputs"] == f"gs://{BUCKET}/{folder}/inputs/"
    assert result["run"]["outputs"] == f"gs://{BUCKET}/{folder}/outputs/"
    assert saved(storage) == {
        f"{folder}/inputs/product-0.png",
        f"{folder}/outputs/front-0.png",
    }


async def test_video_try_on_input_names(monkeypatch, storage):
    monkeypatch.setattr(tools, "run_video_tryon", events(VIDEO, COMPLETE))

    result = await tools.video_try_on(
        tryon_image_base64=b64(PERSON),
        back_image_base64=b64(PRODUCT),
        product_images_base64=[b64(PRODUCT)],
        input="gcs",
        output="gcs",
    )

    folder = f"runs/{TODAY}/no-session/{result['run']['run_id']}"
    assert saved(storage) == {
        f"{folder}/inputs/tryon.png",
        f"{folder}/inputs/back.png",
        f"{folder}/inputs/product-0.png",
        f"{folder}/outputs/video-0.mp4",
    }
    assert result["run"]["outputs"] == f"gs://{BUCKET}/{folder}/outputs/"


@pytest.mark.parametrize(
    ("input_mode", "output_mode"),
    [
        ("inline", "inline"),
        ("gcs", "inline"),
        ("inline", "gcs"),
        ("gcs", "gcs"),
    ],
)
async def test_input_and_output_modes_are_independent(
    monkeypatch, storage, input_mode, output_mode
):
    monkeypatch.setattr(tools, "run_spin", events(VARIATION, COMPLETE))

    result = await tools.spin_360(
        [b64(PRODUCT)], input=input_mode, output=output_mode
    )

    run, (variation,) = result["run"], result["variations"]
    folder = f"runs/{TODAY}/no-session/{run['run_id']}"
    paths = saved(storage)
    assert (f"{folder}/inputs/product-0.png" in paths) == (input_mode == "gcs")
    assert (f"{folder}/outputs/front-0.png" in paths) == (output_mode == "gcs")
    assert (run["inputs"] is not None) == (input_mode == "gcs")
    assert (variation["media_base64"] is None) == (output_mode == "gcs")


async def test_default_input_follows_the_deployment(monkeypatch, storage):
    monkeypatch.setattr(tools, "run_spin", events(VARIATION, COMPLETE))
    monkeypatch.setenv("GENMEDIA_INPUT", "gcs")
    get_settings.cache_clear()

    result = await tools.spin_360([b64(PRODUCT)])

    assert result["run"]["inputs"].endswith("/inputs/")
    assert len(storage.uploads) == 1


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"input": "s3"}, "input"),
        ({"run_id": "not-a-run"}, "run_id"),
        ({"session_id": "a/b"}, "session_id"),
        ({"product_images_base64": ["gs://elsewhere/x.png"]}, "bucket"),
    ],
)
async def test_invalid_run_params_are_errors(
    monkeypatch, storage, kwargs, field
):
    run = events(COMPLETE)
    monkeypatch.setattr(tools, "run_spin", run)

    result = await tools.spin_360(
        **{"product_images_base64": [b64(PRODUCT)], **kwargs}
    )

    assert set(result) == {"error", "retryable"}
    assert field in result["error"]
    assert run.requests == []
    assert storage.uploads == {}
