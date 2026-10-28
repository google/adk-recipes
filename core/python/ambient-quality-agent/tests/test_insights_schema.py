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

"""Holds `terraform/modules/aqa/schemas` as the only insight schema.

The writer in `tools/insights/store.py` and the models behind it must fit the
tables Terraform declares. These tests read the definitions Terraform reads and
drive the real writer against them, so a column added on one side alone fails
here rather than in a load job against a deployed dataset. The read path names
its columns in raw SQL rather than in a row dict, so it is held to the same
schema by pulling the column names back out of the statements `InsightReader`
and `InsightStore` send.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.insights.bigquery_store import (
    BigQueryInsightStore,
)
from ambient_quality_agent.tools.insights.clustering import Cluster
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
    ProposedEdit,
    RootCause,
    RubricExample,
)
from ambient_quality_agent.tools.insights.root_cause_store import RootCauseStore
from google.cloud import bigquery
from pydantic import BaseModel

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = _REPO_ROOT / "terraform" / "modules" / "aqa" / "schemas"

# Python type the writer has to emit for each declared column type. A TIMESTAMP
# and a JSON column both cross as strings: an ISO-8601 instant and a serialized
# payload.
_PYTHON_TYPE: dict[str, type] = {
    "STRING": str,
    "TIMESTAMP": str,
    "INT64": int,
    "JSON": str,
}

# JSON columns loaded as native Python structures to store queryable JSON arrays
# in BigQuery. Other JSON columns are declared the same way but hold string
# scalars, and a query written for one encoding reads nothing off the other, so
# the rows in each table decide what its writer emits. Adding a column here is a
# decision about that table's data, not a consistency fix.
_JSON_VALUE_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {("insight_root_causes", "edits")}
)

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

# Table each SQL alias stands for. ``rc`` is the root-cause table read beside an
# insight, ``m`` and ``rm`` are the insights row an occurrence's and a
# diagnosis's owner are resolved through -- ``m`` in the EXISTS subquery too,
# which scopes its own ``o`` and ``m`` -- and ``t`` is the merge target the
# write checks is not itself a duplicate.
_ALIASED_TABLES: dict[str, str] = {
    "i": "insights",
    "o": "insight_occurrences",
    "rc": "insight_root_causes",
    "m": "insights",
    "rm": "insights",
    "t": "insights",
}

# Read like an alias but name no table: the UNNEST(@renames) struct, and the
# `owned` CTE, whose columns are checked against the two tables where it selects
# them rather than where it is read.
_NON_TABLE_QUALIFIERS = frozenset({"r", "owned"})

_QUALIFIED_COLUMN = re.compile(r"\b(\w+)\.(\w+)\b")
_LOWER_CASE_WORD = re.compile(r"\b[a-z_]\w*\b")
_TABLE_REFERENCE = re.compile(r"`([^`]*)`")


def _declaration(table_id: str) -> dict[str, Any]:
    """Loads schema declaration JSON for the specified table.

    Args:
        table_id: Short table identifier matching filename in `SCHEMA_DIR`.

    Returns:
        Deserialized schema declaration dictionary.
    """
    path = SCHEMA_DIR / f"{table_id}.json"
    assert path.exists(), f"no table {table_id} declared in {SCHEMA_DIR}"
    return json.loads(path.read_text(encoding="utf-8"))


def _terraform_columns(table_id: str) -> dict[str, tuple[str, str]]:
    """Extracts column definitions declared by Terraform for a table.

    Args:
        table_id: Short table identifier.

    Returns:
        Mapping of column name to `(type, mode)` tuple.
    """
    return {
        column["name"]: (column["type"], column.get("mode", "NULLABLE"))
        for column in _declaration(table_id)["schema"]
    }


def _written_rows() -> dict[str, tuple[dict[str, Any], bigquery.LoadJobConfig]]:
    """Executes a complete save cycle and returns captured row payloads per table.

    Both writers share one mock client, so every load lands in one call list and
    each table's row is picked out by the reference its writer passed.

    Returns:
        Mapping of short table name to `(row_dict, job_config)` tuple.
    """
    client = mock.MagicMock()
    RootCauseStore(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
    ).save(
        RootCause(
            root_cause_id="rc-1",
            insight_id="ins-1",
            occurrence_id="occ-1",
            agent_revision="agent-00042-abc",
            summary="The tool docstring names no allowed categories.",
            edits=[
                ProposedEdit(
                    path="app/agent.py",
                    start_line=48,
                    end_line=52,
                    before="def file_expense(amount, category):\n",
                    after="def file_expense(amount, category: ExpenseCategory):\n",
                    rationale="Constrains the argument to the allowed set.",
                )
            ],
            created_at=_NOW,
        )
    )
    BigQueryInsightStore(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).save_investigation_result(
        new_insights=[
            Insight(
                insight_id="ins-1",
                agent_name="root_agent",
                label="made no tool call",
                status=InsightStatus.NEW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        ],
        seen_insight_ids=[],
        recurring_insight_ids=[],
        occurrences=[
            InsightOccurrence(
                occurrence_id="occ-1",
                insight_id="ins-1",
                occurrence_state=OccurrenceState.TRACKED,
                run_id="run-1",
                created_at=_NOW,
                agent_name="root_agent",
                label="made no tool call",
                item_count=1,
                trace_count=2,
                # More ids than sampled rubrics, as a real cluster has: the
                # examples are capped, the trajectory ids are not.
                trajectory_ids=["case-1", "case-2"],
                rubrics=[
                    RubricExample(
                        rubric=Finding(
                            expected_behavior="call create_ticket",
                            actual_behavior="no tool call was made",
                            session_id="case-1",
                        )
                    )
                ],
            )
        ],
        now=_NOW,
    )
    return {
        call.args[1].rsplit(".", 1)[-1]: (
            call.args[0][0],
            call.kwargs["job_config"],
        )
        for call in client.load_table_from_json.call_args_list
    }


def _executed_statements() -> list[tuple[str, str]]:
    """Executes all query methods across reader and store, capturing SQL text.

    A mock client turns each call into a no-op over no rows, so what is under
    test is the SQL text alone. Every optional argument is supplied, because a
    filter clause only reaches the text when its argument does. Two callers are
    left out on purpose: `get_insight_with_occurrences` composes the two reads
    below it and has no SQL of its own, and the insert path loads a row dict
    that `test_written_columns_fit_the_terraform_schema` already covers.

    Returns:
        List of `(sender_class_name, sql_string)` tuples.
    """
    reader_client = mock.MagicMock()
    reader = BigQueryInsightReader(
        client=reader_client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    )
    reader.list_insights(
        statuses=[InsightStatus.NEW],
        run_id="run-1",
        limit=10,
        offset=0,
        has_root_cause=True,
    )
    reader.get_insight("ins-1")
    reader.list_occurrences(
        insight_id="ins-1", run_id="run-1", limit=10, offset=0
    )
    reader.list_root_causes("ins-1")
    reader.list_root_causes("ins-1", history=True)

    store_client = mock.MagicMock()
    store = BigQueryInsightStore(
        client=store_client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    )
    store.delete_failed_insights("run-1")
    store.find_existing_insights([Cluster(label="made no tool call")])
    store.save_investigation_result(
        new_insights=[],
        seen_insight_ids=["ins-1"],
        recurring_insight_ids=["ins-1"],
        occurrences=[],
        now=_NOW,
    )
    store.resolve_stale_insights(_NOW, window_days=30)
    store.dismiss_insight("ins-1")
    store.merge_insight(insight_id="ins-1", target_insight_id="ins-2")

    return [
        (sender, call.args[0])
        for sender, client in (
            ("BigQueryInsightReader", reader_client),
            ("InsightStore", store_client),
        )
        for call in client.query.call_args_list
    ]


def _queried_tables(sql: str) -> set[str]:
    """Extracts short table names from backquoted references in a SQL statement.

    Args:
        sql: SQL query string.

    Returns:
        Set of short table names referenced.
    """
    return {ref.rsplit(".", 1)[-1] for ref in _TABLE_REFERENCE.findall(sql)}


def _referenced_columns(sql: str) -> set[tuple[str | None, str]]:
    """Extracts referenced column names and their table qualifiers from SQL text.

    Args:
        sql: SQL query text to inspect.

    Returns:
        Set of `(table_id_or_none, column_name)` tuples.
    """
    body = sql
    for noise in (_TABLE_REFERENCE.pattern, r"'[^']*'", r"@\w+"):
        body = re.sub(noise, " ", body)

    qualified = _QUALIFIED_COLUMN.findall(body)
    for qualifier, _ in qualified:
        assert (
            qualifier in _ALIASED_TABLES or qualifier in _NON_TABLE_QUALIFIERS
        ), (
            f"{qualifier!r} qualifies a column but names no known table, in:\n{sql}"
        )

    body = _QUALIFIED_COLUMN.sub(" ", body)
    body = re.sub(r"\bAS\s+\w+", " ", body)
    body = re.sub(r"\w+\s*\(", " ", body)
    # `FROM ... i` leaves the alias itself standing where its columns were.
    bare = set(_LOWER_CASE_WORD.findall(body)) - _ALIASED_TABLES.keys()
    bare -= _NON_TABLE_QUALIFIERS

    if qualified:
        return {
            (_ALIASED_TABLES[q], name)
            for q, name in qualified
            if q in _ALIASED_TABLES
        } | {(None, word) for word in bare}

    tables = _queried_tables(sql)
    assert len(tables) == 1, (
        f"unqualified statement over {tables or 'no table'}:\n{sql}"
    )
    table = tables.pop()
    return {(table, word) for word in bare}


@pytest.mark.parametrize(
    "table_id", ["insights", "insight_occurrences", "insight_root_causes"]
)
def test_written_columns_fit_the_terraform_schema(table_id: str) -> None:
    columns = _terraform_columns(table_id)
    row, _ = _written_rows()[table_id]

    for name, value in row.items():
        assert name in columns, (
            f"{table_id}.{name} is written but never declared"
        )
        column_type, mode = columns[name]
        if value is None:
            assert mode == "NULLABLE", (
                f"{table_id}.{name} is {mode} but written null"
            )
        elif mode == "REPEATED":
            assert isinstance(value, list), (
                f"{table_id}.{name} is REPEATED, written scalar"
            )
            for element in value:
                assert isinstance(element, _PYTHON_TYPE[column_type]), (
                    f"{table_id}.{name} holds {column_type}, written {type(element)}"
                )
        else:
            expected = (
                list
                if (table_id, name) in _JSON_VALUE_COLUMNS
                else _PYTHON_TYPE[column_type]
            )
            assert isinstance(value, expected), (
                f"{table_id}.{name} is {column_type} but written as {type(value)}"
            )

    required = {
        name for name, (_, mode) in columns.items() if mode == "REQUIRED"
    }
    assert required <= set(row), (
        f"{table_id} never writes {required - set(row)}"
    )


def test_a_rejected_candidate_is_written_naming_no_insight() -> None:
    """The row the verification pass leaves behind has to fit the table.

    ``insight_id`` is NULLABLE for exactly this: a rejection is recorded as an
    occurrence naming no insight, and a load job is where a column that would
    not take it fails -- against the deployed dataset, long after here. The row
    says which of the two nameless states it is in, so the null is explained
    rather than inferred.
    """
    client = mock.MagicMock()
    BigQueryInsightStore(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    ).save_investigation_result(
        new_insights=[],
        seen_insight_ids=[],
        recurring_insight_ids=[],
        occurrences=[
            InsightOccurrence(
                occurrence_id="occ-1",
                insight_id=None,
                occurrence_state=OccurrenceState.REJECTED,
                run_id="run-1",
                created_at=_NOW,
                agent_name="root_agent",
                label="made no tool call",
            )
        ],
        now=_NOW,
    )

    (row,) = client.load_table_from_json.call_args.args[0]
    assert row["insight_id"] is None
    assert row["occurrence_state"] == "REJECTED"
    assert (
        _terraform_columns("insight_occurrences")["insight_id"][1] == "NULLABLE"
    )


@pytest.mark.parametrize(
    "table_id", ["insights", "insight_occurrences", "insight_root_causes"]
)
def test_a_repeated_column_is_never_written_null(table_id: str) -> None:
    """BigQuery has no null array: a REPEATED column is empty or it is absent,
    and a load job rejects an explicit null for one."""
    row, _ = _written_rows()[table_id]
    for name, (_, mode) in _terraform_columns(table_id).items():
        if mode == "REPEATED":
            assert row[name] is not None, (
                f"{table_id}.{name} is REPEATED, written null"
            )


@pytest.mark.parametrize(
    ("table_id", "model"),
    [
        ("insights", Insight),
        ("insight_occurrences", InsightOccurrence),
        ("insight_root_causes", RootCause),
    ],
)
def test_the_model_and_the_schema_hold_the_same_columns(
    table_id: str, model: type[BaseModel]
) -> None:
    """The writer names its columns one at a time, so a field added to the model
    and left out of that dict is written nowhere and read as null. Neither side
    is the source of truth for the other, and only this holds them level."""
    declared = set(_terraform_columns(table_id))
    fields = set(model.model_fields)

    assert fields == declared, (
        f"{model.__name__} and {table_id} disagree: "
        f"model only {sorted(fields - declared)}, "
        f"table only {sorted(declared - fields)}"
    )


@pytest.mark.parametrize(
    "table_id", ["insights", "insight_occurrences", "insight_root_causes"]
)
def test_the_writer_cannot_create_or_widen_a_table(table_id: str) -> None:
    """`load_table_from_json` autodetects a schema for a table it cannot find,
    which would leave a deployment running against a table Terraform never
    declared."""
    _, job_config = _written_rows()[table_id]

    assert (
        job_config.create_disposition == bigquery.CreateDisposition.CREATE_NEVER
    )
    assert job_config.autodetect is False
    # Both tables are append-only histories, so a truncating load would replace
    # one with whatever a single sweep happened to produce.
    assert (
        job_config.write_disposition == bigquery.WriteDisposition.WRITE_APPEND
    )
    assert not job_config.schema_update_options


def test_the_deploy_guide_repeats_the_declared_layout() -> None:
    """`bq` reads only the schema out of a definition file, so the guide passes
    the clustering as a flag. That is the one place the layout is written twice,
    and a table gains clustering only when it is created."""
    guide = (_REPO_ROOT / "extension" / "README.md").read_text(encoding="utf-8")

    for table_id in ("insights", "insight_occurrences", "insight_root_causes"):
        declared = _declaration(table_id)
        command = next(
            (b for b in guide.split("bq mk ") if f'.{table_id}"' in b), None
        )
        assert command is not None, f"the guide never creates {table_id}"

        clustering = ",".join(declared["clustering"])
        assert f"--clustering_fields {clustering}" in command, (
            f"the guide clusters {table_id} on something else"
        )

        # `insights.tf` reads a definition's columns and clustering and nothing
        # else, so a partition asked for here would be built by `bq` and not by
        # Terraform, and the two would drift on the one thing a table cannot be
        # given afterwards.
        assert "time_partitioning" not in declared, (
            f"{table_id} declares a partition Terraform will not build"
        )
        assert "--time_partitioning" not in command, (
            f"the guide partitions {table_id}, the definition does not"
        )


def test_queried_columns_exist_in_the_terraform_schema() -> None:
    """The read path spells its columns into SQL, where nothing checks them.

    The writer hands BigQuery a row dict the tests above can read back, but
    every query in `tools/insights/reader.py` and the DML in
    `tools/insights/store.py` name their columns in query text. Renaming a
    column in the schema and in the writer together leaves that text asking for
    a column the table no longer has, and only the deployed query fails.
    """
    for sender, sql in _executed_statements():
        # Naming no column would leave this quiet while the row is still read
        # by name.
        assert "SELECT *" not in sql, (
            f"{sender} selects every column, in:\n{sql}"
        )

        referenced = _referenced_columns(sql)
        queried = _queried_tables(sql)
        anywhere = {
            name for table in queried for name in _terraform_columns(table)
        }

        for table_id in queried:
            columns = set(_terraform_columns(table_id))
            read = {name for table, name in referenced if table == table_id}
            undeclared = read - columns
            assert not undeclared, (
                f"{sender} queries {table_id} for undeclared {sorted(undeclared)}, "
                f"in:\n{sql}"
            )
            # Reading nothing out of a table means the extraction lost sight
            # of part of the SQL, leaving this green over unchecked columns.
            assert read, (
                f"{sender} reads no column out of {table_id}, in:\n{sql}"
            )

        loose = {name for table, name in referenced if table is None} - anywhere
        assert not loose, (
            f"{sender} names undeclared {sorted(loose)}, in:\n{sql}"
        )
