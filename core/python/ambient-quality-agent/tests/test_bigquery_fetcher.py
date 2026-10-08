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

"""Tests for BigQueryFetcher."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.agent_revision import (
    UNRECORDED_PARAMETERS,
    AgentRevisionCache,
)
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.base import (
    AGENT_COUNT_LIMIT,
    BaseFetcher,
)
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.review import session_review
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from google.cloud import bigquery

from .conftest import FakeTrajectoryRecorder

_PROJECT = "test-project"
_DATASET = "agent_analytics"
_TABLE = "agent_events"
_LOCATION = "us-central1"
_TABLE_REF = f"{_PROJECT}.{_DATASET}.{_TABLE}"

_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)

_OBSERVED_AGENT = "COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent)"


def _multi_turn_row() -> dict[str, Any]:
    """Builds a test row for a multi-turn session covering mapped event types.

    Returns:
        A dictionary formatted as a BigQuery row.
    """
    return {
        "session_id": "sess-1",
        "events": [
            {
                "invocation_id": "inv-1",
                "event_type": "AGENT_STARTING",
                "agent": "weather-agent",
                "content": "You are a helpful weather agent.",
            },
            {
                "invocation_id": "inv-1",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "weather-agent",
                "content": {"text_summary": "What's the weather?"},
            },
            {
                "invocation_id": "inv-1",
                "event_type": "TOOL_STARTING",
                "agent": "weather-agent",
                "content": {"tool": "get_weather", "args": {"city": "NYC"}},
            },
            {
                "invocation_id": "inv-1",
                "event_type": "TOOL_COMPLETED",
                "agent": "weather-agent",
                "content": {"tool": "get_weather", "result": {"temp": 72}},
            },
            {
                "invocation_id": "inv-1",
                "event_type": "AGENT_RESPONSE",
                "agent": "weather-agent",
                "content": {"response": "It is 72F."},
            },
            {
                "invocation_id": "inv-2",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "weather-agent",
                "content": {"text_summary": "Thanks!"},
            },
        ],
    }


def _multi_agent_row() -> dict[str, Any]:
    """Builds a test row for a multi-agent session where root delegates to subagent.

    Returns:
        A dictionary formatted as a BigQuery row.
    """
    return {
        "session_id": "sess-2",
        "events": [
            {
                "invocation_id": "inv-1",
                "event_type": "AGENT_STARTING",
                "agent": "root-agent",
                "content": "Root instruction.",
            },
            {
                "invocation_id": "inv-1",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "root-agent",
                "content": {"text_summary": "Plan my trip."},
            },
            {
                "invocation_id": "inv-1",
                "event_type": "AGENT_STARTING",
                "agent": "flights-subagent",
                "content": "Flights sub-agent instruction.",
            },
            {
                "invocation_id": "inv-1",
                "event_type": "AGENT_RESPONSE",
                "agent": "flights-subagent",
                "content": {"response": "Found flights."},
            },
            {
                "invocation_id": "inv-1",
                "event_type": "AGENT_RESPONSE",
                "agent": "root-agent",
                "content": {"response": "Here is your plan."},
            },
        ],
    }


def _single_turn_row() -> dict[str, Any]:
    return {
        "invocation_id": "inv-9",
        "events": [
            {
                "event_type": "AGENT_STARTING",
                "agent": "weather-agent",
                "content": "System prompt here.",
            },
            {
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "weather-agent",
                "content": {"text_summary": "Is it raining?"},
            },
            {
                "event_type": "TOOL_STARTING",
                "agent": "weather-agent",
                "content": {"tool": "get_rain", "args": {"city": "LA"}},
            },
            {
                "event_type": "TOOL_COMPLETED",
                "agent": "weather-agent",
                "content": {"tool": "get_rain", "result": {"raining": False}},
            },
            {
                "event_type": "AGENT_RESPONSE",
                "agent": "weather-agent",
                "content": {"response": "No rain today."},
            },
        ],
    }


def _make_fetcher(
    client: mock.MagicMock, recorder: Any | None = None, selector_sql: str = ""
) -> BigQueryFetcher:
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=client,
    ):
        return BigQueryFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location=_LOCATION,
            recorder=recorder,
            selector_sql=selector_sql,
        )


def test_bigquery_fetcher_is_a_base_fetcher() -> None:
    """BigQueryFetcher conforms to the common BaseFetcher interface."""
    assert issubclass(BigQueryFetcher, BaseFetcher)
    assert isinstance(_make_fetcher(mock.MagicMock()), BaseFetcher)


def _row(values: dict[str, Any]) -> mock.MagicMock:
    row = mock.MagicMock()
    row.items.return_value = values.items()
    return row


def _configure_paging(
    client: mock.MagicMock,
    *,
    page_rows: list[dict[str, Any]],
    next_page_token: str | None,
) -> None:
    """Configures a mock client for a single fetch_page call.

    Args:
        client: Mock BigQuery client.
        page_rows: Row dictionaries to return in the page.
        next_page_token: Token for the next page, or None if exhausted.
    """
    query_job = mock.MagicMock(spec=bigquery.QueryJob)
    query_job.destination = "dest-table-ref"
    client.get_job.return_value = query_job

    row_iter = mock.MagicMock()
    row_iter.pages = iter([[_row(row_values) for row_values in page_rows]])
    row_iter.next_page_token = next_page_token
    client.list_rows.return_value = row_iter


@pytest.mark.parametrize(
    ("metric_type", "expected_keyword"),
    [
        (MetricType.MULTI_TURN, "TargetSessions"),
        (MetricType.SINGLE_TURN, "TargetInvocations"),
    ],
)
def test_submit_query_selects_sql_by_metric_type(
    metric_type: MetricType, expected_keyword: str
) -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-123"
    fetcher = _make_fetcher(client)

    job_id = fetcher.submit_query(
        "my-agent", metric_type, _START, _END, limit=50
    )

    assert job_id == "job-123"
    sql = client.query.call_args.args[0]
    assert expected_keyword in sql
    assert _TABLE_REF in sql


def test_submit_query_binds_parameters() -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.SINGLE_TURN, _START, _END, limit=7
    )

    job_config = client.query.call_args.kwargs["job_config"]
    params = {param.name: param.value for param in job_config.query_parameters}
    assert params == {
        "agent_name": "agent-a",
        "window_start": _START,
        "window_end": _END,
        "limit": 7,
    }


def test_fetch_page_does_not_constrain_page_size() -> None:
    """``fetch_page`` trusts BigQuery's ``list_rows`` to pick a page
    size; it must not pass ``max_results``.
    """
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"session_id": "s1"}],
        next_page_token="tok-2",
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    # No max_results in the list_rows call.
    assert "max_results" not in client.list_rows.call_args.kwargs
    # Destination is the only positional arg.
    assert client.list_rows.call_args.args == ("dest-table-ref",)
    assert len(page.dataset.eval_cases) == 1
    assert page.next_page_token == "tok-2"
    assert page.is_exhausted is False


def test_fetch_page_passes_page_token() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"a": 1}],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    fetcher.fetch_page("job-1", MetricType.MULTI_TURN, page_token="resume-here")

    assert client.list_rows.call_args.kwargs["page_token"] == "resume-here"


def test_fetch_page_exhausted_when_no_next_token() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"a": 1}],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert page.next_page_token is None
    assert page.is_exhausted is True


def test_fetch_page_stops_early_on_memory_guard() -> None:
    """The generic base guard truncates the page (covers the big_query path)."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[_multi_turn_row(), _multi_turn_row()],
        next_page_token="tok-next",
    )
    fetcher = _make_fetcher(client)
    # Fire the guard before the second row.
    calls = {"n": 0}

    def guard() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    fetcher._memory_guard = guard

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert len(page.dataset.eval_cases) == 1
    assert page.memory_truncated is True
    assert page.next_page_token is None


def test_fetch_page_empty_destination_returns_no_cases() -> None:
    """An empty destination table yields an empty, exhausted page."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert page.dataset.eval_cases == []
    assert page.next_page_token is None
    assert page.is_exhausted is True


def test_fetch_page_raises_without_destination() -> None:
    client = mock.MagicMock()
    query_job = mock.MagicMock(spec=bigquery.QueryJob)
    query_job.destination = None
    client.get_job.return_value = query_job
    fetcher = _make_fetcher(client)

    with pytest.raises(ValueError, match="no destination"):
        fetcher.fetch_page("job-1", MetricType.MULTI_TURN)


def test_map_page_logs_progress_every_100_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Progress is logged at each 100-row boundary and once at the last row."""
    fetcher = _make_fetcher(mock.MagicMock())
    rows = [_multi_turn_row() for _ in range(250)]

    with caplog.at_level(logging.INFO):
        fetcher._map_page(rows, MetricType.MULTI_TURN)

    progress = [
        r.getMessage()
        for r in caplog.records
        if "Mapping row" in r.getMessage()
    ]
    assert progress == [
        "Mapping row 100/250.",
        "Mapping row 200/250.",
        "Mapping row 250/250.",
    ]


