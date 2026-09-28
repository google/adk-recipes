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

"""Background changer for images.

Provides functions to preprocess images and place a person in a new background
using a two-step approach with face correction.
"""

# Standard library imports
import logging
import os

from workflows.shared.debug_utils import save_debug_image

# Project imports
from workflows.shared.image_utils import (
    crop_face,
    replace_background,
    upscale_image_bytes,
)
from workflows.shared.llm_utils import get_generate_content_config
from workflows.shared.nano_banana import generate_nano
from workflows.shared.person_eval import submit_evaluation

logger = logging.getLogger(__name__)

# Grey background color for face preprocessing (same as VTO)
BG_CHANGER_BACKGROUND_COLOR = "#F0F0F0"


def preprocess_face_image(client, upscale_client, img_bytes):
    """Preprocess face image: crop face, upscale, then remove background.

    Args:
        client: Gemini client instance for background removal
        upscale_client: Gemini client instance for upscaling (must be global location)
        img_bytes (bytes): The input face image as bytes

    Returns:
        tuple: (reference_face, preprocessed_face)
            - reference_face: Cropped and upscaled face (for evaluation)
            - preprocessed_face: Face with background removed (for generation correction)
            Returns (None, None) if no face is detected.

    """
    try:
        save_debug_image(img_bytes, "01_original", prefix="bg_preproc_face")

        # Step 1: Crop face (with generous padding for hair)
        logger.info("[Background Changer Face Preprocessing] Cropping face...")
        face_cropped = crop_face(img_bytes, debug_prefix="bg_preproc_face")
        if face_cropped is None:
            logger.warning(
                "[Background Changer Face Preprocessing] No face detected"
            )
            return None, None

        save_debug_image(face_cropped, "02_cropped", prefix="bg_preproc_face")

        # Step 2: Upscale
        logger.info(
            "[Background Changer Face Preprocessing] Upscaling cropped face..."
        )
        reference_face = upscale_image_bytes(
            upscale_client, face_cropped, upscale_factor="x4"
        )

        save_debug_image(
            reference_face, "03_upscaled_reference", prefix="bg_preproc_face"
        )

        # Step 3: Remove background (for generation input)
        logger.info(
            "[Background Changer Face Preprocessing] Removing background..."
        )
        preprocessed_face = replace_background(
            client, reference_face, 0.01, BG_CHANGER_BACKGROUND_COLOR
        )

        save_debug_image(
            preprocessed_face, "04_bg_removed", prefix="bg_preproc_face"
        )

        logger.info(
            "[Background Changer Face Preprocessing] Preprocessing complete"
        )
        return reference_face, preprocessed_face
    except Exception as e:
        logger.error(f"[Background Changer Face Preprocessing] Error: {e}")
        return None, None


def preprocess_person_image(client, upscale_client, img_bytes):
    """Preprocess person image: remove background and upscale.

    Args:
        client: Gemini client instance for background removal
        upscale_client: Gemini client instance for upscaling (must be global location)
        img_bytes (bytes): The input person image as bytes

    Returns:
        bytes: The preprocessed person image as bytes

    """
    try:
        logger.info("[Background Changer] Removing person background...")
        img_no_bg = replace_background(
            client, img_bytes, contour_tolerance=0.01, background_color=None
        )

        logger.info("[Background Changer] Upscaling person image...")
        img_upscaled = upscale_image_bytes(
            upscale_client, img_no_bg, upscale_factor="x4"
        )

        logger.info("[Background Changer] Person preprocessing complete")
        return img_upscaled
    except Exception as e:
        logger.error(f"[Background Changer] Error during preprocessing: {e}")
        logger.warning("[Background Changer] Returning original image")
        return img_bytes


