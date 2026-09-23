# Copyright 2026 Google LLC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presentation_agent.sub_agents.google_research.agent import (
    google_research_grounded_tool,
)


@pytest.mark.asyncio
async def test_google_research_programmatic_extraction():
    """
    Mocks the research runner to verify URIs are extracted from grounding chunks.
    """
    mock_event = MagicMock()
    # Mocking the complex nested structure of groundingMetadata
    mock_event.model_dump.return_value = {
        "content": {
            "parts": [
                {
                    "text": "Research finding text.",
                    "groundingMetadata": {
                        "groundingChunks": [
                            {
                                "web": {
                                    "uri": "https://verified-source.com",
                                    "title": "Verified Title",
                                }
                            }
                        ]
                    },
                }
            ]
        }
    }

    # We also need the content attribute to be present for the text extraction part
    mock_part = MagicMock()
    mock_part.text = "Research finding text."
    mock_event.content.parts = [mock_part]

    async def mock_run_async(*args, **kwargs):
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = mock_run_async

    with patch(
        "presentation_agent.sub_agents.google_research.agent.Runner",
        return_value=mock_runner,
    ):
        with patch(
            "presentation_agent.sub_agents.google_research.agent.InMemorySessionService",
            return_value=AsyncMock(),
        ):
            result = await google_research_grounded_tool("test query")

            assert "Research finding text." in result
            assert (
                "### Verified Source URLs (Programmatic Grounding):" in result
            )
            assert "https://verified-source.com" in result