def test_map_page_logs_progress_once_for_small_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A page under 100 rows still reports once, at the final row."""
    fetcher = _make_fetcher(mock.MagicMock())
    rows = [_multi_turn_row() for _ in range(10)]

    with caplog.at_level(logging.INFO):
        fetcher._map_page(rows, MetricType.MULTI_TURN)

    progress = [
        r.getMessage()
        for r in caplog.records
        if "Mapping row" in r.getMessage()
    ]
    assert progress == ["Mapping row 10/10."]


def test_map_multi_turn_events() -> None:
    fetcher = _make_fetcher(mock.MagicMock())

    cases, _ = fetcher._map_page([_multi_turn_row()], MetricType.MULTI_TURN)

    assert len(cases) == 1
    # The case is tagged with the source ``session_id`` so the summary
    # can later attribute failures back to a concrete BigQuery row.
    assert cases[0].eval_case_id == "sess-1"
    agent_data = cases[0].agent_data

    # Two invocations -> two turns, in order.
    assert [turn.turn_id for turn in agent_data.turns] == ["inv-1", "inv-2"]
    assert [turn.turn_index for turn in agent_data.turns] == [0, 1]

    first_turn_events = agent_data.turns[0].events

    # USER_MESSAGE_RECEIVED -> author "user", text part.
    user_event = first_turn_events[0]
    assert user_event.author == "user"
    assert user_event.content.parts[0].text == "What's the weather?"

    # TOOL_STARTING -> function_call part authored by the agent.
    tool_starting_event = first_turn_events[1]
    assert tool_starting_event.author == "weather-agent"
    assert (
        tool_starting_event.content.parts[0].function_call.name == "get_weather"
    )
    assert tool_starting_event.content.parts[0].function_call.args == {
        "city": "NYC"
    }

    # TOOL_COMPLETED -> function_response part.
    tool_completed_event = first_turn_events[2]
    assert (
        tool_completed_event.content.parts[0].function_response.name
        == "get_weather"
    )
    assert tool_completed_event.content.parts[0].function_response.response == {
        "temp": 72
    }

    # AGENT_RESPONSE -> text part authored by the agent.
    agent_response = first_turn_events[3]
    assert agent_response.author == "weather-agent"
    assert agent_response.content.parts[0].text == "It is 72F."


def test_tool_error_maps_error_message_column() -> None:
    """A TOOL_ERROR event surfaces its ``error_message`` column value."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = {
        "session_id": "sess-err",
        "events": [
            {
                "invocation_id": "inv-1",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "it-agent",
                "content": {"text_summary": "file a ticket"},
            },
            {
                "invocation_id": "inv-1",
                "event_type": "TOOL_STARTING",
                "agent": "it-agent",
                "content": {
                    "tool": "create_ticket",
                    "args": {"priority": "Urgent"},
                },
            },
            {
                "invocation_id": "inv-1",
                "event_type": "TOOL_ERROR",
                "agent": "it-agent",
                # The error text lives on the column, not inside content.
                "content": {
                    "tool": "create_ticket",
                    "args": {"priority": "Urgent"},
                },
                "error_message": "Invalid priority level.",
            },
        ],
    }

    case = fetcher._map_page([row], MetricType.MULTI_TURN)[0][0]
    parts = [
        p for e in case.agent_data.turns[0].events for p in e.content.parts
    ]
    error_responses = [
        p.function_response for p in parts if p.function_response is not None
    ]
    assert len(error_responses) == 1
    assert error_responses[0].name == "create_ticket"
    assert error_responses[0].response == {"error": "Invalid priority level."}


_ERROR_EVENT_TYPES = {
    "LLM_ERROR",
    "TOOL_ERROR",
    "AGENT_ERROR",
    "INVOCATION_ERROR",
}

# A made-up traceback, shaped like the one the plugin writes on AGENT_ERROR.
_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/agent.py", line 12, in run\n'
    "    raise ValueError('quota exhausted')\n"
    "ValueError: quota exhausted\n"
    "\n"
)


def _parse_event_type_filter(sql: str) -> list[str]:
    """Parse the event types an ingestion query selects.

    Args:
        sql: Full ingestion SQL query text.

    Returns:
        The quoted values of the ``event_type IN (...)`` list, in order.
    """
    in_list = sql.split("e.event_type IN (", 1)[1].split(")", 1)[0]
    return re.findall(r"'([A-Z0-9_]+)'", in_list)


def _build_error_event(
    event_type: str,
    agent: str,
    error_message: str | None,
    content: dict[str, Any] | None = None,
    invocation_id: str = "inv-1",
) -> dict[str, Any]:
    """Build an error row as projected by the ingestion queries.

    Args:
        event_type: The error event type.
        agent: Agent that logged the row.
        error_message: Value of the ``error_message`` column.
        content: Value of the ``content`` column.
        invocation_id: Invocation the row belongs to.

    Returns:
        An error event dictionary.
    """
    return {
        "invocation_id": invocation_id,
        "event_type": event_type,
        "agent": agent,
        "content": content,
        "error_message": error_message,
    }


def _build_session_row(*events: dict[str, Any]) -> dict[str, Any]:
    """Build a multi-turn row whose one turn opens with a user message.

    Args:
        *events: Rows to append after the user message.

    Returns:
        A dictionary formatted as a BigQuery row.
    """
    return {
        "session_id": "sess-err",
        "events": [
            {
                "invocation_id": "inv-1",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "root-agent",
                "content": {"text_summary": "Book a table for two."},
            },
            *events,
        ],
    }


def _list_function_responses(case: Any) -> list[Any]:
    """List the ``function_response`` events of a case.

    Args:
        case: Mapped evaluation case.

    Returns:
        The events whose part carries a ``function_response``, in order.
    """
    return [
        event
        for turn in case.agent_data.turns
        for event in turn.events
        if event.content.parts[0].function_response is not None
    ]


def test_both_queries_select_every_error_event_type() -> None:
    """A type missing from either query never reaches the case."""
    fetcher = _make_fetcher(mock.MagicMock())

    multi_turn = _parse_event_type_filter(
        fetcher._build_ingestion_sql(MetricType.MULTI_TURN)
    )
    single_turn = _parse_event_type_filter(
        fetcher._build_ingestion_sql(MetricType.SINGLE_TURN)
    )

    assert multi_turn == single_turn
    assert _ERROR_EVENT_TYPES <= set(multi_turn)


@pytest.mark.parametrize(
    ("event_type", "name"),
    [
        ("LLM_ERROR", "llm_error"),
        ("AGENT_ERROR", "agent_error"),
        ("INVOCATION_ERROR", "invocation_error"),
    ],
)
def test_a_runtime_error_becomes_an_error_response(
    event_type: str, name: str
) -> None:
    """The runtime, not the model, recorded the failure, so it takes the
    ``user`` role while staying attributed to the agent that failed."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _build_session_row(
        _build_error_event(event_type, "booking-agent", "503 UNAVAILABLE.")
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    [event] = _list_function_responses(case)
    assert event.content.parts[0].function_response.name == name
    assert event.content.parts[0].function_response.response == {
        "error": "503 UNAVAILABLE."
    }
    assert event.author == "booking-agent"
    assert event.content.role == "user"


@pytest.mark.parametrize("error_message", ["", None])
def test_an_llm_error_without_a_message_reports_error(
    error_message: str | None,
) -> None:
    """A timeout can carry no message and LLM_ERROR has no content to fall
    back on; the event must still say that something failed."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _build_session_row(
        _build_error_event("LLM_ERROR", "root-agent", error_message)
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    [event] = _list_function_responses(case)
    assert event.content.parts[0].function_response.response == {
        "error": "error"
    }


