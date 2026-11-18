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

"""The images a Studio job may name (``job_call.check_refs``, checked by
``quota.start`` before any claim): its own studio's files, catalogue images
and copies, and its own try-on records as ``source_run``."""

from __future__ import annotations

from typing import Any

import pytest

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import jobs, quota, service, store
from genmedia4commerce.studio.job_call import check_refs


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def run(studio_id: str) -> str:
    return f"runs/2026-01-02/studio-{studio_id}/0123456789ab"


def upload(studio_id: str, name: str = "me.png") -> str:
    return media(f"{run(studio_id)}/inputs/upload-{name}")


def output(studio_id: str, name: str = "front-0.png") -> str:
    return media(f"{run(studio_id)}/outputs/{name}")


def _spec(**fields: Any) -> dict[str, Any]:
    return {"capability": "tryon", "studio_id": "s1", **fields}


def test_a_spec_of_its_own_files_and_the_catalogue_passes(catalog_file):
    check_refs(
        _spec(
            person=upload("s1"),
            reference_model="catalog:m_emma",
            products=["catalog:pa1", media("runs/studio_catalog/pb2/0.png")],
            tryon_image=output("s1"),
            back_image=output("s1", "back-0.png"),
            source_run=media(f"{run('s1')}/manifest.json"),
            spin=["catalog:s_shoe/2", upload("s1", "side.png")],
        ),
        "s1",
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"person": upload("s2")},  # another studio's upload
        {"products": [upload("s1"), output("s2")]},
        {"spin": [upload("s2", "side.png")]},
        {"tryon_image": output("s2")},
        {"back_image": output("s2", "back-0.png")},
        {"reference_model": upload("s2")},
        {"person": media(f"{run('s1')}/manifest.json")},  # not an image
        {"products": [media(f"{run('s1')}/record/photo-0.png")]},
        {"person": media("sessions/studio-s1/state.json")},
        {"person": "gs://another-bucket/runs/x.png"},
        {"person": "catalog:nope"},  # no such catalogue item
        {"products": [7]},
        {"products": "catalog:pa1"},  # not a list
    ],
)
def test_a_spec_naming_other_objects_is_refused(catalog_file, fields):
    with pytest.raises(InputError):
        check_refs(_spec(**fields), "s1")


@pytest.mark.parametrize(
    "source_run",
    [
        "gs://x/manifest.json",
        media(f"{run('s2')}/manifest.json"),  # another studio's record
        media(f"{run('s1')}/outputs/front-0.png"),
        media("runs/2026-01-02/rest-client/0123456789ab/manifest.json"),
    ],
)
def test_a_source_run_must_be_a_record_of_the_studio(source_run):
    with pytest.raises(InputError, match="source_run"):
        check_refs(_spec(tryon_image=output("s1"), source_run=source_run), "s1")


@pytest.mark.asyncio
async def test_a_refused_spec_claims_nothing(storage, monkeypatch):
    monkeypatch.setattr(quota, "_STARTS", quota.collections.deque())
    monkeypatch.setattr(jobs, "start", lambda spec, job_id: {})
    doc = {"state": {"capability": "spin"}, "updated_at": 1.0}
    store.write(store.state_path("s1"), doc)
    spec = {"studio_id": "s1", "capability": "spin", "spin": [upload("s2")]}
    with pytest.raises(InputError, match="Not an allowed image"):
        await service.job_start(spec)
    assert "job" not in store.read(store.state_path("s1"))["state"]
    assert not quota._STARTS
