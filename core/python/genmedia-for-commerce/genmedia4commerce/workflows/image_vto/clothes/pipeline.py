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

"""Core VTO pipeline as an async generator.

This module is the single source of truth for the image VTO orchestration.
It is consumed by:
  - The FastAPI route (main.py) — streams results as SSE
  - The MCP server (mcp_server.py) — returns the best result
  - (future) The video VTO backend — uses the best image for video generation
"""

import asyncio
import base64
import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from google import genai
from workflows.image_vto.clothes.garment_description import (
    describe_all_garments,
)
from workflows.image_vto.clothes.garment_eval import evaluate_garments
from workflows.image_vto.clothes.vto_generation import (
    evaluate_vto_image,
    generate_vto,
    preprocess_face_image,
    preprocess_model_image,
)
from workflows.shared.person_eval import validate_model_photo

logger = logging.getLogger(__name__)


def _now():
    """Return current time as HH:MM:SS.mmm."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Gemini clients (initialised once at import time, same as the old main.py)
# ---------------------------------------------------------------------------
_config_loaded = False


def _ensure_config():
    global _config_loaded
    if _config_loaded:
        return
    from dotenv import load_dotenv

    config_path = Path(__file__).parent.parent.parent.parent / "config.env"
    if config_path.exists():
        load_dotenv(config_path)
    _config_loaded = True


def _get_clients() -> tuple[genai.Client, genai.Client]:
    """Return (genai_client, nano_client), creating them on first call."""
    _ensure_config()
    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")
    genai_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    nano_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    return genai_client, nano_client


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class VTOResult:
    """A single result yielded by the VTO pipeline."""

    index: int = -1
    status: Literal[
        "reference_face", "ready", "discarded", "failed", "complete"
    ] = "ready"
    image: bytes | None = None
    image_base64: str | None = None
    reference_face_base64: str | None = None
    evaluation: dict | None = None
    garments_evaluation: dict | None = None
    final_score: float | None = None
    face_score: float | None = None
    error: str | None = None
    total: int | None = None

    def to_sse_dict(self) -> dict:
        """Convert to a dict suitable for SSE JSON serialisation."""
        d: dict = {}
        if self.status == "reference_face":
            d["reference_face_base64"] = self.reference_face_base64
            return d
        if self.status == "complete":
            d["status"] = "complete"
            d["total"] = self.total
            return d
        d["index"] = self.index
        d["status"] = self.status
        if self.status == "failed":
            d["error"] = self.error or "Unknown error"
            return d
        if self.image is not None:
            d["image_base64"] = self.image_base64 or base64.b64encode(
                self.image
            ).decode("utf-8")
        if self.evaluation is not None:
            d["evaluation"] = self.evaluation
        if self.garments_evaluation is not None:
            d["garments_evaluation"] = self.garments_evaluation
        if self.final_score is not None:
            d["final_score"] = self.final_score
        if self.face_score is not None:
            d["face_score"] = self.face_score
        return d


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
async def run_image_vto(
    full_body_image: bytes,
    garment_images: list[bytes | str],
    scenario: str = "a plain light grey studio environment",
    num_variations: int = 3,
    face_image: bytes | None = None,
    image_size: str = "1K",
) -> AsyncGenerator[VTOResult, None]:
    """Run the full image VTO pipeline.  Yields results as each variation completes.

    Yield order:
      1. A single VTOResult with status="reference_face" (carries reference_face_base64)
      2. One VTOResult per variation (status = ready | discarded | failed)
      3. A final VTOResult with status="complete"
    """
    genai_client, nano_client = _get_clients()

    # --- Validate model photo (face visible, looking at camera, eyes open) ---
    face_source = face_image if face_image else full_body_image
    validation = await asyncio.get_event_loop().run_in_executor(
        None, validate_model_photo, face_source
    )
    if not validation["valid"]:
        logger.warning(
            f"[VTO] Model photo validation failed: {validation['reason']}"
        )
        yield VTOResult(
            status="failed",
            error=validation["reason"],
        )
        return

    # --- Preprocessing (face + body + garment descriptions in parallel) ---
    logger.debug(
        f"[VTO] Starting parallel preprocessing "
        f"(face from {'separate image' if face_image else 'full body'} + body + garment descriptions)"
    )

    face_result, preprocessed_body, garment_descriptions = await asyncio.gather(
        asyncio.get_event_loop().run_in_executor(
            None, preprocess_face_image, nano_client, face_source
        ),
        asyncio.get_event_loop().run_in_executor(
            None, preprocess_model_image, nano_client, full_body_image
        ),
        asyncio.get_event_loop().run_in_executor(
            None, describe_all_garments, genai_client, garment_images
        ),
    )

    framing = "full_body"
    logger.debug(f"[VTO] Framing: {framing}")
    logger.debug(f"[VTO] Garment descriptions: {garment_descriptions}")

    reference_face, preprocessed_face = face_result

    if reference_face is None:
        logger.error("[VTO] No face detected in image")
        yield VTOResult(
            status="failed",
            error="No face detected in the image. Please upload an image with a visible face.",
        )
        return

    preprocessed_person_images = [preprocessed_face, preprocessed_body]
    logger.debug("[VTO] Parallel preprocessing complete - ready for generation")

    # Emit reference face
    yield VTOResult(
        status="reference_face",
        reference_face_base64=base64.b64encode(reference_face).decode("utf-8"),
    )

    # --- Generate variations in parallel ---
    result_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def _generate_variation(idx: int):
        """Generate one variation, evaluate it, and put the result on the queue."""
        try:
            vto_result = await asyncio.get_event_loop().run_in_executor(
                None,
                generate_vto,
                nano_client,
                scenario,
                garment_images,
                preprocessed_person_images,
                framing,
                garment_descriptions,
                image_size,
            )

            if vto_result is None:
                await result_queue.put(
                    {"index": idx, "image": None, "error": "Generation failed"}
                )
                return

            step1_image = vto_result["step1_image"]
            step2_image = vto_result["step2_image"]
            logger.debug(
                f"[VTO] Variation {idx} images generated, evaluating both steps"
            )

            # Evaluate Step 1
            step1_eval = await asyncio.get_event_loop().run_in_executor(
                None, evaluate_vto_image, step1_image, reference_face
            )
            logger.debug(
                f"[VTO] Variation {idx} Step 1 score: "
                f"{step1_eval['similarity_percentage']:.1f}%"
            )

            # Evaluate Step 2 if it exists
            if step2_image is not None:
                step2_eval = await asyncio.get_event_loop().run_in_executor(
                    None, evaluate_vto_image, step2_image, reference_face
                )
                logger.debug(
                    f"[VTO] Variation {idx} Step 2 score: "
                    f"{step2_eval['similarity_percentage']:.1f}%"
                )
                if (
                    step2_eval["similarity_percentage"]
                    >= step1_eval["similarity_percentage"]
                ):
                    best_image, best_eval, best_step = (
                        step2_image,
                        step2_eval,
                        2,
                    )
                else:
                    best_image, best_eval, best_step = (
                        step1_image,
                        step1_eval,
                        1,
                    )
            else:
                best_image, best_eval, best_step = step1_image, step1_eval, 1

            logger.debug(
                f"[VTO] Variation {idx} selected Step {best_step} "
                f"(score: {best_eval['similarity_percentage']:.1f}%)"
            )

            # Evaluate garment accuracy
            garments_eval = await asyncio.get_event_loop().run_in_executor(
                None,
                evaluate_garments,
                genai_client,
                best_image,
                garment_images,
                os.getenv("MODEL_NAME_GENERATED_4"),
                garment_descriptions,
            )

            eval_data = {
                **best_eval,
                "step1_score": step1_eval["similarity_percentage"],
                "step2_score": (
                    step2_eval["similarity_percentage"] if step2_image else None
                ),
                "selected_step": best_step,
            }

            # Debug save discarded
            if garments_eval["discard"]:
                from workflows.shared.debug_utils import DEBUG_ENABLED

                if DEBUG_ENABLED:
                    debug_dir = Path(__file__).parent / "debug"
                    debug_dir.mkdir(exist_ok=True)
                    (
                        debug_dir / f"{_now()}_var{idx}_discarded.png"
                    ).write_bytes(best_image)
                logger.debug(
                    f"[VTO] Variation {idx} discarded: garment score below threshold "
                    f"(scores: {[d['score'] for d in garments_eval['garment_details']]})"
                )
                await result_queue.put(
                    {
                        "index": idx,
                        "image": best_image,
                        "status": "discarded",
                        "evaluation": eval_data,
                        "garments_evaluation": garments_eval,
                    }
                )
                return

            face_similarity = best_eval["similarity_percentage"]
            garments_score = garments_eval["garments_score"]
            face_weight = 0.7
            final_score = face_similarity * face_weight + garments_score * (
                1 - face_weight
            )
            logger.debug(
                f"[VTO] Variation {idx} final_score: {final_score:.1f} "
                f"(face: {face_similarity:.1f}, garments: {garments_score:.1f})"
            )

            # Debug save
            from workflows.shared.debug_utils import DEBUG_ENABLED

            if DEBUG_ENABLED:
                debug_dir = Path(__file__).parent / "debug"
                debug_dir.mkdir(exist_ok=True)
                (debug_dir / f"{_now()}_var{idx}.png").write_bytes(best_image)

            await result_queue.put(
                {
                    "index": idx,
                    "image": best_image,
                    "evaluation": eval_data,
                    "garments_evaluation": garments_eval,
                    "final_score": final_score,
                    "face_score": face_similarity,
                }
            )

        except Exception as e:
            logger.error(f"[VTO] Variation {idx} error: {e}")
            await result_queue.put(
                {"index": idx, "image": None, "error": str(e)}
            )

    # Launch all variations in parallel
    logger.debug(f"[VTO] Starting all {num_variations} variations in parallel")
    tasks = [
        asyncio.create_task(_generate_variation(idx))
        for idx in range(num_variations)
    ]

    # Yield results as they complete
    for _ in range(num_variations):
        try:
            item = await asyncio.wait_for(result_queue.get(), timeout=420)

            if item.get("image") is None:
                yield VTOResult(
                    index=item["index"],
                    status="failed",
                    error=item.get("error", "Unknown error"),
                )
            elif item.get("status") == "discarded":
                yield VTOResult(
                    index=item["index"],
                    status="discarded",
                    image=item["image"],
                    image_base64=base64.b64encode(item["image"]).decode(
                        "utf-8"
                    ),
                    evaluation=item["evaluation"],
                    garments_evaluation=item["garments_evaluation"],
                )
            else:
                yield VTOResult(
                    index=item["index"],
                    status="ready",
                    image=item["image"],
                    image_base64=base64.b64encode(item["image"]).decode(
                        "utf-8"
                    ),
                    evaluation=item["evaluation"],
                    garments_evaluation=item.get("garments_evaluation"),
                    final_score=item.get("final_score"),
                    face_score=item.get("face_score"),
                )

        except TimeoutError:
            logger.error("[VTO] Variation timeout")
            yield VTOResult(status="failed", error="Timeout")

    # Cleanup
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    yield VTOResult(status="complete", total=num_variations)
