# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""REST API endpoint for the product fitting workflow (catalogue enrichment)."""

import logging
import os

from fastapi import APIRouter, HTTPException
from mcp_server.product_enrichment.product_fitting.product_fitting_mcp import (
    run_product_fitting,
    run_product_fitting_animation,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/product-enrichment/product-fitting",
    tags=["Product Fitting"],
)


class ProductFittingRequest(BaseModel):
    """Request schema for product fitting generation."""

    garment_images_base64: list[str] = Field(
        ..., description="List of base64-encoded garment product images"
    )
    gender: str = Field(..., description="Model gender: man or woman")
    ethnicity: str = Field(
        default="european",
        description="Model ethnicity preset: african, asian, european",
    )
    scenario: str = Field(
        default="a pure white background (#FFFFFF), no shadows, no gradients",
        description="Background description for the generated image",
    )
    max_retries: int = Field(
        default=3, description="Maximum generation attempts per view"
    )
    generation_model: str = Field(
        default=os.getenv("MODEL_NAME_GENERATED_5"),
        description="Gemini model for image generation",
    )
    product_id: str = Field(
        default="", description="Optional product identifier"
    )
    model_photos: dict[str, str] | None = Field(
        default=None,
        description="Optional custom model photos as base64. "
        "Keys: front_top, front_bottom. If provided, ethnicity preset is ignored.",
    )


@router.post("/generate-fitting-pipeline")
async def generate_fitting_pipeline(request: ProductFittingRequest) -> dict:
    """Generate product fitting images for catalogue enrichment.

    Takes a single garment's product photos and generates realistic images of that
    garment worn on an AI model body (front and back views). Designed for B2B
    catalogue imagery — for B2C virtual try-on with multiple garments on a real
    person, use the image VTO endpoint instead.
    """
    if not request.garment_images_base64:
        raise HTTPException(
            status_code=400,
            detail="Missing 'garment_images_base64' in request body.",
        )

    if not request.gender:
        raise HTTPException(
            status_code=400, detail="'gender' is required (e.g. man, woman)."
        )

    if not request.model_photos and not request.ethnicity:
        raise HTTPException(
            status_code=400,
            detail="Must provide either 'ethnicity' (with 'gender') or 'model_photos' "
            "(dict with keys: front_top, front_bottom).",
        )

    logger.info(
        f"[REST product_fitting] Called with {len(request.garment_images_base64)} images, "
        f"gender={request.gender}, ethnicity={request.ethnicity}"
    )

    result = await run_product_fitting(
        garment_images_base64=request.garment_images_base64,
        gender=request.gender,
        ethnicity=request.ethnicity,
        scenario=request.scenario,
        max_retries=request.max_retries,
        generation_model=request.generation_model,
        product_id=request.product_id,
        model_photos=request.model_photos,
    )

    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])

    logger.info(f"[REST product_fitting] Complete. Keys: {list(result.keys())}")
    return result


class ProductFittingVideoRequest(BaseModel):
    """Request schema for product fitting video generation."""

    front_image_base64: str = Field(..., description="Base64 front image")
    back_image_base64: str | None = Field(
        default=None, description="Base64 back image"
    )
    framing: str = Field(default="full_body", description="Framing")
    prompt: str = Field(default="", description="Custom prompt")


@router.post("/generate-video")
async def generate_video(request: ProductFittingVideoRequest) -> dict:
    """Generate product fitting video."""
    logger.info(
        f"[REST product_fitting] Called /generate-video, framing={request.framing}"
    )
    result = await run_product_fitting_animation(
        front_image_base64=request.front_image_base64,
        back_image_base64=request.back_image_base64,
        framing=request.framing,
        number_of_videos=1,
        prompt=request.prompt,
    )
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return result
