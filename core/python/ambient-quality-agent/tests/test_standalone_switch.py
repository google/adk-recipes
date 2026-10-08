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

"""`AQA_STANDALONE=1`: the switch, and a whole sweep behind it.

The test that carries the weight is the sweep. It runs the deployed graph over
the deployed nodes with `google.cloud.bigquery.Client` rigged to raise, then
reads the insight back through the provider the dashboard reads through. That is the
only check that the write adapters and the read adapters agree about one store.

The rest covers the switch itself:

- what `standalone` implies for the rest of the configuration,
- that the in-memory bindings share one store,
- that a trajectory written by the recorder is found by the reader,
- that the goal, the memories and the agent configuration are files under
  `.aqua/job/` rather than objects in a bucket.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_agent import backends
from ambient_quality_agent import config as config_module
from ambient_quality_agent.core import memory_chat_tools
from ambient_quality_agent.core.investigation import job_execution as execute
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.standalone import bindings as standalone_bindings
from ambient_quality_agent.standalone.files import FileObjectStore
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.tools import gcs
from ambient_quality_agent.tools.documents import memories as memories_doc
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.observed_agent_config import (
    commands,
    effective_config,
)
from ambient_quality_agent.tools.observed_agent_config import (
    store as agent_store,
)
from ambient_quality_agent.tools.orchestrator import (
    document_tools,
    health_tools,
    insight_tools,
    trajectory_tools,
)
from ambient_quality_agent.tools.trajectories.models import IngestStatus

from .conftest import (
    CallbackContext,
    FakeFetcher,
    FakeInsightModelCall,
    FakeReviewCall,
    StateContext,
    ToolContext,
    create_run,
    get_run,
    make_agent_case,
    make_config,
    make_page,
)

_WINDOW = {
    "window_start": "2026-01-01T00:00:00+00:00",
    "window_end": "2026-01-08T00:00:00+00:00",
    "budget_per_metric": 50,
}


@pytest.fixture(autouse=True)
def clear_ignored_setting_log() -> None:
    """Each test sees the once-per-process warnings as a new process would."""
    config_module._log_ignored_setting.cache_clear()


# --- what the configuration implies ------------------------------------------ #


def test_standalone_implies_running_the_sweep_inline() -> None:
    """There is no Agent Runtime to submit to, so the sweep runs inline."""
    assert make_config(standalone=True).sync_investigation is True


def test_standalone_survives_the_dict_round_trip() -> None:
    """A Config rebuilt from `asdict` is still standalone.

    The durable boundary and `effective_config.load` both rebuild one that way,
    so the implication lives in `__post_init__` rather than in `load()`.
    """
    rebuilt = config_module.Config(
        **dataclasses.asdict(make_config(standalone=True))
    )

    assert rebuilt.standalone is True
    assert rebuilt.sync_investigation is True


def test_a_workflow_state_seeded_from_config_is_still_standalone() -> None:
    """The sweep reads `standalone` off the run's state to decide whether an
    empty window fails. `local_run.py` and the preview seed that state through
    `from_config`, so it has to carry the flag the durable seed already does."""
    assert (
        WorkflowState.from_config(make_config(standalone=True)).standalone
        is True
    )
    assert WorkflowState.from_config(make_config()).standalone is False


def test_standalone_drops_the_delayed_update_wiring_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stray environment variable must not leave a run half-deployed.

    Dropping configuration is fine; dropping it silently is not.
    """
    with caplog.at_level("WARNING"):
        cfg = make_config(
            standalone=True,
            delay_task_queue="projects/p/locations/l/queues/q",
            ambient_caller_sa="caller@example.iam.gserviceaccount.com",
        )

    assert cfg.delay_task_queue == ""
    assert cfg.ambient_caller_sa == ""
    assert "delay_task_queue" in caplog.text
    assert "ambient_caller_sa" in caplog.text


