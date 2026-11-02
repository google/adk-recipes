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

"""Gemini structured JSON generation client with automatic retries.

Provides a unified Gemini platform client configuration for clustering, merging, and
review producers with standard retry policies and telemetry labels.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from google.genai import types

if TYPE_CHECKING:
    from ambient_quality_agent.config import Model

CHUNK_ATTEMPTS = 4
"""Total model call attempts per request."""

CALL_TIMEOUT_MS = 15 * 60 * 1000
"""Default HTTP timeout per attempt, in milliseconds.

The SDK sets no timeout, so a stalled connection blocks the call, and the
review page that waits on it, indefinitely. The SDK retries a timed-out attempt
within `CHUNK_ATTEMPTS`. Session review calls take 147 s at p99, and two full
5,553-case runs completed with this value on every caller."""

LONG_CALL_TIMEOUT_MS = 45 * 60 * 1000
"""HTTP timeout per attempt, in milliseconds, for calls with a large output.

A single clustering call over 4,000 findings took 26 minutes, so
`CALL_TIMEOUT_MS` would cut off legitimate work."""

# Defined as an integer constant because HTTP 499 is absent from http.HTTPStatus.
_CLIENT_CLOSED_REQUEST = 499

RETRY_STATUS_CODES = [
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.TOO_MANY_REQUESTS,
    _CLIENT_CLOSED_REQUEST,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
]
"""HTTP status codes eligible for automatic retry on transient serving errors."""


def call_gemini(
    prompt: str,
    schema: Any,
    thinking_level: types.ThinkingLevel | None = types.ThinkingLevel.MEDIUM,
    model: Model | None = None,
    temperature: float | None = None,
    timeout_ms: int = CALL_TIMEOUT_MS,
) -> Any:
    """Executes a Gemini request with structured JSON output constrained to schema.

    Initializes a Gemini platform client with transient retry settings and requests
    content generation with structured JSON schema enforcement.

    Args:
        prompt: Prompt text sent to the model.
        schema: Pydantic model or schema definition constraining the output JSON.
        thinking_level: Thinking budget for reasoning models. Defaults to MEDIUM
            to prevent token exhaustion on structured IDs. Pass None to omit
            thinking configuration.
        model: Optional model configuration override; defaults to ``config.insights_model``.
        temperature: Sampling temperature. When None, uses the model default.
        timeout_ms: HTTP timeout per attempt, in milliseconds.

    Returns:
        The GenerateContentResponse from the Gemini model.
    """
    from ambient_quality_agent import clients
    from ambient_quality_agent.config import config
    from ambient_quality_agent.core.labels import build_request_labels

    spec = model or config.insights_model
    client = clients.create_genai_client(
        project=config.project_id,
        location=spec.location,
        # Retry transient serving errors encountered during long-running calls.
        retry_options=types.HttpRetryOptions(
            attempts=CHUNK_ATTEMPTS,
            http_status_codes=RETRY_STATUS_CODES,
        ),
        timeout_ms=timeout_ms,
    )
    return client.models.generate_content(
        model=spec.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            # Omit thinking_config when None so models or generations without
            # thinking support do not reject the request with INVALID_ARGUMENT.
            thinking_config=(
                types.ThinkingConfig(thinking_level=thinking_level)
                if thinking_level is not None
                else None
            ),
            # Passing None retains the model's default temperature setting.
            temperature=temperature,
            labels=build_request_labels(),
        ),
    )
