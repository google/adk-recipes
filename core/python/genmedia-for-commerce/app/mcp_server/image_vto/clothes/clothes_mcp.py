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

"""MCP tool wrapper for the clothes image VTO pipeline."""

import base64
import logging

from workflows.image_vto.clothes.pipeline import run_image_vto

logger = logging.getLogger(__name__)


async def run_image_vto_clothes(
    full_body_image_base64: str,
    garment_images_base64: list[str],
    scenario: str = "a plain light grey studio environment",
    num_variations: int = 3,
    face_image_base64: str = "",
) -> dict:
    """Generate virtual try-on images of a real person wearing garments (B2C).

    Virtual try-on is a consumer-facing tool: it takes a real person's photo
    and one or more garment images, then generates realistic images of that
    person wearing those garments with face preservation. Unlike product
    fitting (B2B catalogue enrichment with a single product on an AI model),
    VTO supports multiple garments and uses the customer's own photo.

    Args:
        full_body_image_base64: Base64-encoded full body image of the model/person.
        garment_images_base64: List of base64-encoded garment images to try on.
            At least one garment image required.
        scenario: Description of the scene/setting for the generated image.
            Default: a plain light grey studio environment.
        num_variations: Number of variations to generate. Default: 3.
        face_image_base64: Optional base64-encoded face image for better face
            preservation. If empty, face is extracted from full body image.

    Returns:
        Dictionary with best result including image_base64, final_score,
        face_score, evaluation details, and garments_evaluation.

    """
    if not full_body_image_base64:
        return {"error": "Full body image is required."}
    if not garment_images_base64:
        return {"error": "At least one garment image is required."}

    def _decode_image(val: str, label: str) -> bytes | str:
        """Decode base64 to bytes, or pass through gs:// URIs as-is."""
        if val.startswith("gs://"):
            return val
        try:
            return base64.b64decode(val)
        except Exception as e:
            raise ValueError(f"Invalid base64 for {label}: {e}") from e

    try:
        full_body_bytes = _decode_image(
            full_body_image_base64, "full body image"
        )
    except ValueError as e:
        return {"error": str(e)}

    garment_images: list[bytes | str] = []
    for idx, img in enumerate(garment_images_base64):
        try:
            garment_images.append(_decode_image(img, f"garment image {idx}"))
        except ValueError as e:
            return {"error": str(e)}

    face_bytes = None
    if face_image_base64:
        try:
            face_bytes = _decode_image(face_image_base64, "face image")
        except ValueError as e:
            return {"error": str(e)}

    logger.info(
        f"[MCP image_vto_clothes] Starting: {len(garment_images)} garments, "
        f"num_variations={num_variations}"
    )

    best_result = None
    best_score = -1.0

    async for result in run_image_vto(
        full_body_bytes, garment_images, scenario, num_variations, face_bytes
    ):
        logger.info(
            f"[MCP image_vto_clothes] Pipeline event: status={result.status}, "
            f"index={getattr(result, 'index', '?')}, "
            f"score={getattr(result, 'final_score', None)}, "
            f"error={getattr(result, 'error', None)}"
        )
        if result.status == "failed" and result.index == -1:
            return {"error": result.error or "Pipeline failed"}
        if result.status == "complete":
            break
        if result.status == "ready" and result.image is not None:
            score = result.final_score or 0.0
            logger.info(
                f"[MCP image_vto_clothes] Variation {getattr(result, 'index', '?')} ready, score={score:.2f}"
            )
            if score > best_score:
                best_score = score
                best_result = result

    if best_result is None:
        return {"error": "No valid VTO result was generated."}

    response = {
        "image_base64": best_result.image_base64
        or base64.b64encode(best_result.image).decode("utf-8"),
        "final_score": best_result.final_score,
        "face_score": best_result.face_score,
    }
    if best_result.evaluation:
        response["evaluation"] = best_result.evaluation
    if best_result.garments_evaluation:
        response["garments_evaluation"] = best_result.garments_evaluation

    logger.info(f"[MCP image_vto_clothes] Complete. score={best_score:.1f}")
    return response
