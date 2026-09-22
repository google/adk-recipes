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

"""The served app mounts the A2A, /lha/*, and scheduler routers.

Routes attach inside the FastAPI lifespan, so paths are collected INSIDE the
TestClient context. Assertions are prefix-based — ADK adds its own base routes
(/run, /docs, /apps/...), so exact full-route equality is not asserted.
"""

import pytest

from horizon.fast_api_app import _build_app

# One representative prefix per router group.
_EXPECTED_PREFIXES = (
    "/a2a",
    "/lha/sessions",
    "/lha/state",
    "/lha/contexts",
    "/lha/memories",
    "/lha/uploads",
    "/lha/sandbox",
    "/feedback",
    "/lha/secrets",
    "/lha/gcp",
    "/lha/routines",
    "/scheduler/routine-tick",
    "/scheduler/dream-review",
    "/scheduler/snapshot",
)


@pytest.fixture(autouse=True)
def _hermetic_app_env(monkeypatch):
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    monkeypatch.setenv("USE_IN_MEMORY_TASK_STORE", "true")
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")


def test_all_routers_mount():
    from fastapi.testclient import TestClient

    app = _build_app()
    with TestClient(app):
        paths = {p for r in app.routes if (p := getattr(r, "path", None))}
    for prefix in _EXPECTED_PREFIXES:
        assert any(p.startswith(prefix) for p in paths), (
            f"route {prefix} missing"
        )


def test_app_state_wired():
    from fastapi.testclient import TestClient

    app = _build_app()
    with TestClient(app):
        assert app.state.runner is not None
        assert app.state.a2a_handler is not None
