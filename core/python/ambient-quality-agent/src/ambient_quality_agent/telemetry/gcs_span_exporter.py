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

"""OpenTelemetry span exporter writing gzipped NDJSON to Google Cloud Storage.

Each line corresponds to a row in the Cloud Trace BigQuery view `_AllSpans`,
matching the schema in `all_spans.schema.json`. This format allows exported
spans to be loaded into BigQuery and queried directly by `CloudOpsFetcher`.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger(__name__)

_CONTENT_TYPE = "application/gzip"


class GcsSpanExporter(SpanExporter):
    """Exports span batches as individual gzipped NDJSON objects to GCS.

    Objects are partitioned by UTC hour using the path format
    ``<prefix>/dt=YYYY-MM-DD/HH/<uuid4>.jsonl.gz``.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        resource_attributes: Mapping[str, Any],
        client_factory: Callable[[], Any],
    ) -> None:
        """Initializes the GCS span exporter.

        Args:
            bucket: Target GCS bucket name.
            prefix: Object path prefix without trailing slashes.
            resource_attributes: Resource attributes applied to every span row.
            client_factory: Callable returning a `google.cloud.storage.Client`.
        """
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._resource_attributes = dict(resource_attributes)
        self._client_factory = client_factory
        self._client: Any | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Serializes and uploads a batch of spans as a gzipped NDJSON object.

        Args:
            spans: OpenTelemetry spans to export.

        Returns:
            `SpanExportResult.SUCCESS` if export succeeded, or `SpanExportResult.FAILURE` on error.
        """
        if not spans:
            return SpanExportResult.SUCCESS
        lines: list[str] = []
        for span in spans:
            try:
                # `allow_nan=False` rejects NaN and Infinity, which json emits as
                # bare tokens no JSON parser accepts. Raising here drops the one
                # bad span; emitting them would break the reader's `bq load`.
                lines.append(
                    json.dumps(self._span_to_row(span), allow_nan=False)
                )
            except Exception:
                # Telemetry may never fail the work it observes, so any malformed
                # span is dropped and the rest of the batch still exports.
                logger.exception(
                    "Skipping span %r: not serializable.", span.name
                )
        if not lines:
            return SpanExportResult.FAILURE
        try:
            payload = gzip.compress("\n".join(lines).encode("utf-8"))
            blob = self._build_blob(
                _build_object_name(self._prefix, _read_utc_clock())
            )
            blob.upload_from_string(payload, content_type=_CONTENT_TYPE)
        except Exception:
            logger.exception("Failed to export %d span(s) to GCS.", len(spans))
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Cleans up the cached storage client."""
        self._client = None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flushes buffered spans.

        Args:
            timeout_millis: Timeout in milliseconds (unused).

        Returns:
            Always True since exports write immediately.
        """
        return True

    def _build_blob(self, object_name: str) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client.bucket(self._bucket).blob(object_name)

    def _span_to_row(self, span: ReadableSpan) -> dict[str, Any]:
        """Converts an OpenTelemetry span to an ``_AllSpans`` table row.

        Args:
            span: Span to convert.

        Returns:
            Dictionary matching the BigQuery span schema.
        """
        context = span.context
        parent = span.parent
        return {
            "trace_id": format(context.trace_id, "032x") if context else None,
            "span_id": format(context.span_id, "016x") if context else None,
            "parent_span_id": format(parent.span_id, "016x")
            if parent
            else None,
            "name": span.name,
            "start_time": _format_rfc3339(span.start_time),
            "end_time": _format_rfc3339(span.end_time),
            "attributes": _format_attributes(span.attributes),
            "status": {
                "code": span.status.status_code.value,
                "message": span.status.description,
            },
            "events": [
                {
                    "time": _format_rfc3339(event.timestamp),
                    "name": event.name,
                    "attributes": _format_attributes(event.attributes),
                }
                for event in span.events
            ],
            "resource": {"attributes": dict(self._resource_attributes)},
        }


def _read_utc_clock() -> dt.datetime:
    """Returns the current UTC time.

    Returns:
        Current datetime in UTC timezone.
    """
    return dt.datetime.now(dt.UTC)


def _build_object_name(prefix: str, now: dt.datetime) -> str:
    """Generates an hour-partitioned GCS object path for a timestamp.

    Args:
        prefix: Object path prefix.
        now: Timestamp determining the partition date and hour.

    Returns:
        Formatted GCS object path.
    """
    stamp = now.astimezone(dt.UTC)
    name = f"dt={stamp:%Y-%m-%d}/{stamp:%H}/{uuid.uuid4()}.jsonl.gz"
    return f"{prefix}/{name}" if prefix else name


def _format_rfc3339(timestamp_ns: int | None) -> str | None:
    """Converts epoch nanoseconds to an RFC 3339 UTC string.

    Args:
        timestamp_ns: Nanoseconds since Unix epoch.

    Returns:
        Formatted UTC timestamp string with microsecond precision, or None if input is None.
    """
    if timestamp_ns is None:
        return None
    seconds, remainder = divmod(timestamp_ns, 1_000_000_000)
    moment = dt.datetime.fromtimestamp(seconds, dt.UTC).replace(
        microsecond=remainder // 1000
    )
    return f"{moment:%Y-%m-%dT%H:%M:%S.%f}Z"


def _format_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Formats OpenTelemetry span attributes for JSON serialization.

    Args:
        attributes: Attribute key-value mapping.

    Returns:
        Dictionary with serialized primitive values and arrays.
    """
    rendered: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if isinstance(value, str):
            rendered[key] = value
        elif isinstance(value, Sequence):
            rendered[key] = list(value)
        else:
            rendered[key] = value
    return rendered
