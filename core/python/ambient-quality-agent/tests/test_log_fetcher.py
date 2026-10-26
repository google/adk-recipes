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

"""Tests for `tools/ingestion/log_fetcher.py`."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from typing import Any

import pytest
from ambient_quality_agent.tools.ingestion.log_fetcher import LogFetcher
from google.api_core import exceptions as api_exceptions
from google.cloud import logging_v2


@dataclasses.dataclass
class _Entry:
    span_id: str
    labels: dict[str, Any]
    json_payload: dict[str, Any]


@dataclasses.dataclass
class _Page:
    entries: list[Any]


@dataclasses.dataclass
class _Response:
    pages: list[_Page]


class _FakeClient:
    def __init__(
        self,
        entries: list[Any],
        *,
        responses: list[list[list[Any]]] | None = None,
    ) -> None:
        """Initializes the fake client with canned log entries.

        Args:
            entries: Log entry objects returned as one page by every
                `list_log_entries` call, unless ``responses`` is set.
            responses: Pages to return, one list of pages per call in call
                order.
        """
        self._entries = entries
        self._responses = list(responses or [])
        self.requests: list[Any] = []
        self.retries: list[Any] = []

    @property
    def last_request(self) -> Any:
        """Returns the most recent `list_log_entries` request, or None.

        Returns:
            The latest recorded request object, or None if no call was made.
        """
        return self.requests[-1] if self.requests else None

    def list_log_entries(self, request: Any, retry: Any = None) -> _Response:
        """Records `request` and `retry` and returns the next canned response.

        Args:
            request: Cloud Logging `ListLogEntriesRequest` payload.
            retry: The retry the caller asked for.

        Returns:
            The next scripted `_Response` if any, else one page wrapping
            `self._entries`.
        """
        self.requests.append(request)
        self.retries.append(retry)
        if self._responses:
            return _Response([_Page(page) for page in self._responses.pop(0)])
        return _Response([_Page(self._entries)])


def test_indexes_entries_by_span_id() -> None:
    entries = [
        _Entry(
            "span-1",
            {"event.name": "gen_ai.choice"},
            {"content": {"role": "model"}},
        ),
        _Entry(
            "span-1", {"event.name": "gen_ai.user.message"}, {"content": "hi"}
        ),
        _Entry(
            "span-2",
            {"event.name": "gen_ai.system.message"},
            {"content": "be nice"},
        ),
    ]
    fetcher = LogFetcher("proj", client=_FakeClient(entries))

    result = fetcher.fetch_entries_by_span(["trace-1"])

    assert set(result) == {"span-1", "span-2"}
    assert len(result["span-1"]) == 2
    assert result["span-1"][0].labels["event.name"] == "gen_ai.user.message"
    assert result["span-1"][0].json_payload == {"content": "hi"}
    assert result["span-2"][0].json_payload == {"content": "be nice"}


def test_lists_entries_newest_first_and_reverses_to_chronological_order() -> (
    None
):
    newer_entry = _Entry(
        "span-1",
        {"event.name": "gen_ai.choice"},
        {"content": {"role": "model"}},
    )
    older_entry = _Entry(
        "span-1", {"event.name": "gen_ai.user.message"}, {"content": "hi"}
    )
    client = _FakeClient([newer_entry, older_entry])
    fetcher = LogFetcher("proj", client=client)

    result = fetcher.fetch_entries_by_span(["trace-1"])

    assert client.last_request.order_by == "timestamp desc"
    assert [entry.labels["event.name"] for entry in result["span-1"]] == [
        "gen_ai.user.message",
        "gen_ai.choice",
    ]


def test_skips_entries_without_span_id() -> None:
    entries = [
        _Entry("", {"event.name": "gen_ai.user.message"}, {"content": "x"}),
        _Entry(
            "span-1", {"event.name": "gen_ai.user.message"}, {"content": "y"}
        ),
    ]
    fetcher = LogFetcher("proj", client=_FakeClient(entries))

    result = fetcher.fetch_entries_by_span(["trace-1"])

    assert list(result) == ["span-1"]


def test_builds_filter_with_traces() -> None:
    client = _FakeClient([])
    fetcher = LogFetcher("my-proj", client=client)

    fetcher.fetch_entries_by_span(["t1", "t2"])

    log_filter = client.last_request.filter
    assert "logName" not in log_filter
    assert 'spanId!=""' in log_filter
    # Both the resource-name and the bare-ID forms, grouped so the trace
    # alternatives do not escape the AND with the other clauses.
    assert log_filter.endswith(
        ' AND (trace="projects/my-proj/traces/t1" OR trace="t1"'
        ' OR trace="projects/my-proj/traces/t2" OR trace="t2")'
    )
    assert client.last_request.resource_names == ["projects/my-proj"]


def test_splits_many_traces_across_requests_under_the_filter_limit() -> None:
    entry = _Entry(
        "span-1", {"event.name": "gen_ai.user.message"}, {"content": "hi"}
    )
    client = _FakeClient([entry])
    # The longest project ID allowed, so each trace's clause is longest.
    fetcher = LogFetcher("p" * 30, client=client)
    trace_ids = [f"{index:032x}" for index in range(250)]

    result = fetcher.fetch_entries_by_span(trace_ids)

    assert len(client.requests) == 3
    filters = [request.filter for request in client.requests]
    assert all(len(log_filter) < 20_000 for log_filter in filters)
    for trace_id in trace_ids:
        assert sum(f'trace="{trace_id}"' in f for f in filters) == 1
    # One entry per request, merged in request order.
    assert len(result["span-1"]) == 3


def test_filter_includes_buffered_time_window() -> None:
    client = _FakeClient([])
    fetcher = LogFetcher("my-proj", client=client)
    start = dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 1, 2, 12, 30, tzinfo=dt.UTC)

    fetcher.fetch_entries_by_span(["t1"], start=start, end=end)

    log_filter = client.last_request.filter
    assert 'timestamp>="2026-01-02T11:55:00+00:00"' in log_filter
    assert 'timestamp<="2026-01-02T12:35:00+00:00"' in log_filter


def test_filter_omits_time_window_when_unset() -> None:
    client = _FakeClient([])
    fetcher = LogFetcher("my-proj", client=client)

    fetcher.fetch_entries_by_span(["t1"])

    assert "timestamp" not in client.last_request.filter


def test_page_cap_keeps_the_newest_pages_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Page 0 holds the newest entry, page 11 the oldest.
    pages = [
        [_Entry("span-1", {"page": str(index)}, {})] for index in range(12)
    ]
    client = _FakeClient([], responses=[pages])
    fetcher = LogFetcher("proj", client=client)

    with caplog.at_level(logging.WARNING):
        result = fetcher.fetch_entries_by_span(["trace-1"])

    assert [view.labels["page"] for view in result["span-1"]] == [
        str(index) for index in reversed(range(10))
    ]
    assert "oldest" in caplog.text


def _assert_plain_python(value: Any) -> None:
    """Asserts ``value`` nests only plain `dict`, `list` and scalar values.

    Args:
        value: Value to inspect recursively.
    """
    if isinstance(value, dict):
        for item in value.values():
            _assert_plain_python(item)
    elif isinstance(value, list):
        for item in value:
            _assert_plain_python(item)
    else:
        assert isinstance(value, (str, int, float, bool, type(None))), type(
            value
        )


def test_converts_a_proto_plus_payload_into_plain_python() -> None:
    """A real `LogEntry` exposes nested `MapComposite` / `RepeatedComposite`
    values that `json.dumps` rejects; the view must hold plain Python."""
    entry = logging_v2.types.LogEntry(
        span_id="span-1",
        labels={"event.name": "gen_ai.user.message"},
        json_payload={
            "content": {"role": "user", "parts": [{"text": "hi"}]},
            "index": 1,
            "final": True,
            "finish_reason": None,
        },
    )
    fetcher = LogFetcher("proj", client=_FakeClient([entry]))

    result = fetcher.fetch_entries_by_span(["trace-1"])

    view = result["span-1"][0]
    assert view.labels == {"event.name": "gen_ai.user.message"}
    assert view.json_payload == {
        "content": {"role": "user", "parts": [{"text": "hi"}]},
        "index": 1,
        "final": True,
        "finish_reason": None,
    }
    _assert_plain_python(view.json_payload)
    assert json.loads(json.dumps(view.json_payload)) == view.json_payload


def test_empty_trace_ids_returns_empty_without_querying() -> None:
    client = _FakeClient([])
    fetcher = LogFetcher("proj", client=client)

    assert fetcher.fetch_entries_by_span([]) == {}
    assert client.last_request is None


def test_has_entries_reads_one_entry_since_the_start() -> None:
    client = _FakeClient([_Entry("span-1", {}, {})])
    fetcher = LogFetcher("proj", client=client)

    assert fetcher.has_entries(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) is True
    assert client.last_request.resource_names == ["projects/proj"]
    assert (
        client.last_request.filter == 'timestamp>="2026-01-01T00:00:00+00:00"'
    )
    assert client.last_request.page_size == 1


def test_has_entries_is_false_for_an_empty_view() -> None:
    fetcher = LogFetcher("proj", client=_FakeClient([]))

    assert fetcher.has_entries(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) is False


_KEYS = ("gen_ai.input.messages_ref", "gen_ai.output.messages_ref")
_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)


def test_get_payload_ref_filters_on_the_agent_and_any_ref_label() -> None:
    client = _FakeClient(
        [
            _Entry(
                "span-1", {"gen_ai.output.messages_ref": "gs://b/out.jsonl"}, {}
            )
        ]
    )
    fetcher = LogFetcher("proj", client=client)

    ref = fetcher.get_payload_ref('my "agent"', _KEYS, start=_START, end=_END)

    assert ref == "gs://b/out.jsonl"
    log_filter = client.last_request.filter
    assert 'labels."gen_ai.agent.name"="my \\"agent\\""' in log_filter
    assert (
        '(labels."gen_ai.input.messages_ref":* OR labels."gen_ai.output.messages_ref":*)'
        in log_filter
    )
    assert 'timestamp<="2026-01-08T00:00:00+00:00"' in log_filter
    assert client.last_request.page_size == 1


def test_get_payload_ref_is_none_without_a_matching_entry() -> None:
    fetcher = LogFetcher("proj", client=_FakeClient([]))

    assert (
        fetcher.get_payload_ref("agent", _KEYS, start=_START, end=_END) is None
    )


class _FailingClient:
    def __init__(self, errors: list[Exception], entries: list[Any]) -> None:
        """Initializes the fake client with errors to raise before answering.

        Args:
            errors: Errors to raise, one per attempt, before the first answer.
            entries: Log entry objects returned as one page once `errors` run
                out.
        """
        self._errors = list(errors)
        self._entries = entries
        self.attempts = 0

    def list_log_entries(self, request: Any, retry: Any) -> _Response:
        """Sends `request` under `retry`, as the generated client does.

        Args:
            request: Cloud Logging `ListLogEntriesRequest` payload.
            retry: The retry the caller asked for.

        Returns:
            One page wrapping `self._entries`, once every scripted error has
            been raised.
        """
        del request

        def send() -> _Response:
            self.attempts += 1
            if self._errors:
                raise self._errors.pop(0)
            return _Response([_Page(self._entries)])

        return retry(send)()


def _read_one_span(fetcher: LogFetcher) -> Any:
    return fetcher.fetch_entries_by_span(["trace-1"])


def _read_has_entries(fetcher: LogFetcher) -> Any:
    return fetcher.has_entries(_START)


def _read_payload_ref(fetcher: LogFetcher) -> Any:
    return fetcher.get_payload_ref("agent", _KEYS, start=_START, end=_END)


@pytest.mark.parametrize(
    "read", [_read_one_span, _read_has_entries, _read_payload_ref]
)
def test_a_read_over_the_quota_is_retried(
    monkeypatch: pytest.MonkeyPatch, read: Any
) -> None:
    """The project's read quota is shared, so another reader can exhaust it."""
    monkeypatch.setattr(
        "google.api_core.retry.retry_unary.time.sleep", lambda _seconds: None
    )
    entry = _Entry("span-1", {"gen_ai.output.messages_ref": "gs://b/o"}, {})
    client = _FailingClient(
        [
            api_exceptions.ResourceExhausted("Quota exceeded"),
            api_exceptions.ServiceUnavailable("unavailable"),
        ],
        [entry],
    )

    result = read(LogFetcher("proj", client=client))

    assert client.attempts == 3
    assert result


def test_a_denied_read_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "google.api_core.retry.retry_unary.time.sleep", lambda _seconds: None
    )
    client = _FailingClient([api_exceptions.PermissionDenied("denied")], [])

    with pytest.raises(api_exceptions.PermissionDenied):
        LogFetcher("proj", client=client).has_entries(_START)
    assert client.attempts == 1
