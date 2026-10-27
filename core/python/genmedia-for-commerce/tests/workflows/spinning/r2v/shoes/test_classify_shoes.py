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

"""Tests for classify_shoes.py - Shoe position classification with mocked Gemini."""

import io
import json
import os
from unittest.mock import Mock, patch

import pytest
from PIL import Image
from workflows.spinning.r2v.shoes.classify_shoes import (
    classify_shoe,
    classify_shoe_closure,
)


@pytest.fixture
def sample_shoe_image():
    """Create a sample shoe image as bytes."""
    img = Image.new("RGB", (200, 200), color=(200, 150, 100))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def mock_gemini_client():
    """Create a mock Gemini client."""
    return Mock()


class TestClassifyShoe:
    """Tests for classify_shoe function."""

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_classifies_front_position(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should classify shoe in front position."""
        mock_generate.return_value = "front"

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == "front"
        mock_generate.assert_called_once()

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_classifies_left_position(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should classify shoe in left position."""
        mock_generate.return_value = "left"

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == "left"

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_classifies_right_position(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should classify shoe in right position."""
        mock_generate.return_value = "right"

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == "right"

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_classifies_back_position(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should classify shoe in back position."""
        mock_generate.return_value = "back"

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == "back"

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_classifies_multiple(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should classify multiple shoes."""
        mock_generate.return_value = "multiple"

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == "multiple"

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_classifies_invalid(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should return invalid for non-shoe images."""
        mock_generate.return_value = "invalid"

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == "invalid"

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_uses_normal_mode_by_default(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should use normal classification prompt by default."""
        mock_generate.return_value = "front"

        classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
            mode="normal",
        )

        # Check config uses normal system prompt
        call_kwargs = mock_generate.call_args.kwargs
        config = call_kwargs["config"]
        # System instruction should be from normal mode
        assert config.system_instruction is not None

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_uses_validation_mode(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should use validation prompt when mode is 'validation'."""
        mock_generate.return_value = "front"

        classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
            mode="validation",
        )

        call_kwargs = mock_generate.call_args.kwargs
        config = call_kwargs["config"]
        assert config.system_instruction is not None

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_uses_temperature_zero(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should use temperature 0 for deterministic output."""
        mock_generate.return_value = "front"

        classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        call_kwargs = mock_generate.call_args.kwargs
        config = call_kwargs["config"]
        assert config.temperature == 0

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_passes_image_to_gemini(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should pass the image to generate_gemini."""
        mock_generate.return_value = "front"

        classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        call_kwargs = mock_generate.call_args.kwargs
        text_images = call_kwargs["text_images_pieces"]
        assert sample_shoe_image in text_images

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_passes_correct_model(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should pass the specified model."""
        mock_generate.return_value = "front"

        classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-custom-model",
        )

        call_kwargs = mock_generate.call_args.kwargs
        assert call_kwargs["model"] == "gemini-custom-model"


class TestClassifyShoePositions:
    """Tests for all shoe position classifications."""

    @pytest.mark.parametrize(
        "position",
        [
            "front",
            "front_right",
            "front_left",
            "top_front",
            "back",
            "back_right",
            "back_left",
            "right",
            "left",
            "sole",
            "multiple",
            "invalid",
        ],
    )
    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_all_valid_positions(
        self, mock_generate, position, sample_shoe_image, mock_gemini_client
    ):
        """Should handle all valid position classifications."""
        mock_generate.return_value = position

        result = classify_shoe(
            image=sample_shoe_image,
            client=mock_gemini_client,
            model="gemini-3.8-flash",
        )

        assert result == position


class TestClassifyShoeClosure:
    """Tests for classify_shoe_closure function (velcro detection)."""

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_detects_velcro(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should detect velcro in shoe."""
        mock_generate.return_value = json.dumps(
            {
                "has_velcro": True,
                "explanation": "Visible velcro straps on the shoe.",
            }
        )

        result = classify_shoe_closure(
            image_bytes_list=[sample_shoe_image], client=mock_gemini_client
        )

        assert result["has_velcro"]
        assert "velcro" in result["explanation"].lower()

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_no_velcro(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should detect no velcro in shoe."""
        mock_generate.return_value = json.dumps(
            {
                "has_velcro": False,
                "explanation": "No velcro or hook-and-loop fasteners visible.",
            }
        )

        result = classify_shoe_closure(
            image_bytes_list=[sample_shoe_image], client=mock_gemini_client
        )

        assert not result["has_velcro"]

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_handles_multiple_images(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should handle multiple input images."""
        mock_generate.return_value = json.dumps(
            {
                "has_velcro": True,
                "explanation": "Velcro visible in multiple angles.",
            }
        )

        images = [sample_shoe_image, sample_shoe_image, sample_shoe_image]

        result = classify_shoe_closure(
            image_bytes_list=images, client=mock_gemini_client
        )

        assert "has_velcro" in result
        # Check all images were passed
        call_kwargs = mock_generate.call_args.kwargs
        text_images = call_kwargs["text_images_pieces"]
        assert len([x for x in text_images if isinstance(x, bytes)]) == 3

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_uses_default_model(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should use default flash-lite model."""
        mock_generate.return_value = json.dumps(
            {"has_velcro": False, "explanation": "No velcro."}
        )

        classify_shoe_closure(
            image_bytes_list=[sample_shoe_image], client=mock_gemini_client
        )

        call_kwargs = mock_generate.call_args.kwargs
        assert call_kwargs["model"] == os.getenv(
            "MODEL_NAME_GENERATED_2", "gemini-3.5-flash-lite"
        )

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_uses_custom_model(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should use custom model when specified."""
        mock_generate.return_value = json.dumps(
            {"has_velcro": False, "explanation": "No velcro."}
        )

        classify_shoe_closure(
            image_bytes_list=[sample_shoe_image],
            client=mock_gemini_client,
            model="gemini-custom-model",
        )

        call_kwargs = mock_generate.call_args.kwargs
        assert call_kwargs["model"] == "gemini-custom-model"

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_returns_json_response(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should return parsed JSON response."""
        mock_generate.return_value = json.dumps(
            {"has_velcro": True, "explanation": "Found velcro."}
        )

        result = classify_shoe_closure(
            image_bytes_list=[sample_shoe_image], client=mock_gemini_client
        )

        assert isinstance(result, dict)
        assert "has_velcro" in result
        assert "explanation" in result

    @patch("workflows.spinning.r2v.shoes.classify_shoes.generate_gemini")
    def test_uses_json_response_format(
        self, mock_generate, sample_shoe_image, mock_gemini_client
    ):
        """Should request JSON response format."""
        mock_generate.return_value = json.dumps(
            {"has_velcro": False, "explanation": "No velcro."}
        )

        classify_shoe_closure(
            image_bytes_list=[sample_shoe_image], client=mock_gemini_client
        )

        call_kwargs = mock_generate.call_args.kwargs
        config = call_kwargs["config"]
        assert config.response_mime_type == "application/json"
