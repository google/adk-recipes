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

"""Utilities for splitting images containing multiple shoes."""

import io
import logging
import time

import cv2
import numpy as np
from google.genai import types
from google.genai.types import SegmentImageSource
from PIL import Image as PImage

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DIVISION_PROMPT = """
Segment both footwears in the image with precise definition.
"""


def sort_masks_by_horizontal_position(
    mask_data_list: list, mode: str = "right", verbose: bool = False
) -> list:
    """Sorts a list of (mask, value) tuples based on the mask's horizontal position.

    The score is calculated by summing the transformed x-coordinates of
    all positive pixels ('True' values) in the mask. The transformation is:

    transformed_x = (pixel_x_coordinate - image_middle_x)

    - A large mask on the RIGHT will have a large POSITIVE score.
    - A large mask on the LEFT will have a large NEGATIVE score.

    Args:
        mask_data_list: A list of tuples, where each tuple is
                        (boolean_mask, any_value). The boolean_mask is a
                        2D boolean array (list of lists). The any_value
                        is carried along but not used for sorting.
        mode: The sorting direction.
            - 'right': Sorts "rightmost" first (highest score).
            - 'left':  Sorts "leftmost" first (lowest score).
        verbose: If True, prints status messages.

    Returns:
        A new list containing the same (mask, value) tuples,
        sorted based on the mask's horizontal position.

    """
    if not mask_data_list:
        return []

    # --- 1. Validate Mode ---
    if mode not in ["right", "left"]:
        if verbose:
            print(f"Warning: Invalid mode '{mode}'. Defaulting to 'right'.")
        mode = "right"

    # --- 2. Get image dimensions ---
    try:
        # Get the mask from the first tuple to find dimensions
        first_mask = mask_data_list[0][0]

        len(first_mask)
        img_w = len(first_mask[0])
        middle_x = img_w / 2.0

    except (IndexError, TypeError, AttributeError):
        if verbose:
            print(
                "Warning: Could not determine mask dimensions. Skipping sort."
            )
        return mask_data_list

    if img_w == 0:
        if verbose:
            print("Warning: Mask width is 0. Skipping sort.")
        return mask_data_list

    # --- 3. Define the 'key' function for sorting ---
    def calculate_horizontal_score(mask_data_tuple: tuple) -> float:
        """Calculate the score for a single mask extracted from the (mask, value) tuple."""
        try:
            # Extract the mask (first element) from the tuple
            bool_mask = mask_data_tuple[0]

            np_mask = np.array(bool_mask, dtype=bool)
            # [1] gets the x-coordinates (column indices)
            x_coords = np.where(np_mask)[1]

            if x_coords.size == 0:
                # If mask is empty, give it the "worst" score
                # so it goes to the end of the list.
                return -float("inf") if mode == "right" else float("inf")

            # Sum all (x - middle_x) values
            score = np.sum(x_coords - middle_x)
            return float(score)

        except Exception as e:
            # Handle malformed masks
            if verbose:
                print(f"Warning: Could not process a mask. Error: {e}")
            return -float("inf") if mode == "right" else float("inf")

    # --- 4. Determine sorting direction ---
    # 'right' mode (highest score first) -> reverse=True
    # 'left' mode (lowest score first) -> reverse=False
    sort_reverse = mode == "right"

    # --- 5. Sort the list ---
    # sorted() will pass each item (the full tuple) from mask_data_list
    # to the calculate_horizontal_score key function.
    sorted_data = sorted(
        mask_data_list, key=calculate_horizontal_score, reverse=sort_reverse
    )

    if verbose:
        print(
            f"Sorted {len(sorted_data)} mask/data tuples by '{mode}most' score."
        )
    return sorted_data


def subtract_masks(mask_a: np.ndarray, mask_b: np.ndarray) -> np.ndarray:
    """Perform a set subtraction operation on two binary masks.

    Returns a mask containing only the regions present in mask_a that are NOT in mask_b.

    Args:
        mask_a (np.ndarray): The primary mask.
        mask_b (np.ndarray): The mask to subtract from the primary mask.

    Returns:
        np.ndarray: The resulting subtracted mask.

    """
    subtraction = mask_a & (~mask_b)

    return subtraction


def _prepare_mask(mask: np.ndarray) -> np.ndarray:
    """Standardize a mask into a binary 8-bit, single-channel image (values 0 or 255).

    Crucial for ensuring OpenCV functions work predictably.
    """
    if mask.ndim > 2:
        # Convert to grayscale if it's 3-channel
        mask = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY)

    if mask.dtype != np.uint8 or mask.max() <= 1:
        # Convert from bool or 0/1 to 0/255 uint8
        mask = (mask > 0).astype(np.uint8) * 255

    # Ensure it's strictly binary (0 or 255)
    _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    return mask


