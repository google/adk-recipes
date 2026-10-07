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

"""Tests for shared/image_utils.py - Canvas creation and image stacking."""

import io

import pytest
from PIL import Image
from workflows.shared.image_utils import (
    create_canvas,
    create_canvas_with_height_scaling,
    stack_and_canvas_images,
    stack_images_horizontally,
)


class TestCreateCanvas:
    """Tests for create_canvas function."""

    def test_default_size(self, sample_image_bytes):
        """Canvas should have default 1920x1080 dimensions."""
        result = create_canvas(sample_image_bytes)
        img = Image.open(io.BytesIO(result))
        assert img.size == (1920, 1080)

    def test_custom_dimensions(self, sample_image_bytes):
        """Canvas should respect custom width and height."""
        result = create_canvas(
            sample_image_bytes,
            canvas_width=800,
            canvas_height=600,
        )
        img = Image.open(io.BytesIO(result))
        assert img.size == (800, 600)

    def test_custom_margins(self, sample_image_bytes):
        """Canvas should respect custom margins (product fits within margins)."""
        result = create_canvas(
            sample_image_bytes,
            canvas_width=1000,
            canvas_height=1000,
            margin_top=200,
            margin_side=200,
        )
        img = Image.open(io.BytesIO(result))
        assert img.size == (1000, 1000)
        # Product should be centered and fit within 600x600 available space

    def test_zoom_factor(self, sample_image_bytes):
        """Zoom factor should scale the product image."""
        result_normal = create_canvas(
            sample_image_bytes, canvas_width=500, canvas_height=500
        )
        result_zoomed = create_canvas(
            sample_image_bytes,
            canvas_width=500,
            canvas_height=500,
            zoom_factor=0.5,
        )
        # Both should produce valid images with same canvas size
        img_normal = Image.open(io.BytesIO(result_normal))
        img_zoomed = Image.open(io.BytesIO(result_zoomed))
        assert img_normal.size == img_zoomed.size == (500, 500)

    def test_target_height_scaling(self, sample_image_bytes):
        """Target height should scale product to specific height."""
        result = create_canvas(
            sample_image_bytes,
            canvas_width=1000,
            canvas_height=1000,
            target_height=200,
        )
        img = Image.open(io.BytesIO(result))
        assert img.size == (1000, 1000)

    def test_target_diagonal_scaling(self, sample_image_bytes):
        """Target diagonal should scale product to specific diagonal size."""
        result = create_canvas(
            sample_image_bytes,
            canvas_width=1000,
            canvas_height=1000,
            target_diagonal=300,
        )
        img = Image.open(io.BytesIO(result))
        assert img.size == (1000, 1000)

    def test_rgba_input(self, sample_rgba_image_bytes):
        """RGBA images should be handled correctly."""
        result = create_canvas(sample_rgba_image_bytes)
        img = Image.open(io.BytesIO(result))
        assert img.size == (1920, 1080)
        assert img.mode == "RGB"  # Canvas is RGB

    def test_custom_background_color(self, sample_image_bytes):
        """Custom background color should be applied."""
        result = create_canvas(
            sample_image_bytes,
            canvas_width=500,
            canvas_height=500,
            margin_top=50,
            margin_side=50,
            bg_color=(0, 0, 0),  # Black background
        )
        img = Image.open(io.BytesIO(result))
        # Check corner pixel is black (background visible in margins)
        corner_pixel = img.getpixel((0, 0))
        assert corner_pixel == (0, 0, 0)


class TestStackImagesHorizontally:
    """Tests for stack_images_horizontally function."""

    def test_basic_stacking(self, sample_image_bytes):
        """Two images should be stacked side by side."""
        result = stack_images_horizontally(
            sample_image_bytes, sample_image_bytes
        )
        img = Image.open(io.BytesIO(result))
        # Both 100x100 images stacked with 3% padding of 100 = 3px
        # Total width = 100 + 3 + 100 = 203
        assert img.height == 100
        assert img.width > 100  # Combined width

    def test_different_heights(
        self, sample_image_bytes_200x100, sample_image_bytes_100x200
    ):
        """Images with different heights should be scaled to match."""
        result = stack_images_horizontally(
            sample_image_bytes_200x100,  # 200x100
            sample_image_bytes_100x200,  # 100x200
        )
        img = Image.open(io.BytesIO(result))
        # Should scale to smaller height (100)
        assert img.height == 100

    def test_custom_padding(self, sample_image_bytes):
        """Custom padding should affect the gap between images."""
        result_small = stack_images_horizontally(
            sample_image_bytes, sample_image_bytes, padding=0.01
        )
        result_large = stack_images_horizontally(
            sample_image_bytes, sample_image_bytes, padding=0.10
        )
        img_small = Image.open(io.BytesIO(result_small))
        img_large = Image.open(io.BytesIO(result_large))
        # Larger padding should produce wider image
        assert img_large.width > img_small.width

    def test_output_format(self, sample_image_bytes):
        """Output should be valid PNG bytes."""
        result = stack_images_horizontally(
            sample_image_bytes, sample_image_bytes
        )
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"
        assert img.mode == "RGB"


