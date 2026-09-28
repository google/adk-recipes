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

"""Virtual try-on image generation.

Provides functions to preprocess images and generate virtual try-on images
using a two-step approach with face correction.
"""

# Standard library imports
import logging

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

# Grey background color for upscaling compatibility
VTO_BACKGROUND_COLOR = "#F0F0F0"


def preprocess_face_image(client, img_bytes):
    """Preprocess face image: crop face, upscale, then remove background.

    Args:
        client: Gemini client instance
        img_bytes (bytes): The input face image as bytes

    Returns:
        tuple: (reference_face, preprocessed_face)
            - reference_face: Cropped and upscaled face (for evaluation)
            - preprocessed_face: Face with background removed (for generation)
            Returns (None, None) if no face is detected.

    """
    # Debug: save original input
    save_debug_image(img_bytes, "01_original", prefix="preproc_face")

    # Step 1: Crop face (with generous padding for hair)
    logger.debug("[VTO Face Preprocessing] Cropping face...")
    face_cropped = crop_face(img_bytes, debug_prefix="preproc_face")
    if face_cropped is None:
        logger.warning("[VTO Face Preprocessing] No face detected")
        return None, None

    # Debug: save cropped face
    save_debug_image(face_cropped, "02_cropped", prefix="preproc_face")

    # Step 2: Upscale (best-effort — skip if API fails)
    logger.debug("[VTO Face Preprocessing] Upscaling cropped face...")
    try:
        reference_face = upscale_image_bytes(
            client, face_cropped, upscale_factor="x4"
        )
    except Exception as e:
        logger.warning(
            f"[VTO Face Preprocessing] Upscale failed, using cropped face: {e}"
        )
        reference_face = face_cropped

    # Debug: save upscaled face (this is the reference for evaluation)
    save_debug_image(
        reference_face, "03_upscaled_reference", prefix="preproc_face"
    )

    # Step 3: Remove background (for generation input, best-effort)
    logger.debug("[VTO Face Preprocessing] Removing background...")
    try:
        preprocessed_face = replace_background(
            client,
            reference_face,
            0.01,
            VTO_BACKGROUND_COLOR,
            mask_margin_pixels=2,
            feather_radius=3,
        )
    except Exception as e:
        logger.warning(
            f"[VTO Face Preprocessing] Background removal failed, using reference face: {e}"
        )
        preprocessed_face = reference_face

    # Debug: save final preprocessed face
    save_debug_image(preprocessed_face, "04_bg_removed", prefix="preproc_face")

    logger.debug("[VTO Face Preprocessing] Preprocessing complete")
    return reference_face, preprocessed_face


def preprocess_model_image(client, img_bytes):
    """Preprocess full body model image: remove background and upscale.

    Args:
        client: Gemini client instance
        img_bytes (bytes): The input model image as bytes

    Returns:
        bytes: The preprocessed image as bytes

    """
    try:
        # Debug: save original input
        save_debug_image(img_bytes, "01_original", prefix="preproc_body")

        logger.debug("[VTO Body Preprocessing] Removing background...")
        img_no_bg = replace_background(
            client,
            img_bytes,
            background_color=VTO_BACKGROUND_COLOR,
            mask_margin_pixels=3,
            feather_radius=5,
        )

        # Debug: save after background removal
        save_debug_image(img_no_bg, "02_bg_removed", prefix="preproc_body")

        logger.debug("[VTO Body Preprocessing] Upscaling image...")
        img_upscaled = upscale_image_bytes(
            client, img_no_bg, upscale_factor="x4"
        )

        # Debug: save final upscaled image
        save_debug_image(img_upscaled, "03_upscaled", prefix="preproc_body")

        logger.debug("[VTO Body Preprocessing] Preprocessing complete")
        return img_upscaled
    except Exception as e:
        logger.error(
            f"[VTO Body Preprocessing] Error during preprocessing: {e}"
        )
        logger.warning("[VTO Body Preprocessing] Returning original image")
        return img_bytes


