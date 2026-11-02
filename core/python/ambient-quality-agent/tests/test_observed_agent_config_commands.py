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

"""The engine side of `aqua attach`: merging a request over how an agent is
observed now, validating it the way the runtime does, and storing it.

The bucket is a `FakeGcsClient` holding real bytes, so what these tests store is
read back through `store`, the reader every investigation uses.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import types
from typing import Any

import pytest
from ambient_quality_agent.config import Config, ObservedAgentConfig
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.observed_agent_config import (
    access_check,
    commands,
    effective_config,
    store,
)
from ambient_quality_agent.tools.telemetry import schema

from .conftest import FakeGcsClient, load_schema_fixture, make_config

BUCKET = "jobs-bucket"
WATCHED = "watched_agent"
WATCHED_KEY = WATCHED
"""An agent in AQuA's own project keeps its bare name."""
KEYED = f"test-project__{WATCHED}"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Config:
    """A deployment whose configuration names no agent, with a few agent
    fields away from their defaults, so an attach that started from them
    rather than from the defaults would show."""
    cfg = make_config(
        observed_agent_name="",
        metrics_gcs_bucket="metrics-bucket",
        telemetry_dataset="env_dataset",
        data_lookback_window=14,
    )
    monkeypatch.setattr(commands, "env_config", cfg)
    monkeypatch.setattr(effective_config, "env_config", cfg)
    effective_config.clear_cache()
    return cfg


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch, env: Config) -> FakeGcsClient:
    client = FakeGcsClient()
    jobs = GcsObjectStore(BUCKET, client_factory=lambda: client)
    monkeypatch.setattr(objects, "jobs_store_factory", lambda: jobs)
    return client


def _attach(
    agent_name: str = WATCHED, dry_run: bool = False, **fields: Any
) -> dict[str, Any]:
    """Runs the attach tool once.

    Args:
        agent_name: Agent to attach.
        dry_run: Whether to plan without storing.
        **fields: Fields to change.

    Returns:
        The tool's response.
    """
    return asyncio.run(
        commands.attach_agent(
            agent_name=agent_name, fields=fields, dry_run=dry_run
        )
    )


def _store_unkeyed(name: str, **agent: Any) -> None:
    """Stores an attachment under the agent's bare name.

    Args:
        name: Agent name.
        **agent: Fields of the object's `agent` section.
    """
    objects.jobs_store_factory().write_text(
        store.build_object_name(name),
        json.dumps({"schema_version": 1, "agent": agent}),
    )


def _get_stored_record(name: str = WATCHED) -> store.AgentRecord | None:
    """Reads an agent's object back through the reader investigations use.

    Args:
        name: Agent name, or its key.

    Returns:
        The stored record, or None if the agent is not attached.
    """
    return store.get_agent_record(objects.jobs_store_factory(), name)


def _extract_changes(result: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Extracts the plan's changes as a mapping.

    Args:
        result: Attach tool response.

    Returns:
        `(before, after)` pairs keyed by field name.
    """
    return {c["field"]: (c["before"], c["after"]) for c in result["changes"]}


# --- a first attach ---------------------------------------------------------- #


def test_a_first_attach_starts_from_the_defaults(
    bucket: FakeGcsClient,
) -> None:
    """Setting one flag changes one field; the rest are the defaults, not the
    deployment's own configuration."""
    result = _attach(data_lookback_window=30)

    assert "error" not in result, result
    assert _extract_changes(result) == {"data_lookback_window": (7, 30)}
    record = _get_stored_record()
    assert record is not None
    assert record.config.data_lookback_window == 30
    assert record.config.telemetry_dataset == "agent_analytics"


def test_a_first_attach_with_no_flags_stores_the_defaults(
    bucket: FakeGcsClient,
) -> None:
    """Nothing changes about how the agent is watched, but the object is
    written: storing it is what attaches the agent."""
    result = _attach()

    assert result["changes"] == []
    assert result["written"] is True
    record = _get_stored_record()
    assert record is not None
    assert record.config.telemetry_dataset == "agent_analytics"
    assert record.config.data_lookback_window == 7


def test_the_plan_says_each_trigger_investigates_its_own_agent(
    bucket: FakeGcsClient,
) -> None:
    """The CLI wires the update trigger of an agent besides the default only
    then."""
    assert _attach("other_agent", dry_run=True)["triggers_name_agent"] is True


# --- re-attaching ------------------------------------------------------------- #


def test_re_attaching_keeps_what_the_request_does_not_mention(
    bucket: FakeGcsClient,
) -> None:
    _attach(data_lookback_window=30)

    result = _attach(data_evaluation_cap=250)

    assert _extract_changes(result) == {"data_evaluation_cap": (1000, 250)}
    record = _get_stored_record()
    assert record is not None
    assert record.config.data_lookback_window == 30
    assert record.config.data_evaluation_cap == 250


def test_re_attaching_the_same_thing_writes_nothing(
    bucket: FakeGcsClient,
) -> None:
    _attach(data_lookback_window=30)
    uploads = len(bucket.uploads)

    result = _attach(data_lookback_window=30)

    assert result["changes"] == []
    assert result["written"] is False
    assert len(bucket.uploads) == uploads


def test_re_attaching_keeps_what_the_last_apply_recorded(
    bucket: FakeGcsClient,
) -> None:
    """`applied` is written once the trigger is wired, not by the request, so
    an attach request carries it forward."""
    applied = store.Applied(at="2026-09-25T09:00:00Z", observed_engine_id="42")
    store.save_agent_record(
        objects.jobs_store_factory(),
        store.AgentRecord(
            config=ObservedAgentConfig(
                observed_agent_name=WATCHED, agent_key=WATCHED_KEY
            ),
            applied=applied,
        ),
    )

    _attach(data_lookback_window=30)

    record = _get_stored_record()
    assert record is not None
    assert record.applied == applied


def test_a_dry_run_plans_and_writes_nothing(bucket: FakeGcsClient) -> None:
    result = _attach(dry_run=True, data_lookback_window=30)

    assert _extract_changes(result) == {"data_lookback_window": (7, 30)}
    assert result["written"] is False
    assert bucket.uploads == []


# --- the access check -------------------------------------------------------------- #


@pytest.fixture
def checked(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[ObservedAgentConfig, str, str]]:
    """Replaces the access check with one that records what it was asked about."""
    calls: list[tuple[ObservedAgentConfig, str, str]] = []

    def fake_check(
        agent: ObservedAgentConfig, *, project_id: str, payload_bucket: str
    ) -> dict[str, Any]:
        calls.append((agent, project_id, payload_bucket))
        return {"service_account": "aqua@sa", "checks": []}

    monkeypatch.setattr(commands.access_check, "check_access", fake_check)
    return calls


def test_a_dry_run_that_asks_checks_the_fields_about_to_be_stored(
    bucket: FakeGcsClient, checked: list
) -> None:
    """Not the stored ones: the check answers for the attachment being planned."""
    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={"telemetry_dataset": "new_dataset"},
            dry_run=True,
            check_access=True,
        )
    )

    assert result["access"] == {"service_account": "aqua@sa", "checks": []}
    [(agent, project_id, payload_bucket)] = checked
    assert agent.telemetry_dataset == "new_dataset"
    assert agent.data_lookback_window == 7
    assert project_id == "test-project"
    assert payload_bucket == ""
    assert bucket.uploads == []


