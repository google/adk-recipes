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

"""Holds `schemas/trajectory_payloads.json` and its turns file as the only
definition of the archived conversation.

The treatment `test_trajectories_schema.py` gives the id store: the writer, the
models behind it, the module and the `bq` guide all have to fit the tables
Terraform declares, so a column added on one side alone fails here rather than
in a load job against a deployed dataset.

Three properties are load-bearing enough to be tested rather than trusted:

* **The key has no `run_id`.** These tables record the *conversation*, so one
  copy serves every sweep that sampled it; `trajectories` records a *sweep's
  ingestion* and is keyed by the run. Adding the run here would store the
  bulky copy once per sighting, which is the whole cost the split avoids.
* **They expire and `trajectories` does not.** BigQuery expires partitions
  rather than columns, which is why the payload is a table and not a column:
  the index is the history the per-day chart reads and must outlive the
  telemetry, while the payload holds conversation content and must not.
* **The declaration is the one place the layout is written.** A partition
  cannot be added to a table afterwards, so the module, the guide and the test
  all have to read the same number before anything is created.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.trajectories.bigquery_store import (
    PAYLOADS_TABLE,
    TURNS_TABLE,
    BigQueryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    PayloadStatus,
    TrajectoryPayload,
    TrajectoryPayloadTurn,
)
from ambient_quality_agent.tools.trajectories.payloads import TrajectoryArchive
from pydantic import BaseModel, ValidationError

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = _REPO_ROOT / "terraform" / "modules" / "aqa" / "schemas"
MODULE_DIR = _REPO_ROOT / "terraform" / "modules" / "aqa"
PAYLOADS_TF = MODULE_DIR / "trajectory_payloads.tf"
VARIABLES_TF = MODULE_DIR / "variables.tf"

_SECONDS_A_DAY = 24 * 60 * 60

# Python type the model has to hold for each declared column type. A TIMESTAMP
# is a datetime here and crosses as ISO-8601 text; a JSON column takes the
# object, not text of it -- see `test_a_json_column_holds_an_object`.
_PYTHON_TYPE: dict[str, type] = {
    "STRING": str,
    "TIMESTAMP": dt.datetime,
    "INT64": int,
    "JSON": dict,
}

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

# The model behind each table, and a record of it holding every column --
# nullable ones included, since one left unset would pass the type check below
# by never being looked at.
_TABLES: dict[str, tuple[type[BaseModel], BaseModel]] = {
    "trajectory_payloads": (
        TrajectoryPayload,
        TrajectoryPayload(
            agent_name="root_agent",
            trajectory_id="session-abc",
            created_at=_NOW,
            status=PayloadStatus.INGESTED,
            turn_count=3,
            agents={"root_agent": {"instruction": "be helpful"}},
        ),
    ),
    "trajectory_payload_turns": (
        TrajectoryPayloadTurn,
        TrajectoryPayloadTurn(
            agent_name="root_agent",
            trajectory_id="session-abc",
            turn_index=0,
            created_at=_NOW,
            turn={"turn_index": 0, "events": [{"author": "user"}]},
        ),
    ),
}

_TABLE_IDS = list(_TABLES)


def _declaration(table_id: str) -> dict[str, Any]:
    """Loads schema declaration for one of the payload tables.

    Args:
        table_id: Identifier of the payload table schema.

    Returns:
        Dictionary parsed from the JSON schema definition.
    """
    return json.loads(
        (SCHEMA_DIR / f"{table_id}.json").read_text(encoding="utf-8")
    )


def _columns(table_id: str) -> dict[str, tuple[str, str]]:
    """Extracts column definitions declared in Terraform for `table_id`.

    Args:
        table_id: Identifier of the target table.

    Returns:
        Mapping of column names to (type, mode) tuples.
    """
    return {
        column["name"]: (column["type"], column.get("mode", "NULLABLE"))
        for column in _declaration(table_id)["schema"]
    }


def _bq_command(table_id: str) -> str:
    """Extracts the `bq mk` invocation documented in the deploy guide.

    Args:
        table_id: Identifier of the table to find in the deploy guide.

    Returns:
        The command block string for creating the table.
    """
    guide = (_REPO_ROOT / "extension" / "README.md").read_text(encoding="utf-8")
    command = next(
        (block for block in guide.split("bq mk ") if f'.{table_id}"' in block),
        None,
    )
    assert command is not None, f"the guide never creates {table_id}"
    return command


def _written() -> dict[str, list[dict[str, Any]]]:
    """Runs a sample archive through a mock client and captures loaded rows.

    Every nullable column is given a value to ensure all columns are exercised.

    Returns:
        Dictionary mapping table names to lists of loaded row dictionaries.
    """
    client = mock.MagicMock()
    payload = _TABLES[PAYLOADS_TABLE][1]
    turn = _TABLES[TURNS_TABLE][1]
    assert isinstance(payload, TrajectoryPayload)
    assert isinstance(turn, TrajectoryPayloadTurn)
    BigQueryTrajectoryStore(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).record_payloads([TrajectoryArchive(payload=payload, turns=(turn,))])
    return {
        call.args[1].rsplit(".", 1)[-1]: call.args[0]
        for call in client.load_table_from_json.call_args_list
    }


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_the_model_and_the_schema_hold_the_same_columns(table_id: str) -> None:
    """A field on one side alone is written nowhere and read as null."""
    model, _ = _TABLES[table_id]
    declared = set(_columns(table_id))
    fields = set(model.model_fields)

    assert fields == declared, (
        f"{model.__name__} and {table_id} disagree: "
        f"model only {sorted(fields - declared)}, "
        f"table only {sorted(declared - fields)}"
    )


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_the_model_holds_what_each_column_takes(table_id: str) -> None:
    model, record = _TABLES[table_id]
    columns = _columns(table_id)

    for name, value in dict(record).items():
        column_type, mode = columns[name]
        if value is None:
            assert mode == "NULLABLE", (
                f"{table_id}.{name} is {mode} but held as None"
            )
        else:
            assert isinstance(value, _PYTHON_TYPE[column_type]), (
                f"{table_id}.{name} is {column_type} but held as {type(value)}"
            )

    required = {
        name for name, (_, mode) in columns.items() if mode == "REQUIRED"
    }
    unset = {
        name
        for name in required
        if model.model_fields[name].is_required() is False
    }
    assert not unset, (
        f"{table_id} declares {sorted(unset)} REQUIRED, the model defaults it"
    )


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_a_json_column_holds_an_object(table_id: str) -> None:
    """Not text of one. `load_table_from_json` serializes the row it is given,
    so a value already rendered as a string lands in the JSON column as a
    string *scalar*, and `turn.events` then reads nothing -- silently.

    `BigQueryInvestigationStore` writes its JSON columns as text and carries
    `_UNWRAPPED_COUNTERS` to read around it. This is the archive an insight's
    evidence lives in, so it is stored the way it can be queried.
    """
    _, record = _TABLES[table_id]
    for name, (column_type, _) in _columns(table_id).items():
        if column_type != "JSON":
            continue
        value = getattr(record, name)
        assert not isinstance(value, str), (
            f"{table_id}.{name} is JSON, held as text -- it would store a string scalar"
        )


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_the_conversation_is_keyed_without_the_run(table_id: str) -> None:
    """`trajectories` is one sweep's ingestion and carries `run_id`; this is
    the conversation, which is the same thing whichever sweep sampled it.
    Keying it by the run would store a 50 KB copy per sighting, and a payload
    is referenced by every sweep that sampled it -- so there is also nothing a
    per-run cleanup could safely delete."""
    columns = _columns(table_id)

    assert "run_id" not in columns
    for column in ("agent_name", "trajectory_id"):
        assert columns[column] == ("STRING", "REQUIRED")


def test_the_agent_is_part_of_the_key_on_both_tables() -> None:
    """Trajectory ids being unique across agents is an assumption about the
    observed agent's telemetry, which AQA does not control -- the `big_query`
    source takes whatever `session_id` the analytics schema holds. In the key
    it makes a collision impossible, and it costs nothing."""
    for table_id in _TABLE_IDS:
        assert _declaration(table_id)["clustering"] == [
            "agent_name",
            "trajectory_id",
        ]


def test_the_manifest_holds_the_agent_configs_and_a_turn_does_not() -> None:
    """`AgentData.agents` belongs to the conversation, not to a turn of it. On
    a turn row it would be repeated on every one with no rule for which copy
    wins, which is what the second table exists to avoid."""
    assert "agents" in _columns("trajectory_payloads")
    assert "agents" not in _columns("trajectory_payload_turns")


def test_a_turn_is_ordered_by_an_index_every_source_has() -> None:
    """Split per turn rather than per trace: a multi-turn trajectory is one
    trace per turn, but the `big_query` source carries no trace ids at all,
    while `turn_index` exists on every source."""
    columns = _columns("trajectory_payload_turns")

    assert columns["turn_index"] == ("INT64", "REQUIRED")
    assert "trace_id" not in columns


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_both_tables_partition_and_expire_together(table_id: str) -> None:
    """A manifest whose turns have expired is a conversation nobody can read,
    so the two windows are one number in one place."""
    partitioning = _declaration(table_id)["partitioning"]

    assert partitioning["type"] == "DAY"
    assert partitioning["field"] == "created_at"
    assert partitioning["expiration_days"] == 30

    other = next(t for t in _TABLE_IDS if t != table_id)
    assert partitioning == _declaration(other)["partitioning"]


def test_the_index_beside_them_does_not_expire() -> None:
    """The contrast is why these are tables rather than a column on
    `trajectories`: BigQuery expires partitions, not columns, so one table
    would force the chart's history to the payload's retention."""
    assert "expiration_days" not in _declaration("trajectories")["partitioning"]


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_the_module_builds_the_declared_layout(table_id: str) -> None:
    """Read out of the declaration rather than repeated in HCL. The partition
    is the property a table cannot be given afterwards, and the expiration is
    the one an operator overrides, so both have to come from one place."""
    module = PAYLOADS_TF.read_text(encoding="utf-8")
    local = f"local.{table_id}_table"

    assert f"{local}.partitioning.type" in module
    assert f"{local}.partitioning.field" in module
    assert f"{local}.clustering" in module
    assert f"{local}.schema" in module
    assert "expiration_ms = local.trajectory_payload_expiration_ms" in module


def test_the_retention_override_falls_back_to_the_declared_default() -> None:
    """One default, in the declaration the guide and this test also read. A
    variable with its own default would be a second copy of the number, and
    the two would disagree the first time either moved."""
    module = PAYLOADS_TF.read_text(encoding="utf-8")

    assert "var.trajectory_payload_retention_days" in module
    assert (
        "local.trajectory_payloads_table.partitioning.expiration_days" in module
    ), "the module does not fall back to the declared default"

    variables = VARIABLES_TF.read_text(encoding="utf-8")
    declaration = variables.split(
        'variable "trajectory_payload_retention_days"'
    )[1]
    assert "default     = null" in declaration.split("variable ")[0], (
        "a default here would shadow the declaration's"
    )


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_the_deploy_guide_repeats_the_declared_layout(table_id: str) -> None:
    """`bq` reads only the schema out of a definition file, so the guide passes
    the rest as flags -- the one place the layout is written twice, and none of
    it can be added to a table after it exists."""
    command = _bq_command(table_id)
    declared = _declaration(table_id)

    clustering = ",".join(declared["clustering"])
    assert f"--clustering_fields {clustering}" in command
    assert (
        f"--time_partitioning_type {declared['partitioning']['type']}"
        in command
    )
    assert (
        f"--time_partitioning_field {declared['partitioning']['field']}"
        in command
    )

    expiration = declared["partitioning"]["expiration_days"] * _SECONDS_A_DAY
    assert f"--time_partitioning_expiration {expiration}" in command, (
        f"the guide keeps {table_id} for a different window than the declaration"
    )


def test_the_stored_copy_and_one_sweeps_attempt_are_different_vocabularies() -> (
    None
):
    """`status` indicates what the archive contains; `ingest_status` reflects what one
    sweep made of one attempt. They overlap on two values and differ on the
    third, which is the whole reason both exist: `truncated` is a decision we
    made, and ingestion has no equivalent of it."""
    assert set(PayloadStatus) != set(IngestStatus)
    assert PayloadStatus.TRUNCATED not in set(IngestStatus)
    assert IngestStatus.NOT_INGESTED not in set(PayloadStatus), (
        "a conversation nothing could be made of has no payload to store"
    )


@pytest.mark.parametrize("status", list(PayloadStatus))
def test_a_status_serializes_as_the_bare_string_the_column_holds(
    status: PayloadStatus,
) -> None:
    record = _TABLES["trajectory_payloads"][1].model_copy(
        update={"status": status}
    )

    assert record.model_dump(mode="json")["status"] == status.value


def test_what_a_stored_copy_is_can_never_be_unstated() -> None:
    """REQUIRED and undefaulted, so a conversation we cut cannot be archived as
    a whole one -- and a reader deciding whether to trust it as evidence never
    has to guess."""
    assert _columns("trajectory_payloads")["status"] == ("STRING", "REQUIRED")

    with pytest.raises(ValidationError):
        TrajectoryPayload(
            agent_name="root_agent",
            trajectory_id="session-abc",
            created_at=_NOW,
            turn_count=1,
        )  # ty: ignore[missing-argument]


def test_a_conversation_cannot_claim_a_negative_number_of_turns() -> None:
    """`turn_count` is how many turn rows a reader should expect to find, so a
    nonsense value reads as a partly-written payload rather than as a bug."""
    with pytest.raises(ValidationError):
        TrajectoryPayload(
            agent_name="root_agent",
            trajectory_id="session-abc",
            created_at=_NOW,
            status=PayloadStatus.INGESTED,
            turn_count=-1,
        )


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_written_columns_fit_the_terraform_schema(table_id: str) -> None:
    rows = _written()
    columns = _columns(table_id)

    for name, value in rows[table_id][0].items():
        assert name in columns, (
            f"{table_id}.{name} is written but never declared"
        )
        column_type, mode = columns[name]
        if value is None:
            assert mode == "NULLABLE", (
                f"{table_id}.{name} is {mode} but written null"
            )
        elif column_type == "TIMESTAMP":
            # A TIMESTAMP crosses the wire as an ISO-8601 instant, not a datetime.
            assert isinstance(value, str), (
                f"{table_id}.{name} is not ISO-8601 text"
            )
        else:
            assert isinstance(value, _PYTHON_TYPE[column_type]), (
                f"{table_id}.{name} is {column_type} but written as {type(value)}"
            )

    # Exactly, not merely the required ones: a column added to the declaration
    # and forgotten in the writer would read null on every archived
    # conversation, silently, and the row dict is the only place to catch it.
    assert set(rows[table_id][0]) == set(columns), (
        f"{table_id} names {sorted(set(rows[table_id][0]) ^ set(columns))} "
        "on one side only"
    )