@pytest.mark.parametrize("event_type", ["AGENT_ERROR", "INVOCATION_ERROR"])
def test_an_agent_error_without_a_message_reports_the_last_traceback_line(
    event_type: str,
) -> None:
    """The last line names the exception; the rest holds file paths."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _build_session_row(
        _build_error_event(
            event_type, "root-agent", "", {"error_traceback": _TRACEBACK}
        )
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    [event] = _list_function_responses(case)
    assert event.content.parts[0].function_response.response == {
        "error": "ValueError: quota exhausted"
    }
    assert "Traceback" not in case.model_dump_json()


def test_a_tool_error_without_a_message_reports_error() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    row = _build_session_row(
        _build_error_event(
            "TOOL_ERROR", "root-agent", None, {"tool": "book_table"}
        )
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    [event] = _list_function_responses(case)
    assert event.content.parts[0].function_response.name == "book_table"
    assert event.content.parts[0].function_response.response == {
        "error": "error"
    }


@pytest.mark.parametrize(
    ("event_type", "content", "name"),
    [
        ("LLM_ERROR", None, "llm_error"),
        ("TOOL_ERROR", {"tool": "book_table"}, "book_table"),
    ],
)
def test_a_propagated_error_is_shown_once(
    event_type: str, content: dict[str, Any] | None, name: str
) -> None:
    """ADK logs one exception once for each level it escapes; only the row
    that started it is kept."""
    fetcher = _make_fetcher(mock.MagicMock())
    message = "429 RESOURCE_EXHAUSTED."
    row = _build_session_row(
        _build_error_event(event_type, "worker-agent", message, content),
        _build_error_event("AGENT_ERROR", "worker-agent", message),
        _build_error_event("AGENT_ERROR", "root-agent", message),
        _build_error_event("INVOCATION_ERROR", "root-agent", message),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    [event] = _list_function_responses(case)
    assert event.content.parts[0].function_response.name == name
    assert event.author == "worker-agent"


def test_a_propagated_error_is_shown_once_in_a_single_turn_case() -> None:
    """The single-turn query selects no ``invocation_id``, so every row of the
    turn shares one chain."""
    fetcher = _make_fetcher(mock.MagicMock())
    message = "429 RESOURCE_EXHAUSTED."
    events = _build_session_row(
        _build_error_event("LLM_ERROR", "worker-agent", message),
        _build_error_event("AGENT_ERROR", "root-agent", message),
        _build_error_event("INVOCATION_ERROR", "root-agent", message),
    )["events"]
    for event in events:
        del event["invocation_id"]

    case = fetcher._build_case(
        {"invocation_id": "inv-1", "events": events}, MetricType.SINGLE_TURN
    )

    [event] = _list_function_responses(case)
    assert event.content.parts[0].function_response.name == "llm_error"


def test_a_propagated_error_is_tracked_per_invocation() -> None:
    """Rows of another invocation that land inside a chain do not end it."""
    fetcher = _make_fetcher(mock.MagicMock())
    message = "429 RESOURCE_EXHAUSTED."
    row = _build_session_row(
        _build_error_event("LLM_ERROR", "worker-agent", message),
        {
            "invocation_id": "inv-2",
            "event_type": "USER_MESSAGE_RECEIVED",
            "agent": "root-agent",
            "content": {"text_summary": "Make it three."},
        },
        _build_error_event("AGENT_ERROR", "root-agent", message),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert [
        event.content.parts[0].function_response.name
        for event in _list_function_responses(case)
    ] == ["llm_error"]


def test_a_new_error_after_a_handled_one_is_kept() -> None:
    """A row between two errors, here the LLM_REQUEST after a callback handled
    the first, makes the second a new failure even with the same message."""
    fetcher = _make_fetcher(mock.MagicMock())
    message = "Connection reset."
    row = _build_session_row(
        _build_error_event(
            "TOOL_ERROR", "root-agent", message, {"tool": "book"}
        ),
        _llm_request("inv-1", "root-agent", "Book tables."),
        _build_error_event("AGENT_ERROR", "root-agent", message),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert [
        event.content.parts[0].function_response.name
        for event in _list_function_responses(case)
    ] == ["book", "agent_error"]


def test_an_error_with_a_different_message_is_kept() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    row = _build_session_row(
        _build_error_event("LLM_ERROR", "root-agent", "Deadline exceeded."),
        _build_error_event("AGENT_ERROR", "root-agent", "Callback failed."),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert [
        event.content.parts[0].function_response.response
        for event in _list_function_responses(case)
    ] == [{"error": "Deadline exceeded."}, {"error": "Callback failed."}]


def _build_model_error_events(
    invocation_id: str | None,
) -> list[dict[str, Any]]:
    """Build a made-up delegation whose worker fails on its first model call.

    Args:
        invocation_id: Invocation id to set on each row, or None to omit it,
            as the single-turn query does.

    Returns:
        Chronological event rows.
    """
    events: list[dict[str, Any]] = [
        {
            "event_type": "AGENT_STARTING",
            "agent": "root-agent",
            "content": "Route each request to the right agent.",
        },
        {
            "event_type": "USER_MESSAGE_RECEIVED",
            "agent": "root-agent",
            "content": {"text_summary": "Track parcel 0001."},
        },
        {
            "event_type": "TOOL_STARTING",
            "agent": "root-agent",
            "content": {
                "tool": "transfer_to_agent",
                "args": {"agent_name": "tracking-agent"},
            },
        },
        {
            "event_type": "TOOL_COMPLETED",
            "agent": "root-agent",
            "content": {"tool": "transfer_to_agent", "result": None},
        },
        {
            "event_type": "AGENT_STARTING",
            "agent": "tracking-agent",
            "content": "Look up parcel status.",
        },
        {
            "event_type": "LLM_REQUEST",
            "agent": "tracking-agent",
            "content": None,
            "system_prompt": "Look up parcel status.",
        },
        {
            "event_type": "LLM_ERROR",
            "agent": "tracking-agent",
            "content": None,
            "error_message": (
                "400 INVALID_ARGUMENT. Request contains an invalid argument."
            ),
        },
    ]
    if invocation_id is not None:
        for event in events:
            event["invocation_id"] = invocation_id
    return events


@pytest.mark.parametrize(
    ("metric_type", "row"),
    [
        (
            MetricType.MULTI_TURN,
            {
                "session_id": "sess-1",
                "events": _build_model_error_events("inv-1"),
            },
        ),
        (
            MetricType.SINGLE_TURN,
            {
                "invocation_id": "inv-1",
                "events": _build_model_error_events(None),
            },
        ),
    ],
)
def test_a_session_cut_short_by_a_model_error_ends_with_the_error(
    metric_type: MetricType, row: dict[str, Any]
) -> None:
    """Without the error event the turn just stops, and the reviewer blames
    the agent for an answer it never gave."""
    fetcher = _make_fetcher(mock.MagicMock())

    case = fetcher._build_case(row, metric_type)

    last = case.agent_data.turns[-1].events[-1]
    assert last.author == "tracking-agent"
    assert last.content.parts[0].function_response.name == "llm_error"
    assert last.content.parts[0].function_response.response == {
        "error": "400 INVALID_ARGUMENT. Request contains an invalid argument."
    }
    assert all(
        event.content.parts[0].model_dump(exclude_none=True)
        for turn in case.agent_data.turns
        for event in turn.events
    )


def test_agent_starting_is_not_a_turn_event() -> None:
    """It carries configuration, and an unmapped event would reach the
    reviewer as an empty message."""
    fetcher = _make_fetcher(mock.MagicMock())

    turns = fetcher._build_case(
        _multi_agent_row(), MetricType.MULTI_TURN
    ).agent_data.turns

    assert [event.content.parts[0].text for event in turns[0].events] == [
        "Plan my trip.",
        "Found flights.",
        "Here is your plan.",
    ]


def test_map_multi_turn_with_sub_agents_row() -> None:
    fetcher = _make_fetcher(mock.MagicMock())

    case = fetcher._map_page([_multi_agent_row()], MetricType.MULTI_TURN)[0][0]
    agent_data = case.agent_data

    events = agent_data.turns[0].events
    assert [event.author for event in events] == [
        "user",
        "flights-subagent",
        "root-agent",
    ]

    # Every distinct agent is recorded with its own instruction.
    assert set(agent_data.agents) == {"root-agent", "flights-subagent"}
    assert agent_data.agents["root-agent"].instruction == "Root instruction."
    assert (
        agent_data.agents["flights-subagent"].instruction
        == "Flights sub-agent instruction."
    )


def test_map_single_turn_structure() -> None:
    fetcher = _make_fetcher(mock.MagicMock())

    cases, _ = fetcher._map_page([_single_turn_row()], MetricType.SINGLE_TURN)

    assert len(cases) == 1
    case = cases[0]
    # The case is tagged with the source ``invocation_id`` so the
    # summary can later attribute failures back to a concrete row.
    assert case.eval_case_id == "inv-9"
    agent_data = case.agent_data
    assert len(agent_data.turns) == 1
    turn = agent_data.turns[0]
    assert turn.turn_index == 0
    assert turn.turn_id == "inv-9"
    authors = [event.author for event in turn.events]
    assert authors == [
        "user",
        "weather-agent",
        "weather-agent",
        "weather-agent",
    ]
    assert turn.events[0].content.parts[0].text == "Is it raining?"
    assert turn.events[1].content.parts[0].function_call.name == "get_rain"
    assert turn.events[2].content.parts[0].function_response.response == {
        "raining": False
    }
    assert (
        agent_data.agents["weather-agent"].instruction == "System prompt here."
    )


def test_map_single_turn_includes_all_agents_events() -> None:
    row = {
        "invocation_id": "inv-sub",
        "events": [
            {
                "event_type": "AGENT_STARTING",
                "agent": "root-agent",
                "content": "Root instruction.",
            },
            {
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "root-agent",
                "content": {"text_summary": "Plan trip."},
            },
            {
                "event_type": "AGENT_STARTING",
                "agent": "sub-agent",
                "content": "Sub instruction.",
            },
            {
                "event_type": "AGENT_RESPONSE",
                "agent": "sub-agent",
                "content": {"response": "Sub answer."},
            },
            {
                "event_type": "AGENT_RESPONSE",
                "agent": "root-agent",
                "content": {"response": "Final answer."},
            },
        ],
    }
    fetcher = _make_fetcher(mock.MagicMock())

    case = fetcher._map_page([row], MetricType.SINGLE_TURN)[0][0]
    agent_data = case.agent_data

    assert set(agent_data.agents) == {"root-agent", "sub-agent"}
    assert agent_data.agents["root-agent"].instruction == "Root instruction."
    assert agent_data.agents["sub-agent"].instruction == "Sub instruction."

    events = agent_data.turns[0].events
    response_authors = [
        event.author
        for event in events
        if event.content.parts[0].text in {"Sub answer.", "Final answer."}
    ]
    assert response_authors == ["sub-agent", "root-agent"]


def test_map_rows_empty_input() -> None:
    fetcher = _make_fetcher(mock.MagicMock())

    assert fetcher._map_page([], MetricType.MULTI_TURN) == ([], {})
    assert fetcher._map_page([], MetricType.SINGLE_TURN) == ([], {})


# --- counting and ingestion tallies -------------------------------------------


@pytest.mark.parametrize(
    ("metric_type", "unit_column"),
    [
        (MetricType.MULTI_TURN, "session_id"),
        (MetricType.SINGLE_TURN, "invocation_id"),
    ],
)
def test_count_scanned_is_the_sampling_query_without_the_sampling(
    metric_type: MetricType, unit_column: str
) -> None:
    """The counted population has to be the sampled one, or the ratio the
    dashboard shows compares two different things."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 42}]
    fetcher = _make_fetcher(client)

    scanned = fetcher.count_scanned("agent-a", metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    assert "COUNT(*) AS scanned" in sql
    assert f"GROUP BY {unit_column}" in sql
    # No downsampling, and so no limit to bind.
    assert "ORDER BY RAND()" not in sql and "LIMIT" not in sql
    params = {
        p.name: p.value
        for p in client.query.call_args.kwargs["job_config"].query_parameters
    }
    assert params == {
        "agent_name": "agent-a",
        "window_start": _START,
        "window_end": _END,
    }
    assert scanned == 42
    assert fetcher.counts.scanned == 42


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
@pytest.mark.parametrize("sql_getter", ["submit_query", "count_scanned"])
def test_the_default_targets_match_the_root_agent_not_the_row_author(
    metric_type: MetricType, sql_getter: str
) -> None:
    """When the Runner starts at a sub-agent, no row names the root in `agent`;
    only `attributes.root_agent_name` does."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    client.query.return_value.result.return_value = [{"scanned": 0}]
    fetcher = _make_fetcher(client)

    if sql_getter == "submit_query":
        fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)
    else:
        fetcher.count_scanned("agent-a", metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    assert f"{_OBSERVED_AGENT} = @agent_name" in sql
    assert re.search(r"\bagent = @agent_name", sql) is None


@pytest.mark.parametrize(
    ("metric_type", "unit_column"),
    [
        (MetricType.MULTI_TURN, "session_id"),
        (MetricType.SINGLE_TURN, "invocation_id"),
    ],
)
def test_count_by_agent_counts_the_same_unit_for_every_agent(
    metric_type: MetricType, unit_column: str
) -> None:
    """The names an empty window's error offers: the population `count_scanned`
    counts, grouped by agent instead of filtered on one."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"agent_name": "weather-agent", "scanned": 9},
        {"agent_name": "news-agent", "scanned": 2},
    ]
    fetcher = _make_fetcher(client)

    counts = fetcher.count_by_agent(metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    assert f"COUNT(DISTINCT {unit_column}) AS scanned" in sql
    assert "GROUP BY agent_name" in sql
    assert f"{_OBSERVED_AGENT} AS agent_name" in sql
    assert f"{_OBSERVED_AGENT} IS NOT NULL" in sql
    assert "@agent_name" not in sql
    assert f"LIMIT {AGENT_COUNT_LIMIT}" in sql
    assert "ORDER BY scanned DESC, agent_name" in sql
    params = {
        p.name: p.value
        for p in client.query.call_args.kwargs["job_config"].query_parameters
    }
    assert params == {"window_start": _START, "window_end": _END}
    assert counts == {"weather-agent": 9, "news-agent": 2}
    assert fetcher.counts.scanned == 0, (
        "these traces are not the run's population"
    )


def test_count_by_agent_ignores_the_selector() -> None:
    """The question is which names the telemetry records at all; a selector
    narrows the run, not the table."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    fetcher = _make_fetcher(
        client, selector_sql="SELECT session_id AS target_id FROM agent_events"
    )

    assert fetcher.count_by_agent(MetricType.MULTI_TURN, _START, _END) == {}
    assert "target_id" not in client.query.call_args.args[0]


def test_a_counted_zero_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sweep that follows reports a clean run over nothing, which reads the
    same whether the agent was idle or the configured name is wrong. This is the
    one log line that knows the window was empty, so it has to be findable."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 0}]
    fetcher = _make_fetcher(client)

    with caplog.at_level(logging.WARNING):
        fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)

    assert [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ] == [
        "MULTI_TURN window holds 0 trace(s) for agent 'agent-a' before sampling."
    ]


def test_a_nonzero_count_stays_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A window with traces in it is the ordinary case and must not page anyone."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 3}]
    fetcher = _make_fetcher(client)

    with caplog.at_level(logging.DEBUG):
        fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_count_scanned_accumulates_across_the_two_scopes() -> None:
    """Both scopes count through one run's counters, so the second adds to the
    first rather than replacing it."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 5}]
    fetcher = _make_fetcher(client)

    fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)
    fetcher.count_scanned("agent-a", MetricType.SINGLE_TURN, _START, _END)

    assert fetcher.counts.scanned == 10


