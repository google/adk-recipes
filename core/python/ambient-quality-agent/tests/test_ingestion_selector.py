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

"""Tests for selector wrapping, validation, and fetcher factory propagation.

Validates selector wrapping, `AI.IF` placeholder substitution, lexical checks,
and BigQuery dry-run table allowlist enforcement.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent import backends
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import BigQueryJobFetcher
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)
from google.api_core import exceptions as api_exceptions

from .conftest import StateContext

_SELECTOR = "SELECT session_id AS target_id FROM t"


def test_the_wrap_renames_the_agreed_column_to_the_sources_id() -> None:
    """Verifies that the target column is aliased to the expected join column."""
    wrapped = selector.wrap(_SELECTOR, "session_id")

    assert wrapped.startswith(
        f"SELECT DISTINCT {selector.TARGET_COLUMN} AS session_id FROM ("  # noqa: S608 - trusted test constants
    )
    assert _SELECTOR in wrapped


def test_the_wrap_deduplicates_rather_than_trusting_the_selector() -> None:
    """Verifies that target IDs are deduplicated to prevent multiplying joined rows."""
    assert "SELECT DISTINCT" in selector.wrap(_SELECTOR, "session_id")


def test_the_wrap_binds_no_limit() -> None:
    """Verifies that the wrapped query omits @limit to allow count queries."""
    assert "@limit" not in selector.wrap(_SELECTOR, "session_id")


@pytest.mark.parametrize("source", ["big_query", "cloud_ops", "cloud_logging"])
def test_the_factory_hands_the_selector_to_whichever_source_is_configured(
    source: str,
) -> None:
    """Verifies that the fetcher factory propagates selector_sql to fetchers."""
    state: dict[str, Any] = {
        "project_id": "p",
        "observed_agent_name": "watched-agent",
        "run_id": "run-1",
        "telemetry_ingestion_source": source,
        "telemetry_dataset": "some_dataset",
        "telemetry_table": "some_table",
        "selector_sql": _SELECTOR,
    }
    with (
        mock.patch.object(backends.bigquery, "Client"),
        mock.patch.object(_common.storage, "Client"),
        mock.patch(
            "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher"
        ),
    ):
        fetcher = _common._create_default_fetcher(StateContext(state))

    assert fetcher._selector_sql == _SELECTOR


def test_a_run_with_no_selector_leaves_the_fetcher_sampling_at_random() -> None:
    """Verifies that fetchers initialize with an empty selector when unspecified."""
    state: dict[str, Any] = {
        "project_id": "p",
        "observed_agent_name": "watched-agent",
        "telemetry_ingestion_source": "big_query",
        "telemetry_dataset": "some_dataset",
    }
    with mock.patch.object(backends.bigquery, "Client"):
        fetcher = _common._create_default_fetcher(StateContext(state))

    assert fetcher._selector_sql == ""


@pytest.mark.parametrize("source", ["big_query", "cloud_ops", "cloud_logging"])
@pytest.mark.parametrize(
    ("snapshot", "owner"),
    [
        ({"observed_project_id": "agent-project"}, "agent-project"),
        ({"observed_project_id": ""}, "p"),
        # A run snapshot without observed_project_id.
        ({}, "p"),
    ],
)
def test_the_factory_reads_the_table_in_the_project_the_run_names(
    source: str, snapshot: dict[str, Any], owner: str
) -> None:
    state: dict[str, Any] = {
        "project_id": "p",
        "observed_agent_name": "watched-agent",
        "telemetry_ingestion_source": source,
        "telemetry_dataset": "some_dataset",
        "telemetry_table": "some_table",
        **snapshot,
    }
    with (
        mock.patch.object(backends.bigquery, "Client") as client,
        mock.patch.object(_common.storage, "Client"),
        mock.patch(
            "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher"
        ),
    ):
        fetcher = _common._create_default_fetcher(StateContext(state))

    assert fetcher.table_ref == f"{owner}.some_dataset.some_table"
    assert client.call_args.kwargs["project"] == "p"


# --- AI.IF placeholders: one selector, any deployment ---------------------- #

_MODEL = "gemini-3.5-flash-lite"

_AI_SELECTOR = f"""
SELECT session_id AS target_id
FROM `t`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
  AND AI.IF(
    ('Did the user ask for a refund?', text),
    endpoint => '{selector.AI_MODEL_PLACEHOLDER}'
  )