def test_standalone_drops_aquas_own_buckets_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verifies that standalone mode resets cloud bucket settings to prevent writes.

    Because `.env.example` defines a placeholder jobs bucket, running standalone
    without clearing them would direct chat transcripts, spans, goals, and
    memories to external GCS buckets.
    """
    with caplog.at_level("WARNING"):
        cfg = make_config(
            standalone=True,
            jobs_gcs_bucket="a-project-aqua-jobs",
            traces_gcs_bucket="a-project-aqua-traces",
        )

    assert cfg.jobs_gcs_bucket == ""
    assert cfg.traces_gcs_bucket == ""
    assert "jobs_gcs_bucket" in caplog.text
    assert "traces_gcs_bucket" in caplog.text


def test_an_ignored_setting_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `Config` is rebuilt several times at start-up and once per run. The
    correction applies every time; the warning is logged once."""
    with caplog.at_level("WARNING"):
        for _ in range(3):
            make_config(standalone=True, jobs_gcs_bucket="a-project-aqua-jobs")

    assert caplog.text.count("ignores jobs_gcs_bucket") == 1


def test_a_deployment_keeps_its_buckets() -> None:
    cfg = make_config(jobs_gcs_bucket="a-project-aqua-jobs")

    assert cfg.jobs_gcs_bucket == "a-project-aqua-jobs"


def test_a_deployment_is_unaffected() -> None:
    """The switch is off unless asked for, and changes nothing when it is off."""
    cfg = make_config()

    assert cfg.standalone is False
    assert cfg.sync_investigation is False


def test_the_switch_reads_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AQA_OBSERVED_AGENT_NAME", "root_agent")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    monkeypatch.setenv("AQA_STANDALONE", "1")

    assert config_module.load().standalone is True


# --- the wiring ------------------------------------------------------------- #


def test_standalone_binds_every_provider_to_one_store() -> None:
    """The read and write adapters close over the same store.

    The sweep writes on its own thread and the routes read on theirs, so they
    have to be looking at one object. A factory that built its own store per
    call would satisfy every type and lose every write.
    """
    with backends.override_providers(standalone=True) as store:
        state = {"observed_agent_name": "root_agent"}
        reader = insight_tools.reader_factory(state)
        writer = insight_tools.store_factory(state)

    assert isinstance(store, InMemoryStore)
    assert reader._store is store
    assert writer._store is store


def test_standalone_health_does_not_blame_a_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the first sweep, health tells a standalone user to start one."""
    monkeypatch.setenv("AQA_OBSERVED_AGENT_NAME", "root_agent")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    monkeypatch.setenv("AQA_STANDALONE", "1")

    with backends.override_providers(standalone=True):
        verdict = asyncio.run(health_tools.get_ambient_health(ToolContext()))

    assert verdict["verdict"] == "never"
    assert "Run investigation" in verdict["reason"]


# --- the sweep ---------------------------------------------------------------- #


@pytest.fixture
def no_bigquery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make constructing a BigQuery client an error.

    Standalone's claim is not "it writes somewhere else" but "it reaches no
    dataset at all". Without this, a provider left bound to the deployed factory
    would pass every assertion below by writing to a client nobody looked at.
    """
    from google.cloud import bigquery

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("standalone reached BigQuery")

    monkeypatch.setattr(bigquery, "Client", explode)


def _finds_one_defect(holder: dict[str, Any]) -> None:
    """Configures faked models to report a defect for the sweep to store.

    The default test fakes return empty findings and clusters. Faking a defect
    ensures the sweep exercises the full store-and-read path.

    Args:
        holder: Dictionary of test factory instances to update.
    """
    holder["fetcher"] = FakeFetcher(pages=[make_page([make_agent_case("s1")])])
    holder["reviewer"] = FakeReviewCall(
        json.dumps(
            {
                "findings": [
                    {
                        "agent_id": "root_agent",
                        "expected_behavior": "calls the tool the prompt requires",
                        "actual_behavior": "answered without calling it",
                    }
                ]
            }
        )
    )
    holder["model_call"] = FakeInsightModelCall(
        json.dumps(
            {"clusters": [{"label": "made no tool call", "finding_ids": ["0"]}]}
        )
    )


def _standalone_config() -> Any:
    """Builds the effective configuration a standalone run executes under.

    Constructed from the environment rather than make_config so the agent name
    matches between the sweep's run config and dashboard reads.

    Returns:
        Config instance with standalone mode enabled.
    """
    return dataclasses.replace(effective_config.load({}), standalone=True)


