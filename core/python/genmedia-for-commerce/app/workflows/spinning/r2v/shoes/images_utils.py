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

"""Image processing utilities for shoe spinning R2V."""

# Standard library imports
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-party imports
import cv2
import numpy as np
from workflows.shared.video_utils import find_most_similar_frame_index

# Project imports
from workflows.spinning.r2v.shoes.classify_shoes import classify_shoe

logger = logging.getLogger(__name__)


def detect_background_color(frame):
    """Detect the background color by sampling pixels from the borders of the frame.

    Returns the median color value (BGR format).

    Args:
        frame: numpy array (image frame)

    Returns:
        tuple: (B, G, R) color values

    """
    height, width = frame.shape[:2]

    # Sample border pixels from all four edges
    border_thickness = 5  # pixels from edge
    border_pixels = []

    # Top border
    border_pixels.append(frame[0:border_thickness, :, :].reshape(-1, 3))
    # Bottom border
    border_pixels.append(
        frame[height - border_thickness : height, :, :].reshape(-1, 3)
    )
    # Left border
    border_pixels.append(frame[:, 0:border_thickness, :].reshape(-1, 3))
    # Right border
    border_pixels.append(
        frame[:, width - border_thickness : width, :].reshape(-1, 3)
    )

    # Combine all border pixels
    all_border_pixels = np.vstack(border_pixels)

    # Calculate median color (more robust than mean against outliers)
    median_color = np.median(all_border_pixels, axis=0)

    # Convert to integers
    bg_color = tuple(int(c) for c in median_color)

    return bg_color


def detect_product_bounds_from_frames(frames, sample_frames=30):
    """Analyze a sample of frames to detect the actual product boundaries.

    Returns the bounding box that encompasses the product across all sampled frames.

    Args:
        frames: List of numpy arrays (decoded frames)
        sample_frames: Number of frames to sample for detection

    """
    if not frames:
        return None

    total_frames = len(frames)
    height, width = frames[0].shape[:2]

    # Sample frames evenly throughout the list
    frame_indices = np.linspace(
        0, total_frames - 1, min(sample_frames, total_frames), dtype=int
    )

    # Track the overall bounding box across all frames
    min_x, min_y = width, height
    max_x, max_y = 0, 0

    logger.info(
        f"  Analyzing {len(frame_indices)} frames to detect product boundaries..."
    )

    for idx in frame_indices:
        frame = frames[idx]

        # Convert to grayscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Use edge detection to find the product
        edges = cv2.Canny(gray, 50, 150)

        # Find contours
        contours, _ = cv2.findContours(
            edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        if contours:
            # Get the bounding box of all contours combined
            all_points = np.vstack(contours)
            x, y, w, h = cv2.boundingRect(all_points)

            # Update overall bounds
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + w)
            max_y = max(max_y, y + h)

    # Add a small margin (5% on each side) to avoid cutting the product
    margin_x = int((max_x - min_x) * 0.05)
    margin_y = int((max_y - min_y) * 0.05)

    min_x = max(0, min_x - margin_x)
    min_y = max(0, min_y - margin_y)
    max_x = min(width, max_x + margin_x)
    max_y = min(height, max_y + margin_y)

    product_width = max_x - min_x
    product_height = max_y - min_y

    logger.info(
        f"  Product bounds detected: {product_width}x{product_height} at ({min_x}, {min_y})"
    )

    return {
        "x": min_x,
        "y": min_y,
        "width": product_width,
        "height": product_height,
    }


