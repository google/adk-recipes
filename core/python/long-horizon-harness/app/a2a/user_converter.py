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

"""Custom A2A request converter that supplies the authenticated user_id.

ADK's default `convert_a2a_request_to_agent_run_request` derives
`user_id = f"A2A_USER_{context_id}"`, which puts every chat in its own user
namespace — fine for single-shot agents, but it makes "list this browser's
chats" impossible (every chat is one session under one synthetic user).

We replace ``user_id`` with the identity resolved by ``IdentityMiddleware``
(stored on a ContextVar so the A2A executor — which doesn't receive the
FastAPI Request — can still read it). When no authenticated identity is
present (e.g. unit tests), we fall back to ADK's default.
"""

from __future__ import annotations

from a2a.server.agent_execution import RequestContext
from google.adk.a2a.converters.part_converter import (
    A2APartToGenAIPartConverter,
    convert_a2a_part_to_genai_part,
)
from google.adk.a2a.converters.request_converter import (
    AgentRunRequest,
    convert_a2a_request_to_agent_run_request,
)
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types as genai_types

from app.a2a.datapart import unwrap_a2a_datapart_text
from app.auth import get_user_id_from_context


def _detoxify_part(part: genai_types.Part | None) -> genai_types.Part | None:
    blob = getattr(part, "inline_data", None) if part is not None else None
    if blob is not None:
        text = unwrap_a2a_datapart_text(getattr(blob, "data", None))
        if text is not None:
            return genai_types.Part(text=text)
    return part


def _wrap_part_converter(
    inner: A2APartToGenAIPartConverter,
) -> A2APartToGenAIPartConverter:
    """Wrap a part converter so untyped DataPart blobs become readable text.

    ADK turns DataParts with no metadata-type key into an opaque
    ``<a2a_datapart_json>`` text/plain blob. We recover the payload as readable
    text right where the bad value is born, so the model reads content instead
    of a tagged blob.
    """

    def converter(a2a_part):
        result = inner(a2a_part)
        if isinstance(result, list):
            return [_detoxify_part(p) for p in result]
        return _detoxify_part(result)

    return converter


def request_converter(
    request: RequestContext,
    part_converter: A2APartToGenAIPartConverter = convert_a2a_part_to_genai_part,
) -> AgentRunRequest:
    run_req = convert_a2a_request_to_agent_run_request(
        request, _wrap_part_converter(part_converter)
    )
    authenticated = get_user_id_from_context()
    if authenticated:
        run_req.user_id = authenticated
    # Flip to SSE so the runner emits partial token deltas — the web UI
    # accumulates them for a typewriter effect (see chat-shell stream loop).
    # Preserve any custom_metadata upstream stashed under "a2a_metadata".
    existing_custom = (
        run_req.run_config.custom_metadata if run_req.run_config else {}
    ) or {}
    run_req.run_config = RunConfig(
        streaming_mode=StreamingMode.SSE,
        custom_metadata=existing_custom,
    )
    return run_req
