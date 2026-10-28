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

"""Effective runtime config -- AQuA's own configuration from the environment
with the attached agent's stored configuration over it -- and the one
orchestrator tool that reports it."""

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
WATCHED = "watched_agent"


@pytest.fixture
def env_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    """The deployment's own configuration, which names no agent."""
    cfg = make_config(
        observed_agent_name="",
        metrics_gcs_bucket="metrics-bucket",
    )
    monkeypatch.setattr(effective_config, "env_config", cfg)
    return cfg


def _attach(
    monkeypatch: pytest.MonkeyPatch, payload: Any, key: str = WATCHED
) -> FakeGcsClient:
    """Uploads a configuration object to a fake jobs bucket and binds the jobs store to it.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        payload: Configuration payload (dictionary or raw bytes).
        key: What the object is named by.

    Returns:
        FakeGcsClient instance containing the uploaded configuration object.
    """
    raw = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload).encode("utf-8")
    )
    client = FakeGcsClient({(BUCKET, store.build_object_name(key)): raw})
    jobs = GcsObjectStore(BUCKET, client_factory=lambda: client)
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: jobs)
    return client


def test_with_nothing_attached_the_config_is_the_deployments_own(
    env_config: Config,
) -> None:
    """No agent is investigated until one is attached."""
    config = effective_config.load({})

    assert config == env_config
    assert config.observed_agent_name == ""
    assert effective_config.resolve_default_agent_key() is None


def test_a_deployment_without_a_jobs_bucket_has_no_agent_quietly(
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
        store.build_object_name(WATCHED),
        json.dumps({"agent": {"data_lookback_window": 30}}),
    )

    assert effective_config.load({}).data_lookback_window == 30


def test_session_state_cannot_change_it(
    env_config: Config, jobs_store: Any
) -> None:
    """Session state is ignored for configuration overrides."""
    _store_agent(jobs_store, WATCHED, data_lookback_window=30)
    state = {"app:aqa_config_overrides": {"data_lookback_window": 14}}

    assert effective_config.load(state).data_lookback_window == 30


# --- the stored object over the deployment's configuration ------------------ #


def test_the_attached_agents_settings_win(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _attach(
        monkeypatch,
        {"schema_version": 1, "agent": {"data_lookback_window": 30}},
    )

    config = effective_config.load({})

    assert config.data_lookback_window == 30


def test_the_object_answers_for_the_whole_agent_half(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key the object omits falls back to the field's default, not to the
    deployment's configuration. An agent is either attached, and the object describes it, or it
    is not -- there is no third state where half the answer comes from each,
    which nobody reading the object could see.
    """
    _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})

    config = effective_config.load({})

    assert config.data_lookback_window == 30
    # The deployment's configuration says 100; the object says nothing, so the
    # default stands.
    assert env_config.data_evaluation_cap == 100
    assert config.data_evaluation_cap == 1000


def test_the_object_cannot_touch_aquas_own_fields(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
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
    assert config.project_id == env_config.project_id


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json at all", id="unparseable"),
        pytest.param(
            {"agent": {"quality_analysis_mode": "nonsense"}}, id="invalid-value"
        ),
    ],
)
def test_an_unusable_object_leaves_no_agent_selected(
    env_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    payload: Any,
) -> None:
    """A configuration read is not worth failing every request over, so the
    request runs as if nothing were attached. Logged, never silent."""
    _attach(monkeypatch, payload)

    config = effective_config.load({})

    assert config == env_config
    assert caplog.records


# --- how often it reads ------------------------------------------------------ #


def test_the_object_is_read_once_and_reused(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `/config` route is hit far more often than anyone re-attaches.
    The first load reads the object to resolve the default agent and again for
    its settings; later loads reuse both."""
    client = _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})
    effective_config.load({})
    first_reads = len(client.downloads)

    for _ in range(2):
        effective_config.load({})

    assert first_reads > 0
    assert len(client.downloads) == first_reads


def test_clearing_the_cache_reads_again(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})
    effective_config.load({})
    first_reads = len(client.downloads)

    effective_config.clear_cache()
    effective_config.load({})

    assert len(client.downloads) == 2 * first_reads


