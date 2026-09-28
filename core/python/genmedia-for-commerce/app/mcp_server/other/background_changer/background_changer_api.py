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

"""REST API endpoint for the background changer workflow (SSE streaming)."""

import asyncio
import base64
import json
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from google import genai
from starlette.responses import StreamingResponse
from workflows.other.background_changer.background_changer import (
    evaluate_background_change_image,
    generate_background_change_only,
    preprocess_face_image,
    preprocess_person_image,
)

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

client = genai.Client(vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION)
nano_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)

router = APIRouter(
    prefix="/api/other/background-changer",
    tags=["Background Changer"],
)


@router.post("/change-background")
async def change_background_endpoint(
    person_image: UploadFile = File(...),  # noqa: B008
    background_description: str = Form(None),
    background_image: UploadFile = File(None),  # noqa: B008
    num_variations: int = Form(4),
):
    """Generate background change images with SSE streaming.

    Returns results progressively as each variation completes.
    """
    try:
        person_image_bytes = await person_image.read()
        background_image_bytes = (
            await background_image.read() if background_image else None
        )
    except Exception as e:
        logger.error(f"Error reading uploaded files: {e}")
        raise HTTPException(
            status_code=400, detail=f"Error reading uploaded files: {e}"
        ) from e

    if not background_description and not background_image_bytes:
        raise HTTPException(
            status_code=400,
            detail="Either background_description or background_image must be provided",
        )

    logger.info(f"Generating {num_variations} background change variations")

    async def generate_stream():
        try:

            async def process_face():
                return await run_in_threadpool(
                    preprocess_face_image,
                    client,
                    nano_client,
                    person_image_bytes,
                )

            async def process_person():
                return await run_in_threadpool(
                    preprocess_person_image,
                    client,
                    nano_client,
                    person_image_bytes,
                )

            face_result, preprocessed_person_image = await asyncio.gather(
                process_face(), process_person()
            )

            reference_face_processed, preprocessed_face = face_result

            if reference_face_processed is None:
                yield f"data: {json.dumps({'error': 'No face detected in the person image.'})}\n\n"
                return

            result_queue = asyncio.Queue()
            loop = asyncio.get_event_loop()

            async def generate_variation(idx):
                try:
                    gen_result = await run_in_threadpool(
                        generate_background_change_only,
                        nano_client,
                        preprocessed_person_image,
                        preprocessed_face,
                        background_description,
                        background_image_bytes,
                    )

                    if gen_result is None:
                        await result_queue.put(
                            {
                                "index": idx,
                                "image": None,
                                "error": "Generation failed",
                            }
                        )
                        return

                    step1_image = gen_result["step1_image"]
                    step2_image = gen_result["step2_image"]

                    step1_eval = await loop.run_in_executor(
                        None,
                        evaluate_background_change_image,
                        nano_client,
                        step1_image,
                        reference_face_processed,
                    )

                    if step2_image is not None:
                        step2_eval = await loop.run_in_executor(
                            None,
                            evaluate_background_change_image,
                            nano_client,
                            step2_image,
                            reference_face_processed,
                        )
                        if (
                            step2_eval["similarity_percentage"]
                            >= step1_eval["similarity_percentage"]
                        ):
                            best_image, best_eval = step2_image, step2_eval
                        else:
                            best_image, best_eval = step1_image, step1_eval
                    else:
                        best_image, best_eval = step1_image, step1_eval

                    await result_queue.put(
                        {
                            "index": idx,
                            "image": best_image,
                            "evaluation": best_eval,
                        }
                    )
                except Exception as e:
                    logger.error(
                        f"[Background Change Variation {idx}] Error: {e}"
                    )
                    await result_queue.put(
                        {"index": idx, "image": None, "error": str(e)}
                    )

            tasks = [
                asyncio.create_task(generate_variation(idx))
                for idx in range(num_variations)
            ]

            results_received = 0
            while results_received < num_variations:
                try:
                    item = await asyncio.wait_for(
                        result_queue.get(), timeout=420
                    )
                    idx = item["index"]

                    if item.get("image") is None:
                        yield f"data: {json.dumps({'index': idx, 'status': 'failed', 'error': item.get('error', 'Unknown error')})}\n\n"
                    else:
                        yield f"data: {json.dumps({'index': idx, 'status': 'ready', 'image_base64': base64.b64encode(item['image']).decode('utf-8'), 'evaluation': item['evaluation']})}\n\n"

                    results_received += 1
                except TimeoutError:
                    yield f"data: {json.dumps({'index': results_received, 'status': 'failed', 'error': 'Timeout'})}\n\n"
                    results_received += 1

            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            yield f"data: {json.dumps({'status': 'complete', 'total': results_received})}\n\n"

        except Exception as e:
            logger.error(
                f"Error in change_background_endpoint: {e}", exc_info=True
            )
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate_stream(), media_type="text/event-stream")
