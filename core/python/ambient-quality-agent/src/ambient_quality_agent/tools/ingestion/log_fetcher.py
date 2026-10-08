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

"""Fetch agent log entries from Cloud Logging, indexed by span id.

Retrieves message content emitted as structured log entries for agents
whose span attributes carry only metadata.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ambient_quality_agent.tools.ingestion.trace_converter import (
    ATTR_AGENT_NAME,
)
from ambient_quality_agent.tools.ingestion.trace_models import LogEntryView
from google.cloud import logging_v2
from google.cloud.logging_v2.services.logging_service_v2 import (
    LoggingServiceV2Client,
)

logger = logging.getLogger(__name__)

# Cap retrieved pages to prevent a runaway trace from stalling ingestion.
_PAGE_SIZE = 1000
_MAX_PAGES = 10

# Each trace adds 105 characters plus the project ID's length (at most 30) to
# the filter, and Cloud Logging rejects a filter longer than 20,000 characters
# with INVALID_ARGUMENT. 100 traces per request keep the filter under 14,000.
_TRACES_PER_REQUEST = 100

# Absorbs clock skew and ingestion delay around the span time window.
_TIME_BUFFER = dt.timedelta(minutes=5)


class LogFetcher:
    """Reads raw log entries from Cloud Logging, grouped by span id."""

    def __init__(self, project_id: str, client: Any | None = None) -> None:
        """Initialize the fetcher.

        Args:
            project_id: Project whose ``_Default`` log bucket holds the
                agent logs.
            client: Optional `LoggingServiceV2Client`.
        """
        self._project_id = project_id
        self._client = client or LoggingServiceV2Client()

    def fetch_entries_by_span(
        self,
        trace_ids: Sequence[str],
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> dict[str, list[LogEntryView]]:
        """Fetch log entries for ``trace_ids``, indexed by span id.

        Args:
            trace_ids: Trace ids (hex, as stored on spans) to fetch logs
                for.
            start: Earliest span time in the batch; the log query lower
                bound (minus a buffer). When ``None``, no lower bound.
            end: Latest span time in the batch; the log query upper bound
                (plus a buffer). When ``None``, no upper bound.

        Returns:
            ``{span_id: [LogEntryView, ...]}`` preserving entry order, for
            entries that carry a non-empty span id.
        """
        if not trace_ids:
            return {}

        result: dict[str, list[LogEntryView]] = {}
        for entry in self._list_entries(trace_ids, start, end):
            span_id = entry.span_id
            if not span_id:
                continue
            result.setdefault(span_id, []).append(
                LogEntryView(
                    labels=dict(entry.labels),
                    json_payload=_struct_to_dict(entry.json_payload),
                )
            )
        logger.info(
            "Fetched log entries for %d span(s) across %d trace(s).",
            len(result),
            len(trace_ids),
        )
        return result

    def has_entries(self, start: dt.datetime) -> bool:
        """Checks whether any log entry since `start` is readable.

        Reads through the same log view `fetch_entries_by_span` does, so a
        denied read here is the one ingestion would hit.

        Args:
            start: Earliest entry timestamp to consider.

        Returns:
            True if at least one entry is readable.
        """
        request = logging_v2.types.ListLogEntriesRequest(
            resource_names=[f"projects/{self._project_id}"],
            filter=f'timestamp>="{start.isoformat()}"',
            page_size=1,
        )
        first_page = next(
            iter(self._client.list_log_entries(request=request).pages), None
        )
        return bool(first_page and first_page.entries)

    def get_payload_ref(
        self,
        agent_name: str,
        ref_keys: Sequence[str],
        *,
        start: dt.datetime,
        end: dt.datetime,
    ) -> str | None:
        """Returns one payload reference from the labels of the agent's log entries.

        Args:
            agent_name: Agent name, as the entries' ``gen_ai.agent.name`` label
                records it.
            ref_keys: Label keys that hold a ``gs://`` reference, in the order
                to prefer them.
            start: Earliest entry timestamp.
            end: Latest entry timestamp.

        Returns:
            A ``gs://`` URI, or None when no entry in the window carries one.
        """
        if not ref_keys:
            return None
        any_ref = " OR ".join(f'labels."{key}":*' for key in ref_keys)
        request = logging_v2.types.ListLogEntriesRequest(
            resource_names=[f"projects/{self._project_id}"],
            filter=(
                f'timestamp>="{start.isoformat()}" AND timestamp<="{end.isoformat()}" '
                f'AND labels."{ATTR_AGENT_NAME}"={_quote_filter_value(agent_name)} '
                f"AND ({any_ref})"
            ),
            page_size=1,
        )
        first_page = next(
            iter(self._client.list_log_entries(request=request).pages), None
        )
        for entry in first_page.entries if first_page else []:
            labels = dict(entry.labels)
            for key in ref_keys:
                if labels.get(key):
                    return str(labels[key])
        return None

    def _list_entries(
        self,
        trace_ids: Sequence[str],
        start: dt.datetime | None,
        end: dt.datetime | None,
    ) -> list[Any]:
        """List trace-correlated log entries for ``trace_ids``.

        Sends one request per `_TRACES_PER_REQUEST` trace IDs, each capped at
        `_MAX_PAGES` pages.

        Args:
            trace_ids: Trace IDs to fetch correlated logs for.
            start: Lower bound timestamp for the query window.
            end: Upper bound timestamp for the query window.

        Returns:
            Log entry objects matching the query, in request order and then in
            timestamp order within each request.
        """
        entries: list[Any] = []
        for offset in range(0, len(trace_ids), _TRACES_PER_REQUEST):
            batch = trace_ids[offset : offset + _TRACES_PER_REQUEST]
            entries.extend(self._list_batch_entries(batch, start, end))
        return entries

    def _list_batch_entries(
        self,
        trace_ids: Sequence[str],
        start: dt.datetime | None,
        end: dt.datetime | None,
    ) -> list[Any]:
        """List trace-correlated log entries for one request's ``trace_ids``.

        Args:
            trace_ids: Trace IDs to fetch correlated logs for, at most
                `_TRACES_PER_REQUEST` of them.
            start: Lower bound timestamp for the query window.
            end: Upper bound timestamp for the query window.

        Returns:
            Log entry objects matching the query, in timestamp order.
        """
        # Exporters write `trace` either as the full resource name or as the
        # bare hex ID (ADK's OTLP log exporter, Agent Runtime stdout), so both
        # forms are matched.
        traces = " OR ".join(
            f'trace="projects/{self._project_id}/traces/{tid}" OR trace="{tid}"'
            for tid in trace_ids
        )
        clauses = ['spanId!=""']
        if start is not None:
            lower = (start - _TIME_BUFFER).isoformat()
            clauses.append(f'timestamp>="{lower}"')
        if end is not None:
            upper = (end + _TIME_BUFFER).isoformat()
            clauses.append(f'timestamp<="{upper}"')
        clauses.append(f"({traces})")
        log_filter = " AND ".join(clauses)
        request = logging_v2.types.ListLogEntriesRequest(
            resource_names=[f"projects/{self._project_id}"],
            filter=log_filter,
            order_by="timestamp asc",
            page_size=_PAGE_SIZE,
        )
        entries: list[Any] = []
        pages = self._client.list_log_entries(request=request).pages
        for page_count, page in enumerate(pages):
            entries.extend(page.entries)
            if page_count + 1 >= _MAX_PAGES:
                logger.warning(
                    "Reached log page cap (%d); truncating.", _MAX_PAGES
                )
                break
        return entries


def _quote_filter_value(value: str) -> str:
    """Quotes a value for a Cloud Logging filter.

    Args:
        value: Value to compare against.

    Returns:
        The value as a double-quoted filter string.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _struct_to_dict(payload: Any) -> dict[str, Any]:
    """Convert a proto Struct ``jsonPayload`` into a plain dict.

    proto-plus exposes a Struct as a `MapComposite` whose nested values stay
    `MapComposite` / `RepeatedComposite`, which `json.dumps` rejects. The
    converter serializes nested payload content, so the whole tree is
    converted to plain Python.

    Args:
        payload: Protobuf Struct or dictionary payload.

    Returns:
        A plain `dict` payload unchanged; otherwise a dictionary of plain
        `dict`, `list` and scalar values. Empty for ``None`` or a non-mapping.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, Mapping):
        return _struct_value_to_python(payload)
    return {}


def _struct_value_to_python(value: Any) -> Any:
    """Recursively convert a proto-plus Struct value into plain Python.

    Args:
        value: Struct value: a mapping, a non-string sequence, or a scalar.

    Returns:
        ``value`` with every mapping turned into a `dict` and every non-string
        sequence into a `list`; scalars are returned unchanged.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _struct_value_to_python(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_struct_value_to_python(item) for item in value]
    return value
