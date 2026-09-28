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

"""Video validation utilities for product spin R2V."""

# Standard library imports
import logging

from workflows.shared.video_utils import (
    create_mp4_from_bytes_to_bytes,
    extract_frames_as_bytes_list,
    find_most_similar_frame_index,
)

# Project imports
from workflows.spinning.r2v.shoes.classify_shoes import (
    _is_endpoint_available,
    classify_shoe,
)
from workflows.spinning.r2v.shoes.images_utils import classify_frames

logger = logging.getLogger(__name__)


def is_valid_path(video_path, allowed_path):
    """Check if a sequence of nodes represents a valid path through the allowed graph."""
    # A path with 0 or 1 node is technically valid as there are no transitions to violate.
    if len(video_path) <= 1:
        return True, "Path is valid (too short to violate rules)"

    for i in range(len(video_path) - 1):
        current_node = video_path[i]
        next_node = video_path[i + 1]

        # 1. Check if the current node itself exists in our allowed graph rules
        if current_node not in allowed_path:
            return (
                False,
                f"Invalid node found at index {i}: '{current_node}' is not in the map.",
            )

        # 2. Check if the transition to the next node is allowed
        if next_node not in allowed_path[current_node]:
            return (
                False,
                f"Invalid transition at index {i}: cannot go from '{current_node}' to '{next_node}'",
            )

    return True, "Path is valid"


def is_valid_path_lessstrict(
    video_path, allowed_path, max_consecutive_violations=5
):
    """Check if a sequence of nodes represents a valid path through the allowed graph.

    Allows tolerance for temporary misclassifications and oscillations during transitions.

    This function handles:
    1. Random misclassifications (noise)
    2. Oscillations between adjacent classes during transitions (e.g., back_left <-> back)
    3. Early "peeks" at the next class before fully transitioning

    Strategy: Track valid transitions while allowing backwards movement to adjacent
    classes that are part of the valid path progression.

    Args:
        video_path: List of class labels for each frame
        allowed_path: Dictionary defining valid transitions between classes
        max_consecutive_violations: Maximum violations before failing (default: 5)

    Returns:
        Tuple of (is_valid: bool, message: str, corrected_path: list)
        - is_valid: Whether the path is valid
        - message: Validation message with details
        - corrected_path: Cleaned path with misclassifications/oscillations corrected to valid positions

    """
    if len(video_path) <= 1:
        return (
            True,
            "Path is valid (too short to violate rules)",
            video_path.copy(),
        )

    # Track the current valid node and the set of nodes we've progressed through
    current_valid_node = None
    visited_nodes = set()
    consecutive_violations = 0
    consecutive_oscillations = 0  # Track consecutive oscillations
    violations_log = []

    # Create corrected path - will be populated with cleaned classifications
    corrected_path = [None] * len(video_path)

    # Build reverse mapping to know which nodes can transition TO each node
    reverse_allowed = {}
    for node, allowed_next in allowed_path.items():
        for next_node in allowed_next:
            if next_node not in reverse_allowed:
                reverse_allowed[next_node] = set()
            reverse_allowed[next_node].add(node)

    for i in range(len(video_path)):
        node = video_path[i]

        # Check if node exists in the graph
        if node not in allowed_path:
            consecutive_violations += 1
            violations_log.append(f"Unknown node at index {i}: '{node}'")
            if consecutive_violations > max_consecutive_violations:
                return (
                    False,
                    f"Too many consecutive violations ({consecutive_violations})",
                    None,
                )
            # Correct unknown nodes to current valid node
            corrected_path[i] = (
                current_valid_node if current_valid_node else node
            )
            continue

        # Initialize with first valid node
        if current_valid_node is None:
            current_valid_node = node
            visited_nodes.add(node)
            consecutive_violations = 0
            corrected_path[i] = node
            continue

        # Same node - always valid
        if node == current_valid_node:
            consecutive_violations = 0
            consecutive_oscillations = (
                0  # Reset oscillation counter on valid movement
            )
            corrected_path[i] = node
            continue

        # Check if this is a valid forward transition
        if node in allowed_path[current_valid_node]:
            # Valid forward movement
            current_valid_node = node
            visited_nodes.add(node)
            consecutive_violations = 0
            consecutive_oscillations = (
                0  # Reset oscillation counter on valid forward movement
            )
            corrected_path[i] = node
            continue

        # Check if this is a backward movement to a visited adjacent node
        # This handles oscillations during transitions (e.g., back_left -> back -> back_left -> back)
        if node in visited_nodes:
            # Check if we can move back to this node from current position
            if node in reverse_allowed.get(current_valid_node, set()):
                # Valid backward oscillation - don't count as violation
                # But don't update current_valid_node - we're oscillating
                # Correct to current_valid_node (the forward position we're transitioning to)
                consecutive_oscillations += 1
                if consecutive_oscillations > 3:
                    return False, "Video not following path", None
                consecutive_violations = 0
                corrected_path[i] = current_valid_node
                continue
            # Also check if current can go to node (bidirectional oscillation)
            elif node in allowed_path.get(current_valid_node, []):
                consecutive_oscillations += 1
                if consecutive_oscillations > 3:
                    return False, "Video not following path", None
                consecutive_violations = 0
                corrected_path[i] = current_valid_node
                continue

        # Check if this could be a valid skip-ahead (going through intermediate node)
        # E.g., from back_left we might briefly see back, then return to back_left
        is_valid_skip = False
        for intermediate in allowed_path.get(current_valid_node, []):
            # Can we reach this node from any valid next step?
            if (
                node in allowed_path.get(intermediate, [])
                or node == intermediate
            ):
                is_valid_skip = True
                violations_log.append(
                    f"Skip transition at index {i}: '{current_valid_node}' -> '{node}' (via '{intermediate}')"
                )
                consecutive_violations = 0
                corrected_path[i] = current_valid_node
                break

        if is_valid_skip:
            continue

        # If none of the above, treat as violation
        consecutive_violations += 1
        violations_log.append(
            f"Invalid transition at index {i}: '{current_valid_node}' -> '{node}'"
        )
        # Correct violation to current valid node
        corrected_path[i] = current_valid_node

        if consecutive_violations > max_consecutive_violations:
            return (
                False,
                f"Too many consecutive violations ({consecutive_violations}) ending at index {i}: cannot go from '{current_valid_node}' to '{node}'",
                None,
            )

    if violations_log:
        return (
            True,
            f"Path is valid with {len(violations_log)} misclassifications tolerated: {', '.join(violations_log)}",
            corrected_path,
        )
    else:
        return True, "Path is valid with no violations", corrected_path


