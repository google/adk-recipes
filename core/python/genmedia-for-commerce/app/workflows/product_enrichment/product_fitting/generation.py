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

"""Product fitting image generation -- single-step generation.

Framing-adapted prompts focused on garment fit (no face correction step).
"""

import logging
import os

from workflows.shared.debug_utils import save_debug_image
from workflows.shared.llm_utils import get_generate_content_config
from workflows.shared.nano_banana import NANO_TIMEOUT_SECONDS, generate_nano

logger = logging.getLogger(__name__)


def generate_fitting(
    client,
    scenario,
    garment_images,
    preprocessed_model,
    framing="full_body",
    model=os.getenv("MODEL_NAME_GENERATED_5"),  # noqa: B008
    garment_view="front",
    garment_description=None,
    model_gender=None,
    timeout=NANO_TIMEOUT_SECONDS,
):
    """Generate a product fitting image -- single-step generation (no face correction).

    Args:
        client: Gemini client instance
        scenario: Description of the scene/environment
        garment_images: List of garment image bytes (up to 2 reference images of the same product)
        preprocessed_model: Preprocessed full body model image bytes
        framing: One of "full_body", "upper_body", "lower_body"
        model: Gemini model to use for generation
        garment_view: "front" or "back" -- which side of the garment is shown
        garment_description: Pre-extracted description of the garment (default: None)
        model_gender: Gender string or None
        timeout: Timeout in seconds

    Returns:
        tuple: (image_bytes, user_message, config) or (None, None, None) if failed

    """
    logger.debug(
        f"[Fitting] Starting product fitting generation (framing: {framing}, model: {model}, garment_view: {garment_view}, num_ref_images: {len(garment_images)})"
    )

    if framing == "upper_body":
        system_prompt, user_task, aspect_ratio = _get_upper_body_fitting_prompt(
            scenario, garment_view, garment_description, model_gender
        )
    elif framing == "head":
        system_prompt, user_task, aspect_ratio = _get_head_fitting_prompt(
            scenario, garment_view, garment_description, model_gender
        )
    elif framing == "footwear":
        system_prompt, user_task, aspect_ratio = _get_footwear_fitting_prompt(
            scenario, garment_view, garment_description, model_gender
        )
    elif framing == "lower_body":
        system_prompt, user_task, aspect_ratio = _get_lower_body_fitting_prompt(
            scenario, garment_view, garment_description, model_gender
        )
    else:
        system_prompt, user_task, aspect_ratio = _get_full_body_fitting_prompt(
            scenario, garment_view, garment_description, model_gender
        )

    logger.debug(f"[Fitting] System prompt:\n{system_prompt}")
    logger.debug(f"[Fitting] User task:\n{user_task}")

    view_label = "BACK VIEW" if garment_view == "back" else "FRONT VIEW"

    user_message = [
        "### MODEL IMAGE (Reference for body shape and pose): ",
        preprocessed_model,
    ]
    if len(garment_images) == 1:
        user_message.append(f"### PRODUCT TO FIT ON THE MODEL ({view_label}): ")
        user_message.append(garment_images[0])
    else:
        user_message.append(
            f"### PRODUCT TO FIT ON THE MODEL ({view_label}) — {len(garment_images)} reference images of the SAME product from different angles: "
        )
        for img in garment_images:
            user_message.append(img)
    user_message.append(user_task)

    config = get_generate_content_config(
        temperature=0.1,
        response_modalities=["IMAGE"],
        system_instruction=system_prompt,
        image_config={
            "aspect_ratio": aspect_ratio,
            "image_size": "2K",
            "output_mime_type": "image/png",
        },
    )

    result = generate_nano(
        client, user_message, model=model, config=config, timeout=timeout
    )

    if result is None:
        logger.error("[Fitting] Generation failed")
        return None, None, None

    save_debug_image(result, "fitting_result", prefix="fitting")
    logger.debug("[Fitting] Generation complete")
    return result, user_message, config


def _get_full_body_fitting_prompt(
    scenario, garment_view="front", garment_description=None, model_gender=None
):
    """Prompt for full body product fitting — adaptive framing based on garment length."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )
    view_str = (
        "from behind (back view)"
        if garment_view == "back"
        else "from the front, facing the camera"
    )

    system_prompt = f"""You are an expert fashion photographer creating a product fitting image — showing how a product looks when worn on a real body.{identity_str}
