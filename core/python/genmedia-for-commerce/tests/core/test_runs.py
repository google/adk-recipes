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

"""Run folders: ids, saved inputs, and output delivery (inline or GCS)."""

import asyncio
import base64
import dataclasses
import datetime
import re

import pytest

from genmedia4commerce.core import gcs, runs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import mp4_stub, png

IMAGE = png(8, 8, (1, 2, 3))
BUCKET = "test-project-genmedia-for-commerce-media-payloads"
RUN = runs.RunRef(run_id="abcdef012345", session_id="tab-1", date="2026-01-02")
FOLDER = "runs/2026-01-02/tab-1/abcdef012345"


def variation(index=0, media=IMAGE, **extra) -> VariationEvent:
    return VariationEvent(
        index=index,
        status="ready",
        media=media,
        verdict=None,
        score=1.0,
        extra=extra,
    )


async def stream(*events):
    for event in events:
        yield event


async def collect(events, run=RUN, **kw) -> list:
    return [e async for e in runs.deliver(stream(*events), run=run, **kw)]


@pytest.fixture
def uploads(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_upload(data, *, bucket, path, content_type):
        calls.append(
            {
                "data": data,
                "bucket": bucket,
                "path": path,
                "content_type": content_type,
            }
        )
        return f"gs://{bucket}/{path}"

    monkeypatch.setattr(gcs, "upload", fake_upload)
    return calls


# --- parse_mode / media_url ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("inline", "inline"), ("gcs", "gcs")],
)
def test_parse_mode(value, expected):
    assert runs.parse_mode(value, name="output") == expected


@pytest.mark.parametrize("name", ["input", "output"])
@pytest.mark.parametrize("value", ["GCS", "s3", "base64", " gcs"])
def test_parse_mode_rejects_other_values_naming_the_field(name, value):
    with pytest.raises(InputError, match=rf"^{name} must be"):
        runs.parse_mode(value, name=name)


def test_media_url_quotes_the_whole_uri():
    uri = f"gs://b/{FOLDER}/outputs/image-front-0.png"

    assert runs.media_url(uri) == (
        "/api/media?uri=gs%3A%2F%2Fb%2Fruns%2F2026-01-02%2Ftab-1%2F"
        "abcdef012345%2Foutputs%2Fimage-front-0.png"
    )


# --- RunRef / new_run ------------------------------------------------------------


def test_prefix_is_the_run_folder():
    assert RUN.prefix() == FOLDER


def test_folder_uri_is_in_the_media_bucket():
    assert runs.folder_uri(RUN, "inputs") == (f"gs://{BUCKET}/{FOLDER}/inputs/")
    assert runs.folder_uri(RUN, "outputs") == (
        f"gs://{BUCKET}/{FOLDER}/outputs/"
    )


def test_new_run_defaults():
    run = runs.new_run()

    assert re.fullmatch(r"[0-9a-f]{12}", run.run_id)
    assert run.session_id == "no-session"
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    assert run.date == today
    assert run.prefix() == f"runs/{today}/no-session/{run.run_id}"
    assert runs.new_run().run_id != run.run_id


def test_new_run_keeps_the_callers_ids():
    run = runs.new_run(run_id="0123456789ab", session_id="Tab_7-x")

    assert (run.run_id, run.session_id) == ("0123456789ab", "Tab_7-x")


def test_new_run_treats_empty_ids_as_absent():
    run = runs.new_run(run_id="", session_id="")

    assert run.session_id == "no-session"
    assert len(run.run_id) == 12


def test_run_ref_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RUN.run_id = "x"


@pytest.mark.parametrize(
    "run_id",
    [
        "ABCDEF012345",
        "abcdef01234",
        "abcdef0123456",
        "abcdef01234g",
        "../../abcdef",
        "abcdef012345\n",
    ],
)
def test_new_run_rejects_bad_run_ids(run_id):
    with pytest.raises(InputError, match="run_id"):
        runs.new_run(run_id=run_id)


@pytest.mark.parametrize(
    "session_id", ["a/b", "a b", "../x", "x" * 65, "é", "tab\n", "a.b"]
)
def test_new_run_rejects_bad_session_ids(session_id):
    with pytest.raises(InputError, match="session_id"):
        runs.new_run(session_id=session_id)


def test_new_run_accepts_a_64_character_session_id():
    assert runs.new_run(session_id="s" * 64).session_id == "s" * 64


# --- save_inputs -----------------------------------------------------------------


def test_save_inputs_names_and_paths(uploads):
    jpeg = b"\xff\xd8\xff\xe0jpeg"

    saved = runs.save_inputs(RUN, {"product-0": IMAGE, "person": jpeg})

    assert saved == {
        "product-0": f"gs://{BUCKET}/{FOLDER}/inputs/product-0.png",
        "person": f"gs://{BUCKET}/{FOLDER}/inputs/person.jpg",
    }
    assert [(c["bucket"], c["content_type"]) for c in uploads] == [
        (BUCKET, "image/png"),
        (BUCKET, "image/jpeg"),
    ]
    assert uploads[0]["data"] == IMAGE