@pytest.mark.parametrize(
    "value", ["gs://agent-logs", "agent-logs/completions", "Agent_Logs", "ab"]
)
def test_a_payload_bucket_that_is_not_a_bucket_name_is_refused(
    bucket: FakeGcsClient, value: str
) -> None:
    """It goes into a request path and a grant command as it is."""
    result = _attach(dry_run=True, telemetry_payload_bucket=value)

    assert "must be a bucket name" in result["error"]
    assert bucket.uploads == []


def test_a_dry_run_reports_a_table_in_another_format_as_json(
    bucket: FakeGcsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real check, with only its BigQuery reads faked: the schema result
    and the source the table fits reach the plan as plain JSON."""
    sink_columns = schema.load_required_schema(
        "gen_ai_client_inference_operation_details.schema.json"
    )
    counted: list[tuple[Any, ...]] = []

    def count_scanned(*args: Any) -> int:
        counted.append(args)
        return 3

    fetcher = types.SimpleNamespace(
        load_table=lambda: types.SimpleNamespace(schema=list(sink_columns)),
        count_scanned=count_scanned,
    )
    monkeypatch.setattr(
        access_check,
        "_create_default_fetcher",
        lambda _agent, _project: fetcher,
    )
    monkeypatch.setattr(access_check, "resolve_service_account", lambda: "sa")

    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={"telemetry_ingestion_source": "big_query"},
            dry_run=True,
            check_access=True,
        )
    )

    checks = json.loads(json.dumps(result["access"]))["checks"]
    assert [(c["name"], c["status"]) for c in checks] == [
        ("table", "passed"),
        ("schema", "mismatch"),
        ("rows", "skipped"),
        ("payload", "skipped"),
    ]
    assert checks[1]["matching_sources"] == ["cloud_logging"]
    assert counted == []
    assert bucket.uploads == []


def test_a_dry_run_that_asks_fills_in_where_the_table_is(
    bucket: FakeGcsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real read and check, with only BigQuery faked: the location comes
    from the table, the check reads there, and the plan says why, as JSON."""
    built: list[ObservedAgentConfig] = []
    fetcher = types.SimpleNamespace(
        load_table=lambda: types.SimpleNamespace(
            schema=load_schema_fixture("agent_events.json"), location="EU"
        ),
        count_scanned=lambda *_args: 3,
    )

    def build_fetcher(agent: ObservedAgentConfig, _project: str) -> Any:
        built.append(agent)
        return fetcher

    monkeypatch.setattr(access_check, "_create_default_fetcher", build_fetcher)
    monkeypatch.setattr(access_check, "resolve_service_account", lambda: "sa")

    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={
                "telemetry_dataset": "analytics",
                "telemetry_table": "events",
            },
            dry_run=True,
            check_access=True,
            fill_from_table=True,
        )
    )

    assert json.loads(json.dumps(result["filled"])) == [
        {
            "field": "telemetry_location",
            "value": "EU",
            "reason": "the location of test-project.analytics.events",
        }
    ]
    assert _extract_changes(result)["telemetry_location"] == (
        "us-central1",
        "EU",
    )
    # The first fetcher reads the table's settings, the second checks them.
    assert [agent.telemetry_location for agent in built] == [
        "us-central1",
        "EU",
    ]
    assert [(c["name"], c["status"]) for c in result["access"]["checks"]] == [
        ("table", "passed"),
        ("schema", "passed"),
        ("rows", "passed"),
        ("payload", "skipped"),
    ]
    assert bucket.uploads == []


