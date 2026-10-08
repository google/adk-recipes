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

"""The boot-time schema checks, and the reads underneath them.

The schema tests cover models against Terraform; these cover what a deployment's
tables actually have.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent import startup_checks
from ambient_quality_agent.tools.insights.bigquery_store import (
    BigQueryInsightStore,
)
from ambient_quality_agent.tools.investigations.bigquery_store import (
    BigQueryInvestigationStore,
)
from ambient_quality_agent.tools.investigations.models import (
    list_snapshot_columns,
)


@dataclasses.dataclass
class _Field:
    """One column of a BigQuery table's schema, as `get_table` reports it."""

    name: str


def _table_with(columns: list[str]) -> mock.MagicMock:
    table = mock.MagicMock()
    table.schema = [_Field(name) for name in columns]
    return table


def _investigation_store(get_table: Any) -> BigQueryInvestigationStore:
    client = mock.MagicMock()
    client.get_table = get_table
    return BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )


def _insight_store(get_table: Any) -> BigQueryInsightStore:
    client = mock.MagicMock()
    client.get_table = get_table
    return BigQueryInsightStore(
        client=client, project_id="p", dataset="d", agent_name="a"
    )


# --- what the stores report ------------------------------------------------ #


def test_a_table_with_every_column_reports_nothing_missing() -> None:
    store = _investigation_store(
        lambda *_a, **_k: _table_with(list_snapshot_columns())
    )

    assert store.list_missing_columns() == []


def test_a_column_the_table_lacks_is_named() -> None:
    """What this exists to catch: every write touching that column fails."""
    present = [c for c in list_snapshot_columns() if c != "counters"]
    store = _investigation_store(lambda *_a, **_k: _table_with(present))

    assert store.list_missing_columns() == ["counters"]


def test_a_column_the_table_has_and_the_model_does_not_is_not_drift() -> None:
    """Somebody else's addition, not drift: reporting it would be noise."""
    store = _investigation_store(
        lambda *_a, **_k: _table_with(
            [*list_snapshot_columns(), "someone_elses_column"]
        )
    )

    assert store.list_missing_columns() == []


def test_an_unreachable_table_is_none_rather_than_clean() -> None:
    """`[]` would report a table nobody could read as one found complete."""

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no such dataset")

    assert _investigation_store(explode).list_missing_columns() is None


def test_each_insight_table_is_reported_on_its_own() -> None:
    """One unreachable table must not hide whether the other has drifted."""

    def one_table_only(ref: str, **_k: Any) -> Any:
        if ref.endswith("insights"):
            return _table_with(["insight_id"])
        raise RuntimeError("occurrences are not there yet")

    missing = _insight_store(one_table_only).list_missing_columns()

    assert missing["insight_occurrences"] is None
    assert "label" in (missing["insights"] or [])


# --- what the checks log --------------------------------------------------- #


def test_a_missing_column_is_logged_as_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = _investigation_store(
        lambda *_a, **_k: _table_with(
            [c for c in list_snapshot_columns() if c != "counters"]
        )
    )
    monkeypatch.setattr(
        BigQueryInvestigationStore,
        "from_config",
        classmethod(lambda _cls, _cfg: store),
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(startup_checks.check_investigations_schema())

    assert "counters" in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_an_unreachable_table_does_not_read_as_drift(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Local dev has neither dataset nor credentials; not a fault to report."""

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no credentials here")

    store = _investigation_store(explode)
    monkeypatch.setattr(
        BigQueryInvestigationStore,
        "from_config",
        classmethod(lambda _cls, _cfg: store),
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(startup_checks.check_investigations_schema())

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "skipping the schema check" in caplog.text


def test_a_check_that_cannot_even_build_its_store_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It runs in the lifespan: raising would stop the container serving."""

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no config")

    monkeypatch.setattr(
        BigQueryInvestigationStore, "from_config", classmethod(explode)
    )
    monkeypatch.setattr(startup_checks, "_build_insight_store", explode)

    asyncio.run(startup_checks.check_investigations_schema())
    asyncio.run(startup_checks.check_insights_schema())


def test_the_lifespan_runs_both_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check nothing invokes reads as coverage and reports nothing."""
    from ambient_quality_agent import fast_api_app
    from fastapi import FastAPI

    ran: list[str] = []

    async def record(name: str) -> None:
        await asyncio.sleep(0)  # a real check awaits a thread; so does this
        ran.append(name)

    monkeypatch.setattr(
        fast_api_app,
        "check_investigations_schema",
        lambda: record("investigations"),
    )
    monkeypatch.setattr(
        fast_api_app, "check_insights_schema", lambda: record("insights")
    )
    # Everything the lifespan does after the checks, stubbed out.
    monkeypatch.setattr(fast_api_app, "attach_a2a_routes", mock.AsyncMock())
    monkeypatch.setattr(
        fast_api_app.chat_agent, "build_chat_agent", mock.MagicMock()
    )
    monkeypatch.setattr(fast_api_app, "_build_task_store", mock.MagicMock())
    monkeypatch.setattr(fast_api_app, "App", mock.MagicMock())
    monkeypatch.setattr(fast_api_app, "Runner", mock.MagicMock())
    monkeypatch.setattr(
        fast_api_app.services, "get_session_service", mock.MagicMock()
    )
    monkeypatch.setattr(
        fast_api_app.services, "get_artifact_service", mock.MagicMock()
    )

    async def drive() -> None:
        async with fast_api_app._lifespan(FastAPI()):
            pass

    asyncio.run(drive())

    assert ran == ["investigations", "insights"]