class TestStackAndCanvasImages:
    """Tests for stack_and_canvas_images function."""

    def test_single_image(self, sample_image_bytes):
        """Single image should produce one canvas."""
        result = stack_and_canvas_images([sample_image_bytes])
        assert len(result) == 1
        img = Image.open(io.BytesIO(result[0]))
        assert img.size == (3840, 2160)  # Default 4K

    def test_two_images(self, sample_image_bytes):
        """Two images should produce two canvases."""
        result = stack_and_canvas_images(
            [sample_image_bytes, sample_image_bytes]
        )
        assert len(result) == 2

    def test_three_images(self, sample_image_bytes):
        """Three images should produce three canvases."""
        result = stack_and_canvas_images(
            [sample_image_bytes, sample_image_bytes, sample_image_bytes]
        )
        assert len(result) == 3

    def test_four_images_stacks_last_two(self, sample_image_bytes):
        """Four images should stack last two and produce three canvases."""
        result = stack_and_canvas_images(
            [
                sample_image_bytes,
                sample_image_bytes,
                sample_image_bytes,
                sample_image_bytes,
            ]
        )
        # 4 images -> 3 canvases (last two stacked)
        assert len(result) == 3

    def test_custom_canvas_dimensions(self, sample_image_bytes):
        """Custom canvas dimensions should be respected."""
        result = stack_and_canvas_images(
            [sample_image_bytes],
            canvas_width=1920,
            canvas_height=1080,
        )
        img = Image.open(io.BytesIO(result[0]))
        assert img.size == (1920, 1080)

    def test_with_classes_single(self, sample_image_bytes):
        """Classes should be returned unchanged for 1-3 images."""
        images, classes = stack_and_canvas_images(
            [sample_image_bytes, sample_image_bytes],
            classes=["class_a", "class_b"],
        )
        assert len(images) == 2
        assert classes == ["class_a", "class_b"]

    def test_with_classes_four_images(self, sample_image_bytes):
        """Classes for 4 images should have last two combined as tuple."""
        images, classes = stack_and_canvas_images(
            [sample_image_bytes] * 4,
            classes=["a", "b", "c", "d"],
        )
        assert len(images) == 3
        assert classes == ["a", "b", ("c", "d")]


class TestCreateCanvasWithHeightScaling:
    """Tests for create_canvas_with_height_scaling function."""

    def test_single_image(self, sample_image_bytes):
        """Single image should produce one canvas."""
        result = create_canvas_with_height_scaling([sample_image_bytes])
        assert len(result) == 1

    def test_multiple_images(self, sample_image_bytes):
        """Multiple images should produce canvases with consistent height scaling."""
        result = create_canvas_with_height_scaling(
            [sample_image_bytes, sample_image_bytes, sample_image_bytes]
        )
        assert len(result) == 3

    def test_custom_dimensions(self, sample_image_bytes):
        """Custom canvas dimensions should be respected."""
        result = create_canvas_with_height_scaling(
            [sample_image_bytes],
            canvas_width=1920,
            canvas_height=1080,
        )
        img = Image.open(io.BytesIO(result[0]))
        assert img.size == (1920, 1080)


# =============================================================================
# MOCKED API TESTS
# Tests for functions that require external API calls (Vertex AI, Vision, etc.)
# =============================================================================

from unittest.mock import Mock, patch  # noqa: E402

import numpy as np  # noqa: E402
from workflows.shared.image_utils import (  # noqa: E402
    crop_face,
    extract_upscale_product,
    get_background_mask_vertex,
    replace_background,
    upscale_image_bytes,
)


