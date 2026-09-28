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

"""Deterministic tests for hiding signed artifact URLs from the model.

Assert only on the request-contents mutation (the url field), never on model
output. The callback must redact ``llm_request.contents`` — the deep copy the
model actually reads — not ``session.events``.
"""

from __future__ import annotations

import pytest
from google.genai.types import Content, FunctionResponse, Part

from app.context.artifact_url_redaction import (
    REDACTED_URL,
    redact_artifact_urls,
    redact_artifact_urls_callback,
)

_SIGNED = (
    "https://storage.googleapis.com/b/o/file.html/0?X-Goog-Signature=deadbeef"
)


def _content(name: str, response: dict) -> Content:
    return Content(
        role="user",
        parts=[
            Part(
                function_response=FunctionResponse(
                    id="x", name=name, response=response
                )
            )
        ],
    )


def _url(content: Content):
    return content.parts[0].function_response.response.get("url")


def test_artifact_save_url_is_redacted():
    c = _content(
        "artifact", {"success": True, "filename": "p.html", "url": _SIGNED}
    )
    assert redact_artifact_urls([c]) == 1
    assert _url(c) == REDACTED_URL
    # Other fields survive — the model still knows what was saved.
    resp = c.parts[0].function_response.response
    assert resp["success"] is True and resp["filename"] == "p.html"


def test_non_artifact_tool_url_untouched():
    c = _content("web_search", {"url": _SIGNED})
    assert redact_artifact_urls([c]) == 0
    assert _url(c) == _SIGNED


def test_artifact_load_without_url_is_noop():
    c = _content(
        "artifact", {"success": True, "filename": "p.html", "dest_path": "/x"}
    )
    assert redact_artifact_urls([c]) == 0


def test_redaction_is_idempotent():
    c = _content("artifact", {"url": _SIGNED})
    assert redact_artifact_urls([c]) == 1
    assert redact_artifact_urls([c]) == 0  # already redacted, not re-counted
    assert _url(c) == REDACTED_URL


@pytest.mark.asyncio
async def test_callback_redacts_request_contents():
    # The model reads llm_request.contents (a deep copy of session.events), so the
    # callback must mutate that, not the events.
    c = _content("artifact", {"filename": "p.html", "url": _SIGNED})
    llm_request = type("Req", (), {"contents": [c]})()
    await redact_artifact_urls_callback(
        callback_context=object(), llm_request=llm_request
    )
    assert _url(c) == REDACTED_URL