def is_valid(video_path, strict=True):
    """Check if the video path is valid.

    Args:
        video_path: List of class labels for each frame.
        strict: Whether to use strict path validation. Default: True.

    Returns:
        Tuple of (is_valid, reversed, message, corrected_path).

    """
    allowed_path = {
        "front": ["front", "front_left"],
        "front_right": ["front_right", "front"],
        "front_left": ["front_left", "left"],
        # 'front_left': ['front_left', 'left', 'back_left'],
        "back": ["back", "back_right"],
        #'back_right': ['back_right', 'right', 'front_right'],
        "back_right": ["back_right", "right"],
        "back_left": ["back_left", "back"],
        "right": ["front_right", "right"],
        "left": ["back_left", "left"],
    }

    if strict:
        is_valid_clockwise, clock_wise_message = is_valid_path(
            video_path, allowed_path
        )
        corrected_path_clockwise = None
    else:
        is_valid_clockwise, clock_wise_message, corrected_path_clockwise = (
            is_valid_path_lessstrict(video_path, allowed_path)
        )

    if is_valid_clockwise:
        return (
            True,
            False,
            f"Clockwise {clock_wise_message}",
            corrected_path_clockwise,
        )

    if not is_valid_clockwise:
        if strict:
            is_valid_anticlockwise, anticlock_wise_message = is_valid_path(
                video_path[::-1], allowed_path
            )
            corrected_path_anticlockwise = None
        else:
            (
                is_valid_anticlockwise,
                anticlock_wise_message,
                corrected_path_anticlockwise,
            ) = is_valid_path_lessstrict(video_path[::-1], allowed_path)

        if is_valid_anticlockwise:
            return (
                True,
                True,
                f"Anti Clockwise {anticlock_wise_message}",
                corrected_path_anticlockwise,
            )

    return (
        False,
        False,
        f"Clockwise: {clock_wise_message}, AntiClockwise {anticlock_wise_message}",
        None,
    )


