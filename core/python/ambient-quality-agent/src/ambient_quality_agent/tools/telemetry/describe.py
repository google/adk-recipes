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

"""The `describe_telemetry` chat tool.

Resolves the telemetry source and selector table from the effective
configuration on every call so the response reflects `agents-cli aqua attach`
updates. Matches the selector validator's table resolution so the reported
table is always the one selectors are checked against.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core import custom_investigation_skill
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.telemetry import schema
from google.adk.tools import ToolContext
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

if TYPE_CHECKING:
    from ambient_quality_agent.config import Config

logger = logging.getLogger(__name__)

SCHEMA_READ_TIMEOUT_SECONDS = 10.0
"""Per-request timeout and retry deadline for the table metadata read.

Not a hard bound on the tool: an attempt started just before the deadline still
gets its full timeout, and credential refresh is outside it. The read usually
takes under a second.
"""


def _create_default_client(config: Config) -> bigquery.Client:
    """Creates a BigQuery client for the telemetry dataset.

    Args:
        config: Effective configuration naming AQuA's project and the telemetry
            location.

    Returns:
        A BigQuery client bound to AQuA's project and the telemetry location.
        Reading another project's table metadata needs no client there.
    """
    return bigquery.Client(
        project=config.project_id, location=config.telemetry_location
    )


client_factory: Callable[[Config], bigquery.Client] = _create_default_client
"""Factory for the client that reads table metadata; swappable for testing."""


def _load_table_columns(
    config: Config, table: str
) -> list[schema.TelemetryColumn]:
    """Reads a table's live columns using a new BigQuery client.

    Runs on a worker thread because credential resolution and metadata reads
    perform blocking network I/O.

    Args:
        config: Effective configuration used to initialize the client.
        table: Fully-qualified `project.dataset.table` reference.

    Returns:
        The table's columns in definition order.
    """
    with client_factory(config) as client:
        return schema.load_telemetry_schema(
            table, client=client, timeout=SCHEMA_READ_TIMEOUT_SECONDS
        )


def _format_read_error(exc: Exception) -> str:
    """Formats a schema read failure for the model.

    A BigQuery API error's string form appends its `details`, which can carry
    kilobytes of server-side stack trace; its `message` keeps only the cause.
    An error with an empty `message` falls back to the string form, which
    still names the status code.

    Args:
        exc: The exception raised while reading the table metadata.

    Returns:
        A short description of the failure.
    """
    if isinstance(exc, api_exceptions.GoogleAPICallError) and exc.message:
        return exc.message
    # An API error with no message renders as "<code> ", trailing space included.
    return str(exc).strip()


async def describe_telemetry(tool_context: ToolContext) -> dict[str, Any]:
    """Describe the observed agent's telemetry table as a selector must read it.

    Call this before writing a selector or a telemetry query. The columns are
    read from the live table, so a column missing from them does not exist.

    Returns:
        `source`: the telemetry source (`big_query`, `cloud_ops` or
        `cloud_logging`). `table`: the one fully-qualified table a selector may
        read. `selector_recipes`: the `skill_name` and `file_path` to pass to
        `load_skill_resource` for this source's column notes and recipes.
        `columns`: the table's columns with type, mode, description and nested
        fields. On failure, `error` says what went wrong; when only reading
        the table failed, `source`, `table` and `selector_recipes` are still
        returned.
    """
    try:
        config = effective_config.load(tool_context.state)
        source = config.telemetry_ingestion_source
        table = selector.build_selector_table_ref(
            source,
            project_id=config.resolve_observed_project_id(),
            dataset=config.telemetry_dataset,
            telemetry_table=config.telemetry_table,
        )
        selector_recipes = custom_investigation_skill.resolve_selector_recipes(
            source
        )
    except Exception as exc:
        logger.warning(
            "Could not resolve the telemetry table to describe", exc_info=True
        )
        return {
            "error": f"Failed to read this deployment's configuration: {exc}"
        }
    described: dict[str, Any] = {
        "source": source,
        "table": table,
        "selector_recipes": selector_recipes,
    }
    try:
        columns = await asyncio.to_thread(_load_table_columns, config, table)
    except Exception as exc:
        logger.warning("Could not read the schema of %s", table, exc_info=True)
        return {
            **described,
            "error": (
                f"Could not read the schema of {table}: "
                f"{_format_read_error(exc)}"
            ),
        }
    return {**described, "columns": [column.to_payload() for column in columns]}