Dress the model with the provided product. Keep everything else about the person identical — body, pose, skin, hands, proportions.
Frame the shot to show the ENTIRE garment — from slightly above where it starts to slightly below where it ends."""

    user_task = f"""Fit this product on the model. Show the model {view_str}.

Rules:
- **Keep the model identical** — same body, pose, skin tone, proportions, hands. Only clothing changes.
- **Reproduce the product exactly** — correct color, pattern, texture, logos, and all design details.
- **Do NOT invent hidden details** — only reproduce details clearly visible in the reference image. For surfaces not shown in the reference (e.g. underside of a hat brim, inside of a shoe, back of a product shown from the front), keep them plain and neutral, matching the product's base color and material. Do NOT copy or move patterns, prints, or logos onto surfaces where they are not visible in the reference.
- **Do NOT mirror the design** — the reference image shows the garment as seen from the front, which is the same left/right orientation as looking at the model. A logo on the LEFT side of the reference image must appear on the LEFT side of the model as you look at them (i.e. on the model's right). Do NOT flip or swap sides.
- **Product must be 100% visible and stand out** — every part of the product must be fully visible. No other garment may cover, overlap, or hide any part of it. Choose complementary garments in colors that contrast with the product so it stands out clearly.
- **Complete outfit** — add complementary garments that do not hide the product.
- **Realistic fit** — natural wrinkles, drape, fabric tension. Not flat or pasted on.
- **Framing** — adapt the framing to the garment's length. The ENTIRE product must be visible from its topmost point (e.g. shoulder straps, neckline) to its bottommost point (e.g. hem, cuffs). Include a small margin of the body beyond where the garment ends so the edges are clearly visible. If the garment reaches the ankles or below, include shoes.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def _get_upper_body_fitting_prompt(
    scenario, garment_view="front", garment_description=None, model_gender=None
):
    """Prompt for upper body product fitting — waist up."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )
    view_str = (
        "from behind (back view)"
        if garment_view == "back"
        else "from the front, facing the camera"
    )

    system_prompt = f"""You are an expert fashion photographer creating a product fitting image — showing how a product looks when worn on a real body.{identity_str}
Dress the model with the provided product. Keep everything else about the person identical — body, pose, skin, hands, proportions.
Frame the shot from the waist up."""

    user_task = f"""Fit this product on the model. Show the model {view_str}. Waist-up shot.

Rules:
- **Keep the model identical** — same body, pose, skin tone, proportions, hands. Only clothing changes.
- **Reproduce the product exactly** — correct color, pattern, texture, logos, and all design details.
- **Do NOT invent hidden details** — only reproduce details clearly visible in the reference image. For surfaces not shown in the reference (e.g. underside of a hat brim, inside of a shoe, back of a product shown from the front), keep them plain and neutral, matching the product's base color and material. Do NOT copy or move patterns, prints, or logos onto surfaces where they are not visible in the reference.
- **Do NOT mirror the design** — the reference image shows the garment as seen from the front, which is the same left/right orientation as looking at the model. A logo on the LEFT side of the reference image must appear on the LEFT side of the model as you look at them (i.e. on the model's right). Do NOT flip or swap sides.
- **Product must be 100% visible and stand out** — every part of the product must be fully visible. No other garment may cover, overlap, or hide any part of it. Choose complementary garments in colors that contrast with the product so it stands out clearly.
- **Realistic fit** — natural wrinkles, drape, fabric tension. Not flat or pasted on.
- **Framing** — frame from the waist up. The product should be fully visible and prominently displayed.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def _get_head_fitting_prompt(
    scenario, garment_view="front", garment_description=None, model_gender=None
):
    """Prompt for head accessory product fitting — head to shoulders, slightly above eye level."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )
    if model_gender in ("boy", "girl"):
        age_str = f" The model is a child ({model_gender}, approximately 12 years old) — the face and head must look like a child's, NOT an adult's."
    else:
        age_str = ""
    view_str = (
        "from behind (back view)"
        if garment_view == "back"
        else "from the front"
    )

    system_prompt = f"""You are an expert fashion photographer creating a product fitting image — showing how a head accessory looks when worn.{identity_str}{age_str}
Put the provided accessory on the model. Keep the person's face, skin tone, and body the same — but allow natural adjustments (e.g. hair tucked under a hat, slight pose change) so the accessory looks naturally worn.
Frame from head to chest."""

    user_task = f"""Put this accessory on the model. Frame from head to chest. Show the model {view_str}.

Rules:
- **Keep the model recognizable** — same face, skin tone, proportions. Allow natural adjustments to hair and pose so the accessory fits realistically.
- **Reproduce the product exactly** — correct color, pattern, texture, logos, and all design details.
- **Do NOT invent hidden details** — only reproduce details clearly visible in the reference image. For surfaces not shown in the reference (e.g. underside of a hat brim, inside of glasses frames), keep them plain and neutral, matching the product's base color and material. Do NOT copy or move patterns, prints, or logos onto surfaces where they are not visible in the reference.
- **Do NOT mirror the design** — the reference image shows the product as seen from the front. Do NOT flip or swap sides.
- **Realistic fit** — the accessory must sit naturally on the model's head/face with proper proportions.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def _get_footwear_fitting_prompt(
    scenario, garment_view="front", garment_description=None, model_gender=None
):
    """Prompt for footwear product fitting — knees down, tight crop on shoes."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )
    if model_gender in ("boy", "girl"):
        age_str = f" The model is a child ({model_gender}, approximately 12 years old) — the legs, feet, and skin must look like a child's, NOT an adult's. Use appropriately small, child-sized footwear."
    else:
        age_str = ""
    view_str = (
        "from behind (back view)"
        if garment_view == "back"
        else "from the front"
    )

    system_prompt = f"""You are an expert fashion photographer creating a product fitting image — showing how footwear looks when worn on a real body.{identity_str}{age_str}