def find_second_occurrence_range(
    classes, original_indices, min_frame_distance=50
):
    """Find the initial and ending original indices where the starting class appears for the second time.

    If not second appearance return None None.

    The function reverses through the classes list and finds:
    1. The first occurrence of the starting class (which we skip)
    2. The second occurrence of the starting class (which we want to find the range for)
       - Must be at least min_frame_distance frames away from the first occurrence

    Args:
        classes: List of classified positions (e.g., ['right', 'front_right', 'front', ...])
        original_indices: List of original frame indices corresponding to each class
        min_frame_distance: Minimum frame distance between first and second occurrence (default: 50)

    Returns:
        tuple: (start_original_index, end_original_index) for the second occurrence,
               or (None, None) if second occurrence not found

    """
    if (
        not classes
        or not original_indices
        or len(classes) != len(original_indices)
    ):
        return None, None

    target_class = classes[0]

    # Find where the first occurrence ends
    first_occurrence_end_index = None
    for index, position in enumerate(classes):
        if position != target_class:
            first_occurrence_end_index = index
            break

    if first_occurrence_end_index is None:
        # All elements are the same class
        return None, None

    # Get the frame index where first occurrence ends
    first_occurrence_last_frame = original_indices[
        first_occurrence_end_index - 1
    ]

    # Search for second occurrence that is at least min_frame_distance frames away
    second_occurrence_start = None
    second_occurrence_end = None

    for index in range(first_occurrence_end_index, len(classes)):
        if classes[index] == target_class:
            # Check if this occurrence is far enough from the first
            current_frame = original_indices[index]
            if (
                current_frame - first_occurrence_last_frame
                >= min_frame_distance
            ):
                second_occurrence_start = index
                break

    if second_occurrence_start is None:
        return None, None

    # Find where this second occurrence ends
    # Allow for 1 misclassification - only end if we see 2 consecutive non-target classes
    # or if the next class after a non-target is also not the target
    for index in range(second_occurrence_start + 1, len(classes)):
        if classes[index] != target_class:
            # Found a potential end - check if next frame (if exists) is also not target_class
            if index + 1 < len(classes):
                if classes[index + 1] != target_class:
                    # Two consecutive non-target classes - this is the real end
                    second_occurrence_end = index - 1
                    break
                else:
                    # Next frame is target_class - this was just a misclassification, continue
                    continue
            else:
                # We're at the last frame and it's not target_class
                second_occurrence_end = index - 1
                break
    else:
        # Reached end of list without finding end of occurrence
        second_occurrence_end = len(classes) - 1

    start_original_index = original_indices[second_occurrence_start]
    end_original_index = original_indices[second_occurrence_end]

    return start_original_index, end_original_index


def sample_frames(frame_list, target_samples_per_sec, original_fps=24):
    """Sample frames from the video at a target rate.

    Args:
        frame_list: List of frames.
        target_samples_per_sec: Target number of samples per second.
        original_fps: Original frames per second. Default: 24.

    Returns:
        Tuple of (sampled_frames, sampled_indices).

    """
    interval = int(original_fps / target_samples_per_sec)
    sampled_indices = list(range(0, len(frame_list), interval))
    if len(frame_list) - 1 not in sampled_indices:
        sampled_indices.append(len(frame_list) - 1)
    sampled_frames = [frame_list[i] for i in sampled_indices]
    return sampled_frames, sampled_indices