def test_map_page_counts_every_row_as_ingested() -> None:
    """The analytics schema carries its content inline: nothing to fail to
    resolve, so every row maps whole."""
    fetcher = _make_fetcher(mock.MagicMock())

    fetcher._map_page(
        [_multi_turn_row(), _multi_turn_row()], MetricType.MULTI_TURN
    )

    assert dataclasses.astuple(fetcher.counts) == (0, 2, 0, 0)


def test_map_page_does_not_count_the_rows_a_memory_stop_left_unread() -> None:
    """Those rows were never attempted, so they belong in no bucket -- counting
    them as dropped would report a mapping failure that never happened."""
    fetcher = _make_fetcher(mock.MagicMock())
    fetcher._memory_guard = lambda: True

    fetcher._map_page([_multi_turn_row()], MetricType.MULTI_TURN)

    assert dataclasses.astuple(fetcher.counts) == (0, 0, 0, 0)


def _llm_request(
    invocation_id: str,
    agent: str,
    system_prompt: str | None,
    tools: Any = None,
) -> dict[str, Any]:
    """Constructs an LLM_REQUEST row dictionary as projected by ingestion queries.

    Args:
        invocation_id: Invocation identifier.
        agent: Agent name.
        system_prompt: Resolved system prompt.
        tools: The ``tools`` projection: a list from a JSON column, JSON text
            from a STRING column, or None when the request logged no tools.

    Returns:
        An LLM_REQUEST event dictionary.
    """
    return {
        "invocation_id": invocation_id,
        "event_type": "LLM_REQUEST",
        "agent": agent,
        "content": None,
        "system_prompt": system_prompt,
        "tools": tools,
    }


def test_instruction_prefers_the_resolved_system_prompt() -> None:
    """AGENT_STARTING holds the instruction as authored; the model was given the
    resolved one, so that is what the judge has to be shown."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        1,
        _llm_request(
            "inv-1", "weather-agent", "Authored.\n<MEMORY>a fact</MEMORY>"
        ),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert (
        case.agent_data.agents["weather-agent"].instruction
        == "Authored.\n<MEMORY>a fact</MEMORY>"
    )


def test_instruction_falls_back_to_agent_starting() -> None:
    """A session without an LLM_REQUEST falls back to the AGENT_STARTING instruction."""
    fetcher = _make_fetcher(mock.MagicMock())

    case = fetcher._build_case(_multi_turn_row(), MetricType.MULTI_TURN)

    assert (
        case.agent_data.agents["weather-agent"].instruction
        == "You are a helpful weather agent."
    )


def test_llm_request_is_not_itself_a_turn_event() -> None:
    """It carries no conversational content, and an unmapped event type would
    reach the judge as an empty one."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    before = len(
        fetcher._build_case(row, MetricType.MULTI_TURN)
        .agent_data.turns[0]
        .events
    )
    row["events"].insert(1, _llm_request("inv-1", "weather-agent", "Resolved."))

    turns = fetcher._build_case(row, MetricType.MULTI_TURN).agent_data.turns

    assert len(turns[0].events) == before