def test_a_bucket_this_deployment_cannot_read_is_tried_once(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read is cached like no attached agent, so a broken bucket
    costs one attempt per TTL rather than one per request."""
    _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})
    attempts: list[str] = []

    def _boom(*_a: Any, **_k: Any) -> None:
        attempts.append("read")
        raise RuntimeError("no credentials here")

    monkeypatch.setattr(store, "get_agent_record", _boom)

    for _ in range(3):
        assert effective_config.load({}) == env_config

    assert len(attempts) == 1


def test_a_read_is_reused_only_while_it_is_fresh(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _attach(monkeypatch, {"agent": {"data_lookback_window": 30}})
    clock = [1000.0]
    monkeypatch.setattr(effective_config.time, "monotonic", lambda: clock[0])

    effective_config.load({})
    first_reads = len(client.downloads)
    clock[0] += effective_config.CACHE_TTL_SECONDS + 1
    effective_config.load({})

    assert len(client.downloads) == 2 * first_reads


# --- the tool and what is gone ----------------------------------------------- #


def test_show_config_reports_it(env_config: Config, jobs_store: Any) -> None:
    _store_agent(
        jobs_store,
        WATCHED,
        data_lookback_window=30,
        multi_turn_metrics=["task_success"],
    )

    reported = orchestrator.show_config(StateContext())["config"]

    assert reported["observed_agent_name"] == WATCHED
    assert reported["data_lookback_window"] == 30
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


# --- the default agent --------------------------------------------------------- #


def _store_agent(jobs_store: Any, key: str, **agent: Any) -> None:
    """Writes an attached agent's object into the jobs store.

    Args:
        jobs_store: The test's jobs store.
        key: Agent key, `<project id>__<agent name>`, or the bare name of an
            agent in AQuA's own project.
        **agent: Fields of the object's `agent` section.
    """
    jobs_store.write_text(
        store.build_object_name(key),
        json.dumps(
            {
                "schema_version": 1,
                "agent": {
                    "observed_agent_name": key.rpartition("__")[2],
                    **agent,
                },
            }
        ),
    )


def test_the_default_attached_agent_is_the_one_investigated(
    env_config: Config, jobs_store: Any
) -> None:
    _store_agent(jobs_store, "acme-aqua__agent_a", data_lookback_window=30)
    _store_agent(
        jobs_store, "acme-aqua__agent_b", default=True, data_lookback_window=14
    )

    config = effective_config.load({})

    assert config.observed_agent_name == "agent_b"
    assert config.agent_key == "acme-aqua__agent_b"
    assert config.data_lookback_window == 14


def test_an_attach_changes_the_default_at_once(
    env_config: Config, jobs_store: Any
) -> None:
    """The default is cached, and a write in this process drops the cache."""
    assert effective_config.resolve_default_agent_key() is None
    _store_agent(jobs_store, "acme-aqua__agent_a")
    effective_config.forget_cached_record("acme-aqua__agent_a")

    assert effective_config.resolve_default_agent_key() == "acme-aqua__agent_a"


def test_an_agent_is_read_from_its_bare_name(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An object stored under the bare name configures the agent."""
    _attach(monkeypatch, {"agent": {"data_lookback_window": 30}}, key=WATCHED)

    config = effective_config.load({})

    assert config.data_lookback_window == 30
    assert config.agent_key == WATCHED


def test_a_bare_name_attachment_keeps_the_project_it_records(
    env_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored under its bare name with another project, the agent keeps that
    key, and its settings apply."""
    _attach(
        monkeypatch,
        {
            "agent": {
                "observed_project_id": "acme-prod",
                "data_lookback_window": 30,
            }
        },
        key=WATCHED,
    )

    config = effective_config.load({})

    assert config.agent_key == WATCHED
    assert config.observed_project_id == "acme-prod"
    assert config.data_lookback_window == 30


# --- the agent a request selects ---------------------------------------------- #


def _build_selection_state(key: str) -> dict[str, str]:
    """Builds the state of a request that selects `key`.

    Args:
        key: Agent key.

    Returns:
        State holding the selection.
    """
    return {effective_config.AGENT_KEY_STATE: key}


def test_a_request_selects_an_agent_besides_the_default(
    env_config: Config, jobs_store: Any
) -> None:
    _store_agent(jobs_store, "acme-prod__bot", data_lookback_window=21)
    _store_agent(jobs_store, "acme-staging__bot", data_lookback_window=3)

    config = effective_config.load(_build_selection_state("acme-staging__bot"))

    assert config.agent_key == "acme-staging__bot"
    assert config.observed_agent_name == "bot"
    assert config.data_lookback_window == 3
    # The default stays what a request selecting none gets.
    assert effective_config.load({}).agent_key == "acme-prod__bot"


def test_selecting_an_agent_that_is_not_attached_is_an_error(
    env_config: Config, jobs_store: Any
) -> None:
    """Falling back would show the default agent's data under another key."""
    _store_agent(jobs_store, "acme-prod__bot")

    with pytest.raises(effective_config.UnknownAgentError, match="gone"):
        effective_config.load(_build_selection_state("acme-staging__gone"))


def test_selecting_an_agent_with_an_invalid_configuration_is_an_error(
    env_config: Config, jobs_store: Any
) -> None:
    _store_agent(jobs_store, "acme-prod__bot")
    _store_agent(jobs_store, "acme-staging__bot", quality_analysis_mode="vibes")

    with pytest.raises(effective_config.UnknownAgentError, match="valid"):
        effective_config.load(_build_selection_state("acme-staging__bot"))


# --- the agents ambient triggers are for -------------------------------------- #


def test_a_tick_is_for_every_attached_agent(
    env_config: Config, jobs_store: Any
) -> None:
    _store_agent(jobs_store, "acme-staging__bot")
    _store_agent(jobs_store, "acme-prod__bot", default=True)

    assert effective_config.list_agent_keys() == [
        "acme-prod__bot",
        "acme-staging__bot",
    ]


def test_a_tick_with_nothing_attached_is_for_no_agent(
    env_config: Config,
) -> None:
    assert effective_config.list_agent_keys() == []


def test_an_update_is_for_the_agent_that_records_its_engine(
    env_config: Config, jobs_store: Any
) -> None:
    """Staging and prod share a name; the engine tells them apart."""
    _store_agent(
        jobs_store, "acme-prod__bot", default=True, observed_engine_id="1"
    )
    _store_agent(
        jobs_store,
        "acme-staging__bot",
        observed_engine_id="projects/acme-staging/locations/l/reasoningEngines/2",
    )

    assert effective_config.resolve_engine_agent_key("2") == "acme-staging__bot"
    assert effective_config.resolve_engine_agent_key("1") == "acme-prod__bot"


def test_an_update_of_another_engine_is_for_no_agent(
    env_config: Config, jobs_store: Any
) -> None:
    """The default records its own engine, so the update is not its."""
    _store_agent(jobs_store, "acme-prod__bot", observed_engine_id="1")

    assert effective_config.resolve_engine_agent_key("3") is None


def test_an_update_is_for_the_default_agent_when_it_records_no_engine(
    env_config: Config, jobs_store: Any
) -> None:
    """An engine no attachment records belongs to the default, which may not
    have recorded its engine yet; an engine another agent records stays its."""
    _store_agent(jobs_store, "acme-prod__bot", default=True)
    _store_agent(jobs_store, "acme-staging__bot", observed_engine_id="1")

    assert effective_config.resolve_engine_agent_key("3") == "acme-prod__bot"
    assert effective_config.resolve_engine_agent_key("1") == "acme-staging__bot"


def test_an_update_of_aquas_own_engine_is_for_no_agent(
    monkeypatch: pytest.MonkeyPatch, jobs_store: Any
) -> None:
    monkeypatch.setattr(
        effective_config,
        "env_config",
        make_config(observed_agent_name="", aqa_engine_id="99"),
    )

    assert effective_config.resolve_engine_agent_key("99") is None


def test_an_unreadable_attachment_fails_the_update(
    env_config: Config, jobs_store: Any
) -> None:
    """Read as missing, the update would go to the default agent; raising has
    Pub/Sub deliver it again."""
    _store_agent(jobs_store, "acme-prod__bot", default=True)
    jobs_store.write_text(store.build_object_name("acme-staging__bot"), "{")

    with pytest.raises(ValueError):
        effective_config.resolve_engine_agent_key("2")


def test_a_triggers_agent_is_read_past_a_cached_failed_read(
    env_config: Config, jobs_store: Any
) -> None:
    """The cache holds a failed read as no attachment; a trigger reads again,
    and what it reads is cached for the investigation it starts."""
    effective_config._cache["acme-prod__bot"] = (float("inf"), None)
    _store_agent(jobs_store, "acme-prod__bot", data_lookback_window=21)

    config = effective_config.load_fresh_agent_config("acme-prod__bot")

    assert config.data_lookback_window == 21
    assert (
        effective_config.load(_build_selection_state("acme-prod__bot"))
        == config
    )


def test_a_trigger_for_an_agent_that_is_not_attached_is_an_error(
    env_config: Config, jobs_store: Any
) -> None:
    with pytest.raises(effective_config.UnknownAgentError, match="gone"):
        effective_config.load_fresh_agent_config("acme-prod__gone")


def test_a_triggers_attachment_that_cannot_be_read_raises(
    env_config: Config, jobs_store: Any
) -> None:
    jobs_store.write_text(store.build_object_name("acme-prod__bot"), "{")

    with pytest.raises(ValueError):
        effective_config.load_fresh_agent_config("acme-prod__bot")


def test_an_update_reads_the_default_attached_agent_afresh(
    env_config: Config, jobs_store: Any
) -> None:
    """A cached failed resolve would answer the update as no agent's."""
    effective_config._default_cache[:] = [(float("inf"), None)]
    _store_agent(jobs_store, "acme-prod__bot")

    assert effective_config.resolve_engine_agent_key("3") == "acme-prod__bot"
