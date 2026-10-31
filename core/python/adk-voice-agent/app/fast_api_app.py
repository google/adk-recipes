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

"""FastAPI app serving ADK `/run_live`, the control-plane REST API, and the built web UI."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from google.adk.cli.fast_api import get_fast_api_app

from app.app_utils import services
from app.app_utils.env_flags import env_flag
from app.auth.env_store import PROJECT_ROOT, load_dotenv_defaults

load_dotenv_defaults()
from app.app_utils.host_guard import LocalHostGuard  # noqa: E402
from app.auth.identity import IdentityMiddleware  # noqa: E402
from app.auth.providers import is_remote_deployment  # noqa: E402

_local_origins = (
    "http://127.0.0.1:3000,http://localhost:3000,"
    "http://127.0.0.1:8000,http://localhost:8000,"
    "http://127.0.0.1:8080,http://localhost:8080"
)


# Deployed UI is same-origin; omit local dev origins when credentials are allowed.
def default_allow_origins() -> str:
    if is_remote_deployment():
        return (os.getenv("APP_URL") or "").strip().rstrip("/")
    return _local_origins


allow_origins = [
    origin.strip()
    for origin in (os.getenv("ALLOW_ORIGINS") or default_allow_origins()).split(
        ","
    )
    if origin.strip()
]
otel_to_cloud = False

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WEB_DIST = PROJECT_ROOT / "web" / "dist"


def web_dist_dir() -> Path:
    return Path(os.getenv("VOICE_AGENT_WEB_DIST") or DEFAULT_WEB_DIST)


def warn_if_thread_store_is_ephemeral() -> bool:
    from app.store.engine import resolve_database_url

    if not is_remote_deployment() or not resolve_database_url().startswith(
        "sqlite"
    ):
        return False
    logging.getLogger(__name__).warning(
        "THREAD_DB_URL is SQLite inside the container: thread history and per-user "
        "settings reset on every restart or redeploy and are not shared between "
        "instances. Set enable_cloud_sql = true in terraform for a multi-user "
        "deployment."
    )
    return True


def warn_if_memory_is_off() -> bool:
    from app.app_utils.services import agent_engine

    if agent_engine() is not None:
        return False
    logging.getLogger(__name__).warning(
        "GOOGLE_CLOUD_AGENT_ENGINE_ID is not set: memory is off. `make onboard` "
        "creates the deployment's Agent Engine and sets it in .env."
    )
    return True


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.auth import ensure_fresh_access_token
    from app.auth.credential_store import get_credential_store
    from app.auth.providers import WORKSPACE_PROVIDER

    warn_if_thread_store_is_ephemeral()
    warn_if_memory_is_off()

    if not get_credential_store().per_user and not env_flag(
        "VOICE_AGENT_SKIP_STARTUP_WARMUP"
    ):
        try:
            await ensure_fresh_access_token(WORKSPACE_PROVIDER)
        except Exception:
            logging.getLogger(__name__).warning(
                "Startup token refresh failed", exc_info=True
            )

    from app.store.engine import close_db
    from app.threads.registry import get_thread_registry

    try:
        recovered = await get_thread_registry().recover_interrupted()
        if recovered:
            logging.getLogger(__name__).info(
                "Marked %d interrupted thread(s) as failed", recovered
            )
    except Exception:
        logging.getLogger(__name__).exception("Thread store startup failed")

    from app.threads.retention import retention_loop

    retention = asyncio.create_task(retention_loop(), name="thread-retention")

    from app.workers.providers.reaper import reaper_enabled, reaper_loop
    from app.workers.providers.registry import get_computer_provider

    reaper: asyncio.Task[None] | None = None
    try:
        provider = get_computer_provider()
        if reaper_enabled(provider):
            reaper = asyncio.create_task(
                reaper_loop(provider), name="computer-reaper"
            )
    except Exception:
        logging.getLogger(__name__).warning(
            "The computer idle reaper did not start", exc_info=True
        )

    yield
    for background in (retention, reaper):
        if background is None:
            continue
        background.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background
    try:
        await close_db()
    except Exception:
        logging.getLogger(__name__).warning(
            "Closing the thread store failed", exc_info=True
        )


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=False,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
    # Loopback bind enables ADK's Host check against DNS rebinding.
    bind_host=os.getenv("VOICE_AGENT_BIND_HOST") or None,
)
# Imported after load_dotenv_defaults() because route modules read env at import time.
from app.api import router as ui_control_router  # noqa: E402
from app.api.mcp import mcp_router  # noqa: E402
from app.app_utils.user_context import UserContextMiddleware  # noqa: E402

app.title = "adk-voice-agent"
app.description = (
    "ADK Voice Agent: a Gemini Live voice agent that runs coding threads in a sandbox "
    "and works across your connected apps."
)
app.include_router(ui_control_router)
app.include_router(mcp_router)
# Runs in reverse registration order: LocalHostGuard -> IdentityMiddleware -> UserContextMiddleware.
app.add_middleware(UserContextMiddleware)
app.add_middleware(IdentityMiddleware)
app.add_middleware(LocalHostGuard)


@app.get("/{path:path}", include_in_schema=False)
async def serve_web_ui(path: str, request: Request) -> FileResponse:
    dist = web_dist_dir().resolve()
    target = (dist / path).resolve()
    if path and target.is_file() and target.is_relative_to(dist):
        return FileResponse(target)
    index = dist / "index.html"
    if index.is_file() and (
        not path or "text/html" in request.headers.get("accept", "")
    ):
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104 - dev entry point; the container must accept outside traffic
