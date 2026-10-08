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

"""Attaches GCS span export to the application's existing OpenTelemetry tracer provider.

ADK initializes the global `TracerProvider` with default span processors.
This module registers the GCS span exporter with the active provider to preserve
existing telemetry exporters and debug routes.
"""

from __future__ import annotations

import logging
import os

import ambient_quality_agent
from ambient_quality_agent import config as config_module
from ambient_quality_agent.telemetry.gcs_span_exporter import GcsSpanExporter
from ambient_quality_agent.tools import gcs
from ambient_quality_shared.telemetry import SPAN_OBJECT_PREFIX
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

SERVICE_NAME = "ambient_quality_agent"
"""``service.name`` stamped on every exported row."""

_FLUSH_TIMEOUT_MILLIS = 10_000

_EXPORT_SCHEDULE_DELAY_MILLIS = 30_000
"""Delay in milliseconds between span batch exports.

A 30-second window batches spans into fewer GCS objects to optimize downstream
listing and query performance. Configured explicitly to avoid setting
`OTEL_BSP_SCHEDULE_DELAY`, which is process-global and would alter ADK's batch
processors.

When spans arrive faster than the export queue drains, the SDK's
`BatchProcessor` drops them and logs a de-duplicated warning.
"""

_UNRESOLVED_AGENT_VERSION = "0.0.0"
"""Fallback version set by the deployment tool when project version is unresolved."""


def _resolve_service_version() -> str:
    """Resolves the service version for exported span resources.

    Returns:
        The `AGENT_VERSION` environment variable if set and valid, otherwise the
        package version.
    """
    stamped = os.environ.get("AGENT_VERSION", "").strip()
    if stamped and stamped != _UNRESOLVED_AGENT_VERSION:
        return stamped
    return ambient_quality_agent.__version__


def attach_gcs_exporter(provider: trace.TracerProvider) -> bool:
    """Attaches the GCS span exporter to the specified tracer provider.

    Args:
        provider: OpenTelemetry tracer provider instance.

    Returns:
        True if the exporter was successfully attached, False otherwise.
    """
    config = config_module.load()
    if not config.traces_gcs_bucket:
        logger.info("No AQA_TRACES_GCS_BUCKET; spans are not exported to GCS.")
        return False
    if not isinstance(provider, TracerProvider):
        logger.warning(
            "Tracer provider is %s, not an SDK TracerProvider; spans are not "
            "exported to GCS.",
            type(provider).__name__,
        )
        return False

    provider.add_span_processor(
        BatchSpanProcessor(
            GcsSpanExporter(
                bucket=config.traces_gcs_bucket,
                prefix=SPAN_OBJECT_PREFIX,
                resource_attributes={
                    "service.name": SERVICE_NAME,
                    "service.version": _resolve_service_version(),
                },
                client_factory=gcs.create_storage_client,
            ),
            schedule_delay_millis=_EXPORT_SCHEDULE_DELAY_MILLIS,
        )
    )
    logger.info(
        "Exporting spans to gs://%s/%s.",
        config.traces_gcs_bucket,
        SPAN_OBJECT_PREFIX,
    )
    return True


def flush_spans() -> None:
    """Flushes currently batched spans to storage on a best-effort basis."""
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush is None:
        return
    try:
        flush(_FLUSH_TIMEOUT_MILLIS)
    except Exception:
        logger.exception("Failed to flush spans.")


def shutdown_spans() -> None:
    """Flushes and shuts down active span processors on a best-effort basis."""
    flush_spans()
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is None:
        return
    try:
        shutdown()
    except Exception:
        logger.exception("Failed to shut down span processors.")