"""  # noqa: S608 - trusted test constants


def test_a_selector_without_placeholders_is_spliced_verbatim() -> None:
    """Verifies that queries without placeholders are left unchanged."""
    assert _SELECTOR in selector.wrap(_SELECTOR, "session_id", ai_model=_MODEL)


def test_the_wrap_resolves_the_ai_model_placeholder() -> None:
    """Verifies that the AI.IF model placeholder is substituted with the configured value."""
    wrapped = selector.wrap(_AI_SELECTOR, "session_id", ai_model=_MODEL)

    assert f"endpoint => '{_MODEL}'" in wrapped
    assert selector.AI_MODEL_PLACEHOLDER not in wrapped


@pytest.mark.parametrize("source", ["big_query", "cloud_ops", "cloud_logging"])
def test_every_source_resolves_the_model_placeholder_in_its_query(
    source: str,
) -> None:
    """Verifies that all fetcher sources resolve the AI.IF model placeholder."""
    state: dict[str, Any] = {
        "project_id": "p",
        "observed_agent_name": "watched-agent",
        "run_id": "run-1",
        "telemetry_ingestion_source": source,
        "telemetry_dataset": "some_dataset",
        "telemetry_table": "some_table",
        "selector_sql": _AI_SELECTOR,
        "selector_ai_model": _MODEL,
    }
    with (
        mock.patch.object(backends.bigquery, "Client"),
        mock.patch.object(_common.storage, "Client"),
        mock.patch(
            "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher"
        ),
    ):
        fetcher = _common._create_default_fetcher(StateContext(state))

    sql = fetcher._build_ingestion_sql(MetricType.MULTI_TURN)

    assert f"endpoint => '{_MODEL}'" in sql
    assert selector.AI_MODEL_PLACEHOLDER not in sql


# --- the lexical checks: hygiene ahead of the dry run ---------------------- #

_PROJECT = "test-project"
_DATASET = "agent_analytics"
_TABLE = "agent_events"
_TABLE_REF = f"{_PROJECT}.{_DATASET}.{_TABLE}"
_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)

_VALID_SELECTOR = f"""
SELECT session_id AS target_id
FROM `{_TABLE_REF}`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
"""  # noqa: S608 - trusted test constants


def test_an_empty_selector_is_a_caller_mistake_not_a_rejection() -> None:
    """Verifies that an empty or whitespace selector raises a ValueError."""
    with pytest.raises(ValueError, match="empty selector"):
        selector.find_lexical_violation("   \n ")


def test_a_valid_selector_clears_every_lexical_check() -> None:
    """Verifies that a valid, window- and agent-filtered selector passes lexical checks."""
    assert selector.find_lexical_violation(_VALID_SELECTOR) is None


@pytest.mark.parametrize(
    ("selector_sql", "reason"),
    [
        (
            f"{_VALID_SELECTOR};\nSELECT 1",
            selector.RejectionReason.STATEMENT_SEPARATOR,
        ),
        (
            f"{_VALID_SELECTOR}\nAND (DELETE FROM x)",  # noqa: S608 - trusted test constants
            selector.RejectionReason.FORBIDDEN_KEYWORD,
        ),
        (
            "SELECT session_id AS target_id FROM t WHERE agent = @agent_name",
            selector.RejectionReason.UNBOUNDED_WINDOW,
        ),
        (
            "SELECT session_id AS target_id FROM t "
            "WHERE timestamp BETWEEN @window_start AND @window_end",
            selector.RejectionReason.MISSING_AGENT_FILTER,
        ),
        (
            f"{_VALID_SELECTOR}\nLIMIT @limit",
            selector.RejectionReason.RESERVED_LIMIT_PARAMETER,
        ),
    ],
)
def test_each_lexical_rejection_names_its_reason(
    selector_sql: str, reason: selector.RejectionReason
) -> None:
    """Verifies that lexical violations return the corresponding RejectionReason."""
    violation = selector.find_lexical_violation(selector_sql)

    assert violation is not None
    assert violation.reason is reason
    assert violation.explanation


def test_only_one_bound_of_the_window_is_still_unbounded() -> None:
    """Verifies that referencing only one window boundary fails lexical validation."""
    violation = selector.find_lexical_violation(
        "SELECT session_id AS target_id FROM t "
        "WHERE timestamp > @window_start AND agent = @agent_name"
    )

    assert violation is not None
    assert violation.reason is selector.RejectionReason.UNBOUNDED_WINDOW


def test_the_budget_parameter_is_caught_before_the_count_fails_on_it() -> None:
    """Verify that selectors referencing `@limit` are rejected lexically.

    `count_scanned` binds only agent and window parameters; catching `@limit`
    during lexical validation prevents runtime failures from unbound parameters.
    """
    violation = selector.find_lexical_violation(
        f"{_VALID_SELECTOR}\nORDER BY timestamp\nLIMIT @limit"
    )

    assert violation is not None
    assert violation.reason is selector.RejectionReason.RESERVED_LIMIT_PARAMETER
    assert selector.LIMIT_PARAMETER in violation.explanation


# --- the precheck: the dry run and the table allowlist --------------------- #


def _table(project: str, dataset: str, table: str) -> mock.MagicMock:
    """Creates a mock BigQuery TableReference for dry-run testing.

    Args:
        project: Project identifier.
        dataset: Dataset identifier.
        table: Table identifier.

    Returns:
        Mock BigQuery TableReference.
    """
    return mock.MagicMock(project=project, dataset_id=dataset, table_id=table)


def _make_fetcher(client: mock.MagicMock, selector_sql: str) -> BigQueryFetcher:
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        return BigQueryFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location="us-central1",
            selector_sql=selector_sql,
        )


_WRAP_MARKER = f"SELECT DISTINCT {selector.TARGET_COLUMN} AS"
"""Prefix added by `selector.wrap` to distinguish wrapped run queries from standalone selectors."""


def _as_project_id(table: mock.MagicMock) -> mock.MagicMock:
    """Normalize a table reference to use the alphanumeric project ID.

    Simulates BigQuery dry-run behavior, which always resolves project numbers
    to their canonical project ID in `referenced_tables`.

    Args:
        table: Mock table reference that may have a numeric project identifier.

    Returns:
        Table reference normalized to the project ID.
    """
    if not table.project.isdigit():
        return table
    return _table(_PROJECT, table.dataset_id, table.table_id)


def _dry_run_client(
    *,
    referenced: list[mock.MagicMock] | None = None,
    selector_referenced: list[mock.MagicMock] | None = None,
    total_bytes: int = 4096,
) -> mock.MagicMock:
    """Create a mock BigQuery client simulating wrapped and standalone dry runs.

    Args:
        referenced: Tables returned for wrapped ingestion query dry runs.
        selector_referenced: Tables returned for standalone selector dry runs,
            defaulting to `referenced`.
        total_bytes: Simulated byte count processed by the query.

    Returns:
        Mock BigQuery client.
    """
    run_tables = [
        _as_project_id(table)
        for table in (
            referenced
            if referenced is not None
            else [_table(_PROJECT, _DATASET, _TABLE)]
        )
    ]
    selector_tables = (
        run_tables
        if selector_referenced is None
        else [_as_project_id(table) for table in selector_referenced]
    )

    def dry_run(sql: str, **_: Any) -> mock.MagicMock:
        return mock.MagicMock(
            referenced_tables=run_tables
            if _WRAP_MARKER in sql
            else selector_tables,
            total_bytes_processed=total_bytes,
        )

    client = mock.MagicMock()
    client.query.side_effect = dry_run
    return client


def test_a_fetcher_with_no_selector_has_nothing_to_precheck() -> None:
    """Verifies that prechecking a fetcher without a selector raises a ValueError."""
    fetcher = _make_fetcher(mock.MagicMock(), "")

    with pytest.raises(ValueError, match="no selector"):
        fetcher.precheck_selector("agent-a", _START, _END)


def test_a_valid_selector_passes_and_reports_what_it_would_scan() -> None:
    """Verifies that a valid selector passes precheck and reports table and byte estimates."""
    fetcher = _make_fetcher(_dry_run_client(), _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.is_valid
    assert result.rejection is None
    assert result.referenced_tables == (_TABLE_REF,)
    assert result.estimated_bytes == 4096


def test_a_selector_reading_off_the_allowlist_is_rejected() -> None:
    """Verifies that queries referencing unauthorized tables are rejected.

    Ensures selectors cannot access unauthorized datasets (such as internal AQA tables).
    """
    client = _dry_run_client(
        referenced=[
            _table(_PROJECT, _DATASET, _TABLE),
            _table(_PROJECT, "aqua_insights", "trajectory_payload_turns"),
        ]
    )
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert not result.is_valid
    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert "trajectory_payload_turns" in result.rejection.explanation
    assert len(result.referenced_tables) == 2


def test_a_selector_reading_no_table_at_all_is_not_a_selector() -> None:
    """Verifies that selectors referencing no tables (e.g. literal arrays) are rejected.

    The standalone dry run detects queries that bypass telemetry tables, which
    the wrapped query's outer `FROM` clause would otherwise mask.
    """
    client = _dry_run_client(selector_referenced=[])
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert not result.is_valid
    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert "reads no table at all" in result.rejection.explanation
    assert result.referenced_tables == ()


_PROJECT_NUMBER = "123456789012"
"""Simulated numeric project identifier injected by Agent Runtime."""

_NUMBERED_TABLE_REF = f"{_PROJECT_NUMBER}.{_DATASET}.{_TABLE}"


def _number_configured_fetcher(client: mock.MagicMock) -> BigQueryFetcher:
    """Creates a fetcher configured with a project number as in Agent Runtime.

    Args:
        client: Mock BigQuery client to attach to the fetcher.

    Returns:
        BigQueryFetcher configured with a numeric project identifier.
    """
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        return BigQueryFetcher(
            project_id=_PROJECT_NUMBER,
            dataset=_DATASET,
            table=_TABLE,
            location="us-central1",
            selector_sql=_VALID_SELECTOR.replace(
                _TABLE_REF, _NUMBERED_TABLE_REF
            ),
        )


def test_a_selector_passes_when_the_project_is_configured_as_a_number() -> None:
    """Verifies that precheck succeeds when the fetcher uses a project number.

    Agent Runtime supplies the project number while BigQuery dry runs report
    referenced tables under the project ID. Precheck takes the canonical
    project from the run dry run's telemetry-table entry to bridge this.
    """
    client = _dry_run_client()
    fetcher = _number_configured_fetcher(client)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.is_valid
    assert result.rejection is None
    assert result.referenced_tables == (_TABLE_REF,)
    assert fetcher.table_ref == _NUMBERED_TABLE_REF


def test_a_numbered_selector_reading_off_the_allowlist_is_still_refused() -> (
    None
):
    """Verify numeric project configuration still rejects unauthorized tables."""
    client = _dry_run_client(
        referenced=[
            _table(_PROJECT_NUMBER, _DATASET, _TABLE),
            _table(_PROJECT, "aqua_insights", "insights"),
        ]
    )
    fetcher = _number_configured_fetcher(client)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert not result.is_valid
    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert "aqua_insights.insights" in result.rejection.explanation


def test_the_same_table_in_another_project_is_a_second_table() -> None:
    """Verifies that accessing a same-named table in another project is rejected.

    Two run dry-run entries match the telemetry dataset and table, so no
    canonical project can be anchored.
    """
    client = _dry_run_client(
        referenced=[
            _table(_PROJECT, _DATASET, _TABLE),
            _table("someone-elses-project", _DATASET, _TABLE),
        ]
    )
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert not result.is_valid
    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert "someone-elses-project" in result.rejection.explanation


def test_a_refusal_names_the_telemetry_table_it_wanted() -> None:
    """Verify refusal messages include the expected telemetry table name.

    The error message must clearly identify the expected table so automated
    correction can construct a valid query.
    """
    client = _dry_run_client(
        referenced=[_table(_PROJECT, "aqua_insights", "insights")]
    )
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert f"{_DATASET}.{_TABLE}" in result.rejection.explanation
    assert "aqua_insights.insights" in result.rejection.explanation


def test_the_precheck_dry_runs_the_bare_selector_as_well_as_the_run_query() -> (
    None
):
    """Verifies that precheck executes dry runs for both wrapped and standalone queries."""
    client = _dry_run_client()
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    fetcher.precheck_selector("agent-a", _START, _END)

    submitted = [call.args[0] for call in client.query.call_args_list]
    assert len(submitted) == 2
    assert submitted[0] == fetcher._build_ingestion_sql(MetricType.MULTI_TURN)
    assert submitted[1] == _VALID_SELECTOR


def test_the_bare_selector_dry_run_binds_no_budget() -> None:
    """Verifies that standalone dry runs omit the `@limit` parameter."""
    client = _dry_run_client()
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    fetcher.precheck_selector("agent-a", _START, _END, limit=7)

    job_config = client.query.call_args_list[1].kwargs["job_config"]
    assert job_config.dry_run is True
    assert {
        parameter.name: parameter.value
        for parameter in job_config.query_parameters
    } == {"agent_name": "agent-a", "window_start": _START, "window_end": _END}


def test_the_bare_selector_dry_run_resolves_the_ai_model_placeholder() -> None:
    """Verifies that standalone dry runs substitute the `AI.IF` model placeholder."""
    client = _dry_run_client()
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        fetcher = BigQueryFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location="us-central1",
            selector_sql=_AI_SELECTOR.replace("`t`", f"`{_TABLE_REF}`"),
            selector_ai_model=_MODEL,
        )

    fetcher.precheck_selector("agent-a", _START, _END)

    bare = client.query.call_args_list[1].args[0]
    assert f"endpoint => '{_MODEL}'" in bare
    assert "connection_id" not in bare
    assert selector.AI_MODEL_PLACEHOLDER not in bare


def test_an_information_schema_read_is_refused_like_any_other_table() -> None:
    """Verify that INFORMATION_SCHEMA queries are rejected by the table allowlist.

    BigQuery includes metadata views in `referenced_tables`, allowing exact-match
    validation to prevent unauthorized schema inspection.
    """
    client = _dry_run_client(
        referenced=[_table(_PROJECT, _DATASET, "INFORMATION_SCHEMA.TABLES")]
    )
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert not result.is_valid
    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert "INFORMATION_SCHEMA.TABLES" in result.rejection.explanation


def test_federating_out_of_bigquery_is_refused_lexically() -> None:
    """Verify that EXTERNAL_QUERY calls are rejected during lexical checks.

    External queries federate to non-BigQuery data sources that do not appear in
    dry-run `referenced_tables`, requiring proactive lexical rejection.
    """
    violation = selector.find_lexical_violation(
        f"{_VALID_SELECTOR}\n  AND session_id IN ("  # noqa: S608 - trusted test constants
        "SELECT id FROM EXTERNAL_QUERY('conn', 'SELECT id FROM users'))"
    )

    assert violation is not None
    assert violation.reason is selector.RejectionReason.FORBIDDEN_KEYWORD


def test_a_lexical_failure_never_reaches_bigquery() -> None:
    """Verifies that lexical failures prevent BigQuery dry-run execution."""
    client = _dry_run_client()
    fetcher = _make_fetcher(client, "SELECT session_id AS target_id FROM t")

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert result.rejection.reason is selector.RejectionReason.UNBOUNDED_WINDOW
    client.query.assert_not_called()


def test_the_precheck_submits_exactly_what_the_run_would() -> None:
    """Verifies that precheck and execution submit identical query strings."""
    client = _dry_run_client()
    client.query.return_value.result.return_value = []
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=7
    )
    submitted = client.query.call_args.args[0]
    fetcher.precheck_selector("agent-a", _START, _END, limit=7)
    prechecked = client.query.call_args_list[1].args[0]

    assert prechecked == submitted


def test_the_dry_run_binds_the_parameters_the_run_binds() -> None:
    """Verifies that dry-run query jobs bind all standard execution parameters."""
    client = _dry_run_client()
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    fetcher.precheck_selector("agent-a", _START, _END, limit=7)

    job_config = client.query.call_args_list[0].kwargs["job_config"]
    assert job_config.dry_run is True
    assert job_config.use_query_cache is False
    assert {
        parameter.name: parameter.value
        for parameter in job_config.query_parameters
    } == {
        "agent_name": "agent-a",
        "window_start": _START,
        "window_end": _END,
        "limit": 7,
    }


def test_a_selector_holding_braces_reaches_the_dry_run_intact() -> None:
    """Verifies that SQL braces in selectors survive template substitution."""
    brace_selector = f"""
