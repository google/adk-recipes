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

"""Glasses VTO image generation.

Provides functions to preprocess images, describe glasses, and generate
virtual try-on images for glasses using a two-step approach.
"""

import logging
import os

from workflows.shared.gemini import generate_gemini
from workflows.shared.image_utils import (
    crop_face,
    replace_background,
    upscale_image_bytes,
)
from workflows.shared.llm_utils import get_generate_content_config
from workflows.shared.nano_banana import generate_nano
from workflows.shared.person_eval import submit_evaluation

logger = logging.getLogger(__name__)

# Grey background color for upscaling compatibility
VTO_BACKGROUND_COLOR = "#F0F0F0"

# ---------------------------------------------------------------------------
# System + user prompts (moved from frontend)
# ---------------------------------------------------------------------------

GLASSES_SYSTEM_PROMPT = """You are a photorealistic portrait photographer inside a luxury eyewear boutique store.
A customer is trying on glasses in the store. You take a candid, natural photo of them wearing the glasses — shot on a professional camera with shallow depth of field.
The background always shows the real boutique interior: wooden shelves with glasses on display, warm ambient lighting, other customers or staff slightly blurred in the background.
Your photos are indistinguishable from real candid snapshots taken in an actual eyewear shop."""

GLASSES_USER_PROMPT = """Photograph this customer trying on these glasses inside the eyewear boutique.

The setting is INSIDE THE STORE — shelves of glasses on display behind them, warm interior lighting, natural boutique atmosphere. NOT a white studio, NOT a plain background. This must look like a candid snapshot taken inside a real shop.

RULES:
- This is a REAL photo of a REAL person who has PHYSICALLY put on these glasses. Not a composite, not an overlay, not photoshop.
- The glasses are CORRECTLY SIZED for this face — frame width roughly equals face width at the cheekbones. Never oversized.
- **LENS OPTICS** (critical for realism): Real lenses have TWO visual layers that combine:
  1. **Outer surface**: The lens tint color + environment reflections. The store environment (shelves, lights, shapes) should be faintly REFLECTED on the lens surface. Match the reference glasses' tint color and reflectivity exactly.
  2. **Inner view (through the lens)**: The face/skin behind the lens is visible but COLOR-FILTERED by the tint. The person's skin tone BLENDS with the lens color — e.g. green tint over warm skin produces a dark olive/brownish-green, not pure green. Blue tint over skin produces a muted steel-blue, not solid blue. The eyes, eyebrows, and skin are darkened and hue-shifted by the lens, never erased.
  - The final lens appearance is the combination of both layers: filtered skin showing through + surface reflections on top. This creates DEPTH — not a flat single-color fill.
  - Match the reference glasses' opacity: if the lenses are dark sunglasses where eyes are barely visible → keep them dark. If clear/prescription → keep them clear.
  - NEVER render lenses as a single flat opaque color. Even very dark lenses show a faint trace of the face underneath at certain angles.
- The glasses rest naturally on the nose bridge and hook behind the ears. Contact shadows where frame touches skin.
- Same lighting on glasses and face. Same perspective. Same depth of field.
- **FACE CONSISTENCY** (critical): The generated face must be an EXACT match to the reference photo:
  - Same facial expression — if the person is smiling, they must be smiling with the same intensity. If neutral, keep neutral. Do NOT change the expression.
  - Same mouth position — open/closed, teeth showing or not, lip shape must all match exactly.
  - Same eye direction and openness — gaze direction, squint level, eyebrow position must be identical.
  - Same skin texture, wrinkles, and facial lines — preserve every detail of the person's face.
  - Same head angle and tilt — do not rotate or reposition the head.
  - The ONLY difference should be the glasses on the face. Everything else about the face must be pixel-level consistent with the reference.
- Do NOT paste or overlay the glasses image onto the face — REGENERATE the entire portrait with the person wearing them naturally."""


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------