@pytest.fixture
def table_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> list[ObservedAgentConfig]:
    """Makes every table decide another source and location.

    Returns:
        The agents whose table was read, in order.
    """
    read: list[ObservedAgentConfig] = []

    def fake_read(
        agent: ObservedAgentConfig, *, project_id: str
    ) -> list[access_check.TableSetting]:
        read.append(agent)
        return [
            access_check.TableSetting(
                "telemetry_ingestion_source", "cloud_ops", "the only source"
            ),
            access_check.TableSetting(
                "telemetry_location", "EU", "its location"
            ),
        ]

    monkeypatch.setattr(commands.access_check, "read_table_settings", fake_read)
    return read


def test_the_table_decides_nothing_the_request_sets(
    bucket: FakeGcsClient, checked: list, table_settings: list
) -> None:
    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={"telemetry_location": "us-east1"},
            dry_run=True,
            check_access=True,
            fill_from_table=True,
        )
    )

    assert [f["field"] for f in result["filled"]] == [
        "telemetry_ingestion_source"
    ]
    [(agent, _, _)] = checked
    assert agent.telemetry_ingestion_source == "cloud_ops"
    assert agent.telemetry_location == "us-east1"


@pytest.mark.parametrize(
    ("check_access", "fill_from_table"), [(True, False), (False, True)]
)
def test_a_dry_run_fills_in_nothing_unless_it_checks_and_asks(
    bucket: FakeGcsClient,
    checked: list,
    table_settings: list,
    check_access: bool,
    fill_from_table: bool,
) -> None:
    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={},
            dry_run=True,
            check_access=check_access,
            fill_from_table=fill_from_table,
        )
    )

    assert result["filled"] == []
    assert table_settings == []


def test_a_write_reads_nothing_from_the_table(
    bucket: FakeGcsClient, checked: list, table_settings: list
) -> None:
    """The caller sends what its dry run filled in, so storing does not wait
    on BigQuery."""
    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={},
            dry_run=False,
            check_access=True,
            fill_from_table=True,
        )
    )

    assert result["written"] is True
    assert result["filled"] == []
    assert table_settings == []


def test_the_check_tries_the_payload_bucket_about_to_be_stored(
    bucket: FakeGcsClient, checked: list
) -> None:
    """So a first attach, before any traffic, can grant the bucket."""
    asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED,
            fields={"telemetry_payload_bucket": "agent-logs"},
            dry_run=True,
            check_access=True,
        )
    )

    [(_, _, payload_bucket)] = checked
    assert payload_bucket == "agent-logs"


def test_the_check_runs_only_when_asked(
    bucket: FakeGcsClient, checked: list
) -> None:
    result = _attach(dry_run=True, data_lookback_window=30)

    assert "access" not in result
    assert checked == []


def test_a_write_never_runs_the_check(
    bucket: FakeGcsClient, checked: list
) -> None:
    """The caller checked in the dry run it confirmed; storing does not wait on
    BigQuery a second time."""
    result = asyncio.run(
        commands.attach_agent(
            agent_name=WATCHED, fields={}, dry_run=False, check_access=True
        )
    )

    assert result["written"] is True
    assert checked == []


# --- validation ----------------------------------------------------------------- #


def test_a_value_the_runtime_refuses_is_never_stored(
    bucket: FakeGcsClient,
) -> None:
    result = _attach(quality_analysis_mode="vibes")

    assert "quality_analysis_mode" in result["error"]
    assert bucket.uploads == []


def test_a_value_the_runtime_clamps_is_stored_clamped_and_reported(
    bucket: FakeGcsClient,
) -> None:
    """Stored as the runtime will use it, and the caller is told."""
    result = _attach(data_evaluation_cap=9999, data_lookback_window=0)

    record = _get_stored_record()
    assert record is not None
    assert record.config.data_evaluation_cap == 1000
    assert record.config.data_lookback_window == 1
    assert result["adjusted"] == [
        {"field": "data_evaluation_cap", "requested": 9999, "stored": 1000},
        {"field": "data_lookback_window", "requested": 0, "stored": 1},
    ]


def test_a_long_lookback_window_is_stored_unchanged(
    bucket: FakeGcsClient,
) -> None:
    result = _attach(data_lookback_window=365)

    record = _get_stored_record()
    assert record is not None
    assert record.config.data_lookback_window == 365
    assert result["adjusted"] == []


def test_a_lookback_window_above_the_max_is_stored_clamped_and_reported(
    bucket: FakeGcsClient,
) -> None:
    result = _attach(data_lookback_window=999999)

    record = _get_stored_record()
    assert record is not None
    assert record.config.data_lookback_window == 36500
    assert result["adjusted"] == [
        {"field": "data_lookback_window", "requested": 999999, "stored": 36500}
    ]


