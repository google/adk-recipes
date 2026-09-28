#!/usr/bin/env python3
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

"""Rotation Direction Classifier V3f - Production Script.

This classifier detects rotation direction in product videos using sparse optical flow.
It returns one of: "clockwise", "anticlockwise", "invalid", or "unknown".

V3f Parameters (grid search optimized - BEST: 98.3% accuracy on 876 examples):
- dx_spike: 32 (horizontal motion spike threshold)
- dy_spike: 35 (vertical motion spike threshold)
- min_ratio: 0.1 (minimum minority direction ratio)
- min_avg_dx: 1.95 (minimum minority direction strength)
- high_ratio: 0.45 (high ratio threshold for invalid)
- min_segment_frames: 48 (minimum frames for a valid segment)
- min_segment_strength: 1.5 (minimum avg_dx for a valid segment)

Accuracy: 98.3% overall (video=48/54, shoes=813/822, overall=861/876)

Classification Rules (in order):
1. SPIKE: If any |dx| > 32 or |dy| > 35 → invalid
2. DIR_CHANGE: If minority_ratio > 0.1 AND minority_strength > 1.95 → invalid
3. HIGH_RATIO: If minority_ratio > 0.45 → invalid
4. MULTI_DIR: If multiple filtered directions detected → invalid
5. Otherwise: Return the detected direction (clockwise/anticlockwise/unknown)

Author: Generated with Claude Code
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np

# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ClassifierConfig:
    """Configuration for the V3f rotation classifier."""

    # Spike detection thresholds (V3f: grid search optimized)
    dx_spike: float = 32.0
    dy_spike: float = 35.0

    # Direction change detection
    min_ratio: float = 0.1
    min_avg_dx: float = 1.95
    high_ratio: float = 0.45

    # Segment filtering
    min_segment_frames: int = 48
    min_segment_strength: float = 1.5

    # Optical flow parameters
    window_frames: int = 24
    min_confidence: float = 0.3


# Default configuration (V3f optimized - 98.3% accuracy)
DEFAULT_CONFIG = ClassifierConfig()


# =============================================================================
# MAIN CLASSIFICATION FUNCTION
# =============================================================================


def classify_rotation(
    video_path: str, config: ClassifierConfig | None = None
) -> Literal["clockwise", "anticlockwise", "invalid", "unknown", "error"]:
    """Classify the rotation direction of a spinning product video.

    Args:
        video_path: Path to the video file (mp4, etc.)
        config: Optional configuration (uses V3f defaults if not provided)

    Returns:
        One of:
        - "clockwise": Consistent clockwise rotation
        - "anticlockwise": Consistent anticlockwise rotation
        - "invalid": Invalid rotation detected (spike, direction change, etc.)
        - "unknown": Could not determine direction
        - "error": Failed to process video

    """
    result = classify_rotation_detailed(video_path, config)
    return result["classification"]


def classify_rotation_detailed(
    video_path: str, config: ClassifierConfig | None = None
) -> dict[str, Any]:
    """Classify rotation with detailed results.

    Returns dict containing:
        - classification: The final classification
        - reason: Why this classification was made
        - detected_directions: List of directions from filtered segments
        - total_frames: Total frames in video
        - raw_segments: All detected segments before filtering
        - filtered_segments: Segments after filtering
        - dx_max, dx_min, dy_max, dy_min: Motion statistics
    """
    if config is None:
        config = DEFAULT_CONFIG

    # Step 1: Detect rotation using optical flow
    detection = c_detect_rotation_segments(video_path, config)

    if detection["error"]:
        return {
            "classification": "error",
            "reason": detection["error"],
            "detected_directions": [],
            "total_frames": 0,
            "raw_segments": [],
            "filtered_segments": [],
            "dx_max": 0,
            "dx_min": 0,
            "dy_max": 0,
            "dy_min": 0,
        }

    # Step 2: Apply V3f classification rules
    classification, reason = _apply_v3f_rules(detection, config)

    return {
        "classification": classification,
        "reason": reason,
        "detected_directions": detection["directions"],
        "total_frames": detection["total_frames"],
        "raw_segments": detection["raw_segments"],
        "filtered_segments": detection["filtered_segments"],
        "dx_max": detection["dx_max"],
        "dx_min": detection["dx_min"],
        "dy_max": detection["dy_max"],
        "dy_min": detection["dy_min"],
    }


# =============================================================================
# V3f CLASSIFICATION RULES
# =============================================================================


def _apply_v3f_rules(detection: dict, config: ClassifierConfig) -> tuple:
    """Apply V3f classification rules.

    Rules (in order):
    1. SPIKE: If any |dx| > dx_spike or |dy| > dy_spike → invalid
    2. DIR_CHANGE: If minority_ratio > min_ratio AND minority_strength > min_avg_dx → invalid
    3. HIGH_RATIO: If minority_ratio > high_ratio → invalid
    4. MULTI_DIR: If multiple filtered directions → invalid
    5. Otherwise: Return detected direction

    Returns:
        Tuple of (classification, reason)

    """
    dx_max = detection["dx_max"]
    dx_min = detection["dx_min"]
    dy_max = detection["dy_max"]
    dy_min = detection["dy_min"]

    # Rule 1: Spike Detection
    if abs(dx_max) > config.dx_spike:
        return "invalid", f"SPIKE: dx_max={dx_max:.1f} > {config.dx_spike}"
    if abs(dx_min) > config.dx_spike:
        return (
            "invalid",
            f"SPIKE: |dx_min|={abs(dx_min):.1f} > {config.dx_spike}",
        )
    if abs(dy_max) > config.dy_spike:
        return "invalid", f"SPIKE: dy_max={dy_max:.1f} > {config.dy_spike}"
    if abs(dy_min) > config.dy_spike:
        return (
            "invalid",
            f"SPIKE: |dy_min|={abs(dy_min):.1f} > {config.dy_spike}",
        )

    # Rule 2 & 3: Direction Change Detection
    raw_segments = detection["raw_segments"]

    cw_frames = 0
    cw_dx_sum = 0
    acw_frames = 0
    acw_dx_sum = 0

    for seg in raw_segments:
        if seg["direction"] == "clockwise":
            cw_frames += seg["duration"]
            cw_dx_sum += seg["avg_dx"] * seg["duration"]
        elif seg["direction"] == "anticlockwise":
            acw_frames += seg["duration"]
            acw_dx_sum += seg["avg_dx"] * seg["duration"]

    total_frames = cw_frames + acw_frames

    if total_frames > 0 and cw_frames > 0 and acw_frames > 0:
        if cw_frames > acw_frames:
            minority_ratio = acw_frames / total_frames
            minority_avg_dx = (
                abs(acw_dx_sum / acw_frames) if acw_frames > 0 else 0
            )
        else:
            minority_ratio = cw_frames / total_frames
            minority_avg_dx = abs(cw_dx_sum / cw_frames) if cw_frames > 0 else 0

        # Rule 2: Direction change with significant strength
        if (
            minority_ratio > config.min_ratio
            and minority_avg_dx > config.min_avg_dx
        ):
            return (
                "invalid",
                f"DIR_CHANGE: ratio={minority_ratio:.2f}, strength={minority_avg_dx:.2f}",
            )

        # Rule 3: High ratio alone (even with weak strength)
        if minority_ratio > config.high_ratio:
            return "invalid", f"HIGH_RATIO: ratio={minority_ratio:.2f}"

    # Rule 4: Multiple filtered directions
    detected = detection["directions"]
    if len(detected) > 1:
        return "invalid", f"MULTI_DIR: {detected}"

    # Rule 5: Return detected direction
    if len(detected) == 1 and detected[0] in ["clockwise", "anticlockwise"]:
        return detected[0], f"DETECTED: {detected[0]}"
    else:
        return "unknown", f"UNKNOWN: {detected}"


# =============================================================================
# OPTICAL FLOW DETECTION
# =============================================================================


def c_detect_rotation_segments(
    video_path: str, config: ClassifierConfig
) -> dict:
    """Detect rotation segments using sparse optical flow.

    Returns detection results including segments and statistics.
    """
    # Optical flow parameters
    feature_params = {
        "maxCorners": 100,
        "qualityLevel": 0.01,
        "minDistance": 10,
        "blockSize": 7,
    }

    lk_params = {
        "winSize": (15, 15),
        "maxLevel": 2,
        "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03),
    }

    # Open video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"error": f"Could not open video: {video_path}"}

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    ret, old_frame = cap.read()
    if not ret:
        cap.release()
        return {"error": f"Could not read first frame: {video_path}"}

    old_gray = cv2.cvtColor(old_frame, cv2.COLOR_BGR2GRAY)
    p0 = cv2.goodFeaturesToTrack(old_gray, mask=None, **feature_params)

    if p0 is None:
        cap.release()
        return {
            "error": None,
            "directions": ["unknown"],
            "raw_segments": [],
            "filtered_segments": [],
            "total_frames": total_frames,
            "dx_max": 0,
            "dx_min": 0,
            "dy_max": 0,
            "dy_min": 0,
        }

    frame_data = []
    frame_idx = 0
    redetect_interval = config.window_frames

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        p1, st, _err = cv2.calcOpticalFlowPyrLK(
            old_gray, frame_gray, p0, None, **lk_params
        )

        avg_dx = 0
        avg_dy = 0

        if p1 is not None:
            good_new = p1[st == 1]
            good_old = p0[st == 1]

            if len(good_new) > 0:
                dx_values = good_new[:, 0] - good_old[:, 0]
                dy_values = good_new[:, 1] - good_old[:, 1]

                # For avg_dx: use points with significant horizontal motion (for direction detection)
                mask_dx = (np.abs(dx_values) > 0.5) & (
                    np.abs(dy_values) < np.abs(dx_values)
                )
                if np.sum(mask_dx) > 0:
                    avg_dx = float(np.mean(dx_values[mask_dx]))

                # For avg_dy: use ALL points with any motion (for spike detection) - notebook approach
                mask_all = (np.abs(dx_values) > 0.3) | (np.abs(dy_values) > 0.3)
                if np.sum(mask_all) > 0:
                    avg_dy = float(np.mean(dy_values[mask_all]))

                p0 = good_new.reshape(-1, 1, 2)

            if frame_idx % redetect_interval == 0 or len(good_new) < 10:
                p0_new = cv2.goodFeaturesToTrack(
                    frame_gray, mask=None, **feature_params
                )
                if p0_new is not None:
                    p0 = p0_new

        frame_data.append(
            {
                "frame": frame_idx,
                "avg_dx": avg_dx,
                "avg_dy": avg_dy,
            }
        )

        old_gray = frame_gray.copy()

    cap.release()

    if len(frame_data) == 0:
        return {
            "error": None,
            "directions": ["unknown"],
            "raw_segments": [],
            "filtered_segments": [],
            "total_frames": total_frames,
            "dx_max": 0,
            "dx_min": 0,
            "dy_max": 0,
            "dy_min": 0,
        }

    # Calculate statistics
    dx_values = [f["avg_dx"] for f in frame_data]
    dy_values = [f["avg_dy"] for f in frame_data]

    dx_max = float(np.max(dx_values))
    dx_min = float(np.min(dx_values))
    dy_max = float(np.max(dy_values))
    dy_min = float(np.min(dy_values))

    # Segment the motion
    raw_segments = _segment_motion(frame_data, config)

    # Filter segments
    filtered_segments = [
        s
        for s in raw_segments
        if s["duration"] >= config.min_segment_frames
        and abs(s["avg_dx"]) >= config.min_segment_strength
    ]

    # Get directions from filtered segments
    if len(filtered_segments) == 0:
        if len(raw_segments) > 0:
            strongest = max(
                raw_segments, key=lambda s: abs(s["avg_dx"]) * s["duration"]
            )
            directions = [strongest["direction"]]
            filtered_segments = [strongest]
        else:
            directions = ["unknown"]
    else:
        directions = [filtered_segments[0]["direction"]]
        for seg in filtered_segments[1:]:
            if seg["direction"] != directions[-1]:
                directions.append(seg["direction"])

    return {
        "error": None,
        "directions": directions,
        "raw_segments": raw_segments,
        "filtered_segments": filtered_segments,
        "total_frames": total_frames,
        "dx_max": dx_max,
        "dx_min": dx_min,
        "dy_max": dy_max,
        "dy_min": dy_min,
        "frame_data": frame_data,
    }


def _segment_motion(
    frame_data: list[dict], config: ClassifierConfig
) -> list[dict]:
    """Segment frame data into continuous direction segments using sliding windows."""
    if not frame_data:
        return []

    dx_values = [f["avg_dx"] for f in frame_data]
    window_size = min(config.window_frames, len(dx_values))

    raw_segments = []
    current_direction = None
    segment_start = 0

    # Slide through with 50% overlap
    for i in range(0, len(dx_values), window_size // 2):
        window_end = min(i + window_size, len(dx_values))
        window_dx = dx_values[i:window_end]

        if len(window_dx) == 0:
            continue

        avg_window_dx = np.mean(window_dx)

        if abs(avg_window_dx) < config.min_confidence:
            window_direction = None
        elif avg_window_dx < 0:
            window_direction = "clockwise"
        else:
            window_direction = "anticlockwise"

        if window_direction is not None:
            if current_direction is None:
                current_direction = window_direction
                segment_start = i
            elif window_direction != current_direction:
                raw_segments.append(
                    {
                        "direction": current_direction,
                        "start_frame": segment_start,
                        "end_frame": i,
                        "duration": i - segment_start,
                        "avg_dx": float(np.mean(dx_values[segment_start:i])),
                    }
                )
                current_direction = window_direction
                segment_start = i

    # Final segment
    if current_direction is not None:
        raw_segments.append(
            {
                "direction": current_direction,
                "start_frame": segment_start,
                "end_frame": len(dx_values),
                "duration": len(dx_values) - segment_start,
                "avg_dx": float(np.mean(dx_values[segment_start:])),
            }
        )

    return raw_segments


# =============================================================================
# COMMAND LINE INTERFACE
# =============================================================================

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Rotation Classifier V3f")
        print("=" * 50)
        print(
            "\nUsage: python rotation_classifier.py <video_path> [--detailed]"
        )
        print("\nClassifies rotation direction of a spinning product video.")
        print("\nReturns one of:")
        print("  clockwise     - Consistent clockwise rotation")
        print("  anticlockwise - Consistent anticlockwise rotation")
        print(
            "  invalid       - Invalid rotation (spike, direction change, etc.)"
        )
        print("  unknown       - Could not determine direction")
        print("  error         - Failed to process video")
        print("\nOptions:")
        print("  --detailed    Print detailed classification results as JSON")
        print("\nV3f: Grid search optimized (dx>32, dy>35) - 98.3% accuracy")
        sys.exit(1)

    video_path = sys.argv[1]
    detailed = "--detailed" in sys.argv

    if not Path(video_path).exists():
        print("error")
        sys.exit(1)

    if detailed:
        result = classify_rotation_detailed(video_path)
        # Convert to JSON-serializable format
        output = {k: v for k, v in result.items() if k != "frame_data"}
        print(json.dumps(output, indent=2))
    else:
        result = classify_rotation(video_path)
        print(result)
