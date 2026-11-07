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
"""Tests for collect_research_sources_callback."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.genai import types


def _grounded_event(url: str, claim: str) -> SimpleNamespace:
    """An event whose grounding metadata cites `url` for `claim`."""
    return SimpleNamespace(
        grounding_metadata=types.GroundingMetadata(
            grounding_chunks=[
                types.GroundingChunk(
                    web=types.GroundingChunkWeb(
                        uri=url, title=url, domain="example.com"
                    )
                )
            ],
            grounding_supports=[
                types.GroundingSupport(
                    segment=types.Segment(text=claim),
                    grounding_chunk_indices=[0],
                    confidence_scores=[0.9],
                )
            ],
        )
    )


def test_each_claim_is_collected_once_across_search_rounds() -> None:
    """Later callback calls must not re-add claims from earlier searches."""
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
    os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")
    with patch(
        "google.auth.default", return_value=(MagicMock(), "test-project")
    ):
        from app.agent import collect_research_sources_callback

    session = SimpleNamespace(events=[])
    callback_context = SimpleNamespace(
        _invocation_context=SimpleNamespace(session=session), state={}
    )
    # section_researcher, then two enhanced_search_executor rounds; the
    # callback runs after each one over the whole session history.
    for url, claim in [
        ("https://a.example.com", "claim A"),
        ("https://b.example.com", "claim B"),
        ("https://a.example.com", "claim A2"),
    ]:
        session.events.append(_grounded_event(url, claim))
        collect_research_sources_callback(callback_context)

    claims = {
        short_id: (
            source["url"],
            [c["text_segment"] for c in source["supported_claims"]],
        )
        for short_id, source in callback_context.state["sources"].items()
    }
    assert claims == {
        "src-1": ("https://a.example.com", ["claim A", "claim A2"]),
        "src-2": ("https://b.example.com", ["claim B"]),
    }
