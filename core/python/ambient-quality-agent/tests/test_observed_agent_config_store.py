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

"""The stored agent configuration: its envelope, its reader, and the default
rule a sessionless request resolves through."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.observed_agent_config import store

from .conftest import FakeGcsClient

BUCKET = "jobs-bucket"


KEY = "acme-aqua__watched_agent"


def _config(key: str = KEY, **fields: Any) -> ObservedAgentConfig:
    return ObservedAgentConfig(
        observed_agent_name=key.rpartition("__")[2], agent_key=key, **fields
    )


def _stored(
    name: str, payload: dict[str, Any]
) -> tuple[tuple[str, str], bytes]:
    return (BUCKET, store.build_object_name(name)), json.dumps(payload).encode(
        "utf-8"
    )


def _client(*objects: tuple[tuple[str, str], bytes]) -> FakeGcsClient:
    return FakeGcsClient(dict(objects))


# --- the object round trip -------------------------------------------------- #


def _over(client: FakeGcsClient) -> GcsObjectStore:
    return GcsObjectStore(BUCKET, client_factory=lambda: client)


def test_what_was_written_is_what_comes_back() -> None:
    client = FakeGcsClient()
    record = store.AgentRecord(
        config=_config(data_lookback_window=30, telemetry_dataset="custom"),
        default=True,
        observed_deployment_name="prod",
        investigation_schedule="0 12 * * *",
        observed_engine_id="1234567890",
    )

    written = store.save_agent_record(_over(client), record)
    read_back = store.get_agent_record(_over(client), KEY)

    assert read_back == written
    # `save_agent_record` stamps the time; everything else survives untouched.
    assert written.updated_at
    assert written == type(record)(
        **{**vars(record), "updated_at": written.updated_at}
    )


def test_an_agent_nobody_attached_reads_as_absent() -> None:
    """Not an error: it is the normal state of a deployment before `attach`."""
    assert store.get_agent_record(_over(FakeGcsClient()), KEY) is None


def test_the_stored_object_is_flat_and_readable() -> None:
    """One read tells a reader how the agent is observed, without their having
    to know which field the runtime acts on and which only the update trigger
    does."""
    record = store.AgentRecord(
        config=_config(data_lookback_window=30),
        investigation_schedule="0 12 * * *",
    )

    payload = store.agent_record_to_json(record)

    assert payload["schema_version"] == store.SCHEMA_VERSION
    assert payload["agent"]["data_lookback_window"] == 30
    assert payload["agent"]["investigation_schedule"] == "0 12 * * *"
    # Never applied, so there is nothing to report as in effect.
    assert "applied" not in payload


# --- a reader that outlives the writer that wrote the object ---------------- #


def test_a_key_this_build_does_not_know_is_dropped_and_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment reads objects written by whatever `attach` the operator has.

    `Config(**fields)` raises `TypeError` on an unexpected key, and that failure
    would take out every request rather than just the read -- so an unknown key
    is dropped. Named in the log, because a key dropped for being misspelled
    looks exactly like one dropped for being newer.
    """
    client = _client(
        _stored(
            "watched-agent",
            {
                "schema_version": 1,
                "agent": {
                    "data_lookback_window": 21,
                    "invented_later": "surprise",
                },
            },
        )
    )

    with caplog.at_level(logging.WARNING):
        record = store.get_agent_record(_over(client), "watched-agent")

    assert record is not None
    assert record.config.data_lookback_window == 21
    assert "invented_later" in caplog.text


def test_a_field_the_build_does_know_is_never_named_as_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify a well-formed object reads without an unknown-key warning.

    The `agent` section carries the runtime fields and the attachment fields side
    by side, and they are read into two different dataclasses, so the unknown
    keys are the ones outside their union.
    `test_a_key_this_build_does_not_know_is_dropped_and_named` checks that an
    unknown key is named; this one checks that no known key is.
    """
    client = _client(
        _stored(
            "watched-agent",
            {
                "schema_version": 1,
                "agent": {
                    "data_lookback_window": 21,
                    "telemetry_dataset": "somewhere",
                    "default": True,
                    "observed_deployment_name": "a-deployment",
                    "investigation_schedule": "0 12 * * *",
                },
            },
        )
    )

    with caplog.at_level(logging.WARNING):
        record = store.get_agent_record(_over(client), "watched-agent")

    assert record is not None
    assert record.config.data_lookback_window == 21
    assert record.default is True
    assert record.investigation_schedule == "0 12 * * *"
    assert "does not know" not in caplog.text


def test_a_newer_schema_version_is_read_rather_than_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(
        _stored(
            "watched-agent", {"schema_version": 99, "agent": {"default": True}}
        )
    )

    with caplog.at_level(logging.WARNING):
        record = store.get_agent_record(_over(client), "watched-agent")

    assert record is not None and record.default is True
    assert "99" in caplog.text


def test_the_object_key_is_the_agents_identity() -> None:
    """An object claiming a key or a name other than its own would be
    reachable under two keys or none, so the object's key wins."""
    client = _client(
        _stored(
            KEY,
            {
                "agent": {
                    "agent_key": "acme-aqua__someone_else",
                    "observed_agent_name": "someone_else",
                }
            },
        )
    )

    record = store.get_agent_record(_over(client), KEY)

    assert record is not None
    assert record.agent_key == KEY
    assert record.agent_name == "watched_agent"


