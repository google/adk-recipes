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

"""Effective runtime config -- the environment with the attached agent's stored
configuration over it -- and the one orchestrator tool that reports it."""

from __future__ import annotations

import json
from typing import Any

import pytest
from ambient_quality_agent.config import Config
from ambient_quality_agent.core import orchestrator
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.observed_agent_config import (
    effective_config,
    store,
)
from ambient_quality_agent.tools.orchestrator import config_tools

from .conftest import FakeGcsClient, StateContext, make_config

BUCKET = "jobs-bucket"


@pytest.fixture
def env_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = make_config()
    monkeypatch.setattr(effective_config, "env_config", cfg)
    return cfg


@pytest.fixture
def attached_env(monkeypatch: pytest.MonkeyPatch) -> Config:
    """An environment config for the agent `_attach` stores an object for."""
    cfg = make_config(
        observed_agent_name="watched-agent",
        metrics_gcs_bucket="metrics-bucket",
    )
    monkeypatch.setattr(effective_config, "env_config", cfg)
    return cfg


def _attach(monkeypatch: pytest.MonkeyPatch, payload: Any) -> FakeGcsClient:
    """Uploads a configuration object to a fake jobs bucket and binds the jobs store to it.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        payload: Configuration payload (dictionary or raw bytes).

    Returns:
        FakeGcsClient instance containing the uploaded configuration object.
    """
    raw = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload).encode("utf-8")
    )
    client = FakeGcsClient(
        {(BUCKET, store.build_object_name("watched-agent")): raw}
    )
    jobs = GcsObjectStore(BUCKET, client_factory=lambda: client)
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: jobs)
    return client


def test_the_effective_config_is_the_deploy_time_one(
    env_config: Config,
) -> None:
    """No object for the agent: the environment answers for both halves."""
    config = effective_config.load({})

    assert config == env_config


def test_a_deployment_without_a_jobs_bucket_reads_the_environment_quietly(
    env_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nowhere for an object to be is not a failed read, so nothing is logged."""
    monkeypatch.setattr(
        objects, "jobs_store_factory", lambda: GcsObjectStore("")
    )

    assert effective_config.load({}) == env_config
    assert not caplog.records


def test_a_standalone_run_reads_the_object_from_its_directory(
    env_config: Config, jobs_store: Any
) -> None:
    """The jobs store is files there, and the object is read the same way."""
    jobs_store.write_text(
        store.build_object_name(env_config.observed_agent_name),
        json.dumps({"agent": {"data_lookback_window": 30}}),
    )

    assert effective_config.load({}).data_lookback_window == 30


def test_session_state_cannot_change_it(env_config: Config) -> None:
    """Session state is ignored for configuration overrides."""
    state = {"app:aqa_config_overrides": {"data_lookback_window": 14}}

    assert effective_config.load(state).data_lookback_window == 7


# --- the stored object over the environment --------------------------------- #


def test_the_attached_agents_settings_win(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _attach(
        monkeypatch,
        {"schema_version": 1, "agent": {"data_lookback_window": 30}},
    )

    config = effective_config.load({})

    assert config.data_lookback_window == 30


def test_the_object_answers_for_the_whole_agent_half(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key the object omits falls back to the field's default, not to the
    environment. An agent is either attached, and the object describes it, or it
    is not -- there is no third state where half the answer comes from each,
    which nobody reading the object could see.
    """
    _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})

    config = effective_config.load({})

    assert config.data_lookback_window == 30
    # The environment says 100; the object says nothing, so the default stands.
    assert attached_env.data_evaluation_cap == 100
    assert config.data_evaluation_cap == 1000


def test_the_object_cannot_touch_aquas_own_fields(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The split is what stops an attach from repointing the bucket whose
    contents execute on the engine's credentials."""
    _attach(
        monkeypatch,
        {
            "agent": {
                "metrics_gcs_bucket": "attacker-bucket",
                "project_id": "theirs",
            }
        },
    )

    config = effective_config.load({})

    assert config.metrics_gcs_bucket == "metrics-bucket"
    assert config.project_id == attached_env.project_id


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json at all", id="unparseable"),
        pytest.param(
            {"agent": {"quality_analysis_mode": "nonsense"}}, id="invalid-value"
        ),
    ],
)
def test_an_unusable_object_leaves_the_environment_answering(
    attached_env: Config,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    payload: Any,
) -> None:
    """Until the environment stops carrying the agent's configuration it is a
    working answer, and a configuration read is not worth failing every request
    over. Logged, never silent."""
    _attach(monkeypatch, payload)

    config = effective_config.load({})

    assert config == attached_env
    assert caplog.records


