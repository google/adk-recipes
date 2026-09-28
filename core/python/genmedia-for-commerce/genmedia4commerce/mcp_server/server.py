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

"""GenMedia MCP Server.

Exposes each media generation workflow as an MCP tool.
Can be run standalone via stdio or connected to by the ADK agent.

Usage:
    # Run via stdio (for ADK agent or mcp client)
    python -m mcp_server.server

    # Or via the Makefile
    make mcp-server
"""

import json
import logging
import os
import sys
from pathlib import Path

# Ensure genmedia4commerce/ is on the path for backend imports
genmedia_dir = str(Path(__file__).parent.parent)
if genmedia_dir not in sys.path:
    sys.path.insert(0, genmedia_dir)

# Load environment variables
from dotenv import load_dotenv  # noqa: E402

project_root = Path(__file__).parent.parent.parent
config_path = Path(__file__).parent.parent / "config.env"
if config_path.exists():
    load_dotenv(config_path)

# Configure logging — logs go to stderr (visible in terminal) and to file for persistence
_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

_file_handler = logging.FileHandler(project_root / "mcp_server.log", mode="a")
_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(_file_handler)
root_logger.addHandler(_console_handler)

logger = logging.getLogger("mcp_server")
logger.info("MCP server starting")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp_server.image_vto.image_vto_mcp import run_image_vto  # noqa: E402
from mcp_server.other.background_changer.background_changer_mcp import (  # noqa: E402
    run_background_changer,
)

# Import all MCP tool functions
from mcp_server.product_enrichment.product_fitting.product_fitting_mcp import (  # noqa: E402
    run_product_fitting,
)
from mcp_server.shared.catalog.catalog_mcp import (  # noqa: E402
    run_catalog_search,
)
from mcp_server.spinning.spinning_mcp import run_product_spinning  # noqa: E402
from mcp_server.video_vto.clothes.clothes_mcp import (  # noqa: E402
    run_animate_model_mcp,
)
from mcp_server.video_vto.video_vto_mcp import run_video_vto  # noqa: E402

_mcp_port_str = os.getenv("MCP_SERVER_PORT", "8081")
try:
    _mcp_port = int(_mcp_port_str)
except (ValueError, TypeError):
    _mcp_port = 8081

server = FastMCP(
    "genmedia-retail",
    host="0.0.0.0",  # noqa: S104
    port=_mcp_port,
)


# ---------------------------------------------------------------------------
# Product Enrichment
# ---------------------------------------------------------------------------