def _standalone_run(state: dict[str, Any]) -> Any:
    """Creates a PENDING standalone run ready for `execute_investigation`.

    Args:
        state: Shared state dictionary for the run.

    Returns:
        Created investigation record.
    """
    cfg = _standalone_config()
    return create_run(
        state,
        runs.InvestigationRecord(
            status=runs.RunStatus.PENDING,
            observed_agent_name=cfg.observed_agent_name,
            effective_config=dataclasses.asdict(cfg),
            **_WINDOW,
        ),
    )


def test_a_sweep_runs_and_its_insights_read_back(
    patched_factories: dict[str, Any], no_bigquery: None
) -> None:
    """A sweep completes with no dataset, and its insight reads back.

    The graph runs to `done`, and the insight it wrote comes back through
    `insight_tools.reader_factory` -- the provider the dashboard's `insights/list`
    route reads through. Two separate adapters over one store.

    `patched_factories` supplies the fetcher, the evaluator, the reviewer and
    the model calls. Telemetry and Gemini platform stay real in standalone, so faking
    them is this test's business rather than the switch's. It also sets two
    storage providers; `override_providers` overrides those and gives them back after.
    """
    _finds_one_defect(patched_factories)
    state: dict[str, Any] = {}

    with backends.override_providers(standalone=True) as store:
        record = _standalone_run(state)
        result = asyncio.run(
            execute.execute_investigation(record.run_id, StateContext(state))
        )

        assert result["status"] == "done", result.get("error")

        insights, total = insight_tools.reader_factory(state).list_insights(
            limit=10, offset=0
        )

    assert total >= 1
    assert insights[0].label
    # And the store really is where they went, not a fake the graph was handed.
    assert len(store.insights) == total


def test_a_sweeps_events_reach_the_run_registry(
    patched_factories: dict[str, Any], no_bigquery: None
) -> None:
    """The nodes' events and the run record meet in one store.

    They travel through two providers, `core_state.event_writer` and
    `investigation_model.store_factory`. A backend that bound one and not the
    other would leave the run detail page empty while the sweep reported
    success.
    """
    state: dict[str, Any] = {}

    with backends.override_providers(standalone=True):
        record = _standalone_run(state)
        asyncio.run(
            execute.execute_investigation(record.run_id, StateContext(state))
        )
        stored = get_run(state, record.run_id)

    assert stored is not None
    assert stored.status is runs.RunStatus.DONE
    assert stored.events


def test_the_trajectory_halves_meet_over_one_store(
    patched_factories: dict[str, Any], no_bigquery: None
) -> None:
    """The recorder writes and the reader reads, through two separate providers.

    The sweep above does not cover this: `FakeFetcher` serves pages without
    reporting to a recorder, so a run over it ingests nothing. Driving the two
    factories directly is the narrower check, and the one that fails if either
    provider is bound to a store of its own.
    """
    agent = effective_config.load({}).observed_agent_name
    ctx = StateContext(
        {
            "observed_agent_name": agent,
            "run_id": "run-1",
            "telemetry_source": "cloud_ops",
        }
    )

    with backends.override_providers(standalone=True):
        recorder = _common.trajectory_recorder_factory(ctx)
        recorder.add(
            trajectory_id="chat-1",
            session_id="chat-1",
            trace_ids=["trace-1"],
            status=IngestStatus.INGESTED,
        )
        recorder.flush()

        found = trajectory_tools.reader_factory(
            {"observed_agent_name": agent}
        ).get_trajectories([("run-1", "chat-1")])

    assert found["run-1", "chat-1"].source_trace_ids == ["trace-1"]


# --- the jobs store ----------------------------------------------------------- #


@pytest.fixture
def no_gcs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make constructing a Cloud Storage client an error, as `no_bigquery` does
    for the dataset."""

    def explode() -> Any:
        raise AssertionError("standalone reached Cloud Storage")

    monkeypatch.setattr(gcs, "create_storage_client", explode)


def test_standalone_keeps_its_files_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.aqua/` is gitignored, here and in a project agents-cli scaffolds."""
    monkeypatch.chdir(tmp_path)

    assert standalone_bindings.resolve_jobs_dir() == tmp_path / ".aqua" / "job"