def test_save_inputs_with_nothing_to_save(uploads):
    assert runs.save_inputs(RUN, {}) == {}
    assert uploads == []


# --- deliver -------------------------------------------------------------------


def run_event(**data) -> ProgressEvent:
    base = {
        "run_id": RUN.run_id,
        "session_id": RUN.session_id,
        "inputs": None,
        "outputs": None,
        "manifest": None,
    }
    return ProgressEvent("run", f"Run {RUN.run_id}", data={**base, **data})


@pytest.mark.asyncio
async def test_inline_announces_the_run_then_passes_events_through(uploads):
    events = [ProgressEvent("a", "A"), variation(), CompleteEvent([0], 0)]

    out = await collect(events, mode="inline")

    assert out[0] == run_event()
    assert all(a is b for a, b in zip(out[1:], events, strict=True))
    assert uploads == []


@pytest.mark.asyncio
async def test_run_event_reports_the_gcs_folders(uploads):
    inputs = runs.folder_uri(RUN, "inputs")

    (first,) = await collect([], mode="gcs", run_info={"inputs": inputs})

    assert first == run_event(
        inputs=f"gs://{BUCKET}/{FOLDER}/inputs/",
        outputs=f"gs://{BUCKET}/{FOLDER}/outputs/",
    )
    assert first.to_dict()["data"]["outputs"].endswith("/outputs/")


@pytest.mark.asyncio
async def test_default_mode_comes_from_settings(uploads, monkeypatch):
    assert get_settings().output_mode == "inline"
    assert (await collect([variation()]))[1].media == IMAGE

    monkeypatch.setenv("GENMEDIA_OUTPUT", "gcs")
    get_settings.cache_clear()
    run, event = await collect([variation()])

    assert run.data["outputs"] == runs.folder_uri(RUN, "outputs")
    assert event.media is None
    assert len(uploads) == 1


@pytest.mark.asyncio
async def test_gcs_uploads_to_the_run_outputs_folder(uploads):
    original = variation(2, stage="image", view="front")

    _, event = await collect([original], mode="gcs")

    (call,) = uploads
    assert call["data"] == IMAGE
    assert call["bucket"] == BUCKET
    assert call["content_type"] == "image/png"
    assert call["path"] == f"{FOLDER}/outputs/image-front-2.png"
    uri = f"gs://{BUCKET}/{call['path']}"
    assert event.media is None
    assert event.media_uri == uri
    assert event.media_url == runs.media_url(uri)
    assert event.extra == {"stage": "image", "view": "front"}
    assert (event.index, event.status, event.score) == (2, "ready", 1.0)
    # The workflow may keep its event (video try-on animates it): not mutated.
    assert original.media == IMAGE and original.media_uri is None


@pytest.mark.parametrize(
    ("media", "extra", "name"),
    [
        (IMAGE, {"view": "back"}, "back-1.png"),
        (mp4_stub(frames=2), {"stage": "video"}, "video-1.mp4"),
        (mp4_stub(frames=2), {}, "1.mp4"),
        (b"\xff\xd8\xff\xe0jpeg", {"view": ""}, "1.jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", {}, "1.webp"),
        (b"\x00\x01unknown", {"stage": "image"}, "image-1.bin"),
    ],
)
@pytest.mark.asyncio
async def test_gcs_object_name_and_extension(uploads, media, extra, name):
    await collect([variation(1, media, **extra)], mode="gcs")

    assert uploads[0]["path"] == f"{FOLDER}/outputs/{name}"


@pytest.mark.asyncio
async def test_gcs_leaves_other_events_and_empty_variations(uploads):
    events = [
        ClassifiedEvent({"mode": "single"}),
        ProgressEvent("image_selected", "Animating", data={"front_index": 0}),
        variation(media=None),
        CompleteEvent([0], None),
    ]

    out = await collect(events, mode="gcs")

    assert all(a is b for a, b in zip(out[1:], events, strict=True))
    assert uploads == []


@pytest.mark.asyncio
async def test_upload_failure_falls_back_to_inline(monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise RuntimeError("bucket missing")

    monkeypatch.setattr(gcs, "upload", fail)
    original = variation(view="front")

    _, event = await collect([original], mode="gcs")

    assert event.media == IMAGE
    assert event.media_uri is None and event.media_url is None
    assert event.extra == {"view": "front", "output_fallback": "inline"}
    assert original.extra == {"view": "front"}
    assert base64.b64decode(event.to_dict()["media_base64"]) == IMAGE
    assert "bucket missing" in caplog.text


@pytest.mark.asyncio
async def test_closing_deliver_closes_the_workflow(uploads):
    state = {}

    async def workflow():
        try:
            yield variation()
            await asyncio.sleep(60)
        finally:
            state["closed"] = True

    events = runs.deliver(workflow(), run=RUN, mode="inline")
    await anext(events)  # the run event
    await anext(events)
    await events.aclose()

    assert state == {"closed": True}
