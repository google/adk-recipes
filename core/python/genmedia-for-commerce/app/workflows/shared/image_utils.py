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

"""Shared utilities for core image processing.

Includes upscaling, background removal, and product extraction.
"""

# Standard library imports
import io
import logging
import os

# Third-party imports
import cv2
import numpy as np
from google.cloud import vision
from google.genai import types
from google.genai.errors import ClientError
from google.genai.types import SegmentImageSource
from PIL import Image as PImage
from PIL import ImageOps
from workflows.shared.debug_utils import save_debug_image

# Project imports
from workflows.shared.llm_utils import retry_with_exponential_backoff
from workflows.shared.utils import predict_parallel

# Defense-in-depth against decompression-bomb image inputs (e.g. the FITS
# vector in GHSA-whj4-6x5x-4v2j). 50M pixels (~7000x7000) comfortably covers
# any legitimate retail-media input. Set after imports to avoid an E402
# (module-level import not at top of file) violation on the imports below.
PImage.MAX_IMAGE_PIXELS = 50_000_000

logger = logging.getLogger(__name__)


def get_background_mask_rembg(img_bytes):
    """Generate a background mask using the rembg library."""
    from rembg import remove

    return remove(img_bytes, only_mask=True)


def _rgb_to_hsv_array(rgb_array: np.ndarray) -> np.ndarray:
    """Convert RGB array to HSV using vectorized numpy operations.

    Args:
        rgb_array: RGB image as numpy array (H, W, 3) with values 0-255

    Returns:
        HSV array (H, W, 3) with H: 0-360, S: 0-100, V: 0-100

    """
    rgb_normalized = rgb_array.astype(np.float32) / 255.0
    r, g, b = (
        rgb_normalized[:, :, 0],
        rgb_normalized[:, :, 1],
        rgb_normalized[:, :, 2],
    )

    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    delta = max_c - min_c

    h = np.zeros_like(max_c)
    mask = delta != 0

    red_max = mask & (max_c == r)
    h[red_max] = 60 * (((g[red_max] - b[red_max]) / delta[red_max]) % 6)

    green_max = mask & (max_c == g)
    h[green_max] = 60 * (((b[green_max] - r[green_max]) / delta[green_max]) + 2)

    blue_max = mask & (max_c == b)
    h[blue_max] = 60 * (((r[blue_max] - g[blue_max]) / delta[blue_max]) + 4)

    s = np.where(max_c != 0, (delta / max_c) * 100, 0)
    v = max_c * 100

    return np.stack([h, s, v], axis=-1)


def remove_background_hsv(
    img_bytes: bytes,
    saturation_max: float = 24.0,
    value_min: float = 90.0,
) -> np.ndarray:
    """Fast background removal for white/gray backgrounds using HSV color detection.

    This is a fast alternative to ML-based background removal (like rembg) that
    works well for product images on white/gray backgrounds. Uses pure numpy
    operations, making it ~50x faster than rembg.

    Args:
        img_bytes: Input image as PNG/JPEG bytes
        saturation_max: Maximum saturation % to consider as background (default: 25)
        value_min: Minimum brightness % to consider as background (default: 50)

    Returns:
        RGBA numpy array with transparent background

    """
    img = PImage.open(io.BytesIO(img_bytes)).convert("RGBA")
    data = np.array(img)
    rgb = data[:, :, :3]

    hsv = _rgb_to_hsv_array(rgb)
    s, v = hsv[:, :, 1], hsv[:, :, 2]

    bg_mask = (s < saturation_max) & (v > value_min)
    data[bg_mask, 3] = 0

    return data