SELECT session_id AS target_id
FROM `{_TABLE_REF}`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
  AND REGEXP_CONTAINS(JSON_VALUE(content.text_summary), r'\\d{{3}}')
  AND TO_JSON_STRING(content) = '{{"tool":"refund"}}'
"""  # noqa: S608 - trusted test constants
    client = _dry_run_client()
    fetcher = _make_fetcher(client, brace_selector)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.is_valid
    assert brace_selector in client.query.call_args_list[0].args[0]


def test_a_bigquery_failure_comes_back_without_its_message() -> None:
    """Verifies that BigQuery errors return sanitized rejection details without row data."""
    long_predicate = "  AND " + " AND ".join(
        f"col_{index} IS NOT NULL" for index in range(40)
    )
    broken_selector = f"{_VALID_SELECTOR}{long_predicate}\n"
    client = _dry_run_client()
    fetcher = _make_fetcher(client, broken_selector)
    offending_line = (
        fetcher._build_ingestion_sql(MetricType.MULTI_TURN)
        .splitlines()
        .index(long_predicate)
        + 1
    )
    client.query.side_effect = api_exceptions.BadRequest(
        f"Invalid cast at [{offending_line}:7]: 'sensitive-row-value'",
        errors=[{"reason": "invalidQuery"}],
    )

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert result.rejection.reason is selector.RejectionReason.INVALID_SQL
    assert "invalidQuery" in result.rejection.explanation
    assert "sensitive-row-value" not in result.rejection.explanation
    assert long_predicate.strip() not in result.rejection.explanation
    assert long_predicate.strip()[:120] in result.rejection.explanation


def test_a_transient_bigquery_failure_is_not_the_selectors_fault() -> None:
    """Verifies that BigQuery 5xx errors propagate instead of returning rejections."""
    client = _dry_run_client()
    client.query.side_effect = api_exceptions.ServiceUnavailable("backend down")
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    with pytest.raises(api_exceptions.ServiceUnavailable):
        fetcher.precheck_selector("agent-a", _START, _END)


def test_english_in_an_ai_prompt_is_not_a_ddl_keyword() -> None:
    """Verifies that natural language text inside string literals is ignored by keyword checks."""
    prose_selector = f"""
