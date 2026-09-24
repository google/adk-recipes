# Copyright 2026 Google LLC
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

"""ADK agent and tools for virtual try-on.

Exposes a :data:`root_agent` (and :data:`app`) that wraps the image and video
try-on tools.
"""

import logging

from google.adk import agents, apps, models

from scripts.config import config
from scripts.tryon_processor import generate_tryon_image, generate_tryon_video

logger = logging.getLogger(__name__)


def try_on_product_image(
    product_id: str,
    user_photo_path: str,
    product_image_path: str,
    product_category: str = "clothing",
    product_description: str = "",
) -> dict:
    """Generate a high-fidelity image showing the product tried on by the user.

    Args:
        product_id: The ID of the product.
        user_photo_path: Local path or gs:// URI to the user's uploaded portrait image.
        product_image_path: Local path or gs:// URI of the product image.
        product_category: The category of the product (e.g. clothing, eyewear, jewelry, shoes).
        product_description: Short text description of the product (colors, style).

    Returns:
        A dictionary with "status" ("success" or "error"), "output_uri" (if bucket configured) or
        "output_path" (if saved locally), and "model_used".
    """
    if not config.GOOGLE_CLOUD_PROJECT:
        return {
            "status": "error",
            "error": "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in.",
        }

    logger.info(
        "try_on_product_image: product_id=%s category=%s",
        product_id,
        product_category,
    )
    try:
        res = generate_tryon_image(
            person_image_path=user_photo_path,
            product_image_path=product_image_path,
            project_id=config.GOOGLE_CLOUD_PROJECT,
            output_bucket=config.TRYON_OUTPUT_BUCKET or None,
            model_name=config.GEMINI_IMAGE_MODEL,
            product_category=product_category,
            product_description=product_description,
        )
        return {"status": "success", "product_id": product_id, **res}
    except Exception as e:
        logger.exception(
            "try_on_product_image failed for product_id=%s", product_id
        )
        return {"status": "error", "product_id": product_id, "error": str(e)}


def try_on_product_video(
    product_id: str,
    user_photo_path: str,
    product_image_path: str,
    product_category: str = "clothing",
    product_description: str = "",
    scene_description: str = "a minimalist fashion studio catwalk setting",
) -> dict:
    """Generate a short video catwalk animation of the user trying on the product.

    Args:
        product_id: The ID of the product.
        user_photo_path: Local path or gs:// URI to the user's uploaded portrait image.
        product_image_path: Local path or gs:// URI of the product image.
        product_category: The category of the product (e.g. clothing, eyewear, jewelry, shoes).
        product_description: Short text description of the product.
        scene_description: Optional description of the catwalk walk setting.

    Returns:
        A dictionary with "status" ("success" or "error"), "output_uri" (if bucket configured) or
        "output_path" (if saved locally) of the generated catwalk video.
    """
    if not config.GOOGLE_CLOUD_PROJECT:
        return {
            "status": "error",
            "error": "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in.",
        }

    logger.info(
        "try_on_product_video: product_id=%s category=%s",
        product_id,
        product_category,
    )
    try:
        # Step 1: Generate the try-on image composite
        img_res = generate_tryon_image(
            person_image_path=user_photo_path,
            product_image_path=product_image_path,
            project_id=config.GOOGLE_CLOUD_PROJECT,
            output_bucket=None,  # Keep temporary on disk
            model_name=config.GEMINI_IMAGE_MODEL,
            product_category=product_category,
            product_description=product_description,
        )

        # Step 2: Use the composite image as reference for video generation
        tryon_image_bytes = img_res["image_bytes"]
        video_res = generate_tryon_video(
            tryon_image_bytes=tryon_image_bytes,
            project_id=config.GOOGLE_CLOUD_PROJECT,
            output_bucket=config.TRYON_OUTPUT_BUCKET or None,
            scene_description=scene_description,
        )

        return {"status": "success", "product_id": product_id, **video_res}
    except Exception as e:
        logger.exception(
            "try_on_product_video failed for product_id=%s", product_id
        )
        return {"status": "error", "product_id": product_id, "error": str(e)}


INSTRUCTION = """You are a virtual try-on assistant for retail products.
Use the try_on_product_image tool when the user wants to see how a product
looks on them as a still image, and the try_on_product_video tool when the
user wants a short catwalk animation.

Always ask the user to provide their portrait photo and confirm the product
they want to try on before calling a tool. Present the returned output URI
or path to the user so they can view the result."""


root_agent = agents.Agent(
    name="root_agent",
    model=models.Gemini(model=config.GEMINI_MODEL),
    instruction=INSTRUCTION,
    tools=[try_on_product_image, try_on_product_video],
)

app = apps.App(
    root_agent=root_agent,
    # ADK's `adk web` auto-names the app from the agent module's parent
    # directory. The agent lives at scripts/tryon_agent.py, so ADK names it
    # "scripts". The App name MUST match the auto-discovered name or
    # session creation fails.
    name="scripts",
)
