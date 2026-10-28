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

"""Tests for the orchestrator: `before_agent` (deterministic keyword routing)
and its chat-facing tool surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core import orchestrator
from ambient_quality_agent.core.investigation import job_execution
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.standalone.investigations import (
    InMemoryInvestigationStore,
)
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.tools.observed_agent_config import (
    effective_config,
    store,
)

from .conftest import CallbackContext, StateContext, create_run, get_run


def test_before_agent_routes_keyword_to_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the real graph so we don't touch BigQuery/Gemini platform.
    fake_graph = mock.AsyncMock(
        return_value={
            "status": "done",
            "summary": {"window_start": "s", "window_end": "e"},
        }
    )
    monkeypatch.setattr(job_execution, "run_investigation_graph", fake_graph)

    # Seed a pending run the keyword message will reference.
    ctx = CallbackContext(f"{orchestrator.RUN_KEYWORD} run42")
    create_run(
        ctx.state,
        runs.InvestigationRecord(run_id="run42", status=runs.RunStatus.PENDING),
    )

    out = asyncio.run(orchestrator.before_agent(ctx))

    # Returns a Content (ends the turn deterministically; LLM not consulted).
    assert out is not None
    parts = out.parts or []
    assert "run42" in (parts[0].text or "")
    # The run was driven to done by the work branch.
    assert get_run(ctx.state, "run42").status == runs.RunStatus.DONE


def test_the_job_finds_a_run_of_an_agent_other_than_the_default(
    monkeypatch: pytest.MonkeyPatch, jobs_store: Any
) -> None:
    """The job selects no agent, so its store is the default agent's; the run
    is still found by its id, which is unique across agents."""
    key = "acme-staging__it_support_agent"
    jobs_store.write_text(
        store.build_object_name(key),
        json.dumps(
            {
                "schema_version": 1,
                "agent": {
                    "observed_agent_name": "it_support_agent",
                    "observed_project_id": "acme-staging",
                },
            }
        ),
    )
    backing = InMemoryStore()
    monkeypatch.setattr(
        runs,
        "store_factory",
        lambda state: InMemoryInvestigationStore(
            backing,
            agent_name=effective_config.load(state).resolve_agent_key(),
        ),
    )
    monkeypatch.setattr(
        job_execution,
        "run_investigation_graph",
        mock.AsyncMock(return_value={"status": "done", "summary": {}}),
    )
    run_id = runs.InvestigationRecord().run_id
    InMemoryInvestigationStore(backing, agent_name=key).append(
        runs.InvestigationRecord(
            run_id=run_id,
            status=runs.RunStatus.PENDING,
            observed_agent_name=key,
        )
    )

    asyncio.run(
        orchestrator.before_agent(
            CallbackContext(f"{orchestrator.RUN_KEYWORD} {run_id}")
        )
    )

    assert backing.runs[run_id].status == runs.RunStatus.DONE


def test_before_agent_passes_through_normal_message() -> None:
    ctx = CallbackContext("what can you do?")
    out = asyncio.run(orchestrator.before_agent(ctx))
    # No short-circuit: the LLM handles it, and nothing was seeded on the way.
    assert out is None
    assert dict(ctx.state) == {}


def test_insight_read_tools_registered() -> None:
    tool_names = {t.__name__ for t in orchestrator.ORCHESTRATOR_TOOLS}
    assert {"list_insights", "get_insight"} <= tool_names


def test_the_orchestrator_reports_a_root_cause_but_cannot_author_one() -> None:
    """Verifies that the orchestrator can relay existing root causes but cannot record new ones."""
    tool_names = {t.__name__ for t in orchestrator.ORCHESTRATOR_TOOLS}
    instruction = " ".join(orchestrator._INSTRUCTION.split())

    assert "record_root_cause" not in tool_names
    assert "`root_causes` that `get_insight` returns" in instruction
    assert (
        "never diagnose one yourself or propose a fix of your own"
        in instruction
    )


def test_get_investigation_missing_and_empty() -> None:
    tc = StateContext()
    assert "error" in asyncio.run(orchestrator.get_investigation("missing", tc))
    assert "error" in asyncio.run(orchestrator.get_investigation("", tc))
