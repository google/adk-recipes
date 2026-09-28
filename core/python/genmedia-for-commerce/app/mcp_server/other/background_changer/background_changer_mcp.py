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

"""MCP tool wrapper for the background changer pipeline."""

import asyncio
import base64
import logging
import os

from fastapi.concurrency import run_in_threadpool
from google import genai
from workflows.other.background_changer.background_changer import (
    evaluate_background_change_image,
    generate_background_change_only,
    preprocess_face_image,
    preprocess_person_image,
)

logger = logging.getLogger(__name__)


def _get_clients():
    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")
    client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    nano_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    return client, nano_client


async def run_background_changer(
    person_image_base64: str,
    background_description: str = "",
    background_image_base64: str = "",
    num_variations: int = 4,
) -> dict:
    """Change the background of a person image while preserving the person exactly.

    Takes a person image and either a text description or reference image for the
    new background. Generates multiple variations and returns the best one.

    Args:
        person_image_base64: Base64-encoded image of the person.
        background_description: Text description of desired background.
            Either this or background_image_base64 must be provided.
        background_image_base64: Base64-encoded background reference image.
            Either this or background_description must be provided.
        num_variations: Number of variations to generate. Default: 4.

    Returns:
        Dictionary with best result including image_base64, evaluation score,
        and generation details.

    """
    if not person_image_base64:
        return {"error": "Person image is required."}
    if not background_description and not background_image_base64:
        return {
            "error": "Either background_description or background_image_base64 is required."
        }

    try:
        person_bytes = base64.b64decode(person_image_base64)
    except Exception as e:
        return {"error": f"Invalid base64 for person image: {e}"}

    background_bytes = None
    if background_image_base64:
        try:
            background_bytes = base64.b64decode(background_image_base64)
        except Exception as e:
            return {"error": f"Invalid base64 for background image: {e}"}

    client, nano_client = _get_clients()

    logger.info(
        f"[MCP background_changer] Starting: "
        f"mode={'image' if background_bytes else 'text'}, "
        f"num_variations={num_variations}"
    )

    # Preprocess face and person in parallel
    async def process_face():
        return await run_in_threadpool(
            preprocess_face_image, client, nano_client, person_bytes
        )

    async def process_person():
        return await run_in_threadpool(
            preprocess_person_image, client, nano_client, person_bytes
        )

    face_result, preprocessed_person = await asyncio.gather(
        process_face(), process_person()
    )

    reference_face, preprocessed_face = face_result

    if reference_face is None:
        return {
            "error": "No face detected in the person image. Please upload a clearer image."
        }

    # Generate variations in parallel and pick the best
    best_image = None
    best_score = -1.0
    best_eval = None

    async def generate_variation(idx):
        gen_result = await run_in_threadpool(
            generate_background_change_only,
            nano_client,
            preprocessed_person,
            preprocessed_face,
            background_description or None,
            background_bytes,
        )

        if gen_result is None:
            return None

        step1_image = gen_result["step1_image"]
        step2_image = gen_result["step2_image"]

        loop = asyncio.get_event_loop()
        step1_eval = await loop.run_in_executor(
            None,
            evaluate_background_change_image,
            nano_client,
            step1_image,
            reference_face,
        )

        if step2_image is not None:
            step2_eval = await loop.run_in_executor(
                None,
                evaluate_background_change_image,
                nano_client,
                step2_image,
                reference_face,
            )
            if (
                step2_eval["similarity_percentage"]
                >= step1_eval["similarity_percentage"]
            ):
                return step2_image, step2_eval
            else:
                return step1_image, step1_eval
        return step1_image, step1_eval

    tasks = [generate_variation(i) for i in range(num_variations)]
    results = await asyncio.gather(*tasks)

    for r in results:
        if r is None:
            continue
        image, evaluation = r
        score = evaluation.get("similarity_percentage", 0.0)
        if score > best_score:
            best_score = score
            best_image = image
            best_eval = evaluation

    if best_image is None:
        return {"error": "All background change variations failed."}

    logger.info(
        f"[MCP background_changer] Complete. best_score={best_score:.1f}%"
    )
    return {
        "image_base64": base64.b64encode(best_image).decode("utf-8"),
        "evaluation": best_eval,
        "similarity_score": best_score,
    }
