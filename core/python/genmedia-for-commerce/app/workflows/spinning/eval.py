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

"""Shared evaluation utilities for spinning video generation.

Uses V3f rotation classifier (optical flow) for most products.
Uses mask-based shape classifier for glasses (symmetric objects).
Includes glitch detection using Gemini vision models.
"""

import json
import logging
import os
import tempfile

# Project imports
from google.genai import types
from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config
from workflows.spinning.glasses_rotation_classifier import (
    classify_glasses_rotation,
)
from workflows.spinning.rotation_classifier import classify_rotation

logger = logging.getLogger(__name__)


def classify_product_type(
    client,
    image_bytes: bytes,
    model: str = os.getenv("MODEL_NAME_GENERATED_2"),
) -> str:
    """Classify whether a product image shows glasses/eyewear or something else.

    Args:
        client: Gemini client instance
        image_bytes: A single product image as bytes
        model: Gemini model to use

    Returns:
        "glasses" or "other"

    """
    system_prompt = """You are a product classifier. Given a product image, determine if the product is glasses or eyewear (sunglasses, optical frames, goggles, etc.) or something else.

Return a JSON object:
{"product_type": "glasses"} or {"product_type": "other"}

Only return the JSON object, nothing else."""

    config = get_generate_content_config(
        temperature=0,
        thinking_budget=0,
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=[image_bytes, "What type of product is this?"],
            client=client,
            config=config,
            model=model,
        )
        result = json.loads(response_text)
        product_type = result.get("product_type", "other")
        if product_type not in ("glasses", "other"):
            product_type = "other"
        logger.info(f"[Product Type] Classified as: {product_type}")
        return product_type
    except Exception as e:
        logger.error(f"[Product Type] Classification error: {e}")
        return "other"


def check_spin_direction(
    video_bytes: bytes,
    product_type: str = "other",
    segmentation_client=None,
) -> str:
    """Check the rotation direction of a video.

    For glasses: uses mask-based shape classifier (handles symmetric objects).
    For other products: uses V3f optical flow classifier.

    Args:
        video_bytes: Video as bytes
        product_type: "glasses" or "other" (determines which classifier to use)
        segmentation_client: Vertex AI client for segmentation (required for glasses)

    Returns:
        str: "clockwise", "anticlockwise", or "invalid"

    """
    if product_type == "glasses" and segmentation_client is not None:
        result = classify_glasses_rotation(segmentation_client, video_bytes)
        logger.info(f"[Spin Direction] Glasses classifier result: {result}")
        if result in ("clockwise", "anticlockwise"):
            return result
        return "invalid"

    # Default: optical flow classifier
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
        tmp_file.write(video_bytes)
        tmp_path = tmp_file.name

    try:
        result = classify_rotation(tmp_path)
        if result in ("clockwise", "anticlockwise"):
            return result
        return "invalid"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def get_rotation_classification(video_bytes: bytes) -> str:
    """Get the full rotation classification for a video.

    Args:
        video_bytes: Video as bytes

    Returns:
        str: One of "clockwise", "anticlockwise", "invalid", "unknown", or "error"

    """
    # Write video bytes to a temporary file
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
        tmp_file.write(video_bytes)
        tmp_path = tmp_file.name

    try:
        return classify_rotation(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def glitch_detection(
    client, video_bytes: bytes, model: str = os.getenv("MODEL_NAME_GENERATED_4")
) -> dict:
    """Detect visual glitches in a 360-degree product spinning video.

    Uses Gemini vision model to analyze the video for common issues such as:
    - Direction changes (clockwise to anticlockwise or vice versa)
    - Unnatural transformations (text mirroring, impossible perspectives)
    - Product breaking the spin (zooming, tilting, moving to top/side)
    - Discontinuities in the rotation

    Minor imperfections on the product are acceptable.

    Args:
        client: Gemini client instance
        video_bytes: Video as bytes
        model: Gemini model to use (default: "gemini-3.6-flash")

    Returns:
        dict: {
            "explanation": str - Explanation of why video is valid or invalid
            "is_valid": bool - True if video is valid, False if glitches detected
        }

    """
    system_prompt = """You are an expert at analyzing product spinning videos for visual quality and glitches.

Your task is to analyze a 360-degree product rotation video and detect any visual glitches or unnatural artifacts.

**What to look for (INVALID):**
1. **Direction changes**: The product must clearly rotate in one direction for a significant portion of the video and then reverse to the opposite direction for another significant portion. A brief wobble, hesitation, or slight back-and-forth at the start or end is NOT a direction change — it must be a sustained reversal where the product visibly travels in both directions.
2. **Unnatural transformations**:
   - Text or logos mirroring or flipping unnaturally
   - A smartphone showing screens on both sides simultaneously when rotating
   - Product features appearing/disappearing incorrectly
3. **Breaking the spin**:
   - Product suddenly zooming in or out
   - Product tilting significantly up or down
   - Product moving to the top, bottom, or sides of frame
   - Product jumping or teleporting to different positions
4. **Discontinuities**: Sudden jumps or cuts in the rotation flow where the product visibly skips a large portion of the rotation (e.g., teleporting from rear view to front view)

**What is acceptable (VALID):**
- Minor imperfections on the product surface
- Slight variations in lighting
- Natural reflections and shadows
- Smooth, continuous 360-degree rotation in one direction
- Minor wobbles or slight hesitations in the rotation — these are normal and should NOT be flagged

Return your analysis as a valid JSON object with exactly two keys:
{
  "explanation": "A clear explanation of why the video is valid or what glitches were detected",
  "is_valid": true or false
}

Only return the JSON object, nothing else."""

    config = get_generate_content_config(
        temperature=0,
        thinking_level="HIGH",
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        text_part = ["Analyze this product spinning video for glitches:"]
        response_text = generate_gemini(
            text_images_pieces=([video_bytes, *text_part]),
            client=client,
            config=config,
            model=model,
            video_metadata=types.VideoMetadata(fps=2),
        )

        result = json.loads(response_text)

        # Ensure the result has the required keys
        if "explanation" not in result or "is_valid" not in result:
            raise ValueError("Response missing required keys")

        return result

    except Exception as e:
        # Return error case
        return {
            "explanation": f"Error during glitch detection: {e!s}",
            "is_valid": False,
        }