SELECT session_id AS target_id  -- drop nothing, just read
FROM `{_TABLE_REF}`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
  AND AI.IF(('Did the user ask to delete or update their account?', text))
"""  # noqa: S608 - trusted test constants

    assert selector.find_lexical_violation(prose_selector) is None


def test_a_semicolon_inside_a_literal_is_not_a_second_statement() -> None:
    """Verifies that semicolons inside string literals do not trigger statement separation errors."""
    quoted_selector = (
        "SELECT session_id AS target_id FROM t "
        "WHERE timestamp BETWEEN @window_start AND @window_end "
        "AND agent = @agent_name AND note = 'step one; step two'"
    )

    assert selector.find_lexical_violation(quoted_selector) is None


def test_a_keyword_outside_a_literal_is_still_caught() -> None:
    """Verifies that DDL/DML keywords outside string literals are detected."""
    violation = selector.find_lexical_violation(
        f"{_VALID_SELECTOR}\n  AND (SELECT 1 FROM UNNEST([1]) WHERE 'delete' IS NULL) "  # noqa: S608 - trusted test constants
        "IS NULL\nUNION ALL\nDELETE FROM x"
    )

    assert violation is not None
    assert violation.reason is selector.RejectionReason.FORBIDDEN_KEYWORD


def test_a_bare_selector_reading_a_same_named_foreign_table_is_refused() -> (
    None
):
    """Verifies that the standalone dry run must stay in the canonical project.

    The run dry run anchors the canonical project; a selector whose own dry run
    resolves the same dataset and table in another project is still refused.
    """
    client = _dry_run_client(
        selector_referenced=[_table("someone-elses-project", _DATASET, _TABLE)]
    )
    fetcher = _make_fetcher(client, _VALID_SELECTOR)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert "someone-elses-project" in result.rejection.explanation


# --- selector table per source ---------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "telemetry_table", "table"),
    [
        ("big_query", "agent_events", "agent_events"),
        ("cloud_ops", "_AllSpans", "_AllSpans"),
        ("cloud_logging", "sink", selector.CLOUD_LOGGING_SELECTOR_VIEW),
    ],
)
def test_each_source_names_one_selector_table(
    source: str, telemetry_table: str, table: str
) -> None:
    """Verifies that `build_selector_table_ref` resolves the view only for `cloud_logging`."""
    assert (
        selector.build_selector_table_ref(
            source, project_id="p", dataset="d", telemetry_table=telemetry_table
        )
        == f"p.d.{table}"
    )


@pytest.mark.parametrize(
    ("source", "footprint"),
    [
        ("big_query", {"t"}),
        ("cloud_ops", {"t"}),
        ("cloud_logging", {"t", selector.CLOUD_LOGGING_VIEW_BASE_TABLE}),
    ],
)
def test_each_source_names_its_selector_footprint(
    source: str, footprint: set[str]
) -> None:
    """Verifies that only the view-backed source adds its view's base table."""
    assert selector.resolve_selector_footprint(source, "t") == frozenset(
        footprint
    )