class TestUpscaleImageBytes:
    """Tests for upscale_image_bytes function with mocked Imagen client."""

    @pytest.fixture
    def mock_imagen_client(self, monkeypatch):
        """Create a mock Imagen client."""
        monkeypatch.setenv("MODEL_NAME_GENERATED_8", "mock-upscale-model")
        client = Mock()

        def create_upscale_response(img_bytes):
            # Create a larger version of the image (simulating upscale)
            img = Image.open(io.BytesIO(img_bytes))
            upscaled = img.resize(
                (img.width * 2, img.height * 2), Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            upscaled.save(buffer, format="PNG")
            upscaled_bytes = buffer.getvalue()

            mock_image = Mock()
            mock_image.image.image_bytes = upscaled_bytes

            mock_response = Mock()
            mock_response.generated_images = [mock_image]
            return mock_response

        client.models.upscale_image.side_effect = lambda **kwargs: (
            create_upscale_response(kwargs["image"].image_bytes)
        )

        return client

    def test_upscale_disabled_returns_original(
        self, monkeypatch, mock_imagen_client, sample_image_bytes
    ):
        """Should return original image when MODEL_NAME_GENERATED_8 is None."""
        monkeypatch.setenv("MODEL_NAME_GENERATED_8", "None")
        result = upscale_image_bytes(mock_imagen_client, sample_image_bytes)
        assert result == sample_image_bytes
        mock_imagen_client.models.upscale_image.assert_not_called()

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_upscales_image(
        self, mock_save, mock_imagen_client, sample_image_bytes
    ):
        """Should upscale image using API."""
        result = upscale_image_bytes(mock_imagen_client, sample_image_bytes)

        assert isinstance(result, bytes)
        mock_imagen_client.models.upscale_image.assert_called_once()

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_uses_x4_by_default(
        self, mock_save, mock_imagen_client, sample_image_bytes
    ):
        """Should use x4 upscale factor by default."""
        upscale_image_bytes(mock_imagen_client, sample_image_bytes)

        call_kwargs = mock_imagen_client.models.upscale_image.call_args.kwargs
        assert call_kwargs["upscale_factor"] == "x4"

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_respects_upscale_factor(
        self, mock_save, mock_imagen_client, sample_image_bytes
    ):
        """Should use specified upscale factor."""
        upscale_image_bytes(
            mock_imagen_client, sample_image_bytes, upscale_factor="x2"
        )

        call_kwargs = mock_imagen_client.models.upscale_image.call_args.kwargs
        assert call_kwargs["upscale_factor"] == "x2"

    def test_rejects_invalid_factor(
        self, mock_imagen_client, sample_image_bytes
    ):
        """Should reject invalid upscale factors."""
        with pytest.raises(ValueError, match="must be 'x2', 'x3', or 'x4'"):
            upscale_image_bytes(
                mock_imagen_client, sample_image_bytes, upscale_factor="x5"
            )

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_downgrades_factor_for_large_images(
        self, mock_save, mock_imagen_client
    ):
        """Should downgrade factor for images that would exceed max pixels."""
        # Create a large image (2000x2000 = 4M pixels)
        # x4 would = 64M pixels, exceeds 17M limit
        large_img = Image.new("RGB", (2000, 2000), color=(100, 100, 100))
        buffer = io.BytesIO()
        large_img.save(buffer, format="PNG")
        large_bytes = buffer.getvalue()

        upscale_image_bytes(
            mock_imagen_client, large_bytes, upscale_factor="x4"
        )

        # Should have downgraded to x2 (4M * 4 = 16M, under limit)
        call_kwargs = mock_imagen_client.models.upscale_image.call_args.kwargs
        assert call_kwargs["upscale_factor"] == "x2"

    def test_returns_original_for_huge_images(self, mock_imagen_client):
        """Should return original image if too large to upscale even at x2."""
        # Create very large image (5000x5000 = 25M pixels)
        # Even x2 would = 100M pixels
        huge_img = Image.new("RGB", (5000, 5000), color=(100, 100, 100))
        buffer = io.BytesIO()
        huge_img.save(buffer, format="PNG")
        huge_bytes = buffer.getvalue()

        result = upscale_image_bytes(mock_imagen_client, huge_bytes)

        # Should return original, no API call
        assert result == huge_bytes
        mock_imagen_client.models.upscale_image.assert_not_called()


class TestReplaceBackground:
    """Tests for replace_background function with mocked segmentation."""

    @pytest.fixture
    def mock_segmentation_client(self, sample_image_bytes):
        """Create a mock client for Vertex segmentation."""
        client = Mock()

        # Create a mock mask (white center, black edges)
        mask_img = Image.new("L", (100, 100), color=0)
        # Draw white center area (product region)
        for y in range(20, 80):
            for x in range(20, 80):
                mask_img.putpixel((x, y), 255)

        mask_buffer = io.BytesIO()
        mask_img.save(mask_buffer, format="PNG")
        mask_bytes = mask_buffer.getvalue()

        mock_result = Mock()
        mock_result.generated_masks = [Mock()]
        mock_result.generated_masks[0].mask.image_bytes = mask_bytes

        client.models.segment_image.return_value = mock_result

        return client

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_replaces_background_white(
        self, mock_save, mock_segmentation_client, sample_image_bytes
    ):
        """Should extract product on white background."""
        result = replace_background(
            mock_segmentation_client, sample_image_bytes
        )

        assert isinstance(result, bytes)
        img = Image.open(io.BytesIO(result))
        assert img.mode == "RGB"

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_custom_background_color(
        self, mock_save, mock_segmentation_client, sample_image_bytes
    ):
        """Should use custom background color."""
        result = replace_background(
            mock_segmentation_client,
            sample_image_bytes,
            background_color="#FF0000",
        )

        Image.open(io.BytesIO(result))
        # Result should have red background visible
        assert isinstance(result, bytes)

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_transparent_background(
        self, mock_save, mock_segmentation_client, sample_image_bytes
    ):
        """Should create transparent background when None specified."""
        result = replace_background(
            mock_segmentation_client, sample_image_bytes, background_color=None
        )

        img = Image.open(io.BytesIO(result))
        assert img.mode == "RGBA"

    @patch("workflows.shared.image_utils.save_debug_image")
    def test_mask_margin_pixels(
        self, mock_save, mock_segmentation_client, sample_image_bytes
    ):
        """Should apply mask margin when specified."""
        result = replace_background(
            mock_segmentation_client, sample_image_bytes, mask_margin_pixels=10
        )

        assert isinstance(result, bytes)

    @patch("workflows.shared.image_utils.get_background_mask_rembg")
    @patch("workflows.shared.image_utils.get_background_mask_vertex")
    @patch("workflows.shared.image_utils.save_debug_image")
    def test_falls_back_to_rembg(
        self, mock_save, mock_vertex, mock_rembg, sample_image_bytes
    ):
        """Should fall back to rembg when Vertex fails."""
        # Vertex fails with any exception (not specific ClientError which needs complex args)
        mock_vertex.side_effect = Exception("API error")

        # rembg returns valid mask
        mask_img = Image.new("L", (100, 100), color=128)
        mask_buffer = io.BytesIO()
        mask_img.save(mask_buffer, format="PNG")
        mock_rembg.return_value = mask_buffer.getvalue()

        result = replace_background(Mock(), sample_image_bytes)

        mock_rembg.assert_called_once()
        assert isinstance(result, bytes)


class TestCropFace:
    """Tests for crop_face function with mocked Vision API."""

    @pytest.fixture
    def mock_vision_response_with_face(self):
        """Create mock Vision API response with detected face."""
        response = Mock()

        # Create mock face annotation
        face = Mock()
        vertex_0 = Mock()
        vertex_0.x = 40
        vertex_0.y = 30
        vertex_2 = Mock()
        vertex_2.x = 60
        vertex_2.y = 70

        face.bounding_poly.vertices = [vertex_0, Mock(), vertex_2, Mock()]
        response.face_annotations = [face]

        return response

    @pytest.fixture
    def mock_vision_response_no_face(self):
        """Create mock Vision API response with no faces."""
        response = Mock()
        response.face_annotations = []
        return response

    @patch("workflows.shared.image_utils.vision.ImageAnnotatorClient")
    @patch("workflows.shared.image_utils.save_debug_image")
    def test_crops_detected_face(
        self,
        mock_save,
        mock_client_class,
        mock_vision_response_with_face,
        sample_image_bytes,
    ):
        """Should crop detected face with padding."""
        mock_client = Mock()
        mock_client.face_detection.return_value = mock_vision_response_with_face
        mock_client_class.return_value = mock_client

        result = crop_face(sample_image_bytes)

        assert isinstance(result, bytes)
        mock_client.face_detection.assert_called_once()

    @patch("workflows.shared.image_utils.vision.ImageAnnotatorClient")
    @patch("workflows.shared.image_utils.save_debug_image")
    def test_returns_none_when_no_face(
        self,
        mock_save,
        mock_client_class,
        mock_vision_response_no_face,
        sample_image_bytes,
    ):
        """Should return None when no face detected."""
        mock_client = Mock()
        mock_client.face_detection.return_value = mock_vision_response_no_face
        mock_client_class.return_value = mock_client

        result = crop_face(sample_image_bytes)

        assert result is None

    @patch("workflows.shared.image_utils.vision.ImageAnnotatorClient")
    @patch("workflows.shared.image_utils.save_debug_image")
    def test_respects_padding_ratio(
        self,
        mock_save,
        mock_client_class,
        mock_vision_response_with_face,
        sample_image_bytes,
    ):
        """Should apply specified padding ratio."""
        mock_client = Mock()
        mock_client.face_detection.return_value = mock_vision_response_with_face
        mock_client_class.return_value = mock_client

        # Different padding ratios should produce different crop sizes
        result_small = crop_face(sample_image_bytes, padding_ratio=0.1)
        result_large = crop_face(sample_image_bytes, padding_ratio=0.5)

        # Both should be valid images
        assert isinstance(result_small, bytes)
        assert isinstance(result_large, bytes)

        # Larger padding should produce larger crop
        img_small = Image.open(io.BytesIO(result_small))
        img_large = Image.open(io.BytesIO(result_large))
        assert img_large.width >= img_small.width

    @patch("workflows.shared.image_utils.vision.ImageAnnotatorClient")
    @patch("workflows.shared.image_utils.save_debug_image")
    def test_saves_debug_images(
        self,
        mock_save,
        mock_client_class,
        mock_vision_response_with_face,
        sample_image_bytes,
    ):
        """Should save debug images during processing."""
        mock_client = Mock()
        mock_client.face_detection.return_value = mock_vision_response_with_face
        mock_client_class.return_value = mock_client

        crop_face(sample_image_bytes)

        # Should save input and cropped face
        assert mock_save.call_count >= 1


class TestExtractUpscaleProduct:
    """Tests for extract_upscale_product function with mocked clients."""

    @patch("workflows.shared.image_utils.apply_scaled_mask_after_upscale")
    @patch("workflows.shared.image_utils.upscale_image_bytes")
    @patch("workflows.shared.image_utils.replace_background_with_mask_return")
    def test_extracts_and_upscales(
        self,
        mock_replace_mask,
        mock_upscale,
        mock_apply_mask,
        sample_image_bytes,
    ):
        """Should extract product and upscale."""
        # Mock mask as numpy array
        mock_mask = np.ones((100, 100), dtype=np.uint8) * 255
        mock_replace_mask.return_value = (sample_image_bytes, mock_mask)
        mock_upscale.return_value = sample_image_bytes
        mock_apply_mask.return_value = sample_image_bytes

        mock_client = Mock()
        mock_upscale_client = Mock()

        result = extract_upscale_product(
            mock_client, mock_upscale_client, sample_image_bytes
        )

        # Check result is valid image bytes
        assert isinstance(result, bytes)
        assert len(result) > 0
        mock_replace_mask.assert_called_once()
        mock_upscale.assert_called_once()
        mock_apply_mask.assert_called_once()

    @patch("workflows.shared.image_utils.apply_scaled_mask_after_upscale")
    @patch("workflows.shared.image_utils.upscale_image_bytes")
    @patch("workflows.shared.image_utils.replace_background_with_mask_return")
    def test_cleans_after_upscale(
        self,
        mock_replace_mask,
        mock_upscale,
        mock_apply_mask,
        sample_image_bytes,
    ):
        """Should clean artifacts after upscaling when enabled."""
        mock_mask = np.ones((100, 100), dtype=np.uint8) * 255
        mock_replace_mask.return_value = (sample_image_bytes, mock_mask)
        mock_upscale.return_value = sample_image_bytes
        mock_apply_mask.return_value = sample_image_bytes

        mock_client = Mock()
        mock_upscale_client = Mock()

        result = extract_upscale_product(
            mock_client,
            mock_upscale_client,
            sample_image_bytes,
            clean_after_upscale=True,
        )

        # Should call replace_background_with_mask_return, upscale, and apply_mask
        mock_replace_mask.assert_called_once()
        mock_upscale.assert_called_once()
        mock_apply_mask.assert_called_once()
        assert isinstance(result, bytes)
        assert len(result) > 0

    @patch("workflows.shared.image_utils.upscale_image_bytes")
    @patch("workflows.shared.image_utils.replace_background")
    def test_skips_clean_when_disabled(
        self, mock_replace, mock_upscale, sample_image_bytes
    ):
        """Should skip cleaning when disabled."""
        mock_replace.return_value = sample_image_bytes
        mock_upscale.return_value = sample_image_bytes

        mock_client = Mock()
        mock_upscale_client = Mock()

        result = extract_upscale_product(
            mock_client,
            mock_upscale_client,
            sample_image_bytes,
            clean_after_upscale=False,
        )

        # replace_background should be called once (no mask reuse)
        mock_replace.assert_called_once()
        mock_upscale.assert_called_once()
        assert isinstance(result, bytes)
        assert len(result) > 0

    @patch("workflows.shared.image_utils.replace_background_with_mask_return")
    def test_returns_original_on_error(
        self, mock_replace_mask, sample_image_bytes
    ):
        """Should return original image on processing error."""
        mock_replace_mask.side_effect = Exception("Processing failed")

        mock_client = Mock()
        mock_upscale_client = Mock()

        result = extract_upscale_product(
            mock_client, mock_upscale_client, sample_image_bytes
        )

        assert result == sample_image_bytes

    @patch("workflows.shared.image_utils.apply_scaled_mask_after_upscale")
    @patch("workflows.shared.image_utils.upscale_image_bytes")
    @patch("workflows.shared.image_utils.replace_background_with_mask_return")
    def test_applies_mask_after_upscale(
        self,
        mock_replace_mask,
        mock_upscale,
        mock_apply_mask,
        sample_image_bytes,
    ):
        """Should apply scaled mask after upscaling to remove artifacts."""
        mock_mask = np.ones((100, 100), dtype=np.uint8) * 255
        mock_replace_mask.return_value = (sample_image_bytes, mock_mask)
        mock_upscale.return_value = sample_image_bytes
        mock_apply_mask.return_value = sample_image_bytes

        mock_client = Mock()
        mock_upscale_client = Mock()

        result = extract_upscale_product(
            mock_client,
            mock_upscale_client,
            sample_image_bytes,
            clean_after_upscale=True,
        )

        # Verify mask is passed to apply_scaled_mask_after_upscale
        mock_apply_mask.assert_called_once()
        call_args = mock_apply_mask.call_args[0]
        assert np.array_equal(call_args[1], mock_mask)
        assert isinstance(result, bytes)
        assert len(result) > 0


class TestPreprocessImages:
    """Tests for preprocess_images function with mocked dependencies."""

    @patch("workflows.shared.image_utils.create_canvas_with_height_scaling")
    @patch("workflows.shared.image_utils.predict_parallel")
    @patch("workflows.shared.image_utils.extract_upscale_product")
    def test_upscale_mode_calls_extract_upscale_product(
        self, mock_extract, mock_parallel, mock_canvas, sample_image_bytes
    ):
        """Should use extract_upscale_product when upscale_images=True."""
        from workflows.shared.image_utils import preprocess_images

        mock_parallel.return_value = [sample_image_bytes]
        mock_canvas.return_value = [sample_image_bytes]

        mock_client = Mock()
        mock_upscale_client = Mock()

        preprocess_images(
            [sample_image_bytes],
            mock_client,
            mock_upscale_client,
            upscale_images=True,
        )

        # Verify predict_parallel was called
        mock_parallel.assert_called_once()
        # The lambda should reference extract_upscale_product
        call_args = mock_parallel.call_args
        assert call_args[0][0] == [
            sample_image_bytes
        ]  # First arg is image list

    @patch("workflows.shared.image_utils.create_canvas_with_height_scaling")
    @patch("workflows.shared.image_utils.predict_parallel")
    @patch("workflows.shared.image_utils.replace_background")
    def test_no_upscale_mode_calls_replace_background(
        self, mock_replace, mock_parallel, mock_canvas, sample_image_bytes
    ):
        """Should use replace_background when upscale_images=False."""
        from workflows.shared.image_utils import preprocess_images

        mock_parallel.return_value = [sample_image_bytes]
        mock_canvas.return_value = [sample_image_bytes]

        mock_client = Mock()
        mock_upscale_client = Mock()

        preprocess_images(
            [sample_image_bytes],
            mock_client,
            mock_upscale_client,
            upscale_images=False,
        )

        # Verify predict_parallel was called
        mock_parallel.assert_called_once()

    @patch("workflows.shared.image_utils.create_canvas_with_height_scaling")
    @patch("workflows.shared.image_utils.predict_parallel")
    def test_creates_canvas_when_enabled(
        self, mock_parallel, mock_canvas, sample_image_bytes
    ):
        """Should call create_canvas_with_height_scaling when create_canva=True."""
        from workflows.shared.image_utils import preprocess_images

        mock_parallel.return_value = [sample_image_bytes]
        mock_canvas.return_value = [sample_image_bytes]

        mock_client = Mock()
        mock_upscale_client = Mock()

        preprocess_images(
            [sample_image_bytes],
            mock_client,
            mock_upscale_client,
            create_canva=True,
        )

        mock_canvas.assert_called_once_with(images_bytes=[sample_image_bytes])

    @patch("workflows.shared.image_utils.create_canvas_with_height_scaling")
    @patch("workflows.shared.image_utils.predict_parallel")
    def test_skips_canvas_when_disabled(
        self, mock_parallel, mock_canvas, sample_image_bytes
    ):
        """Should skip canvas creation when create_canva=False."""
        from workflows.shared.image_utils import preprocess_images

        mock_parallel.return_value = [sample_image_bytes]

        mock_client = Mock()
        mock_upscale_client = Mock()

        preprocess_images(
            [sample_image_bytes],
            mock_client,
            mock_upscale_client,
            create_canva=False,
        )

        mock_canvas.assert_not_called()

    @patch("workflows.shared.image_utils.create_canvas_with_height_scaling")
    @patch("workflows.shared.image_utils.predict_parallel")
    def test_returns_list_of_bytes(
        self, mock_parallel, mock_canvas, sample_image_bytes
    ):
        """Should return a list of bytes."""
        from workflows.shared.image_utils import preprocess_images

        mock_parallel.return_value = [sample_image_bytes, sample_image_bytes]
        mock_canvas.return_value = [sample_image_bytes, sample_image_bytes]

        mock_client = Mock()
        mock_upscale_client = Mock()

        result = preprocess_images(
            [sample_image_bytes, sample_image_bytes],
            mock_client,
            mock_upscale_client,
        )

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(item, bytes) for item in result)