def test_an_object_stored_before_keys_is_named_by_its_bare_name() -> None:
    client = _client(_stored("watched_agent", {"agent": {}}))

    record = store.get_agent_record(_over(client), "watched_agent")

    assert record is not None
    assert record.agent_key == "watched_agent"
    assert record.agent_name == "watched_agent"


def test_a_bare_name_with_a_separator_in_it_is_the_name_it_records() -> None:
    """`support__bot` is a valid agent name, which also reads as a key."""
    client = _client(
        _stored(
            "support__bot", {"agent": {"observed_agent_name": "support__bot"}}
        )
    )

    record = store.get_agent_record(_over(client), "support__bot")

    assert record is not None
    assert record.agent_name == "support__bot"


def test_a_record_without_a_key_is_not_stored() -> None:
    """It would land under a name no read looks for."""
    record = store.AgentRecord(config=_config(key=""))

    with pytest.raises(ValueError, match="no key"):
        store.save_agent_record(_over(FakeGcsClient()), record)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(b'{"schema_version": 1}', id="no-agent-section"),
    ],
)
def test_an_object_that_is_not_a_config_raises(payload: bytes) -> None:
    """The reader is tolerant of keys it does not know, not of a file that is
    not one of these. The caller decides what to do about it."""
    with pytest.raises(ValueError):
        store.parse_agent_record(payload, KEY)


# --- which agent a sessionless request means -------------------------------- #


def test_list_agents_reads_the_names_off_the_keys() -> None:
    client = _client(
        _stored("zeta", {"agent": {}}),
        _stored("alpha", {"agent": {}}),
        ((BUCKET, "agents/nested/thing.json"), b"{}"),
        ((BUCKET, "agents/not-json.txt"), b""),
        ((BUCKET, "runs/something.json"), b"{}"),
    )

    assert store.list_agents(_over(client)) == [
        "alpha",
        "zeta",
    ]
    # One listing, whatever the contents say: no object was downloaded.
    assert client.downloads == []


def test_the_flagged_agent_is_the_default() -> None:
    client = _client(
        _stored("alpha", {"agent": {}}),
        _stored("zeta", {"agent": {"default": True}}),
    )

    assert store.resolve_default(_over(client)) == "zeta"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        pytest.param({}, "alpha", id="none-flagged"),
        pytest.param(
            {"alpha": True, "zeta": True}, "alpha", id="several-flagged"
        ),
    ],
)
def test_the_tie_break_is_alphabetical(
    flags: dict[str, bool], expected: str
) -> None:
    """Deterministic without any cross-object write, which is what lets
    `attach --default` leave its siblings' objects alone."""
    client = _client(
        *(
            _stored(name, {"agent": {"default": flags.get(name, False)}})
            for name in ("zeta", "alpha")
        )
    )

    assert store.resolve_default(_over(client)) == expected


@pytest.mark.parametrize(
    ("name", "default", "expected"),
    [
        pytest.param("beta", True, "beta", id="new-default-wins"),
        pytest.param("aaron", False, "zeta", id="new-unflagged-loses"),
        pytest.param("zeta", False, "alpha", id="default-cleared"),
    ],
)
def test_a_pending_record_resolves_as_if_saved(
    name: str, default: bool, expected: str
) -> None:
    client = _client(
        _stored("alpha", {"agent": {}}),
        _stored("zeta", {"agent": {"default": True}}),
    )
    pending = store.AgentRecord(config=_config(name), default=default)

    assert store.resolve_default(_over(client), pending=pending) == expected


def test_no_agents_resolves_to_nobody() -> None:
    assert store.resolve_default(_over(FakeGcsClient())) is None


# --- the schedule ------------------------------------------------------------ #


def test_a_record_with_no_schedule_given_has_the_default() -> None:
    assert (
        store.AgentRecord(config=_config()).investigation_schedule
        == store.DEFAULT_INVESTIGATION_SCHEDULE
    )


def test_a_schedule_stored_empty_reads_as_the_default() -> None:
    """The engine runs an empty schedule on the default, so the object says so."""
    client = _client(
        _stored(
            KEY, {"schema_version": 1, "agent": {"investigation_schedule": ""}}
        )
    )

    record = store.get_agent_record(_over(client), KEY)

    assert record is not None
    assert record.investigation_schedule == store.DEFAULT_INVESTIGATION_SCHEDULE