_SINK = "gen_ai_client_inference_operation_details"
_LOGGING_DATASET = "travel_desk_telemetry"
_VIEW_REF = (
    f"{_PROJECT}.{_LOGGING_DATASET}.{selector.CLOUD_LOGGING_SELECTOR_VIEW}"
)

_VIEW_SELECTOR = f"""
SELECT conversation_id AS target_id
FROM `{_VIEW_REF}`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent_name = @agent_name
"""  # noqa: S608 - trusted test constants

_SINK_TABLE = _table(_PROJECT, _LOGGING_DATASET, _SINK)
_COMPLETIONS_TABLE = _table(
    _PROJECT, _LOGGING_DATASET, selector.CLOUD_LOGGING_VIEW_BASE_TABLE
)


def _logging_fetcher(client: mock.MagicMock) -> CloudLoggingFetcher:
    """Creates a Cloud Logging fetcher over a mock BigQuery client.

    Args:
        client: Mock BigQuery client to attach to the fetcher.

    Returns:
        CloudLoggingFetcher configured with `_VIEW_SELECTOR`.
    """
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        return CloudLoggingFetcher(
            project_id=_PROJECT,
            dataset=_LOGGING_DATASET,
            table=_SINK,
            location="us-central1",
            gcs_reader=mock.MagicMock(),
            selector_sql=_VIEW_SELECTOR,
        )


def test_the_cloud_logging_selector_table_is_the_completions_view() -> None:
    """Verifies that selectors read the view while ingestion reads the sink."""
    fetcher = _logging_fetcher(mock.MagicMock())

    assert fetcher.selector_table_id == selector.CLOUD_LOGGING_SELECTOR_VIEW
    assert fetcher.table_ref == f"{_PROJECT}.{_LOGGING_DATASET}.{_SINK}"
    assert fetcher.selector_footprint == frozenset(
        {_SINK, selector.CLOUD_LOGGING_VIEW_BASE_TABLE}
    )


def test_a_cloud_logging_selector_over_the_view_passes() -> None:
    """Verifies that the view's base tables in both dry runs pass the precheck.

    A dry run over a view reports its base tables, so both the run query and
    the bare selector resolve to the sink and the BigLake completions table.
    """
    client = _dry_run_client(referenced=[_SINK_TABLE, _COMPLETIONS_TABLE])
    fetcher = _logging_fetcher(client)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.is_valid
    assert result.referenced_tables == (
        f"{_PROJECT}.{_LOGGING_DATASET}.{selector.CLOUD_LOGGING_VIEW_BASE_TABLE}",
        f"{_PROJECT}.{_LOGGING_DATASET}.{_SINK}",
    )


def test_a_cloud_logging_selector_passes_when_the_project_is_a_number() -> None:
    """Verifies that the view footprint is anchored on the dry-run project ID.

    Configuration holds the project number while the dry runs report the
    project ID, as BigQuery does.
    """
    client = _dry_run_client(referenced=[_SINK_TABLE, _COMPLETIONS_TABLE])
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        fetcher = CloudLoggingFetcher(
            project_id=_PROJECT_NUMBER,
            dataset=_LOGGING_DATASET,
            table=_SINK,
            location="us-central1",
            gcs_reader=mock.MagicMock(),
            selector_sql=_VIEW_SELECTOR.replace(_PROJECT, _PROJECT_NUMBER),
        )

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.is_valid
    assert result.referenced_tables == (_COMPLETIONS_REF, _SINK_REF)


