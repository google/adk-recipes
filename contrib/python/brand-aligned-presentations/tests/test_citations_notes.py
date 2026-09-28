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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presentation_agent.sub_agents.synthesizer.agent import (
    batch_generate_slides,
)


class MockResponse:
    def __init__(self, text):
        self.text = text


@pytest.mark.asyncio
async def test_citation_recovery():
    """Verify that existing citations are preserved if the model doesn't provide them."""
    existing_citations = ["https://example.com/1"]
    slides = [
        {
            "title": "Slide with Citations",
            "layout_name": "Title and Content",
            "bullets": ["Existing focus"],
            "citations": existing_citations,
        }
    ]
    research_summary = "Rich research summary"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    # Mocking response WITHOUT citations
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(
            '{"title": "Slide with Citations", "layout_name": "Title and Content", "bullets": ["B1", "B2"]}'
        )
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        result = await batch_generate_slides(
            tool_context=tool_context,
            research_summary=research_summary,
            slides=slides,
        )

    assert result["status"] == "Success"
    final_slide = result["deck_spec"]["slides"][0]
    assert final_slide["citations"] == existing_citations
    print("Citation recovery test passed.")


@pytest.mark.asyncio
async def test_speaker_notes_recovery():
    """Verify that existing speaker notes are preserved if the model doesn't provide them."""
    existing_notes = "Manual speaker notes with custom URLs."
    slides = [
        {
            "title": "Slide with Notes",
            "layout_name": "Title and Content",
            "bullets": ["Existing focus"],
            "speaker_notes": existing_notes,
        }
    ]
    research_summary = "Rich research summary"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    # Mocking response WITHOUT speaker_notes
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(
            '{"title": "Slide with Notes", "layout_name": "Title and Content", "bullets": ["B1", "B2"]}'
        )
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        result = await batch_generate_slides(
            tool_context=tool_context,
            research_summary=research_summary,
            slides=slides,
        )

    assert result["status"] == "Success"
    final_slide = result["deck_spec"]["slides"][0]
    assert final_slide["speaker_notes"] == existing_notes
    print("Speaker notes recovery test passed.")


@pytest.mark.asyncio
async def test_speaker_notes_appending():
    """Verify that existing notes are appended if they are unique."""
    existing_notes = "Manual source: https://manual.com"
    generated_notes = "This is the generated narrative for the slide."
    slides = [
        {
            "title": "Slide with Merged Notes",
            "layout_name": "Title and Content",
            "bullets": ["Existing focus"],
            "speaker_notes": existing_notes,
        }
    ]
    research_summary = "Rich research summary"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    # Mocking response WITH speaker_notes
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(
            f'{{"title": "Slide with Merged Notes", "layout_name": "Title and Content", "bullets": ["B1"], "speaker_notes": "{generated_notes}"}}'
        )
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        result = await batch_generate_slides(
            tool_context=tool_context,
            research_summary=research_summary,
            slides=slides,
        )

    assert result["status"] == "Success"
    final_slide = result["deck_spec"]["slides"][0]
    assert generated_notes in final_slide["speaker_notes"]
    assert existing_notes in final_slide["speaker_notes"]
    assert (
        final_slide["speaker_notes"] == f"{generated_notes}\n\n{existing_notes}"
    )
    print("Speaker notes appending test passed.")


@pytest.mark.asyncio
async def test_no_duplicate_notes_appending():
    """Verify that existing notes are NOT appended if they are already in the generated notes."""
    existing_notes = "Manual source."
    generated_notes = "This is the generated narrative. Manual source."
    slides = [
        {
            "title": "Slide with Dupe Notes",
            "layout_name": "Title and Content",
            "bullets": ["Existing focus"],
            "speaker_notes": existing_notes,
        }
    ]
    research_summary = "Rich research summary"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    # Mocking response WITH speaker_notes that already contain the manual notes
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(
            f'{{"title": "Slide with Dupe Notes", "layout_name": "Title and Content", "bullets": ["B1"], "speaker_notes": "{generated_notes}"}}'
        )
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        result = await batch_generate_slides(
            tool_context=tool_context,
            research_summary=research_summary,
            slides=slides,
        )

    assert result["status"] == "Success"
    final_slide = result["deck_spec"]["slides"][0]
    assert final_slide["speaker_notes"] == generated_notes
    print("No duplicate notes appending test passed.")


@pytest.mark.asyncio
async def test_layout_sanitization_with_visual():
    """Verify that layout is updated to 'Title and Image' if a visual is present."""
    slides = [
        {
            "title": "Visual Slide",
            "layout_name": "Title and Content",
            "bullets": ["Existing focus"],
        }
    ]
    research_summary = "Rich research summary"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    # Mocking response WITH a visual prompt
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(
            '{"title": "Visual Slide", "layout_name": "Title and Content", "bullets": ["B1"], "visual_prompt": "image: A futuristic city"}'
        )
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        result = await batch_generate_slides(
            tool_context=tool_context,
            research_summary=research_summary,
            slides=slides,
        )

    assert result["status"] == "Success"
    final_slide = result["deck_spec"]["slides"][0]
    assert final_slide["layout_name"] == "Title and Image"
    print("Layout sanitization with visual test passed.")


if __name__ == "__main__":
    asyncio.run(test_citation_recovery())
    asyncio.run(test_speaker_notes_recovery())
    asyncio.run(test_speaker_notes_appending())
    asyncio.run(test_no_duplicate_notes_appending())
    asyncio.run(test_layout_sanitization_with_visual())
