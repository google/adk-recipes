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

"""Run records: kept as the run's manifest.json in output mode gcs, read
back by a later run."""

import json

import pytest

from genmedia4commerce.core import runs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import ProgressEvent, RecordEvent
from genmedia4commerce.core.settings import Mode
from genmedia4commerce.core.testing import png

BUCKET = "test-project-genmedia-for-commerce-media-payloads"
RUN = runs.RunRef(run_id="abcdef012345", session_id="tab-1", date="2026-01-02")
FOLDER = f"gs://{BUCKET}/runs/2026-01-02/tab-1/abcdef012345"
PHOTO, CROP = png(8, 8, (1, 2, 3)), png(9, 9, (4, 5, 6))
RECORD = RecordEvent(
    payload={"version": 1, "products": [{"file": "photo-0"}]},
    files={"photo-0": PHOTO, "crop-top": CROP},
)


async def deliver(mode: Mode) -> list:
    async def events():
        yield RecordEvent(RECORD.payload, RECORD.files)

    return [e async for e in runs.deliver(events(), run=RUN, mode=mode)]


@pytest.mark.asyncio
async def test_gcs_keeps_the_record_as_the_manifest(storage):
    run, saved = await deliver("gcs")

    assert run.data["manifest"] is None  # set once the record is saved
    manifest = f"{FOLDER}/manifest.json"
    assert saved == ProgressEvent(
        "record", "Saved the run record", data={"manifest": manifest}
    )
    kept = json.loads(storage.objects[manifest])
    assert kept == {
        **RECORD.payload,
        "files": {
            "photo-0": f"{FOLDER}/record/photo-0.png",
            "crop-top": f"{FOLDER}/record/crop-top.png",
        },
    }
    assert storage.objects[f"{FOLDER}/record/photo-0.png"] == PHOTO


@pytest.mark.asyncio
async def test_inline_keeps_no_record(storage):
    (run,) = await deliver("inline")
    assert run.stage == "run" and storage.uploads == {}


@pytest.mark.asyncio
async def test_a_failed_save_drops_the_record_and_goes_on(storage):
    def down(*_args, **_kwargs):
        raise OSError("storage down")

    storage.write = down
    (run,) = await deliver("gcs")
    assert run.stage == "run"


def test_a_record_reads_back_from_any_uri_of_its_run(storage):
    runs.save_record(RUN, RECORD)

    for uri in (FOLDER, f"{FOLDER}/", f"{FOLDER}/outputs/front-0.png"):
        manifest, files = runs.load_record(uri)
        assert manifest["version"] == 1
        assert files == RECORD.files


def test_a_run_without_a_record_has_none(storage):
    assert runs.load_record(f"{FOLDER}/manifest.json") is None


@pytest.mark.parametrize(
    "uri",
    [
        "gs://other-bucket/runs/2026-01-02/tab-1/abcdef012345",
        f"gs://{BUCKET}/sessions/s1/state.json",
        f"gs://{BUCKET}/runs/2026-01-02/tab-1/not-a-run-id",
        f"{FOLDER}/../../x/abcdef012345",
        "abcdef012345",
    ],
)
def test_only_run_folders_of_the_media_bucket_name_a_record(storage, uri):
    with pytest.raises(InputError, match="source_run"):
        runs.load_record(uri)


def test_a_manifest_naming_files_outside_its_run_is_refused(storage):
    other = f"gs://{BUCKET}/runs/2026-01-02/tab-1/0123456789ab/record/x.png"
    storage.objects[f"{FOLDER}/manifest.json"] = json.dumps(
        {"files": {"x": other}}
    ).encode()
    with pytest.raises(InputError, match="no valid files"):
        runs.load_record(FOLDER)