Put the provided shoes/boots/sandals on the model's feet. Keep the person's body, pose, skin, and proportions identical.
Choose pants or legwear that match the footwear's style and brand — do NOT keep the model's original clothes.
Frame the shot tightly from the knees down to the ground, focusing on the feet and shoes."""

    user_task = f"""Put this footwear on the model. Frame tightly from KNEES DOWN to the ground — the shoes must be the focal point and take up a large portion of the image. Show the model {view_str}.

Rules:
- **Keep the model's body identical** — same pose, skin tone, proportions. Only footwear and legwear change.
- **Reproduce the footwear exactly** — correct color, pattern, texture, logos, sole, laces, and all design details.
- **Do NOT invent hidden details** — only reproduce details clearly visible in the reference image. For surfaces not shown in the reference (e.g. sole bottom, inside of the shoe), keep them plain and neutral, matching the product's base color and material. Do NOT copy or move patterns, prints, or logos onto surfaces where they are not visible in the reference.
- **Do NOT mirror the design** — the reference image shows the footwear as seen from the front, which is the same left/right orientation as looking at the model. Do NOT flip or swap sides.
- **Footwear must be the hero** — the shoes must be clearly visible, well-lit, and take up significant space in the frame. Both shoes should be visible.
- **Tight framing** — show from knees down only. Do NOT show the full body, waist, or torso. The camera is low, near ground level, angled slightly down at the feet.
- **Style-matched legwear** — choose legwear that matches the footwear's style AND brand. If the shoes are from a recognizable brand (e.g. Adidas, Nike), use pants/joggers from the same brand. Match the overall aesthetic: sandals/heels with bare legs or a skirt, sneakers with joggers or jeans, dress shoes with tailored trousers, boots with jeans. Do NOT keep the model's original pants.
- **Realistic fit** — natural stance, shoes properly on feet, laces tied, realistic ground contact and shadows.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def _get_lower_body_fitting_prompt(
    scenario, garment_view="front", garment_description=None, model_gender=None
):
    """Prompt for lower body product fitting — hips down."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )
    view_str = (
        "from behind (back view)"
        if garment_view == "back"
        else "from the front"
    )

    system_prompt = f"""You are an expert fashion photographer creating a product fitting image — showing how a product looks when worn on a real body.{identity_str}
Dress the model with the provided product. Keep everything else about the person identical — body, pose, skin, hands, proportions.
Frame the shot from waist to feet."""

    user_task = f"""Fit this product on the model. Frame from waist to feet. Show the model {view_str}.

