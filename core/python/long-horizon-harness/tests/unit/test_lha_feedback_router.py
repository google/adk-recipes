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

"""POST /feedback — must capture the record and return success even when the sink raises."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.feedback as feedback_router


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "alice@x")
    app = FastAPI()
    # No runner needed unless include_context=True; pass a stub so attach succeeds.
    feedback_router.attach_feedback_routes(app, runner=None)
    return TestClient(app)


def test_feedback_happy_path_returns_success(app_client, monkeypatch):
    emit = MagicMock()
    monkeypatch.setattr(feedback_router, "emit_feedback_record", emit)

    r = app_client.post("/feedback", json={"text": "please add dark mode"})

    assert r.status_code == 200
    assert r.json() == {"status": "success"}
    (record,), _ = emit.call_args
    assert record["text"] == "please add dark mode"
    assert record["origin"] == "user"
    assert record["user_id"] == "alice@x"


def test_feedback_returns_success_when_sink_raises(
    app_client, monkeypatch, caplog
):
    # Endpoint must not 500 the user even if emit itself blows up.
    def boom(_record):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(feedback_router, "emit_feedback_record", boom)

    with caplog.at_level(logging.ERROR):
        r = app_client.post("/feedback", json={"text": "dark mode pls"})

    assert r.status_code == 200
    assert r.json() == {"status": "success"}
    # The record text was captured in the logs as a last-resort durable copy.
    assert any("dark mode pls" in rec.getMessage() for rec in caplog.records)