def test_changed_instruction_is_recorded_as_a_state_delta() -> None:
    """`AgentConfig` keeps the first instruction; a later one that differs is
    shown once, on the first event the agent authors under it."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(0, _llm_request("inv-1", "weather-agent", "Be brief."))
    row["events"].append(_llm_request("inv-2", "weather-agent", "Be verbose."))
    row["events"].append(
        {
            "invocation_id": "inv-2",
            "event_type": "AGENT_RESPONSE",
            "agent": "weather-agent",
            "content": {"response": "At length, then."},
        }
    )
    row["events"].append(
        {
            "invocation_id": "inv-2",
            "event_type": "AGENT_RESPONSE",
            "agent": "weather-agent",
            "content": {"response": "And more."},
        }
    )

    turns = fetcher._build_case(row, MetricType.MULTI_TURN).agent_data.turns

    assert turns[0].events[0].state_delta is None
    assert turns[1].events[-2].state_delta == {
        "system_instruction": "Be verbose."
    }
    assert turns[1].events[-1].state_delta is None


def test_instruction_equal_to_baseline_carries_no_state_delta() -> None:
    """The review prompt already shows the baseline in the agent block, so an
    instruction that never moved off it is not repeated on any event."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(0, _llm_request("inv-1", "weather-agent", "Be brief."))
    row["events"].append(_llm_request("inv-2", "weather-agent", "Be brief."))
    row["events"].append(
        {
            "invocation_id": "inv-2",
            "event_type": "AGENT_RESPONSE",
            "agent": "weather-agent",
            "content": {"response": "Briefly."},
        }
    )

    turns = fetcher._build_case(row, MetricType.MULTI_TURN).agent_data.turns

    assert turns[1].events[-1].state_delta is None


def _build_agent_response_event(
    invocation_id: str, agent: str, text: str
) -> dict[str, Any]:
    """Constructs an AGENT_RESPONSE row dictionary.

    Args:
        invocation_id: Invocation identifier.
        agent: Agent name.
        text: Response text.

    Returns:
        An AGENT_RESPONSE event dictionary.
    """
    return {
        "invocation_id": invocation_id,
        "event_type": "AGENT_RESPONSE",
        "agent": agent,
        "content": {"response": text},
    }


def _list_tool_names(tools: list[Any] | None) -> list[str | None] | None:
    """Lists the declaration names of a toolset.

    Args:
        tools: Toolset, or None.

    Returns:
        The names in declaration order, or None for no toolset.
    """
    if tools is None:
        return None
    return [
        declaration.name
        for tool in tools
        for declaration in tool.function_declarations or []
    ]


def test_tools_logged_by_name_reach_the_agent_config() -> None:
    """Without them the review prompt tells the judge the agent had no tools,
    and every call it made reads as a call to a tool it was not given."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        1,
        _llm_request(
            "inv-1", "weather-agent", "Resolved.", ["get_weather", "get_rain"]
        ),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    tools = case.agent_data.agents["weather-agent"].tools
    assert _list_tool_names(tools) == ["get_weather", "get_rain"]
    assert tools[0].function_declarations[0].parameters_json_schema is None


def test_tools_logged_as_declarations_keep_their_schema() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    row = _multi_turn_row()
    row["events"].insert(
        1,
        _llm_request(
            "inv-1",
            "weather-agent",
            "Resolved.",
            [
                {
                    "name": "get_weather",
                    "description": "Weather for a city.",
                    "parameters": schema,
                },
                {"name": "get_time"},
            ],
        ),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    declarations = (
        case.agent_data.agents["weather-agent"].tools[0].function_declarations
    )
    assert [
        (d.name, d.description, d.parameters_json_schema) for d in declarations
    ] == [
        ("get_weather", "Weather for a city.", schema),
        ("get_time", None, None),
    ]


def test_tools_logged_as_json_text_are_decoded() -> None:
    """A table whose ``attributes`` column is a STRING returns the tools as JSON
    text rather than a parsed list."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        1,
        _llm_request(
            "inv-1",
            "weather-agent",
            "Resolved.",
            json.dumps(["get_weather", {"name": "get_rain"}]),
        ),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert _list_tool_names(case.agent_data.agents["weather-agent"].tools) == [
        "get_weather",
        "get_rain",
    ]


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param("[SANITIZE_BUDGET_EXCEEDED]", id="sanitizer_placeholder"),
        pytest.param(
            json.dumps("[SANITIZE_BUDGET_EXCEEDED]"), id="json_string"
        ),
        pytest.param({"name": "get_weather"}, id="not_a_list"),
        pytest.param([], id="empty_list"),
        pytest.param([None, 3, ""], id="no_usable_entry"),
    ],
)
def test_unusable_tools_yield_no_toolset(tools: Any) -> None:
    """A value the plugin truncated or that is not a list of tools is not a
    toolset, and must not fail the whole session."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        1, _llm_request("inv-1", "weather-agent", "Resolved.", tools)
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert case.agent_data.agents["weather-agent"].tools is None
    assert all(
        event.active_tools is None
        for turn in case.agent_data.turns
        for event in turn.events
    )


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(
            ["get_weather", "get_rain", "[SANITIZE_BUDGET_EXCEEDED]"],
            id="list_cut_short",
        ),
        pytest.param(
            [
                {
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "[SANITIZE_BUDGET_EXCEEDED]": (
                                "[SANITIZE_BUDGET_EXCEEDED]"
                            ),
                        },
                    },
                }
            ],
            id="schema_cut_short",
        ),
    ],
)
def test_a_toolset_the_plugin_cut_short_is_not_used(tools: list[Any]) -> None:
    """The plugin marks where its size budget ran out. Shown as complete, the
    rest of the list would read as tools the agent was not given, so the
    complete toolset of a later request is the baseline instead."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        1, _llm_request("inv-1", "weather-agent", "Resolved.", tools)
    )
    row["events"].append(
        _llm_request(
            "inv-2", "weather-agent", "Resolved.", ["get_weather", "get_time"]
        )
    )
    row["events"].append(
        _build_agent_response_event("inv-2", "weather-agent", "Noon.")
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert _list_tool_names(case.agent_data.agents["weather-agent"].tools) == [
        "get_weather",
        "get_time",
    ]
    assert all(
        event.active_tools is None
        for turn in case.agent_data.turns
        for event in turn.events
    )


def test_a_placeholder_for_one_tool_is_not_read_as_a_tool_name() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        1,
        _llm_request(
            "inv-1",
            "weather-agent",
            "Resolved.",
            ["get_weather", "[UNSUPPORTED_OBJECT]"],
        ),
    )

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert _list_tool_names(case.agent_data.agents["weather-agent"].tools) == [
        "get_weather"
    ]


def test_an_agent_without_logged_tools_has_none() -> None:
    """None, as on the Cloud Trace path: the telemetry did not say."""
    fetcher = _make_fetcher(mock.MagicMock())

    case = fetcher._build_case(_multi_turn_row(), MetricType.MULTI_TURN)

    assert case.agent_data.agents["weather-agent"].tools is None


def test_each_agent_keeps_the_tools_of_its_own_first_request() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_agent_row()
    row["events"][1:1] = [
        _llm_request("inv-1", "root-agent", "Root.", ["transfer_to_agent"]),
    ]
    row["events"][4:4] = [
        _llm_request(
            "inv-1", "flights-subagent", "Flights.", ["search_flights"]
        ),
        _llm_request(
            "inv-1",
            "flights-subagent",
            "Flights.",
            ["search_flights", "book_flight"],
        ),
    ]

    agents = fetcher._build_case(row, MetricType.MULTI_TURN).agent_data.agents

    assert _list_tool_names(agents["root-agent"].tools) == ["transfer_to_agent"]
    assert _list_tool_names(agents["flights-subagent"].tools) == [
        "search_flights"
    ]