def _build_description_block(garment_descriptions: list[dict] | None) -> str:
    """Build a description block for VTO prompts from garment descriptions.

    Args:
        garment_descriptions: List of dicts with "general" and "details" keys,
            one per garment. None if no descriptions available.

    Returns:
        String to insert into the prompt, or empty string if no descriptions.

    """
    if not garment_descriptions:
        return ""
    blocks = []
    for i, desc in enumerate(garment_descriptions):
        general = desc.get("general", "")
        details = desc.get("details", "")
        if not general and not details:
            continue
        label = (
            f"GARMENT {i + 1}" if len(garment_descriptions) > 1 else "GARMENT"
        )
        block = f"**{label}:**"
        if general:
            block += f"\n{general}"
        if details:
            # Only include EXTERIOR details — INTERIOR tags are hidden when worn
            exterior_lines = [
                line.strip()
                for line in details.split("\n")
                if "[EXTERIOR]" in line
            ]
            if exterior_lines:
                block += "\nKey visual details to reproduce:\n" + "\n".join(
                    f"- {line}" for line in exterior_lines
                )
        blocks.append(block)
    if not blocks:
        return ""
    return (
        "\n\n### GARMENT DESCRIPTIONS\n"
        + "\n\n".join(blocks)
        + "\nIMPORTANT: Reproduce all described EXTERIOR logos, text, and brand marks accurately in the generated image.\n"
    )


