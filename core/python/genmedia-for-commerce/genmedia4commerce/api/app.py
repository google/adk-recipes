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

"""The FastAPI app: ADK agent API, the REST routers and the single-page app
(static files from ``frontend/dist``)."""

import asyncio
import contextlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import google.auth
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from google.adk.apps import App
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.base_agent_loader import BaseAgentLoader
from starlette.routing import Match, Mount, get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from genmedia4commerce.api import (
    catalog,
    media,
    spin,
    status,
    studio,
    tryon,
    uploads,
    video_tryon,
)
from genmedia4commerce.api.message_limit import MessageLimit
from genmedia4commerce.api.security_headers import SecurityHeadersMiddleware
from genmedia4commerce.app_utils.telemetry import setup_telemetry
from genmedia4commerce.core.errors import ConfigError, InputError
from genmedia4commerce.mcp import studio as studio_mcp
from genmedia4commerce.studio import jobs, sessions

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)

GENMEDIA_DIR = Path(__file__).parents[1]
# ADK puts its agents folder on sys.path, so it gets one that holds only ADK's
# local data: with genmedia4commerce/ our mcp/, requests.py and tools.py
# would shadow the libraries of the same name.
ADK_DIR = Path(__file__).with_name("adk_home")
DIST_DIR = GENMEDIA_DIR.parent / "frontend" / "dist"
# Threads for asyncio.to_thread: every model call blocks one for up to its
# deadline, so the default pool (CPUs + 4) would starve the other routes.
MAX_THREADS = 64
_ROUTERS = (
    tryon,
    video_tryon,
    spin,
    uploads,
    catalog,
    media,
    status,
    studio,
)


def _lifespan(mcp_server):
    """Startup: thread pool and the Studio MCP sessions. Shutdown: the Studio jobs still running
    here are marked interrupted, so the panel and the agent stop waiting
    for them."""

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(MAX_THREADS, thread_name_prefix="genmedia")
        )
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            if stopped := await jobs.interrupt_local():
                logger.info(f"{stopped} running Studio job(s) interrupted")

    return lifespan


def _has_credentials() -> bool:
    """Whether Application Default Credentials exist (Cloud telemetry needs them)."""
    try:
        google.auth.default()
    except Exception as exc:
        logger.warning(
            f"no Application Default Credentials ({exc}); telemetry off"
        )
        return False
    return True


def _static_file(path: str) -> Path | None:
    """Return the file at ``path`` under dist, if there is one."""
    base = DIST_DIR.resolve()
    candidate = (base / path).resolve()
    if candidate.is_relative_to(base) and candidate.is_file():
        return candidate
    return None


async def serve_spa(full_path: str) -> Response:
    """Serve a static file, or index.html for client-side routes."""
    parts = PurePosixPath(full_path).parts
    not_found = JSONResponse({"detail": "Not Found"}, status_code=404)
    # A null byte would make Path.resolve() raise: a 500, not a 404. An
    # absolute path (GET //etc/passwd) is never a page of the app.
    if (
        ".." in parts
        or "\x00" in full_path
        or full_path.startswith("/")
        or parts[:1] in (("api",), ("mcp",))
    ):
        return not_found
    if full_path and (file := _static_file(full_path)):
        return FileResponse(file)
    index = DIST_DIR / "index.html"
    if "." in full_path.rsplit("/", 1)[-1] or not index.is_file():
        return not_found
    return FileResponse(index)