@server.tool()
async def product_fitting(
    garment_images_base64: list[str],
    gender: str,
    scenario: str = "a pure white background (#FFFFFF), no shadows, no gradients",
    max_retries: int = 3,
    generation_model: str = os.getenv("MODEL_NAME_GENERATED_5"),
    product_id: str = "",
) -> str:
    """Generate product fitting images showing garments worn on a model body.

    Takes garment product photos and generates realistic images of the garments
    being worn by a model, producing both front and back views.

    Args:
        garment_images_base64: List of base64-encoded garment product images.
        gender: Model gender. One of: man, woman.
        scenario: Background description for the generated image.
        max_retries: Maximum generation attempts per view (front/back).
        generation_model: Gemini model for image generation.
        product_id: Optional product identifier for logging.

    Returns:
        JSON string with front and back fitting results including base64 images.

    """
    import random

    ethnicity = random.choice(["african", "asian", "european"])  # noqa: S311
    logger.info(
        f"[product_fitting] Called with {len(garment_images_base64)} images, ethnicity={ethnicity}"
    )
    try:
        result = await run_product_fitting(
            garment_images_base64=garment_images_base64,
            gender=gender,
            ethnicity=ethnicity,
            scenario=scenario,
            max_retries=max_retries,
            generation_model=generation_model,
            product_id=product_id,
        )
        return json.dumps(result)
    except Exception as e:
        logger.exception(f"[product_fitting] Error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Image VTO (clothes + glasses)
# ---------------------------------------------------------------------------


@server.tool()
async def image_vto(
    person_image_base64: str,
    product_images_base64: list[str],
    is_glasses: bool = False,
    scenario: str = "a plain light grey studio environment",
    num_variations: int = 3,
    face_image_base64: str = "",
) -> str:
    """Generate virtual try-on images showing a person wearing garments or glasses.

    For clothes: provide a full body image. For glasses: provide a front face image.
    Set is_glasses=True when the product is eyewear/sunglasses.

    Args:
        person_image_base64: Base64-encoded image of the person (full body for clothes, face for glasses).
        product_images_base64: Base64-encoded product images (garments or glasses).
        is_glasses: Set True for glasses/eyewear try-on. Default: False (clothes).
        scenario: Scene/setting description (clothes only). Default: light grey studio.
        num_variations: Number of variations to generate. Default: 3.
        face_image_base64: Optional separate face image for better preservation (clothes only).

    Returns:
        JSON with best result: image_base64, scores, evaluation.

    """
    logger.info(
        f"[image_vto] Called with {len(product_images_base64)} images, is_glasses={is_glasses}"
    )
    try:
        result = await run_image_vto(
            person_image_base64=person_image_base64,
            product_images_base64=product_images_base64,
            is_glasses=is_glasses,
            scenario=scenario,
            num_variations=num_variations,
            face_image_base64=face_image_base64,
        )
        return json.dumps(result)
    except Exception as e:
        logger.exception(f"[image_vto] Error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Video VTO (clothes + glasses)
# ---------------------------------------------------------------------------


@server.tool()
async def video_vto(
    person_image_base64: str,
    product_images_base64: list[str],
    is_glasses: bool = False,
    scenario: str = "a plain white studio background",
    num_variations: int = 3,
    face_image_base64: str = "",
    number_of_videos: int = 4,
    prompt: str = "",
) -> str:
    """Generate virtual try-on video of a person wearing garments or glasses.

    For clothes: runs image VTO first, picks the best result, then generates videos.
    For glasses: generates videos directly from the face and glasses images.
    Set is_glasses=True when the product is eyewear/sunglasses.

    Args:
        person_image_base64: Base64-encoded image (full body for clothes, face for glasses).
        product_images_base64: Base64-encoded product images (garments or glasses).
        is_glasses: Set True for glasses/eyewear video. Default: False (clothes).
        scenario: Background description (clothes only). Default: white studio.
        num_variations: Image VTO variations before video (clothes only). Default: 3.
        face_image_base64: Optional face image for preservation (clothes only).
        number_of_videos: Videos to generate. Default: 4.
        prompt: Optional custom video prompt.

    Returns:
        JSON with videos (base64), scores, and metadata.

    """
    logger.info(
        f"[video_vto] Called with {len(product_images_base64)} images, is_glasses={is_glasses}"
    )
    try:
        result = await run_video_vto(
            person_image_base64=person_image_base64,
            product_images_base64=product_images_base64,
            is_glasses=is_glasses,
            scenario=scenario,
            num_variations=num_variations,
            face_image_base64=face_image_base64,
            number_of_videos=number_of_videos,
            prompt=prompt,
        )
        return json.dumps(result)
    except Exception as e:
        logger.exception(f"[video_vto] Error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Background Changer
# ---------------------------------------------------------------------------


@server.tool()
async def background_changer(
    person_image_base64: str,
    background_description: str = "",
    background_image_base64: str = "",
    num_variations: int = 4,
) -> str:
    """Change the background of a person image while preserving the person exactly.

    Args:
        person_image_base64: Base64-encoded image of the person.
        background_description: Text description of desired background.
        background_image_base64: Base64-encoded background reference image.
        num_variations: Variations to generate. Default: 4.

    Returns:
        JSON with best image_base64, evaluation score.

    """
    logger.info("[background_changer] Called")
    try:
        result = await run_background_changer(
            person_image_base64=person_image_base64,
            background_description=background_description,
            background_image_base64=background_image_base64,
            num_variations=num_variations,
        )
        return json.dumps(result)
    except Exception as e:
        logger.exception(f"[background_changer] Error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Product Spinning (shoes + other products)
# ---------------------------------------------------------------------------


@server.tool()
async def product_spinning(
    images_base64: list[str],
    is_shoes: bool = False,
    max_retries: int = 5,
    veo_model: str = "veo-3.1-generate-001",
    product_id: str = "",
    gcs_bucket: str = "",
    gcs_destination_prefix: str = "spinning_outputs",
    gcs_project_id: str = "",
) -> str:
    """Generate a 360-degree spinning video of a product.

    Works with any product type. Set is_shoes=True for shoes to use the
    specialized shoes pipeline with validation and upscaling.

    Args:
        images_base64: Base64-encoded product images from multiple angles.
        is_shoes: Set True for shoes (specialized pipeline). Default: False.
        max_retries: Max retries for validation (shoes only). Default: 5.
        veo_model: Veo model (shoes only). Default: veo-3.1-generate-001.
        product_id: Product identifier for GCS (shoes only).
        gcs_bucket: GCS bucket for uploads (shoes only).
        gcs_destination_prefix: GCS prefix. Default: spinning_outputs.
        gcs_project_id: GCP project ID for GCS (shoes only).

    Returns:
        JSON with video_base64 and metadata.

    """
    logger.info(
        f"[product_spinning] Called with {len(images_base64)} images, is_shoes={is_shoes}"
    )
    try:
        result = await run_product_spinning(
            images_base64=images_base64,
            is_shoes=is_shoes,
            max_retries=max_retries,
            veo_model=veo_model,
            product_id=product_id,
            gcs_bucket=gcs_bucket,
            gcs_destination_prefix=gcs_destination_prefix,
            gcs_project_id=gcs_project_id,
        )
        return json.dumps(result)
    except Exception as e:
        logger.exception(f"[product_spinning] Error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Animate Model
# ---------------------------------------------------------------------------


@server.tool()
async def animate_model(
    model_image_base64: str,
    number_of_videos: int = 4,
    prompt: str = "",
) -> str:
    """Animate a model image into catwalk-style videos.

    Takes an image of a model already wearing garments and generates
    animation videos. Use this when the model image is already ready
    (e.g. from a previous image VTO) and you only need the video.

    Args:
        model_image_base64: Base64-encoded image of the model wearing garments.
        number_of_videos: Videos to generate. Default: 4.
        prompt: Optional custom animation prompt. Defaults to catwalk sequence.

    Returns:
        JSON with videos (base64), scores, and filenames.

    """
    logger.info("[animate_model] Called")
    try:
        result = await run_animate_model_mcp(
            model_image_base64=model_image_base64,
            number_of_videos=number_of_videos,
            prompt=prompt,
        )
        return json.dumps(result)
    except Exception as e:
        logger.exception(f"[animate_model] Error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Catalog Search
# ---------------------------------------------------------------------------


@server.tool()
async def catalog_search(
    query: str,
    k: int = 5,
) -> str:
    """Search the product catalog for items matching a text query.

    Args:
        query: Text description to search for (e.g. "red casual dress").
        k: Number of results to return. Default: 5.

    Returns:
        JSON list of product data fields.

    """
    logger.info(f"[catalog_search] Query: {query}")
    try:
        result = await run_catalog_search(query=query, k=k)
    except Exception as e:
        logger.exception(f"[catalog_search] Error: {e}")
        return json.dumps({"error": str(e)})

    items = result.get("results", [])
    return json.dumps(items)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport: stdio (for ADK agent) or sse (HTTP, for external clients)",
    )
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    # Override port from CLI if provided
    server.settings.port = args.port

    logger.info(
        f"Starting MCP server with transport={args.transport}, port={args.port}"
    )
    server.run(transport=args.transport)