def test_changed_tools_ride_on_the_later_events_of_that_agent_only() -> None:
    """`AgentConfig` keeps the first toolset; a later one that differs rides on
    the events its agent emits under it, as on the Cloud Trace path."""
    fetcher = _make_fetcher(mock.MagicMock())
    discovery = ["transfer_to_agent", "find_booking", "amend_booking"]
    execution = ["transfer_to_agent", "find_booking"]
    row = {
        "session_id": "sess-3",
        "events": [
            _llm_request("inv-1", "root-agent", "Root.", ["transfer_to_agent"]),
            _build_agent_response_event("inv-1", "root-agent", "Routing."),
            _llm_request("inv-1", "worker-agent", "Work.", discovery),
            _build_agent_response_event("inv-1", "worker-agent", "Found it."),
            {
                "invocation_id": "inv-2",
                "event_type": "AGENT_STARTING",
                "agent": "worker-agent",
                "content": "Work.",
            },
            _llm_request("inv-2", "worker-agent", "Work.", execution),
            {
                "invocation_id": "inv-2",
                "event_type": "TOOL_STARTING",
                "agent": "worker-agent",
                "content": {"tool": "find_booking", "args": {"ref": "B1"}},
            },
            _build_agent_response_event("inv-2", "root-agent", "Done."),
            _build_agent_response_event("inv-2", "worker-agent", "Amended."),
        ],
    }

    case = fetcher._build_case(row, MetricType.MULTI_TURN)

    assert (
        _list_tool_names(case.agent_data.agents["worker-agent"].tools)
        == discovery
    )
    first, second = case.agent_data.turns
    assert [e.active_tools for e in first.events] == [None, None]
    assert [
        (e.author, _list_tool_names(e.active_tools)) for e in second.events
    ] == [
        ("worker-agent", execution),
        ("root-agent", None),
        ("worker-agent", execution),
    ]


@pytest.mark.parametrize(
    "later_tools",
    [
        pytest.param(["get_weather", "get_rain"], id="back_at_baseline"),
        pytest.param(["get_rain", "get_weather"], id="reordered"),
        pytest.param(None, id="not_logged"),
    ],
)
def test_tools_equivalent_to_the_baseline_carry_no_active_tools(
    later_tools: list[str] | None,
) -> None:
    """The comparison is against the baseline, ignoring order; a request that
    logged no tools keeps the toolset already in force."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        0,
        _llm_request(
            "inv-1", "weather-agent", "Be brief.", ["get_weather", "get_rain"]
        ),
    )
    row["events"].append(
        _llm_request("inv-2", "weather-agent", "Be brief.", later_tools)
    )
    row["events"].append(
        _build_agent_response_event("inv-2", "weather-agent", "Briefly.")
    )

    turns = fetcher._build_case(row, MetricType.MULTI_TURN).agent_data.turns

    assert all(
        event.active_tools is None for turn in turns for event in turn.events
    )


def test_a_request_without_tools_keeps_the_changed_toolset_in_force() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    row = _multi_turn_row()
    row["events"].insert(
        0, _llm_request("inv-1", "weather-agent", "Be brief.", ["get_weather"])
    )
    row["events"].append(
        _llm_request("inv-2", "weather-agent", "Be brief.", ["get_rain"])
    )
    row["events"].append(_llm_request("inv-2", "weather-agent", "Be brief."))
    row["events"].append(
        _build_agent_response_event("inv-2", "weather-agent", "Dry.")
    )

    turns = fetcher._build_case(row, MetricType.MULTI_TURN).agent_data.turns

    assert _list_tool_names(turns[1].events[-1].active_tools) == ["get_rain"]


def test_a_single_turn_case_gets_its_tools() -> None:
    fetcher = _make_fetcher(mock.MagicMock())
    row = _single_turn_row()
    row["events"].insert(
        1,
        {
            "event_type": "LLM_REQUEST",
            "agent": "weather-agent",
            "system_prompt": "Resolved.",
            "tools": ["get_rain"],
        },
    )

    case = fetcher._build_case(row, MetricType.SINGLE_TURN)

    assert _list_tool_names(case.agent_data.agents["weather-agent"].tools) == [
        "get_rain"
    ]


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_both_queries_project_the_logged_tools(metric_type: MetricType) -> None:
    """Nothing else notices the tools going missing from the projection: every
    agent block would just read as having none."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = " ".join(client.query.call_args.args[0].split())
    assert (
        "IF( e.event_type = 'LLM_REQUEST', JSON_QUERY(e.attributes, '$.tools'), "
        "NULL ) AS tools" in sql
    )


def test_a_three_agent_session_with_named_tools_reaches_the_review_prompt() -> (
    None
):
    """Verify end-to-end rendering from analytics row to review prompt: each
    agent block lists its tools with the unknown-parameters marker for
    name-only tools."""
    fetcher = _make_fetcher(mock.MagicMock())
    row = {
        "session_id": "sess-4",
        "events": [
            {
                "invocation_id": "inv-1",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "concierge-agent",
                "content": {"text_summary": "Move my booking."},
            },
            _llm_request(
                "inv-1", "concierge-agent", "Route.", ["transfer_to_agent"]
            ),
            _llm_request(
                "inv-1",
                "router-agent",
                "Pick a worker.",
                ["transfer_to_agent", "list_workers"],
            ),
            _llm_request(
                "inv-1",
                "bookings-agent",
                "Handle bookings.",
                ["find_booking", "move_booking", "notify_customer"],
            ),
            {
                "invocation_id": "inv-1",
                "event_type": "TOOL_STARTING",
                "agent": "bookings-agent",
                "content": {"tool": "move_booking", "args": {"ref": "B1"}},
            },
            _build_agent_response_event("inv-1", "bookings-agent", "Moved."),
        ],
    }

    case = fetcher._build_case(row, MetricType.MULTI_TURN)
    prompt = session_review.build_prompt(
        AgentRevisionCache().build_revisions(case),
        session_review.extract_session_turns(case),
    )

    assert "Tools it could call: []" not in prompt
    assert '"parameters": []' not in prompt
    blocks = {
        block.split("\n", 1)[0]: block
        for block in prompt.split("\n  agent: ")[1:]
    }
    assert set(blocks) == {"bookings-agent", "concierge-agent", "router-agent"}
    for agent, names in {
        "concierge-agent": ["transfer_to_agent"],
        "router-agent": ["transfer_to_agent", "list_workers"],
        "bookings-agent": ["find_booking", "move_booking", "notify_customer"],
    }.items():
        tools_line = blocks[agent].split("Tools it could call: ", 1)[1]
        listed = json.loads(tools_line.split("\n", 1)[0])
        assert [tool["name"] for tool in listed] == names
        assert all(
            tool["parameters"] == UNRECORDED_PARAMETERS for tool in listed
        )


_SAMPLE_CONTENT: dict[str, Any] = {
    "USER_MESSAGE_RECEIVED": {"text_summary": "Go on."},
    "AGENT_STARTING": "Authored instruction.",
    "TOOL_STARTING": {"tool": "lookup", "args": {"q": "x"}},
    "TOOL_COMPLETED": {"tool": "lookup", "result": {"ok": True}},
    "AGENT_RESPONSE": {"response": "Done."},
    "HITL_INPUT_REQUEST_COMPLETED": {"tool": "ask", "result": {"answer": "y"}},
}


def _build_event(
    invocation_id: str | None, event_type: str, agent: str = "worker"
) -> dict[str, Any]:
    """Builds a conversational event row with placeholder content for its type.

    Args:
        invocation_id: Invocation identifier, or None for single-turn rows.
        event_type: Key in `_SAMPLE_CONTENT`.
        agent: Agent name on the row's `agent` column.

    Returns:
        Event dictionary matching the ingestion query projection.
    """
    return {
        "invocation_id": invocation_id,
        "event_type": event_type,
        "agent": agent,
        "content": _SAMPLE_CONTENT[event_type],
    }


def _list_state_deltas(
    row: dict[str, Any], metric_type: MetricType = MetricType.MULTI_TURN
) -> list[list[dict[str, Any] | None]]:
    """Maps a row and returns each turn's event `state_delta`s in order.

    Args:
        row: BigQuery row dictionary.
        metric_type: Ingestion scope to map the row with.

    Returns:
        One list per turn holding each event's `state_delta`.
    """
    case = _make_fetcher(mock.MagicMock())._build_case(row, metric_type)
    return [
        [event.state_delta for event in turn.events]
        for turn in case.agent_data.turns
    ]


def _build_instruction_delta(instruction: str) -> dict[str, Any]:
    """Builds the `state_delta` for an event that shows a changed instruction.

    Args:
        instruction: Instruction text shown on the event.

    Returns:
        Expected `state_delta` dictionary.
    """
    return {"system_instruction": instruction}


