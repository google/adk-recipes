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

"""Provides a Gemini model wrapper configured with an explicit serving location."""

from __future__ import annotations

from typing import Any

from ambient_quality_agent import config as config_module
from ambient_quality_agent.config import Model
from ambient_quality_agent.core.labels import build_request_labels
from ambient_quality_shared.usage_headers import (
    build_usage_headers,
    detect_platform,
)
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.genai import Client, types


class LocatedGemini(Gemini):
    """An ADK `Gemini` whose Gemini platform client is pinned to an explicit location.

    Its requests carry AQuA's engine product token after ADK's own tracking
    tokens.
    """

    # Extra field on top of Gemini's Pydantic model.
    location: str

    async def _preprocess_request(self, llm_request: LlmRequest) -> None:
        """Stamps AQA's billing label and engine product token on every ADK model call.

        This is the single chokepoint for all of AQA's ADK agents (orchestrator,
        metric builder, metric planner, RCA, summary): they set no
        `generate_content_config` of their own, so labeling here makes
        their Gemini spend attributable in Cloud Billing.

        Labels are set *before* delegating: ADK's own `_preprocess_request`
        nulls `config.labels` on the AI-Studio (API-key) backend, which does not
        support them, and leaves them intact on Gemini platform. `api_client` below pins
        this model to Gemini platform, so in practice the label always survives.
        Caller-set labels win, so an explicit override is never clobbered.

        The engine product token goes into the per-request headers because ADK
        sets ``user-agent`` and ``x-goog-api-client`` there on every request,
        overriding the client headers of the same name in genai. ADK prepends
        its tracking tokens after this method runs and keeps existing tokens, so
        AQuA's token survives.

        Args:
            llm_request: Outgoing LLM request to label and to stamp with the
                engine product token.
        """
        if llm_request.config is not None:
            llm_request.config.labels = build_request_labels() | (
                llm_request.config.labels or {}
            )
            http_options = (
                llm_request.config.http_options or types.HttpOptions()
            )
            headers = _append_usage_tokens(http_options.headers or {})
            # Empty while usage analytics is off: leave http_options untouched.
            if headers:
                http_options.headers = headers
                llm_request.config.http_options = http_options
        await super()._preprocess_request(llm_request)

    def _tracking_headers(self) -> dict[str, str]:
        return _append_usage_tokens(super()._tracking_headers())

    @property
    def api_client(self) -> Client:
        base_url, api_version = self._base_url_and_api_version
        http_options: dict[str, Any] = {
            "headers": self._tracking_headers(),
            "retry_options": self.retry_options,
            "base_url": base_url,
            "api_version": api_version or "v1beta1",
        }
        return Client(
            vertexai=True,
            project=config_module.config.project_id,
            location=self.location,
            http_options=types.HttpOptions(**http_options),
        )


def _append_usage_tokens(headers: dict[str, str]) -> dict[str, str]:
    """Appends AQuA's engine product token to the tracking headers.

    Args:
        headers: Client headers from ADK, or per-request headers (often empty
            or set by the caller). Only the lowercase ``user-agent`` and
            ``x-goog-api-client`` keys match; a missing key is created.

    Returns:
        A copy with the engine product token appended to both values, unless
        already present. Unchanged while usage analytics is off.
    """
    merged = dict(headers)
    for name, value in build_usage_headers(platform=detect_platform()).items():
        parts = merged.get(name, "").split()
        parts += [token for token in value.split() if token not in parts]
        merged[name] = " ".join(parts)
    return merged


def build_gemini(spec: Model) -> Gemini:
    """Builds a `Gemini` model served from `spec.location`.

    Different models are available in different locations not bound to
    `GOOGLE_CLOUD_LOCATION`. Requests it issues carry AQA's billing label;
    see `LocatedGemini._preprocess_request`.

    Args:
        spec: Model configuration specifying model name and serving location.

    Returns:
        Configured Gemini model instance.
    """
    return LocatedGemini(model=spec.model, location=spec.location)
