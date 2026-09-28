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

"""A caller can mount their own routes on the built app via include_router."""

from app.fast_api_app import _build_app


def test_include_router_on_built_app(monkeypatch):
    # Keep app construction offline (no Vertex / Cloud SQL / task DB).
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    monkeypatch.setenv("USE_IN_MEMORY_TASK_STORE", "true")
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")

    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/lha/custom-extra-probe")
    def _probe():  # pragma: no cover - route presence is what we assert
        return {"ok": True}

    app = _build_app()
    app.include_router(router)
    paths = {p for r in app.routes if (p := getattr(r, "path", None))}
    assert "/lha/custom-extra-probe" in paths