def calculate_ssim_with_bg_removal(
    img1_bytes: bytes,
    img2_bytes: bytes,
    saturation_max: float = 24.0,
    value_min: float = 90.0,
) -> float:
    """Calculate SSIM similarity between two images after removing backgrounds.

    Removes white/gray backgrounds using fast HSV detection, then computes
    SSIM on the foreground region only. The final score is weighted by IoU
    (Intersection over Union) to penalize shape mismatches.

    Args:
        img1_bytes: First image as bytes
        img2_bytes: Second image as bytes
        saturation_max: Maximum saturation % for background detection (default: 25)
        value_min: Minimum brightness % for background detection (default: 50)

    Returns:
        float: Similarity score between 0 and 1 (SSIM * IoU)

    """
    from skimage.metrics import structural_similarity as ssim

    # Remove backgrounds
    arr1 = remove_background_hsv(img1_bytes, saturation_max, value_min)
    arr2 = remove_background_hsv(img2_bytes, saturation_max, value_min)

    # Resize if needed
    if arr1.shape != arr2.shape:
        img2_pil = PImage.fromarray(arr2)
        img2_pil = img2_pil.resize(
            (arr1.shape[1], arr1.shape[0]), PImage.Resampling.LANCZOS
        )
        arr2 = np.array(img2_pil)

    # Get foreground masks and compute IoU
    mask1 = arr1[:, :, 3] > 0
    mask2 = arr2[:, :, 3] > 0
    mask_union = mask1 | mask2
    mask_intersection = mask1 & mask2

    if not np.any(mask_intersection):
        return 0.0

    iou = np.sum(mask_intersection) / np.sum(mask_union)

    # Convert to grayscale and zero out background
    gray1 = cv2.cvtColor(arr1[:, :, :3], cv2.COLOR_RGB2GRAY)
    gray2 = cv2.cvtColor(arr2[:, :, :3], cv2.COLOR_RGB2GRAY)
    gray1[~mask1] = 0
    gray2[~mask2] = 0

    # Crop to bounding box of union (avoids large black regions affecting SSIM)
    rows = np.any(mask_union, axis=1)
    cols = np.any(mask_union, axis=0)

    if not np.any(rows) or not np.any(cols):
        return 0.0

    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]

    crop1 = gray1[rmin : rmax + 1, cmin : cmax + 1]
    crop2 = gray2[rmin : rmax + 1, cmin : cmax + 1]

    # SSIM requires minimum size (7x7 for default window)
    if crop1.shape[0] < 7 or crop1.shape[1] < 7:
        return float(iou)  # Fall back to IoU only for very small regions

    ssim_score, _ = ssim(crop1, crop2, full=True)

    # Weight by IoU to penalize shape mismatch
    return float(ssim_score * iou)


@retry_with_exponential_backoff(max_retries=5)
def get_background_mask_vertex(client, img_bytes, max_dimension=2048):
    """Generate a background mask using Vertex AI image segmentation.

    Automatically resizes large images before segmentation to stay within API limits.

    Args:
        client: The genai.Client instance
        img_bytes: The input image as bytes
        max_dimension: Maximum dimension to resize to before segmentation (default: 2048)

    Returns:
        bytes: The mask image as bytes

    """
    img = PImage.open(io.BytesIO(img_bytes))

    if max(img.size) > max_dimension:
        ratio = max_dimension / max(img.size)
        new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
        img = img.resize(new_size, PImage.Resampling.LANCZOS)
        logger.debug(f"Resized image to {new_size} for Vertex segmentation")

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        img_bytes = buffer.getvalue()

    try:
        seg_image = SegmentImageSource(image=types.Image(image_bytes=img_bytes))
        result = (
            client.models.segment_image(
                model="image-segmentation-001", source=seg_image
            )
            .generated_masks[0]
            .mask.image_bytes
        )
        return result
    except ClientError as e:
        error_msg = str(e).lower()
        if (
            "not found" in error_msg
            or "not enabled" in error_msg
            or "404" in error_msg
        ):
            logger.warning(
                "image-segmentation-001 not available, falling back to rembg"
            )
            return get_background_mask_rembg(img_bytes)
        raise


