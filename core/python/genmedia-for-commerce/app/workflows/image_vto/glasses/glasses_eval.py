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

"""Glasses evaluation for Image VTO using Gemini vision.

Compares reference glasses images against generated VTO images to assess
frame fidelity (size, shape, color, lens properties).
"""

import json
import logging
import os

from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)


def evaluate_glasses(
    client,
    generated_image_bytes: bytes,
    reference_glasses_bytes: bytes,
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> dict:
    """Evaluate how faithfully the glasses in the generated image match the reference.

    Args:
        client: Gemini client instance
        generated_image_bytes: The generated VTO image as bytes
        reference_glasses_bytes: The reference glasses product image as bytes
        model: Gemini model to use

    Returns:
        dict: {
            "explanation": str,
            "size": int (0-10),
            "shape": int (0-10),
            "color": int (0-10),
            "lenses": int (0-10),
            "score": float (0-100, weighted average),
        }

    """
    system_prompt = """You are an expert eyewear quality inspector. Your task is to compare a reference glasses product image against a generated virtual try-on image and evaluate how accurately the glasses were reproduced.

Score EACH attribute independently on a 0-10 scale (integers only):
- 0: Completely wrong or missing
- 1-3: Major issues (clearly wrong)
- 4-5: Noticeable differences
- 6-7: Close but with minor issues
- 8-9: Very good with only subtle differences
- 10: Perfect match

Attributes to score:
1. **size**: Are the glasses correctly proportioned to the face? Frame width should roughly match face width at the cheekbones. Not oversized, not undersized.
2. **shape**: Does the frame shape match the reference? Cat-eye must stay cat-eye, round must stay round, aviator must stay aviator, etc. The silhouette must match.
3. **color**: Does the frame color match the reference exactly? Green must stay green, black must stay black, tortoiseshell must stay tortoiseshell.
4. **lenses**: Do the lenses match the reference? Check tint color, opacity/darkness level, and reflectivity. Dark sunglasses must stay dark, clear lenses must stay clear, colored tints must match.

BE STRICT. A score of 10 means pixel-perfect. Most good reproductions should score 7-8. Reserve 9-10 for truly flawless matches. Common deductions:
- Slightly different shade of frame color: -2 to -3
- Frame shape approximately right but not exact silhouette: -2 to -3
- Lens tint slightly off: -1 to -2
- Size slightly too large or small: -1 to -3
- Brand text/logo missing or wrong: -1

Return your analysis as a valid JSON object:
{
  "explanation": "Brief assessment noting specific differences",
  "size": <0-10>,
  "shape": <0-10>,
  "color": <0-10>,
  "lenses": <0-10>
}

Only return the JSON object, nothing else."""

    user_prompt = "First image: reference glasses product. Second image: generated virtual try-on. Evaluate how well the glasses match."

    config = get_generate_content_config(
        temperature=0,
        max_output_tokens=400,
        thinking_budget=0,
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=[
                reference_glasses_bytes,
                generated_image_bytes,
                user_prompt,
            ],
            client=client,
            config=config,
            model=model,
        )

        result = json.loads(response_text)

        for key in ("size", "shape", "color", "lenses"):
            if key not in result:
                raise ValueError(f"Response missing required key: {key}")
            result[key] = max(0, min(10, int(result[key])))

        # Weighted average → 0-100 score
        result["score"] = (
            result["size"] * 0.2
            + result["shape"] * 0.3
            + result["color"] * 0.25
            + result["lenses"] * 0.25
        ) * 10

        return result

    except Exception as e:
        logger.error(f"Error during glasses evaluation: {e}")
        return {
            "explanation": f"Error: {e!s}",
            "size": 0,
            "shape": 0,
            "color": 0,
            "lenses": 0,
            "score": 0.0,
        }


def evaluate_all_glasses(
    client,
    generated_image_bytes: bytes,
    glasses_images_bytes_list: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> dict:
    """Evaluate all reference glasses against the generated VTO image.

    Uses the first glasses image as the primary reference.

    Returns:
        dict: {
            "discard": bool - True if score is 0,
            "glasses_score": float - Score normalized to 0-100,
            "glasses_details": dict - Detailed evaluation
        }

    """
    # Use the first glasses image as the primary reference
    primary_glasses = glasses_images_bytes_list[0]
    details = evaluate_glasses(
        client, generated_image_bytes, primary_glasses, model
    )

    glasses_score = details["score"]

    # Discard if any attribute scored 0 (completely wrong/missing)
    any_zero = any(
        details.get(k, 0) == 0 for k in ("size", "shape", "color", "lenses")
    )
    if any_zero:
        return {
            "discard": True,
            "glasses_score": glasses_score,
            "glasses_details": details,
        }

    return {
        "discard": False,
        "glasses_score": glasses_score,
        "glasses_details": details,
    }