def preprocess_face_image(client, img_bytes):
    """Preprocess face image: crop face, upscale, then remove background.

    Returns:
        tuple: (reference_face, preprocessed_face)
            - reference_face: Cropped and upscaled face (for evaluation)
            - preprocessed_face: Face with background removed (for generation)
            Returns (None, None) if no face is detected.

    """
    try:
        logger.info("[Glasses VTO Face Preprocessing] Cropping face...")
        face_cropped = crop_face(img_bytes)
        if face_cropped is None:
            logger.warning("[Glasses VTO Face Preprocessing] No face detected")
            return None, None

        logger.info(
            "[Glasses VTO Face Preprocessing] Upscaling cropped face..."
        )
        reference_face = upscale_image_bytes(
            client, face_cropped, upscale_factor="x4"
        )

        logger.info("[Glasses VTO Face Preprocessing] Removing background...")
        preprocessed_face = replace_background(
            client,
            reference_face,
            0.01,
            VTO_BACKGROUND_COLOR,
            mask_margin_pixels=2,
            feather_radius=3,
        )
        logger.info("[Glasses VTO Face Preprocessing] Preprocessing complete")
        return reference_face, preprocessed_face
    except Exception as e:
        logger.error(f"[Glasses VTO Face Preprocessing] Error: {e}")
        return None, None


def preprocess_glasses_image(client, img_bytes):
    """Preprocess glasses image: remove background to isolate the product.

    Returns:
        bytes: The preprocessed glasses image with background removed.

    """
    try:
        logger.info(
            "[Glasses VTO Product Preprocessing] Removing background..."
        )
        img_no_bg = replace_background(client, img_bytes, 0.01, None)

        logger.info(
            "[Glasses VTO Product Preprocessing] Preprocessing complete"
        )
        return img_no_bg
    except Exception as e:
        logger.error(f"[Glasses VTO Product Preprocessing] Error: {e}")
        logger.warning(
            "[Glasses VTO Product Preprocessing] Returning original image"
        )
        return img_bytes


# ---------------------------------------------------------------------------
# Product description (auto-generated via Gemini vision)
# ---------------------------------------------------------------------------


def describe_glasses(client, glasses_image_bytes: bytes) -> str | None:
    """Use Gemini vision to generate a short product description of the glasses.

    Captures brand name, frame shape, frame color, lens type, and any visible
    text/logos so that the generation model can reproduce them.

    Returns:
        A short description string, or None on failure.

    """
    prompt = (
        "Look carefully at this eyewear product image. Your job is to describe it for an image generation model that needs to reproduce it exactly.\n\n"
        "STEP 1 — BRAND NAME (critical): Zoom into the temples (arms), nose bridge, and lens corners. "
        "Read any text, letters, or logo engravings. Common brands: Ray-Ban, Oakley, Gucci, Prada, Persol, "
        "Tom Ford, Versace, Dolce & Gabbana, Dior, Chanel, Carrera, Maui Jim. "
        "Write the brand name EXACTLY as it appears on the frame. If no text is readable, write 'unbranded'.\n\n"
        "STEP 2 — Describe in one sentence: brand name + frame shape (aviator, round, rectangular, cat-eye, wayfarer, etc.) "
        "+ frame color/material (gold metal, black acetate, tortoiseshell, etc.) "
        "+ lens type (dark tinted, clear, mirrored, gradient, polarized, etc.).\n\n"
        "Example: 'Ray-Ban Aviator with gold metal frame and dark green G-15 tinted lenses'\n\n"
        "Only return the final one-sentence description, nothing else."
    )

    config = get_generate_content_config(
        temperature=0,
        max_output_tokens=150,
        thinking_budget=0,
    )

    try:
        description = generate_gemini(
            text_images_pieces=[glasses_image_bytes, prompt],
            client=client,
            config=config,
            model=os.getenv("MODEL_NAME_GENERATED_4"),
        )
        logger.info(f"[Glasses VTO] Auto-described glasses: {description}")
        return description
    except Exception as e:
        logger.warning(f"[Glasses VTO] Failed to describe glasses: {e}")
        return None


# ---------------------------------------------------------------------------
# Generation (2-step with face correction)
# ---------------------------------------------------------------------------


