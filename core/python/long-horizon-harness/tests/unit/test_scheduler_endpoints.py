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

"""FastAPI endpoints: ``POST /scheduler/dream-review`` + ``/snapshot``.

Both endpoints are mounted under the ``/scheduler`` prefix via APIRouters
so they slot into the lazy ``_build_app()`` factory without forcing
construction at import time. Tests construct a tiny FastAPI app, mount
the routers, and exercise them via ``TestClient`` while monkeypatching
the dream-review / snapshot helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from horizon.sandbox import provider

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch):
    # Dev-bypass the OIDC verifier so the existing endpoint tests don't
    # need to mock token verification.
    monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", "1")
    yield


class _FakeSessionService:
    """Lists/gets sessions for dream-review discovery in endpoint tests."""

    def __init__(self, sessions: list[SimpleNamespace] | None = None) -> None:
        self._sessions = sessions or []

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> Any:
        del app_name
        return SimpleNamespace(
            sessions=[
                s
                for s in self._sessions
                if user_id is None or s.user_id == user_id
            ]
        )

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str
    ):
        del app_name, user_id
        return SimpleNamespace(id=session_id, events=[])


_NO_MEMORY = object()


def _build_test_app(
    *,
    session_service: Any | None = None,
    app_name: str = "app",
    memory_service: Any = _NO_MEMORY,
) -> FastAPI:
    from horizon.scheduler import dream_review_endpoint, snapshot_endpoint

    app = FastAPI()
    app.include_router(dream_review_endpoint.router)
    app.include_router(snapshot_endpoint.router)
    app.state.runner = SimpleNamespace(
        memory_service=object()
        if memory_service is _NO_MEMORY
        else memory_service,
        session_service=session_service
        if session_service is not None
        else _FakeSessionService(),
        app_name=app_name,
    )
    return app


# =============================================================================
# /scheduler/dream-review
# =============================================================================


class TestDreamReviewEndpoint:
    async def test_empty_user_ids_discovers_active_users(self, monkeypatch):
        """Empty body (the cron's payload) dreams about every active user."""
        from horizon.scheduler import dream_review_endpoint

        called: list[dict[str, Any]] = []

        async def fake_run(**kwargs):
            called.append(kwargs)
            return {"success": True, "sessions_reviewed": 1}

        monkeypatch.setattr(
            dream_review_endpoint, "_run_dream_review_for_user", fake_run
        )

        now = datetime.now(UTC).timestamp()
        service = _FakeSessionService(
            [
                SimpleNamespace(id="a1", user_id="alice", last_update_time=now),
                SimpleNamespace(id="b1", user_id="bob", last_update_time=now),
                SimpleNamespace(id="c1", user_id="carol", last_update_time=0.0),
            ]
        )
        client = TestClient(_build_test_app(session_service=service))
        response = client.post(
            "/scheduler/dream-review", json={"user_ids": [], "app_name": "lha"}
        )
        assert response.status_code == 200
        assert response.json()["reviewed"] == 2
        # carol is stale (outside the lookback window) → not reviewed.
        assert sorted(call["user_id"] for call in called) == ["alice", "bob"]
        # app_name comes from the runner, not the request body.
        assert {call["app_name"] for call in called} == {"app"}

    async def test_empty_user_ids_no_active_returns_zero(self):
        client = TestClient(
            _build_test_app(session_service=_FakeSessionService([]))
        )
        response = client.post(
            "/scheduler/dream-review", json={"user_ids": [], "app_name": "lha"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reviewed"] == 0
        assert "active" in body.get("reason", "").lower()

    async def test_session_service_not_registered_returns_zero(self):
        app = _build_test_app()
        app.state.runner = SimpleNamespace(
            memory_service=object(), session_service=None, app_name="app"
        )
        response = TestClient(app).post(
            "/scheduler/dream-review",
            json={"user_ids": ["alice"], "app_name": "lha"},
        )
        body = response.json()
        assert body["reviewed"] == 0
        assert "session" in body.get("reason", "").lower()

    async def test_dispatches_per_user(self, monkeypatch):
        from horizon.scheduler import dream_review_endpoint

        called: list[dict[str, Any]] = []

        async def fake_run(**kwargs):
            called.append(kwargs)
            return {"success": True, "sessions_reviewed": 2}

        monkeypatch.setattr(
            dream_review_endpoint, "_run_dream_review_for_user", fake_run
        )

        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/dream-review",
            json={"user_ids": ["alice", "bob"], "app_name": "lha"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reviewed"] == 2
        assert sorted(call["user_id"] for call in called) == ["alice", "bob"]

    async def test_failures_recorded(self, monkeypatch):
        from horizon.scheduler import dream_review_endpoint

        async def fake_run(**kwargs):
            if kwargs["user_id"] == "alice":
                return {"success": True, "sessions_reviewed": 1}
            return {"success": False, "reason": "no sessions"}

        monkeypatch.setattr(
            dream_review_endpoint, "_run_dream_review_for_user", fake_run
        )

        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/dream-review",
            json={"user_ids": ["alice", "bob"], "app_name": "lha"},
        )
        body = response.json()
        assert body["reviewed"] == 1
        assert body["failed"] == 1

    async def test_endpoint_rejects_unauth_when_oidc_enabled(self, monkeypatch):
        """With auth enabled and no token, /scheduler/dream-review returns 401."""
        monkeypatch.delenv("LHA_SCHEDULER_AUTH_DISABLED", raising=False)
        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/dream-review",
            json={"user_ids": ["alice"], "app_name": "lha"},
        )
        assert response.status_code == 401

    async def test_missing_memory_service_returns_zero(self):
        client = TestClient(_build_test_app(memory_service=None))
        response = client.post(
            "/scheduler/dream-review",
            json={"user_ids": ["alice"], "app_name": "lha"},
        )
        body = response.json()
        assert body["reviewed"] == 0
        assert "memory" in body.get("reason", "").lower()


# =============================================================================
# /scheduler/snapshot
# =============================================================================


def _enable_snapshots(monkeypatch) -> None:
    monkeypatch.setenv("LHA_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", "reg/runtime:v1")
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", "sa@p.iam.gserviceaccount.com")


class TestSnapshotEndpoint:
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.delenv("LHA_SNAPSHOT_ENABLED", raising=False)
        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/snapshot",
            json={"user_ids": ["alice"], "app_name": "lha"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["snapshotted"] == 0
        assert "disabled" in body.get("reason", "").lower()

    async def test_dispatches_per_user(self, monkeypatch):
        from horizon.sandbox import lifecycle

        _enable_snapshots(monkeypatch)
        monkeypatch.setattr(provider, "_vertex_client_factory", object)
        monkeypatch.setattr(
            provider, "_resolve_sandbox_engine", lambda client: "engine"
        )

        seen: list[str] = []

        def fake_snapshot(
            client, *, agent_instance_name, user_id, snapshot_ttl, keep
        ):
            seen.append(user_id)
            return {"status": "snapshotted", "pruned": 0}

        monkeypatch.setattr(lifecycle, "snapshot_and_prune_user", fake_snapshot)

        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/snapshot",
            json={"user_ids": ["alice", "bob"], "app_name": "lha"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["snapshotted"] == 2
        assert sorted(seen) == ["alice", "bob"]

    async def test_no_sandbox_user_not_counted(self, monkeypatch):
        from horizon.sandbox import lifecycle

        _enable_snapshots(monkeypatch)
        monkeypatch.setattr(provider, "_vertex_client_factory", object)
        monkeypatch.setattr(
            provider, "_resolve_sandbox_engine", lambda client: "engine"
        )

        def fake_snapshot(
            client, *, agent_instance_name, user_id, snapshot_ttl, keep
        ):
            return {"status": "no_sandbox"}

        monkeypatch.setattr(lifecycle, "snapshot_and_prune_user", fake_snapshot)

        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/snapshot",
            json={"user_ids": ["alice"], "app_name": "lha"},
        )
        assert response.json()["snapshotted"] == 0

    async def test_endpoint_rejects_unauth_when_oidc_enabled(self, monkeypatch):
        monkeypatch.delenv("LHA_SCHEDULER_AUTH_DISABLED", raising=False)
        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        client = TestClient(_build_test_app())
        response = client.post(
            "/scheduler/snapshot",
            json={"user_ids": ["alice"], "app_name": "lha"},
        )
        assert response.status_code == 401
