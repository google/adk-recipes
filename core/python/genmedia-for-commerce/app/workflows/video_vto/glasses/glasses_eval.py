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

"""Video quality evaluation for glasses VTO.

Includes color detection, face detection, and video quality checks.
"""

import json
import logging
import os
import re

import cv2
import numpy as np
from google.cloud import vision
from google.cloud.vision_v1.types import AnnotateImageRequest, Feature
from google.genai import types
from workflows.shared.llm_utils import get_generate_content_config, get_part

logger = logging.getLogger(__name__)


VIDEO_QC_SYSTEM_PROMPT = """You are a Digital Content QC Specialist for a high-end fashion brand. Your task is to review these silent video clips, which are used on e-commerce product pages, to ensure they are polished and ready for public display.

**IMPORTANT CONTEXT & BASELINE:**
These videos are intentionally minimalist and silent. Slow and deliberate model movements, and a complete lack of audio are the brand's standard style. **These elements must NOT be flagged as anomalies.**

Your goal is to identify true errors, unfinished elements, and surreal visual effects that are out of place with the brand's clean, realistic aesthetic. Please categorize your findings as follows:

1. **Production Artifacts & Errors:** Look for "behind-the-scenes" elements that indicate an unfinished or glitched video. This includes:
* Visible green screens or blue screens.
* Nonsense placeholder text.
* Editing software interface elements, thumbnails, or watermarks.
* Abrupt glitches, pixelation, or sudden drops in video quality.

2. **Surreal or Incongruous Visual Effects (VFX):** Identify any animated graphics, floating shapes, or fluid-like simulations overlaid on the video that break from the realistic presentation of the product.

For each anomaly found, please provide:
- A flag True if the video is anomalous, False otherwise:
- A brief explanation of the why

Your responses must be provided in the following JSON format:
{
"is_glitched": true,
"reason": "The video contains significant visual artifacts, including screen tearing and color distortion, making the content unwatchable."
}
"""


def check_video_for_glitches(client, video_bytes):
    """Use Gemini to analyze a video for glitches and production artifacts.

    Args:
        client: The Gemini client instance
        video_bytes: The video data to analyze (bytes)

    Returns:
        dict: Dictionary containing 'is_glitched' (bool) and 'reason' (str)
        Returns None if analysis fails

    """
    contents = [
        types.Content(
            role="user",
            parts=[
                get_part(video_bytes),
                get_part("Analyze this video for quality issues and glitches."),
            ],
        ),
    ]

    config = get_generate_content_config(
        temperature=1, response_modalities=["TEXT"]
    )

    # Add thinking config and system instruction
    config.thinking_config = types.ThinkingConfig(thinking_budget=-1)
    config.system_instruction = [get_part(VIDEO_QC_SYSTEM_PROMPT)]

    response_text = ""
    for chunk in client.models.generate_content_stream(
        model=os.getenv("MODEL_NAME_GENERATED_3"),
        contents=contents,
        config=config,
    ):
        response_text += chunk.text

    # Parse JSON response
    try:
        # Extract JSON from response (in case there's extra text)
        json_match = re.search(
            r'\{[^}]*"is_glitched"[^}]*\}', response_text, re.DOTALL
        )
        if json_match:
            json_str = json_match.group(0)
            result = json.loads(json_str)
            return result
        else:
            logger.warning(
                f"No valid JSON found in Gemini response: {response_text}"
            )
            return None
    except json.JSONDecodeError as e:
        logger.error(
            f"Failed to parse Gemini JSON response: {e}, response: {response_text}"
        )
        return None


# --- Color Detection Functions ---