def _logging_fetcher_with(
    client: mock.MagicMock, selector_sql: str
) -> CloudLoggingFetcher:
    """Creates a Cloud Logging fetcher holding `selector_sql`.

    Args:
        client: Mock BigQuery client to attach to the fetcher.
        selector_sql: Selector to configure.

    Returns:
        CloudLoggingFetcher over `client`.
    """
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        return CloudLoggingFetcher(
            project_id=_PROJECT,
            dataset=_LOGGING_DATASET,
            table=_SINK,
            location="us-central1",
            gcs_reader=mock.MagicMock(),
            selector_sql=selector_sql,
        )


_SINK_REF = f"{_PROJECT}.{_LOGGING_DATASET}.{_SINK}"
_COMPLETIONS_REF = (
    f"{_PROJECT}.{_LOGGING_DATASET}.{selector.CLOUD_LOGGING_VIEW_BASE_TABLE}"
)


@pytest.mark.parametrize(
    ("from_clause", "forbidden"),
    [
        pytest.param(
            f"`{_SINK_REF}` JOIN `{_COMPLETIONS_REF}` USING (session_id)",
            (_SINK, selector.CLOUD_LOGGING_VIEW_BASE_TABLE),
            id="base-tables-directly",
        ),
        pytest.param(
            f"`{_VIEW_REF}` AS v JOIN `{_SINK_REF}` AS s USING (session_id)",
            (_SINK,),
            id="view-join-sink",
        ),
        pytest.param(
            f"`{_VIEW_REF}` AS v JOIN `{_COMPLETIONS_REF}` AS c USING (session_id)",
            (selector.CLOUD_LOGGING_VIEW_BASE_TABLE,),
            id="view-join-completions",
        ),
    ],
)
def test_a_selector_naming_the_views_base_tables_is_refused_lexically(
    from_clause: str, forbidden: tuple[str, ...]
) -> None:
    """Verifies that naming a view's base table is refused before any dry run.

    A dry run cannot tell the view from a direct join of its base tables, so
    both resolve to the same footprint; only the selector text shows which
    tables it names.
    """
    client = _dry_run_client(referenced=[_SINK_TABLE, _COMPLETIONS_TABLE])
    fetcher = _logging_fetcher_with(
        client, _VIEW_SELECTOR.replace(f"`{_VIEW_REF}`", from_clause)
    )

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert (
        f"{_LOGGING_DATASET}.{selector.CLOUD_LOGGING_SELECTOR_VIEW}"
        in result.rejection.explanation
    )
    for table in forbidden:
        assert f"{_LOGGING_DATASET}.{table}" in result.rejection.explanation
    client.query.assert_not_called()


def test_a_self_join_on_the_view_passes() -> None:
    """Verifies that reading the view twice still names only the view."""
    client = _dry_run_client(referenced=[_SINK_TABLE, _COMPLETIONS_TABLE])
    fetcher = _logging_fetcher_with(
        client,
        _VIEW_SELECTOR.replace(
            f"`{_VIEW_REF}`",
            f"`{_VIEW_REF}` AS a JOIN `{_VIEW_REF}` AS b USING (session_id)",
        ),
    )

    assert fetcher.precheck_selector("agent-a", _START, _END).is_valid


def test_base_table_names_in_literals_and_comments_are_not_references() -> None:
    """Verifies that prose naming a base table does not trip the table guard."""
    client = _dry_run_client(referenced=[_SINK_TABLE, _COMPLETIONS_TABLE])
    fetcher = _logging_fetcher_with(
        client,
        f"""
SELECT session_id AS target_id  -- not {_SINK}, not completions
FROM `{_VIEW_REF}`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
  /* completions */
  AND AI.IF(('Did the user mention completions or {_SINK}?', text),
            endpoint => '{selector.AI_MODEL_PLACEHOLDER}')
""",  # noqa: S608 - trusted test constants
    )

    assert fetcher.precheck_selector("agent-a", _START, _END).is_valid


def test_a_selector_not_naming_its_selector_table_is_refused_lexically() -> (
    None
):
    """Verifies that a selector must name the selector table, before any dry run."""
    client = _dry_run_client()
    fetcher = _make_fetcher(
        client,
        _VALID_SELECTOR.replace(_TABLE_REF, f"{_PROJECT}.{_DATASET}.other"),
    )

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert f"{_DATASET}.{_TABLE}" in result.rejection.explanation
    client.query.assert_not_called()


@pytest.mark.parametrize(
    ("source", "telemetry_table"),
    [
        ("big_query", "agent_events"),
        ("cloud_ops", "_AllSpans"),
        ("cloud_logging", _SINK),
    ],
)
def test_a_selector_naming_its_selector_table_clears_the_table_guard(
    source: str, telemetry_table: str
) -> None:
    """Verifies that each source accepts a selector over its own selector table."""
    table_id = selector.resolve_selector_table_id(source, telemetry_table)
    sql = (
        f"SELECT session_id AS target_id FROM `p.d.{table_id}` "  # noqa: S608 - trusted test constants
        "WHERE timestamp BETWEEN @window_start AND @window_end "
        "AND agent = @agent_name"
    )

    assert (
        selector.find_table_violation(
            sql,
            dataset="d",
            selector_table_id=table_id,
            other_table_ids=selector.resolve_selector_footprint(
                source, telemetry_table
            )
            - {table_id},
        )
        is None
    )


