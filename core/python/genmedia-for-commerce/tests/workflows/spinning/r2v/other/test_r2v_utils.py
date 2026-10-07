# Copyright 2025 Google LLC
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

"""Tests for r2v_utils.py - R2V prompt template and product description generation."""

import io
from unittest.mock import Mock

import pytest
from PIL import Image
from workflows.spinning.r2v.other.r2v_utils import (
    VEO_R2V_PROMPT_TEMPLATE,
    generate_product_description,
)


@pytest.fixture
def red_image_bytes():
    """Create a 100x100 red image."""
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


class TestVeoR2vPromptTemplate:
    """Tests for VEO_R2V_PROMPT_TEMPLATE constant."""

    def test_contains_subject_placeholder(self):
        """Template should contain a {{description}} placeholder."""
        assert "{{description}}" in VEO_R2V_PROMPT_TEMPLATE

    def test_contains_subject_section(self):
        """Template should contain a [Subject] section."""
        assert "[Subject]" in VEO_R2V_PROMPT_TEMPLATE

    def test_contains_action_section(self):
        """Template should contain an [Action] section."""
        assert "[Action]" in VEO_R2V_PROMPT_TEMPLATE

    def test_mentions_360_orbit(self):
        """Template should mention 360-degree camera orbit."""
        assert "360" in VEO_R2V_PROMPT_TEMPLATE

    def test_mentions_white_background(self):
        """Template should mention white studio background."""
        assert "white" in VEO_R2V_PROMPT_TEMPLATE.lower()


class TestGenerateProductDescription:
    """Tests for generate_product_description function."""

    def test_returns_stripped_text(self, red_image_bytes):
        """Should return the stripped text from Gemini response."""
        mock_client = Mock()
        mock_response = Mock()
        mock_response.text = "  A red ceramic mug standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)  "
        mock_client.models.generate_content.return_value = mock_response

        result = generate_product_description(
            mock_client, "gemini-3.8-flash", [red_image_bytes]
        )

        assert (
            result
            == "A red ceramic mug standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)"
        )
        assert not result.startswith(" ")
        assert not result.endswith(" ")

    def test_calls_gemini_with_correct_model(self, red_image_bytes):
        """Should pass the specified model name to generate_content."""
        mock_client = Mock()
        mock_response = Mock()
        mock_response.text = "A product"
        mock_client.models.generate_content.return_value = mock_response

        generate_product_description(
            mock_client, "gemini-3.1-pro-preview", [red_image_bytes]
        )

        call_kwargs = mock_client.models.generate_content.call_args
        assert call_kwargs.kwargs["model"] == "gemini-3.1-pro-preview"

    def test_passes_all_images(self, red_image_bytes):
        """Should include all provided images in the request parts."""
        mock_client = Mock()
        mock_response = Mock()
        mock_response.text = "A product"
        mock_client.models.generate_content.return_value = mock_response

        images = [red_image_bytes, red_image_bytes, red_image_bytes]
        generate_product_description(mock_client, "gemini-3.8-flash", images)

        call_kwargs = mock_client.models.generate_content.call_args
        contents = call_kwargs.kwargs["contents"]
        # Contents is a list with one Content object; its parts should include all images
        parts = contents[0].parts
        # 1 text part + 3 image parts = 4 parts
        assert len(parts) == 4