def test_changed_instruction_is_shown_again_in_every_turn() -> None:
    """Each turn is stored and read independently, so tracking resets to the
    baseline and subsequent turns still under the changed instruction show it
    again."""
    row = {
        "session_id": "sess-1",
        "events": [
            _build_event("inv-1", "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-1", "worker", "v1"),
            _llm_request("inv-1", "worker", "v2"),
            _build_event("inv-1", "TOOL_STARTING"),
            _build_event("inv-1", "AGENT_RESPONSE"),
            _build_event("inv-2", "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-2", "worker", "v2"),
            _build_event("inv-2", "TOOL_STARTING"),
            _build_event("inv-2", "AGENT_RESPONSE"),
        ],
    }

    assert _list_state_deltas(row) == [
        [None, _build_instruction_delta("v2"), None],
        [None, _build_instruction_delta("v2"), None],
    ]


def test_agent_starting_never_shows_the_previous_turns_instruction() -> None:
    """`AGENT_STARTING` precedes the turn's first `LLM_REQUEST`, so the
    instruction in force at that point comes from the previous turn. The row is
    not a turn event, so the turn shows the new instruction on the agent's first
    own event after the request."""
    events: list[dict[str, Any]] = []
    for number in (1, 2, 3):
        invocation_id = f"inv-{number}"
        events += [
            _build_event(invocation_id, "USER_MESSAGE_RECEIVED"),
            _build_event(invocation_id, "AGENT_STARTING"),
            _llm_request(invocation_id, "worker", f"m{number}"),
            _build_event(invocation_id, "TOOL_STARTING"),
            _build_event(invocation_id, "AGENT_RESPONSE"),
        ]

    deltas = _list_state_deltas({"session_id": "sess-1", "events": events})

    assert deltas == [
        [None, None, None],
        [None, _build_instruction_delta("m2"), None],
        [None, _build_instruction_delta("m3"), None],
    ]


def test_change_back_to_baseline_within_a_turn_is_shown() -> None:
    """After a turn shows a changed instruction, a return to the baseline in
    the same turn must also be shown so the changed instruction does not appear
    to stay in effect."""
    row = {
        "session_id": "sess-1",
        "events": [
            _build_event("inv-1", "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-1", "worker", "v1"),
            _build_event("inv-1", "AGENT_RESPONSE"),
            _build_event("inv-2", "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-2", "worker", "v2"),
            _build_event("inv-2", "TOOL_STARTING"),
            _build_event("inv-2", "TOOL_COMPLETED"),
            _llm_request("inv-2", "worker", "v1"),
            _build_event("inv-2", "AGENT_RESPONSE"),
        ],
    }

    assert _list_state_deltas(row)[1] == [
        None,
        _build_instruction_delta("v2"),
        None,
        _build_instruction_delta("v1"),
    ]


def test_interleaved_agents_are_tracked_independently() -> None:
    """One agent's events must not consume or reset another agent's change."""
    row = {
        "session_id": "sess-1",
        "events": [
            _build_event("inv-1", "USER_MESSAGE_RECEIVED", "root"),
            _llm_request("inv-1", "root", "r1"),
            _build_event("inv-1", "AGENT_RESPONSE", "root"),
            _llm_request("inv-1", "sub", "s1"),
            _build_event("inv-1", "AGENT_RESPONSE", "sub"),
            _build_event("inv-2", "USER_MESSAGE_RECEIVED", "root"),
            _llm_request("inv-2", "root", "r2"),
            _build_event("inv-2", "TOOL_STARTING", "root"),
            _llm_request("inv-2", "sub", "s2"),
            _build_event("inv-2", "TOOL_STARTING", "sub"),
            _build_event("inv-2", "TOOL_COMPLETED", "root"),
            _build_event("inv-2", "TOOL_COMPLETED", "sub"),
            _build_event("inv-2", "AGENT_RESPONSE", "sub"),
            _build_event("inv-2", "AGENT_RESPONSE", "root"),
        ],
    }

    assert _list_state_deltas(row)[1] == [
        None,
        _build_instruction_delta("r2"),
        _build_instruction_delta("s2"),
        None,
        None,
        None,
        None,
    ]


def test_overlapping_invocations_each_show_the_changed_instruction() -> None:
    """Tracking runs per grouped turn, not in session time order, so a turn
    whose events interleave with another's still shows the change once.
    `inv-b` sends no `LLM_REQUEST`; pairing is session-wide (a known
    limitation), so it runs under the instruction that `inv-a`'s request set."""
    row = {
        "session_id": "sess-1",
        "events": [
            _build_event("inv-0", "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-0", "worker", "v1"),
            _build_event("inv-0", "AGENT_RESPONSE"),
            _llm_request("inv-a", "worker", "v2"),
            _build_event("inv-a", "USER_MESSAGE_RECEIVED"),
            _build_event("inv-b", "USER_MESSAGE_RECEIVED"),
            _build_event("inv-a", "TOOL_STARTING"),
            _build_event("inv-b", "TOOL_STARTING"),
            _build_event("inv-a", "TOOL_COMPLETED"),
            _build_event("inv-b", "AGENT_RESPONSE"),
            _build_event("inv-a", "AGENT_RESPONSE"),
        ],
    }

    assert _list_state_deltas(row)[1:] == [
        [None, _build_instruction_delta("v2"), None, None],
        [None, _build_instruction_delta("v2"), None],
    ]


def test_user_authored_events_never_carry_the_instruction() -> None:
    """User events do not run under the agent's instruction, so the change is
    deferred until the agent's next own event. Completed HITL requests are user
    answers and are treated as user-authored."""
    row = {
        "session_id": "sess-1",
        "events": [
            _build_event("inv-1", "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-1", "worker", "v1"),
            _build_event("inv-1", "TOOL_STARTING"),
            _llm_request("inv-1", "worker", "v2"),
            _build_event("inv-1", "AGENT_RESPONSE"),
            _build_event("inv-2", "USER_MESSAGE_RECEIVED"),
            _build_event("inv-2", "HITL_INPUT_REQUEST_COMPLETED"),
            _build_event("inv-2", "AGENT_RESPONSE"),
        ],
    }

    assert _list_state_deltas(row) == [
        [None, None, _build_instruction_delta("v2")],
        [None, None, _build_instruction_delta("v2")],
    ]


def test_single_turn_case_shows_the_changed_instruction_once() -> None:
    """A single-turn case is one turn, so the same once-per-turn rule holds."""
    row = {
        "invocation_id": "inv-9",
        "events": [
            _build_event(None, "USER_MESSAGE_RECEIVED"),
            _llm_request("inv-9", "worker", "v1"),
            _build_event(None, "TOOL_STARTING"),
            _build_event(None, "TOOL_COMPLETED"),
            _llm_request("inv-9", "worker", "v2"),
            _build_event(None, "TOOL_STARTING"),
            _build_event(None, "TOOL_COMPLETED"),
            _llm_request("inv-9", "worker", "v2"),
            _build_event(None, "AGENT_RESPONSE"),
        ],
    }

    assert _list_state_deltas(row, MetricType.SINGLE_TURN) == [
        [None, None, None, _build_instruction_delta("v2"), None, None]
    ]


def test_two_phase_case_shows_the_changed_instruction_once() -> None:
    """A system instruction can be very large, so it reaches the review prompt
    once per turn, not once per event (b/570915568)."""
    v1 = "Greet the user and collect the request."
    v2 = "Follow the SOP step by step. " * 5000
    events = [
        _build_event("inv-1", "USER_MESSAGE_RECEIVED"),
        _build_event("inv-1", "AGENT_STARTING"),
        _llm_request("inv-1", "worker", v1),
        _build_event("inv-1", "TOOL_STARTING"),
        _build_event("inv-1", "TOOL_COMPLETED"),
        _build_event("inv-1", "AGENT_RESPONSE"),
        _build_event("inv-2", "USER_MESSAGE_RECEIVED"),
        _build_event("inv-2", "AGENT_STARTING"),
    ]
    for _ in range(3):
        events += [
            _llm_request("inv-2", "worker", v2),
            _build_event("inv-2", "TOOL_STARTING"),
            _build_event("inv-2", "TOOL_COMPLETED"),
        ]
    events += [
        _llm_request("inv-2", "worker", v2),
        _build_event("inv-2", "AGENT_RESPONSE"),
    ]
    fetcher = _make_fetcher(mock.MagicMock())

    case = fetcher._build_case(
        {"session_id": "sess-1", "events": events}, MetricType.MULTI_TURN
    )

    carriers = [
        (turn_position, event_position)
        for turn_position, turn in enumerate(case.agent_data.turns)
        for event_position, event in enumerate(turn.events)
        if event.state_delta == _build_instruction_delta(v2)
    ]
    assert carriers == [(1, 1)], "only turn 2's first TOOL_STARTING carries v2"
    prompt = session_review.build_prompt(
        AgentRevisionCache().build_revisions(case),
        session_review.extract_session_turns(case),
    )
    assert prompt.count(v2) == 1


