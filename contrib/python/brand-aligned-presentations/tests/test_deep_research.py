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

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from presentation_agent.sub_agents.deep_research.tools.deep_research_tool import (
    _deep_research_sync_impl,
    _parse_deep_research_stream,
    deep_research_search,
    latex_to_text,
)


def test_latex_to_text():
    text = r"The formula is $$\text{Hello} \times \approx \le \ge \frac$$"
    assert latex_to_text(text) == "The formula is Hello × ≈ ≤ ≥ "  # noqa: RUF001

    # Test without match
    assert latex_to_text("Normal text") == "Normal text"


def test_parse_deep_research_stream():
    chunk1 = MagicMock()
    chunk1.interaction.id = "123"
    chunk1.event_type = "content.delta"
    chunk1.delta.type = "text"
    chunk1.delta.text = "Hello"

    citation1 = MagicMock()
    citation1.source = "http://example.com"
    citation1.url = None
    citation1.title = None
    chunk1.delta.annotations = [citation1]

    chunk2 = MagicMock()
    chunk2.interaction.id = None
    chunk2.event_type = "content.delta"
    chunk2.delta.type = "text"
    chunk2.delta.text = " World"
    chunk2.delta.annotations = None

    # Empty chunk
    class EmptyChunk:
        pass

    # Chunk with delta = None
    chunk_no_delta = MagicMock()
    chunk_no_delta.event_type = "content.delta"
    chunk_no_delta.delta = None

    # Chunk with delta_type != text
    chunk_other_delta = MagicMock()
    chunk_other_delta.event_type = "content.delta"
    chunk_other_delta.delta.type = "other"

    stream = [chunk1, chunk2, EmptyChunk(), chunk_no_delta, chunk_other_delta]

    report, citations, interaction_id = _parse_deep_research_stream(stream)

    assert report == "Hello World"
    assert citations == ["http://example.com"]
    assert interaction_id == "123"

    # Test with url instead of source
    chunk3 = MagicMock()
    chunk3.interaction.id = "456"
    chunk3.event_type = "content.delta"
    chunk3.delta.type = "text"
    chunk3.delta.text = " Test"
    citation2 = MagicMock()
    citation2.source = None
    citation2.url = "http://test.com"
    citation2.title = None
    chunk3.delta.annotations = [citation2]

    report2, citations2, interaction_id2 = _parse_deep_research_stream([chunk3])
    assert report2 == " Test"
    assert citations2 == ["http://test.com"]
    assert interaction_id2 == "456"


@patch(
    "presentation_agent.sub_agents.deep_research.tools.deep_research_tool.genai.Client"
)
def test_deep_research_sync_impl(mock_client_class):
    mock_client = mock_client_class.return_value
    mock_interactions = mock_client.interactions

    chunk = MagicMock()
    chunk.interaction.id = "456"
    chunk.event_type = "content.delta"
    chunk.delta.type = "text"
    chunk.delta.text = "Result"
    citation = MagicMock()
    citation.source = "http://test.com"
    citation.url = None
    chunk.delta.annotations = [citation]

    mock_interactions.create.return_value = [chunk]

    result = _deep_research_sync_impl("test query")
    assert "Result" in result
    assert "http://test.com" in result
    assert "Extracted Sources:" in result

    # test without citations
    chunk_no_cite = MagicMock()
    chunk_no_cite.interaction.id = "789"
    chunk_no_cite.event_type = "content.delta"
    chunk_no_cite.delta.type = "text"
    chunk_no_cite.delta.text = "Result2"
    chunk_no_cite.delta.annotations = None
    mock_interactions.create.return_value = [chunk_no_cite]

    result2 = _deep_research_sync_impl("test query2")
    assert "Result2" in result2
    assert "Extracted Sources:" not in result2

    # Test error handling
    mock_interactions.create.side_effect = Exception("API Error")
    result_err = _deep_research_sync_impl("test query3")
    assert "Error: Deep Research failed" in result_err


@pytest.mark.asyncio
@patch(
    "presentation_agent.sub_agents.deep_research.tools.deep_research_tool._deep_research_sync_impl"
)
async def test_deep_research_search(mock_sync_impl):
    mock_sync_impl.return_value = "Async Result"

    result = await deep_research_search("query")
    assert result == "Async Result"

    # Test timeout
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result_timeout = await deep_research_search("query")
        assert "Deep Research timed out" in result_timeout
