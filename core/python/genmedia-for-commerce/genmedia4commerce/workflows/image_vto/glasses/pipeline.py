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

"""Core glasses VTO pipeline as an async generator.

Follows the same pattern as the clothes VTO pipeline (clothes/pipeline.py).
Yields results as SSE events for progressive display.
"""

import asyncio
import base64
import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from google import genai
from workflows.image_vto.glasses.glasses_eval import evaluate_all_glasses
from workflows.image_vto.glasses.image_generation import (
    create_frame_nano,
    describe_glasses,
    evaluate_vto_image,
    preprocess_face_image,
    preprocess_glasses_image,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gemini clients (initialised lazily, same pattern as clothes pipeline)
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
# Result dataclass (same structure as clothes VTOResult)
# ---------------------------------------------------------------------------
@dataclass
class VTOResult:
    """A single result yielded by the glasses VTO pipeline."""

    index: int = -1
    status: Literal[
        "reference_face", "ready", "discarded", "failed", "complete"
    ] = "ready"
    image: bytes | None = None
    image_base64: str | None = None
    reference_face_base64: str | None = None
    evaluation: dict | None = None
    glasses_evaluation: dict | None = None
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
        if self.glasses_evaluation is not None:
            d["glasses_evaluation"] = self.glasses_evaluation
        if self.final_score is not None:
            d["final_score"] = self.final_score
        if self.face_score is not None:
            d["face_score"] = self.face_score
        return d


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
async def run_glasses_vto(
    model_image: bytes,
    glasses_images: list[bytes],
    num_variations: int = 3,
) -> AsyncGenerator[VTOResult, None]:
    """Run the full glasses VTO pipeline.  Yields results as each variation completes.

    Yield order:
      1. A single VTOResult with status="reference_face"
      2. One VTOResult per variation (status = ready | failed)
      3. A final VTOResult with status="complete"
    """
    genai_client, nano_client = _get_clients()

    # --- Preprocessing (face + glasses in parallel) ---
    logger.info(
        "[Glasses VTO] Starting parallel preprocessing (face + glasses)"
    )

    preprocess_tasks = [
        asyncio.get_event_loop().run_in_executor(
            None, preprocess_face_image, nano_client, model_image
        ),
    ]
    # Preprocess each glasses image in parallel
    for g_img in glasses_images:
        preprocess_tasks.append(
            asyncio.get_event_loop().run_in_executor(
                None, preprocess_glasses_image, genai_client, g_img
            )
        )
    # Auto-describe glasses from the first product image (runs in parallel)
    preprocess_tasks.append(
        asyncio.get_event_loop().run_in_executor(
            None, describe_glasses, genai_client, glasses_images[0]
        )
    )

    preprocess_results = await asyncio.gather(*preprocess_tasks)

    face_result = preprocess_results[0]
    # Results between face and description are the preprocessed glasses images
    preprocessed_glasses = list(preprocess_results[1:-1])
    glasses_description = preprocess_results[-1]  # str | None

    reference_face, preprocessed_face = face_result

    if reference_face is None:
        logger.error("[Glasses VTO] No face detected in image")
        yield VTOResult(
            status="failed",
            error="No face detected in the image. Please upload an image with a visible face.",
        )
        return

    logger.info("[Glasses VTO] Parallel preprocessing complete")

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
                create_frame_nano,
                nano_client,
                preprocessed_glasses,
                preprocessed_face,
                reference_face,
                glasses_description,
            )

            if vto_result is None:
                await result_queue.put(
                    {"index": idx, "image": None, "error": "Generation failed"}
                )
                return

            step1_image = vto_result["step1_image"]
            step2_image = vto_result["step2_image"]
            logger.info(
                f"[Glasses VTO] Variation {idx} images generated, evaluating both steps"
            )

            # Evaluate Step 1
            step1_eval = await asyncio.get_event_loop().run_in_executor(
                None, evaluate_vto_image, step1_image, reference_face
            )
            logger.info(
                f"[Glasses VTO] Variation {idx} Step 1 score: "
                f"{step1_eval['similarity_percentage']:.1f}%"
            )

            # Evaluate Step 2 if it exists
            if step2_image is not None:
                step2_eval = await asyncio.get_event_loop().run_in_executor(
                    None, evaluate_vto_image, step2_image, reference_face
                )
                logger.info(
                    f"[Glasses VTO] Variation {idx} Step 2 score: "
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

            logger.info(
                f"[Glasses VTO] Variation {idx} selected Step {best_step} "
                f"(score: {best_eval['similarity_percentage']:.1f}%)"
            )

            face_similarity = best_eval["similarity_percentage"]
            eval_data = {
                **best_eval,
                "step1_score": step1_eval["similarity_percentage"],
                "step2_score": (
                    step2_eval["similarity_percentage"] if step2_image else None
                ),
                "selected_step": best_step,
            }

            # Evaluate glasses fidelity against reference
            glasses_eval = await asyncio.get_event_loop().run_in_executor(
                None,
                evaluate_all_glasses,
                genai_client,
                best_image,
                glasses_images,
            )

            if glasses_eval["discard"]:
                logger.info(
                    f"[Glasses VTO] Variation {idx} discarded: glasses mismatch "
                    f"({glasses_eval['glasses_details'].get('explanation', '')})"
                )
                await result_queue.put(
                    {
                        "index": idx,
                        "image": best_image,
                        "status": "discarded",
                        "evaluation": eval_data,
                        "glasses_evaluation": glasses_eval,
                    }
                )
                return

            glasses_score = glasses_eval["glasses_score"]
            face_weight = 0.7
            final_score = face_similarity * face_weight + glasses_score * (
                1 - face_weight
            )

            logger.info(
                f"[Glasses VTO] Variation {idx} final_score: {final_score:.1f} "
                f"(face: {face_similarity:.1f}, glasses: {glasses_score:.1f})"
            )

            await result_queue.put(
                {
                    "index": idx,
                    "image": best_image,
                    "evaluation": eval_data,
                    "glasses_evaluation": glasses_eval,
                    "final_score": final_score,
                    "face_score": face_similarity,
                }
            )

        except Exception as e:
            logger.error(f"[Glasses VTO] Variation {idx} error: {e}")
            await result_queue.put(
                {"index": idx, "image": None, "error": str(e)}
            )

    # Launch all variations in parallel
    logger.info(
        f"[Glasses VTO] Starting all {num_variations} variations in parallel"
    )
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
                    glasses_evaluation=item["glasses_evaluation"],
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
                    glasses_evaluation=item.get("glasses_evaluation"),
                    final_score=item.get("final_score"),
                    face_score=item.get("face_score"),
                )

        except TimeoutError:
            logger.error("[Glasses VTO] Variation timeout")
            yield VTOResult(status="failed", error="Timeout")

    # Cleanup
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    yield VTOResult(status="complete", total=num_variations)
