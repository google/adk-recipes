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
import pathlib
import re
from typing import Any

import pytest
from ambient_quality_agent.config import Config, ObservedAgentConfig
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.observed_agent_config import (
    commands,
    effective_config,
    store,
)

from .conftest import FakeGcsClient, make_config

BUCKET = "jobs-bucket"
WATCHED = "watched-agent"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Config:
    """A deployment watching `WATCHED`, whose environment sets a few agent
    fields away from their defaults, so a first attach that fell back to the
    defaults would show."""
    cfg = make_config(
        observed_agent_name=WATCHED,
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
    agent_name: str = "", dry_run: bool = False, **fields: Any
) -> dict[str, Any]:
    """Runs the attach tool once.

    Args:
        agent_name: Agent to attach; empty means the watched agent.
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


def _get_stored_record(name: str = WATCHED) -> store.AgentRecord | None:
    """Reads an agent's object back through the reader investigations use.

    Args:
        name: Agent name.

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


def test_a_first_attach_starts_from_the_environment(
    bucket: FakeGcsClient,
) -> None:
    """Setting one flag changes one field, and the agent stays on the dataset
    the environment names."""
    result = _attach(data_lookback_window=30)

    assert "error" not in result, result
    assert _extract_changes(result) == {"data_lookback_window": (14, 30)}
    record = _get_stored_record()
    assert record is not None
    assert record.config.data_lookback_window == 30
    assert record.config.telemetry_dataset == "env_dataset"


def test_a_first_attach_with_no_flags_stores_what_the_environment_says(
    bucket: FakeGcsClient,
) -> None:
    """Nothing changes about how the agent is watched; the settings move from
    the environment into the object, which is the point of attaching."""
    result = _attach()

    assert result["changes"] == []
    assert result["written"] is True
    record = _get_stored_record()
    assert record is not None
    assert record.config.telemetry_dataset == "env_dataset"
    assert record.config.data_lookback_window == 14


def test_without_a_name_it_attaches_the_agent_the_deployment_watches(
    bucket: FakeGcsClient,
) -> None:
    result = _attach()

    assert result["agent_name"] == WATCHED
    assert result["watched"] is True


def test_another_agent_starts_from_the_defaults(bucket: FakeGcsClient) -> None:
    """The environment describes the agent it names, and no other."""
    result = _attach("other-agent")

    assert result["watched"] is False
    record = _get_stored_record("other-agent")
    assert record is not None
    assert record.config.telemetry_dataset == "agent_analytics"
    assert record.config.data_lookback_window == 7


# --- re-attaching ------------------------------------------------------------- #


def test_re_attaching_keeps_what_the_request_does_not_mention(
    bucket: FakeGcsClient,
) -> None:
    _attach(data_lookback_window=30)

    result = _attach(data_evaluation_cap=250)

    assert _extract_changes(result) == {"data_evaluation_cap": (100, 250)}
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
    """`applied` is written by `attach --apply`, not by the request, so a plain
    attach carries it forward."""
    applied = store.Applied(
        at="2026-09-25T09:00:00Z", investigation_schedule="0 6 * * *"
    )
    store.save_agent_record(
        objects.jobs_store_factory(),
        store.AgentRecord(
            config=ObservedAgentConfig(observed_agent_name=WATCHED),
            applied=applied,
        ),
    )

    _attach(data_lookback_window=30)

    record = _get_stored_record()
    assert record is not None
    assert record.applied == applied


def test_a_dry_run_plans_and_writes_nothing(bucket: FakeGcsClient) -> None:
    result = _attach(dry_run=True, data_lookback_window=30)

    assert _extract_changes(result) == {"data_lookback_window": (14, 30)}
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
            agent_name="",
            fields={"telemetry_dataset": "new_dataset"},
            dry_run=True,
            check_access=True,
        )
    )

    assert result["access"] == {"service_account": "aqua@sa", "checks": []}
    [(agent, project_id, payload_bucket)] = checked
    assert agent.telemetry_dataset == "new_dataset"
    assert agent.data_lookback_window == 14
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