def check_contour_count(mask: np.ndarray, max_allowed: int = 3) -> bool:
    """Check if the mask contains exactly one distinct object.

    This is useful for discarding empty masks (0 contours) or
    fragmented masks (2+ contours).

    Args:
        mask (np.ndarray): The binary segmentation mask (H, W).
        max_allowed (int): Maximum allowed number of contours. Default: 3.

    Returns:
        bool: True if the mask has exactly one contour, False otherwise.

    """
    mask = _prepare_mask(mask)

    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    return len(contours) <= max_allowed


def filter_by_score_distribution(
    masks: list, minimum: int = 3
) -> tuple[list, float]:
    """Filter a list of (mask, score) tuples, keeping only those in the top 30% of scores.

    Ensures a minimum number of masks are always returned, even if they fall below the threshold.

    Args:
        masks (list): A list of tuples, e.g., [(mask_array, score_float), ...].
        minimum (int): The minimum number of masks to return to avoid returning too few results.

    Returns:
        tuple: (filtered_masks_list, score_threshold_used)

    """
    scores = [item[1] for item in masks]
    score_threshold = np.percentile(scores, 70)

    top_results = [item for item in masks if item[1] >= score_threshold]

    if len(top_results) < minimum:
        top_results = masks[:minimum]

    return top_results, score_threshold


def construct_image(image: PImage.Image, mask: PImage.Image) -> PImage.Image:
    """Create a new image where the masked area is visible and the rest is white.

    The image is cropped to the mask's bounding box to center the extracted object.

    Args:
        image (PIL.Image): The original source image.
        mask (PIL.Image): The mask to apply as an alpha channel.

    Returns:
        PIL.Image: An RGBA image with white background, cropped to mask bounds.

    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    # Find the bounding box of the mask
    bbox = mask.getbbox()

    if bbox is None:
        # If mask is empty, return a small white image
        return PImage.new("RGBA", (1, 1), (255, 255, 255, 255))

    # Crop both image and mask to the bounding box
    cropped_image = image.crop(bbox)
    cropped_mask = mask.crop(bbox)

    # Create white background with cropped dimensions
    white_background = PImage.new(
        "RGBA", cropped_image.size, (255, 255, 255, 255)
    )
    white_background.paste(cropped_image, mask=cropped_mask)

    return white_background


def divide_duplicate_image(
    image_bytes: bytes, client, verbose: bool = False, return_masks=False
) -> list:
    """Segment an image based on a prompt, identify duplicate objects, and separate them.

    Args:
        image_bytes (bytes): Raw image data.
        client: GenAI client.
        verbose (bool): If True, displays intermediate debug images.
        return_masks (bool): If True, returns masks instead of images.

    Returns:
        list: A list of cleaned, non-overlapping final masks or images.

    """
    original_image = PImage.open(io.BytesIO(image_bytes))
    source = SegmentImageSource(
        image=types.Image(image_bytes=image_bytes), prompt=DIVISION_PROMPT
    )
    sleep_time = 0
    retry_num = -1
    while True:
        try:
            response = client.models.segment_image(
                model="image-segmentation-001",
                source=source,
                config=types.SegmentImageConfig(
                    mode="prompt", confidenceThreshold=0
                ),
            )
            break
        except Exception as e:
            if hasattr(e, "code") and e.code in (401, 403, 404):
                logger.error(
                    f"Non-retryable error ({e.code}) during segmentation: {e}"
                )
                raise
            sleep_time += 10
            retry_num += 1
            time.sleep(sleep_time)
            if retry_num >= 100:
                return []
            logger.error(
                f"Error during segmentation: {e}, waiting: {sleep_time}"
            )
    # Extract boolean masks and scores from model response
    masks = [
        (
            np.array(mask.mask._pil_image.convert("L")) > 0,
            round(float(mask.labels[0].score), 5),
        )
        for mask in response.generated_masks
    ]

    if verbose:
        print(f"Number of masks retrieved {len(masks)}")

    masks, threshold = filter_by_score_distribution(masks)

    if verbose:
        print(
            f"Calculated threshold {threshold}, Number of masks selected  {len(masks)}"
        )

    masks = sort_masks_by_horizontal_position(masks, "right")

    if verbose:
        print("Sorted Masks")

    right_mask = masks[0]
    left_mask = masks[-1]

    final_images = []
    final_right = subtract_masks(right_mask[0], left_mask[0])
    final_left = subtract_masks(left_mask[0], right_mask[0])

    if np.sum(final_right) >= 10 and check_contour_count(
        final_right, max_allowed=1
    ):
        final_images.append(final_right)
    if np.sum(final_left) >= 10 and check_contour_count(
        final_left, max_allowed=1
    ):
        final_images.append(final_left)

    if return_masks:
        return final_images

    final_images2 = []
    for mask in final_images:
        mask_pil = PImage.fromarray((mask * 255).astype("uint8"), mode="L")

        # Construct image with mask applied
        split_image = construct_image(original_image, mask_pil)

        # Convert to bytes with highest quality PNG settings
        split_buffer = io.BytesIO()
        split_image.save(
            split_buffer, format="PNG", compress_level=1, optimize=False
        )
        final_images2.append(split_buffer.getvalue())
    return final_images2