def generate_vto(
    client,
    scenario,
    garment_images,
    preprocessed_person_images,
    framing="full_body",
    garment_descriptions=None,
    image_size="1K",
):
    """Generate a virtual try-on image using 2-step approach (always full body).

    Args:
        client: Gemini client instance
        scenario: Description of the scene/environment
        garment_images: List of garment image bytes
        preprocessed_person_images: List of already-preprocessed person image bytes
            - First: preprocessed face (for correction step)
            - Second: preprocessed full body model
        framing: Kept for API compatibility (always "full_body")
        garment_descriptions: Optional list of garment descriptions from
            describe_all_garments(), each with "general" and "details" keys
        image_size: Image size to generate (default: "1K")

    Returns:
        dict: {
            "step1_image": bytes,  # Image from step 1
            "step2_image": bytes,  # Image from step 2 (or None if failed)
        }

    """
    logger.debug("[VTO] Starting virtual try-on generation (full body)")

    reference_face = (
        preprocessed_person_images[0]
        if len(preprocessed_person_images) >= 1
        else None
    )
    preprocessed_model = (
        preprocessed_person_images[1]
        if len(preprocessed_person_images) >= 2
        else preprocessed_person_images[0]
    )

    system_prompt = """You are an expert fashion photographer, **high-end retoucher**, and virtual try-on specialist.
Your task is to dress the model in new garments while preserving their identity and pose.
**You must show the ENTIRE person from head to toe, including feet and shoes. The full body must be visible.**
**You must improve the image quality, fixing any input noise or masking artifacts, while ensuring the subject looks exactly like the reference.**
**LIGHTING & COMPOSITING:** You must apply uniform, consistent lighting across the entire image — the model's face, body, and garments must share the same light direction, intensity, and color temperature. Add natural soft shadows beneath the feet/shoes and subtle contact shadows where garments meet the body. The result must look like a single cohesive photograph, never like a cut-out pasted onto a background."""

    user_task = f"""### TASK: FULL BODY VIRTUAL TRY-ON & RESTORATION

Dress the model in the provided garments and generate a **full body head-to-toe shot**:

1. **PRESERVE IDENTITY & ANATOMY** - Keep the exact pose, body shape, body size, facial features, and skin tone. Do NOT make the person thinner or change their body proportions in any way.
2. **REPLACE THE CLOTHES** - Fit the provided garments naturally onto the model. Adapt the garment size to the person's actual body — the clothes should look like they fit this specific person, not like the person was changed to fit the clothes.
3. **FULL BODY FRAMING** - The image MUST show the entire person from head to feet. Do NOT crop at the knees or waist. The feet and shoes must be visible at the bottom of the frame.
4. **COMPLETE THE OUTFIT** - If not all garments are provided (e.g. only a coat), add appropriate complementary clothing (pants, shoes, etc.) that matches the style and formality of the provided garments. For example: elegant coat → dress shoes and tailored trousers, casual jacket → sneakers and jeans. The entire outfit must look cohesive.
5. **IMAGE RESTORATION** - Denoise the subject and correct any jagged masking edges or artifacts to ensure a seamless, high-fidelity result.
6. **UNIFORM LIGHTING** - Apply consistent studio lighting across the entire person — face, body, and garments must share the same light direction, intensity, and color temperature. Eliminate any lighting mismatches between the model and garments.
7. **NATURAL SHADOWS** - Add a soft, natural drop shadow beneath the person's feet/shoes on the ground plane, and subtle contact shadows where garments meet the body. This is essential to avoid a "floating cut-out" appearance.
8. **PHOTOREALISTIC RESULT** - The final output should look like a single high-resolution studio photograph, not a composite. Smooth all mask edges and blend seamlessly.

Setting: {scenario}

**CRITICAL CONSTRAINTS:**
- **Identity Lock:** The face, body shape, and body size must remain exactly as the specific person in the input. NEVER slim down, reshape, or alter the person's body in any way.
- **Garment Sizing:** Adapt the garments to fit the person's real body. The clothes stretch and drape to match the person, not the other way around.
- **Full Body:** The ENTIRE person must be visible from head to toe. Never crop the image above the feet.
- **Shoes Required:** The person MUST be wearing shoes. Never generate bare feet. If shoes are not provided as a garment, add stylistically appropriate footwear.
- **Quality Boost:** Ignore low-quality artifacts in the source. Generate a clean, sharp version of that same person.
- **Edge Correction:** If the input has a "cut-out" look, feather the edges, fill in natural lighting, and add soft shadows to blend the model seamlessly into the scene. Never leave hard mask edges visible.
- **Lighting Uniformity:** Match the light on the face, skin, and garments so the entire image has one coherent light source. No mismatched highlights or shadows.
- Only the clothing changes; the person remains the same.
{_build_description_block(garment_descriptions)}
Generate the high-fidelity image."""

    logger.debug("[VTO] Step 1: Virtual try-on (full body)...")

    user_message_step1 = [
        "### MODEL IMAGE (Reference for identity, pose, and body shape): ",
        preprocessed_model,
        "### GARMENTS TO DRESS THE MODEL IN: ",
        *garment_images,
        user_task,
    ]

    config = get_generate_content_config(
        temperature=0.1,
        response_modalities=["IMAGE"],
        system_instruction=system_prompt,
        image_config={
            "aspect_ratio": "3:4",
            "image_size": image_size,
            "output_mime_type": "image/png",
        },
    )

    step1_result = generate_nano(client, user_message_step1, config=config)

    if step1_result is None:
        logger.error("[VTO] Step 1 failed: Could not generate virtual try-on")
        return None

    save_debug_image(step1_result, "step1_vto_result", prefix="vto")
    logger.debug("[VTO] Step 1 complete")

    logger.debug("[VTO] Step 2: Face improvement with correction...")

    correction_message = [
        *user_message_step1,
        step1_result,
        "No, the face is different. Use this face:",
        reference_face,
    ]

    step2_result = generate_nano(client, correction_message, config=config)

    if step2_result is None:
        logger.warning("[VTO] Step 2 failed, returning only Step 1 result")
        return {
            "step1_image": step1_result,
            "step2_image": None,
        }

    save_debug_image(step2_result, "step2_corrected", prefix="vto")
    logger.debug("[VTO] Step 2 complete")

    return {
        "step1_image": step1_result,
        "step2_image": step2_result,
    }


def evaluate_vto_image(vto_image_bytes, reference_face_bytes):
    """Evaluate a generated VTO image against reference face using InsightFace.

    Args:
        vto_image_bytes: Generated VTO image bytes
        reference_face_bytes: Reference face image bytes for evaluation

    Returns:
        dict: {
            "similarity_percentage": float,
            "distance": float,
            "model": str,
            "face_detected": bool
        }

    """
    logger.debug("[VTO] Evaluating with InsightFace")
    try:
        future = submit_evaluation(reference_face_bytes, vto_image_bytes)
        return future.result(timeout=120)
    except Exception as e:
        logger.error(f"[VTO] Evaluation failed: {e}")
        return {
            "similarity_percentage": 0.0,
            "distance": 2.0,
            "model": "InsightFace-ArcFace",
            "face_detected": False,
            "error": str(e),
        }