class _SlashlessMount(Mount):
    """Mount that also matches the exact prefix without a trailing slash."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if (
            scope["type"] in ("http", "websocket")
            and get_route_path(scope) == self.path
        ):
            slashed = f"{scope['path']}/"
            match, child_scope = super().matches({**scope, "path": slashed})
            return match, {"path": slashed, **child_scope}
        return super().matches(scope)


def _studio_mcp_server():
    """The Studio MCP App over stateless streamable HTTP, at ``/mcp``.

    Cloud Run IAM authenticates callers (the Gemini Enterprise service
    agent), so FastMCP's DNS-rebinding check is off: it needs the list of
    allowed hosts (the run.app URL, any custom domain), which the app does
    not know. ADK's origin check, in front of every route, still refuses
    browser calls from other origins.
    """
    server = studio_mcp.build_server()
    server.settings.streamable_http_path = "/"
    server.settings.transport_security = None
    # Stateless: each call is one short request, so Gemini Enterprise holds
    # no long-lived GET stream per session (those used up the concurrency).
    server.settings.stateless_http = True
    return server


# ADK's prefix for a pattern in allow_origins
_REGEX = "regex:"


def _allowed_origins() -> list[str] | None:
    """The cross-origin callers allowed by ``ALLOW_ORIGINS`` (comma-separated).

    Each entry is an origin or, with ADK's ``regex:`` prefix, a pattern that
    the whole origin must match (Cloud Shell's Web Preview); a pattern cannot
    contain a comma. None, the default, allows none: ADK then adds no CORS
    middleware and accepts browser requests from the app's own origin only.
    """
    raw = os.getenv("ALLOW_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins:
        raise ConfigError(
            "ALLOW_ORIGINS must list origins (https://host[:port]), not '*': "
            "ADK would accept credentialed requests from any website and "
            "turn off its origin and DNS-rebinding checks"
        )
    for origin in origins:
        if origin.startswith(_REGEX):
            try:
                re.compile(origin.removeprefix(_REGEX))
            except re.error as exc:
                raise ConfigError(
                    f"ALLOW_ORIGINS: {origin!r} is not a valid regular "
                    f"expression ({exc})"
                ) from exc
    return origins or None


_SESSION_LIST = re.compile(r"/apps/[^/]+/users/[^/]+/sessions/?")
_SESSION = re.compile(r"/apps/[^/]+/users/[^/]+/sessions/[^/]+/?")


class _HiddenSessionRoutes:
    """ADK's session list and delete routes answer 404.

    The Studio keeps every chat under one shared user: listing its sessions
    would show anyone every chat, and delete would let them end any.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http" and _is_hidden(
            scope["method"], scope["path"]
        ):
            not_found = JSONResponse({"detail": "Not Found"}, status_code=404)
            await not_found(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _is_hidden(method: str, path: str) -> bool:
    if method in ("GET", "HEAD"):
        return _SESSION_LIST.fullmatch(path) is not None
    return method == "DELETE" and _SESSION.fullmatch(path) is not None


async def _input_error(_request: Request, exc: InputError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


class _AgentLoader(BaseAgentLoader):
    """Serves our one ADK app under its own name (``App.name``), which the
    ADK runner requires for sessions to match."""

    NAME = "genmedia4commerce"

    def load_agent(self, agent_name: str) -> App:
        if agent_name != self.NAME:
            raise ValueError(f"unknown agent {agent_name!r}")
        from genmedia4commerce.agent import app  # heavy: on first use

        return app

    def list_agents(self) -> list[str]:
        return [self.NAME]


def create_app() -> FastAPI:
    """Build the app: ADK endpoints, the REST routers, then the SPA catch-all.

    ADK sessions are in the media bucket (``studio://``, the Studio's
    sessions) unless ``SESSION_SERVICE_URI`` names another service.
    """
    logging.basicConfig(level=logging.INFO)
    setup_telemetry()
    sessions.register()
    logs_bucket = os.getenv("LOGS_BUCKET_NAME")
    mcp_server = _studio_mcp_server()
    app = get_fast_api_app(
        agents_dir=str(ADK_DIR),
        agent_loader=_AgentLoader(),
        # No ADK web UI: it needs the session list this app hides
        # (_HiddenSessionRoutes). make playground runs it on its own (adk web).
        web=False,
        artifact_service_uri=f"gs://{logs_bucket}" if logs_bucket else None,
        session_service_uri=os.getenv("SESSION_SERVICE_URI")
        or f"{sessions.SCHEME}://",
        allow_origins=_allowed_origins(),
        # Cloud Run (K_SERVICE) serves on 0.0.0.0 behind its front end; ADK
        # must not guess the bind from the connection's local address.
        bind_host="0.0.0.0" if os.getenv("K_SERVICE") else None,  # noqa: S104
        otel_to_cloud=_has_credentials(),
        lifespan=_lifespan(mcp_server),
    )
    app.title = "GenMedia for Commerce"
    # Our /health (status router) replaces the ADK one.
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != "/health"
    ]
    app.add_exception_handler(InputError, _input_error)
    app.add_middleware(_HiddenSessionRoutes)
    app.add_middleware(MessageLimit)
    app.add_middleware(SecurityHeadersMiddleware)
    for module in _ROUTERS:
        app.include_router(module.router)
    mcp_app = mcp_server.streamable_http_app()
    app.router.routes.append(_SlashlessMount("/mcp", app=mcp_app))
    app.add_api_route(
        "/{full_path:path}", serve_spa, methods=["GET"], include_in_schema=False
    )
    return app


app = create_app()
