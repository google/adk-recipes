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

"""Garment evaluation for Image VTO using Gemini vision.

Compares reference garment images against generated VTO images to assess accuracy.
"""

import json
import logging
import os

from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)


def evaluate_garment(
    client,
    generated_image_bytes: bytes,
    reference_garment_bytes: bytes | list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    view_details: str = "",
    garment_description: str = "",
) -> dict:
    """Evaluate how well a single reference garment was reproduced in the generated product fitting image.

    Args:
        client: Gemini client instance
        generated_image_bytes: The generated image as bytes
        reference_garment_bytes: The reference garment image(s) as bytes — single image or list of images showing different angles/views of the same garment
        model: Gemini model to use
        view_details: Pre-extracted description of visible logos/text/marks on the garment
        garment_description: Brand/product description (e.g. "Lenz sportswear black ski jacket")

    Returns:
        dict: {"explanation": str, "score": int} where score is 0-10

    """
    # Normalize to list
    if isinstance(reference_garment_bytes, bytes):
        reference_images = [reference_garment_bytes]
    else:
        reference_images = reference_garment_bytes
    brand_block = ""
    if garment_description:
        brand_block = f"""

**GARMENT IDENTIFICATION:**
{garment_description}
Use this ONLY to correct text/brand name misreads in the known details below. The details were extracted automatically and may contain OCR errors for tonal or hard-to-read text. If the details say the text is "LEAF" but the brand is "LENZ", then "LENZ" is the correct text — treat the generated image showing "LENZ" as a PASS for that text detail.
This brand context does NOT change the visual verification rules. You must still physically see each detail in the generated image — do NOT assume logos or tags are present just because you know the brand."""

    details_block = ""
    if view_details:
        details_block = f"""
{brand_block}
**KNOWN DETAILS ON THIS GARMENT (from prior analysis):**
{view_details}

Details may be classified as [EXTERIOR] or [INTERIOR]:
- [EXTERIOR] details are part of the garment's outward-facing design — these MUST be reproduced and are STRICT PASS/FAIL.
- [INTERIOR] details are elements on the inner-facing side of the garment — interior linings, inner fabric colors, interior branding/text, sewn-in tags, care labels, size labels, chamois pads. These would be hidden when worn and should NOT be penalized if missing. However, if an [INTERIOR] detail IS visible in the generated image (e.g. interior lining color showing through, interior branding text visible on a worn garment), that is a flaw — score MUST be 6 or below.

For each [EXTERIOR] detail, you MUST verify it against the generated image using this strict process:
1. **LOCATE**: Can you point to a specific, clearly visible element in the generated image that corresponds to this detail? Describe exactly what you see (shape, color, position). If you cannot identify a specific visible element, it is ABSENT — do NOT assume it exists.
2. **POSITION**: Is it in the CORRECT POSITION as described? (e.g. "lower part of strap" must be on the lower part, not the upper part = FAIL if wrong position)
3. **SIDE**: Is it on the CORRECT SIDE as described? (e.g. "left leg" must be on the left leg, not the right = FAIL if wrong side)
4. **VERDICT**: PASS or FAIL for this detail.

For each [INTERIOR] detail, you MUST check whether it is VISIBLE on the worn garment:
1. **CHECK VISIBILITY**: Look at the generated image carefully. Can you see this interior element (lining color, interior text, tags, chamois pad) on the model wearing the garment?
2. **VERDICT**: If the interior element IS visible on the worn garment → FAIL (this is unrealistic — interior elements should be hidden when worn). If it is NOT visible → PASS (correctly hidden).
3. **IMPORTANT**: The reference image may show interior elements because it is a flat-lay/product-only photo where the garment is laid open. This does NOT mean interior elements should be visible when worn. A grey quilted lining visible in a flat-lay reference must NOT appear on the collar/neckline of the worn garment.

**ANTI-HALLUCINATION WARNING**: Small logos, tags, and patches are frequently MISSING in generated images. Do NOT assume they are present just because the rest of the garment looks good. If you cannot clearly see a distinct element (a visible patch, text, or mark) at the expected location, mark it as ABSENT. A blank/plain area of fabric where a logo should be = ABSENT.

Any FAILED [EXTERIOR] item counts as a missing or incorrect logo/detail for scoring purposes.
Any FAILED [INTERIOR] item (interior element incorrectly visible on the worn garment) → score MUST be 6 or below. Interior elements showing through on a worn garment is a serious flaw that makes the image look AI-generated and unrealistic."""

    system_prompt = (
        """You are an expert fashion image evaluator. Your task is to compare a reference garment image against a generated product fitting image and evaluate how accurately the garment was reproduced.
"""
        + details_block
        + """
Pay close attention to ALL of these details:
- Color and fabric texture
- Pattern (stripes, checks, prints, etc.)
- Shape and silhouette
- Construction details (seams, stitching, panels)
- **Exterior-facing logos, brand marks, prints, and emblems** — if the reference has a visible exterior logo or brand mark and it is missing or incorrect in the generated image, this is a significant flaw

**IMPORTANT — INTERIOR TAGS vs. EXTERIOR BRANDING:**
Distinguish between these two types of markings:
- **Exterior branding**: Logos, prints, or brand marks that are part of the garment's outward design (e.g. a Nike swoosh on the chest, "ADIDAS" printed on a leg, a patch sewn on the front). These MUST be reproduced.
- **Interior elements**: Any element on the inner-facing side of the garment — interior linings, inner fabric colors, interior branding/text, sewn-in tags, care labels, size labels, chamois pads. These are only visible in flat-lay or product-only reference photos because the garment is laid open. When the garment is worn by a model, interior elements are naturally hidden by the wearer's body. Do NOT penalize for missing interior elements — they would not be visible in real photography either.

How to tell the difference: Interior elements are on the inside surface of the garment and would be concealed when worn. Exterior branding is designed to be visible when worn.

**INTERIOR ELEMENTS VISIBLE WHEN WORN:**
If the generated image shows an interior element (e.g. interior lining color, interior branding text, a neckline brand tag, care label) that is clearly visible on the model when it should be hidden inside the garment, this is a flaw. Interior elements showing on a worn garment look unrealistic and should be penalized — score MUST be 6 or below.

**IMPORTANT — PERSPECTIVE AND VIEWING ANGLE:**
The reference image(s) and the generated image may show the garment from different angles. Consider that:
- The reference may not be a perfect front/back shot — it could be slightly angled, showing more text or detail than would be visible from straight on.
- Text or logos that wrap around the garment (e.g. on a leg or sleeve) will only be partially visible from a straight front or back view. If the reference shows "SCOTT" but the generated image shows "SCO" or "SCOT" because of the viewing angle, that is correct — do NOT penalize.
- Only penalize for details that should clearly be visible from the generated image's angle but are completely absent or wrong.

**MULTIPLE REFERENCE IMAGES:**
You may receive multiple reference images of the SAME garment from different angles or perspectives. If so:
- Use ALL reference images together to understand the garment's full design.
- First, determine which reference image the generated image most closely matches in terms of viewing angle and perspective.
- Only require details that would be visible from that specific perspective. Details visible in one reference angle but not in the angle the generated image chose are NOT required.
- Do NOT penalize the generated image for missing details that are only visible from a different angle than the one it depicts.
- DO penalize if the generated image adds features (e.g. stripes, panels, logos) that don't exist in ANY of the reference images. However, if a detail from the checklist IS present on the correct side AND also appears duplicated on an additional side not mentioned in the checklist, treat this as duplication (score cap 7), not as a missing detail (score cap 6).

Score the reproduction on a 0-10 scale:
- 0: The garment is completely missing or the original clothing was not replaced at all
- 1-2: The garment is barely recognizable (completely wrong color AND pattern AND shape)
- 3-4: Major imperfections (wrong color, wrong pattern, or significantly distorted shape)
- 5-6: Notable flaws — completely missing an exterior-facing logo that was clearly visible in the reference, or wrong color/pattern
- 7: Good reproduction — color and overall design are correct, but with noticeable construction differences (missing or extra seam panels, different cuff style)
- 8: Very good — correct color, pattern, shape, and exterior logos present, with only minor differences (slight texture variation, minor seam placement, partial logo due to angle)
- 9: Near-perfect — all details accurately reproduced with negligible differences
- 10: Perfect reproduction — indistinguishable from the reference in every detail

**HARD RULES — these override everything above:**
- If the reference clearly shows an EXTERIOR-FACING logo (part of the garment's outward design) and it is COMPLETELY ABSENT in the generated image → score MUST be 6 or below
- Interior tags/labels (e.g. brand tags sewn inside the neckline, care labels) do NOT trigger this rule — missing interior tags should NOT limit the score
- If an exterior logo or detail is present but in the WRONG POSITION (e.g. described as "lower part" but appears on upper part, or on wrong leg/strap) → score MUST be 6 or below
- If logos or details are DUPLICATED (e.g. reference shows 2 logos but generated image shows 4) → score MUST be 7 or below
- If a logo is present but partially visible due to viewing angle or perspective, that is acceptable — do NOT penalize
- If an INTERIOR element (interior lining, interior branding, tags, care labels) is clearly visible on the worn garment when it should be hidden inside → score MUST be 6 or below
- Construction details (seams, panels, stitching) are LESS important than color, pattern, and logos — missing seams alone should NOT drop the score below 7 if color, pattern, and logos are all correct

Return your analysis as a valid JSON object with exactly two keys:
{
  "explanation": "For each known detail from the checklist, state what you see (or don't see) in the generated image and whether it PASSES or FAILS. Then give your overall assessment.",
  "score": <0-10>
}

Only return the JSON object, nothing else."""
    )

    if len(reference_images) == 1:
        user_prompt = "Here is the reference garment image followed by the generated virtual try-on image. Evaluate how well the garment was reproduced in the try-on image."
    else:
        user_prompt = f"Here are {len(reference_images)} reference images of the SAME garment (showing different angles or details), followed by the generated virtual try-on image. Use ALL reference images together to evaluate how well the garment was reproduced — but only require details visible from the generated image's specific angle."

    config = get_generate_content_config(
        temperature=0,
        thinking_level="HIGH",
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    # Build pieces: all reference images + generated image + prompt
    pieces = []
    for i, ref_img in enumerate(reference_images):
        if len(reference_images) > 1:
            pieces.append(f"REFERENCE IMAGE {i + 1}:")
        pieces.append(ref_img)
    pieces.append("GENERATED IMAGE:")
    pieces.append(generated_image_bytes)
    pieces.append(user_prompt)

    try:
        response_text = generate_gemini(
            text_images_pieces=pieces,
            client=client,
            config=config,
            model=model,
        )

        result = json.loads(response_text)

        if "explanation" not in result or "score" not in result:
            raise ValueError("Response missing required keys")

        result["score"] = max(0, min(10, int(result["score"])))
        return result

    except Exception as e:
        logger.error(f"Error during garment evaluation: {e}")
        return {
            "explanation": f"Error during garment evaluation: {e!s}",
            "score": 0,
        }


def evaluate_footwear(
    client,
    generated_image_bytes: bytes,
    reference_images: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    garment_description: str = "",
) -> dict:
    """Evaluate how well footwear was reproduced in a generated fitting image.

    Simpler and less strict than clothing eval — focuses on whether the shoe
    looks like the same product (shape, color, key design elements).

    Returns:
        dict: {
            "discard": bool,
            "garments_score": float (0-100),
            "garment_details": [{"explanation": str, "score": int}]
        }

    """
    num_refs = len(reference_images)

    description_block = ""
    if garment_description:
        description_block = f"""
**KNOWN PRODUCT DETAILS (from prior analysis of the reference images):**
{garment_description}

Use these details as a checklist — verify each key [EXTERIOR] feature is present in the generated image's shoes. If a prominent feature from this list is clearly missing, it MUST lower the score. However, features not visible from the generated image's viewing angle should NOT be penalized.
"""

    system_prompt = f"""You are a product image evaluator for footwear. You will receive {num_refs} reference image(s) of a shoe/boot/sandal, followed by a generated image showing a model wearing that footwear.

Your job is to assess TWO things:

**A) PRODUCT MATCH** — Does the generated footwear look like the SAME product as the reference?
{description_block}
1. **Overall shape and silhouette** — Does the shoe have the same general shape? (e.g. low-top vs high-top, chunky vs slim, boot vs sneaker)
2. **Color scheme** — Are the main colors correct? Minor shade differences are acceptable.
3. **Key design elements** — Are the major visual features preserved? (e.g. stripe pattern, sole color, lace style, distinctive panels)

What NOT to be strict about:
- The generated image shows the shoe on a foot from a potentially different angle than the reference — some details may not be visible and that's fine.
- Minor texture or material differences are acceptable.
- Small logos or brand text may be missing or slightly different — only penalize if a large, prominent logo/design element is completely wrong or missing.
- The reference may show the shoe from multiple angles — the generated image only needs to match from whatever angle it shows.

**B) IMAGE QUALITY** — Does the image look natural or does it have obvious AI artifacts?

Check for:
- Distorted or melted shoe shapes
- Extra or missing feet/toes/legs
- Shoes clipping through the ground or floating
- Obvious AI glitches (warped textures, impossible geometry)
- Unnatural foot/ankle proportions

Do NOT penalize for:
- The rest of the outfit (clothing choices, fit, style) — only evaluate the footwear area
- Minor rendering differences that wouldn't be noticed at a glance

**ANTI-HALLUCINATION — CRITICAL:**
You MUST physically look at the GENERATED IMAGE ONLY (the first, full-body image) when verifying features. Do NOT assume features exist just because they appear in the reference images or the product details checklist. AI-generated images frequently MISS prominent design elements like stripes, logos, and color panels. For each key feature, ask yourself: "Can I actually SEE this specific element on the shoes in the generated image?" If you cannot clearly see it, it is MISSING — score accordingly.

Score on a 0-10 scale:
- 0-3: Wrong shoe entirely OR major AI glitches (distorted shape, extra limbs)
- 4-5: Same type but significant product differences (e.g. missing prominent stripes/patterns), OR noticeable AI artifacts
- 6-7: Recognizably the same shoe with some notable differences or minor artifacts
- 8-9: Good match, natural looking — correct shape, colors, and main design elements all clearly visible
- 10: Near-perfect match, looks like a real photo

Return a JSON object:
{{
  "explanation": "For each key feature from the checklist, state whether you can SEE it in the generated image or not. Then give your overall assessment.",
  "score": <0-10>
}}

Only return the JSON object, nothing else."""

    pieces = []
    pieces.append(
        "GENERATED IMAGE (this is the AI-generated image you must evaluate — it shows a full-body model wearing the shoes, look at the feet):"
    )
    pieces.append(generated_image_bytes)
    pieces.append(
        f"REFERENCE IMAGES ({num_refs} product photos of the original shoe — compare the generated shoes above against these):"
    )
    for i, ref_img in enumerate(reference_images):
        if num_refs > 1:
            pieces.append(f"REFERENCE {i + 1}:")
        pieces.append(ref_img)
    pieces.append(
        "IMPORTANT: The FIRST image above is the generated one (full-body model shot). All subsequent images are references. Zoom into the FEET area of the first image and compare the shoes there against the reference product photos. Do NOT confuse the reference images with the generated image."
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
        if "explanation" not in result or "score" not in result:
            raise ValueError("Response missing required keys")
        result["score"] = max(0, min(10, int(result["score"])))
    except Exception as e:
        logger.error(f"Error during footwear evaluation: {e}")
        result = {
            "explanation": f"Error during footwear evaluation: {e!s}",
            "score": 0,
        }

    garment_details = [result]
    score = result["score"]
    garments_score = (score / 10) * 100

    return {
        "discard": score < 7,
        "garments_score": garments_score,
        "garment_details": garment_details,
    }


def evaluate_garments(
    client,
    generated_image_bytes: bytes,
    garment_images_bytes_list: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    view_details: str = "",
    garment_description: str = "",
) -> dict:
    """Evaluate reference garment images against the generated product fitting image.

    All images in garment_images_bytes_list are treated as different views/angles
    of the SAME garment and passed together to a single evaluation call.

    Returns:
        dict: {
            "discard": bool - True if garment scored below threshold,
            "garments_score": float - Score normalized to 0-100,
            "garment_details": list[dict] - Garment evaluation (single entry)
        }

    """
    # Keep [INTERIOR] details so eval can penalize if they are incorrectly visible in the generated image

    # Pass all reference images together as multiple views of the same garment
    garment_result = evaluate_garment(
        client,
        generated_image_bytes,
        garment_images_bytes_list,
        model,
        view_details,
        garment_description,
    )
    garment_details = [garment_result]

    # Average scores and normalize to 0-100
    avg_score = sum(detail["score"] for detail in garment_details) / len(
        garment_details
    )
    garments_score = (avg_score / 10) * 100

    # Discard if any garment scores below 7
    if any(detail["score"] < 7 for detail in garment_details):
        return {
            "discard": True,
            "garments_score": garments_score,
            "garment_details": garment_details,
        }

    return {
        "discard": False,
        "garments_score": garments_score,
        "garment_details": garment_details,
    }


def evaluate_wearing_quality(
    client,
    generated_image_bytes: bytes,
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> dict:
    """Evaluate the wearing quality of a generated VTO image.

    No reference garment needed — only assesses how naturally the outfit is worn.

    Returns:
        dict: {"explanation": str, "score": int} where score is 0-3

    """
    system_prompt = """You are a quality control inspector for AI-generated fashion images. Your task is to evaluate ONLY whether the outfit is worn naturally and realistically — how the garments sit on the body.

**IMPORTANT — SCOPE OF THIS EVALUATION:**
You are ONLY evaluating how the garments are WORN and DISPLAYED on the model. You are NOT evaluating garment accuracy, fidelity, or consistency with any reference. Specifically, do NOT comment on or penalize:
- Misspelled or distorted branding/logos/text
- Wrong colors, patterns, or design details
- Missing or extra logos, patches, or decorative elements
- Asymmetric pockets, seams, or construction details
- Any difference between the garment and a reference image
These are handled by a separate garment fidelity evaluation. Your job is ONLY about fit, draping, and structural integrity.

**Check for these issues:**

STRUCTURAL ISSUES:
- Garments folded in half, bunched up severely, or collapsed
- Garments clipping through the body or merging/melting into other garments
- Parts of garments missing, cut off, or absorbed into other clothing
- Sleeves or pant legs that are missing or severely distorted
- Obvious AI rendering errors: melted/fused areas, impossible geometry, strange objects or protrusions that shouldn't exist (e.g. loops, cords, or straps appearing from nowhere)

FIT AND DRAPING ISSUES (important):
- A garment that is partially tucked in when it should hang freely (e.g. a sweater or top that is half-tucked into pants on one side but hanging on the other)
- Asymmetric hemlines where one side of a garment is significantly higher or shorter than the other
- A garment bunched or gathered unnaturally at the waist, as if it has been stuffed into the waistband
- Garments that appear to be the wrong size or distorted compared to what a customer would expect

PRODUCT VISIBILITY (critical):
- This is a product fitting image — the product being showcased must be 100% visible, with no part hidden, covered, or obscured by other clothing
- No other garment may overlap or hide any part of the product
- The product may be layered on top of other garments for visibility — this is intentional and correct. Do NOT apply real-world wearing conventions (e.g. cycling bib straps under a jersey). The goal is to show the product clearly, not to dress the model as they would in real life.
- Bare feet are not acceptable — shoes must be worn (unless the image is cropped above the feet)

OVERALL:
- The outfit should look like something styled for an e-commerce product photo
- Each garment should be clearly visible and properly displayed

Score on a 0-3 scale:
- 0: Unwearable — garment is completely broken (folded in half, major body parts missing, garment not recognizable), OR the product is significantly hidden/covered by other clothing
- 1: Poor — significant issues that are immediately noticeable (garment half-tucked when it should hang freely, large asymmetry in hemline, garment bunched into waistband, major clipping or merging, part of the product obscured by other garments, obvious AI rendering errors like strange protrusions or melted areas)
- 2: Acceptable — minor imperfections that are noticeable on close inspection but the outfit generally looks fine (small texture artifacts, slight asymmetry, minor AI rendering issues like button or belt imperfections)
- 3: Excellent — outfit looks natural and properly worn, could pass as a real e-commerce photograph

Return your analysis as a valid JSON object with exactly two keys:
{
  "explanation": "A clear explanation of what issues were found or why the outfit looks good",
  "score": <0-3>
}

Only return the JSON object, nothing else."""

    user_prompt = "Evaluate the wearing quality of the outfit in this generated product fitting image."

    config = get_generate_content_config(
        temperature=0,
        thinking_level="HIGH",
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=[generated_image_bytes, user_prompt],
            client=client,
            config=config,
            model=model,
        )

        result = json.loads(response_text)

        if "explanation" not in result or "score" not in result:
            raise ValueError("Response missing required keys")

        result["score"] = max(0, min(3, int(result["score"])))
        return result

    except Exception as e:
        logger.error(f"Error during wearing quality evaluation: {e}")
        return {
            "explanation": f"Error during wearing quality evaluation: {e!s}",
            "score": 0,
        }


def rank_fitting_variations(
    client,
    variations: list[tuple[int, bytes]],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> list[dict]:
    """Rank all non-discarded fitting variations for a single view by comparing them side-by-side.

    Args:
        client: Gemini client instance
        variations: List of (original_index, image_bytes) tuples for non-discarded results
        model: Gemini model to use

    Returns:
        list[dict]: Ranked list from best to worst, each entry:
            {"index": int, "rank": int, "realism_score": int, "styling_score": int,
             "overall_score": float, "explanation": str}

    """
    if len(variations) <= 1:
        if variations:
            idx, _ = variations[0]
            return [
                {
                    "index": idx,
                    "rank": 1,
                    "realism_score": 10,
                    "styling_score": 10,
                    "overall_score": 10.0,
                    "explanation": "Only one variation — no comparison needed.",
                }
            ]
        return []

    num = len(variations)
    index_labels = {i: variations[i][0] for i in range(num)}

    system_prompt = f"""You are an expert fashion photography judge. You will see {num} product fitting images labeled IMAGE_0, IMAGE_1, etc. All show the same product on the same model — your job is to RANK them from best to worst.

Evaluate each image on two criteria (score each 1-10):

1. **REALISM** (1-10): How real and natural does the image look?
   - Does the product look like it's genuinely being worn (natural drape, wrinkles, fabric tension)?
   - Does the lighting on the product match the scene?
   - Are there any AI artifacts, distortions, or unnatural elements?
   - Does the person look natural (proportions, skin, pose)?

2. **PRODUCT VISIBILITY** (1-10): How well does the product stand out in the image?
   - The product is the star — it must be clearly visible and distinguishable from the rest of the outfit.
   - Prefer complementary garments that contrast with the product's color (e.g. if the product is dark/black, a lighter-colored top makes the product stand out better).
   - The product should not blend into or be confused with other garments.
   - Is every part of the product fully visible and not hidden by other clothing?

Return a JSON object with rankings from best to worst:
{{
  "rankings": [
    {{"image_index": 0, "realism_score": 8, "styling_score": 7, "explanation": "brief reason"}},
    ...
  ]
}}

Order the rankings array from BEST to WORST. Only return the JSON object, nothing else."""

    pieces: list = []
    for i, (_, img_bytes) in enumerate(variations):
        pieces.append(f"IMAGE_{i}:")
        pieces.append(img_bytes)
    pieces.append("Compare all images and rank them from best to worst.")

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

        # Try parsing; if the model produces trailing commas, strip them
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            import re

            cleaned = re.sub(r",\s*([}\]])", r"\1", response_text)
            result = json.loads(cleaned)
        rankings = result.get("rankings", [])

        ranked = []
        for rank_pos, entry in enumerate(rankings):
            img_idx = entry.get("image_index", 0)
            if img_idx < 0 or img_idx >= num:
                continue
            realism = max(1, min(10, int(entry.get("realism_score", 5))))
            styling = max(1, min(10, int(entry.get("styling_score", 5))))
            overall = (realism + styling) / 2.0
            ranked.append(
                {
                    "index": index_labels[img_idx],
                    "rank": rank_pos + 1,
                    "realism_score": realism,
                    "styling_score": styling,
                    "overall_score": overall,
                    "explanation": entry.get("explanation", ""),
                }
            )

        # Fill in any variations that weren't ranked by the model
        ranked_indices = {r["index"] for r in ranked}
        for i in range(num):
            orig_idx = index_labels[i]
            if orig_idx not in ranked_indices:
                ranked.append(
                    {
                        "index": orig_idx,
                        "rank": len(ranked) + 1,
                        "realism_score": 5,
                        "styling_score": 5,
                        "overall_score": 5.0,
                        "explanation": "Not ranked by evaluator.",
                    }
                )

        return ranked

    except Exception as e:
        logger.error(f"Error during variation ranking: {e}")
        return [
            {
                "index": index_labels[i],
                "rank": i + 1,
                "realism_score": 5,
                "styling_score": 5,
                "overall_score": 5.0,
                "explanation": f"Ranking error: {e!s}",
            }
            for i in range(num)
        ]
