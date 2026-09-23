# Copyright 2026 Google LLC
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presentation_agent.sub_agents.synthesizer.agent import (
    batch_generate_slides,
)


class MockResponse:
    def __init__(self, text):
        self.text = text


@pytest.mark.asyncio
async def test_citation_extraction_logic():
    # 1. Setup mock slide and research summary WITH citations
    slides = [
        {
            "title": "Market Trends",
            "layout_name": "Title and Content",
            "bullets": ["Focus on growth"],
        }
    ]
    research_summary = "Market is growing at 15% [https://market-reports.com/2026]. New entrants are increasing (Source: https://news.com/biotech)."

    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()

    # 2. Mock the Slide Writer's response to include the extracted citations
    # This simulates the model following the NEW SYNTHESIZER_SLIDE_INSTRUCTION
    mock_slide_output = {
        "title": "Market Trends",
        "bullets": [
            "Market growth is hitting 15% annually.",
            "New biotech entrants are surging.",
        ],
        "layout_name": "Title and Content",
        "citations": [
            "https://market-reports.com/2026",
            "https://news.com/biotech",
        ],
        "speaker_notes": "The market shows strong signs of expansion.",
    }

    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(json.dumps(mock_slide_output))
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

    # 3. Verify the result contains the citations
    assert result["status"] == "Success"
    generated_slide = result["deck_spec"]["slides"][0]
    assert "citations" in generated_slide
    assert len(generated_slide["citations"]) == 2
    assert "https://market-reports.com/2026" in generated_slide["citations"]
    assert "https://news.com/biotech" in generated_slide["citations"]
    print("\n✅ Citation extraction logic verified in DeckSpec.")
