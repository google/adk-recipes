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

"""Utilities for R2V (Reference-to-Video) product spinning video generation."""

# Third-party imports
from google.genai import types

# Project imports
from workflows.shared.llm_utils import get_generate_content_config, get_part

# R2V Prompt Template for product spinning videos
VEO_R2V_PROMPT_TEMPLATE = """**[Subject]:** {{description}}

**[Action]:** The camera performs **one continuous, seamless, very fast 360-degree orbit** around the stationary product. The camera movement is perfectly smooth and steady, maintaining a constant distance and speed throughout the entire clip. The product does not move or rotate; only the camera moves.
**[Scene]:** A completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255). The only visible element is the product, nothing else.
"""


def generate_product_description(
    client, gemini_model: str, all_images_bytes: list
) -> str:
    """Generate a description of the product from images using Gemini.

    Generalized version for any product (not just footwear).

    Args:
        client: Gemini client instance
        gemini_model: Model name to use for generation
        all_images_bytes: List of image bytes to analyze

    Returns:
        str: Product description in the format "A [type] standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)""

    """
    system_prompt = """You are an expert in product analysis. Your role is to return a very short description of the product you see in the images.
Always return the description using the following template: A [type of product], standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)"
The type of product should only include the product type and primary color.
Avoid describing the product with the brand, even if it is clearly visible.

Example outputs:
- "A red ceramic mug standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)""
- "A silver smartwatch standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)""
- "A blue backpack standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)""
- "A white wireless headphones standing still in a completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255)""

Return ONLY the description, nothing else.
"""

    # Build parts using shared utility
    parts = [get_part("Return a description of this product: ")]
    for img_bytes in all_images_bytes:
        parts.append(get_part(img_bytes))

    config = get_generate_content_config(
        temperature=0,
        max_output_tokens=100,
    )
    config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    config.system_instruction = [get_part(system_prompt)]

    response = client.models.generate_content(
        model=gemini_model,
        contents=[types.Content(role="user", parts=parts)],
        config=config,
    )

    return response.text.strip()