# --- how often it reads ------------------------------------------------------ #


def test_the_object_is_read_once_and_reused(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `/config` route is hit far more often than anyone re-attaches."""
    client = _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})

    for _ in range(3):
        effective_config.load({})

    assert len(client.downloads) == 1


def test_clearing_the_cache_reads_again(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})
    effective_config.load({})

    effective_config.clear_cache()
    effective_config.load({})

    assert len(client.downloads) == 2


def test_a_bucket_this_deployment_cannot_read_is_tried_once(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read is cached like an absent object, so a broken bucket costs
    one attempt per TTL rather than one per request."""
    attempts: list[str] = []

    def _boom(*_a: Any, **_k: Any) -> None:
        attempts.append("read")
        raise RuntimeError("no credentials here")

    monkeypatch.setattr(store, "get_agent_record", _boom)

    for _ in range(3):
        assert effective_config.load({}) == attached_env

    assert len(attempts) == 1


def test_a_read_is_reused_only_while_it_is_fresh(
    attached_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})
    clock = [1000.0]
    monkeypatch.setattr(effective_config.time, "monotonic", lambda: clock[0])

    effective_config.load({})
    clock[0] += effective_config.CACHE_TTL_SECONDS + 1
    effective_config.load({})

    assert len(client.downloads) == 2


# --- the tool and what is gone ----------------------------------------------- #


def test_show_config_reports_it(env_config: Config) -> None:
    reported = orchestrator.show_config(StateContext())["config"]

    assert reported["data_lookback_window"] == 7
    assert reported["multi_turn_metrics"] == ["task_success"]


def test_the_config_writers_are_gone() -> None:
    """Configuration has one writer -- `agents-cli aqua attach` -- and a chat
    turn is not it. A session override was invisible to everyone outside that
    session and silently changed what every later sweep ran with.

    Asserted against the module and the tool list rather than by name alone, so
    re-adding a writer under any name fails here too: `show_config` is the only
    config tool the orchestrator holds.
    """
    for gone in (
        "update_config",
        "reset_config",
        "save_override",
        "clear_overrides",
        "MUTABLE_FIELDS",
        "OVERRIDES_KEY",
    ):
        for module in (config_tools, effective_config):
            assert not hasattr(module, gone), (module.__name__, gone)

    config_tool_names = {
        tool.__name__
        for tool in orchestrator.ORCHESTRATOR_TOOLS
        if "config" in tool.__name__
    }
    assert config_tool_names == {"show_config"}


# --- a deployment that names no agent ----------------------------------------- #


@pytest.fixture
def unnamed_env(monkeypatch: pytest.MonkeyPatch) -> Config:
    """A deployment whose environment names no observed agent."""
    cfg = make_config(observed_agent_name="")
    monkeypatch.setattr(effective_config, "env_config", cfg)
    return cfg


def _store_agent(jobs_store: Any, name: str, **agent: Any) -> None:
    """Writes an attached agent's object into the jobs store.

    Args:
        jobs_store: The test's jobs store.
        name: Agent name.
        **agent: Fields of the object's `agent` section.
    """
    jobs_store.write_text(
        store.build_object_name(name),
        json.dumps(
            {
                "schema_version": 1,
                "agent": {"observed_agent_name": name, **agent},
            }
        ),
    )


def test_with_nothing_attached_there_is_no_agent(unnamed_env: Config) -> None:
    assert effective_config.load({}).observed_agent_name == ""
    assert effective_config.resolve_observed_agent_name() is None


def test_the_default_attached_agent_is_the_one_investigated(
    unnamed_env: Config, jobs_store: Any
) -> None:
    _store_agent(jobs_store, "agent-a", data_lookback_window=30)
    _store_agent(jobs_store, "agent-b", default=True, data_lookback_window=14)

    config = effective_config.load({})

    assert config.observed_agent_name == "agent-b"
    assert config.data_lookback_window == 14


def test_the_environments_agent_outranks_the_default(
    env_config: Config, jobs_store: Any
) -> None:
    _store_agent(jobs_store, "agent-b", default=True)

    assert (
        effective_config.resolve_observed_agent_name()
        == env_config.observed_agent_name
    )


def test_an_attach_changes_the_default_at_once(
    unnamed_env: Config, jobs_store: Any
) -> None:
    """The default is cached, and a write in this process drops the cache."""
    assert effective_config.resolve_observed_agent_name() is None
    _store_agent(jobs_store, "agent-a")
    effective_config.forget_cached_record("agent-a")

    assert effective_config.resolve_observed_agent_name() == "agent-a"