class TestGetBackgroundMaskVertex:
    """Tests for get_background_mask_vertex function."""

    @pytest.fixture
    def mock_vertex_client(self):
        """Create mock Vertex AI client for segmentation."""
        client = Mock()

        mask_img = Image.new("L", (100, 100), color=128)
        buffer = io.BytesIO()
        mask_img.save(buffer, format="PNG")
        mask_bytes = buffer.getvalue()

        mock_result = Mock()
        mock_result.generated_masks = [Mock()]
        mock_result.generated_masks[0].mask.image_bytes = mask_bytes

        client.models.segment_image.return_value = mock_result

        return client

    def test_returns_mask_bytes(self, mock_vertex_client, sample_image_bytes):
        """Should return mask image bytes."""
        result = get_background_mask_vertex(
            mock_vertex_client, sample_image_bytes
        )

        assert isinstance(result, bytes)
        img = Image.open(io.BytesIO(result))
        assert img.mode == "L"  # Grayscale mask

    def test_resizes_large_images(self, mock_vertex_client):
        """Should resize images larger than max_dimension."""
        # Create large image
        large_img = Image.new("RGB", (4000, 4000), color=(100, 100, 100))
        buffer = io.BytesIO()
        large_img.save(buffer, format="PNG")
        large_bytes = buffer.getvalue()

        get_background_mask_vertex(
            mock_vertex_client, large_bytes, max_dimension=1024
        )

        # API should have been called
        mock_vertex_client.models.segment_image.assert_called_once()

    def test_preserves_small_images(
        self, mock_vertex_client, sample_image_bytes
    ):
        """Should not resize images smaller than max_dimension."""
        get_background_mask_vertex(
            mock_vertex_client, sample_image_bytes, max_dimension=2048
        )

        mock_vertex_client.models.segment_image.assert_called_once()
