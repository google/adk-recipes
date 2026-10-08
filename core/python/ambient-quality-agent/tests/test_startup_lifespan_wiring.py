# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Why `fast_api_app` starts things through `lifespan=` and not `on_startup`.

ADK's `api_server.py` always builds `FastAPI(lifespan=internal_lifespan)`, and
Starlette only drives the deprecated `on_startup`/`on_shutdown` handler lists
when the app was constructed with `lifespan=None`. A handler added with
`add_event_handler` on such an app is silently never called -- no error, no
warning, the app just starts as if it were not there.

The first two tests build the app the way ADK does rather than importing ours,
so they pin the ADK behaviour itself and would catch it changing under an
upgrade. The third pins that our module actually uses the hook.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from ambient_quality_agent import fast_api_app
from ambient_quality_agent.app_utils import services
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _adk_style_app(lifespan: Any) -> FastAPI:
    """Builds a FastAPI app mimicking ADK's `api_server.py` internal lifespan wrapper.

    Args:
        lifespan: Optional application lifespan context manager.

    Returns:
        Configured FastAPI instance.
    """

    @asynccontextmanager
    async def internal_lifespan(app: FastAPI) -> AsyncIterator[None]:
        if lifespan:
            async with lifespan(app):
                yield
        else:
            yield

    return FastAPI(lifespan=internal_lifespan)


def test_an_event_handler_added_after_lifespan_is_set_never_fires() -> None:
    fired: list[str] = []
    app = _adk_style_app(lifespan=None)
    app.router.add_event_handler("startup", lambda: fired.append("startup"))

    with TestClient(app):
        pass

    assert fired == []


def test_a_lifespan_passed_to_the_app_is_driven() -> None:
    """The fix: hand the work to `lifespan=`, which `internal_lifespan` wraps
    and Starlette does drive on ASGI startup."""
    fired: list[str] = []

    @asynccontextmanager
    async def ours(_app: FastAPI) -> AsyncIterator[None]:
        fired.append("startup")
        try:
            yield
        finally:
            fired.append("shutdown")

    with TestClient(_adk_style_app(lifespan=ours)):
        pass

    assert fired == ["startup", "shutdown"]


def test_fast_api_app_hands_adk_its_lifespan_and_the_shared_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuild the module against a recording `get_fast_api_app`, so the whole
    wiring contract is asserted in one place rather than inferred from the
    built app."""
    captured: dict[str, Any] = {}

    def _record(**kwargs: Any) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    # Patched where it is defined, not where it is used: the reload below
    # re-executes `from google.adk.cli.fast_api import get_fast_api_app`, which
    # would rebind a stub set on `fast_api_app` back to the real function.
    monkeypatch.setattr("google.adk.cli.fast_api.get_fast_api_app", _record)
    rebuilt = importlib.reload(fast_api_app)
    try:
        assert captured["lifespan"] is rebuilt._lifespan
        assert captured["session_service_uri"] == services.SESSION_SERVICE_URI
        assert captured["artifact_service_uri"] == services.ARTIFACT_SERVICE_URI
        # Without this ADK serves only its own routes and `:asyncQuery`, which
        # is how a durable investigation reports back, has nothing to call.
        assert captured["gemini_enterprise_app_name"] == rebuilt.APP_NAME
    finally:
        # The reload above left the module bound to the recording stub; put the
        # real app back so later tests import a working one.
        monkeypatch.undo()
        importlib.reload(fast_api_app)