@pytest.mark.parametrize(
    ("sql", "valid"),
    [
        pytest.param("SELECT x FROM `p.d.completions_view`", True, id="view"),
        pytest.param(
            "SELECT x FROM d.completions_view", True, id="unquoted-view"
        ),
        pytest.param(
            "SELECT x FROM `p`.`d`.`completions_view`", True, id="split"
        ),
        pytest.param("SELECT x FROM `p.d.COMPLETIONS_VIEW`", True, id="case"),
        pytest.param("SELECT x FROM `p.d.completions`", False, id="base-only"),
        pytest.param(
            "SELECT x FROM d.completions_view JOIN d.Completions USING (y)",
            False,
            id="base-case",
        ),
        pytest.param(
            "SELECT x FROM `p.d.completions_view_v2`", False, id="longer-name"
        ),
        pytest.param(
            "SELECT x FROM `my-completions.d.completions_view`",
            True,
            id="base-name-in-project",
        ),
        pytest.param(
            "SELECT x FROM `p.d.completions_view` WHERE y = 'completions'",
            True,
            id="literal",
        ),
    ],
)
def test_the_table_guard_matches_whole_identifiers(
    sql: str, valid: bool
) -> None:
    """Verifies identifier boundaries: `completions_view` does not name `completions`."""
    rejection = selector.find_table_violation(
        sql,
        dataset="d",
        selector_table_id=selector.CLOUD_LOGGING_SELECTOR_VIEW,
        other_table_ids=frozenset(
            {"sink", selector.CLOUD_LOGGING_VIEW_BASE_TABLE}
        ),
    )

    assert (rejection is None) is valid


@pytest.mark.parametrize(
    ("run_tables", "selector_tables", "named"),
    [
        pytest.param(
            [_SINK_TABLE],
            [_SINK_TABLE],
            _COMPLETIONS_REF,
            id="sink-alone",
        ),
        pytest.param(
            [_SINK_TABLE, _COMPLETIONS_TABLE],
            [_COMPLETIONS_TABLE],
            _SINK_REF,
            id="completions-alone",
        ),
        pytest.param(
            [
                _SINK_TABLE,
                _table(
                    "someone-elses-project",
                    _LOGGING_DATASET,
                    selector.CLOUD_LOGGING_VIEW_BASE_TABLE,
                ),
            ],
            None,
            "someone-elses-project",
            id="foreign-completions",
        ),
        pytest.param(
            [_SINK_TABLE, _COMPLETIONS_TABLE],
            [
                _SINK_TABLE,
                _table(_PROJECT, _LOGGING_DATASET, "INFORMATION_SCHEMA.TABLES"),
            ],
            "INFORMATION_SCHEMA.TABLES",
            id="information-schema",
        ),
    ],
)
def test_a_cloud_logging_selector_off_the_view_is_refused(
    run_tables: list[mock.MagicMock],
    selector_tables: list[mock.MagicMock] | None,
    named: str,
) -> None:
    """Verifies that resolving anything but the view's full footprint is refused.

    The refusal names the view as the one table the selector may read, and
    the tables read that are not allowed or the footprint tables not read.
    """
    client = _dry_run_client(
        referenced=run_tables, selector_referenced=selector_tables
    )
    fetcher = _logging_fetcher(client)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    assert (
        f"{_LOGGING_DATASET}.{selector.CLOUD_LOGGING_SELECTOR_VIEW}"
        in result.rejection.explanation
    )
    assert named in result.rejection.explanation


def test_a_refusal_separates_disallowed_tables_from_missing_ones() -> None:
    """Verifies that the refusal lists extra and missing tables separately.

    Only the tables outside the footprint are reported as disallowed; the
    footprint table that was not read is reported as missing.
    """
    client = _dry_run_client(
        referenced=[_SINK_TABLE, _COMPLETIONS_TABLE],
        selector_referenced=[
            _SINK_TABLE,
            _table(_PROJECT, "aqua_insights", "insights"),
        ],
    )
    fetcher = _logging_fetcher(client)

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    explanation = result.rejection.explanation
    assert (
        f"It reads tables it may not: {_PROJECT}.aqua_insights.insights."
        in (explanation)
    )
    assert f"It does not resolve to {_COMPLETIONS_REF}," in explanation
    assert f"may not: {_SINK_REF}" not in explanation


def test_a_run_dry_run_missing_a_footprint_table_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verifies that footprint drift, such as a changed view, is logged."""
    client = _dry_run_client(referenced=[_SINK_TABLE])
    fetcher = _logging_fetcher(client)

    with caplog.at_level(logging.WARNING):
        result = fetcher.precheck_selector("agent-a", _START, _END)

    assert not result.is_valid
    assert any(
        _COMPLETIONS_REF in record.getMessage()
        and _SINK_REF in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("fetcher_class", "source"),
    [
        (BigQueryFetcher, "big_query"),
        (CloudOpsFetcher, "cloud_ops"),
        (CloudLoggingFetcher, "cloud_logging"),
    ],
)
def test_each_fetcher_names_the_telemetry_source_it_serves(
    fetcher_class: type[BigQueryJobFetcher], source: str
) -> None:
    """Verifies that each fetcher's source key selects its selector table."""
    assert fetcher_class.TELEMETRY_SOURCE == source


def test_a_bigquery_fetchers_selector_table_is_its_telemetry_table() -> None:
    """Verifies that non-view sources select their telemetry table."""
    fetcher = _make_fetcher(mock.MagicMock(), _VALID_SELECTOR)

    assert fetcher.selector_table_id == _TABLE
    assert fetcher.selector_footprint == frozenset({_TABLE})


# --- quoted identifiers: visible to the lexical checks --------------------- #

_VIEW_WHERE = "WHERE timestamp BETWEEN @window_start AND @window_end AND agent_name = @agent_name"


