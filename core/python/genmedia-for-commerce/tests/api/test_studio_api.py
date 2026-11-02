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

"""/api/studio: the Studio REST API of the web app."""

import pytest
from google.auth import exceptions as auth_exceptions

from genmedia4commerce import tools
from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import (
    BusyError,
    ConfigError,
    DeadlineExceeded,
    GenerationError,
    StorageError,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from genmedia4commerce.studio import agent as sa
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import jobs, service, store
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio import turn as studio_turn


@pytest.fixture(autouse=True)
def _offline(storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Studio data in the fake bucket, previews and notes offline."""
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(jobs, "_LOCAL", {})
    monkeypatch.setattr(jobs, "_RUNS", {})
    monkeypatch.setattr(
        sm, "preview_url", lambda uri, video=False: f"https://p/{uri}"
    )
    monkeypatch.setattr(sh, "note", lambda studio_id, text: True)


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def test_agent_turn_returns_the_reply_and_the_saved_state(
    client, monkeypatch
) -> None:
    async def fake_turn(message: str, studio_id: str):
        assert (message, studio_id) == ("open the studio", "s1")
        patches = [{"capability": "spin"}]
        saved = ss.save_patched("s1", patches)
        return "s1", studio_turn.AgentTurn(
            reply="Here it is.", patches=patches, saved_at=saved
        )

    monkeypatch.setattr(sa, "run_agent_turn", fake_turn)
    res = client.post(
        "/api/studio/s1/agent", json={"message": "open the studio"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["studio_id"] == "s1" and body["reply"] == "Here it is."
    assert body["state"]["capability"] == "spin"
    assert body["updated_at"] > 0


@pytest.mark.parametrize("studio_id", ["a b", "x" * 58, "a.b"])
def test_a_bad_studio_id_is_422(client, studio_id) -> None:
    res = client.get(f"/api/studio/{studio_id}/state")
    assert res.status_code == 422


def test_jobs_is_not_a_studio_id(client) -> None:
    """It would clash with /api/studio/jobs/{job_id}."""
    res = client.post("/api/studio/jobs/actions", json={"event": {}})
    assert res.status_code == 422


@pytest.mark.parametrize("message", ["", "x" * 4001])
def test_an_empty_or_huge_agent_message_is_422(client, message) -> None:
    res = client.post("/api/studio/s1/agent", json={"message": message})
    assert res.status_code == 422


def test_state_messages_and_actions(client) -> None:
    empty = client.get("/api/studio/s1/state").json()
    assert empty == {
        "studio_id": "s1",
        "saved": False,
        "updated_at": 0,
        "state": {},
    }
    run = "runs/2026-01-02/studio-s1/0123456789ab"
    me = {"label": "me", "ref": media(f"{run}/inputs/upload-me.png")}
    event = {"set": {"field": "person", "item": me}, "source": "upload"}
    res = client.post("/api/studio/s1/actions", json={"event": event})
    assert res.status_code == 200
    assert res.json()["state"]["person"]["ref"] == me["ref"]
    saved = client.get("/api/studio/s1/state").json()
    assert saved["saved"] and saved["state"]["person"]["ref"] == me["ref"]
    assert client.get("/api/studio/s1/messages").json() == {
        "studio_id": "s1",
        "messages": [],
    }


def test_a_bad_action_is_400(client) -> None:
    res = client.post("/api/studio/s1/actions", json={"event": {"nope": 1}})
    assert res.status_code == 400
    assert res.json()["detail"]
    res = client.post(
        "/api/studio/s1/actions", json={"event": {"job_finished": {}}}
    )
    assert res.status_code == 400 and "job_id" in res.json()["detail"]


def test_expired_credentials_say_so(client, monkeypatch) -> None:
    try:
        try:
            raise auth_exceptions.RefreshError("Reauthentication is needed.")
        except auth_exceptions.RefreshError as exc:
            msg = "could not read sessions/studio-s1/state.json"
            raise StorageError(msg) from exc
    except StorageError as err:
        error = err

    def fail(*_args):
        raise error

    monkeypatch.setattr(service, "state", fail)
    res = client.get("/api/studio/s1/state")
    assert res.status_code == 503
    detail = res.json()["detail"]
    assert "gcloud auth application-default login" in detail
    assert "sessions/" not in detail


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (
            StorageError("gs://b/studios/s1: 403"),
            503,
            "Storage is unavailable; try again.",
        ),
        (GenerationError("model down"), 502, "model down"),
        (DeadlineExceeded("too slow"), 504, "too slow"),
        (BusyError("busy; try again"), 503, "busy; try again"),
        (
            ConfigError("sa@p.iam.gserviceaccount.com cannot sign"),
            500,
            "The request failed on the server; try again.",
        ),
    ],
)
def test_errors_map_to_http(client, monkeypatch, error, code, detail) -> None:
    def fail(*_args):
        raise error

    monkeypatch.setattr(service, "event", fail)
    res = client.post("/api/studio/s1/actions", json={"event": {}})
    assert res.status_code == code
    assert res.json() == {"detail": detail}


def test_an_unexpected_error_is_500_without_its_message(
    dist, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from genmedia4commerce.api.app import create_app

    def fail(*_args):
        raise RuntimeError("secret path /srv/key.json")

    monkeypatch.setattr(service, "event", fail)
    client = TestClient(create_app(), raise_server_exceptions=False)
    res = client.post("/api/studio/s1/actions", json={"event": {}})
    assert res.status_code == 500
    assert "secret" not in res.text


def test_upload_saves_an_image_and_refuses_other_files(
    client, storage, fake_gen
) -> None:
    fake_gen.text("a grey square")
    image = png(8, 8, (9, 9, 9))
    res = client.post(
        "/api/studio/s1/uploads", files={"file": ("me.png", image, "image/png")}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ref"] in storage.uploads and "/studio-s1/" in body["ref"]
    assert body["label"] == "me.png" and body["description"] == "a grey square"
    assert body["thumb"].startswith("data:image/jpeg;base64,")
    res = client.post(
        "/api/studio/s1/uploads", files={"file": ("a.txt", b"hi", "text/plain")}
    )
    assert res.status_code == 400 and "not a PNG" in res.json()["detail"]


def test_upload_refuses_an_image_over_the_limit(
    client, storage, monkeypatch
) -> None:
    image = png(8, 8, (9, 9, 9))
    monkeypatch.setattr(gcs, "MAX_IMAGE_BYTES", len(image) - 1)
    res = client.post(
        "/api/studio/s1/uploads", files={"file": ("me.png", image, "image/png")}
    )
    assert res.status_code == 400
    assert "at most" in res.json()["detail"] and storage.uploads == {}


def test_catalog_search(client, monkeypatch, catalog_file) -> None:
    async def fake_search(query: str, k: int = 5, audience: str = "") -> dict:
        del k
        return {"items": [{"id": "pb2", "title": f"{query} {audience}"}]}

    monkeypatch.setattr(tools, "catalog_search", fake_search)
    res = client.post(
        "/api/studio/s1/catalog-search",
        json={"query": "red dress", "audience": "women"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["query"] == "red dress" and body["error"] is None
    assert body["items"] == [
        {
            "label": "red dress women",
            "ref": "catalog:pb2",
            "thumb": "/api/catalog/image/pb2/0?w=256",
        }
    ]


def test_catalog_search_without_a_query_lists_the_featured(
    client, catalog_file
) -> None:
    res = client.post("/api/studio/s1/catalog-search", json={})
    assert [i["ref"] for i in res.json()["items"]] == [
        "catalog:pa1",
        "catalog:p_glasses_4",
    ]


def test_job_start_takes_the_studio_from_the_path(client, monkeypatch) -> None:
    seen = {}

    def start(spec: dict, job_id: str) -> dict:
        seen.update(spec)
        return {"job_id": job_id, "status": "running", "capability": "spin"}

    monkeypatch.setattr(jobs, "start", start)
    ref = media("runs/2026-01-02/studio-s1/0123456789ab/inputs/upload-a.png")
    spin = {"set": {"field": "spin", "items": [{"ref": ref}]}}
    assert client.post(
        "/api/studio/s1/actions", json={"event": spin}
    ).is_success
    spec = {"capability": "spin", "spin": [ref], "studio_id": "other"}
    res = client.post("/api/studio/s1/jobs", json=spec)
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json() == {
        "job_id": job_id,
        "status": "running",
        "capability": "spin",
    }
    assert seen == {"capability": "spin", "spin": [ref], "studio_id": "s1"}
    # The studio follows it at once; a second start is refused.
    state = client.get("/api/studio/s1/state").json()["state"]
    assert state["job"] == {"job_id": job_id, "capability": "spin"}
    again = client.post("/api/studio/s1/jobs", json=spec)
    assert again.status_code == 400 and "still running" in again.text


def test_job_start_refuses_a_studio_never_saved(client, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "start", lambda spec, job_id: {})
    res = client.post("/api/studio/nobody/jobs", json={"capability": "spin"})
    assert res.status_code == 400 and "add its inputs" in res.json()["detail"]


def test_job_start_refuses_an_unknown_capability(client) -> None:
    res = client.post("/api/studio/s1/jobs", json={"capability": "x"})
    assert res.status_code == 400 and "capability" in res.json()["detail"]
    assert client.post("/api/studio/s1/jobs", json={}).status_code == 422


def test_job_status_gallery_and_previews(client, catalog_file) -> None:
    res = client.get("/api/studio/jobs/nope")
    assert res.json() == {"job_id": "nope", "status": "unknown"}
    assert client.get("/api/studio/jobs/a.b").status_code == 422
    gallery = client.get("/api/studio/gallery").json()
    assert gallery["models"] == [
        {
            "label": "Emma",
            "ref": "catalog:m_emma",
            "thumb": "/api/catalog/image/m_emma/0?w=256",
        }
    ]
    assert gallery["spin"][0]["refs"][1] == "catalog:s_shoe/1"
    ref = media("runs/2026-01-02/studio-s1/0123456789ab/outputs/front-0.png")
    manifest = media("runs/2026-01-02/studio-s1/0123456789ab/manifest.json")
    refs = [ref, "gs://x/y", "catalog:pa1", manifest]
    res = client.post("/api/studio/previews", json={"refs": refs})
    assert res.json() == {
        "previews": {
            ref: f"https://p/{ref}",
            "catalog:pa1": "/api/catalog/image/pa1/0?w=256",
        }
    }