def all_classes_present(product_position_frames):
    """Check if all required classes are present in the frames.

    Args:
        product_position_frames: List of classified positions.

    Returns:
        Tuple of (bool, str) indicating if all classes are present and a message.

    """
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
    unique_positions = set(product_position_frames)
    missing_positions = [
        pos for pos in class_order if pos not in unique_positions
    ]

    if missing_positions:
        return (
            False,
            f"Missing required positions in video: {', '.join(missing_positions)}",
        )
    # elif product_position_frames[0] != product_position_frames[-1]:
    #     return False, "Not complete 360"
    else:
        return True, "All required positions are present"


def frames_in_extremity_class(
    image_list, frame_classes, client, shoe_classifier_model, position="last"
):
    """Find the range of frames that have the same class as either the first or last image.

    Uses binary search, searching only within 60 frames from the target position.

    Args:
        image_list: List of images (in original order)
        frame_classes: List of classes for each frame
        client: Client for classify_shoe
        shoe_classifier_model: Model for classify_shoe
        position: Either 'first' or 'last' - determines which end to search

    Returns:
        Number of consecutive frames at the specified position that share the same class,
        or 0 if no match found in the search range

    """
    if not image_list:
        return 0

    n = len(image_list)
    if n == 1:
        return 0

    if position == "last":
        # Working from the end of the list - reverse it
        search_list = image_list[::-1]
        frame_classes = frame_classes[::-1]
    elif position == "first":
        # Working from the beginning of the list - use as is
        search_list = image_list
    else:
        raise ValueError(
            f"position must be 'first' or 'last', got '{position}'"
        )

    # Get the class of the reference image (at index 0 of search_list)
    # reference_class = classify_shoe(search_list[0], client, shoe_classifier_model)
    reference_class = frame_classes[0]

    # Search only within the first 60 frames from the reference position
    # We want to find the LAST occurrence (furthest from 0, closest to 60)
    search_end = min(60, n)

    # Binary search to find the last occurrence within range
    left, right = (
        1,
        search_end - 1,
    )  # Start from 1 since we know index 0 matches
    result = 0  # Default to first image

    times = 0

    while left <= right:
        mid = (left + right) // 2
        current_class = classify_shoe(
            search_list[mid], client, shoe_classifier_model
        )
        times += 1
        if current_class == reference_class:
            # Found a match, search right half for later match (further from start)
            result = mid
            left = mid + 1
        else:
            # No match, search left half (closer to start)
            right = mid - 1

    return result + 1