@retry_with_exponential_backoff(max_retries=5)
def upscale_image_bytes(client, image_bytes, upscale_factor="x4"):
    """Upscales an image using GCP's Imagen 4.0 Upscale API.

    Automatically adjusts upscale factor if the requested factor would exceed
    the maximum allowed output size. Falls back to x3, then x2, or returns
    original image if even x2 is too large.

    Args:
        client: The genai.Client instance (must be configured for us-central1)
        image_bytes: The input image as bytes
        upscale_factor: Desired upscale factor - 'x2', 'x3', or 'x4' (default: "x4")

    Returns:
        bytes: The upscaled image as bytes, or original if too large to upscale

    """
    if upscale_factor not in ["x2", "x3", "x4"]:
        raise ValueError("upscale_factor must be 'x2', 'x3', or 'x4'.")

    img = PImage.open(io.BytesIO(image_bytes))
    original_pixels = img.width * img.height
    logger.debug(
        f"Image size: {img.width}x{img.height} = {original_pixels:,} pixels"
    )

    MAX_RESULT_PIXELS = 17_000_000

    # Calculate the maximum safe upscale factor
    if original_pixels * 16 <= MAX_RESULT_PIXELS:
        safe_factor = "x4"
    elif original_pixels * 9 <= MAX_RESULT_PIXELS:
        safe_factor = "x3"
    elif original_pixels * 4 <= MAX_RESULT_PIXELS:
        safe_factor = "x2"
    else:
        logger.warning(
            f"Image too large to upscale ({original_pixels:,} pixels). Skipping."
        )
        return image_bytes

    # Downgrade requested factor if needed
    factor_map = {"x4": 4, "x3": 3, "x2": 2}
    if factor_map[upscale_factor] > factor_map[safe_factor]:
        logger.warning(
            f"Image too large for {upscale_factor}, using {safe_factor} instead"
        )
        upscale_factor = safe_factor

    logger.debug(f"Upscaling with factor: {upscale_factor}")

    # Debug: save before upscaling
    import hashlib

    img_hash = hashlib.md5(image_bytes).hexdigest()[:6]  # noqa: S324
    save_debug_image(image_bytes, f"upscale_input_{img_hash}", prefix="upscale")

    input_image = types.Image(image_bytes=image_bytes)

    response = client.models.upscale_image(
        model=os.getenv("MODEL_NAME_GENERATED_8"),
        image=input_image,
        upscale_factor=upscale_factor,
        config=types.UpscaleImageConfig(
            include_rai_reason=False,
            output_mime_type="image/png",
        ),
    )

    upscaled_bytes = response.generated_images[0].image.image_bytes

    # Debug: save after upscaling
    save_debug_image(
        upscaled_bytes, f"upscale_output_{img_hash}", prefix="upscale"
    )

    return upscaled_bytes


