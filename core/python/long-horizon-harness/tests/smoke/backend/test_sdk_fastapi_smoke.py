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

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SMOKE") != "1", reason="RUN_SMOKE!=1"
)


def test_served_app_builds_with_routes(monkeypatch):
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    monkeypatch.setenv("USE_IN_MEMORY_TASK_STORE", "true")
    from fastapi.testclient import TestClient

    from app.fast_api_app import _build_app

    app = _build_app()
    # The /lha and /a2a routes are attached inside the FastAPI lifespan, so
    # enter the lifespan via TestClient before asserting on app.routes.
    with TestClient(app):
        paths = {getattr(r, "path", None) for r in app.routes}
        assert any(p and p.startswith("/lha") for p in paths)
        assert any(p and "a2a" in p for p in paths)
        assert app.state.runner is not None