def process_frames_to_target_size(frame_bytes_list, target_size=(1000, 1000)):
    """Process a list of frame bytes, detect product boundaries, and crop to maximize product size.

    Returns the processed frames as a bytelist at the specified dimensions.

    Args:
        frame_bytes_list: List of bytes objects, each representing an encoded frame (e.g., JPEG, PNG)
        target_size: Tuple of (width, height) for output dimensions (default (1000, 1000))

    Returns:
        List of bytes objects representing the processed frames (encoded as PNG)

    """
    if not frame_bytes_list:
        logger.error("❌ Error: Empty frame list")
        return []

    # Handle both tuple and single value for backwards compatibility
    if isinstance(target_size, (int, float)):
        target_width = target_height = int(target_size)
    else:
        target_width, target_height = target_size

    # Decode all frames from bytes to numpy arrays
    logger.info(f"  Decoding {len(frame_bytes_list)} frames...")
    frames = []
    for i, frame_bytes in enumerate(frame_bytes_list):
        # Convert bytes to numpy array
        nparr = np.frombuffer(frame_bytes, np.uint8)
        # Decode image
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            logger.error(f"❌ Warning: Could not decode frame {i}, skipping...")
            continue
        frames.append(frame)

    if not frames:
        logger.error("❌ Error: No frames could be decoded")
        return []

    total_frames = len(frames)
    original_height, original_width = frames[0].shape[:2]

    logger.info(
        f"  Original size: {original_width}x{original_height}, Frames: {total_frames}"
    )
    logger.info(f"  Target size: {target_width}x{target_height}")

    # Detect background color from the first frame
    bg_color = detect_background_color(frames[0])
    logger.info(f"  Detected background color (BGR): {bg_color}")

    # Detect product boundaries
    bounds = detect_product_bounds_from_frames(frames)

    if bounds is None:
        logger.info("❌ Error: Could not detect product bounds")
        return []

    # Calculate how to crop and scale the product region
    product_width = bounds["width"]
    product_height = bounds["height"]

    # Calculate scale to fit the product into target dimensions
    scale = min(target_width / product_width, target_height / product_height)

    # Calculate the final dimensions after scaling
    new_width = int(product_width * scale)
    new_height = int(product_height * scale)

    # Calculate padding to center the product in the canvas
    pad_left = (target_width - new_width) // 2
    pad_right = target_width - new_width - pad_left
    pad_top = (target_height - new_height) // 2
    pad_bottom = target_height - new_height - pad_top

    logger.info(f"  Cropping to product region, scaling by {scale:.2f}x")
    logger.info(
        f"  Output will have {pad_left + pad_right}px horizontal and {pad_top + pad_bottom}px vertical padding"
    )

    # Process all frames
    processed_frames = []
    for i, frame in enumerate(frames):
        # Crop to the product region
        cropped_frame = frame[
            bounds["y"] : bounds["y"] + bounds["height"],
            bounds["x"] : bounds["x"] + bounds["width"],
        ]

        # Resize the cropped region to fit within target size
        resized_frame = cv2.resize(
            cropped_frame,
            (new_width, new_height),
            interpolation=cv2.INTER_LANCZOS4,
        )

        # Add padding with detected background color to create final dimensions
        padded_frame = cv2.copyMakeBorder(
            resized_frame,
            pad_top,
            pad_bottom,
            pad_left,
            pad_right,
            cv2.BORDER_CONSTANT,
            value=bg_color,  # Use detected background color (BGR format)
        )

        # Encode frame back to bytes (PNG format for lossless quality)
        success, encoded_frame = cv2.imencode(".png", padded_frame)
        if not success:
            logger.info(f"❌ Warning: Could not encode frame {i}, skipping...")
            continue

        processed_frames.append(encoded_frame.tobytes())

        if (i + 1) % 30 == 0:
            logger.info(f"  Progress: {i + 1}/{total_frames} frames")

    logger.info(f"✅ Successfully processed {len(processed_frames)} frames")
    return processed_frames


def classify_frames(sampled_frames, client, model, mode="normal"):
    """Classify a list of sampled frames using parallel processing.

    Args:
        sampled_frames: List of sampled frame bytes.
        client: Gemini client.
        model: Model for classification.
        mode: Classification mode. Default: "normal".

    Returns:
        List of classification labels.

    """
    max_workers = min(20, len(sampled_frames))
    product_position_frames = [None] * len(sampled_frames)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                classify_shoe, sampled_frames[i], client, model, mode
            ): i
            for i in range(len(sampled_frames))
        }

        for future in as_completed(futures):
            index = futures[future]
            product_position_frames[index] = future.result()

    return product_position_frames