def validate_and_fix_product_spin_consistency_r2v(
    video_bytes: bytes | None = None,
    client=None,
    model=None,
    pre_classified_frames: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate if a product spin video follows a consistent rotation path.

    Args:
        video_bytes: The video as bytes (optional if pre_classified_frames is provided)
        client: GenAI client for predictions (optional if pre_classified_frames is provided)
        model: Model endpoint for shoe position classification (optional if pre_classified_frames is provided)
        pre_classified_frames: Pre-classified frame labels (if already classified during trimming)

    Returns:
        Tuple of (is_valid, message, new_video_bytes, sampled_classifications, sampled_indices, total_frames, frame_list)
        - is_valid: Whether the video is valid
        - message: Validation message
        - new_video_bytes: The processed video bytes (potentially reversed/cropped)
        - sampled_classifications: List of classifications for sampled frames (in original video order)
        - sampled_indices: List of frame indices that were sampled (in original video order)
        - total_frames: Total number of frames in the video
        - frame_list: List of frame bytes (after processing - reversed/cropped if needed)

    """
    if pre_classified_frames is not None:
        product_position_frames = pre_classified_frames
        sampled_indices = None
        total_frames = None
        frame_list = None  # No frames available when using pre-classified
    else:
        frame_list = extract_frames_as_bytes_list(video_bytes)
        total_frames = len(frame_list)

        # Local classifier (embedding-based) is fast enough to classify all frames;
        # only sample when using a remote endpoint to reduce API calls.
        if _is_endpoint_available(model):
            sampled_frames, sampled_indices = sample_frames(
                frame_list=frame_list,
                target_samples_per_sec=12,
                original_fps=24,
            )
        else:
            sampled_frames = frame_list
            sampled_indices = list(range(total_frames))

        product_position_frames = classify_frames(
            sampled_frames, client, model, mode="validation"
        )

    # Filter out 'invalid' frames — they carry no path information (motion blur, transitions)
    if sampled_indices is not None:
        filtered = [
            (c, i)
            for c, i in zip(
                product_position_frames, sampled_indices, strict=True
            )
            if c != "invalid"
        ]
        if filtered:
            product_position_frames, sampled_indices = zip(
                *filtered, strict=True
            )
            product_position_frames = list(product_position_frames)
            sampled_indices = list(sampled_indices)
    else:
        product_position_frames = [
            c for c in product_position_frames if c != "invalid"
        ]

    are_classes_present, message_complete = all_classes_present(
        product_position_frames
    )

    if not are_classes_present:
        return (
            False,
            message_complete,
            video_bytes,
            product_position_frames,
            sampled_indices,
            total_frames,
            frame_list,
        )
    else:
        is_valid_spin, reversed, message_valid, _corrected_path = is_valid(
            product_position_frames, strict=False
        )

        if not is_valid_spin:
            valid = False
            message = message_valid
        else:
            start_idx, end_idx = find_second_occurrence_range(
                product_position_frames, sampled_indices
            )
            if start_idx is None and end_idx is None:
                if (
                    "front" == product_position_frames[0].lower()
                    or "front" == product_position_frames[-1].lower()
                    or (
                        "front" in product_position_frames[0].lower()
                        and "front" in product_position_frames[-1].lower()
                    )
                ):
                    valid = False
                    message = "Gap in front."
                else:
                    FIRST_AND_LAST_CLASS_FRAMES_THRESHOLD = 25

                    number_frames_last_class = frames_in_extremity_class(
                        frame_list,
                        product_position_frames,
                        client,
                        model,
                        position="last",
                    )
                    number_frames_first_class = frames_in_extremity_class(
                        frame_list,
                        product_position_frames,
                        client,
                        model,
                        position="first",
                    )
                    combined_number_frames = (
                        number_frames_first_class + number_frames_last_class
                    )

                    if (
                        combined_number_frames
                        < FIRST_AND_LAST_CLASS_FRAMES_THRESHOLD
                    ):
                        valid = False
                        message = f"Insuficinet spin, just {combined_number_frames} frames on init and end class"

                    else:
                        valid = True
                        message = f"Sufficient 360 spin with {combined_number_frames} frames on init and end class"

            elif (
                end_idx - start_idx < 3 and total_frames - end_idx < 3
            ):  # Making sure the is at least two frames to pick from and that this happens close to the end
                message = "Spin ends at the same class as initial and no trimming needed"
                valid = True
            else:
                if start_idx == end_idx:
                    cropped_index = start_idx
                else:
                    similar_image_index = find_most_similar_frame_index(
                        frame_list[start_idx:end_idx], frame_list[0]
                    )
                    cropped_index = start_idx + (similar_image_index + 1)
                message = f"Too long of 360, cropp at position {cropped_index + 1}/{total_frames}"
                valid = True

                frame_list = frame_list[:cropped_index]
                total_frames = len(frame_list)
                filtered = [
                    (idx, position)
                    for idx, position in zip(
                        sampled_indices, product_position_frames, strict=True
                    )
                    if idx < cropped_index
                ]
                sampled_indices = [idx for idx, _ in filtered]
                product_position_frames = [position for _, position in filtered]

        if reversed:
            frame_list = frame_list[::-1]
            product_position_frames = product_position_frames[::-1]
            sampled_indices = [
                total_frames - 1 - idx for idx in sampled_indices[::-1]
            ]

        new_video_bytes = create_mp4_from_bytes_to_bytes(
            frame_list, fps=24, quality=7
        )

        return (
            valid,
            message,
            new_video_bytes,
            product_position_frames,
            sampled_indices,
            total_frames,
            frame_list,
        )
