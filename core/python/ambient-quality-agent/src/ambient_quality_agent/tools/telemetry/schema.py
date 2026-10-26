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

"""Column definitions of a telemetry table: those it has, and those ingestion needs.

`load_telemetry_schema` reads a table's live columns on every call, without
caching, because the observed agent's schema depends on its plugin or exporter
version, and `aqua attach` can switch the target table at runtime. Metadata reads
run no queries and incur no cost.

`load_required_schema` loads a telemetry source's required columns from
`ambient_quality_shared.schemas`, and `list_schema_mismatches` checks a table's
columns against them.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from typing import TYPE_CHECKING, Any

from ambient_quality_shared.schemas import SCHEMA_DIR
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Iterable

_LEGACY_TYPE_NAMES = {
    "BOOL": "BOOLEAN",
    "FLOAT64": "FLOAT",
    "INT64": "INTEGER",
    "STRUCT": "RECORD",
}
"""Standard SQL type names mapped to their legacy BigQuery equivalents, so
columns compare equal under either spelling."""


@dataclasses.dataclass(frozen=True)
class TelemetryColumn:
    """One column of a telemetry table, with its nested fields.

    Attributes:
        name: Column name.
        field_type: BigQuery type, such as `STRING`, `JSON` or `RECORD`.
        mode: `NULLABLE`, `REQUIRED` or `REPEATED`.
        description: Column description from the table metadata, or empty.
        fields: Nested fields of a `RECORD` column, empty otherwise.
    """

    name: str
    field_type: str
    mode: str
    description: str = ""
    fields: tuple[TelemetryColumn, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Formats the column as a JSON-safe dictionary for a tool response.

        Empty descriptions and nested fields are left out to keep the payload
        small for wide tables.

        Returns:
            Dictionary with `name`, `type` and `mode`, plus `description` and
            `fields` when they are set.
        """
        payload: dict[str, Any] = {
            "name": self.name,
            "type": self.field_type,
            "mode": self.mode,
        }
        if self.description:
            payload["description"] = self.description
        if self.fields:
            payload["fields"] = [field.to_payload() for field in self.fields]
        return payload


def load_telemetry_schema(
    table_ref: str, *, client: bigquery.Client, timeout: float
) -> list[TelemetryColumn]:
    """Reads a table's live columns from its BigQuery metadata.

    Args:
        table_ref: Fully-qualified `project.dataset.table` reference.
        client: BigQuery client used to read the table metadata.
        timeout: Seconds allowed for each request, and for retrying failed
            ones.

    Returns:
        The table's columns in definition order.

    Raises:
        google.api_core.exceptions.GoogleAPIError: If the metadata read fails,
            such as when the table does not exist, access is denied, or the
            retries run out of time.
        google.auth.exceptions.GoogleAuthError: If credentials cannot be
            obtained or refreshed.
    """
    # Override the multi-minute default retry deadline to keep chat responses fast.
    table = client.get_table(
        table_ref,
        retry=bigquery.DEFAULT_RETRY.with_timeout(timeout),
        timeout=timeout,
    )
    return _schema_fields_to_columns(table.schema)


def _schema_fields_to_columns(
    fields: Iterable[bigquery.SchemaField],
) -> list[TelemetryColumn]:
    """Converts BigQuery schema fields, recursing into `RECORD` fields.

    Args:
        fields: Schema fields of a table or of a parent `RECORD` field.

    Returns:
        The converted columns in definition order.
    """
    return [
        TelemetryColumn(
            name=field.name,
            field_type=field.field_type,
            mode=field.mode,
            description=field.description or "",
            fields=tuple(_schema_fields_to_columns(field.fields)),
        )
        for field in fields
    ]


@functools.cache
def load_required_schema(file_name: str) -> tuple[bigquery.SchemaField, ...]:
    """Reads a schema file from `ambient_quality_shared.schemas`.

    Args:
        file_name: Schema file name, such as ``agent_events.schema.json``.

    Returns:
        The columns the file lists, in order.

    Raises:
        FileNotFoundError: If no schema file has that name.
    """
    fields = json.loads((SCHEMA_DIR / file_name).read_text(encoding="utf-8"))
    return tuple(bigquery.SchemaField.from_api_repr(field) for field in fields)


def list_schema_mismatches(
    have: Iterable[bigquery.SchemaField], want: Iterable[bigquery.SchemaField]
) -> dict[str, str]:
    """Lists the columns of `want` that `have` lacks or holds in another shape.

    Names compare case-insensitively, standard SQL type names match their legacy
    equivalents (such as ``INT64`` and ``INTEGER``), and NULLABLE matches
    REQUIRED, while REPEATED must match exactly. Extra columns in `have` are
    ignored, and RECORD fields are compared recursively.

    Args:
        have: Columns of the table.
        want: Required columns.

    Returns:
        Mismatch description per column path (such as ``status.code``) in `want`
        order: ``"missing"``, ``"<held>, expected <wanted>"``, ``"REPEATED"``,
        or ``"not REPEATED"``. Empty when `have` satisfies `want`.
    """
    held_by_name = {field.name.lower(): field for field in have}
    mismatches: dict[str, str] = {}
    for wanted in want:
        held = held_by_name.get(wanted.name.lower())
        if held is None:
            mismatches[wanted.name] = "missing"
            continue
        held_type = _LEGACY_TYPE_NAMES.get(held.field_type, held.field_type)
        wanted_type = _LEGACY_TYPE_NAMES.get(
            wanted.field_type, wanted.field_type
        )
        if held_type != wanted_type:
            mismatches[wanted.name] = (
                f"{held.field_type}, expected {wanted.field_type}"
            )
        elif (held.mode == "REPEATED") != (wanted.mode == "REPEATED"):
            mismatches[wanted.name] = (
                "REPEATED" if held.mode == "REPEATED" else "not REPEATED"
            )
        else:
            nested = list_schema_mismatches(held.fields, wanted.fields)
            for path, mismatch in nested.items():
                mismatches[f"{wanted.name}.{path}"] = mismatch
    return mismatches
