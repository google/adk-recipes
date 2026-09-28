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

"""Shoe classification utilities using Gemini or local embeddings."""

# Standard library imports
import json
import logging
import os

# Third-party imports
from pydantic import BaseModel, Field

# Project imports
from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config
from workflows.spinning.r2v.shoes.classify_shoes_local import (
    classify_shoe_local,
)

logger = logging.getLogger(__name__)


def _is_endpoint_available(model) -> bool:
    """Check if a remote shoe classification endpoint is configured."""
    return model is not None and str(model).strip().lower() != "none"


SHOE_CLASSIFICATION_SYSTEM_PROMPT = """You are an **expert in footwear and image understanding**. Your primary role is to **analyze the user-provided image** and **classify the position (viewpoint and orientation) of the footwear product** within it.

The product's position must be classified into one of the following distinct categories:
* **front:** The product is in a **perfect eye-level, front-facing position** relative to the camera (i.e., the toe is directly facing the camera).
* **front_right:** The product front and right end side are prominent and visible. Ie. It is possible to understand how both the right side and the front side of the product look like from the photo.
* **front_left:** The product front and left end side are prominent and visible. Ie. It is possible to understand how both the front side and the left side of the product look like from the photo.
* **top_front:** The product is in a **front-facing position** where the camera is clearly **above the product**, capturing a view from the top down (i.e. the top of the shoe is prominent).
* **back:** The product is in a **perfect back-facing position** relative to the camera (i.e., the **heel** is directly facing the camera).
* **back_right:** The product back and right end side are prominent and visible. Ie. It is possible to understand how both the back side and the right side of the product look like from the photo.
* **back_left:** The product back and left end side are prominent and visible. Ie. It is possible to understand how both the back side and the left side of the product look like from the photo.
* **right:** The product is **rotated to the right**. Both the toe and the heel are visible, and the **toe is on the right** side of the image relative to the heel.
* **left:** The product is **rotated to the left**. Both the toe and the heel are visible, and the **toe is on the left** side of the image relative to the heel.
* **sole:** The product is **upside down**, and the **sole** is the dominant feature, filling the majority of the image frame.
* **multiple:** The image contains **two or more** distinct footwear items (e.g., shoes, boots, sandals, etc.). This includes cases where the items are a matching pair, the items are unmatched and one or more item is partially obscured.
* **invalid:** Return `invalid` in all other cases including:
    * The image contains no footwear.
    * The image show snowshoes or snowsrackets (ski boot are considered valid)
    * The image show a shoe but the image is altered (for instance the sole is split into the inner parts) or it is a zoom-in detail of the shoe (in this cases one or more of the edges of the picture terminates with a straight line as the product is cut inside the picture)
    * A person is wearing the footwear (i.e., the footwear is on a human subject).
    * The image is a diagram or a table.
    * A shoe is present against a non-neutral background (for instance, the background is not plain white, black, or gray)
    * The shoe displays missing parts or components from an adjacent shoe. (This error results from poor image segmentation and the splitting of multiple shoes.)
"""


