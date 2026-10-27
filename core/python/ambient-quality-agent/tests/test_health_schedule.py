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

"""The schedule the health badge shows: the one the engine checks scheduler
ticks against for the agent a request is about."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ambient_quality_agent import config as config_module
from ambient_quality_agent.config import Config, ObservedAgentConfig
from ambient_quality_agent.core.investigation import model
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.observed_agent_config import (
    effective_config,
    store,
)
from ambient_quality_agent.tools.orchestrator import health_tools, insight_tools

from .conftest import FakeGcsClient, ToolContext, make_config

WATCHED = "watched_agent"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Config:
    """A deployment with nothing run and nothing found.

    Its configuration names no agent, as `config.load()` leaves it; the agent
    comes from an attachment.
    """
    cfg = make_config(observed_agent_name="")
    monkeypatch.setattr(effective_config, "env_config", cfg)
    monkeypatch.setattr(config_module, "load", lambda: cfg)
    effective_config.clear_cache()
    monkeypatch.setattr(
        model, "list_investigations", AsyncMock(return_value={"runs": []})
    )
    monkeypatch.setattr(
        insight_tools, "list_insights", AsyncMock(return_value={"insights": []})
    )
    return cfg


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch, env: Config) -> GcsObjectStore:
    client = FakeGcsClient()
    jobs_store = GcsObjectStore("jobs-bucket", client_factory=lambda: client)
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: jobs_store)
    return jobs_store


def _save(jobs: GcsObjectStore, **fields: Any) -> None:
    """Stores an attachment for the watched agent.

    Args:
        jobs: The jobs store.
        **fields: `AgentRecord` fields.
    """
    store.save_agent_record(
        jobs,
        store.AgentRecord(
            config=ObservedAgentConfig(
                observed_agent_name=WATCHED, agent_key=WATCHED
            ),
            **fields,
        ),
    )
    effective_config.clear_cache()


def _compute_shown_schedule() -> str | None:
    """Runs the health check the dashboard's badge reads.

    Returns:
        The schedule the badge shows, or None if it shows none.
    """
    return asyncio.run(health_tools.get_ambient_health(ToolContext()))[
        "schedule"
    ]


def test_the_attachments_schedule_is_shown(jobs: GcsObjectStore) -> None:
    """The engine reads it on the next tick."""
    _save(jobs, investigation_schedule="0 6 * * *")

    assert _compute_shown_schedule() == "0 6 * * *"


def test_a_paused_schedule_is_not_shown(jobs: GcsObjectStore) -> None:
    _save(
        jobs,
        investigation_schedule="0 6 * * *",
        scheduled_trigger_enabled=False,
    )

    assert _compute_shown_schedule() is None


def test_an_attachment_without_a_schedule_runs_on_the_default(
    jobs: GcsObjectStore,
) -> None:
    """The engine checks ticks against the default schedule."""
    _save(jobs)

    assert _compute_shown_schedule() == store.DEFAULT_INVESTIGATION_SCHEDULE


def test_with_no_agent_attached_no_schedule_is_shown(
    jobs: GcsObjectStore,
) -> None:
    assert _compute_shown_schedule() is None


def test_an_unreadable_attachment_shows_no_schedule(
    jobs: GcsObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the default: the agent's own schedule may be paused."""

    def fail(*args: Any) -> None:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(store, "get_agent_record", fail)

    assert _compute_shown_schedule() is None


def test_the_selected_agents_schedule_is_shown(jobs: GcsObjectStore) -> None:
    """Each agent has a schedule of its own; a request about one shows that
    one's."""
    # The flag keeps `WATCHED` the default over the alphabetically first key.
    _save(jobs, investigation_schedule="0 6 * * *", default=True)
    staging = "acme-staging__watched_agent"
    store.save_agent_record(
        jobs,
        store.AgentRecord(
            config=ObservedAgentConfig(
                observed_agent_name=WATCHED, agent_key=staging
            ),
            investigation_schedule="30 * * * *",
        ),
    )
    effective_config.clear_cache()
    context = ToolContext(state={effective_config.AGENT_KEY_STATE: staging})

    shown = asyncio.run(health_tools.get_ambient_health(context))["schedule"]

    assert shown == "30 * * * *"
    assert _compute_shown_schedule() == "0 6 * * *"


def test_a_standalone_run_shows_no_schedule(
    jobs: GcsObjectStore, env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone run has no scheduler, whatever an attachment says."""
    _save(jobs, investigation_schedule="0 6 * * *")
    monkeypatch.setattr(
        config_module,
        "load",
        lambda: dataclasses.replace(env, standalone=True),
    )

    assert _compute_shown_schedule() is None
