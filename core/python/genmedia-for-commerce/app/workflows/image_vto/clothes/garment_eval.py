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

"""Garment evaluation for Image VTO.

Uses the product fitting's detailed evaluate_garment (0-10 scale with
EXTERIOR/INTERIOR awareness) instead of the old simple 0-3 scale.
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from workflows.product_enrichment.product_fitting.garment_eval import (
    evaluate_garment,
)
from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)


def evaluate_garments(
    client,
    generated_image_bytes: bytes,
    garment_images_bytes_list: list[bytes | str],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    garment_descriptions: list[dict] | None = None,
) -> dict:
    """Evaluate all reference garments against the generated VTO image in parallel.

    Uses the product fitting's evaluate_garment (0-10 scale) with
    EXTERIOR/INTERIOR detail awareness when descriptions are provided.

    Args:
        client: Gemini client instance
        generated_image_bytes: The generated VTO image as bytes
        garment_images_bytes_list: List of reference garment images as bytes
        model: Gemini model to use
        garment_descriptions: Optional list of garment descriptions from
            describe_all_garments(), each with "general" and "details" keys

    Returns:
        dict: {
            "discard": bool - True if any garment scored below 8,
            "garments_score": float - Average score normalized to 0-100,
            "garment_details": list[dict] - Individual garment evaluations
        }

    """
    if garment_descriptions is None:
        garment_descriptions = [{}] * len(garment_images_bytes_list)

    with ThreadPoolExecutor(
        max_workers=len(garment_images_bytes_list)
    ) as executor:
        futures = [
            executor.submit(
                evaluate_garment,
                client,
                generated_image_bytes,
                garment_bytes,
                model,
                view_details=desc.get("details", ""),
                garment_description=desc.get("general", ""),
            )
            for garment_bytes, desc in zip(
                garment_images_bytes_list, garment_descriptions, strict=False
            )
        ]
        garment_details = [f.result() for f in futures]

    # Average scores and normalize to 0-100 (scores are 0-10)
    avg_score = sum(detail["score"] for detail in garment_details) / len(
        garment_details
    )
    garments_score = (avg_score / 10) * 100

    # Discard if any garment scores below 5 (wrong color/pattern, missing garment, etc.)
    # VTO uses a lower threshold than product fitting (8) because it has a single
    # garment image per item and multiple garments, so logo precision is less strict.
    if any(detail["score"] < 5 for detail in garment_details):
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

    Args:
        client: Gemini client instance
        generated_image_bytes: The generated VTO image as bytes
        model: Gemini model to use

    Returns:
        dict: {"explanation": str, "score": int} where score is 0-3:
            0: unwearable — garment completely broken, folded in half, or major body parts missing
            1: poor — significant structural issues (large asymmetry, garment merging/melting, major clipping)
            2: acceptable — minor imperfections (small texture artifacts, slight asymmetry, minor AI artifacts)
            3: excellent — outfit looks natural and properly worn, indistinguishable from a real photo

    """
    system_prompt = """You are a quality control inspector for AI-generated fashion images. Your task is to evaluate whether the outfit in the image is worn naturally, realistically, and as a customer would expect to see the garments displayed.

**Check for these issues:**

STRUCTURAL ISSUES:
- Garments folded in half, bunched up severely, or collapsed
- Garments clipping through the body or merging/melting into other garments
- Parts of garments missing, cut off, or absorbed into other clothing
- Sleeves or pant legs that are missing or severely distorted

FIT AND DRAPING ISSUES (important):
- A garment that is partially tucked in when it should hang freely (e.g. a sweater or top that is half-tucked into pants on one side but hanging on the other)
- Asymmetric hemlines where one side of a garment is significantly higher or shorter than the other
- A garment bunched or gathered unnaturally at the waist, as if it has been stuffed into the waistband
- Garments that appear to be the wrong size or distorted compared to what a customer would expect

OVERALL:
- The outfit should look like something styled for an e-commerce product photo
- Each garment should be clearly visible and properly displayed

Score on a 0-3 scale:
- 0: Unwearable — garment is completely broken (folded in half, major body parts missing, garment not recognizable)
- 1: Poor — significant issues that are immediately noticeable (garment half-tucked when it should hang freely, large asymmetry in hemline, garment bunched into waistband, major clipping or merging)
- 2: Acceptable — minor imperfections that are noticeable on close inspection but the outfit generally looks fine (small texture artifacts, slight asymmetry, minor AI rendering issues like button or belt imperfections)
- 3: Excellent — outfit looks natural and properly worn, could pass as a real e-commerce photograph

Return your analysis as a valid JSON object with exactly two keys:
{
  "explanation": "A clear explanation of what issues were found or why the outfit looks good",
  "score": <0-3>
}

Only return the JSON object, nothing else."""

    user_prompt = "Evaluate the wearing quality of the outfit in this generated virtual try-on image."

    config = get_generate_content_config(
        temperature=0,
        max_output_tokens=300,
        thinking_budget=0,
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