def sample_and_process_frames(
    frame_list: list[bytes],
    target_num_frames: int = 50,
    target_size: tuple[int, int] | None = None,
    initial_class: str | None = None,
    reference_images: list[bytes] | None = None,
    reference_labels: list[str] | None = None,
    client=None,
    gemini_model=None,
) -> list[bytes]:
    """Sample frames evenly from a frame list and optionally resize them.

    This utility avoids re-extracting frames from video when they're already available.

    Args:
        frame_list: List of frame bytes (already extracted from video)
        target_num_frames: Number of frames to sample (default: 50)
        target_size: Optional target size as (width, height) to resize frames
        initial_class: Optional initial class to start the video with.
        reference_images: Optional list of reference image bytes.
        reference_labels: Optional list of reference labels.
        client: Optional client for classification.
        gemini_model: Optional model for classification.

    Returns:
        List of sampled (and optionally resized) frame bytes

    """
    total_frames = len(frame_list)

    # Sample frames evenly
    if total_frames <= target_num_frames:
        sampled_frames = frame_list
    else:
        indices = [
            int(i * (total_frames - 1) / (target_num_frames - 1))
            for i in range(target_num_frames)
        ]
        sampled_frames = [frame_list[i] for i in indices]

    # Make sure it starts on initial
    if initial_class:
        new_frame_order = []
        initial_frame_index = 0

        if (
            classify_shoe(
                sampled_frames[0], client, gemini_model, mode="validation"
            )
            != initial_class
        ):
            product_position = classify_frames(
                sampled_frames, client, gemini_model, mode="validation"
            )

            sampled_frames_initial_class = [
                (idx, frame)
                for idx, (frame, position) in enumerate(
                    zip(sampled_frames, product_position, strict=True)
                )
                if position == initial_class
            ]
            frames = [frame for _, frame in sampled_frames_initial_class]

            # Reference images has a initial class example
            if (
                reference_images
                and reference_labels
                and initial_class in reference_labels
            ):
                reference_frame = reference_images[
                    reference_labels.index(initial_class)
                ]
                initial_frame_index_class = find_most_similar_frame_index(
                    frames, reference_frame
                )
                initial_frame_index = sampled_frames_initial_class[
                    initial_frame_index_class
                ][0]
            # Reference images has NOT a initial class example
            elif sampled_frames_initial_class:
                indices = [idx for idx, _ in sampled_frames_initial_class]
                if len(indices) == 1:
                    initial_frame_index = indices[0]
                else:
                    # Check if indices wrap around (e.g., [0, 1, 2, 47, 48, 49])
                    gaps = [
                        indices[i + 1] - indices[i]
                        for i in range(len(indices) - 1)
                    ]
                    max_gap_idx = gaps.index(max(gaps)) if gaps else -1

                    if max_gap_idx >= 0 and gaps[max_gap_idx] > 1:
                        wrapped_sequence = (
                            indices[max_gap_idx + 1 :]
                            + indices[: max_gap_idx + 1]
                        )
                        middle_idx_in_wrapped = len(wrapped_sequence) // 2
                        initial_frame_index = wrapped_sequence[
                            middle_idx_in_wrapped
                        ]
                    else:
                        middle_idx = len(indices) // 2
                        initial_frame_index = indices[middle_idx]

            if initial_frame_index != 0:
                new_frame_order = (
                    sampled_frames[initial_frame_index:]
                    + sampled_frames[:initial_frame_index]
                )
                sampled_frames = new_frame_order

    # Resize if target size is specified
    if target_size:
        sampled_frames = process_frames_to_target_size(
            sampled_frames, target_size
        )

    converted_frames = []
    for frame_bytes in sampled_frames:
        # Decode frame from bytes (PNG from process_frames_to_target_size)
        nparr = np.frombuffer(frame_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is not None:
            # Re-encode as JPEG with 95% quality
            success, encoded_frame = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95]
            )
            if success:
                converted_frames.append(encoded_frame.tobytes())
            else:
                # Fallback to original if encoding fails
                converted_frames.append(frame_bytes)
        else:
            # Fallback to original if decoding fails
            converted_frames.append(frame_bytes)

    sampled_frames = converted_frames

    return sampled_frames


def image_closure_selection(bytes_list, images_classes):
    """Select images for closure classification based on desirable views.

    Args:
        bytes_list: List of image bytes.
        images_classes: List of class labels for the images.

    Returns:
        List of selected image bytes.

    """
    desirable_classifications = [
        "top_front",
        "front",
        "front_right",
        "front_left",
        "left",
        "right",
        "back_left",
        "back_right",
    ]

    final_images = []

    for el in desirable_classifications:
        if len(final_images) > 3:
            break
        if el in images_classes:
            ind = images_classes.index(el)
            final_images.append(bytes_list[ind])

    return final_images
