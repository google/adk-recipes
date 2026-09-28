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

"""Utilities for Interpolation mode video generation.

Generates videos by interpolating between consecutive frames.
"""

import os

# Standard library imports
# Project imports
from workflows.shared.gemini import generate_gemini
from workflows.shared.veo_utils import generate_veo as generate_veo_shared
from workflows.shared.video_utils import (
    convert_image_to_video_frame,
    create_mp4_from_bytes_to_bytes,
    extract_frames_as_bytes_list,
    find_most_similar_frame_index,
)


def generate_generic_product_title(
    client,
    gemini_model: str = os.getenv("MODEL_NAME_GENERATED_2"),
    all_images_bytes: list[bytes] | None = None,
) -> str:
    """Generate a very generic product title from images.

    Args:
        client: Gemini client for generation
        gemini_model: Model to use for title generation
        all_images_bytes: List of image bytes to analyze

    Returns:
        str: Generic product title (e.g., "a smartphone", "a t-shirt", "a discovery SUV")

    """
    system_prompt = """You are an expert in product categorization. Your role is to return a very generic, short title for the product you see in the images.

The title should be extremely simple and generic, containing ONLY the product type/category. Do NOT include:
- Colors (no "black", "white", "red", etc.)
- Brands (no "Nike", "Apple", "Samsung", etc.)
- Materials (no "leather", "cotton", "plastic", etc.)
- Specific models or styles
- Any other descriptive details

Example outputs:
- "a smartphone"
- "a discovery SUV"
- "a t-shirt"
- "a sneaker"
- "a watch"
- "a laptop"
- "a backpack"
- "a mug"

Return ONLY the generic title starting with "a" or "an", nothing else.
"""
    config = {
        "temperature": 0,
        "max_output_tokens": 50,
        "thinking_config": {"thinking_budget": 0},
        "system_instruction": system_prompt,
    }

    text_part = ["Return a generic title for this product: "]
    return generate_gemini(
        text_images_pieces=(text_part + all_images_bytes),
        client=client,
        config=config,
        model=gemini_model,
    )


def get_interpolation_prompt(
    client,
    gemini_model: str = os.getenv("MODEL_NAME_GENERATED_2"),
    all_images_bytes: list[bytes] | None = None,
):
    """Generate the prompt used for interpolation video generation with a generic product title.

    Args:
        client: Gemini client for title generation
        gemini_model: Model to use for title generation (default: "gemini-3.5-flash-lite")
        all_images_bytes: List of image bytes to analyze

    Returns:
        str: Complete interpolation prompt with generated product title

    """
    product_title = generate_generic_product_title(
        client, gemini_model=gemini_model, all_images_bytes=all_images_bytes
    )

    return f"""[Subject]: {product_title.strip()} rotating clockwise in a perfect white void.

**[Action]:** The camera performs **one continuous, seamless orbit** around the stationary product. The camera movement is perfectly smooth and steady, maintaining a constant distance and speed throughout the entire clip. The product does not move or rotate; only the camera moves.

**[Scene]:** A completely white studio void (Hex: #FFFFFF, RGB: 255, 255, 255). The only visible element is the product, nothing else."""


def generate_veo(client, start_img, end_img, veo_prompt):
    """Generate video using Veo interpolation mode.

    Creates a video that transitions smoothly from start_img to end_img
    using Veo's interpolation capabilities.

    Args:
        client: Veo client instance
        start_img: Starting frame image as bytes
        end_img: Ending frame image as bytes
        veo_prompt: Text prompt describing the desired video transition

    Returns:
        bytes: Generated video as bytes, or None if generation fails

    """
    videos = generate_veo_shared(
        client=client,
        image=start_img,
        prompt=veo_prompt,
        last_frame=end_img,
        model="veo-3.1-generate-001",
        duration=4,
        number_of_videos=1,
    )
    return videos[0] if videos else None


def post_process_single_video(
    video_bytes: bytes,
    end_image: bytes,
    num_frames_for_similarity=15,
    is_first_video=False,
):
    """Post-process a generated video by trimming to the most similar end frame.

    Finds the frame most similar to the target end image and trims the video
    at that point. Optionally removes the first frame for seamless concatenation.

    Args:
        video_bytes: Raw video data as bytes
        end_image: Target end frame image as bytes
        num_frames_for_similarity: Number of frames from the end to search
                                   for similarity match (default: 15)
        is_first_video: If True, keeps the first frame; if False, removes it
                        for seamless video concatenation (default: False)

    Returns:
        bytes: Processed video as MP4 bytes

    Raises:
        ValueError: If no frames could be extracted from the video

    """
    try:
        frames_list = extract_frames_as_bytes_list(video_bytes)

        if not frames_list:
            raise ValueError("Failed to extract any frames.")

        end_interpolated = convert_image_to_video_frame(
            frames_list[0], end_image
        )

        top_similar_idx_end = find_most_similar_frame_index(
            frames_list,
            end_interpolated,
            num_frames_to_check=num_frames_for_similarity,
        )

        last_valid_frame_idx = (
            len(frames_list) - num_frames_for_similarity + top_similar_idx_end
        )
        frames_list = frames_list[:last_valid_frame_idx]

        if not is_first_video:
            frames_list = frames_list[1:]

        video_bytes = create_mp4_from_bytes_to_bytes(frames_list)
        return video_bytes
    except Exception as e:
        print(f"CRITICAL ERROR in post_process_single_video: {e}")
        raise


def process_single_video(
    client,
    start_image: bytes,
    end_image: bytes,
    prompt: str,
    index: int,
    num_frames_for_similarity=15,
    background_color: str = "#FFFFFF",
):
    """Generate and post-process a single video segment.

    Combines video generation and post-processing into a single operation.
    Used for creating individual segments in a multi-segment video pipeline.
    Retry logic is handled by generate_veo_shared (5 retries for Veo errors).

    Args:
        client: Veo client instance
        start_image: Starting frame image as bytes
        end_image: Ending frame image as bytes
        prompt: Text prompt for video generation
        index: Segment index (0 for first video, affects frame trimming)
        num_frames_for_similarity: Frames to search for end similarity (default: 15)
        background_color: Background color hex code (default: "#FFFFFF")

    Returns:
        bytes: Processed video segment as MP4 bytes

    """
    veo_video = generate_veo(client, start_image, end_image, prompt)
    video = post_process_single_video(
        video_bytes=veo_video,
        end_image=end_image,
        num_frames_for_similarity=num_frames_for_similarity,
        is_first_video=(index == 0),
    )
    return video