def create_frame_nano(
    client,
    glasses_images,
    preprocessed_face,
    reference_face,
    glasses_description=None,
):
    """Generate a glasses VTO image using 2-step approach with face correction.

    Args:
        client: Gemini client instance
        glasses_images: List of glasses image bytes
        preprocessed_face: Preprocessed face (bg removed) for generation
        reference_face: Original reference face for correction step
        glasses_description: Optional text description of the glasses

    Returns:
        dict: {
            "step1_image": bytes,
            "step2_image": bytes | None,
        }

    """
    logger.info("[Glasses VTO] Starting generation (2-step)")

    glasses_ref_text = "### THE EXACT GLASSES THEY ARE TRYING ON — you MUST match these precisely: same frame shape, same frame color, same lens tint/darkness, same lens reflectivity, same proportional size. Study every detail of this reference: "
    if glasses_description:
        glasses_ref_text += f"\nProduct description: {glasses_description}. Reproduce any brand logos or text visible on the frames exactly as they appear."

    user_message_step1 = [
        "### THE CUSTOMER (preserve this person's exact identity): ",
        preprocessed_face,
        glasses_ref_text,
        *glasses_images,
        GLASSES_USER_PROMPT,
    ]

    config = get_generate_content_config(
        temperature=0.4,
        response_modalities=["IMAGE"],
        system_instruction=GLASSES_SYSTEM_PROMPT,
        image_config={
            "aspect_ratio": "3:4",
            "output_mime_type": "image/png",
        },
    )

    logger.info("[Glasses VTO] Step 1: Generate VTO...")
    step1_result = generate_nano(client, user_message_step1, config=config)

    if step1_result is None:
        logger.error("[Glasses VTO] Step 1 failed")
        return None

    logger.info("[Glasses VTO] Step 1 complete")

    logger.info("[Glasses VTO] Step 2: Face correction...")
    correction_message = [
        *user_message_step1,
        step1_result,
        "No, the face is different. Use this face:",
        reference_face,
    ]

    step2_result = generate_nano(client, correction_message, config=config)

    if step2_result is None:
        logger.warning(
            "[Glasses VTO] Step 2 failed, returning only Step 1 result"
        )
        return {
            "step1_image": step1_result,
            "step2_image": None,
        }

    logger.info("[Glasses VTO] Step 2 complete")

    return {
        "step1_image": step1_result,
        "step2_image": step2_result,
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_vto_image(vto_image_bytes, reference_face_bytes):
    """Evaluate a generated VTO image against reference face using InsightFace.

    Returns:
        dict: {
            "similarity_percentage": float,
            "distance": float,
            "model": str,
            "face_detected": bool
        }

    """
    logger.info("[Glasses VTO] Evaluating with InsightFace")
    try:
        future = submit_evaluation(
            reference_face_bytes, vto_image_bytes, mask_eyes=True
        )
        return future.result(timeout=120)
    except Exception as e:
        logger.error(f"[Glasses VTO] Evaluation failed: {e}")
        return {
            "similarity_percentage": 0.0,
            "distance": 2.0,
            "model": "InsightFace-ArcFace",
            "face_detected": False,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Legacy endpoints (enhance + edit — unchanged)
# ---------------------------------------------------------------------------


def enhance_photo_nano(client, image_bytes, view_type="front"):
    """Enhance a photo to studio quality using Gemini image generation.

    Args:
        client: Gemini client instance
        image_bytes: Input image as bytes
        view_type: View type - "front", "side", or "model_side"

    Returns:
        bytes: Enhanced image data, or None if generation fails

    """
    if view_type in {"side", "model_side"}:
        prompt = """A 4K studio-quality image shows the subject's side profile, facing toward the right or left. The subject is not wearing any glasses and maintains a neutral, natural expression. The lighting is bright and even, highlighting the facial contours and casting no shadows. The background is a simple, clean white, and the photograph is exceptionally sharp, free of any motion blur. When generating the image, Keep the person's hair and face intact."""
    else:
        prompt = """A professional 4K studio-quality image shows the subject's face in a front-facing view. The subject is not wearing any glasses and maintains a neutral, natural expression. The lighting is bright and even, highlighting the facial features and casting no shadows. The background is a simple, clean white, and the photograph is exceptionally sharp, free of any motion blur. When generating the image, Keep the person's hair and face intact."""

    return generate_nano(
        client, [prompt, image_bytes], model=os.getenv("MODEL_NAME_GENERATED_5")
    )


def edit_frame_nano(client, prompt, generated_image):
    """Edit a generated image using Gemini image generation.

    Args:
        client: Gemini client instance
        prompt: Edit instruction prompt
        generated_image: Image to edit as bytes

    Returns:
        bytes: Edited image data, or None if generation fails

    """
    parts = [prompt]
    parts.extend(["\nHere is the image to modify:\n", generated_image])

    return generate_nano(
        client, parts, model=os.getenv("MODEL_NAME_GENERATED_5")
    )
