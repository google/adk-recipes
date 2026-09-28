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

"""Shared utilities for video processing.

Includes frame extraction, video creation, merging, and frame similarity analysis.
"""

# Standard library imports
import io
import os
import tempfile

# Third-party imports
import cv2
import imageio
import numpy as np
from imageio.v3 import imread
from moviepy import VideoFileClip, concatenate_videoclips
from skimage.metrics import structural_similarity as ssim


def extract_frames_as_bytes_list(video_bytes, image_format=".png"):
    """Extract frames from video bytes and return them as a list of image bytes.

    Args:
        video_bytes: The raw byte data of a video file
        image_format: The desired output image format (default: ".png")

    Returns:
        list: A list where each element is the byte data of a single frame

    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp_video:
        temp_video.write(video_bytes)
        temp_video_path = temp_video.name

    frame_bytes_list = []
    cap = cv2.VideoCapture(temp_video_path)

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        success, encoded_image = cv2.imencode(image_format, frame)
        if success:
            frame_bytes_list.append(encoded_image.tobytes())

    cap.release()
    os.unlink(temp_video_path)
    return frame_bytes_list


def create_mp4_from_bytes_to_bytes(frames_bytes, fps=24, quality=7):
    """Create an MP4 video in memory and return its raw bytes.

    Args:
        frames_bytes: List of frame bytes
        fps: Frames per second (default: 24)
        quality: Video quality (default: 7)

    Returns:
        bytes: The video as bytes

    """
    video_buffer = io.BytesIO()
    with imageio.get_writer(
        video_buffer, format="mp4", fps=fps, codec="libx264", quality=quality
    ) as writer:
        for frame_byte in frames_bytes:
            writer.append_data(imread(frame_byte))
    return video_buffer.getvalue()


def merge_videos_from_bytes(videos_bytes, speeds=None, fps=24):
    """Merge multiple MP4 videos (in bytes) into a single video using temporary files.

    Apply a specific speed to each clip.

    Args:
        videos_bytes: List of video bytes to merge
        speeds: Optional list of speed multipliers for each clip
        fps: Frames per second (default: 24)

    Returns:
        bytes: The merged video as bytes

    """
    temp_files = []
    clips = []

    try:
        # Write each video bytes to temporary files
        for i, video_bytes in enumerate(videos_bytes):
            temp_file = tempfile.NamedTemporaryFile(
                delete=False, suffix=f"_input_{i}.mp4"
            )
            temp_file.write(video_bytes)
            temp_file.close()
            temp_files.append(temp_file.name)

            # Create VideoFileClip from the temporary file
            clip = VideoFileClip(temp_file.name)

            # Apply speed modification if speeds are provided
            if speeds and i < len(speeds):
                clip = clip.with_speed_scaled(speeds[i])

            clip.fps = fps
            clips.append(clip)

        # Concatenate all clips
        final_clip = concatenate_videoclips(clips)
        final_clip.fps = fps

        # Create output temporary file
        output_temp = tempfile.NamedTemporaryFile(
            delete=False, suffix="_merged.mp4"
        )
        output_temp.close()

        # Write the final video to temporary file
        final_clip.write_videofile(
            output_temp.name,
            codec="libx264",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
        )

        # Read the merged video bytes
        with open(output_temp.name, "rb") as f:
            merged_video_bytes = f.read()

        return merged_video_bytes

    finally:
        # Clean up clips
        for clip in clips:
            clip.close()
        if "final_clip" in locals():
            final_clip.close()

        # Clean up temporary files
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        if "output_temp" in locals() and os.path.exists(output_temp.name):
            os.remove(output_temp.name)


def get_frame_similarity_bytes(frame1_bytes, frame2_bytes):
    """Calculate the Structural Similarity Index (SSIM) between two images.

    Args:
        frame1_bytes: First image as bytes
        frame2_bytes: Second image as bytes

    Returns:
        float: SSIM similarity score between 0 and 1

    """
    nparr1 = np.frombuffer(frame1_bytes, np.uint8)
    img1_array = cv2.imdecode(nparr1, cv2.IMREAD_COLOR)

    nparr2 = np.frombuffer(frame2_bytes, np.uint8)
    img2_array = cv2.imdecode(nparr2, cv2.IMREAD_COLOR)

    # Resize images to match dimensions if they differ
    if img1_array.shape != img2_array.shape:
        img2_array = cv2.resize(
            img2_array,
            (img1_array.shape[1], img1_array.shape[0]),
            interpolation=cv2.INTER_LANCZOS4,
        )

    img1_gray = cv2.cvtColor(img1_array, cv2.COLOR_BGR2GRAY)
    img2_gray = cv2.cvtColor(img2_array, cv2.COLOR_BGR2GRAY)

    return float(ssim(img1_gray, img2_gray))


def detect_rotation_direction(
    clip_a_frames: list[bytes],
    clip_b_frames: list[bytes],
    clip_b_reversed_frames: list[bytes] | None = None,
    saturation_max: float = 24.0,
    value_min: float = 90.0,
) -> dict:
    """Detect if two video clips are rotating in opposite directions.

    Compares clip_a with both clip_b (normal) and clip_b (reversed) using
    frame-by-frame SSIM with background removal. Uses majority voting
    across all frames for robust detection.

    Args:
        clip_a_frames: List of frame bytes from first clip
        clip_b_frames: List of frame bytes from second clip
        clip_b_reversed_frames: Optional pre-reversed frames for clip_b.
            If None, clip_b_frames will be reversed in place.
        saturation_max: Maximum saturation % for background detection (default: 24)
        value_min: Minimum brightness % for background detection (default: 90)

    Returns:
        dict with keys:
            - is_anticlockwise: True if clip_b should be reversed
            - votes_normal: Number of frames favoring normal alignment
            - votes_reversed: Number of frames favoring reversed alignment
            - confidence: Percentage of votes for winning direction (0-100)

    """
    from workflows.shared.image_utils import calculate_ssim_with_bg_removal

    clip_b_reversed = (
        clip_b_reversed_frames
        if clip_b_reversed_frames is not None
        else clip_b_frames[::-1]
    )
    n_frames = min(len(clip_a_frames), len(clip_b_frames), len(clip_b_reversed))

    votes_normal = 0
    votes_reversed = 0

    for i in range(n_frames):
        sim_normal = calculate_ssim_with_bg_removal(
            clip_a_frames[i],
            clip_b_frames[i],
            saturation_max,
            value_min,
        )
        sim_reversed = calculate_ssim_with_bg_removal(
            clip_a_frames[i],
            clip_b_reversed[i],
            saturation_max,
            value_min,
        )

        if sim_normal > sim_reversed:
            votes_normal += 1
        else:
            votes_reversed += 1

    is_anticlockwise = votes_reversed > votes_normal
    winner_votes = max(votes_normal, votes_reversed)
    confidence = (winner_votes / n_frames) * 100 if n_frames > 0 else 0

    return {
        "is_anticlockwise": is_anticlockwise,
        "votes_normal": votes_normal,
        "votes_reversed": votes_reversed,
        "confidence": confidence,
    }


def is_rotation_clockwise(
    video_a_bytes: bytes,
    video_b_bytes: bytes,
    sample_fps: int = 4,
    video_fps: int = 24,
    compare_seconds: int = 2,
) -> bool:
    """Determine if two consecutive video clips are rotating in the same direction.

    Extracts frames from both videos, compares video_a with video_b (normal)
    and video_b (reversed), and uses majority voting to determine rotation
    direction consistency.

    For the reversed comparison, we reverse the FULL video_b first, then take
    the first N seconds. This compares video_a[0:Ns] with the LAST N seconds
    of video_b played backwards.

    Args:
        video_a_bytes: First video as bytes (reference clip)
        video_b_bytes: Second video as bytes (clip to check)
        sample_fps: Frames per second to sample for comparison (default: 4)
        video_fps: Original video FPS (default: 24)
        compare_seconds: Number of seconds to compare (default: 2)

    Returns:
        bool: True if clockwise (same direction), False if anticlockwise (needs reversal)

    """
    # DEBUG: Save videos for inspection (remove later)
    # import time; ts = int(time.time()); open(f"debug_video_a_{ts}.mp4", "wb").write(video_a_bytes); open(f"debug_video_b_{ts}.mp4", "wb").write(video_b_bytes)

    # Extract frames from both videos
    frames_a = extract_frames_as_bytes_list(video_a_bytes)
    frames_b = extract_frames_as_bytes_list(video_b_bytes)

    # Sample frames at lower rate for efficiency
    step = max(1, video_fps // sample_fps)
    sampled_a_full = [frames_a[i] for i in range(0, len(frames_a), step)]
    sampled_b_full = [frames_b[i] for i in range(0, len(frames_b), step)]

    # Limit to first N seconds for comparison
    max_frames = compare_seconds * sample_fps
    sampled_a = sampled_a_full[:max_frames]
    sampled_b = sampled_b_full[:max_frames]

    # For reversed: reverse FULL clip_b, then take first N frames
    # This gives us the last N seconds of original clip_b, played backwards
    sampled_b_reversed = sampled_b_full[::-1][:max_frames]

    # Detect rotation direction
    result = detect_rotation_direction(sampled_a, sampled_b, sampled_b_reversed)
    print(result)

    # Return True if clockwise (not anticlockwise)
    return not result["is_anticlockwise"]


def find_most_similar_frame_index(
    all_frames, reference_frame, num_frames_to_check=None
):
    """Find the index of the frame most similar to a reference frame.

    Compares frames against the reference frame using Structural Similarity
    Index (SSIM) and returns the index of the best match.

    Args:
        all_frames: List of frame images as bytes
        reference_frame: Reference image as bytes to compare against
        num_frames_to_check: Number of frames from the end of the list to analyze.
                             If None, analyzes all frames. (default: None)

    Returns:
        int: Index of the most similar frame within the analyzed subset.
             Note: If num_frames_to_check is set, the index is relative to
             the subset (last N frames), not the full list.

    """
    frames_to_check = (
        all_frames[-num_frames_to_check:] if num_frames_to_check else all_frames
    )

    similarities = []
    for frame in frames_to_check:
        similarity_score = get_frame_similarity_bytes(frame, reference_frame)
        similarities.append(similarity_score)
    return int(np.argmax(similarities))


def reverse_video(video_bytes, fps=24, quality=7, return_frames_only=False):
    """Reverses a video by extracting frames, reversing their order, and creating a new video.

    Args:
        video_bytes: The video as bytes
        fps: Frames per second for output video (default: 24)
        quality: Video quality (default: 7)
        return_frames_only: If True, return only the reversed frames list
                           instead of creating a video (default: False)

    Returns:
        bytes: Reversed video as bytes (if return_frames_only is False)
        list: List of reversed frame bytes (if return_frames_only is True)

    """
    frames = extract_frames_as_bytes_list(video_bytes)
    reversed_frames = frames[::-1]

    if return_frames_only:
        return reversed_frames

    reversed_video_bytes = create_mp4_from_bytes_to_bytes(
        reversed_frames, fps=fps, quality=quality
    )
    return reversed_video_bytes


def convert_image_to_video_frame(video_frame_bytes, image_bytes):
    """Resizes and center-crops an image to match the dimensions of a video frame.

    Args:
        video_frame_bytes: Reference video frame as bytes
        image_bytes: Image to resize/crop as bytes

    Returns:
        bytes: Resized and cropped image as bytes

    """
    np_arr_video = np.frombuffer(video_frame_bytes, np.uint8)
    video_frame = cv2.imdecode(np_arr_video, cv2.IMREAD_COLOR)
    video_height, video_width = video_frame.shape[:2]

    np_arr_img = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(np_arr_img, cv2.IMREAD_COLOR)
    img_h, img_w = img.shape[:2]

    video_aspect = video_width / video_height
    img_aspect = img_w / img_h

    if img_aspect > video_aspect:
        scale_factor = video_height / img_h
        new_w = int(img_w * scale_factor)
        resized_img = cv2.resize(
            img, (new_w, video_height), interpolation=cv2.INTER_AREA
        )
        x_start = (new_w - video_width) // 2
        final_frame = resized_img[:, x_start : x_start + video_width]
    else:
        scale_factor = video_width / img_w
        new_h = int(img_h * scale_factor)
        resized_img = cv2.resize(
            img, (video_width, new_h), interpolation=cv2.INTER_AREA
        )
        y_start = (new_h - video_height) // 2
        final_frame = resized_img[y_start : y_start + video_height, :]

    success, buffer = cv2.imencode(".png", final_frame)

    if not success:
        raise ValueError("Could not encode the final image frame.")

    return buffer.tobytes()
