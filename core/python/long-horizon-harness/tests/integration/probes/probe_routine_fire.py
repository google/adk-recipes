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

"""Routine fire path, end-to-end against a REAL sandbox (RUN_SANDBOX_PROBE).

The real-isolation proof for ``/scheduler/routine-tick``: a due routine must run
its turn in its OWN ``lhart-<id>`` sandbox (never the user's), in a tagged
``job_type="routine"`` session. This is the live counterpart to the deterministic
``tests/unit/scheduler/test_routine_tick.py`` (which monkeypatches the turn) — it
drives a real ADK runner + A2A handler so the env-binding (``set_routine_run`` →
``_build_routine_sandbox_environment``) actually provisions Vertex Agent Runtime.

Fixtures (``fresh_user_id``/``sandbox_env_vars``/``reset_env_cache``/
``vertex_client``) come from ``conftest.py``; the gate auto-marks this skipped
unless ``RUN_SANDBOX_PROBE=1``.

The runner/handler are built exactly as ``fast_api_app._build_app``'s lifespan does
(``build_runner`` + ``attach_a2a_routes`` over ``build_executor``) and
hung off ``app.state`` so ``routine_tick`` drives them like production. The task is
trivial and self-contained ("write hello.txt") so no secrets, network, or user
workspace are needed — ``secrets=()``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.infrastructure.constants import (
    APP_NAME,
    SCHEDULER_JOB_KEY,
    SESSION_SOURCE_KEY,
)
from app.scheduler import routine_store as store_mod
from app.scheduler import routine_tick_endpoint
from app.scheduler.routine_store import RoutineRow

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_routine_store(monkeypatch: pytest.MonkeyPatch):
    """Force the in-memory store and reset the singleton around the probe so the
    registered row is the only thing ``routine_tick`` claims, and disable the
    scheduler OIDC gate (no Cloud Scheduler token in a probe)."""
    store_mod.reset_routine_store()
    monkeypatch.setenv("LHA_ROUTINE_STORE", "memory")
    monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", "1")
    yield
    store_mod.reset_routine_store()


async def test_routine_fires_in_isolated_sandbox(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
    vertex_client: object,
) -> None:
    from app.a2a.executor import build_executor
    from app.a2a.routes import attach_a2a_routes
    from app.agent import root_agent
    from app.fast_api_app import _build_task_store, build_runner
    from app.sandbox.lifecycle import (
        find_latest_user_sandbox,
        find_routine_sandbox,
    )
    from app.sandbox.provider import _resolve_sandbox_engine

    image_uri = os.environ.get("LHA_RUNTIME_IMAGE", "").strip()
    routine_id = f"probe-rt-{fresh_user_id}"

    # Owner is a fresh, never-provisioned user so the "main sandbox untouched"
    # assertion is unambiguous: it must be None both before and after the fire.
    owner = fresh_user_id
    agent_instance = _resolve_sandbox_engine(vertex_client)  # type: ignore[arg-type]
    assert (
        find_latest_user_sandbox(
            vertex_client,  # type: ignore[arg-type]
            agent_instance_name=agent_instance,
            user_id=owner,
        )
        is None
    ), "precondition: owner must start with no main sandbox"

    # A real runner + A2A handler, wired exactly like _build_app's lifespan, so
    # routine_tick drives a genuine agent turn (which provisions the lhart- sandbox).
    os.environ["LHA_ENVIRONMENT_BACKEND"] = "sandbox"
    runner = build_runner()

    store = store_mod.get_routine_store()
    await store.add(
        RoutineRow(
            id=routine_id,
            user_id=owner,
            app_name=APP_NAME,
            schedule="*/5 * * * *",
            task=(
                "Write a file named hello.txt containing exactly the text OK, "
                "then stop. Do not ask any questions."
            ),
            secrets=(),
            delivery="report_back",
            next_fire_at=datetime.now(UTC)
            - timedelta(minutes=1),  # already due
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
    )

    app = FastAPI()
    handler = await attach_a2a_routes(
        app,
        agent=root_agent,
        agent_executor=build_executor(runner=runner),
        task_store=_build_task_store(),
    )
    app.include_router(routine_tick_endpoint.router)
    app.state.runner = runner
    app.state.a2a_handler = handler

    try:
        resp = TestClient(app).post("/scheduler/routine-tick")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("fired") == 1, resp.json()

        # (a) the routine got its OWN lhart-<id> sandbox.
        routine_sandbox = find_routine_sandbox(
            vertex_client,  # type: ignore[arg-type]
            agent_instance_name=agent_instance,
            routine_id=routine_id,
            image_uri=image_uri,
        )
        assert routine_sandbox is not None, (
            "routine fire must provision a lhart- sandbox for the routine"
        )

        # (b) the owner's main (lha-<user>-) sandbox was never created/touched.
        assert (
            find_latest_user_sandbox(
                vertex_client,  # type: ignore[arg-type]
                agent_instance_name=agent_instance,
                user_id=owner,
            )
            is None
        ), "routine fire must NOT provision the user's main sandbox"

        # (c) a tagged job_type="routine" session was persisted.
        listed = await runner.session_service.list_sessions(
            app_name=getattr(runner, "app_name", None) or APP_NAME,
            user_id=owner,
        )
        assert len(listed.sessions) == 1, listed.sessions
        full = await runner.session_service.get_session(
            app_name=getattr(runner, "app_name", None) or APP_NAME,
            user_id=owner,
            session_id=listed.sessions[0].id,
        )
        assert full.state[SESSION_SOURCE_KEY] == "scheduler"
        assert full.state[SCHEDULER_JOB_KEY] == "routine"
    finally:
        # Best-effort teardown so the probe doesn't leak the routine sandbox.
        from app.sandbox.lifecycle import delete_sandbox

        leftover = find_routine_sandbox(
            vertex_client,  # type: ignore[arg-type]
            agent_instance_name=agent_instance,
            routine_id=routine_id,
            image_uri=image_uri,
        )
        if leftover:
            try:
                delete_sandbox(vertex_client, sandbox_name=leftover)  # type: ignore[arg-type]
            except Exception:
                pass
