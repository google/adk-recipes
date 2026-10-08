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

"""Exports AQuA telemetry spans from GCS into a portable zip archive.

Generates a standalone archive containing exported OpenTelemetry spans in NDJSON
format, BigQuery load schema (`all_spans.schema.json`), a metadata manifest,
and documentation for replaying or querying the trace data.

All time operations use UTC to match the partition structure in GCS.
Key design considerations:
- Listing window: Object prefixes are partitioned by write hour, not span end
  time. The search window is widened by one hour on each edge, and individual rows
  are filtered by `end_time`.
- Memory efficiency: GCS objects are decompressed and written to the zip
  archive incrementally in chunks to avoid buffering large datasets in memory.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import zipfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ambient_quality_cli import gcs
from ambient_quality_shared.telemetry import SPAN_OBJECT_PREFIX
from ambient_quality_shared.terraform_state import (
    DEPLOYMENT_KEY,
    STATE_DIR_NAME,
    build_state_path,
    read_state_outputs,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

SCHEMA_FILE = "all_spans.schema.json"
"""BigQuery load schema for exported span rows."""

SCHEMA_ENTRY = f"schema/{SCHEMA_FILE}"
MANIFEST_ENTRY = "manifest.json"
README_ENTRY = "README.md"
SPANS_DIR = "spans"

SPANS_ENTRY = f"{SPANS_DIR}/all_spans.jsonl"
"""Archive entry containing all span rows in a single file for `bq load` compatibility."""

# OpenTelemetry attribute identifying the agent or surface emitting the span.
AGENT_NAME_ATTRIBUTE = "gen_ai.agent.name"

# OpenTelemetry attribute identifying the deployment version that wrote the span.
SERVICE_VERSION_ATTRIBUTE = "service.version"

_DURATION_RE = re.compile(r"^(?P<count>\d+)(?P<unit>[dhm])$")
_DURATION_UNITS = {"d": "days", "h": "hours", "m": "minutes"}

# Slack window added when listing GCS prefixes to account for delays between
# span completion and batch flush.
_BOUNDARY_SLACK = dt.timedelta(hours=1)

REMEDIATION = gcs.Remediation(
    forbidden=(
        "Dumping traces needs roles/storage.objectViewer on the bucket -- add "
        "yourself to the module's trace_readers, or ask whoever owns the "
        "deployment."
    ),
    missing=(
        "Apply the Terraform that creates it, or check that .aqua/ belongs to "
        "the project you meant."
    ),
)

_LOCATION_PLACEHOLDER = "@LOCATION@"
"""Substituted with the deployment's region; `str.replace`, not `str.format`,
so the shell `${...}` expansions in the template need no escaping."""

DEFAULT_DATASET_LOCATION = "US"
"""Dataset location used when the dump could not determine the deployment's."""

_README_TEMPLATE = """\
# AQuA trace dump

Exported OpenTelemetry spans from an AQuA deployment, as newline-delimited JSON
in `spans/all_spans.jsonl`. Each line is one row of the Cloud Trace BigQuery
linked-dataset view `_AllSpans`, which is the shape AQuA's own ingestion path
reads.

**These spans may contain conversation content.** `manifest.json` reports which
surfaces they came from and over which range.

## Load it into BigQuery

Unzip the archive and run this from the directory it creates:

```bash
# Any dataset name works; it is created below. LOCATION is where these spans
# were produced.
DATASET=aqa_trace_dump
LOCATION=@LOCATION@
PROJECT=$(gcloud config get-value project)

bq --project_id="${PROJECT}" mk --dataset --location="${LOCATION}" "${DATASET}"
bq --project_id="${PROJECT}" --location="${LOCATION}" load \\
  --source_format=NEWLINE_DELIMITED_JSON \\
  "${PROJECT}:${DATASET}.all_spans" \\
  spans/all_spans.jsonl \\
  schema/all_spans.schema.json
```

Both commands take the same `--location`; a load into a dataset that lives
somewhere else fails. Any location works as long as the two agree.

## Review it with AQuA

Point an AQuA deployment at the table instead of at a live linked dataset, and
the existing ingestion path reads the dump exactly as it reads production:

```
TELEMETRY_INGESTION_SOURCE=cloud_ops
AQA_TELEMETRY_DATASET=<dataset>
AQA_TELEMETRY_TABLE=all_spans
```

## What is in the archive

| Path | Content |
| --- | --- |
| `spans/all_spans.jsonl` | the spans, one JSON object per line |
| `schema/all_spans.schema.json` | the `bq load` schema for those rows |
| `manifest.json` | what was dumped, from where, over which range |
| `README.md` | this file |
"""