def replace_background(
    client,
    img_bytes,
    contour_tolerance=0.01,
    background_color="#FFFFFF",
    mask_margin_pixels=0,
    feather_radius=0,
):
    """Extract the product object from the image and place it on a new background.

    Handles EXIF orientation and ensures mask alignment with the original image.

    Args:
        client: The client for mask generation
        img_bytes: The input image as bytes
        contour_tolerance: A percentage to use as a margin around the bounding box (default: 0.01)
        background_color: The background color as hex string (e.g., "#FFFFFF" for white),
                         or None for transparent RGBA output (default: "#FFFFFF")
        mask_margin_pixels: Pixels to dilate the mask to add margin around product edges (default: 0)
        feather_radius: Radius for Gaussian blur on mask edges to soften contours (default: 0)

    Returns:
        bytes: The product image on the specified background as PNG bytes

    """
    # Open image and handle EXIF orientation
    original_image = PImage.open(io.BytesIO(img_bytes))
    try:
        original_image = ImageOps.exif_transpose(original_image)
    except Exception as e:
        logger.warning(f"Could not apply EXIF orientation: {e}")

    original_image = original_image.convert("RGBA")

    # Save oriented image to bytes for segmentation API
    oriented_buffer = io.BytesIO()
    original_image.save(oriented_buffer, format="PNG", compress_level=1)
    oriented_bytes = oriented_buffer.getvalue()

    try:
        mask_bytes = get_background_mask_vertex(client, oriented_bytes)
    except (ClientError, Exception):
        logger.warning(
            "Vertex segmentation failed, falling back to rembg library"
        )
        mask_bytes = get_background_mask_rembg(oriented_bytes)

    mask_pil = PImage.open(io.BytesIO(mask_bytes)).convert("L")

    # Resize mask to match original image if needed
    if mask_pil.size != original_image.size:
        mask_pil = mask_pil.resize(
            original_image.size, PImage.Resampling.LANCZOS
        )

    mask_np = np.array(mask_pil)

    # Dilate mask to add margin around product edges if requested
    if mask_margin_pixels > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (mask_margin_pixels * 2 + 1, mask_margin_pixels * 2 + 1),
        )
        mask_np = cv2.dilate(mask_np, kernel, iterations=1)
        mask_pil = PImage.fromarray(mask_np)

    height, width = mask_np.shape

    margin = int(contour_tolerance * width)

    contours, _ = cv2.findContours(
        mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    if contours:
        largest_contour = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(largest_contour)

        x1 = max(0, x - margin)
        y1 = max(0, y - margin)
        x2 = min(width, x + w + margin)
        y2 = min(height, y + h + margin)

        bbox = [x1, y1, x2, y2]
    else:
        bbox = [0, 0, width, height]

    cropped_image = original_image.crop(bbox)
    cropped_mask = mask_pil.crop(bbox)

    mask_bbox = cropped_mask.getbbox()

    # Convert hex color to RGB tuple if needed
    if background_color is not None and isinstance(background_color, str):
        bg_rgb = tuple(
            int(background_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)
        )
    else:
        bg_rgb = background_color

    if mask_bbox is None:
        # Return empty image with appropriate background
        if bg_rgb is None:
            empty_img = PImage.new("RGBA", (100, 100), (0, 0, 0, 0))
        else:
            empty_img = PImage.new("RGB", (100, 100), bg_rgb)
        empty_buffer = io.BytesIO()
        empty_img.save(empty_buffer, format="PNG")
        return empty_buffer.getvalue()

    final_cropped_image = cropped_image.crop(mask_bbox)
    final_cropped_mask = cropped_mask.crop(mask_bbox)

    # Feather mask edges with Gaussian blur for smoother compositing
    if feather_radius > 0:
        mask_arr = np.array(final_cropped_mask)
        ksize = feather_radius * 2 + 1
        mask_arr = cv2.GaussianBlur(mask_arr, (ksize, ksize), 0)
        final_cropped_mask = PImage.fromarray(mask_arr)

    if bg_rgb is None:
        # Transparent background - apply mask as alpha channel
        r, g, b, _ = final_cropped_image.split()
        result = PImage.merge("RGBA", (r, g, b, final_cropped_mask))
    else:
        # Solid color background
        background = PImage.new("RGB", final_cropped_image.size, bg_rgb)
        background.paste(final_cropped_image, (0, 0), final_cropped_mask)
        result = background

    output_buffer = io.BytesIO()
    result.save(output_buffer, format="PNG", compress_level=1, optimize=False)
    return output_buffer.getvalue()


def replace_background_with_mask_return(
    client,
    img_bytes,
    contour_tolerance=0.01,
    background_color="#FFFFFF",
    mask_margin_pixels=0,
):
    """Extract the product object and return both the result image and the mask.

    Same as replace_background but also returns the mask for reuse (e.g., after upscaling).

    Args:
        client: The client for mask generation
        img_bytes: The input image as bytes
        contour_tolerance: A percentage to use as a margin around the bounding box (default: 0.01)
        background_color: The background color as hex string (default: "#FFFFFF")
        mask_margin_pixels: Pixels to dilate the mask (default: 0)

    Returns:
        tuple: (image_bytes, mask_pil) - The product image as PNG bytes and the PIL mask

    """
    original_image = PImage.open(io.BytesIO(img_bytes))
    try:
        original_image = ImageOps.exif_transpose(original_image)
    except Exception as e:
        logger.warning(f"Could not apply EXIF orientation: {e}")

    original_image = original_image.convert("RGBA")

    oriented_buffer = io.BytesIO()
    original_image.save(oriented_buffer, format="PNG", compress_level=1)
    oriented_bytes = oriented_buffer.getvalue()

    try:
        mask_bytes = get_background_mask_vertex(client, oriented_bytes)
    except (ClientError, Exception):
        logger.warning(
            "Vertex segmentation failed, falling back to rembg library"
        )
        mask_bytes = get_background_mask_rembg(oriented_bytes)

    mask_pil = PImage.open(io.BytesIO(mask_bytes)).convert("L")

    if mask_pil.size != original_image.size:
        mask_pil = mask_pil.resize(
            original_image.size, PImage.Resampling.LANCZOS
        )

    mask_np = np.array(mask_pil)

    if mask_margin_pixels > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (mask_margin_pixels * 2 + 1, mask_margin_pixels * 2 + 1),
        )
        mask_np = cv2.dilate(mask_np, kernel, iterations=1)
        mask_pil = PImage.fromarray(mask_np)

    height, width = mask_np.shape
    margin = int(contour_tolerance * width)
    contours, _ = cv2.findContours(
        mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    if contours:
        largest_contour = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(largest_contour)
        bbox = [
            max(0, x - margin),
            max(0, y - margin),
            min(width, x + w + margin),
            min(height, y + h + margin),
        ]
    else:
        bbox = [0, 0, width, height]

    cropped_image = original_image.crop(bbox)
    cropped_mask = mask_pil.crop(bbox)
    mask_bbox = cropped_mask.getbbox()

    if background_color is not None and isinstance(background_color, str):
        bg_rgb = tuple(
            int(background_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)
        )
    else:
        bg_rgb = background_color

    if mask_bbox is None:
        if bg_rgb is None:
            empty_img = PImage.new("RGBA", (100, 100), (0, 0, 0, 0))
        else:
            empty_img = PImage.new("RGB", (100, 100), bg_rgb)
        empty_buffer = io.BytesIO()
        empty_img.save(empty_buffer, format="PNG")
        return empty_buffer.getvalue(), None

    final_cropped_image = cropped_image.crop(mask_bbox)
    final_cropped_mask = cropped_mask.crop(mask_bbox)

    background = PImage.new("RGB", final_cropped_image.size, bg_rgb)
    background.paste(final_cropped_image, (0, 0), final_cropped_mask)

    output_buffer = io.BytesIO()
    background.save(
        output_buffer, format="PNG", compress_level=1, optimize=False
    )

    return output_buffer.getvalue(), final_cropped_mask


def apply_scaled_mask_after_upscale(
    upscaled_bytes, original_mask, background_color="#FFFFFF"
):
    """Apply a scaled-up mask to an upscaled image.

    Scales the original mask to match the upscaled image size and applies it,
    removing any artifacts introduced during upscaling.

    Args:
        upscaled_bytes: The upscaled image as bytes
        original_mask: The original mask as a PIL Image
        background_color: The background color as hex string (default: "#FFFFFF")

    Returns:
        bytes: The masked image as PNG bytes

    """
    upscaled_img = PImage.open(io.BytesIO(upscaled_bytes)).convert("RGBA")
    scaled_mask = original_mask.resize(
        upscaled_img.size, PImage.Resampling.NEAREST
    )

    if background_color is not None and isinstance(background_color, str):
        bg_rgb = tuple(
            int(background_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)
        )
    else:
        bg_rgb = background_color or (255, 255, 255)

    background = PImage.new("RGB", upscaled_img.size, bg_rgb)
    background.paste(upscaled_img, (0, 0), scaled_mask)

    mask_bbox = scaled_mask.getbbox()
    if mask_bbox:
        background = background.crop(mask_bbox)

    output_buffer = io.BytesIO()
    background.save(output_buffer, format="PNG", compress_level=1)
    return output_buffer.getvalue()


def crop_face(img_bytes, padding_ratio=0.3, debug_prefix="crop"):
    """Detect a face in the image using Google Cloud Vision API and return the cropped face.

    Padding is calculated as a percentage of the detected face dimensions, ensuring
    consistent cropping regardless of image size or face position.

    Args:
        img_bytes: The input image as bytes.
        padding_ratio: Ratio of padding to add around the detected face (default: 0.3)
                       e.g., 0.3 means 30% of face width/height is added as padding
        debug_prefix: Prefix for debug image filenames

    Returns:
        bytes: The cropped face as PNG bytes, or None if no face is detected.

    """
    # Debug: save input image
    save_debug_image(img_bytes, "01_input", prefix=debug_prefix)

    # Open image and handle EXIF orientation
    img = PImage.open(io.BytesIO(img_bytes))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        logger.warning(f"Could not apply EXIF orientation: {e}")

    img = img.convert("RGB")
    width, height = img.size

    # Convert corrected image to bytes for Vision API
    corrected_buffer = io.BytesIO()
    img.save(corrected_buffer, format="PNG")
    corrected_bytes = corrected_buffer.getvalue()

    client = vision.ImageAnnotatorClient(
        client_options={"quota_project_id": os.getenv("PROJECT_ID", "")}
    )
    vision_image = vision.Image(content=corrected_bytes)
    response = client.face_detection(image=vision_image)

    if not response.face_annotations:
        logger.warning("No face detected")
        return None

    # Pick the largest detected face (by bounding box area) to avoid
    # false positives on tiny background faces.
    best_face = max(
        response.face_annotations,
        key=lambda f: (
            (f.bounding_poly.vertices[2].x - f.bounding_poly.vertices[0].x)
            * (f.bounding_poly.vertices[2].y - f.bounding_poly.vertices[0].y)
        ),
    )
    vertices = best_face.bounding_poly.vertices

    # Calculate face dimensions
    face_width = vertices[2].x - vertices[0].x
    face_height = vertices[2].y - vertices[0].y

    # Add padding as percentage of face dimensions
    padding_horizontal = face_width * padding_ratio
    padding_vertical = face_height * padding_ratio

    # Calculate crop box with padding
    x1 = max(0, vertices[0].x - padding_horizontal)
    y1 = max(0, vertices[0].y - padding_vertical)
    x2 = min(width, vertices[2].x + padding_horizontal)
    y2 = min(height, vertices[2].y + padding_vertical)

    logger.debug(
        f"[crop_face] Face: {face_width:.0f}x{face_height:.0f}, "
        f"Padding: {padding_horizontal:.0f}x{padding_vertical:.0f}, "
        f"Crop box: ({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f})"
    )

    cropped_image = img.crop((x1, y1, x2, y2))

    output_buffer = io.BytesIO()
    cropped_image.save(output_buffer, format="PNG", compress_level=1)
    result_bytes = output_buffer.getvalue()

    # Debug: save cropped face
    save_debug_image(result_bytes, "02_cropped", prefix=debug_prefix)

    return result_bytes


def create_canvas(
    product_image_bytes: bytes,
    canvas_width: int = 1920,
    canvas_height: int = 1080,
    margin_top: int = 150,
    margin_side: int = 0,
    bg_color: tuple = (255, 255, 255, 255),
    zoom_factor: float = 1.0,
    target_diagonal: float | None = None,
    target_height: float | None = None,
    add_shadow: bool = False,
) -> bytes:
    """Create a canvas with a product image centered with configurable margins.

    Supports three scaling modes:
    - target_diagonal: Scales image to match a specific diagonal size (best for rotation videos)
    - target_height: Scales image to match a specific height
    - Default: Fits image within available space respecting margins

    Args:
        product_image_bytes: The product image as bytes
        canvas_width: Width of the output canvas (default: 1920)
        canvas_height: Height of the output canvas (default: 1080)
        margin_top: Minimum top/bottom margin (default: 150)
        margin_side: Minimum left/right margin (default: 0)
        bg_color: RGB(A) background color tuple (default: white)
        zoom_factor: Additional zoom multiplier, 1.0 = fit to margins (default: 1.0)
        target_diagonal: If provided, scales image to match this diagonal size
        target_height: If provided, scales image to match this height
        add_shadow: If True, draw a soft ground shadow below the subject (default: False)

    Returns:
        bytes: The canvas image as PNG bytes

    """
    product_img = PImage.open(io.BytesIO(product_image_bytes))

    if target_diagonal is not None:
        current_diagonal = np.sqrt(product_img.width**2 + product_img.height**2)
        scale_factor = (target_diagonal / current_diagonal) * zoom_factor
    elif target_height is not None:
        scale_factor = (target_height / product_img.height) * zoom_factor
    else:
        available_width = canvas_width - (2 * margin_side)
        available_height = canvas_height - (2 * margin_top)
        scale_factor = (
            min(
                available_width / product_img.width,
                available_height / product_img.height,
            )
            * zoom_factor
        )

    new_width = int(product_img.width * scale_factor)
    new_height = int(product_img.height * scale_factor)

    resized_product = product_img.resize(
        (new_width, new_height), PImage.Resampling.LANCZOS
    )

    x_pos = (canvas_width - new_width) // 2
    y_pos = (canvas_height - new_height) // 2

    canvas = PImage.new("RGB", (canvas_width, canvas_height), bg_color)

    # Draw a soft ground shadow below the subject before pasting
    if add_shadow:
        shadow_cx = x_pos + new_width // 2
        shadow_cy = y_pos + new_height
        shadow_w = int(new_width * 0.45)
        shadow_h = max(3, int(new_height * 0.008))

        shadow_mask = np.zeros((canvas_height, canvas_width), dtype=np.float32)
        cv2.ellipse(
            shadow_mask,
            (shadow_cx, shadow_cy),
            (shadow_w // 2, shadow_h),
            0,
            0,
            360,
            1.0,
            -1,
        )
        shadow_mask = cv2.GaussianBlur(shadow_mask, (0, 0), sigmaX=25, sigmaY=8)

        if shadow_mask.max() > 0:
            shadow_mask = shadow_mask / shadow_mask.max()
        shadow_intensity = 30  # max darkening in pixel values

        canvas_np = np.array(canvas, dtype=np.int16)
        darkening = (shadow_mask * shadow_intensity).astype(np.int16)
        for c in range(3):
            canvas_np[:, :, c] = np.clip(canvas_np[:, :, c] - darkening, 0, 255)
        canvas = PImage.fromarray(canvas_np.astype(np.uint8))

    if resized_product.mode == "RGBA":
        canvas.paste(resized_product, (x_pos, y_pos), resized_product)
    else:
        canvas.paste(resized_product, (x_pos, y_pos))

    output_buffer = io.BytesIO()
    canvas.save(output_buffer, format="PNG", compress_level=1, optimize=False)
    return output_buffer.getvalue()


def create_canvas_with_height_scaling(
    images_bytes,
    canvas_height=1080,
    canvas_width=1920,
    margin_top=60,
    margin_side=300,
):
    """Create canvases for multiple images with consistent height scaling.

    Calculates the maximum safe height that fits all images within the margins,
    then creates canvases for each image using that shared target height.

    Args:
        images_bytes: List of product images as bytes
        canvas_height: Height of each output canvas (default: 1080)
        canvas_width: Width of each output canvas (default: 1920)
        margin_top: Top/bottom margin for each canvas (default: 60)
        margin_side: Left/right margin for each canvas (default: 300)

    Returns:
        list[bytes]: List of canvas images as PNG bytes

    """
    available_height = canvas_height - (2 * margin_top)
    available_width = canvas_width - (2 * margin_side)

    max_safe_heights = []
    for img_bytes in images_bytes:
        img = PImage.open(io.BytesIO(img_bytes))
        max_scale_factor = min(
            available_width / img.width,
            available_height / img.height,
        )
        max_safe_height = img.height * max_scale_factor
        max_safe_heights.append(max_safe_height)

    target_height = min(max_safe_heights)

    canvas_images = []
    for img_bytes in images_bytes:
        canvas_bytes = create_canvas(
            product_image_bytes=img_bytes,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            margin_top=margin_top,
            margin_side=margin_side,
            bg_color=(255, 255, 255),
            zoom_factor=1.0,
            target_height=target_height,
        )
        canvas_images.append(canvas_bytes)
    return canvas_images


def stack_images_horizontally(img1_bytes, img2_bytes, padding=0.03):
    """Stack two images horizontally side by side.

    Resizes both images to have the same height (using the smaller height)
    while maintaining aspect ratios, then combines them with optional padding.

    Args:
        img1_bytes: First image as bytes (placed on left)
        img2_bytes: Second image as bytes (placed on right)
        padding: Horizontal padding as percentage of wider image's width (default: 0.03)

    Returns:
        bytes: Combined image as PNG bytes

    """
    img1 = PImage.open(io.BytesIO(img1_bytes))
    img2 = PImage.open(io.BytesIO(img2_bytes))

    if img1.mode != "RGB":
        img1 = img1.convert("RGB")
    if img2.mode != "RGB":
        img2 = img2.convert("RGB")

    target_height = min(img1.height, img2.height)

    new_width1 = int(img1.width * (target_height / img1.height))
    new_width2 = int(img2.width * (target_height / img2.height))

    img1_resized = img1.resize(
        (new_width1, target_height), PImage.Resampling.LANCZOS
    )
    img2_resized = img2.resize(
        (new_width2, target_height), PImage.Resampling.LANCZOS
    )

    max_width = max(new_width1, new_width2)
    padding_px = int(max_width * padding)

    total_width = new_width1 + new_width2 + padding_px
    stacked = PImage.new("RGB", (total_width, target_height), (255, 255, 255))

    stacked.paste(img1_resized, (0, 0))
    stacked.paste(img2_resized, (new_width1 + padding_px, 0))

    output_buffer = io.BytesIO()
    stacked.save(output_buffer, format="PNG", compress_level=1, optimize=False)
    return output_buffer.getvalue()


def stack_and_canvas_images(
    images,
    classes=None,
    canvas_height=2160,
    canvas_width=3840,
    margin_top=216,
    margin_side=384,
):
    """Create 4K canvases for images, stacking the last two if there are 4 images.

    For 1-3 images: Creates individual canvases with consistent height scaling.
    For 4 images: Stacks images 3 and 4 horizontally, then creates 3 canvases.

    Args:
        images: List of product images as bytes (1-4 images)
        classes: Optional list of class labels corresponding to each image.
                 If provided, returns updated classes alongside canvas images.
        canvas_height: Height of the output canvas (default: 2160 for 4K)
        canvas_width: Width of the output canvas (default: 3840 for 4K)
        margin_top: Top margin in pixels (default: 216)
        margin_side: Side margin in pixels (default: 384)

    Returns:
        If classes is None: List of canvas images as bytes
        If classes provided: tuple (canvas_images, updated_classes)
            - updated_classes: Class labels (last two combined as tuple if 4 images)

    """
    n = len(images)

    if n <= 3:
        canvas_images = create_canvas_with_height_scaling(
            images,
            canvas_height=canvas_height,
            canvas_width=canvas_width,
            margin_top=margin_top,
            margin_side=margin_side,
        )
        if classes is None:
            return canvas_images
        return canvas_images, classes

    if n == 4:
        stacked_last = stack_images_horizontally(
            images[2], images[3], padding=0.20
        )
        canvas_images = create_canvas_with_height_scaling(
            [*images[0:2], stacked_last],
            canvas_height=canvas_height,
            canvas_width=canvas_width,
            margin_top=margin_top,
            margin_side=margin_side,
        )
        if classes is None:
            return canvas_images
        return canvas_images, [classes[0], classes[1], (classes[2], classes[3])]

    # Fallback for unexpected cases
    if classes is None:
        return images
    return images, classes


def extract_upscale_product(
    client, upscale_client, img_bytes, clean_after_upscale=True, skip_crop=False
):
    """Extract product from background and upscale it.

    Performs background removal, upscaling, and optionally reapplies the scaled
    original mask to remove artifacts introduced during upscaling.

    Args:
        client: Gemini client for background removal (segmentation)
        upscale_client: Client for image upscaling (Imagen)
        img_bytes: Input product image as bytes
        clean_after_upscale: If True, reapplies the scaled original mask after
                             upscaling to clean artifacts (default: True)
        skip_crop: If True, skips background removal and cropping, only upscaling (default: False)

    Returns:
        bytes: Processed product image with white background as PNG bytes.
               Returns original image if processing fails.

    """
    try:
        if skip_crop:
            # Just upscale directly without background removal or cropping
            return upscale_image_bytes(upscale_client, img_bytes)

        if clean_after_upscale:
            # Extract product and keep mask for reuse
            extracted_bytes, mask = replace_background_with_mask_return(
                client, img_bytes
            )
            # Upscale
            upscaled_bytes = upscale_image_bytes(
                upscale_client, extracted_bytes
            )
            # Apply scaled mask to remove upscaling artifacts
            if mask is not None:
                upscaled_bytes = apply_scaled_mask_after_upscale(
                    upscaled_bytes, mask
                )
        else:
            # Simple extraction without mask reuse
            extracted_bytes = replace_background(client, img_bytes)
            upscaled_bytes = upscale_image_bytes(
                upscale_client, extracted_bytes
            )

        return upscaled_bytes
    except Exception as e:
        logger.error(f"Failed upscaling and masking process: {e}")
        return img_bytes


def preprocess_images(
    images_bytes_list,
    client,
    upscale_client,
    num_workers=16,
    upscale_images=True,
    create_canva=True,
    skip_crop=False,
):
    """Preprocess images with optional upscaling and canvas creation.

    Used by both Interpolation and R2V modes for video generation.

    Args:
        images_bytes_list: List of image bytes to preprocess
        client: Gemini client for background removal
        upscale_client: Client for image upscaling
        num_workers: Number of parallel workers (default: 16)
        upscale_images: If True, extract and upscale products (default: True)
        create_canva: If True, create canvas with consistent sizing (default: True)
        skip_crop: If True, skips background removal and cropping (default: False)

    Returns:
        List of preprocessed image bytes

    """
    if upscale_images and skip_crop:
        images_bytes_list = predict_parallel(
            images_bytes_list,
            lambda img_bytes: upscale_image_bytes(upscale_client, img_bytes),
            max_workers=num_workers,
            show_progress_bar=False,
        )
    elif upscale_images:
        images_bytes_list = predict_parallel(
            images_bytes_list,
            lambda img_bytes: extract_upscale_product(
                client, upscale_client, img_bytes, skip_crop=False
            ),
            max_workers=num_workers,
            show_progress_bar=False,
        )
    else:
        images_bytes_list = predict_parallel(
            images_bytes_list,
            lambda img_bytes: replace_background(client, img_bytes),
            max_workers=num_workers,
            show_progress_bar=False,
        )
    if create_canva:
        images_bytes_list = create_canvas_with_height_scaling(
            images_bytes=images_bytes_list
        )
    return images_bytes_list