def detect_color_background(
    image_bytes: bytes,
    target_rgb: tuple[int, int, int],
    saturation_min: float = 0.3,
    value_min: float = 0.3,
) -> float:
    """Detect the percentage of a target color in an image using HSV color space.

    Args:
        image_bytes: Image data as bytes.
        target_rgb: The target color to detect as an (R, G, B) tuple.
        saturation_min: Minimum saturation for color detection (0.0-1.0).
        value_min: Minimum value/brightness for color detection (0.0-1.0).

    Returns:
        float: The percentage of pixels matching the target color range.

    """
    image_array = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)

    if image is None:
        raise ValueError("Could not decode image from bytes.")

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    target_rgb_pixel = np.uint8([[target_rgb]])
    target_hsv_pixel = cv2.cvtColor(target_rgb_pixel, cv2.COLOR_RGB2HSV)
    target_hue = target_hsv_pixel[0][0][0]

    min_saturation = int(saturation_min * 255)
    min_value = int(value_min * 255)

    lower_bound = np.array([target_hue, min_saturation, min_value])
    upper_bound = np.array([target_hue, 255, 255])
    color_mask = cv2.inRange(hsv_image, lower_bound, upper_bound)

    total_pixels = image.shape[0] * image.shape[1]
    color_pixels = np.count_nonzero(color_mask)
    color_percentage = color_pixels / total_pixels

    return float(color_percentage)


def find_color_drop_frame(
    frame_bytes: list[bytes],
    target_rgb: tuple[int, int, int, int],
    stability_threshold: float = 0.005,
) -> int:
    """Detect the frame where a target color's ratio drops significantly and stabilizes.

    This is useful for finding the first frame after a greenscreen (or any color screen)
    has been removed.

    Args:
        frame_bytes: A list of video frames, where each frame is a bytes object.
        target_rgb: The color to track as an (R, G, B) tuple.
        stability_threshold: The maximum change between frames to be considered stable.

    Returns:
        int: The index of the first frame after the color drop.
             Returns -1 if no such drop and stabilization is found.

    """
    color_ratios = np.array(
        [detect_color_background(img, target_rgb) for img in frame_bytes]
    )

    if len(color_ratios) < 2:
        return -1

    changes = np.diff(color_ratios)
    biggest_drop_index = np.argmin(changes) + 1

    for i in range(biggest_drop_index, len(color_ratios) - 1):
        future_changes = np.abs(np.diff(color_ratios[i:]))
        if np.all(future_changes < stability_threshold):
            return i

    return -1


# --- Face Detection Functions ---


def get_face_detection_request(image):
    """Create a face detection request for Vision API batch processing."""
    features = [
        Feature(
            model="builtin/latest",
            type_=Feature.Type.FACE_DETECTION,
        )
    ]
    return AnnotateImageRequest(
        {"image": vision.Image(content=image), "features": features}
    )


def is_people_ok(number):
    """Return True if person count is acceptable (0 or 1)."""
    return number <= 1


def count_people(response):
    """Count the number of faces detected in a Vision API response."""
    if not response.face_annotations:
        return 0
    return len(response.face_annotations)


def check_multiple_people(response):
    """Return True if more than one person is detected in the response."""
    if not response.face_annotations:
        return False
    if len(response.face_annotations) == 1:
        return False
    return True


def is_video_valid(people_counts):
    """Validate video based on people counts per frame.

    A video is valid if there is no more than 1 person from a given point onwards.
    Returns the index where the video becomes valid, or -1 if invalid.
    """
    if max(people_counts) <= 1:
        return 0

    index_found = -1
    for idx_1, value in enumerate(people_counts[:-1]):
        is_ok_idx_1 = is_people_ok(value)
        idx_2 = idx_1 + 1
        is_ok_idx_2 = is_people_ok(people_counts[idx_2])
        if not is_ok_idx_1 and is_ok_idx_2:
            index_found = idx_2
            break

    if index_found != -1:
        for elem in people_counts[index_found + 1 :]:
            if not is_people_ok(elem):
                return -1
    return index_found


def get_index_single_person(video_frames) -> int:
    """Analyzes video frames to find the index where only a single person is visible.

    Args:
        video_frames: List of frame bytes to analyze.

    Returns:
        int: Index where video becomes valid, or -1 if invalid.

    """
    requests = [get_face_detection_request(img) for img in video_frames]
    client = vision.ImageAnnotatorClient(
        client_options={"quota_project_id": os.getenv("PROJECT_ID", "")}
    )
    responses = client.batch_annotate_images(requests=requests)
    people_counts = [count_people(response) for response in responses.responses]
    return is_video_valid(people_counts)
