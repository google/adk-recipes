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

"""Tests for the starter selector: the random sample's selection as a selector."""

from __future__ import annotations

from unittest import mock

import pytest
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector, starter
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)

_SOURCES = ["big_query", "cloud_ops", "cloud_logging"]


def _build(source: str) -> str:
    return starter.build_starter_selector(
        source,
        project_id="obs-proj",
        dataset="telemetry",
        telemetry_table="events",
    )


@pytest.mark.parametrize("source", _SOURCES)
def test_every_starter_projects_the_session_id_as_target_id(
    source: str,
) -> None:
    first_line = _build(source).splitlines()[0]

    assert first_line.startswith("SELECT ")
    assert first_line.endswith(f" AS {selector.TARGET_COLUMN}")


@pytest.mark.parametrize("source", _SOURCES)
def test_every_starter_passes_the_lexical_checks(source: str) -> None:
    assert selector.find_lexical_violation(_build(source)) is None


@pytest.mark.parametrize("source", _SOURCES)
def test_every_starter_names_only_its_selector_table(source: str) -> None:
    sql = _build(source)
    table_id = selector.resolve_selector_table_id(source, "events")

    assert f"`obs-proj.telemetry.{table_id}`" in sql
    assert (
        selector.find_table_violation(
            sql,
            dataset="telemetry",
            selector_table_id=table_id,
            other_tables=frozenset({("telemetry", "events")})
            if source == "cloud_logging"
            else frozenset(),
        )
        is None
    )


@pytest.mark.parametrize("source", _SOURCES)
def test_every_starter_binds_the_window_and_the_agent(source: str) -> None:
    sql = _build(source)

    for parameter in (*selector.WINDOW_PARAMETERS, selector.AGENT_PARAMETER):
        assert parameter in sql
    assert selector.LIMIT_PARAMETER not in sql


def test_the_big_query_starter_reads_like_a_hand_written_selector() -> None:
    assert _build("big_query") == (
        "SELECT session_id AS target_id\n"
        "FROM `obs-proj.telemetry.events`\n"
        "WHERE timestamp BETWEEN @window_start AND @window_end\n"
        "  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name\n"
        "  AND session_id IS NOT NULL\n"
        "GROUP BY session_id"
    )


def test_the_cloud_logging_starter_reads_the_view_by_conversation() -> None:
    assert _build("cloud_logging") == (
        "SELECT conversation_id AS target_id\n"
        "FROM `obs-proj.telemetry.completions_view`\n"
        "WHERE timestamp BETWEEN @window_start AND @window_end\n"
        "  AND agent_name = @agent_name\n"
        "  AND conversation_id IS NOT NULL\n"
        "GROUP BY conversation_id"
    )


@pytest.mark.parametrize("fetcher_class", [BigQueryFetcher, CloudOpsFetcher])
def test_the_starter_filters_as_the_random_sample_does(fetcher_class) -> None:
    # Both sources select from the telemetry table itself, so the starter's
    # conditions must be the ones the default multi-turn count runs.
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client"
    ):
        fetcher = fetcher_class(
            project_id="aqua-proj",
            dataset="telemetry",
            table="events",
            location="us-central1",
            observed_project_id="obs-proj",
        )
    default_sql = fetcher._build_count_sql(MetricType.MULTI_TURN)
    sql = _build(fetcher_class.TELEMETRY_SOURCE)

    conditions = [
        line.strip()
        for line in sql.splitlines()
        if line.lstrip().startswith(("FROM", "WHERE", "AND"))
    ]
    assert len(conditions) == 4
    for condition in conditions:
        assert condition in default_sql
