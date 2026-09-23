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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presentation_agent.sub_agents.synthesizer.agent import (
    batch_generate_slides,
)


class MockResponse:
    def __init__(self, text):
        self.text = text


@pytest.mark.asyncio
async def test_batch_generate_slides_success():
    slides = [
        {
            "title": "Title A",
            "layout_name": "Title and Content",
            "bullets": ["A focus"],
        }
    ]
    research_summary = "Sum A"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    # Mocking the async generate_content call
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse(
            '{"title": "Title A", "layout_name": "Title and Content", "bullets": ["B1"]}'
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
    assert len(result["deck_spec"]["slides"]) == 1
    assert result["deck_spec"]["slides"][0]["title"] == "Title A"
    assert result["deck_spec"]["slides"][0]["bullets"] == ["B1"]


@pytest.mark.asyncio
async def test_batch_generate_slides_json_error():
    slides = [
        {
            "title": "Title A",
            "layout_name": "Title and Content",
            "bullets": ["A focus"],
        }
    ]
    research_summary = "Sum A"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=MockResponse("Invalid JSON")
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

    assert (
        result["status"] == "Success"
    )  # The tool returns Success even if some slides fail
    assert result["deck_spec"]["slides"][0]["title"] == "Title A"
    assert (
        "Error generating content"
        in result["deck_spec"]["slides"][0]["bullets"][0]
    )


@pytest.mark.asyncio
async def test_batch_generate_slides_exception():
    slides = [
        {
            "title": "Title A",
            "layout_name": "Title and Content",
            "bullets": ["A focus"],
        }
    ]
    research_summary = "Sum A"
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=Exception("API Call Failed")
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
    assert result["deck_spec"]["slides"][0]["title"] == "Title A"
    assert (
        "Error generating content: API Call Failed"
        in result["deck_spec"]["slides"][0]["bullets"][0]
    )