def test_a_schedule_in_the_applied_section_is_dropped_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The `agent` section's schedule is the one in effect."""
    client = _client(
        _stored(
            KEY,
            {
                "schema_version": 1,
                "agent": {"investigation_schedule": "0 6 * * *"},
                "applied": {
                    "at": "2026-09-21T10:02:00Z",
                    "investigation_schedule": "0 12 * * *",
                    "scheduled_trigger_enabled": False,
                },
            },
        )
    )

    with caplog.at_level(logging.WARNING):
        record = store.get_agent_record(_over(client), KEY)

    assert record is not None
    assert record.investigation_schedule == "0 6 * * *"
    assert record.applied is not None
    assert not hasattr(record.applied, "investigation_schedule")
    assert "investigation_schedule" not in record.list_unapplied_fields()
    assert not caplog.records


# --- desired versus in effect ----------------------------------------------- #


def test_every_trigger_field_is_pending_until_an_apply() -> None:
    record = store.AgentRecord(config=_config(), observed_engine_id="123")

    assert record.list_unapplied_fields() == list(store.TRIGGER_FIELDS)


def test_the_schedule_is_never_pending() -> None:
    """The engine reads it on every tick, so there is nothing to apply."""
    assert "investigation_schedule" not in store.TRIGGER_FIELDS
    assert "scheduled_trigger_enabled" not in store.TRIGGER_FIELDS


def test_only_what_changed_since_the_apply_is_pending() -> None:
    """What a reader is meant to say: the engine shown is the one that will
    apply after the next `attach`, not the one the sink watches."""
    record = store.AgentRecord(
        config=_config(),
        observed_engine_id="456",
        applied=store.Applied(
            at="2026-09-21T10:02:00Z", observed_engine_id="123"
        ),
    )

    assert record.list_unapplied_fields() == ["observed_engine_id"]


def test_nothing_is_pending_when_the_apply_matches() -> None:
    record = store.AgentRecord(
        config=_config(observed_project_id="agent-proj"),
        investigation_schedule="0 6 * * *",
        scheduled_trigger_enabled=False,
        observed_engine_id="123",
        applied=store.Applied(
            at="2026-09-21T10:02:00Z",
            observed_engine_id="123",
            observed_project_id="agent-proj",
        ),
    )

    assert record.list_unapplied_fields() == []


# --- grants recorded for `detach` to revoke --------------------------------- #

_GRANT = store.AppliedGrant(
    resource="proj:agent_telemetry",
    role="roles/bigquery.dataViewer",
    member="serviceAccount:aqua@proj.iam.gserviceaccount.com",
)


def test_grants_alone_put_no_trigger_into_effect() -> None:
    """An attach whose trigger commands failed still ran its grants, and
    records them; the defaults in its `applied` block were never applied."""
    record = store.AgentRecord(
        config=_config(),
        applied=store.Applied(grants=(_GRANT,)),
    )

    assert record.list_unapplied_fields() == list(store.TRIGGER_FIELDS)


def test_recorded_grants_survive_the_round_trip() -> None:
    client = FakeGcsClient()
    record = store.AgentRecord(
        config=_config(),
        applied=store.Applied(at="2026-10-05T10:00:00Z", grants=(_GRANT,)),
    )

    store.save_agent_record(_over(client), record)
    raw = json.loads(client.contents[(BUCKET, store.build_object_name(KEY))])
    read_back = store.get_agent_record(_over(client), KEY)

    assert raw["applied"]["grants"] == [
        {
            "resource": "proj:agent_telemetry",
            "role": "roles/bigquery.dataViewer",
            "member": "serviceAccount:aqua@proj.iam.gserviceaccount.com",
        }
    ]
    assert read_back is not None and read_back.applied is not None
    assert read_back.applied.grants == (_GRANT,)


def test_an_object_written_before_grants_were_recorded_has_none() -> None:
    client = _client(
        _stored(
            "watched-agent",
            {
                "agent": {},
                "applied": {"at": "2026-10-01T00:00:00Z"},
            },
        )
    )

    record = store.get_agent_record(_over(client), "watched-agent")

    assert record is not None and record.applied is not None
    assert record.applied.grants == ()


def test_an_unreadable_grant_is_dropped_and_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The rest still says how the agent is observed, and a binding that
    cannot be read could not be revoked anyway. An extra key is a newer
    build's, and does not cost the grant."""
    good = {**vars(_GRANT), "granted_by": "someone newer"}
    client = _client(
        _stored(
            "watched-agent",
            {
                "agent": {},
                "applied": {
                    "at": "2026-10-01T00:00:00Z",
                    "grants": [good, {"resource": "gs://b"}, "nonsense"],
                },
            },
        )
    )

    with caplog.at_level(logging.WARNING):
        record = store.get_agent_record(_over(client), "watched-agent")

    assert record is not None and record.applied is not None
    assert record.applied.grants == (_GRANT,)
    assert "nonsense" in caplog.text
    assert "gs://b" in caplog.text