Rules:
- **Keep the model identical** — same body, pose, skin tone, proportions, hands. Only clothing changes.
- **Reproduce the product exactly** — correct color, pattern, texture, logos, and all design details.
- **Do NOT invent hidden details** — only reproduce details clearly visible in the reference image. For surfaces not shown in the reference (e.g. underside, inside, back of a product shown from the front), keep them plain and neutral, matching the product's base color and material. Do NOT copy or move patterns, prints, or logos onto surfaces where they are not visible in the reference.
- **Do NOT mirror the design** — the reference image shows the garment as seen from the front, which is the same left/right orientation as looking at the model. A logo on the LEFT side of the reference image must appear on the LEFT side of the model as you look at them (i.e. on the model's right). Do NOT flip or swap sides.
- **Product must be 100% visible and stand out** — every part of the product must be fully visible. No other garment may cover, overlap, or hide any part of it. Choose complementary garments in colors that contrast with the product so it stands out clearly.
- **Complete lower outfit** — add appropriate complementary items (shoes, socks). Must wear shoes.
- **Realistic fit** — natural creases, drape, fabric tension. Not flat or pasted on.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def generate_fitting_back_from_front(
    client,
    scenario,
    back_garment_images,
    best_front_image,
    framing="full_body",
    model=os.getenv("MODEL_NAME_GENERATED_5"),  # noqa: B008
    garment_description=None,
    model_gender=None,
    timeout=NANO_TIMEOUT_SECONDS,
):
    """Generate a back-view fitting image starting from the best front result.

    Returns:
        tuple: (image_bytes, user_message, config) or (None, None, None) if failed

    """
    logger.debug(
        f"[Fitting] Starting back-from-front generation (framing: {framing}, model: {model}, num_back_refs: {len(back_garment_images)})"
    )

    if framing == "upper_body":
        system_prompt, user_task, aspect_ratio = (
            _get_upper_body_back_from_front_prompt(
                scenario, garment_description, model_gender
            )
        )
    elif framing == "lower_body":
        system_prompt, user_task, aspect_ratio = (
            _get_lower_body_back_from_front_prompt(
                scenario, garment_description, model_gender
            )
        )
    else:
        system_prompt, user_task, aspect_ratio = (
            _get_full_body_back_from_front_prompt(
                scenario, garment_description, model_gender
            )
        )

    logger.debug(f"[Fitting] Back-from-front system prompt:\n{system_prompt}")
    logger.debug(f"[Fitting] Back-from-front user task:\n{user_task}")

    user_message = [
        "### FRONT IMAGE (reference for the model -- same person, body, and outfit): ",
        best_front_image,
    ]
    if len(back_garment_images) == 1:
        user_message.append(
            "### BACK OF THE PRODUCT (reference for the garment's back appearance): "
        )
        user_message.append(back_garment_images[0])
    else:
        user_message.append(
            f"### BACK OF THE PRODUCT -- {len(back_garment_images)} reference images of the SAME product's back: "
        )
        for img in back_garment_images:
            user_message.append(img)
    user_message.append(user_task)

    config = get_generate_content_config(
        temperature=0.1,
        response_modalities=["IMAGE"],
        system_instruction=system_prompt,
        image_config={
            "aspect_ratio": aspect_ratio,
            "image_size": "1K",
            "output_mime_type": "image/png",
        },
    )

    result = generate_nano(
        client, user_message, model=model, config=config, timeout=timeout
    )

    if result is None:
        logger.error("[Fitting] Back-from-front generation failed")
        return None, None, None

    save_debug_image(result, "fitting_back_from_front_result", prefix="fitting")
    logger.debug("[Fitting] Back-from-front generation complete")
    return result, user_message, config


def fix_fitting(
    client,
    original_message,
    original_config,
    generated_image,
    eval_feedback,
    model,
    timeout=NANO_TIMEOUT_SECONDS,
):
    """Fix a generated fitting image by appending the AI output and fix feedback.

    Appends to the original generation message (multi-turn style conversation).

    Args:
        client: Gemini client instance
        original_message: The original user_message list from generation
        original_config: The original GenerateContentConfig from generation
        generated_image: The generated fitting image bytes to fix
        eval_feedback: String describing what's wrong (from garment_details)
        model: Gemini model to use
        timeout: Timeout in seconds

    Returns:
        bytes: Fixed fitting image, or None if generation failed

    """
    logger.debug("[Fitting] Starting fix attempt")

    fix_message = [
        *original_message,
        generated_image,
        f"""No, the garment reproduction has issues:\n{eval_feedback}\n\nFix ONLY the issues described above. Keep everything else identical — same pose, body, composition, setting, complementary garments. Reproduce the garment details exactly as shown in the reference images. Generate the corrected image.""",
    ]

    result = generate_nano(
        client,
        fix_message,
        model=model,
        config=original_config,
        timeout=timeout,
    )

    if result is None:
        logger.error("[Fitting] Fix generation failed")
        return None

    save_debug_image(result, "fitting_fix_result", prefix="fitting")
    logger.debug("[Fitting] Fix generation complete")
    return result


def _get_full_body_back_from_front_prompt(
    scenario, garment_description=None, model_gender=None
):
    """Prompt for full body back-from-front fitting — adaptive framing."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )

    system_prompt = f"""You are an expert fashion photographer creating a back-view product fitting image.{identity_str}
Turn the person around to show them from behind. Keep everything identical — same person, body — only change the viewing angle and swap the garment to match the back reference images.
Frame the shot to show the ENTIRE garment — from slightly above where it starts to slightly below where it ends."""

    user_task = f"""Show the SAME person from the front image, now seen from BEHIND.

Rules:
- **Same person, same outfit** — identical body, skin tone, hair, and all other garments. Just turned around. Do NOT change any clothing except swapping the product to its back view.
- **Match the back reference** — the garment's back must match the provided back reference images exactly (color, pattern, logos, design details).
- **Do NOT mirror the design** — the back reference image shows the garment as seen from behind, which is the same left/right orientation as looking at the model from behind. A logo on the LEFT side of the back reference must appear on the LEFT side of the model as you look at them from behind. Do NOT flip or swap sides.
- **Product must be 100% visible** — every part of the product must be fully visible from behind.
- **Realistic fit** — natural drape and wrinkles as seen from behind.
- **Framing** — adapt the framing to the garment's length. The ENTIRE product must be visible from its topmost point (e.g. shoulder straps, neckline) to its bottommost point (e.g. hem, cuffs). Include a small margin of the body beyond where the garment ends so the edges are clearly visible. If the garment reaches the ankles or below, include shoes.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def _get_upper_body_back_from_front_prompt(
    scenario, garment_description=None, model_gender=None
):
    """Prompt for upper body back-from-front fitting."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )

    system_prompt = f"""You are an expert fashion photographer creating a back-view product fitting image.{identity_str}
