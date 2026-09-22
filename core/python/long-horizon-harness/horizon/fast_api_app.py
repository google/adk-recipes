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

"""The served FastAPI app: ``horizon.fast_api_app:app`` (uvicorn / ``adk`` serve).

Session/memory/artifact backends and the sandbox are resolved from the
environment; every router mounts. The agent itself lives in ``horizon.agent`` —
adapt the recipe by editing that + these routes."""

import functools
import logging
import os
from typing import TYPE_CHECKING

from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore, TaskStore
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from horizon.infrastructure.db_resilience import (
    is_pooled_sql_url,
    resilient_engine_kwargs,
    retry_on_disconnect,
)
from horizon.infrastructure.env import env_flag, env_str

if TYPE_CHECKING:
    from google.adk.runners import Runner

logger = logging.getLogger(__name__)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Default dev sqlite lives here (gitignored) so the repo root stays clean.
# Only used when no explicit SESSION_DB_URL / TASK_DB_URL override is set.
_DEV_DATA_DIR = os.path.join(AGENT_DIR, ".data")


async def _shutdown_runtime() -> None:
    # Sibling-agent siblings can write routines, so drain them before
    # closing the routine store. Either may raise on shutdown — we log
    # and continue so the other cleanup still runs.
    from horizon.agent import SIBLING_AGENT_PLUGIN

    try:
        await SIBLING_AGENT_PLUGIN.close()
    except Exception:
        logger.exception("shutdown: sibling agent plugin close failed")
    from horizon.scheduler.routine_store import active_routine_store

    store = active_routine_store()
    close = getattr(store, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:
            logger.exception("shutdown: routine store close failed")


class _ResilientTaskStore(TaskStore):
    def __init__(self, delegate: TaskStore, *, base_delay: float = 0.25):
        self._delegate = delegate
        self._retry_base_delay = base_delay

    def __getattr__(self, name: str):
        # Delegate anything not explicitly wrapped to the inner store. Guard the
        # inner-reference name so a miss before __init__ sets it can't recurse.
        if name == "_delegate":
            raise AttributeError(name)
        return getattr(self._delegate, name)

    async def save(self, *args, **kwargs):
        return await retry_on_disconnect(
            functools.partial(self._delegate.save, *args, **kwargs),
            base_delay=self._retry_base_delay,
        )

    async def get(self, *args, **kwargs):
        return await retry_on_disconnect(
            functools.partial(self._delegate.get, *args, **kwargs),
            base_delay=self._retry_base_delay,
        )

    async def delete(self, *args, **kwargs):
        return await retry_on_disconnect(
            functools.partial(self._delegate.delete, *args, **kwargs),
            base_delay=self._retry_base_delay,
        )

    async def list(self, *args, **kwargs):  # a2a 1.x TaskStore adds list()
        return await retry_on_disconnect(
            functools.partial(self._delegate.list, *args, **kwargs),
            base_delay=self._retry_base_delay,
        )


def _build_task_store() -> TaskStore:
    # TASK_DB_URL → DatabaseTaskStore; USE_IN_MEMORY_TASK_STORE=true →
    # InMemoryTaskStore; otherwise dev default is a sqlite file alongside
    # lha_sessions.db so in-flight A2A tasks survive server restart and the
    # web UI can resume them via tasks/get + tasks/resubscribe.
    if env_flag("USE_IN_MEMORY_TASK_STORE"):
        return InMemoryTaskStore()
    task_db_url = os.environ.get("TASK_DB_URL")
    if not task_db_url:
        os.makedirs(_DEV_DATA_DIR, exist_ok=True)
        task_db_url = (
            f"sqlite+aiosqlite:///{os.path.join(_DEV_DATA_DIR, 'lha_tasks.db')}"
        )
    logger.info("lha: A2A task store backed by %s", task_db_url)
    db_store = DatabaseTaskStore(
        engine=create_async_engine(
            task_db_url,
            **resilient_engine_kwargs(pooled=is_pooled_sql_url(task_db_url)),
        )
    )
    if is_pooled_sql_url(task_db_url):
        return _ResilientTaskStore(db_store)
    return db_store


def _maybe_resilient_session_service(service, session_uri):
    # Only SQL-backed services drop connections; agentengine:// and in-memory
    # don't, and wrapping them just adds indirection.
    from horizon.infrastructure.resilient_session_service import (
        ResilientSessionService,
    )

    if is_pooled_sql_url(session_uri):
        return ResilientSessionService(service)
    return service


def resolve_session_service_uri() -> str | None:
    # SESSION_DB_URL swaps ADK's InMemorySessionService → DatabaseSessionService
    # (sqlite/postgres/mysql); takes precedence over the Agent-Engine URI when
    # both are configured. Returns None if no DB-shaped override is set — the
    # caller then falls back to Agent-Engine discovery or in-memory.
    if env_flag("USE_IN_MEMORY_SESSION"):
        return None

    db_url = os.environ.get("SESSION_DB_URL")
    agent_engine_resource = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
    if db_url:
        if agent_engine_resource:
            logger.warning(
                "Both SESSION_DB_URL and AGENT_ENGINE_RESOURCE_NAME are set; "
                "SESSION_DB_URL takes precedence (DatabaseSessionService)."
            )
        return db_url
    return None


def _resolve_service_uris() -> tuple[str | None, str | None, str | None]:
    """Return ``(session_uri, memory_uri, artifact_uri)`` from the environment.

    When neither ``USE_IN_MEMORY_SESSION`` nor an explicit DB URL is set, a
    Vertex Agent Engine is created (or discovered) and both session and memory
    are pointed at it via the ``agentengine://`` scheme.
    """
    use_in_memory_session = env_flag("USE_IN_MEMORY_SESSION")
    session_uri = resolve_session_service_uri()
    memory_uri: str | None = None

    # Memory Bank lives on a Vertex Agent Engine that may be separate from
    # the session store (e.g. prod runs sessions in Cloud SQL but still needs
    # cross-session memory). AGENT_ENGINE_RESOURCE_NAME points at the shared
    # engine that also hosts sandboxes — see terraform/agent_engine.tf.
    # LHA_MEMORY_BANK_RESOURCE_NAME is an escape hatch for splitting memory
    # onto a different engine if needed.
    memory_bank_resource = os.environ.get(
        "LHA_MEMORY_BANK_RESOURCE_NAME"
    ) or os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
    if memory_bank_resource:
        memory_uri = f"agentengine://{memory_bank_resource}"

    # Local dev default: keep chats around without forcing the dev to spin up
    # a real Vertex Agent Engine. Skipped when explicitly opted out
    # (USE_IN_MEMORY_SESSION), an explicit URI is set, or we're in a deployed
    # env (Cloud Run sets K_SERVICE).
    if (
        not use_in_memory_session
        and session_uri is None
        and not os.environ.get("K_SERVICE")
        and not os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
    ):
        os.makedirs(_DEV_DATA_DIR, exist_ok=True)
        sqlite_path = os.path.join(_DEV_DATA_DIR, "lha_sessions.db")
        session_uri = f"sqlite:///{sqlite_path}"
        logger.info("lha: defaulting session storage to %s", session_uri)

    if not use_in_memory_session and session_uri is None:
        import google.auth
        import vertexai
        from vertexai._genai.types import (
            AgentEngineConfig,
            ReasoningEngineContextSpec,
        )

        from horizon.infrastructure.memory_config import memory_bank_config

        _, project_id = google.auth.default()
        agent_name = env_str("AGENT_ENGINE_SESSION_NAME", "lha-memory-bank")
        # Memory Bank lives in a regional endpoint — `global` (used for Gemini
        # inference) is rejected here. us-central1 is the default Memory Bank
        # region; override with AGENT_ENGINE_LOCATION when needed.
        agent_engine_location = env_str("AGENT_ENGINE_LOCATION", "us-central1")
        client = vertexai.Client(
            project=project_id, location=agent_engine_location
        )

        matching = [
            a
            for a in client.agent_engines.list()
            if a.api_resource.display_name == agent_name
        ]
        agent_engine = (
            matching[0]
            if matching
            else client.agent_engines.create(
                config=AgentEngineConfig(
                    display_name=agent_name,
                    context_spec=ReasoningEngineContextSpec(
                        memory_bank_config=memory_bank_config,
                    ),
                ),
            )
        )
        session_uri = f"agentengine://{agent_engine.api_resource.name}"
        # Memory Bank shares the same Agent Engine resource as sessions; setting
        # memory_service_uri tells ADK to wire VertexAiMemoryBankService instead
        # of the in-memory default. Don't overwrite if LHA_MEMORY_BANK_RESOURCE_NAME
        # already pinned memory to a different engine.
        if memory_uri is None:
            memory_uri = session_uri

    # Symmetry with session/memory: an explicit ARTIFACT_SERVICE_URI wins, else
    # fall back to the LOGS_BUCKET_NAME-derived GCS bucket (else in-memory).
    artifact_uri = os.environ.get("ARTIFACT_SERVICE_URI") or None
    if artifact_uri is None:
        logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
        artifact_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None
    return session_uri, memory_uri, artifact_uri


def build_runner() -> "Runner":
    """Bind the agent to an ADK ``Runner`` with services resolved from the
    environment (sessions/memory default to in-memory locally, Vertex Agent
    Engine when configured). The sandbox backend is chosen by
    ``LHA_ENVIRONMENT_BACKEND`` or a provider installed via
    ``set_environment_provider``. Use this to embed the agent without FastAPI."""
    from google.adk.cli.utils.service_factory import (
        create_artifact_service_from_options,
        create_memory_service_from_options,
        create_session_service_from_options,
    )
    from google.adk.runners import Runner

    from horizon.agent import app
    from horizon.infrastructure.artifact_service import (
        FilenamePreservingArtifactService,
    )

    session_uri, memory_uri, artifact_uri = _resolve_service_uris()
    session_service = _maybe_resilient_session_service(
        create_session_service_from_options(
            base_dir=AGENT_DIR,
            session_service_uri=session_uri,
            use_local_storage=True,
            session_db_kwargs=(
                resilient_engine_kwargs()
                if is_pooled_sql_url(session_uri)
                else None
            ),
        ),
        session_uri,
    )
    memory_service = create_memory_service_from_options(
        base_dir=AGENT_DIR,
        memory_service_uri=memory_uri,
    )
    artifact_service = FilenamePreservingArtifactService(
        create_artifact_service_from_options(
            base_dir=AGENT_DIR,
            artifact_service_uri=artifact_uri,
            strict_uri=True,
            use_local_storage=True,
        )
    )
    return Runner(
        app=app,
        session_service=session_service,
        memory_service=memory_service,
        artifact_service=artifact_service,
    )


def _build_app() -> FastAPI:
    # Build the served FastAPI surface: A2A JSON-RPC + /lha/* routes + scheduler
    # endpoints. Reached lazily via __getattr__("app") so a bare import stays
    # cheap and offline.
    import contextlib
    from collections.abc import AsyncIterator

    from google.adk.cli.fast_api import get_fast_api_app

    from horizon.a2a.executor import build_executor
    from horizon.a2a.routes import attach_a2a_routes
    from horizon.agent import root_agent
    from horizon.api.feedback import attach_feedback_routes
    from horizon.api.memories import attach_memories_routes
    from horizon.api.processes import attach_processes_routes
    from horizon.api.routines import attach_routines_routes
    from horizon.api.sandbox import attach_sandbox_routes
    from horizon.api.secrets import attach_secrets_routes
    from horizon.api.sessions import attach_session_routes
    from horizon.api.state import attach_state_routes
    from horizon.api.tasks import attach_task_routes
    from horizon.api.uploads import attach_uploads_routes
    from horizon.auth import IdentityMiddleware
    from horizon.auth.oauth import attach_gcp_oauth_routes
    from horizon.scheduler import (
        dream_review_endpoint,
        routine_tick_endpoint,
        snapshot_endpoint,
    )
    from horizon.telemetry.otel import setup_telemetry

    setup_telemetry()
    allow_origins = (
        os.getenv("ALLOW_ORIGINS", "").split(",")
        if os.getenv("ALLOW_ORIGINS")
        else None
    )
    session_uri, memory_uri, artifact_uri = _resolve_service_uris()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runner = build_runner()
        # Scheduler routers (routine-tick / dream-review) reach the
        # persistent session_service + agent through this handle to create
        # and run inspectable scheduled sessions.
        app.state.runner = runner
        task_store = _build_task_store()
        # attach_a2a_routes builds the card + handler (the 1.x handler needs the
        # card) and returns the handler. The scheduler routine-tick drives this
        # same handler so a fired routine's turn records an A2A Task (history
        # the web UI can render) like any normal chat.
        request_handler = await attach_a2a_routes(
            app,
            agent=root_agent,
            agent_executor=build_executor(runner=runner),
            task_store=task_store,
        )
        app.state.a2a_handler = request_handler
        attach_state_routes(app, runner=runner)
        attach_session_routes(app, runner=runner)
        attach_task_routes(app, runner=runner, task_store=task_store)
        attach_memories_routes(app, runner=runner)
        attach_uploads_routes(app)
        attach_sandbox_routes(app)
        attach_processes_routes(app)
        attach_feedback_routes(app, runner=runner)
        attach_secrets_routes(app)
        attach_gcp_oauth_routes(app)
        attach_routines_routes(app)
        try:
            yield
        finally:
            await _shutdown_runtime()

    app = get_fast_api_app(
        agents_dir=AGENT_DIR,
        session_service_uri=session_uri,
        session_db_kwargs=(
            resilient_engine_kwargs()
            if is_pooled_sql_url(session_uri)
            else None
        ),
        memory_service_uri=memory_uri,
        artifact_service_uri=artifact_uri,
        allow_origins=allow_origins,
        web=False,
        # Cloud tracing calls google.auth.default(), so it needs ADC just to
        # build the app. On by default only when deployed (Cloud Run sets
        # K_SERVICE) so a bare checkout runs without credentials.
        otel_to_cloud=env_flag(
            "LHA_OTEL_TO_CLOUD", default=bool(os.environ.get("K_SERVICE"))
        ),
        lifespan=lifespan,
    )
    app.title = "horizon"
    app.description = "API for interacting with the Agent horizon"
    app.add_middleware(IdentityMiddleware)
    app.include_router(dream_review_endpoint.router)
    app.include_router(snapshot_endpoint.router)
    app.include_router(routine_tick_endpoint.router)
    return app


def __getattr__(name: str) -> FastAPI:
    # uvicorn / ADK serve resolve ``horizon.fast_api_app:app`` via getattr, so the
    # server app builds on first access rather than at import. Keeps a bare
    # ``import horizon.fast_api_app`` (e.g. reusing build_runner) hermetic — no
    # google.auth / vertexai calls just to reach a utility.
    if name == "app":
        if env_flag("LHA_ADK_SKIP_APP_BUILD"):
            raise AttributeError(
                "horizon.fast_api_app.app is disabled by LHA_ADK_SKIP_APP_BUILD"
            )
        built = _build_app()
        globals()["app"] = built
        return built
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
