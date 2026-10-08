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

"""Live column definitions of a telemetry table.

Reads table metadata on every call without caching because the observed agent's
schema depends on its plugin or exporter version, and `aqua attach` can switch
the target table at runtime. Metadata reads run no queries and incur no cost.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Iterable


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