VALIDATION_SHOE_CLASSIFICATION_SYSTEM_PROMPT = """You are an **expert in footwear and image understanding**. Your primary role is to **analyze the user-provided image** and **classify the position (viewpoint and orientation) of the footwear product** within it.

The product's position must be classified into one of the following distinct categories:
* **front:** The product is in a **perfect eye-level, front-facing position** relative to the camera (i.e., the toe is directly facing the camera).
* **front_right:** The product front and right end side are prominent and visible. Ie. It is possible to understand how both the right side and the front side of the product look like from the photo.
* **front_left:** The product front and left end side are prominent and visible. Ie. It is possible to understand how both the front side and the left side of the product look like from the photo.
* **back:** The product is in a **perfect back-facing position** relative to the camera (i.e., the **heel** is directly facing the camera).
* **back_right:** The product back and right end side are prominent and visible. Ie. It is possible to understand how both the back side and the right side of the product look like from the photo.
* **back_left:** The product back and left end side are prominent and visible. Ie. It is possible to understand how both the back side and the left side of the product look like from the photo.
* **right:** The product is **rotated to the right**. Both the toe and the heel are visible, and the **toe is on the right** side of the image relative to the heel.
* **left:** The product is **rotated to the left**. Both the toe and the heel are visible, and the **toe is on the left** side of the image relative to the heel.
* **invalid:** Return `invalid` in all other cases including:
    * The image contains no footwear.
    * The image show snowshoes or snowsrackets (ski boot are considered valid)
    * The image show a shoe but the image is altered (for instance the sole is split into the inner parts) or it is a zoom-in detail of the shoe (in this cases one or more of the edges of the picture terminates with a straight line as the product is cut inside the picture)
    * A person is wearing the footwear (i.e., the footwear is on a human subject).
    * The image is a diagram or a table.
    * A shoe is present against a non-neutral background (for instance, the background is not plain white, black, or gray)
    * The shoe displays missing parts or components from an adjacent shoe. (This error results from poor image segmentation and the splitting of multiple shoes.)
"""

VELCRO_SYSTEM_PROMPT = """ You are an expert in product analysis and fastening mechanisms.
Your task is to carefully examine product images to determine if they contain velcro or hook-and-loop fasteners.
Look for:
- Velcro straps or patches
- Hook-and-loop closure systems
- Fuzzy/rough textured fastener strips
- Adjustable straps with velcro closures

Be thorough in your analysis and provide clear reasoning for your determination.
Beaware that some products might use side-release buckle or a pin-buckle fastening this is not a velcro or strapped to be flagged.
"""

VELCRO_USER_PROMPT = """
Please analyze the provided product images and determine whether this product has velcro or hook-and-loop fasteners.
Examine all images carefully, looking at different angles, parts and details of the product.
Provide your answer in the structured format requested.
"""


class VelcroDetectionResult(BaseModel):
    """Schema for velcro/hook-and-loop detection result."""

    has_velcro: bool = Field(
        description="Whether the product has velcro or hook-and-loop fasteners. Return true if velcro/hook-and-loop is visible or clearly present, otherwise false."
    )
    explanation: str = Field(
        description="Brief explanation of why velcro was detected or not detected (1 sentence)."
    )


def classify_shoe(image, client, model, mode="normal"):
    """Classify shoe position using remote endpoint or local embeddings fallback."""
    if not _is_endpoint_available(model):
        logger.info(
            "SHOE_CLASSIFICATION_ENDPOINT not set, using local classifier"
        )
        return classify_shoe_local(image, client)

    if mode == "normal":
        system_instructions = SHOE_CLASSIFICATION_SYSTEM_PROMPT
    elif mode == "validation":
        system_instructions = VALIDATION_SHOE_CLASSIFICATION_SYSTEM_PROMPT

    config = get_generate_content_config(
        temperature=0, system_instruction=system_instructions, thinking_budget=0
    )

    return generate_gemini(
        text_images_pieces=["classify this product:", image],
        client=client,
        model=model,
        config=config,
    )


def classify_shoe_closure(
    image_bytes_list: list[bytes],
    client,
    model=os.getenv("MODEL_NAME_GENERATED_2", "gemini-3.6-flash"),  # noqa: B008
):
    """Detect velcro/hook-and-loop fasteners in shoe images."""
    config = get_generate_content_config(
        temperature=0,
        system_instruction=VELCRO_SYSTEM_PROMPT,
        thinking_budget=0,
        response_mime_type="application/json",
        response_schema=VelcroDetectionResult.model_json_schema(),
    )

    # Build input: user prompt followed by all images
    text_images_pieces = [VELCRO_USER_PROMPT, *list(image_bytes_list)]

    result = generate_gemini(
        text_images_pieces=text_images_pieces,
        client=client,
        model=model,
        config=config,
    )

    # Validate result
    if result is None or result == "invalid":
        raise Exception(
            "Velcro classification failed after multiple retries. "
            "The classification service may be temporarily unavailable. Please try again later."
        )

    return json.loads(result)