def test_fetch_page_reports_no_deployment_revision() -> None:
    """The ADK events table carries no revision column, so its cases are
    attributed to a single unnamed revision."""
    client = mock.MagicMock()
    _configure_paging(
        client, page_rows=[_multi_turn_row()], next_page_token=None
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert len(page.dataset.eval_cases) == 1
    assert page.revisions == {}


# --- what ingestion reports to the recorder -----------------------------------


def _recorded(rows: list[dict[str, Any]], metric_type: Any):
    """Maps a page of rows through a recording fetcher.

    Args:
        rows: List of BigQuery row dictionaries.
        metric_type: MetricType (single-turn or multi-turn).

    Returns:
        List of recorded trajectory records.
    """
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(mock.MagicMock(), recorder=recorder)
    fetcher._map_page(rows, metric_type)
    fetcher._recorder.flush()
    return recorder.records


def test_a_multi_turn_trajectory_is_keyed_by_its_session() -> None:
    recorded = _recorded([_multi_turn_row()], MetricType.MULTI_TURN)
    row_session_id = _multi_turn_row()["session_id"]

    assert [t.trajectory_id for t in recorded] == [row_session_id]
    assert recorded[0].session_id == row_session_id


def test_a_single_turn_trajectory_reports_the_session_it_belongs_to() -> None:
    """The case is keyed by the invocation, which on its own says nothing about
    the conversation it was one turn of."""
    recorded = _recorded(
        [{**_single_turn_row(), "session_id": "sess-1"}], MetricType.SINGLE_TURN
    )

    assert recorded[0].trajectory_id == "inv-9"
    assert recorded[0].session_id == "sess-1"


def test_this_source_reports_no_trace_ids_and_never_drops_a_row() -> None:
    """The analytics schema carries no OTel trace ids, so its trajectories get
    no console link -- a property of the telemetry, not of the store. And its
    content is inline, so there is nothing that can fail to resolve."""
    recorded = _recorded([_multi_turn_row()], MetricType.MULTI_TURN)

    assert recorded[0].trace_ids == ()
    assert recorded[0].ingest_status is IngestStatus.INGESTED


def test_the_single_turn_query_projects_the_session_id() -> None:
    """`session_id` is not part of the case, so nothing else would notice it
    going missing from the projection."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.SINGLE_TURN, _START, _END, limit=5
    )

    assert (
        "ANY_VALUE(e.session_id) AS session_id"
        in client.query.call_args.args[0]
    )


@pytest.mark.parametrize("sql_getter", ["submit_query", "count_scanned"])
def test_a_session_less_event_is_not_evaluated_as_a_multi_turn_case(
    sql_getter: str,
) -> None:
    """Without the filter every session-less event in the window groups into
    one case whose id is NULL. The other two fetchers already exclude them, and
    the sampling and counting queries have to exclude the same population."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    client.query.return_value.result.return_value = [{"scanned": 0}]
    fetcher = _make_fetcher(client)

    if sql_getter == "submit_query":
        fetcher.submit_query(
            "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
        )
    else:
        fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)

    assert "session_id IS NOT NULL" in client.query.call_args.args[0]


def test_the_rows_a_memory_stop_left_unread_are_reported_to_nobody() -> None:
    """They were never attempted, so the run did not sample them -- reporting
    them would put a trajectory in the store the sweep never looked at, and the
    counter invariants would stop holding."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[_multi_turn_row(), _multi_turn_row()],
        next_page_token="tok-next",
    )
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(client, recorder=recorder)
    calls = {"n": 0}

    def guard() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    fetcher._memory_guard = guard

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert len(page.dataset.eval_cases) == 1
    assert len(recorder.records) == 1


# --- the selector: which sessions the sweep reviews ------------------------ #

# SQL selector containing braces (regex quantifiers, JSON literals, struct
# literals) to verify format-safe template handling.
_BRACE_SELECTOR = """
SELECT session_id AS target_id
FROM `test-project.agent_analytics.agent_events`
WHERE REGEXP_CONTAINS(JSON_VALUE(content.text_summary), r'\\d{3}')
  AND TO_JSON_STRING(content) = '{"tool":"refund"}'
  AND STRUCT(1 AS a) IS NOT NULL
"""

# Expected targets CTE SQL fragment for window-scoped queries.
_WINDOW_TARGETS = f"""
  SELECT session_id
  FROM `{_TABLE_REF}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_OBSERVED_AGENT} = @agent_name
    AND session_id IS NOT NULL
  GROUP BY session_id
"""  # noqa: S608 - trusted test constants


def _targets_of(sql: str) -> str:
    """Extract the TargetSessions CTE body from a full query string.

    Args:
        sql: Full SQL query text.

    Returns:
        Extracted targets CTE SQL fragment.
    """
    return sql.split("WITH TargetSessions AS (\n", 1)[1].split(
        "\n  ORDER BY RAND()", 1
    )[0]


def _below_the_cte(sql: str) -> str:
    """Extract query clauses below the TargetSessions CTE definition.

    Args:
        sql: Full SQL query text.

    Returns:
        SQL fragment following the target sampling clause.
    """
    return sql.split("\n  ORDER BY RAND()", 1)[1]


def test_a_run_without_a_selector_queries_exactly_what_it_queried_before() -> (
    None
):
    """Unscoped multi-turn queries default to sampling the entire time window."""
    fetcher = _make_fetcher(mock.MagicMock())

    assert (
        _targets_of(fetcher._build_ingestion_sql(MetricType.MULTI_TURN))
        == _WINDOW_TARGETS
    )
    assert (
        fetcher._build_count_sql(MetricType.MULTI_TURN)
        == f"SELECT COUNT(*) AS scanned FROM ({_WINDOW_TARGETS})"  # noqa: S608 - trusted test constants
    )


def test_the_multi_turn_targets_do_not_depend_on_one_event_type() -> None:
    """Session matching must not depend on a single event type that may be
    absent (e.g., in a resumed invocation without a new message, or when
    dropped by the plugin's event allowlist)."""
    fetcher = _make_fetcher(mock.MagicMock())

    assert "event_type" not in _targets_of(
        fetcher._build_ingestion_sql(MetricType.MULTI_TURN)
    )


def test_the_single_turn_targets_count_user_messages_of_the_root_agent() -> (
    None
):
    """Single-turn targets count user messages and filter on the root agent."""
    fetcher = _make_fetcher(mock.MagicMock())

    sql = fetcher._build_count_sql(MetricType.SINGLE_TURN)

    assert "event_type = 'USER_MESSAGE_RECEIVED'" in sql
    assert f"{_OBSERVED_AGENT} = @agent_name" in sql


def test_a_selector_replaces_the_targets_and_nothing_below_them() -> None:
    """Custom selectors override target CTE definitions while preserving outer joins."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )
    default = _make_fetcher(mock.MagicMock())

    sql = fetcher._build_ingestion_sql(MetricType.MULTI_TURN)

    assert _targets_of(sql) == (
        "SELECT DISTINCT target_id AS session_id FROM (\nSELECT s AS target_id\n)"
    )
    assert _below_the_cte(sql) == _below_the_cte(
        default._build_ingestion_sql(MetricType.MULTI_TURN)
    )


def test_the_selected_sessions_are_still_sampled_to_the_budget() -> None:
    """Targeted sessions are sampled with random ordering up to the query limit."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )

    assert "ORDER BY RAND()\n  LIMIT @limit" in fetcher._build_ingestion_sql(
        MetricType.MULTI_TURN
    )


def test_the_count_and_the_sample_ask_about_the_selected_population() -> None:
    """Count and sampling queries evaluate the identical targets CTE definition."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )

    targets = _targets_of(fetcher._build_ingestion_sql(MetricType.MULTI_TURN))

    assert fetcher._build_count_sql(MetricType.MULTI_TURN) == (
        f"SELECT COUNT(*) AS scanned FROM ({targets})"  # noqa: S608 - trusted test constants
    )


def test_a_selector_holding_braces_reaches_bigquery_intact() -> None:
    """SQL containing braces is passed to BigQuery without formatting errors."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 1}]
    fetcher = _make_fetcher(client, selector_sql=_BRACE_SELECTOR)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
    )
    sampled = client.query.call_args.args[0]
    fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)
    counted = client.query.call_args.args[0]

    assert _BRACE_SELECTOR in sampled
    assert _BRACE_SELECTOR in counted
    assert _TABLE_REF in sampled


def test_the_single_turn_query_has_no_selector_path() -> None:
    """Single-turn queries ignore custom selectors and target individual invocations."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )
    default = _make_fetcher(mock.MagicMock())

    assert fetcher._build_ingestion_sql(
        MetricType.SINGLE_TURN
    ) == default._build_ingestion_sql(MetricType.SINGLE_TURN)
    assert fetcher._build_count_sql(
        MetricType.SINGLE_TURN
    ) == default._build_count_sql(MetricType.SINGLE_TURN)