def render_readme(location: str = "") -> str:
    """Renders the archive README for a dump produced in `location`.

    Args:
        location: Region the deployment ran in, used for the `bq` commands.
            Falls back to `DEFAULT_DATASET_LOCATION` when unknown.

    Returns:
        README text with the location substituted in.
    """
    return _README_TEMPLATE.replace(
        _LOCATION_PLACEHOLDER, location or DEFAULT_DATASET_LOCATION
    )


class DumpError(Exception):
    """Raised when trace dump extraction fails with actionable error details."""


@dataclasses.dataclass(frozen=True)
class TimeRange:
    """Represents an inclusive UTC time interval.

    Attributes:
        since: Start timestamp (inclusive).
        until: End timestamp (inclusive).
    """

    since: dt.datetime
    until: dt.datetime

    def covers(self, moment: dt.datetime) -> bool:
        """Checks whether a given timestamp falls within this range.

        Args:
            moment: UTC datetime to check.

        Returns:
            True if moment is between since and until (inclusive).
        """
        return self.since <= moment <= self.until

    def widen(self) -> TimeRange:
        """Expands the range by `_BOUNDARY_SLACK` on both ends.

        Returns:
            New TimeRange extended by one hour before `since` and after `until`.
        """
        return TimeRange(
            self.since - _BOUNDARY_SLACK, self.until + _BOUNDARY_SLACK
        )

    def to_json(self) -> dict[str, str]:
        """Serializes the range bounds to ISO 8601 UTC strings.

        Returns:
            Dictionary with 'since' and 'until' timestamp strings.
        """
        return {
            "since": _format_iso_timestamp(self.since),
            "until": _format_iso_timestamp(self.until),
        }


@dataclasses.dataclass
class DumpResult:
    """Summary of extracted trace dump contents and metadata.

    Attributes:
        bucket: Source GCS bucket name.
        time_range: Requested query time window.
        objects_listed: Total GCS objects matched in prefix scan.
        objects_read: GCS objects successfully processed.
        spans: Total span count matching the filter criteria.
        spans_by_agent: Span count breakdown by agent name.
        agent_versions: Set of distinct service versions observed in spans.
        first_end_time: Earliest span end timestamp in the dump.
        last_end_time: Latest span end timestamp in the dump.
    """

    bucket: str
    time_range: TimeRange
    objects_listed: int = 0
    objects_read: int = 0
    spans: int = 0
    spans_by_agent: dict[str, int] = dataclasses.field(default_factory=dict)
    agent_versions: set[str] = dataclasses.field(default_factory=set)
    first_end_time: str | None = None
    last_end_time: str | None = None


# --------------------------------------------------------------------------- #
# Resolving the bucket                                                         #
# --------------------------------------------------------------------------- #
def resolve_traces_bucket(state_root: Path) -> str:
    """Resolves the telemetry traces GCS bucket from local Terraform state.

    Reads from disk so the bucket can be determined even when the deployment
    is unreachable. Defaults to the provisioned jobs bucket.

    Args:
        state_root: Path to the `.aqua` state directory in the project.

    Returns:
        Name of the GCS bucket storing telemetry spans.

    Raises:
        DumpError: If the Terraform state file is missing or has no bucket outputs.
    """
    path = build_state_path(state_root, DEPLOYMENT_KEY)
    if not path.is_file():
        raise DumpError(
            f"No Terraform state at {path}. The traces bucket is read out of "
            f"{STATE_DIR_NAME}/, so run this from the observed agent's project "
            "root, and provision AQuA first: agents-cli infra single-project "
            "--apply --apply-aqua."
        )
    buckets = read_state_outputs(state_root, DEPLOYMENT_KEY).get("buckets")
    name = buckets.get("jobs") if isinstance(buckets, dict) else None
    if not name:
        raise DumpError(
            f"{path} records no `buckets` output, so the traces bucket is "
            "unknown. Re-apply the deployment root: agents-cli infra "
            "single-project --apply."
        )
    return str(name)