def generate_background_change(
    client,
    preprocessed_person_image,
    reference_face=None,
    background_description=None,
    background_image=None,
):
    """Generate an image with the person placed in a new background using 2-step approach.

    Args:
        client: Gemini client instance
        preprocessed_person_image: Preprocessed person image bytes (background removed, upscaled)
        reference_face: Preprocessed face image for correction step (optional, enables 2-step generation)
        background_description: Text description of the desired background (optional)
        background_image: Background image bytes (optional)

    Returns:
        bytes: Generated image with person in new background

    """
    logger.info("[Background Changer] Starting generation")

    system_prompt = """You are an expert photographer specializing in location photography.
Your task is to place the person in a new environment while keeping everything about them exactly the same.
The result should look like the person was photographed in that location."""

    # Build user message based on whether we have background image or description
    if background_image is not None:
        user_message = [
            "### PERSON (keep EXACT face, body, pose, clothes, everything): ",
            preprocessed_person_image,
            "### BACKGROUND/LOCATION: ",
            background_image,
            """### TASK: BACKGROUND CHANGE

Place this person in the provided background:

1. **KEEP EVERYTHING ABOUT THE PERSON** - same pose, body, face, hair, expression, clothes
2. **ONLY CHANGE THE BACKGROUND** to the provided location
3. **SAME CAMERA VIEW** - same angle, framing, and perspective as the original photo
4. **PHOTOREALISTIC RESULT** - should look like a real photo taken at this location

**CRITICAL:**
- The person must look IDENTICAL - same face, same expression, same hair
- Same body pose and proportions
- Same clothing
- Same camera angle and framing
- Only the background changes

Generate the image.""",
        ]
    else:
        # Use text description
        scene_description = (
            background_description or "a neutral grey studio background"
        )
        user_message = [
            "### PERSON (keep EXACT face, body, pose, clothes, everything): ",
            preprocessed_person_image,
            f"""### TASK: BACKGROUND CHANGE

Place this person in the following setting: {scene_description}

1. **KEEP EVERYTHING ABOUT THE PERSON** - same pose, body, face, hair, expression, clothes
2. **ONLY CHANGE THE BACKGROUND** to the described location
3. **SAME CAMERA VIEW** - same angle, framing, and perspective as the original photo
4. **PHOTOREALISTIC RESULT** - should look like a real photo taken at this location

**CRITICAL:**
- The person must look IDENTICAL - same face, same expression, same hair
- Same body pose and proportions
- Same clothing
- Same camera angle and framing
- Only the background changes

Generate the image.""",
        ]

    # Build config with system instruction and image settings (matching VTO approach)
    config = get_generate_content_config(
        temperature=0.1,
        response_modalities=["IMAGE"],
        system_instruction=system_prompt,
        image_config={
            "aspect_ratio": "3:4",
            "image_size": "1K",
            "output_mime_type": "image/png",
        },
    )

    # Step 1: Initial background change generation
    logger.info(
        "[Background Changer] Step 1: Initial background change generation..."
    )
    step1_result = generate_nano(
        client=client,
        text_images_pieces=user_message,
        model=os.getenv("MODEL_NAME_GENERATED_5"),
        config=config,
    )

    if step1_result is None:
        logger.error(
            "[Background Changer] Step 1 failed: Could not generate image"
        )
        return None

    logger.info("[Background Changer] Step 1 complete")

    # Step 2: Face correction (if reference face is provided)
    if reference_face is None:
        logger.info(
            "[Background Changer] No reference face provided, skipping face correction"
        )
        return {"step1_image": step1_result, "step2_image": None}

    logger.info(
        "[Background Changer] Step 2: Face improvement with correction..."
    )

    correction_message = [
        *user_message,
        step1_result,
        "No, the face is different. Use this face:",
        reference_face,
    ]

    step2_result = generate_nano(
        client=client,
        text_images_pieces=correction_message,
        model=os.getenv("MODEL_NAME_GENERATED_5"),
        config=config,
    )

    if step2_result is None:
        logger.warning(
            "[Background Changer] Step 2 failed, returning Step 1 only"
        )
        return {"step1_image": step1_result, "step2_image": None}

    logger.info("[Background Changer] Step 2 complete")

    return {"step1_image": step1_result, "step2_image": step2_result}


def generate_background_change_only(
    client,
    preprocessed_person_image,
    reference_face=None,
    background_description=None,
    background_image=None,
):
    """Generate a background change image without evaluation.

    Args:
        client: Gemini client instance
        preprocessed_person_image: Preprocessed person image bytes
        reference_face: Preprocessed face image for correction step (optional)
        background_description: Text description of background (optional)
        background_image: Background image bytes (optional)

    Returns:
        bytes: The generated image

    """
    return generate_background_change(
        client,
        preprocessed_person_image,
        reference_face,
        background_description,
        background_image,
    )


def evaluate_background_change_image(
    _client, generated_image_bytes, reference_face_bytes, model_name="ArcFace"
):
    """Evaluate a generated background change image against reference face.

    Uses shared process pool from person_eval to avoid DeepFace threading issues.

    Args:
        _client: The genai.Client instance (unused, kept for API compatibility)
        generated_image_bytes: Generated image bytes
        reference_face_bytes: Reference face image bytes for evaluation
        model_name: DeepFace model to use (default: "ArcFace")

    Returns:
        dict: {
            "similarity_percentage": float,
            "distance": float,
            "model": str,
            "face_detected": bool
        }

    """
    logger.info(
        "[Background Changer] Submitting evaluation to shared process pool"
    )
    try:
        # Submit to shared process pool and wait for result
        future = submit_evaluation(reference_face_bytes, generated_image_bytes)
        return future.result(timeout=120)  # 2 minute timeout
    except Exception as e:
        logger.error(f"[Background Changer] Evaluation failed: {e}")
        return {
            "similarity_percentage": 0.0,
            "distance": float("inf"),
            "model": model_name,
            "face_detected": False,
            "error": str(e),
        }
