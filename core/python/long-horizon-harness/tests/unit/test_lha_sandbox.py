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

"""Unit tests for POST /lha/sandbox/warm and GET /lha/sandbox/status.

The lock under test lives in ``session_start._ensure_environment`` —
without it, the warm-POST + on_session_start_callback would race and
double-provision a sandbox. So we deliberately do NOT monkeypatch
``_ensure_environment``; we patch ``_build_environment`` to a counted
LocalEnvironment factory and let the real lock code run.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport

from app.api import sandbox as lha_sandbox
from app.api.sandbox import attach_sandbox_routes
from app.auth import current_user_id
from app.conversation import session_start
from app.environment import LocalEnvironment

USER_ID = "u@local"


def _build_app() -> FastAPI:
    app = FastAPI()
    attach_sandbox_routes(app)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return app


@pytest.fixture
def reset_provisioning_state() -> None:
    session_start._env_cache.clear()
    session_start._provisioning_locks.clear()
    lha_sandbox._warm_status.clear()
    yield
    session_start._env_cache.clear()
    session_start._provisioning_locks.clear()
    lha_sandbox._warm_status.clear()


@pytest.fixture
def counted_local_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_provisioning_state: None,
) -> dict[str, int]:
    counter = {"builds": 0}
    ws = tmp_path / "ws"
    ws.mkdir()

    def _build(_user_id: str) -> tuple[LocalEnvironment, None]:
        counter["builds"] += 1
        return LocalEnvironment(working_dir=ws), None

    monkeypatch.setattr(session_start, "_build_environment", _build)
    return counter


async def _wait_for_status(
    client: httpx.AsyncClient, target: str, timeout: float = 2.0
) -> dict:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        resp = await client.get("/lha/sandbox/status")
        body = resp.json()
        if body.get("status") == target:
            return body
        if loop.time() > deadline:
            raise AssertionError(
                f"status did not reach {target!r} within {timeout}s; last={body!r}"
            )
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_warm_returns_202_and_status(
    counted_local_env: dict[str, int],
) -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post("/lha/sandbox/warm")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] in {"provisioning", "ready"}
        assert body["user_id"]

        ready = await _wait_for_status(client, "ready")
        assert "ready_at" in ready
        assert ready["user_id"]
    assert counted_local_env["builds"] == 1


@pytest.mark.asyncio
async def test_warm_is_safe_to_call_twice(
    counted_local_env: dict[str, int],
) -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp1 = await client.post("/lha/sandbox/warm")
        assert resp1.status_code == 202
        await _wait_for_status(client, "ready")

        resp2 = await client.post("/lha/sandbox/warm")
        assert resp2.status_code == 202
        # Drain any scheduled background task — the second warm's _kick
        # should hit _env_cache and not call _build_environment again.
        await asyncio.sleep(0.05)
    assert counted_local_env["builds"] == 1


@pytest.mark.asyncio
async def test_concurrent_warm_calls_provision_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_provisioning_state: None,
) -> None:
    """Regression test for the per-user asyncio.Lock.

    Without the lock both coroutines pass the cache miss check, both
    call _build_environment, and the build counter would be 2. The
    slow initialize widens the race window so this is reliable."""
    counter = {"builds": 0}
    ws = tmp_path / "ws"
    ws.mkdir()

    class SlowEnv(LocalEnvironment):
        async def initialize(self) -> None:
            await asyncio.sleep(0.05)
            await super().initialize()

    def _build(_user_id: str) -> tuple[LocalEnvironment, None]:
        counter["builds"] += 1
        return SlowEnv(working_dir=ws), None

    monkeypatch.setattr(session_start, "_build_environment", _build)

    app = _build_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        await asyncio.gather(
            client.post("/lha/sandbox/warm"),
            client.post("/lha/sandbox/warm"),
        )
        await _wait_for_status(client, "ready")
    assert counter["builds"] == 1


def test_warm_missing_user_id_is_401() -> None:
    app = FastAPI()
    attach_sandbox_routes(app)
    os.environ["LHA_AUTH_MODE"] = "iap"
    os.environ["LHA_IAP_AUDIENCE"] = "/projects/0/locations/x/services/y"
    try:
        client = TestClient(app)
        resp = client.post("/lha/sandbox/warm")
        assert resp.status_code == 401
    finally:
        os.environ.pop("LHA_AUTH_MODE", None)
        os.environ.pop("LHA_IAP_AUDIENCE", None)


def test_status_returns_idle_before_any_warm(
    reset_provisioning_state: None,
) -> None:
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/lha/sandbox/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["user_id"]


@pytest.mark.asyncio
async def test_status_surfaces_error_payload(
    monkeypatch: pytest.MonkeyPatch,
    reset_provisioning_state: None,
) -> None:
    def _build_fails(_user_id: str) -> LocalEnvironment:
        raise RuntimeError("simulated provision failure")

    monkeypatch.setattr(session_start, "_build_environment", _build_fails)

    app = _build_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post("/lha/sandbox/warm")
        assert resp.status_code == 202
        body = await _wait_for_status(client, "error")
        assert "simulated provision failure" in body["error"]


def test_status_missing_user_id_is_401() -> None:
    app = FastAPI()
    attach_sandbox_routes(app)
    os.environ["LHA_AUTH_MODE"] = "iap"
    os.environ["LHA_IAP_AUDIENCE"] = "/projects/0/locations/x/services/y"
    try:
        client = TestClient(app)
        resp = client.get("/lha/sandbox/status")
        assert resp.status_code == 401
    finally:
        os.environ.pop("LHA_AUTH_MODE", None)
        os.environ.pop("LHA_IAP_AUDIENCE", None)