def test_the_check_tries_the_payload_bucket_about_to_be_stored(
    bucket: FakeGcsClient, checked: list
) -> None:
    """So a first attach, before any traffic, can grant the bucket."""
    asyncio.run(
        commands.attach_agent(
            agent_name="",
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
            agent_name="", fields={}, dry_run=False, check_access=True
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
    result = _attach(data_lookback_window=9999)

    record = _get_stored_record()
    assert record is not None
    assert record.config.data_lookback_window == 64
    assert result["adjusted"] == [
        {"field": "data_lookback_window", "requested": 9999, "stored": 64}
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


# --- what needs Terraform ---------------------------------------------------------- #


def test_setting_a_schedule_is_reported_as_needing_terraform(
    bucket: FakeGcsClient,
) -> None:
    result = _attach(investigation_schedule="0 12 * * *")

    assert result["needs_apply"] == ["investigation_schedule"]


def test_a_runtime_field_never_needs_terraform(bucket: FakeGcsClient) -> None:
    """The point of the split: how much telemetry is read takes effect on the
    next investigation, with no infrastructure step."""
    assert _attach(data_lookback_window=30)["needs_apply"] == []


_TOPIC = "projects/test-project/topics/aqua-solo-aqua-ambient"


def _report_trigger_target(
    monkeypatch: pytest.MonkeyPatch, env: Config
) -> None:
    """Makes the deployment one that hands its triggers to `attach --apply`.

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


def test_the_plan_carries_the_values_terraform_applies_and_where(
    bucket: FakeGcsClient, env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merged values, including those the request left alone, and the engine
    as it knows itself, so the CLI needs nothing but its resource name."""
    _report_trigger_target(monkeypatch, env)
    _attach(observed_engine_id="42", observed_project_id="agent-project")

    result = _attach(dry_run=True, investigation_schedule="0 6 * * *")

    assert result["triggers"] == {
        "investigation_schedule": "0 6 * * *",
        "scheduled_trigger_enabled": True,
        "observed_engine_id": "42",
        "observed_project_id": "agent-project",
    }
    assert result["trigger_target"] == {
        "region": "us-central1",
        "aqua_engine": "projects/test-project/locations/us-central1/reasoningEngines/7",
        "ambient_topic": _TOPIC,
        "ambient_caller_email": "caller@test-project.iam.gserviceaccount.com",
    }


def test_a_deployment_running_its_own_triggers_reports_no_target(
    bucket: FakeGcsClient,
) -> None:
    """Its environment has no topic, so `attach --apply` cannot add a second
    scheduler for the same agent."""
    assert _attach(dry_run=True)["trigger_target"] == {}
    assert asyncio.run(commands.list_agents())["trigger_target"] == {}


def test_the_target_is_the_attach_roots_instance_and_what_it_records(
    env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI passes the target as Terraform variables, and destroys from the
    `instance` output; a key either side lacks fails the apply."""
    _report_trigger_target(monkeypatch, env)
    keys = set(commands.build_trigger_target())
    root = (
        pathlib.Path(__file__).resolve().parents[1]
        / "terraform/examples/attach"
    )

    declared = dict(
        re.findall(
            r'variable "(\w+)" \{(.*?)\n\}',
            (root / "variables.tf").read_text(),
            re.DOTALL,
        )
    )
    required = {n for n, body in declared.items() if "default" not in body}
    outputs = (root / "outputs.tf").read_text()
    instance = outputs[outputs.index('output "instance"') :]
    recorded = set(re.findall(r"^\s+(\w+)\s+= var\.", instance, re.MULTILINE))

    assert required == keys | {"observed_agent_name"}
    assert recorded == keys


def _record_applied(name: str = WATCHED, **applied: Any) -> dict[str, Any]:
    """Runs the record-applied tool once.

    Args:
        name: Agent name.
        **applied: Applied values, keyed by field name.

    Returns:
        The tool's response.
    """
    return asyncio.run(
        commands.record_applied(agent_name=name, applied=applied)
    )


_APPLIED: dict[str, Any] = {
    "investigation_schedule": "0 6 * * *",
    "scheduled_trigger_enabled": True,
    "observed_engine_id": "42",
    "observed_project_id": "agent-project",
}


def test_recording_an_apply_puts_those_values_into_effect(
    bucket: FakeGcsClient,
) -> None:
    _attach(**_APPLIED)

    result = _record_applied(**_APPLIED)

    assert result["unapplied_fields"] == []
    record = _get_stored_record()
    assert record is not None and record.applied is not None
    assert record.applied.investigation_schedule == "0 6 * * *"
    assert record.applied.at == result["applied_at"]
    assert record.list_unapplied_fields() == []


def test_an_attach_during_the_apply_stays_not_in_effect(
    bucket: FakeGcsClient,
) -> None:
    """The record says what Terraform was given, not what is stored now."""
    _attach(**_APPLIED)
    _attach(investigation_schedule="0 18 * * *")

    result = _record_applied(**_APPLIED)

    assert result["unapplied_fields"] == ["investigation_schedule"]


def test_recording_an_apply_for_an_unattached_agent_is_refused(
    bucket: FakeGcsClient,
) -> None:
    assert "not attached" in _record_applied(**_APPLIED)["error"]
    assert bucket.uploads == []


@pytest.mark.parametrize(
    "applied",
    [
        {"investigation_schedule": "0 6 * * *"},
        {**_APPLIED, "data_lookback_window": 30},
        {**_APPLIED, "scheduled_trigger_enabled": "true"},
    ],
)
def test_recording_an_incomplete_or_mistyped_apply_is_refused(
    bucket: FakeGcsClient, applied: dict[str, Any]
) -> None:
    _attach(**_APPLIED)
    uploads = len(bucket.uploads)

    assert "error" in _record_applied(**applied)
    assert len(bucket.uploads) == uploads


# --- grants recorded for `detach --apply` --------------------------------------------- #

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
            agent_name=WATCHED,
            applied=applied,
            granted=granted,
            revoked=revoked,
        )
    )


def test_grants_are_recorded_without_putting_any_trigger_into_effect(
    bucket: FakeGcsClient, account: str
) -> None:
    """Grants run before the triggers, and Terraform may then fail."""
    _attach(**_APPLIED)

    result = _record_grants(granted=[_DATASET_GRANT])

    assert result["grants"] == [_DATASET_GRANT]
    assert result["applied_at"] == ""
    assert result["unapplied_fields"] == list(store.TERRAFORM_BACKED_FIELDS)


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
    """`detach --apply` revokes what is recorded with an operator's
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
    assert effective_config.load({}).data_lookback_window == 14

    _attach(data_lookback_window=30)

    assert effective_config.load({}).data_lookback_window == 30


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

    raw = bucket.contents[BUCKET, store.build_object_name(WATCHED)]
    payload = json.loads(raw)
    assert payload["schema_version"] == store.SCHEMA_VERSION
    assert payload["agent"]["observed_agent_name"] == WATCHED


# --- list and detach -------------------------------------------------------------------- #


def test_listing_names_every_agent_and_the_default(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent-a")
    _attach("agent-b", default=True)

    listing = asyncio.run(commands.list_agents())

    assert [a["agent_name"] for a in listing["agents"]] == [
        "agent-a",
        "agent-b",
    ]
    assert listing["default_agent"] == "agent-b"
    assert listing["environment_agent"] == WATCHED


def test_with_no_default_set_the_first_alphabetically_answers(
    bucket: FakeGcsClient,
) -> None:
    _attach("agent-b")
    _attach("agent-a")

    assert asyncio.run(commands.list_agents())["default_agent"] == "agent-a"


def test_an_unreadable_object_is_listed_with_the_reason(
    bucket: FakeGcsClient,
) -> None:
    """Left out, it would read as never attached."""
    bucket.contents[BUCKET, store.build_object_name("broken")] = b"not json"
    bucket.objects.add((BUCKET, store.build_object_name("broken")))

    listing = asyncio.run(commands.list_agents())

    assert listing["agents"][0]["agent_name"] == "broken"
    assert "error" in listing["agents"][0]
    assert listing["default_agent"] is None


def test_detaching_falls_back_to_the_environment_at_once(
    bucket: FakeGcsClient,
) -> None:
    _attach(data_lookback_window=30)
    assert effective_config.load({}).data_lookback_window == 30

    result = asyncio.run(commands.detach_agent(agent_name=WATCHED))

    assert result["detached"] is True
    assert _get_stored_record() is None
    assert effective_config.load({}).data_lookback_window == 14


def test_detaching_what_is_not_attached_is_not_an_error(
    bucket: FakeGcsClient,
) -> None:
    result = asyncio.run(commands.detach_agent(agent_name="nobody"))

    assert result == {
        "agent_name": "nobody",
        "detached": False,
        "watched": False,
    }


# --- a deployment that names no agent ----------------------------------------- #


@pytest.fixture
def unnamed(
    monkeypatch: pytest.MonkeyPatch, bucket: FakeGcsClient
) -> FakeGcsClient:
    """A deployment whose environment names no observed agent."""
    cfg = make_config(observed_agent_name="")
    monkeypatch.setattr(commands, "env_config", cfg)
    monkeypatch.setattr(effective_config, "env_config", cfg)
    effective_config.clear_cache()
    return bucket


def test_the_first_agent_attached_is_the_one_watched(
    unnamed: FakeGcsClient,
) -> None:
    plan = _attach("agent-a", dry_run=True)

    assert plan["watched"] is True
    assert plan["environment_agent"] == ""


def test_once_one_is_attached_another_is_not_watched(
    unnamed: FakeGcsClient,
) -> None:
    _attach("agent-a")

    assert _attach("agent-b", dry_run=True)["watched"] is False
    assert effective_config.load({}).observed_agent_name == "agent-a"


def test_another_agent_attached_as_the_default_is_watched(
    unnamed: FakeGcsClient,
) -> None:
    _attach("agent-a")

    assert _attach("agent-b", default=True, dry_run=True)["watched"] is True
    assert _attach("agent-b", default=True)["watched"] is True
    assert effective_config.load({}).observed_agent_name == "agent-b"


def test_listing_marks_the_default_as_watched(unnamed: FakeGcsClient) -> None:
    _attach("agent-a")
    _attach("agent-b", default=True)

    listing = asyncio.run(commands.list_agents())

    assert {a["agent_name"]: a["watched"] for a in listing["agents"]} == {
        "agent-a": False,
        "agent-b": True,
    }


def test_attaching_without_a_name_is_refused(unnamed: FakeGcsClient) -> None:
    assert "names none" in _attach()["error"]