def _view_table_violation(sql: str) -> selector.Rejection | None:
    """Runs the table guard as the `cloud_logging` precheck does.

    Args:
        sql: Selector to check.

    Returns:
        The guard's rejection, or None.
    """
    return selector.find_table_violation(
        sql,
        dataset="d",
        selector_table_id=selector.CLOUD_LOGGING_SELECTOR_VIEW,
        other_table_ids=frozenset(
            {"sink", selector.CLOUD_LOGGING_VIEW_BASE_TABLE}
        ),
    )


def test_an_apostrophe_in_a_quoted_alias_cannot_hide_a_second_table() -> None:
    """Verifies that `` `v'` `` does not open a literal masking the SQL after it."""
    sql = (
        "SELECT conversation_id AS target_id, 1 AS `v'` "  # noqa: S608 - trusted test constants
        "FROM `p.d.completions_view` JOIN `p.d.completions` USING (conversation_id) "
        f"{_VIEW_WHERE} AND note = 'x'"
    )

    assert selector.find_lexical_violation(sql) is None
    rejection = _view_table_violation(sql)
    assert rejection is not None
    assert rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    assert "Remove d.completions:" in rejection.explanation


def test_an_apostrophe_in_a_quoted_alias_cannot_hide_a_forbidden_keyword() -> (
    None
):
    """Verifies that `` `v'` `` does not mask `EXTERNAL_QUERY` from the denylist."""
    sql = (
        "SELECT conversation_id AS target_id, 1 AS `v'`, "  # noqa: S608 - trusted test constants
        "(SELECT 1 FROM EXTERNAL_QUERY('conn', 'SELECT 1')) AS leak "
        f"FROM `p.d.completions_view` {_VIEW_WHERE}"
    )

    violation = selector.find_lexical_violation(sql)

    assert violation is not None
    assert violation.reason is selector.RejectionReason.FORBIDDEN_KEYWORD


def test_an_escape_sequence_in_a_quoted_identifier_is_refused() -> None:
    """Verifies that `` `\\x63ompletions` `` cannot spell a table the guard misses."""
    sql = (
        "SELECT conversation_id AS target_id "  # noqa: S608 - trusted test constants
        "FROM `p.d.completions_view` JOIN `p.d.\\x63ompletions` USING (conversation_id) "
        f"{_VIEW_WHERE}"
    )

    violation = selector.find_lexical_violation(sql)

    assert violation is not None
    assert violation.reason is selector.RejectionReason.UNAUTHORIZED_TABLE


def test_an_escaped_identifier_never_reaches_bigquery() -> None:
    """Verifies that the escape-sequence refusal precedes the dry runs."""
    client = _dry_run_client(referenced=[_SINK_TABLE, _COMPLETIONS_TABLE])
    fetcher = _logging_fetcher_with(
        client,
        _VIEW_SELECTOR.replace(f"`{_VIEW_REF}`", f"`{_VIEW_REF}\\u0020`"),
    )

    result = fetcher.precheck_selector("agent-a", _START, _END)

    assert result.rejection is not None
    assert (
        result.rejection.reason is selector.RejectionReason.UNAUTHORIZED_TABLE
    )
    client.query.assert_not_called()


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            f"SELECT conversation_id AS target_id FROM `p.d.completions_view` "  # noqa: S608 - trusted test constants
            f"{_VIEW_WHERE} AND AI.IF(('Didn\\'t the user say \"can't\"?', text))",
            id="apostrophes-in-literals",
        ),
        pytest.param(
            f"SELECT conversation_id AS target_id FROM `p.d.completions_view` "  # noqa: S608 - trusted test constants
            f'{_VIEW_WHERE} AND note = "it\'s `completions` here"',
            id="backticks-in-literal",
        ),
        pytest.param(
            f"SELECT v.conversation_id AS target_id FROM `p`.`d`.`completions_view` "  # noqa: S608 - trusted test constants
            f"AS `v` {_VIEW_WHERE}",
            id="quoted-segments",
        ),
    ],
)
def test_ordinary_quoting_clears_the_lexical_checks(sql: str) -> None:
    """Verifies that real literals and quoted names still pass both checks."""
    assert selector.find_lexical_violation(sql) is None
    assert _view_table_violation(sql) is None


@pytest.mark.parametrize(
    ("sql", "valid"),
    [
        pytest.param(
            "SELECT COUNT(DISTINCT span_id) AS completions FROM `p.d.completions_view`",
            True,
            id="alias-named-like-a-table",
        ),
        pytest.param(
            "SELECT x FROM `completions.d.completions_view`",
            True,
            id="project-named-like-a-table",
        ),
        pytest.param(
            "SELECT x FROM `p.d`.completions_view", True, id="split-dataset"
        ),
        pytest.param(
            "SELECT x FROM `p.d`.completions_view JOIN `p`.`d`.completions USING (y)",
            False,
            id="split-base-table",
        ),
        pytest.param(
            "SELECT x FROM completions_view", False, id="unqualified-view"
        ),
    ],
)
def test_the_table_guard_matches_only_dataset_qualified_references(
    sql: str, valid: bool
) -> None:
    """Verifies that only ``dataset.table`` references count, however quoted."""
    assert (_view_table_violation(sql) is None) is valid


@pytest.mark.parametrize(
    ("sql", "valid"),
    [
        pytest.param(
            "SELECT x FROM `p.completions.completions_view`", True, id="view"
        ),
        pytest.param(
            "SELECT x FROM `p.completions.completions_view` "
            "JOIN `p.completions.completions` USING (y)",
            False,
            id="base-table",
        ),
    ],
)
def test_a_dataset_named_like_a_footprint_table_is_not_a_reference(
    sql: str, valid: bool
) -> None:
    """Verifies that a dataset segment equal to a table ID does not name that table."""
    rejection = selector.find_table_violation(
        sql,
        dataset="completions",
        selector_table_id=selector.CLOUD_LOGGING_SELECTOR_VIEW,
        other_table_ids=frozenset(
            {"sink", selector.CLOUD_LOGGING_VIEW_BASE_TABLE}
        ),
    )

    assert (rejection is None) is valid
