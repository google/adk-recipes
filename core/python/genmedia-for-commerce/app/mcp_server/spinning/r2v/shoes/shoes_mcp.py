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

"""MCP tool wrapper for the shoes spinning R2V pipeline."""

import asyncio
import base64
import logging
import os

from google import genai
from workflows.spinning.r2v.shoes.pipeline import run_video_gen_pipeline_r2v

logger = logging.getLogger(__name__)


def _get_clients():
    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")
    client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    veo_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    return client, veo_client


async def run_spinning_shoes_r2v(
    images_base64: list[str],
    max_retries: int = 5,
    veo_model: str = "veo-3.1-generate-001",
    reference_type: str = "asset",
    upscale_images: bool = True,
    product_consistency_model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    product_id: str = "",
    gcs_bucket: str = "",
    gcs_destination_prefix: str = "shoe_spinning_outputs",
    gcs_project_id: str = "",
) -> dict:
    """Generate a 360-degree spinning video of shoes using reference-to-video (R2V).

    Takes product images of shoes and generates a smooth spinning video showing
    the product from all angles. Includes classification, validation, and
    optional GCS upload.

    Args:
        images_base64: List of base64-encoded shoe product images.
            Include images from multiple angles for best results.
        max_retries: Maximum retries for video generation validation. Default: 5.
        veo_model: Veo model for video generation. Default: veo-3.1-generate-001.
        reference_type: Type of reference images. Default: asset.
        upscale_images: Whether to upscale images before generation. Default: True.
        product_consistency_model: Model for product consistency validation.
        product_id: Optional product identifier for GCS uploads.
        gcs_bucket: Optional GCS bucket for uploading results.
        gcs_destination_prefix: GCS prefix for uploads. Default: shoe_spinning_outputs.
        gcs_project_id: Optional GCP project ID for GCS.

    Returns:
        Dictionary with video_base64 and frames_base64 (if no GCS bucket),
        or GCS URIs and metadata (if GCS bucket provided).

    """
    if not images_base64:
        return {"error": "At least one shoe image is required."}

    images_bytes = []
    for idx, img_b64 in enumerate(images_base64):
        try:
            images_bytes.append(base64.b64decode(img_b64))
        except Exception as e:
            return {"error": f"Invalid base64 encoding for image {idx}: {e}"}

    client, veo_client = _get_clients()
    shoe_classifier_model = os.getenv("SHOE_CLASSIFICATION_ENDPOINT")
    gemini_model = os.getenv("MODEL_NAME_GENERATED_1")

    logger.info(
        f"[MCP spinning_shoes_r2v] Starting: {len(images_bytes)} images, "
        f"max_retries={max_retries}"
    )

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: run_video_gen_pipeline_r2v(
            images_bytes_list=images_bytes,
            client=client,
            veo_client=veo_client,
            shoe_classifier_model=shoe_classifier_model,
            gemini_model=gemini_model,
            max_retries=max_retries,
            veo_model=veo_model,
            reference_type=reference_type,
            product_consistency_model=product_consistency_model,
            product_id=product_id or None,
            gcs_bucket=gcs_bucket or None,
            gcs_destination_prefix=gcs_destination_prefix,
            gcs_project_id=gcs_project_id or None,
            upscale_images=upscale_images,
            disable_logging=False,
        ),
    )

    if result is None:
        return {"error": "Pipeline returned no result."}

    if isinstance(result, dict) and "video_bytes" in result:
        video_base64 = base64.b64encode(result["video_bytes"]).decode("utf-8")
        frames_base64 = [
            base64.b64encode(frame).decode("utf-8")
            for frame in result["frames"]
        ]
        logger.info(
            f"[MCP spinning_shoes_r2v] Complete. {len(frames_base64)} frames"
        )
        return {
            "video_base64": video_base64,
            "frames_base64": frames_base64,
            "num_frames": len(frames_base64),
            "retry_count": result["retry_count"],
        }
    else:
        logger.info("[MCP spinning_shoes_r2v] Complete. Uploaded to GCS")
        return {
            "video_gen_status": result["video_gen_status"],
            "num_clips": len(result["clips"]),
            "gcs_uris": result["gcs_uris"],
            "gcs_base_uri": result.get("gcs_base_uri", ""),
        }
