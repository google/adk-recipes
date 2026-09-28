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

"""Prompt generation for shoe spinning R2V using Gemini."""

import os

# Standard library imports
# Third-party imports
from jinja2 import Template

# Project imports
from workflows.shared.gemini import generate_gemini

VEO_R2V_PROMPT_TEMPLATE = """**[Subject]:** {{description}}

**[Action]:** The camera performs **one continuous, seamless, very fast 360-degree orbit** around the stationary product. The camera movement is perfectly smooth and steady, maintaining a constant distance and speed throughout the entire clip. The product does not move or rotate; only the camera moves.

**[Scene]:** A completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255). The only visible element is the product, nothing else.
"""


def generate_footwear_description(
    client,
    gemini_model: str = os.getenv("MODEL_NAME_GENERATED_1"),
    all_images_bytes: list[bytes] | None = None,
) -> str:
    """Generate a description of the footwear product from images.

    Args:
        client: Gemini client for generation
        gemini_model: Model to use for description generation
        all_images_bytes: List of image bytes to analyze

    Returns:
        str: Product description

    """
    system_prompt = """ You are an expert in footwear products. Your role is to return a very short description of the product you see in the images.
Always return the description using the following template: A [type of product], standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255).
The type of product should only include the product type such as sneaker, shoe, boot, sandal. etc and primary color.
Avoid describing the footwear with the brand, even if it is clearly visible.

Example outputs:
- "A black sneaker standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)"
- "A white sneaker shoe standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)"
- "A brown leather boot standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)"

Return ONLY the description, nothing else.
"""
    config = {
        "temperature": 0,
        "max_output_tokens": 100,
        "thinking_config": {"thinking_budget": 0},
        "system_instruction": system_prompt,
    }

    text_part = ["Return a description of this footwear product: "]
    return generate_gemini(
        text_images_pieces=(text_part + all_images_bytes),
        client=client,
        config=config,
        model=gemini_model,
    )


def generate_veo_prompt_r2v(
    client,
    gemini_model: str = os.getenv("MODEL_NAME_GENERATED_1"),
    all_images_bytes: list[bytes] | None = None,
) -> str:
    """Generate a Veo prompt for reference-to-video (R2V) generation.

    This function generates a product description using Gemini and combines it with
    a static action template for 360-degree product rotation.

    Args:
        client: Gemini client for description generation
        gemini_model: Model to use for description generation
        all_images_bytes: List of image bytes to analyze for description

    Returns:
        str: Complete Veo prompt with description and static action template

    """
    product_description = generate_footwear_description(
        client, gemini_model=gemini_model, all_images_bytes=all_images_bytes
    )

    veo_prompt = Template(VEO_R2V_PROMPT_TEMPLATE).render(
        {"description": product_description}
    )

    return veo_prompt