def test_standalone_binds_the_jobs_store_to_its_directory(
    tmp_path: Path,
) -> None:
    with backends.override_providers(
        standalone=True, jobs_dir=tmp_path / "job"
    ):
        store = objects.jobs_store_factory()

    assert isinstance(store, FileObjectStore)
    assert store.uri("goal.md") == str(tmp_path / "job" / "goal.md")


def test_the_goal_and_the_memories_are_files_in_standalone(
    tmp_path: Path, no_gcs: None
) -> None:
    """The panes are available, and what they save is a file a person can read.

    Before, both read "unavailable" without a jobs bucket.
    """
    jobs = tmp_path / "job"

    with backends.override_providers(standalone=True, jobs_dir=jobs):
        saved = asyncio.run(
            document_tools.set_goal(ToolContext(), goal="Refunds first.")
        )
        goal = asyncio.run(document_tools.get_goal(ToolContext()))
        memories_doc.record_memory(
            objects.jobs_store_factory(),
            "Tickets need a priority.",
            source="chat-1",
        )
        listed = asyncio.run(document_tools.list_memories(ToolContext()))
        versions = asyncio.run(document_tools.list_goal_versions(ToolContext()))

    assert saved["goal"] == "Refunds first."
    assert goal["available"] is True
    assert goal["uri"] == str(jobs / "goal.md")
    assert (jobs / "goal.md").read_text(encoding="utf-8") == "Refunds first."
    assert [v["text"] for v in versions["versions"]] == ["Refunds first."]
    assert listed["available"] is True
    assert [x["text"] for x in listed["memories"]] == [
        "Tickets need a priority."
    ]


def test_a_memory_is_a_file_that_outlives_the_standalone_run(
    tmp_path: Path, no_gcs: None
) -> None:
    """What the chat remembers lands in `.aqua/job/memories/`, is read back by
    `get_memories` in a later run, and the card's delete removes the file."""
    jobs = tmp_path / "job"
    text = "The agent's system prompt is in app/prompts/system.md."

    with backends.override_providers(standalone=True, jobs_dir=jobs):
        saved = asyncio.run(
            memory_chat_tools.remember(
                text=text,
                tool_context=CallbackContext(
                    "Remember where the prompt is.", session_id="chat-1"
                ),
            )
        )
    path = jobs / "memories" / f"{saved['id']}.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    with backends.override_providers(standalone=True, jobs_dir=jobs):
        read_back = asyncio.run(
            memory_chat_tools.get_memories(tool_context=ToolContext())
        )
        deleted = asyncio.run(
            document_tools.delete_memory(ToolContext(), memory_id=saved["id"])
        )

    assert saved["saved"] is True
    assert on_disk["text"] == text
    assert on_disk["source"] == "chat-1"
    assert [m["text"] for m in read_back["memories"]] == [text]
    assert deleted == {"deleted": True}
    assert not path.exists()


def test_a_standalone_run_reads_the_attached_agents_configuration_from_a_file(
    tmp_path: Path, no_gcs: None
) -> None:
    """An `agents/<name>.json` beside the goal configures the agent, as the
    object `attach` writes does in a deployment."""
    agent = effective_config.load({}).observed_agent_name
    jobs = tmp_path / "job"
    (jobs / "agents").mkdir(parents=True)
    (jobs / agent_store.build_object_name(agent)).write_text(
        json.dumps(
            {"schema_version": 1, "agent": {"data_lookback_window": 21}}
        ),
        encoding="utf-8",
    )

    with backends.override_providers(standalone=True, jobs_dir=jobs):
        effective_config.clear_cache()
        assert effective_config.load({}).data_lookback_window == 21


def test_aqua_attach_stores_a_file_in_standalone(
    tmp_path: Path, no_gcs: None
) -> None:
    """The attach routes write through the same jobs store, so an attach is a
    file, and the next request runs under it."""
    agent = effective_config.load({}).observed_agent_name
    jobs = tmp_path / "job"

    with backends.override_providers(standalone=True, jobs_dir=jobs):
        result = asyncio.run(
            commands.attach_agent(
                agent_name=agent,
                fields={"data_lookback_window": 21},
                dry_run=False,
            )
        )
        loaded = effective_config.load({})

    assert result["written"] is True, result
    assert result["object"] == str(jobs / agent_store.build_object_name(agent))
    assert (jobs / agent_store.build_object_name(agent)).is_file()
    assert loaded.data_lookback_window == 21
