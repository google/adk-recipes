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

"""Garment classification and image selection for product fitting."""

import json
import logging
import os

from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)

GARMENT_CATEGORIES = [
    "top",
    "full_body_outer",
    "bottom",
    "footwear",
    "dress",
    "head_accessory",
]
VALID_VIEWS = ["front", "back", "other"]


def classify_garments(
    client,
    garment_images_bytes_list: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> dict:
    """Classify multiple images of the same garment in a single call.

    Returns:
        dict: {
            "category": str,          # single category for the garment
            "description": str,        # brief description
            "views": [                 # one entry per input image, in order
                {"index": 0, "view": "front"},
                {"index": 1, "view": "back"},
                {"index": 2, "view": "other"},
                ...
            ]
        }

    """
    num_images = len(garment_images_bytes_list)

    system_prompt = f"""You are a garment classifier. You will receive {num_images} image(s) of the SAME clothing item from different angles.

Your task:
1. Determine the garment CATEGORY (one category for the entire garment, not per image).
2. Write a brief DESCRIPTION of the garment (e.g. "black cycling bib shorts").
3. For EACH image, classify the VIEW it shows.

STRICT RULE: The entire product must be visible in the final photo. If a garment has ANY component that extends above the waist (shoulder straps, bib front, suspenders, crossed back straps), it MUST be classified as "dress" — even if the lower portion looks like shorts or pants. This includes cycling bib shorts, ski bib pants, fishing waders, overalls, dungarees, and any garment with built-in straps.

You must also consider the LENGTH of the garment:
- A short jacket (waist-length or hip-length) is a "top"
- A long coat or trench coat that extends below the hips/to the knees or longer is a "full_body_outer"

Categories:
- "top": shirts, t-shirts, blouses, sweaters, hoodies, turtlenecks, tank tops, vests, short jackets, cropped coats (any upper body garment that ends at or above the hips)
- "full_body_outer": long coats, trench coats, long cardigans, dusters, any outerwear that extends below the hips/to the knees or longer
- "bottom": pants, trousers, jeans, shorts, skirts (ONLY if no straps or bib — pure waist-down garments)
- "dress": dresses, jumpsuits, rompers, overalls, bib shorts, bib pants, cycling bibs, dungarees, any garment with shoulder straps or bib construction (any single garment that extends both above and below the waist)
- "footwear": any footwear (sneakers, heels, boots, sandals, slippers, cleats)
- "head_accessory": hats, caps, beanies, headbands, sunglasses, glasses, earrings, or any accessory worn on the head/face

View classification for each image:
- "front": the image shows the front of the garment. Slightly angled front views also count as "front".
- "back": the image shows the back of the garment. Slightly angled back views also count as "back".
- "other": the image is a side view, the garment is worn by a person, there are multiple garments, or the image is not a valid product photo.

How to determine front vs back — use these cues in order of reliability:
1. **Closures and hardware**: buttons, main zippers, snaps, front clasps → FRONT. Back zippers (e.g. dresses) → BACK.
2. **Pockets**: front hip pockets, chest pockets → FRONT. Back patch pockets (e.g. jeans, cycling jerseys) → BACK.
3. **Garment construction and cut**: the FRONT of a garment typically has MORE fabric coverage and structure on the torso (wider panels, fuller coverage). The BACK typically has LESS coverage or thinner construction (racerback straps, cutaway shoulders, thinner panels, open-back designs). For example: vests have a full front panel but a thinner back; tank tops have wider straps in front but a racerback behind; cycling bibs have a bib panel covering the front abdomen while the back has a mesh racerback with a chamois pad in the seat area.
4. **Neckline shape**: deeper V-neck, scoop neck, or decorative neckline → usually FRONT. Higher, simpler neckline → usually BACK.
5. **Tags and labels**: small fabric tags at the inner neckline are typically at the BACK of the garment.

Return a JSON object:
{{
  "category": "<one of: top, full_body_outer, bottom, dress, footwear, head_accessory>",
  "description": "<brief description>",
  "views": [
    {{"index": 0, "view": "<front|back|other>"}},
    {{"index": 1, "view": "<front|back|other>"}},
    ...
  ]
}}

The "views" array must have exactly {num_images} entries, one per image, in order.
Only return the JSON object, nothing else."""

    pieces: list = []
    for i, img in enumerate(garment_images_bytes_list):
        pieces.append(f"IMAGE_{i}:")
        pieces.append(img)
    pieces.append(
        "Classify this garment and determine the view for each image. For the view classification, carefully analyze the garment construction to determine front vs back — do not guess based on first impression."
    )

    config = get_generate_content_config(
        temperature=0,
        thinking_level="HIGH",
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=pieces,
            client=client,
            config=config,
            model=model,
        )
        result = json.loads(response_text)

        # Validate category
        if result.get("category") not in GARMENT_CATEGORIES:
            result["category"] = "top"

        # Validate views
        views = result.get("views", [])
        validated_views = []
        for i in range(num_images):
            if i < len(views):
                view_entry = views[i]
                view_val = view_entry.get("view", "other")
                if view_val not in VALID_VIEWS:
                    view_val = "other"
                validated_views.append({"index": i, "view": view_val})
            else:
                validated_views.append({"index": i, "view": "other"})
        result["views"] = validated_views

        return result
    except Exception as e:
        logger.error(f"Error classifying garments: {e}")
        return {
            "category": "top",
            "description": f"Error: {e!s}",
            "views": [{"index": i, "view": "other"} for i in range(num_images)],
        }


def get_framing(category: str) -> str:
    """Determine the image framing based on the garment category.

    Returns:
        "full_body": dress or full_body_outer
        "upper_body": top
        "head": head_accessory
        "footwear": footwear
        "lower_body": bottom (default)

    """
    if category in ("dress", "full_body_outer"):
        return "full_body"
    elif category == "top":
        return "upper_body"
    elif category == "head_accessory":
        return "head"
    elif category == "footwear":
        return "footwear"
    else:
        return "lower_body"


def _select_best_view(
    client,
    garment_images: list[bytes],
    view: str,
    model: str,
    category: str = "",
) -> dict:
    """Rank pre-classified garment images by quality and extract details.

    The images have already been validated and classified by classify_garments
    (which sees all images together). This function trusts that classification
    and only evaluates angle quality and extracts visible text/logos.

    Args:
        client: Gemini client instance
        garment_images: List of garment image bytes (pre-classified as this view)
        view: "front" or "back"
        model: Gemini model to use
        category: Garment category (e.g. "footwear") for category-specific instructions

    Returns:
        dict: {"best_indices": list[int], "evaluations": list[dict], "view_details": str, "rejected_views": list[dict]}

    """
    num_images = len(garment_images)

    shoes_instruction = ""
    if category == "footwear":
        shoes_instruction = """
**FOOTWEAR-SPECIFIC INSTRUCTIONS:**
These are footwear images. Pick the 2 best images using this priority:
1. **Front/lateral view** (side view showing the shoe's main design) — HIGHEST priority
2. **Medial/internal side view** (the inner side of the shoe) — SECOND priority
3. **3/4 angle view** — acceptable
4. **Top-down, sole-only, or heel-only views** — LOWEST priority

Prefer images where the full shoe is visible with good detail and a clean background.
For the "angle" classification: any clear side/lateral/3/4 view counts as "perfect" for footwear. Only classify as "angled" if the shoe is heavily tilted, partially obscured, or at an unusual angle.
"""

    system_prompt = f"""You are a product image evaluator for e-commerce product photography.

You will receive {num_images} product image(s), labeled IMAGE_0, IMAGE_1, etc.
{"These are footwear product images. No view classification has been done — your job is to pick the best images based on angle and quality." if category == "footwear" else f"These images have already been classified as showing the **{view.upper()} VIEW** of the product. Do NOT re-evaluate which view they show — that has already been determined."}

Your job is to rank them by image quality.
{shoes_instruction}
For each image, evaluate:

1. **Angle** — Classify how straight-on the camera angle is:
   - "perfect": The product is photographed from a dead-on, straight angle. The product is flat, symmetric, and squared to the camera. No rotation, no tilt, no 3/4 turn.
   - "angled": The product is rotated, tilted, or at a slight angle (e.g. 3/4 view, slightly turned). You can see some of the side.

   **Be strict:** Only classify as "perfect" if the product is truly squared to the camera with no visible rotation. If you can see even a slight angle or a hint of the side, classify as "angled" instead.

2. **Quality** — Rate 1-10 based on clarity, resolution, how well product details are visible, clean background.

Pick the **up to 2 best** images ranked by: perfect angle > angled, then by quality descending.

Return a JSON object:
{{
  "evaluations": [
    {{"index": 0, "angle": "perfect|angled", "quality": 1-10}},
    ...
  ],
  "best_indices": [<up to 2 indices, ordered by perfect>angled then quality descending>]
}}

Only return the JSON object, nothing else."""

    pieces: list = []
    for i, img in enumerate(garment_images):
        pieces.append(f"IMAGE_{i}:")
        pieces.append(img)
    pieces.append(f"Rank all {num_images} images by quality.")

    config = get_generate_content_config(
        temperature=0,
        thinking_budget=0,
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=pieces,
            client=client,
            config=config,
            model=model,
        )

        result = json.loads(response_text)
        raw_indices = result.get("best_indices", [])
        if not isinstance(raw_indices, list):
            raw_indices = [raw_indices] if raw_indices is not None else []
        # Filter out-of-range indices
        valid_indices = [
            i for i in raw_indices if isinstance(i, int) and 0 <= i < num_images
        ]

        # Sort: perfect angle first, then angled, preserving quality order
        evaluations = result.get("evaluations", [])
        eval_by_index = {e["index"]: e for e in evaluations if "index" in e}
        valid_indices.sort(
            key=lambda i: (
                0 if eval_by_index.get(i, {}).get("angle") == "perfect" else 1
            )
        )

        result["best_indices"] = valid_indices[:2]
        result["rejected_views"] = []

        return result

    except Exception as e:
        logger.error(f"Error selecting best {view} garment image: {e}")
        return {
            "best_indices": [],
            "evaluations": [],
            "view_details": "",
            "rejected_views": [],
        }


def select_best_front(
    client,
    garment_images: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    category: str = "",
) -> dict:
    """Select up to 2 best front-view garment images from a list."""
    logger.debug(
        f"[Fitting] Selecting best front images from {len(garment_images)} candidates"
    )
    result = _select_best_view(
        client, garment_images, "front", model, category=category
    )
    logger.debug(f"[Fitting] Best front: indices={result.get('best_indices')}")
    for ev in result.get("evaluations", []):
        logger.debug(
            f"[Fitting]   IMAGE_{ev.get('index')}: angle={ev.get('angle')}, quality={ev.get('quality')}"
        )
    return result


def describe_garment_detailed(
    client,
    front_images: list[bytes],
    back_images: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> dict:
    """Generate a detailed description of the garment including brand identification.

    Extracts text/logo details and construction details using all images together.

    Args:
        client: Gemini client instance
        front_images: Selected front garment images
        back_images: Selected back garment images
        model: Gemini model to use (default: "gemini-3.6-flash")

    Returns:
        dict with keys: general, front_details, back_details

    """
    if not front_images and not back_images:
        return {
            "general": "No garment images available",
            "front_details": "No front view available",
            "back_details": "No back view available",
        }

    system_prompt = """You are a fashion product analyst. You will receive product images from different angles of the same product.

Your tasks:
1. **Identify** the product type, material, and color scheme. Combine clues from ALL images. Do NOT guess or infer brand names — only include a brand if its name or logo is clearly and unambiguously visible as text or a printed/embroidered mark in the images.
2. **Extract all visible text, logos, prints, and brand marks** for each view. Systematically check EVERY part of the garment: left leg, right leg, left strap, right strap, waistband, front panel, back panel, collar, sleeves, etc. For EACH text/logo found, describe its content, color, and exact position (e.g. 'Silver SCOTT text on lower front of LEFT leg.'). Do NOT skip any part — if the same text appears on both legs, list it for BOTH legs.
3. **Extract key construction details** for each view — pockets, zippers, seams, panels, closures, ribbing, etc.
4. **Cross-reference both views**: if one view shows a logo on one strap/leg, check the other view for the same or matching element. Symmetrical garments often have the same branding on both sides — if clearly present on one side, infer it for the other.

IMPORTANT — Classify each detail as [EXTERIOR] or [INTERIOR]:
- [EXTERIOR]: Elements on the garment's outward-facing surface visible when worn normally (printed text, embroidered logos, sewn-on patches, construction features).
- [INTERIOR]: Elements on the inner-facing side hidden when worn — interior linings, inner fabric colors, interior branding/text, sewn-in tags, care labels, size labels, chamois pads.

IMPORTANT — Only include details you can clearly identify with confidence:
- Do NOT include tiny marks, dots, or specks that could be image artifacts or fabric texture.
- Do NOT include details you cannot read or identify clearly — if you have to guess, leave it out.
- If you cannot confirm a text/logo in the actual images, do NOT include it.
- Do NOT guess or infer the material, color, or finish of small elements (e.g. do NOT say "silver metal" or "gold embossed" unless you can clearly see it). Just describe the shape and position.

Return a JSON object:
{
  "general": "Product type, material, color scheme. Only include brand if clearly visible in the images.",
  "front_details": "[EXTERIOR]/[INTERIOR] details for the front view — text, logos, and construction details",
  "back_details": "[EXTERIOR]/[INTERIOR] details for the back view — text, logos, and construction details"
}

IMPORTANT:
- Be specific about the product type (e.g. 'cycling bib shorts' not just 'shorts')
- When adding inferred details from cross-referencing, mark them clearly
- If no front or back images are provided, set that view's details to "No [front/back] view available"
"""

    pieces: list = []
    for i, img in enumerate(front_images):
        pieces.append(f"IMAGE {i} (front):")
        pieces.append(img)
    for i, img in enumerate(back_images):
        pieces.append(f"IMAGE {len(front_images) + i} (back):")
        pieces.append(img)
    pieces.append(
        "Identify the brand and product, and refine the detail lists for both views."
    )

    config = get_generate_content_config(
        temperature=0,
        thinking_level="HIGH",
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=pieces,
            client=client,
            config=config,
            model=model,
        )
        result = json.loads(response_text)
        general = result.get("general", "")
        front_details = result.get("front_details", "")
        back_details = result.get("back_details", "")
    except Exception as e:
        logger.error(f"Error describing garment: {e}")
        general = ""
        front_details = ""
        back_details = ""

    description = {
        "general": general,
        "front_details": front_details,
        "back_details": back_details,
    }
    logger.debug(f"[Fitting] Garment description: {description}")
    return description


def select_best_back(
    client,
    garment_images: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    category: str = "",
) -> dict:
    """Select up to 2 best back-view garment images from a list."""
    logger.debug(
        f"[Fitting] Selecting best back images from {len(garment_images)} candidates"
    )
    result = _select_best_view(
        client, garment_images, "back", model, category=category
    )
    logger.debug(f"[Fitting] Best back: indices={result.get('best_indices')}")
    for ev in result.get("evaluations", []):
        logger.debug(
            f"[Fitting]   IMAGE_{ev.get('index')}: angle={ev.get('angle')}, quality={ev.get('quality')}"
        )
    return result
