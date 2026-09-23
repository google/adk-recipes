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

from unittest.mock import MagicMock, patch

import pytest

from presentation_agent.tools.visual_generator import generate_visual


class MockResponse:
    def __init__(self, data=None):
        if data:
            self.candidates = [MagicMock()]
            self.candidates[0].content.parts = [MagicMock()]
            self.candidates[0].content.parts[0].inline_data.data = data
        else:
            self.candidates = []


@pytest.mark.asyncio
async def test_generate_visual_success_gcs():
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(
        return_value=MockResponse(b"image_data")
    )
    mock_storage_client = MagicMock()

    with (
        patch(
            "presentation_agent.tools.visual_generator.initialize_genai_client",
            return_value=mock_client,
        ),
        patch(
            "presentation_agent.tools.visual_generator.GCS_BUCKET_NAME",
            "my-bucket",
        ),
        patch(
            "presentation_agent.tools.visual_generator.get_gcs_client",
            return_value=mock_storage_client,
        ),
    ):
        result = await generate_visual("chart: A test chart")

        assert result.startswith("gs://my-bucket/")
        mock_client.models.generate_content.assert_called_once()
        mock_storage_client.bucket().blob().upload_from_string.assert_called_once()


@pytest.mark.asyncio
async def test_generate_visual_success_local():
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(
        return_value=MockResponse(b"image_data")
    )

    with (
        patch(
            "presentation_agent.tools.visual_generator.initialize_genai_client",
            return_value=mock_client,
        ),
        patch(
            "presentation_agent.tools.visual_generator.GCS_BUCKET_NAME",
            None,
        ),
    ):
        result = await generate_visual("image: A test image")

        assert not result.startswith("Error:")
        assert not result.startswith("gs://")
        assert result.endswith(".png")  # Temp file path
        mock_client.models.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_generate_visual_no_candidates():
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(return_value=MockResponse())

    with (
        patch(
            "presentation_agent.tools.visual_generator.initialize_genai_client",
            return_value=mock_client,
        ),
        patch(
            "presentation_agent.tools.visual_generator.GCS_BUCKET_NAME",
            None,
        ),
    ):
        result = await generate_visual("test prompt")

        assert result.startswith("Error: Visual generation failed.")


@pytest.mark.asyncio
async def test_generate_visual_gcs_no_client():
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(
        return_value=MockResponse(b"image_data")
    )

    with (
        patch(
            "presentation_agent.tools.visual_generator.initialize_genai_client",
            return_value=mock_client,
        ),
        patch(
            "presentation_agent.tools.visual_generator.GCS_BUCKET_NAME",
            "my-bucket",
        ),
        patch(
            "presentation_agent.tools.visual_generator.get_gcs_client",
            return_value=None,
        ),
    ):
        result = await generate_visual("chart: A test chart")

        assert result.startswith(
            "Error: Visual generation failed."
        )  # It fails if storage_client is None


@pytest.mark.asyncio
async def test_generate_visual_exception():
    mock_client = MagicMock()
    mock_client.models.generate_content = MagicMock(
        side_effect=Exception("API Error")
    )

    with (
        patch(
            "presentation_agent.tools.visual_generator.initialize_genai_client",
            return_value=mock_client,
        ),
        patch(
            "presentation_agent.tools.visual_generator.GCS_BUCKET_NAME",
            None,
        ),
    ):
        result = await generate_visual("test prompt")

        assert result.startswith("Error: Visual generation failed.")


@pytest.mark.asyncio
async def test_generate_visual_client_none():
    with patch(
        "presentation_agent.tools.visual_generator.initialize_genai_client",
        return_value=None,
    ):
        result = await generate_visual("test prompt")

        assert result == "Error: Visual generation failed."


@pytest.mark.asyncio
async def test_generate_visual_empty_prompt():
    result = await generate_visual("")
    assert result == "Error: Prompt cannot be empty."