# --------------------------------------------------------------------------- #
# The range, and the object prefixes it covers                                 #
# --------------------------------------------------------------------------- #
def parse_moment(text: str, *, now: dt.datetime) -> dt.datetime:
    """Parses a duration string or ISO 8601 timestamp into a UTC datetime.

    Args:
        text: Relative duration (e.g. "7d", "36h", "90m") relative to `now`,
            or an ISO 8601 date/time string. Timestamps without a timezone are
            treated as UTC.
        now: Reference timestamp used to calculate relative durations.

    Returns:
        Parsed datetime in UTC timezone.

    Raises:
        DumpError: If `text` cannot be parsed as a duration or ISO timestamp.
    """
    text = text.strip()
    duration = _DURATION_RE.match(text)
    if duration:
        unit = _DURATION_UNITS[duration["unit"]]
        return now - dt.timedelta(**{unit: int(duration["count"])})
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DumpError(
            f"{text!r} is neither a duration (7d, 36h, 90m) nor an ISO date or "
            "datetime (2026-09-01, 2026-09-01T12:00:00)."
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def build_hour_prefixes(
    time_range: TimeRange, *, prefix: str = SPAN_OBJECT_PREFIX
) -> list[str]:
    """Generates all hourly partition prefixes covering the given time range.

    Args:
        time_range: Time window to cover.
        prefix: Base GCS object prefix.

    Returns:
        Sorted list of hourly partition prefix strings in chronological order.
    """
    hour = time_range.since.astimezone(dt.UTC).replace(
        minute=0, second=0, microsecond=0
    )
    last = time_range.until.astimezone(dt.UTC)
    prefixes: list[str] = []
    while hour <= last:
        prefixes.append(f"{prefix}/dt={hour:%Y-%m-%d}/{hour:%H}/")
        hour += dt.timedelta(hours=1)
    return prefixes


def _extract_day_prefixes(hours: Iterable[str]) -> list[str]:
    """Extracts unique daily prefixes from a collection of hourly prefixes.

    Listing GCS at daily granularity reduces API round-trips compared to querying
    each hour individually over long time windows.

    Args:
        hours: Hourly partition prefix strings.

    Returns:
        Sorted list of distinct daily prefix strings.
    """
    days = {hour.rsplit("/", 2)[0] + "/" for hour in hours}
    return sorted(days)


def list_span_objects(
    bucket: str,
    token: str,
    time_range: TimeRange,
    *,
    prefix: str = SPAN_OBJECT_PREFIX,
) -> list[str]:
    """Lists GCS object keys whose write partition overlaps with `time_range`.

    Args:
        bucket: Target GCS bucket name.
        token: OAuth2 access token.
        time_range: Time window to search.
        prefix: Base telemetry object prefix.

    Returns:
        Sorted list of matching GCS object names.

    Raises:
        GcsError: If bucket listing fails.
    """
    hours = tuple(build_hour_prefixes(time_range, prefix=prefix))
    found: list[str] = []
    for day in _extract_day_prefixes(hours):
        for item in gcs.list_objects(bucket, token, day, REMEDIATION):
            name = str(item.get("name") or "")
            if name.startswith(hours):
                found.append(name)
    return sorted(found)


# --------------------------------------------------------------------------- #
# Reading rows                                                                 #
# --------------------------------------------------------------------------- #
def iter_rows(
    chunks: Iterable[bytes],
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    """Decompresses and parses a gzipped NDJSON stream line by line.

    Streams decompression so only line fragments remain in memory. Yields
    both raw bytes (to preserve original formatting during archiving) and parsed
    JSON objects for filtering. Malformed JSON lines are skipped.

    Args:
        chunks: Iterable of compressed byte chunks.

    Yields:
        Tuples of `(raw_line_bytes, parsed_json_dict)`.

    Raises:
        DumpError: If decompression fails due to invalid gzip data.
    """
    # Configure zlib for gzip header and stream decompression.
    decompressor = zlib.decompressobj(wbits=31)
    pending = b""
    try:
        for chunk in chunks:
            pending += decompressor.decompress(chunk)
            lines = pending.split(b"\n")
            pending = lines.pop()
            yield from _parse_lines(lines)
        pending += decompressor.flush()
    except zlib.error as exc:
        raise DumpError(f"a span object is not readable gzip: {exc}") from exc
    yield from _parse_lines(pending.split(b"\n"))


def _parse_lines(
    lines: Iterable[bytes],
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield line, row


def extract_end_time(row: dict[str, Any]) -> dt.datetime | None:
    """Extracts the span `end_time` attribute as a UTC datetime.

    Args:
        row: Parsed span JSON dictionary.

    Returns:
        UTC datetime instance, or None if `end_time` is missing or invalid.
    """
    raw = row.get("end_time")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _extract_attribute(row: dict[str, Any], key: str) -> str:
    attributes = row.get("attributes")
    value = attributes.get(key) if isinstance(attributes, dict) else None
    return str(value) if value else ""


def _extract_service_version(row: dict[str, Any]) -> str:
    resource = row.get("resource")
    attributes = (
        resource.get("attributes") if isinstance(resource, dict) else None
    )
    value = (
        attributes.get(SERVICE_VERSION_ATTRIBUTE)
        if isinstance(attributes, dict)
        else None
    )
    return str(value) if value else ""


# --------------------------------------------------------------------------- #
# Writing the dump                                                             #
# --------------------------------------------------------------------------- #
def write_dump(
    *,
    output: Path,
    bucket: str,
    token: str,
    time_range: TimeRange,
    object_names: list[str],
    manifest_extra: Callable[[DumpResult], dict[str, Any]],
    progress: Callable[[str], None],
    location: str = "",
    schema_path: Path | None = None,
) -> DumpResult:
    """Streams and filters GCS span objects into a consolidated zip archive.

    Downloads listed objects, filters rows by `time_range`, and writes matching
    spans along with schema, manifest, and README into `output`. Every row lands
    in the single `SPANS_ENTRY`, which is what `bq load` can consume in one go.

    Args:
        output: Destination zip archive path.
        bucket: Source GCS bucket name.
        token: OAuth2 access token.
        time_range: Target time interval for span filtering.
        object_names: List of GCS object names to read.
        manifest_extra: Callback returning additional metadata for the manifest.
        progress: Callback invoked with status messages per processed object.
        location: Region the deployment ran in, for the README's `bq` commands.
        schema_path: Optional path to custom BigQuery schema file.

    Returns:
        DumpResult containing tally counts and actual timestamp bounds.

    Raises:
        DumpError: If schema is missing or archive generation fails.
        GcsError: If GCS read operations fail.
    """
    schema = schema_path or Path(__file__).with_name(SCHEMA_FILE)
    if not schema.is_file():
        # Validate schema existence upfront before initiating network downloads.
        raise DumpError(f"the `bq load` schema is not at {schema}.")
    result = DumpResult(
        bucket=bucket, time_range=time_range, objects_listed=len(object_names)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Write to a staging file first so aborted runs do not leave incomplete archives.
    staging = output.with_name(f"{output.name}.partial")
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            spans = _SpanEntry(archive)
            try:
                for index, name in enumerate(object_names, start=1):
                    progress(f"[{index}/{len(object_names)}] {name}")
                    result.spans += _copy_object(
                        spans,
                        bucket=bucket,
                        token=token,
                        name=name,
                        time_range=time_range,
                        result=result,
                    )
                    result.objects_read += 1
            finally:
                spans.close()
            archive.write(schema, SCHEMA_ENTRY)
            archive.writestr(README_ENTRY, render_readme(location))
            archive.writestr(
                MANIFEST_ENTRY,
                json.dumps(
                    _build_manifest(result, manifest_extra(result)), indent=2
                )
                + "\n",
            )
        staging.replace(output)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return result


class _SpanEntry:
    """Manages the single span entry in the zip archive, opening it lazily on first write."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self._stream: Any = None

    def write(self, line: bytes) -> None:
        """Appends one NDJSON row, opening the entry lazily if not already open.

        Args:
            line: Raw NDJSON bytes of a single span row.
        """
        if self._stream is None:
            self._stream = self._archive.open(SPANS_ENTRY, "w")
        self._stream.write(line + b"\n")

    def close(self) -> None:
        """Closes the entry stream if it was opened."""
        if self._stream is not None:
            self._stream.close()
            self._stream = None


def _copy_object(
    spans: _SpanEntry,
    *,
    bucket: str,
    token: str,
    name: str,
    time_range: TimeRange,
    result: DumpResult,
) -> int:
    """Streams and filters rows from a single GCS object into the span entry.

    Args:
        spans: Writer for the archive's span entry.
        bucket: GCS bucket name.
        token: OAuth2 access token.
        name: GCS object key name.
        time_range: Time window for filtering span rows.
        result: DumpResult accumulator updated with matching spans.

    Returns:
        Count of rows written to the archive for this object.
    """
    kept = 0
    with gcs.stream(bucket, token, name, REMEDIATION) as chunks:
        for line, row in iter_rows(chunks):
            end_time = extract_end_time(row)
            if end_time is None or not time_range.covers(end_time):
                continue
            spans.write(line)
            kept += 1
            _tally(result, row, end_time)
    return kept


def _tally(
    result: DumpResult, row: dict[str, Any], end_time: dt.datetime
) -> None:
    """Updates tally metrics and timestamp bounds with a matching span row.

    Args:
        result: DumpResult instance to update.
        row: Parsed span JSON dictionary.
        end_time: Span completion timestamp.
    """
    agent = _extract_attribute(row, AGENT_NAME_ATTRIBUTE) or "(unattributed)"
    result.spans_by_agent[agent] = result.spans_by_agent.get(agent, 0) + 1
    version = _extract_service_version(row)
    if version:
        result.agent_versions.add(version)
    stamp = _format_iso_timestamp(end_time)
    if result.first_end_time is None or stamp < result.first_end_time:
        result.first_end_time = stamp
    if result.last_end_time is None or stamp > result.last_end_time:
        result.last_end_time = stamp


def _build_manifest(
    result: DumpResult, extra: dict[str, Any]
) -> dict[str, Any]:
    """Builds the manifest dictionary describing archive contents and telemetry metadata.

    Args:
        result: Aggregated results from processing trace objects.
        extra: Additional deployment metadata to include in the manifest.

    Returns:
        Manifest dictionary ready for JSON serialization.
    """
    return {
        "source": {"bucket": result.bucket, "prefix": SPAN_OBJECT_PREFIX},
        "range": {
            "requested": result.time_range.to_json(),
            "actual": {
                "first_end_time": result.first_end_time,
                "last_end_time": result.last_end_time,
            },
        },
        "counts": {
            "objects_listed": result.objects_listed,
            "objects_read": result.objects_read,
            "spans": result.spans,
            "spans_by_agent": dict(sorted(result.spans_by_agent.items())),
        },
        "agent_versions": sorted(result.agent_versions),
        "agent_versions_note": (
            "The `service.version` resource attribute the exporter stamps: "
            "AQuA's own package version, not an Agent Runtime deployment "
            "revision. It does not distinguish one deployment from the next."
        ),
        **extra,
    }


def _format_iso_timestamp(moment: dt.datetime) -> str:
    return f"{moment.astimezone(dt.UTC):%Y-%m-%dT%H:%M:%S.%f}Z"