@pytest.mark.parametrize(
    ("field", "known"),
    [
        ("multi_turn_metrics", "task_success"),
        ("single_turn_metrics", "hallucination"),
    ],
)
def test_an_unknown_metric_is_refused_and_the_known_ones_are_named(
    bucket: FakeGcsClient, field: str, known: str
) -> None:
    """A published code metric is not one of these, and dropping it would
    store an empty set the caller never asked for."""
    metrics: dict[str, Any] = {field: [known, "create_ticket_error"]}
    result = _attach(**metrics)

    assert "create_ticket_error" in result["error"]
    assert known in result["error"]
    assert "published" in result["error"]
    assert bucket.uploads == []


def test_an_unknown_field_is_refused_and_the_settable_ones_are_named(
    bucket: FakeGcsClient,
) -> None:
    result = _attach(lookback=30)

    assert "lookback" in result["error"]
    assert "data_lookback_window" in result["error"]
    assert bucket.uploads == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_lookback_window", "30"),
        ("data_lookback_window", True),
        ("multi_turn_metrics", "task_success"),
        ("default", "yes"),
        ("insights_verification_enabled", 1),
    ],
)
def test_a_value_of_the_wrong_type_is_refused(
    bucket: FakeGcsClient, field: str, value: Any
) -> None:
    """The runtime's checks assume the types are right: a string where a list
    belongs would be iterated a character at a time."""
    result = _attach(**{field: value})

    assert field in result["error"]
    assert bucket.uploads == []


def test_a_name_with_a_slash_is_refused(bucket: FakeGcsClient) -> None:
    """The object key substitutes the slash, which would attach the agent under
    a name nobody asked for."""
    result = _attach("team/agent")

    assert "'/'" in result["error"]
    assert bucket.uploads == []


def test_a_deployment_without_a_jobs_bucket_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        objects, "jobs_store_factory", lambda: GcsObjectStore("")
    )

    assert "no jobs bucket" in _attach()["error"]


# --- what the update trigger acts on ---------------------------------------------------- #


def test_setting_an_engine_is_reported_as_needing_the_trigger(
    bucket: FakeGcsClient,
) -> None:
    result = _attach(observed_engine_id="42")

    assert result["needs_apply"] == ["observed_engine_id"]


def test_setting_a_schedule_needs_no_trigger_change(
    bucket: FakeGcsClient,
) -> None:
    """The engine reads the schedule on every tick."""
    result = _attach(
        investigation_schedule="0 6 * * *", scheduled_trigger_enabled=False
    )

    assert result["needs_apply"] == []


def test_a_first_attach_stores_the_default_schedule(
    bucket: FakeGcsClient,
) -> None:
    """With no `--schedule`, the object still says when the agent is
    investigated."""
    _attach()

    record = _get_stored_record()
    assert record is not None
    assert record.investigation_schedule == store.DEFAULT_INVESTIGATION_SCHEDULE
    assert record.scheduled_trigger_enabled is True
    stored = json.loads(
        objects.jobs_store_factory().read_text(store.build_object_name(WATCHED))
    )
    assert (
        stored["agent"]["investigation_schedule"]
        == store.DEFAULT_INVESTIGATION_SCHEDULE
    )


def test_the_schedule_given_is_stored(bucket: FakeGcsClient) -> None:
    _attach(investigation_schedule="0 6 * * 1", scheduled_trigger_enabled=False)

    record = _get_stored_record()
    assert record is not None
    assert record.investigation_schedule == "0 6 * * 1"
    assert record.scheduled_trigger_enabled is False


@pytest.mark.parametrize(
    "schedule",
    [
        "",
        "  ",
        "every day",
        "0 12 * *",
        "0 0 12 * * *",
        "@daily",
        "R 12 * * *",
        "0 0 31 2 *",
    ],
)
def test_a_schedule_the_engine_cannot_run_is_refused(
    bucket: FakeGcsClient, schedule: str
) -> None:
    """Stored, it would fail every tick, and nothing would say so at attach."""
    result = _attach(investigation_schedule=schedule)

    assert "investigation_schedule must be a five-field cron" in result["error"]
    assert _get_stored_record() is None


def test_an_attachment_stored_with_no_schedule_has_the_default(
    bucket: FakeGcsClient,
) -> None:
    """Re-attaching changes nothing: the empty value meant the default."""
    _store_unkeyed(
        WATCHED, observed_agent_name=WATCHED, investigation_schedule=""
    )

    result = _attach(dry_run=True)

    assert result["changes"] == []


def test_a_runtime_field_needs_no_trigger_change(bucket: FakeGcsClient) -> None:
    """The point of the split: how much telemetry is read takes effect on the
    next investigation, with no infrastructure step."""
    assert _attach(data_lookback_window=30)["needs_apply"] == []


_TOPIC = "projects/test-project/topics/aqua-solo-aqua-ambient"


def _report_trigger_target(
    monkeypatch: pytest.MonkeyPatch, env: Config
) -> None:
    """Makes the deployment one that hands its update triggers to `attach`.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        env: The deployment's configuration.
    """
    monkeypatch.setattr(
        commands,
        "env_config",
        dataclasses.replace(
            env,
            aqa_engine_id="7",
            ambient_topic=_TOPIC,
            # Config requires the caller and the queue together.
            delay_task_queue="projects/test-project/locations/us-central1/queues/q",
            ambient_caller_sa="caller@test-project.iam.gserviceaccount.com",
        ),
    )


