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

"""Shared utilities for Veo video generation.

Supports image-to-video, interpolation (start/end frames), and R2V (reference images) modes.
"""

# Standard library imports
import time

# Third-party imports
from google.genai import types
from google.genai.errors import ClientError
from google.genai.types import Image, VideoGenerationReferenceImage

# Project imports
from workflows.shared.llm_utils import retry_with_exponential_backoff


class VeoEmptyResultError(Exception):
    """Raised when Veo returns an empty result."""

    pass


@retry_with_exponential_backoff(
    max_retries=5, exceptions=(ClientError, VeoEmptyResultError)
)
def generate_veo(
    client,
    image,
    prompt,
    last_frame=None,
    model="veo-3.1-fast-generate-001",
    duration=8,
    number_of_videos=1,
    aspect_ratio="16:9",
    person_generation=None,
    enhance_prompt=None,
    generate_audio=False,
):
    """Generate video using Veo from a starting image.

    Supports two modes:
    - Image-to-video: Only provide `image`, Veo animates from that frame
    - Interpolation: Provide both `image` and `last_frame`, Veo transitions between them

    Args:
        client: Veo client instance
        image: Starting image as bytes
        prompt: Text prompt for video generation
        last_frame: Optional end frame image as bytes (enables interpolation mode)
        model: Model to use (default: "veo-3.1-fast-generate-001")
        duration: Video duration in seconds (default: 8)
        number_of_videos: Number of videos to generate (default: 1)
        aspect_ratio: Video aspect ratio (default: "16:9")
        person_generation: Person generation setting (e.g., "allow_adult")
        enhance_prompt: Whether to enhance the prompt (True/False)
        generate_audio: Whether to generate audio (default: False)

    Returns:
        list[bytes]: List of generated video bytes, or empty list if generation fails

    """
    first_frame = Image(imageBytes=image, mime_type="image/png")

    # Build config
    config_kwargs = {
        "aspect_ratio": aspect_ratio,
        "number_of_videos": number_of_videos,
        "duration_seconds": duration,
        "generate_audio": generate_audio,
    }

    # Add optional parameters only if specified
    if last_frame is not None:
        config_kwargs["last_frame"] = Image(
            imageBytes=last_frame, mime_type="image/png"
        )
    if person_generation is not None:
        config_kwargs["person_generation"] = person_generation
    if enhance_prompt is not None:
        config_kwargs["enhance_prompt"] = enhance_prompt

    operation = client.models.generate_videos(
        model=model,
        prompt=prompt,
        image=first_frame,
        config=types.GenerateVideosConfig(**config_kwargs),
    )

    while not operation.done:
        time.sleep(1)
        operation = client.operations.get(operation)

    if operation.response and operation.result.generated_videos:
        return [
            video.video.video_bytes
            for video in operation.result.generated_videos
        ]

    # Raise exception to trigger retry decorator
    raise VeoEmptyResultError("Veo returned empty result - no videos generated")


@retry_with_exponential_backoff(
    max_retries=5, exceptions=(ClientError, VeoEmptyResultError)
)
def generate_veo_r2v(
    client,
    reference_images,
    prompt,
    reference_type="asset",
    model="veo-3.1-fast-generate-001",
    duration=8,
    generate_audio=False,
    person_generation=None,
    seed=None,
    aspect_ratio="16:9",
):
    """Generate video using Veo with reference images (R2V mode).

    Args:
        client: Veo client instance
        reference_images: List of reference image bytes
        prompt: Text prompt for video generation
        reference_type: Type of reference - "asset" or "style" (default: "asset")
        model: Model to use (default: "veo-3.1-fast-generate-001")
        duration: Video duration in seconds (default: 8)
        generate_audio: Whether to generate audio (default: False)
        person_generation: Person generation setting (e.g., "allow_adult")
        seed: Random seed (default: None, let Veo choose)
        aspect_ratio: Video aspect ratio (default: "16:9")

    Returns:
        bytes: Generated video data

    """
    ref_images_list = []
    for img_bytes in reference_images:
        ref_image = VideoGenerationReferenceImage(
            image=Image(imageBytes=img_bytes, mime_type="image/png"),
            reference_type=reference_type,
        )
        ref_images_list.append(ref_image)

    config_kwargs = {
        "aspect_ratio": aspect_ratio,
        "number_of_videos": 1,
        "duration_seconds": duration,
        "generate_audio": generate_audio,
        "reference_images": ref_images_list,
    }
    if seed is not None:
        config_kwargs["seed"] = seed
    if person_generation is not None:
        config_kwargs["person_generation"] = person_generation

    operation = client.models.generate_videos(
        model=model,
        prompt=prompt,
        config=types.GenerateVideosConfig(**config_kwargs),
    )

    while not operation.done:
        time.sleep(1)
        operation = client.operations.get(operation)

    if operation.response and operation.result.generated_videos:
        return operation.result.generated_videos[0].video.video_bytes

    # Raise exception to trigger retry decorator
    raise VeoEmptyResultError(
        "Veo R2V returned empty result - no videos generated"
    )
