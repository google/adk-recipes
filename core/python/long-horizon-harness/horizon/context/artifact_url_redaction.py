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

"""Hide signed artifact URLs from the model's view of tool results.

``artifact(action='save')`` returns a private signed GCS URL so display clients
can open the file (the web UI renders the A2A FilePart; Gemini Enterprise gets a
``📎`` link lifted from the function_response by ``a2a/executor.py``). That URL
also lands in the function_response the model reads on its continuation turn, and
the model pastes the whole credentialed blob into its reply. The clients already
render the link, so the model never needs the URL.

This redacts ``llm_request.contents`` — the deep copy ADK builds from
``session.events`` and actually sends to the model (ADK deep-copies the events
into the request, so a before_model callback must mutate the request, not the
events).
The signed URL stays in ``session.events`` (and on the wire), so the A2A
converter and saved task history still surface the link to the client.
"""

from __future__ import annotations

import logging
from typing import Any

from horizon.tools.names import ARTIFACT

logger = logging.getLogger(__name__)

REDACTED_URL = "[link delivered to the user automatically — do not repeat it]"


def redact_artifact_urls(contents: list[Any]) -> int:
    """Replace the ``url`` in artifact-save function_responses in place; returns count."""
    redacted = 0
    for content in contents or []:
        parts = getattr(content, "parts", None)
        if not parts:
            continue
        for part in parts:
            fr = getattr(part, "function_response", None)
            if fr is None or fr.name != ARTIFACT:
                continue
            resp = fr.response
            if (
                isinstance(resp, dict)
                and isinstance(resp.get("url"), str)
                and resp["url"] != REDACTED_URL
            ):
                resp["url"] = REDACTED_URL
                redacted += 1
    return redacted


async def redact_artifact_urls_callback(
    *, callback_context: Any, llm_request: Any
) -> None:
    count = redact_artifact_urls(getattr(llm_request, "contents", None))
    if count:
        logger.info(
            "artifact_url_redaction: hid %d signed URL(s) from the model", count
        )