def test_the_plan_carries_the_values_the_trigger_is_wired_with_and_where(
    bucket: FakeGcsClient, env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merged values, including those the request left alone, and the engine
    as it knows itself, so the CLI needs nothing but its resource name."""
    _report_trigger_target(monkeypatch, env)
    _attach(observed_engine_id="42", observed_project_id="agent-project")

    result = asyncio.run(
        commands.attach_agent(
            agent_name="",
            agent_key="agent-project__watched_agent",
            fields={"investigation_schedule": "0 6 * * *"},
            dry_run=True,
        )
    )

    assert result["triggers"] == {
        "observed_engine_id": "42",
        "observed_project_id": "agent-project",
    }
    # No trigger was ever wired for it.
    assert result["unapplied_fields"] == list(store.TRIGGER_FIELDS)
    assert result["trigger_target"] == {
        "region": "us-central1",
        "aqua_engine": "projects/test-project/locations/us-central1/reasoningEngines/7",
        "ambient_topic": _TOPIC,
        "ambient_caller_email": "caller@test-project.iam.gserviceaccount.com",
    }


def test_a_deployment_with_no_ambient_topic_reports_no_target(
    bucket: FakeGcsClient,
) -> None:
    """With no topic, as in a local run, there is nothing for `attach` to wire
    an update trigger to."""
    assert _attach(dry_run=True)["trigger_target"] == {}
    assert asyncio.run(commands.list_agents())["trigger_target"] == {}


def _record_applied(name: str = WATCHED_KEY, **applied: Any) -> dict[str, Any]:
    """Runs the record-applied tool once.

    Args:
        name: Agent key.
        **applied: Applied values, keyed by field name.

    Returns:
        The tool's response.
    """
    return asyncio.run(commands.record_applied(agent_key=name, applied=applied))


_APPLIED: dict[str, Any] = {
    "observed_engine_id": "42",
    # AQuA's own project, so that a later attach naming no project changes
    # the same attachment.
    "observed_project_id": "test-project",
}


def test_recording_an_apply_puts_those_values_into_effect(
    bucket: FakeGcsClient,
) -> None:
    _attach(**_APPLIED)

    result = _record_applied(**_APPLIED)

    assert result["unapplied_fields"] == []
    record = _get_stored_record()
    assert record is not None and record.applied is not None
    assert record.applied.observed_engine_id == "42"
    assert record.applied.at == result["applied_at"]
    assert record.list_unapplied_fields() == []


def test_an_attach_during_the_apply_stays_not_in_effect(
    bucket: FakeGcsClient,
) -> None:
    """The record says what the update trigger was wired with, not what is
    stored now."""
    without_engine = {**_APPLIED, "observed_engine_id": ""}
    _attach(**without_engine)
    _attach(observed_engine_id="42")

    result = _record_applied(**without_engine)

    assert result["unapplied_fields"] == ["observed_engine_id"]


def test_recording_an_apply_for_an_unattached_agent_is_refused(
    bucket: FakeGcsClient,
) -> None:
    assert "not attached" in _record_applied(**_APPLIED)["error"]
    assert bucket.uploads == []


@pytest.mark.parametrize(
    "applied",
    [
        {"observed_engine_id": "42"},
        {**_APPLIED, "data_lookback_window": 30},
        {**_APPLIED, "investigation_schedule": "0 6 * * *"},
        {**_APPLIED, "observed_engine_id": 42},
    ],
)
def test_recording_an_incomplete_or_mistyped_apply_is_refused(
    bucket: FakeGcsClient, applied: dict[str, Any]
) -> None:
    _attach(**_APPLIED)
    uploads = len(bucket.uploads)

    assert "error" in _record_applied(**applied)
    assert len(bucket.uploads) == uploads


# --- grants recorded for `detach` to revoke --------------------------------------------- #

_MEMBER = "serviceAccount:aqua@proj.iam.gserviceaccount.com"
_DATASET_GRANT = {
    "resource": "proj:agent_telemetry",
    "role": "roles/bigquery.dataViewer",
    "member": _MEMBER,
}
_BUCKET_GRANT = {
    "resource": "gs://proj-agent-logs",
    "role": "roles/storage.objectViewer",
    "member": _MEMBER,
}


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch) -> str:
    """Makes `_MEMBER`'s account the one this engine runs as."""
    email = _MEMBER.removeprefix("serviceAccount:")
    monkeypatch.setattr(
        commands.access_check, "resolve_service_account", lambda: email
    )
    return email


def _record_grants(
    *,
    applied: dict[str, Any] | None = None,
    granted: list[Any] | None = None,
    revoked: list[Any] | None = None,
) -> dict[str, Any]:
    """Runs the record-applied tool with grants.

    Args:
        applied: Trigger values, or None.
        granted: Grants to add, or None.
        revoked: Grants to remove, or None.

    Returns:
        The tool's response.
    """
    return asyncio.run(
        commands.record_applied(
            agent_key=WATCHED_KEY,
            applied=applied,
            granted=granted,
            revoked=revoked,
        )
    )


def test_grants_are_recorded_without_putting_any_trigger_into_effect(
    bucket: FakeGcsClient, account: str
) -> None:
    """Grants run before the update trigger, whose commands may then fail."""
    _attach(**_APPLIED)

    result = _record_grants(granted=[_DATASET_GRANT])

    assert result["grants"] == [_DATASET_GRANT]
    assert result["applied_at"] == ""
    assert result["unapplied_fields"] == list(store.TRIGGER_FIELDS)


def test_grants_accumulate_once_each_and_a_trigger_apply_keeps_them(
    bucket: FakeGcsClient, account: str
) -> None:
    _attach(**_APPLIED)
    _record_grants(granted=[_DATASET_GRANT])
    _record_grants(granted=[_DATASET_GRANT, _BUCKET_GRANT])

    result = _record_applied(**_APPLIED)

    assert result["grants"] == [_DATASET_GRANT, _BUCKET_GRANT]
    assert result["unapplied_fields"] == []


def test_a_revoked_grant_is_forgotten_and_the_others_kept(
    bucket: FakeGcsClient, account: str
) -> None:
    _attach(**_APPLIED)
    _record_grants(applied=_APPLIED, granted=[_DATASET_GRANT, _BUCKET_GRANT])

    result = _record_grants(revoked=[_DATASET_GRANT])

    assert result["grants"] == [_BUCKET_GRANT]
    record = _get_stored_record()
    assert record is not None and record.applied is not None
    assert record.list_unapplied_fields() == []


def test_a_plain_attach_keeps_the_recorded_grants(
    bucket: FakeGcsClient, account: str
) -> None:
    _attach(**_APPLIED)
    _record_grants(granted=[_DATASET_GRANT])

    _attach(data_lookback_window=30)

    listing = asyncio.run(commands.list_agents())
    assert listing["agents"][0]["grants"] == [_DATASET_GRANT]


@pytest.mark.parametrize(
    "request_fields",
    [
        {},
        {"granted": [{"resource": "gs://b", "role": "r"}]},
        {"granted": [{**_DATASET_GRANT, "member": ""}]},
        {"revoked": [{**_DATASET_GRANT, "role": 7}]},
        {"granted": ["proj:agent_telemetry"]},
        {"revoked": [{**_DATASET_GRANT, "extra": "x"}]},
    ],
)
def test_a_record_with_nothing_or_a_malformed_grant_is_refused(
    bucket: FakeGcsClient, request_fields: dict[str, Any]
) -> None:
    _attach(**_APPLIED)
    uploads = len(bucket.uploads)

    assert "error" in _record_grants(**request_fields)
    assert len(bucket.uploads) == uploads


def test_a_grant_to_anybody_but_this_engine_is_refused(
    bucket: FakeGcsClient, account: str
) -> None:
    """`detach` revokes what is recorded with an operator's
    credentials, so the record must not name somebody else's binding."""
    _attach(**_APPLIED)
    uploads = len(bucket.uploads)
    other = {
        **_DATASET_GRANT,
        "member": "serviceAccount:prod@x.iam.gserviceaccount.com",
    }

    result = _record_grants(granted=[_DATASET_GRANT, other])

    assert account in result["error"]
    assert len(bucket.uploads) == uploads


def test_no_grant_is_recorded_when_the_engine_cannot_name_its_account(
    bucket: FakeGcsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        commands.access_check, "resolve_service_account", lambda: ""
    )
    _attach(**_APPLIED)

    assert "error" in _record_grants(granted=[_DATASET_GRANT])


def test_a_revocation_is_recorded_whatever_account_the_engine_has(
    bucket: FakeGcsClient, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forgetting a grant only narrows what a later detach revokes."""
    _attach(**_APPLIED)
    _record_grants(granted=[_DATASET_GRANT])
    monkeypatch.setattr(
        commands.access_check, "resolve_service_account", lambda: ""
    )

    assert _record_grants(revoked=[_DATASET_GRANT])["grants"] == []


# --- what the runtime then reads ------------------------------------------------------ #


def test_an_attach_reaches_the_next_config_read_at_once(
    bucket: FakeGcsClient,
) -> None:
    """The route and the reader share one process's cache, and the write drops
    the entry, so `/config` shows the new value at once."""
    _attach(data_lookback_window=30)
    assert effective_config.load({}).data_lookback_window == 30

    _attach(data_lookback_window=21)

    assert effective_config.load({}).data_lookback_window == 21


def test_what_attach_stores_reads_back_without_a_warning(
    bucket: FakeGcsClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Every key written is one the reader knows. A key it did not would be
    dropped with a warning, and the setting would silently not apply."""
    _attach(investigation_schedule="0 12 * * *", default=True)

    with caplog.at_level(logging.WARNING):
        record = _get_stored_record()

    assert record is not None and record.default is True
    assert "does not know" not in caplog.text


def test_the_stored_object_is_the_envelope_the_reader_expects(
    bucket: FakeGcsClient,
) -> None:
    _attach()

    raw = bucket.contents[BUCKET, store.build_object_name(WATCHED_KEY)]
    payload = json.loads(raw)
    assert payload["schema_version"] == store.SCHEMA_VERSION
    assert payload["agent"]["observed_agent_name"] == WATCHED


# --- list and detach -------------------------------------------------------------------- #


def test_listing_names_every_agent_and_the_default(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent_a")
    _attach("agent_b", default=True)

    listing = asyncio.run(commands.list_agents())

    assert [a["agent_name"] for a in listing["agents"]] == [
        "agent_a",
        "agent_b",
    ]
    assert [a["agent_key"] for a in listing["agents"]] == [
        "agent_a",
        "agent_b",
    ]
    assert listing["default_agent"] == "agent_b"


def test_listing_says_when_each_agent_is_investigated(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent_a")
    _attach("agent_b", investigation_schedule="0 6 * * *")
    _attach("agent_c", scheduled_trigger_enabled=False)

    agents = {
        a["agent_name"]: a
        for a in asyncio.run(commands.list_agents())["agents"]
    }

    assert {
        name: (a["investigation_schedule"], a["scheduled_trigger_enabled"])
        for name, a in agents.items()
    } == {
        "agent_a": (store.DEFAULT_INVESTIGATION_SCHEDULE, True),
        "agent_b": ("0 6 * * *", True),
        "agent_c": (store.DEFAULT_INVESTIGATION_SCHEDULE, False),
    }
    assert "investigation_schedule" not in agents["agent_b"]["unapplied_fields"]


def test_listing_says_where_each_agents_update_sink_is(
    bucket: FakeGcsClient,
) -> None:
    """`detach` finds the sink in the agent's project, which only the
    attachment records for an agent stored under its bare name."""
    _attach(observed_engine_id="42", observed_project_id="agent-project")

    (entry,) = asyncio.run(commands.list_agents())["agents"]

    assert entry["observed_engine_id"] == "42"
    assert entry["observed_project_id"] == "agent-project"


def test_with_no_default_set_the_first_alphabetically_answers(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent_b")
    _attach("agent_a")

    assert asyncio.run(commands.list_agents())["default_agent"] == "agent_a"


def test_an_unreadable_object_is_listed_with_the_reason(
    bucket: FakeGcsClient,
) -> None:
    """Left out, it would read as never attached."""
    bucket.contents[BUCKET, store.build_object_name("broken")] = b"not json"
    bucket.objects.add((BUCKET, store.build_object_name("broken")))

    listing = asyncio.run(commands.list_agents())

    assert listing["agents"][0]["agent_key"] == "broken"
    assert "error" in listing["agents"][0]
    assert listing["default_agent"] is None


def test_detaching_the_default_hands_it_to_the_next_agent_at_once(
    bucket: FakeGcsClient,
) -> None:
    """The write drops the cached default, so the next read sees the change."""
    _attach("agent_a")
    _attach("agent_b")
    assert effective_config.load({}).observed_agent_name == "agent_a"

    result = asyncio.run(commands.detach_agent(agent_key="agent_a"))

    assert result == {"agent_key": "agent_a", "detached": True}
    assert _get_stored_record("agent_a") is None
    assert effective_config.load({}).observed_agent_name == "agent_b"


def test_detaching_what_is_not_attached_is_not_an_error(
    bucket: FakeGcsClient,
) -> None:
    result = asyncio.run(commands.detach_agent(agent_key="nobody"))

    assert result == {"agent_key": "nobody", "detached": False}


# --- which agent is watched ------------------------------------------------------ #


def test_the_first_agent_attached_is_the_one_watched(
    bucket: FakeGcsClient,
) -> None:
    assert _attach("agent_a", dry_run=True)["watched"] is True


def test_once_one_is_attached_another_is_not_watched(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent_a")

    assert _attach("agent_b", dry_run=True)["watched"] is False
    assert effective_config.load({}).observed_agent_name == "agent_a"


def test_another_agent_attached_as_the_default_is_watched(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent_a")

    assert _attach("agent_b", default=True, dry_run=True)["watched"] is True
    assert _attach("agent_b", default=True)["watched"] is True
    assert effective_config.load({}).observed_agent_name == "agent_b"


def test_listing_marks_the_default_as_watched(bucket: FakeGcsClient) -> None:
    _attach("agent_a")
    _attach("agent_b", default=True)

    listing = asyncio.run(commands.list_agents())

    assert {a["agent_name"]: a["watched"] for a in listing["agents"]} == {
        "agent_a": False,
        "agent_b": True,
    }


def test_attaching_without_a_name_is_refused(bucket: FakeGcsClient) -> None:
    """No agent is watched until one is attached by name."""
    assert "Name the agent to attach" in _attach("")["error"]
    assert bucket.uploads == []


# --- the agent key ---------------------------------------------------------------------- #


def test_the_key_is_the_agents_project_and_name(bucket: FakeGcsClient) -> None:
    """Staging and prod share the agent's name; their projects tell them apart,
    so each is its own attachment."""
    staging = _attach("it_support_agent", observed_project_id="acme-staging")
    prod = _attach("it_support_agent", observed_project_id="acme-prod")

    assert staging["agent_key"] == "acme-staging__it_support_agent"
    assert prod["agent_key"] == "acme-prod__it_support_agent"
    assert staging["object"].endswith(
        "agents/acme-staging__it_support_agent.json"
    )
    assert _get_stored_record("acme-staging__it_support_agent") is not None
    assert _get_stored_record("acme-prod__it_support_agent") is not None


def test_aquas_project_by_number_or_by_id_is_aquas_project(
    bucket: FakeGcsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Agent Runtime AQuA's project arrives as a number; the user types the
    id. Either names AQuA's own project."""
    cfg = make_config(
        observed_agent_name="",
        project_id="123456789012",
        delay_task_queue="projects/acme-aqua/locations/us-central1/queues/q",
        ambient_caller_sa="caller@acme-aqua.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(commands, "env_config", cfg)
    monkeypatch.setattr(effective_config, "env_config", cfg)

    by_id = _attach(
        "it_support_agent", dry_run=True, observed_project_id="acme-aqua"
    )
    by_number = _attach(
        "it_support_agent", dry_run=True, observed_project_id="123456789012"
    )

    # Both are AQuA's own project, so the agent keeps its bare name.
    assert by_id["agent_key"] == "it_support_agent"
    assert by_number["agent_key"] == "it_support_agent"


def test_an_agent_in_aquas_project_keeps_its_bare_name(
    bucket: FakeGcsClient,
) -> None:
    """Its attachment and triggers stay under that name. An agent in another
    project gets its key, which keeps it apart from the same agent in AQuA's."""
    local = _attach("it_support_agent")
    remote = _attach("it_support_agent", observed_project_id="acme-staging")

    assert local["agent_key"] == "it_support_agent"
    assert remote["agent_key"] == "acme-staging__it_support_agent"


def test_an_attach_merges_over_the_attachment_stored_by_bare_name(
    bucket: FakeGcsClient,
) -> None:
    """Rather than starting a second attachment from the defaults."""
    _store_unkeyed(WATCHED, data_lookback_window=30)

    result = _attach(single_turn_metrics=["safety"])

    assert result["agent_key"] == WATCHED
    assert result["attached"] is True
    assert store.list_agents(objects.jobs_store_factory()) == [WATCHED]
    record = _get_stored_record(WATCHED)
    assert record is not None and record.config.data_lookback_window == 30


def test_an_attachment_under_the_bare_name_for_its_project_is_kept(
    bucket: FakeGcsClient,
) -> None:
    """An agent in another project stored under its bare name stays there."""
    _store_unkeyed(WATCHED, observed_project_id="acme-staging")

    result = _attach(WATCHED, observed_project_id="acme-staging")

    assert result["agent_key"] == WATCHED
    assert result["attached"] is True


def test_a_bare_name_held_by_another_project_is_not_taken(
    bucket: FakeGcsClient,
) -> None:
    """The agent in AQuA's project gets its key rather than another project's
    attachment."""
    _store_unkeyed(WATCHED, observed_project_id="acme-staging")

    result = _attach(WATCHED, dry_run=True, observed_project_id="test-project")

    assert result["agent_key"] == KEYED
    assert result["attached"] is False


def test_an_attach_naming_no_project_changes_the_attachment_the_agent_has(
    bucket: FakeGcsClient,
) -> None:
    """Changing one setting must not start a second attachment, whichever
    project the existing one records."""
    _store_unkeyed(WATCHED, observed_project_id="acme-prod")

    result = _attach(data_lookback_window=3)

    assert result["agent_key"] == WATCHED
    assert result["attached"] is True
    assert result["watched"] is True
    assert store.list_agents(objects.jobs_store_factory()) == [WATCHED]


def test_a_bare_name_that_reads_as_a_key_is_keyed(
    bucket: FakeGcsClient,
) -> None:
    """`support__bot` stored bare would read as agent `bot` of project
    `support`, so it gets its key."""
    result = _attach("support__bot")

    assert result["agent_key"] == "test-project__support__bot"
    record = _get_stored_record("test-project__support__bot")
    assert record is not None and record.agent_name == "support__bot"


def test_an_attachment_that_cannot_be_read_is_reported(
    bucket: FakeGcsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any) -> None:
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(store, "get_agent_record", refuse)

    result = _attach("it_support_agent")

    assert "403 forbidden" in result["error"]
    assert "it_support_agent" in result["error"]


def test_a_key_that_is_not_well_formed_is_refused(
    bucket: FakeGcsClient,
) -> None:
    result = _attach("it-support-agent")

    assert "Cannot key an attachment" in result["error"]
    assert bucket.uploads == []


def test_an_attachment_is_not_re_pointed_at_another_engine(
    bucket: FakeGcsClient,
) -> None:
    """Re-pointing would hand one engine's history to another."""
    _attach("it_support_agent", observed_engine_id="42")
    uploads = len(bucket.uploads)

    result = _attach("it_support_agent", observed_engine_id="43")

    assert "refusing to re-point it at 43" in result["error"]
    assert len(bucket.uploads) == uploads


def test_an_attachment_named_by_its_key_is_changed(
    bucket: FakeGcsClient,
) -> None:
    _attach("it_support_agent", observed_project_id="acme-staging")

    result = asyncio.run(
        commands.attach_agent(
            agent_name="",
            agent_key="acme-staging__it_support_agent",
            fields={"data_lookback_window": 3},
            dry_run=False,
        )
    )

    assert result["written"] is True, result
    record = _get_stored_record("acme-staging__it_support_agent")
    assert record is not None
    assert record.config.data_lookback_window == 3
    assert record.config.observed_project_id == "acme-staging"


@pytest.mark.parametrize(
    ("agent_key", "fields", "error"),
    [
        ("acme-staging__nobody", {}, "No agent is attached"),
        (
            "acme-staging__it_support_agent",
            {"observed_project_id": "acme-prod"},
            "keeps its project",
        ),
    ],
)
def test_a_key_names_an_attachment_and_keeps_its_project(
    bucket: FakeGcsClient, agent_key: str, fields: dict[str, Any], error: str
) -> None:
    _attach("it_support_agent", observed_project_id="acme-staging")

    result = asyncio.run(
        commands.attach_agent(
            agent_name="", agent_key=agent_key, fields=fields, dry_run=False
        )
    )

    assert error in result["error"]
