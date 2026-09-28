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

"""Selection of shoe images based on classified views."""

import io
from collections import defaultdict

import numpy as np
from PIL import Image as PImage

class_order = [
    "right",
    "front_right",
    "front",
    "front_left",
    "left",
    "back_left",
    "back",
    "back_right",
]


def get_non_white_pixel_count(img_bytes: bytes) -> int:
    """Count non-white pixels in an image as a quality metric.

    More non-white pixels = more complete shoe (less background).
    This helps select the most complete shoe when multiple shoes
    have the same classification.

    Args:
        img_bytes: Image data as bytes

    Returns:
        int: Number of non-white pixels (higher = better/more complete)

    """
    try:
        img = PImage.open(io.BytesIO(img_bytes))

        # Convert to RGB if necessary
        if img.mode == "RGBA":
            # Create a white background
            background = PImage.new("RGB", img.size, (255, 255, 255))
            # Paste using alpha channel as mask
            background.paste(img, mask=img.split()[3])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img_array = np.array(img)

        # Count pixels that aren't white (255, 255, 255)
        # A pixel is white if R=255 AND G=255 AND B=255
        is_white = (
            (img_array[:, :, 0] == 255)
            & (img_array[:, :, 1] == 255)
            & (img_array[:, :, 2] == 255)
        )
        non_white_count = np.sum(~is_white)

        return int(non_white_count)
    except Exception:
        # If image can't be opened, return 0 (lowest priority)
        return 0


def can_generate_views(labels):
    """Check if the labels contain enough views to generate a video.

    Args:
        labels: List of class labels.

    Returns:
        bool: True if enough views are present, False otherwise.

    """
    views_set = set()
    for label in labels:
        if "front" in label and label != "top_front":
            views_set.add("front")
        if "back" in label:
            views_set.add("back")
        if "left" in label:
            views_set.add("left")
        if "right" in label:
            views_set.add("right")
    if len(views_set) == 4:
        return True
    return False


def classify_video_gen_status(labels):
    """Classify the video generation status based on available views.

    Args:
        labels: List of class labels.

    Returns:
        list or str: List of ordered labels if successful, "exclude" otherwise.

    """
    if can_generate_views(labels):
        order = []
        for label in class_order:
            if label in labels:
                order.append(label)
        return order
    return "exclude"


def filer_top_four_views(selected_images):
    """Filter and return up to 4 images in order: right, left, front, back.

    Selection priority:
    - 1st (right): right, front_right, back_right
    - 2nd (left): left, front_left, back_left
    - 3rd (front): front, front_left, front_right
    - 4th (back): back, back_left, back_right

    Ensures no view is selected twice.
    """
    # Create a dict for quick lookup
    side_to_image = {side: img for img, side in selected_images}

    # Define selection priority for each slot
    priority_orders = [
        ["right", "front_right", "back_right"],  # 1st: best right
        ["left", "front_left", "back_left"],  # 2nd: best left
        [
            "front",
            "front_left",
            "front_right",
            "top_front",
        ],  # 3rd: best front (top_front as last resort)
        ["back", "back_left", "back_right"],  # 4th: best back
    ]

    result = []
    used_views = set()

    for priority_list in priority_orders:
        for view in priority_list:
            if view in side_to_image and view not in used_views:
                result.append([side_to_image[view], view])
                used_views.add(view)
                break

    return result


def pick_images_by_ordered_best_side(
    images_classified: list[tuple[bytes, str]],
) -> list[tuple[any, str]]:
    """Select the images for each available ordered side.

    In case of duplication, it selects the most complete image (highest non-white pixel count).

    Args:
        images_classified: A List of Tuple containing image_bytes and classified side

    Returns:
        aselect images: A list of tuples, where each tuple is the identification of the product and the position.

    """
    selected_images = []

    side_image_dict = defaultdict(list)
    for img, side in images_classified:
        side_image_dict[side].append(img)

    available_side = [
        x for x in [*class_order, "top_front"] if x in side_image_dict
    ]
    for side in available_side:
        # If multiple images for the same side, pick the most complete one
        if len(side_image_dict[side]) > 1:
            # Select image with highest non-white pixel count (most complete)
            best_img = max(side_image_dict[side], key=get_non_white_pixel_count)
            selected_images.append([best_img, side])
        else:
            # Only one image for this side
            selected_images.append([side_image_dict[side][0], side])

    return filer_top_four_views(selected_images)
