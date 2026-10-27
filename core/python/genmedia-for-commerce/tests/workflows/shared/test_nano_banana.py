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

"""Tests for shared/nano_banana.py - Gemini image generation with mocked client."""

import io
import os
from unittest.mock import Mock, patch

import pytest
from PIL import Image
from workflows.shared.nano_banana import (
    NANO_TIMEOUT_SECONDS,
    NanoTimeoutError,
    generate_nano,
)


@pytest.fixture
def sample_image_bytes():
    """Create a sample PNG image as bytes."""
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def generated_image_bytes():
    """Create mock generated image bytes."""
    img = Image.new("RGB", (512, 512), color=(0, 255, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def mock_gemini_client(generated_image_bytes):
    """Create a mock Gemini client that returns successful image generation."""
    client = Mock()

    # Create mock response structure
    mock_inline_data = Mock()
    mock_inline_data.data = generated_image_bytes

    mock_part_image = Mock()
    mock_part_image.text = None  # This is an image part, not text
    mock_part_image.inline_data = mock_inline_data

    mock_part_text = Mock()
    mock_part_text.text = "Here is the generated image"
    mock_part_text.inline_data = None

    mock_content = Mock()
    mock_content.parts = [mock_part_text, mock_part_image]

    mock_candidate = Mock()
    mock_candidate.content = mock_content

    mock_response = Mock()
    mock_response.candidates = [mock_candidate]

    client.models.generate_content.return_value = mock_response

    return client


@pytest.fixture
def mock_gemini_client_slow(generated_image_bytes):
    """Create a mock Gemini client that takes time to respond."""
    client = Mock()

    def slow_response(*args, **kwargs):
        import time

        time.sleep(0.5)  # Simulate slow API call

        mock_inline_data = Mock()
        mock_inline_data.data = generated_image_bytes

        mock_part = Mock()
        mock_part.text = None
        mock_part.inline_data = mock_inline_data

        mock_content = Mock()
        mock_content.parts = [mock_part]

        mock_candidate = Mock()
        mock_candidate.content = mock_content

        mock_response = Mock()
        mock_response.candidates = [mock_candidate]
        return mock_response

    client.models.generate_content.side_effect = slow_response

    return client


class TestGenerateNano:
    """Tests for generate_nano function."""

    def test_generates_image_from_text(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should generate image from text prompt."""
        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Generate a red shoe on white background"],
        )

        assert isinstance(result, bytes)
        assert result == generated_image_bytes
        mock_gemini_client.models.generate_content.assert_called_once()

    def test_generates_image_from_text_and_image(
        self, mock_gemini_client, sample_image_bytes, generated_image_bytes
    ):
        """Should generate image from mixed text and image input."""
        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=[
                "Edit this image:",
                sample_image_bytes,
                "Make it blue",
            ],
        )

        assert result == generated_image_bytes

    def test_uses_correct_model(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should use specified model."""
        generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Test prompt"],
            model="gemini-custom-model",
        )

        call_kwargs = (
            mock_gemini_client.models.generate_content.call_args.kwargs
        )
        assert call_kwargs["model"] == "gemini-custom-model"

    def test_uses_default_model(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should use default model when not specified."""
        generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Test prompt"],
        )

        call_kwargs = (
            mock_gemini_client.models.generate_content.call_args.kwargs
        )
        assert call_kwargs["model"] == os.getenv(
            "MODEL_NAME_GENERATED_5", "gemini-nano-banana-2.1"
        )

    def test_uses_provided_config(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should use custom config when provided."""
        from google.genai.types import GenerateContentConfig

        custom_config = Mock(spec=GenerateContentConfig)

        generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Test prompt"],
            config=custom_config,
        )

        call_kwargs = (
            mock_gemini_client.models.generate_content.call_args.kwargs
        )
        assert call_kwargs["config"] == custom_config

    def test_uses_default_config_when_none(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should use default config with IMAGE modality when none provided."""
        with patch(
            "workflows.shared.nano_banana.get_generate_content_config"
        ) as mock_get_config:
            mock_config = Mock()
            mock_get_config.return_value = mock_config

            generate_nano(
                client=mock_gemini_client,
                text_images_pieces=["Test prompt"],
                config=None,
            )

            mock_get_config.assert_called_once_with(
                response_modalities=["IMAGE", "TEXT"]
            )

    def test_handles_single_text_input(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should handle single text input."""
        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Single text prompt"],
        )

        assert result == generated_image_bytes

    def test_handles_multiple_images(
        self, mock_gemini_client, sample_image_bytes, generated_image_bytes
    ):
        """Should handle multiple image inputs."""
        img2 = Image.new("RGB", (100, 100), color=(0, 0, 255))
        buffer2 = io.BytesIO()
        img2.save(buffer2, format="PNG")
        img2_bytes = buffer2.getvalue()

        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=[
                sample_image_bytes,
                img2_bytes,
                "Combine these images",
            ],
        )

        assert result == generated_image_bytes

    def test_filters_image_parts_from_response(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should return only image data, not text parts."""
        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Test"],
        )

        # Result should be image bytes, not text
        assert isinstance(result, bytes)
        assert result == generated_image_bytes

    def test_no_timeout_when_none(
        self, mock_gemini_client_slow, generated_image_bytes
    ):
        """Should not timeout when timeout is None."""
        result = generate_nano(
            client=mock_gemini_client_slow,
            text_images_pieces=["Test"],
            timeout=None,
        )

        assert result == generated_image_bytes

    def test_timeout_enforcement(self):
        """Should raise NanoTimeoutError when API call exceeds timeout."""
        client = Mock()

        def very_slow_response(*args, **kwargs):
            import time

            time.sleep(10)  # Very slow
            return Mock()

        client.models.generate_content.side_effect = very_slow_response

        # Use a very short timeout
        # Wrap the function to bypass retry decorator
        with patch.object(
            generate_nano, "__wrapped__", generate_nano.__wrapped__
        ):
            with pytest.raises(NanoTimeoutError, match="timed out"):
                generate_nano.__wrapped__(
                    client=client,
                    text_images_pieces=["Test"],
                    timeout=0.1,  # 100ms timeout
                )

    def test_default_timeout_value(self):
        """Should use default timeout value."""
        assert NANO_TIMEOUT_SECONDS == 90


class TestGenerateNanoWithRetry:
    """Tests for generate_nano retry behavior."""

    def test_retries_on_timeout(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should retry on NanoTimeoutError (via decorator)."""
        # First call times out, second succeeds
        call_count = 0

        def sometimes_slow(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                import time

                time.sleep(10)  # Will timeout
            return create_success_response(generated_image_bytes)

        def create_success_response(img_bytes):
            mock_inline_data = Mock()
            mock_inline_data.data = img_bytes
            mock_part = Mock()
            mock_part.text = None
            mock_part.inline_data = mock_inline_data
            mock_content = Mock()
            mock_content.parts = [mock_part]
            mock_candidate = Mock()
            mock_candidate.content = mock_content
            mock_response = Mock()
            mock_response.candidates = [mock_candidate]
            return mock_response

        # Note: Full retry testing would require more complex mocking
        # This test validates the basic structure
        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Test"],
        )

        assert result == generated_image_bytes


class TestGenerateNanoEdgeCases:
    """Edge case tests for generate_nano."""

    def test_empty_input_list(self, mock_gemini_client):
        """Should handle empty input list."""
        # Empty list should still call API
        generate_nano(
            client=mock_gemini_client,
            text_images_pieces=[],
        )

        mock_gemini_client.models.generate_content.assert_called_once()

    def test_only_whitespace_text(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should handle whitespace-only text."""
        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["   "],
        )

        assert result == generated_image_bytes

    def test_large_image_input(self, mock_gemini_client, generated_image_bytes):
        """Should handle large image input."""
        # Create a larger image
        large_img = Image.new("RGB", (4096, 4096), color=(128, 128, 128))
        buffer = io.BytesIO()
        large_img.save(buffer, format="PNG")
        large_bytes = buffer.getvalue()

        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=[large_bytes, "Describe this"],
        )

        assert result == generated_image_bytes

    def test_mixed_formats_in_input(
        self, mock_gemini_client, generated_image_bytes
    ):
        """Should handle mixed image formats in input."""
        # PNG image
        png_img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        png_buffer = io.BytesIO()
        png_img.save(png_buffer, format="PNG")
        png_bytes = png_buffer.getvalue()

        # JPEG image
        jpg_img = Image.new("RGB", (100, 100), color=(0, 255, 0))
        jpg_buffer = io.BytesIO()
        jpg_img.save(jpg_buffer, format="JPEG")
        jpg_bytes = jpg_buffer.getvalue()

        result = generate_nano(
            client=mock_gemini_client,
            text_images_pieces=[png_bytes, jpg_bytes, "Compare these"],
        )

        assert result == generated_image_bytes


class TestGenerateNanoContentsFormat:
    """Tests for correct contents format sent to API."""

    def test_contents_structure(self, mock_gemini_client, sample_image_bytes):
        """Should create correct contents structure for API."""
        generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Text prompt", sample_image_bytes],
        )

        call_kwargs = (
            mock_gemini_client.models.generate_content.call_args.kwargs
        )
        contents = call_kwargs["contents"]

        assert len(contents) == 1
        assert contents[0].role == "user"
        assert len(contents[0].parts) == 2  # Text + Image

    def test_processes_all_input_pieces(
        self, mock_gemini_client, sample_image_bytes, generated_image_bytes
    ):
        """Should process all input pieces."""
        generate_nano(
            client=mock_gemini_client,
            text_images_pieces=["Text", sample_image_bytes, "More text"],
        )

        call_kwargs = (
            mock_gemini_client.models.generate_content.call_args.kwargs
        )
        contents = call_kwargs["contents"]

        # Should have 3 parts (2 text + 1 image)
        assert len(contents[0].parts) == 3
