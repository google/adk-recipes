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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.secrets import attach_secrets_routes
from app.secrets import set_secret_store
from app.secrets.store import SecretManagerStore
from tests.unit.test_secret_store import FakeSecretClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "alice@x")
    set_secret_store(
        SecretManagerStore(client=FakeSecretClient(), project_id="proj")
    )
    app = FastAPI()
    attach_secrets_routes(app)
    yield TestClient(app)
    set_secret_store(None)


def test_put_then_list_returns_name_only(client):
    r = client.put("/lha/secrets/OPENAI_API_KEY", json={"value": "sk-1"})
    assert r.status_code == 200
    r = client.get("/lha/secrets")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["secrets"]]
    assert names == ["OPENAI_API_KEY"]


def test_value_is_never_returned(client):
    client.put("/lha/secrets/K", json={"value": "super-secret"})
    body = client.get("/lha/secrets").text
    assert "super-secret" not in body


def test_invalid_name_rejected(client):
    r = client.put("/lha/secrets/bad-name!", json={"value": "x"})
    assert r.status_code == 400


def test_delete(client):
    client.put("/lha/secrets/K", json={"value": "v"})
    r = client.delete("/lha/secrets/K")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/lha/secrets").json()["secrets"] == []


def test_trusted_header_identity_is_honored(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "trusted_header")
    set_secret_store(
        SecretManagerStore(client=FakeSecretClient(), project_id="proj")
    )
    app = FastAPI()
    attach_secrets_routes(app)
    c = TestClient(app)
    c.put(
        "/lha/secrets/K",
        json={"value": "alice-val"},
        headers={"X-LHA-User-Id": "alice@x"},
    )
    bob = c.get("/lha/secrets", headers={"X-LHA-User-Id": "bob@x"})
    assert bob.json()["secrets"] == []
    alice = c.get("/lha/secrets", headers={"X-LHA-User-Id": "alice@x"})
    assert [s["name"] for s in alice.json()["secrets"]] == ["K"]
    set_secret_store(None)


def test_import_preview_returns_names_not_values(client):
    body = {"content": "FOO=secret-val\nBAR=other\n", "commit": False}
    r = client.post("/lha/secrets/import", json=body)
    assert r.status_code == 200
    data = r.json()
    assert sorted(data["valid"]) == ["BAR", "FOO"]
    assert data["committed"] is False
    assert "secret-val" not in r.text
    # Preview must not store anything.
    assert client.get("/lha/secrets").json()["secrets"] == []


def test_import_preview_reports_conflicts_and_skips(client):
    client.put("/lha/secrets/FOO", json={"value": "existing"})
    body = {
        "content": "FOO=new\nNEWKEY=1\nbad-key=2\nno-equals\n",
        "commit": False,
    }
    data = client.post("/lha/secrets/import", json=body).json()
    assert sorted(data["valid"]) == ["FOO", "NEWKEY"]
    assert data["conflicts"] == ["FOO"]
    reasons = {s["reason"] for s in data["skipped"]}
    assert reasons == {"invalid key name", "no '='"}


def test_import_commit_stores_secrets(client):
    body = {"content": "FOO=1\nBAR=2\n", "commit": True}
    data = client.post("/lha/secrets/import", json=body).json()
    assert data["committed"] is True
    names = [s["name"] for s in client.get("/lha/secrets").json()["secrets"]]
    assert names == ["BAR", "FOO"]


def test_import_commit_overwrites_on_conflict(client):
    client.put("/lha/secrets/FOO", json={"value": "old"})
    r = client.post(
        "/lha/secrets/import", json={"content": "FOO=new", "commit": True}
    )
    assert r.json()["committed"] is True
    names = [s["name"] for s in client.get("/lha/secrets").json()["secrets"]]
    assert names == ["FOO"]
