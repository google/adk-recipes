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

"""Video generation utilities for glasses VTO."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

from PIL import Image
from workflows.shared.veo_utils import generate_veo as generate_veo_single

# Import shared utilities
from workflows.shared.video_utils import (
    create_mp4_from_bytes_to_bytes,
    extract_frames_as_bytes_list,
)

# Import evaluation utilities
from workflows.video_vto.glasses.glasses_eval import (
    find_color_drop_frame,
    get_index_single_person,
)

logger = logging.getLogger(__name__)

# Glasses-specific Veo model and settings
VEO_MODEL = "veo-2.0-generate-001"


MAX_VIDEOS_PER_BATCH = 4  # Veo API limit


def _generate_veo_batch(
    client,
    collage_img_bytes,
    veo_prompt,
    number_of_videos,
    duration_seconds,
    batch_id,
):
    """Generate a single batch of videos."""
    logger.info(f"Batch {batch_id}: requesting {number_of_videos} videos")

    generated_videos = generate_veo_single(
        client=client,
        image=collage_img_bytes,
        prompt=veo_prompt,
        model=VEO_MODEL,
        duration=duration_seconds,
        number_of_videos=number_of_videos,
        person_generation="allow_adult",
        enhance_prompt=False,
    )

    if generated_videos:
        logger.info(
            f"Batch {batch_id}: completed with {len(generated_videos)} videos"
        )
    else:
        logger.warning(f"Batch {batch_id}: returned no videos")

    return generated_videos


def generate_veo(
    client, collage_img, veo_prompt, total_videos=4, duration_seconds=8
):
    """Generate videos using Veo, automatically handling batching for requests > 4 videos.

    Args:
        client: The Veo client
        collage_img: The input image bytes
        veo_prompt: The prompt for video generation
        total_videos: Total number of videos to generate (default: 4)
        duration_seconds: Duration of each video (default: 8)

    Returns:
        list: Combined list of all generated video bytes

    """
    # Calculate batch sizes (e.g., 6 videos → [4, 2], 8 → [4, 4], 10 → [4, 4, 2])
    batch_sizes = []
    remaining = total_videos
    while remaining > 0:
        batch_sizes.append(min(remaining, MAX_VIDEOS_PER_BATCH))
        remaining -= MAX_VIDEOS_PER_BATCH

    logger.info(
        f"Generating {total_videos} videos in {len(batch_sizes)} batch(es): {batch_sizes}"
    )
    logger.info(f"Prompt: {veo_prompt}")

    if len(batch_sizes) == 1:
        # Single batch - no need for ThreadPoolExecutor
        return _generate_veo_batch(
            client, collage_img, veo_prompt, batch_sizes[0], duration_seconds, 1
        )

    # Multiple batches - run in parallel
    all_videos = []
    with ThreadPoolExecutor(max_workers=len(batch_sizes)) as executor:
        futures = {
            executor.submit(
                _generate_veo_batch,
                client,
                collage_img,
                veo_prompt,
                batch_size,
                duration_seconds,
                batch_id + 1,
            ): batch_id + 1
            for batch_id, batch_size in enumerate(batch_sizes)
        }

        for future in as_completed(futures):
            batch_id = futures[future]
            try:
                batch_videos = future.result()
                all_videos.extend(batch_videos)
            except Exception as e:
                logger.error(f"Batch {batch_id} failed: {e}")

    logger.info(f"Video generation completed: {len(all_videos)} total videos")
    return all_videos


def create_collage(
    model_image_bytes=None,
    glasses_image_bytes=None,
    model_side_image_bytes=None,
    target_width=3840,
    target_height=2160,
    horizontal_margin=600,
    vertical_margin=300,
    image_spacing=300,
    model_vertical_spacing=50,  # Smaller spacing between stacked model images
    bg_color=(0, 215, 6, 255),  # default as green
):
    """Create a high-quality, centered collage from one to three images.

    This function scales images to fit within the specified layout without
    cropping or distorting them, prioritizing image quality.

        model_image_bytes (bytes, optional): The byte data for the front model image.
        glasses_image_bytes (bytes, optional): The byte data for the product image.
        model_side_image_bytes (bytes, optional): The byte data for the side model image.
        target_width (int): The width of the final collage canvas.
        target_height (int): The height of the final collage canvas.
        horizontal_margin (int): The padding on the left and right of the content.
        vertical_margin (int): The padding on the top and bottom of the content.
        image_spacing (int): The space between the images, if provided.
        model_vertical_spacing (int): The space between front and side model images when stacked.
        bg_color (tuple): The RGB background color for the canvas.

    Returns:
        PIL.Image.Image: The final collage image object as bytes, or None on failure.

    """
    # --- 1. Validate Inputs ---
    if (
        model_image_bytes is None
        and glasses_image_bytes is None
        and model_side_image_bytes is None
    ):
        return None
    # --- 2. Load Image(s) from Bytes ---
    images = []
    model_front_img = None
    model_side_img = None
    glasses_img = None

    try:
        if model_image_bytes:
            model_front_img = Image.open(BytesIO(model_image_bytes))
            images.append(model_front_img)
        if model_side_image_bytes:
            model_side_img = Image.open(BytesIO(model_side_image_bytes))
            images.append(model_side_img)
        if glasses_image_bytes:
            glasses_img = Image.open(BytesIO(glasses_image_bytes))
            images.append(glasses_img)
    except Exception:
        return None
    # --- 3. Define the available area for content ---

    available_height = target_height
    available_width = target_width

    # --- 4. Handle Scaling and Positioning ---
    if len(images) == 1:
        # --- Single Image Logic ---
        img = images[0]
        if model_image_bytes is not None:
            available_height = target_height - (2 * vertical_margin)
        if glasses_image_bytes is not None:
            available_width = target_width - (2 * horizontal_margin)

        # Determine the scaling factor by the dimension that's most constrained.
        scale_factor = min(
            available_width / img.width, available_height / img.height
        )

        # Calculate the new dimensions while maintaining aspect ratio
        new_width = int(img.width * scale_factor)
        new_height = int(img.height * scale_factor)

        # Resize the image with high-quality resampling
        resized_img = img.resize(
            (new_width, new_height), Image.Resampling.LANCZOS
        )

        # Calculate position to center the image on the canvas
        paste_x = (target_width - new_width) // 2
        paste_y = (target_height - new_height) // 2

        # Create the final canvas and paste the image
        collage = Image.new("RGB", (target_width, target_height), bg_color)
        collage.paste(
            resized_img, (paste_x, paste_y), resized_img.convert("RGBA")
        )

    elif len(images) == 2:
        # --- Two Image Logic ---
        img1, img2 = images[0], images[1]

        margin = int(horizontal_margin / 4)

        # Hardcoded 100 margin in case of two pictures
        available_width = target_width - (2 * margin)

        # The available width must also account for the spacing between images
        content_width = available_width - image_spacing

        # To keep images aligned, they must have the same final height.
        ar1, ar2 = img1.width / img1.height, img2.width / img2.height

        # Calculate the max height based on width and height constraints
        max_h_from_width = content_width / (ar1 + ar2)
        final_height = int(min(available_height, max_h_from_width))

        # Calculate the final width for each image based on the new shared height
        final_w1 = int(final_height * ar1)
        final_w2 = int(final_height * ar2)

        # Resize both images
        resized1 = img1.resize(
            (final_w1, final_height), Image.Resampling.LANCZOS
        )
        resized2 = img2.resize(
            (final_w2, final_height), Image.Resampling.LANCZOS
        )

        # Calculate position to center the entire block of images
        total_content_width = final_w1 + image_spacing + final_w2
        start_x = (target_width - total_content_width) // 2
        start_y = (target_height - final_height) // 2

        # Create the canvas and paste the images
        collage = Image.new("RGB", (target_width, target_height), bg_color)
        collage.paste(resized1, (start_x, start_y), resized1.convert("RGBA"))
        collage.paste(
            resized2,
            (start_x + final_w1 + image_spacing, start_y),
            resized2.convert("RGBA"),
        )

    else:  # len(images) == 3 - Three image layout
        # --- Three Image Logic: Model front + side stacked on left, glasses on right ---

        margin = int(horizontal_margin / 4)
        available_width = target_width - (2 * margin)
        available_height = target_height - (2 * vertical_margin)

        # Content width accounts for spacing between left column and glasses
        content_width = available_width - image_spacing

        # Calculate glasses size first (same as 2-image case)
        glasses_ar = glasses_img.width / glasses_img.height

        # Model images stacked vertically - ensure same height
        model_front_ar = model_front_img.width / model_front_img.height
        model_side_ar = model_side_img.width / model_side_img.height

        # Available height for model images (minus small spacing between them)
        model_column_height = available_height - model_vertical_spacing
        individual_model_height = int(model_column_height // 2)

        # Calculate widths based on this shared height (maintaining aspect ratios)
        model_front_width = int(individual_model_height * model_front_ar)
        model_side_width = int(individual_model_height * model_side_ar)

        # Use the wider of the two model images to determine left column width
        left_column_width = max(model_front_width, model_side_width)

        # Calculate glasses dimensions to fill remaining space
        glasses_width = content_width - left_column_width
        glasses_height = int(glasses_width / glasses_ar)

        # If glasses height exceeds available height, scale down everything proportionally
        if glasses_height > available_height:
            scale_factor = available_height / glasses_height
            glasses_height = int(glasses_height * scale_factor)
            glasses_width = int(glasses_width * scale_factor)
            left_column_width = int(left_column_width * scale_factor)
            individual_model_height = int(
                individual_model_height * scale_factor
            )
            model_front_width = int(individual_model_height * model_front_ar)
            model_side_width = int(individual_model_height * model_side_ar)

        # Resize all images - both model images will have the SAME height
        resized_model_front = model_front_img.resize(
            (model_front_width, individual_model_height),
            Image.Resampling.LANCZOS,
        )
        resized_model_side = model_side_img.resize(
            (model_side_width, individual_model_height),
            Image.Resampling.LANCZOS,
        )
        resized_glasses = glasses_img.resize(
            (glasses_width, glasses_height), Image.Resampling.LANCZOS
        )

        # Calculate positioning
        total_content_width = left_column_width + image_spacing + glasses_width
        total_content_height = max(
            2 * individual_model_height + model_vertical_spacing, glasses_height
        )

        start_x = (target_width - total_content_width) // 2
        start_y = (target_height - total_content_height) // 2

        # Create canvas
        collage = Image.new("RGB", (target_width, target_height), bg_color)

        # Paste model images (centered in left column) - both have same height now
        model_front_x = start_x + (left_column_width - model_front_width) // 2
        model_side_x = start_x + (left_column_width - model_side_width) // 2

        collage.paste(
            resized_model_front,
            (model_front_x, start_y),
            resized_model_front.convert("RGBA"),
        )
        collage.paste(
            resized_model_side,
            (
                model_side_x,
                start_y + individual_model_height + model_vertical_spacing,
            ),
            resized_model_side.convert("RGBA"),
        )

        # Paste glasses (centered vertically)
        glasses_x = start_x + left_column_width + image_spacing
        glasses_y = start_y + (total_content_height - glasses_height) // 2
        collage.paste(
            resized_glasses,
            (glasses_x, glasses_y),
            resized_glasses.convert("RGBA"),
        )

    output_buffer = BytesIO()
    collage.save(output_buffer, format="PNG")
    return output_buffer.getvalue()


def process_veo_video_model_on_fit(
    video_bytes, bgcolor, fps=24, fps_to_analyze=2, start_secs_to_skip=1
):
    """Process a video by removing the initial frames that contain a green screen.

    Args:
        video_bytes (bytes): The full video file content as a bytes object.
        bgcolor (Tuple[int, int, int, int]): RGBA background color to detect and remove
        fps (int): Frames per second of the video. Default: 24.
        fps_to_analyze (int): Number of frames per second to analyze. Default: 2.
        start_secs_to_skip (int): Number of seconds to skip at the start. Default: 1.

    Returns:
        bytes: The new video content with the green screen frames removed,
               in MP4 format.

    """
    # 1. Extract frames from the input video
    frame_list = extract_frames_as_bytes_list(video_bytes)
    frames_to_skip = start_secs_to_skip * fps
    skip_frequency = fps / fps_to_analyze
    seconds_to_analyze = 3
    frames_to_analyze = frames_to_skip + seconds_to_analyze * fps

    frame_list_to_check_bg = [
        frame
        for idx, frame in enumerate(frame_list[:frames_to_analyze])
        if idx % skip_frequency == 0
    ]
    frame_list_to_check_faces = [
        frame
        for idx, frame in enumerate(
            frame_list[frames_to_skip:frames_to_analyze]
        )
        if idx % skip_frequency == 0
    ]

    # 2. Find the index where the collage background is no longer detected
    idx_bg = find_color_drop_frame(frame_list_to_check_bg, target_rgb=bgcolor)
    # 3. Find the index where exactly one person is visible
    idx_face = get_index_single_person(frame_list_to_check_faces)
    if idx_bg == -1 or idx_face == -1:
        return None

    # Adjust index to reconsider the initial frames we skipped
    idx_face += fps_to_analyze * start_secs_to_skip

    max_idx = max(idx_bg, idx_face) + 1
    original_seconds = max_idx / fps_to_analyze
    # video is too short, discard
    if original_seconds > 3:
        return None
    original_idx = int(original_seconds * fps)
    return create_mp4_from_bytes_to_bytes(
        frame_list[original_idx:], fps=24, quality=7
    )
