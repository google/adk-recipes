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

"""R2V spinning pipeline for generic products."""

import logging
import os
import time

from google import genai
from google.genai import types
from google.genai.types import Image, VideoGenerationReferenceImage

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

veo_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)


def generate_video_r2v(reference_images_bytes, prompt, index):
    """Generate R2V video (blocking, runs in threadpool)."""
    logger.info(
        f"[R2V] Generating video {index} with {len(reference_images_bytes)} reference images"
    )

    reference_image_sources = []
    for img_bytes in reference_images_bytes:
        ref_image = VideoGenerationReferenceImage(
            image=Image(imageBytes=img_bytes, mime_type="image/png"),
            reference_type="asset",
        )
        reference_image_sources.append(ref_image)

    operation = veo_client.models.generate_videos(
        model="veo-3.1-generate-001",
        prompt=prompt,
        config=types.GenerateVideosConfig(
            aspect_ratio="16:9",
            number_of_videos=1,
            duration_seconds=8,
            generate_audio=False,
            reference_images=reference_image_sources,
        ),
    )

    while not operation.done:
        time.sleep(2)
        operation = veo_client.operations.get(operation)

    if not operation.response:
        raise Exception("Video generation failed - no response from API")

    video_bytes = operation.result.generated_videos[0].video.video_bytes

    if not video_bytes:
        raise Exception("R2V video generation returned empty bytes.")

    logger.info(f"[R2V] Video {index} complete")
    return video_bytes