Turn the person around to show them from behind. Keep everything identical — same person, body — only change the viewing angle and swap the garment to match the back reference images.
Frame the shot from the waist up."""

    user_task = f"""Show the SAME person from the front image, now seen from BEHIND. Waist-up shot.

Rules:
- **Same person, same outfit** — identical body, skin tone, hair, and all other garments. Just turned around. Do NOT change any clothing except swapping the product to its back view.
- **Match the back reference** — the garment's back must match the provided back reference images exactly (color, pattern, logos, design details).
- **Do NOT mirror the design** — the back reference image shows the garment as seen from behind, which is the same left/right orientation as looking at the model from behind. A logo on the LEFT side of the back reference must appear on the LEFT side of the model as you look at them from behind. Do NOT flip or swap sides.
- **Product must be 100% visible** — every part of the product must be fully visible from behind.
- **Realistic fit** — natural drape and wrinkles as seen from behind.
- **Framing** — frame from the waist up. Do NOT show the face.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"


def _get_lower_body_back_from_front_prompt(
    scenario, garment_description=None, model_gender=None
):
    """Prompt for lower body back-from-front fitting."""
    identity_str = (
        f" The model is the {model_gender} in input." if model_gender else ""
    )

    system_prompt = f"""You are an expert fashion photographer creating a back-view product fitting image.{identity_str}
Turn the person around to show the lower body from behind. Keep everything identical — same body, shoes — only change the viewing angle and swap the garment to match the back reference images.
Frame the shot from waist to feet."""

    user_task = f"""Show the SAME person's lower body from the front image, now seen from BEHIND. Frame from waist to feet.

Rules:
- **Same person, same outfit** — identical body proportions, skin tone, shoes, and all other garments. Just turned around. Do NOT change any clothing except swapping the product to its back view.
- **Match the back reference** — the garment's back must match the provided back reference images exactly.
- **Do NOT mirror the design** — the back reference image shows the garment as seen from behind, which is the same left/right orientation as looking at the model from behind. A logo on the LEFT side of the back reference must appear on the LEFT side of the model as you look at them from behind. Do NOT flip or swap sides.
- **Product must be 100% visible** — every part of the product must be fully visible from behind.
- **Realistic fit** — natural creases, drape as seen from behind.
- Must wear shoes.

Setting: {scenario}
{f"Product: {garment_description.get('general', '')}" if garment_description and garment_description.get("general") else ""}
Generate the image."""

    return system_prompt, user_task, "3:4"
