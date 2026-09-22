# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The root-agent and Gemini subagent backends carry a robust HTTP retry config
so transient Vertex 429 RESOURCE_EXHAUSTED responses are retried with backoff
rather than surfaced on the first hit."""

from __future__ import annotations

from google.adk.models import Gemini


def _effective_retry_codes(opts) -> tuple[int, ...]:
    # When http_status_codes is unset the SDK applies its default transient set;
    # mirror that resolution so the test asserts the codes actually retried.
    if opts.http_status_codes:
        return tuple(opts.http_status_codes)
    from google.genai._api_client import _RETRY_HTTP_STATUS_CODES

    return tuple(_RETRY_HTTP_STATUS_CODES)


def test_robust_retry_options_retry_429_with_backoff():
    from horizon.models.registry import ROBUST_RETRY_OPTIONS

    # More than the old attempts=3 and the ADK docs' attempts=2 example: enough
    # retries to ride out a transient per-minute quota spike.
    assert ROBUST_RETRY_OPTIONS.attempts is not None
    assert ROBUST_RETRY_OPTIONS.attempts >= 5
    assert 429 in _effective_retry_codes(ROBUST_RETRY_OPTIONS)
    # Exponential backoff, not a tight hammer.
    assert (ROBUST_RETRY_OPTIONS.exp_base or 0) > 1
    assert (ROBUST_RETRY_OPTIONS.max_delay or 0) >= 30


def test_gemini_root_backend_uses_robust_retry():
    from horizon.models.registry import MODEL_REGISTRY, ROBUST_RETRY_OPTIONS

    entry = MODEL_REGISTRY["gemini-3.7-flash"]
    assert isinstance(entry, Gemini)
    assert entry.retry_options == ROBUST_RETRY_OPTIONS


def test_gemini_subagents_use_robust_retry():
    from horizon.models.registry import ROBUST_RETRY_OPTIONS
    from horizon.subagents.web_research import web_research_agent

    model = web_research_agent.model
    assert isinstance(model, Gemini)
    assert model.retry_options == ROBUST_RETRY_OPTIONS
