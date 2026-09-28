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

"""Video generation pipeline for product fitting.

Mimics the logic of video VTO clothes but adapts to different framings.
"""

import asyncio
import base64
import logging
import os
import uuid
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from workflows.shared.image_utils import crop_face
from workflows.shared.person_eval import (
    evaluate_person_match,
    validate_model_photo,
)
from workflows.shared.veo_utils import generate_veo_r2v
from workflows.shared.video_utils import extract_frames_as_bytes_list

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_config_loaded = False


def _ensure_config():
    global _config_loaded
    if _config_loaded:
        return
    config_path = Path(__file__).parent.parent.parent.parent / "config.env"
    if config_path.exists():
        load_dotenv(config_path)
    _config_loaded = True


# Number of frames to sample uniformly across the video for face evaluation
EVAL_SAMPLE_COUNT = 8

# Minimum face similarity score (%) to include a video in the response
MIN_SIMILARITY_THRESHOLD = 70.0

VEO_MODEL = "veo-3.1-generate-001"

DEFAULT_VEO_PROMPT = """
Subject: The exactly same person from the reference image, wearing the exactly same outfit. The person's identity, face, body, skin tone, and hair must remain perfectly consistent with the reference image throughout the entire video.
Scene: A clean, professional studio setup with a solid high-key white background. The model is centered in the frame.
Lighting: Even, bright studio lighting with minimal harsh shadows, highlighting the texture and details of the garment.

Sequence 1 (00:00 - 00:01):
Action: Front View. The model faces forward toward the camera with one hand on the hip, standing still.
Light and camera movement: Static camera; medium shot, framing the model from the waist up.

Sequence 2 (00:01 - 00:03):
Action: Profile View. The model begins to rotate and turns to the left, highlighting the sleeve and the profile of the hood.
Light and camera movement: Static camera; medium shot (waist up).

Sequence 3 (00:03 - 00:06):
Action: Rear View. The model completes a half-turn to show the full back of the jacket and the coverage of the hood.
Light and camera movement: Static camera; medium shot (waist up).

Sequence 4 (00:06 - 00:08):
Action: Return to Front. The model continues the rotation in the same direction, briefly showing the opposite profile before returning to the original front-facing pose with one hand on the hip.
Light and camera movement: Static camera; medium shot (waist up).
"""

LOWER_BODY_VEO_PROMPT = """
Subject: Identical lower body from reference images. Garment details must remain perfectly consistent. Frame strictly from the waist down (waist to shoes); upper body and head are outside the frame. Do not invent details; reproduce the garment and shoes exactly from references.

Scene: Professional studio, high-key white background, centered framing.
Lighting: Bright, even studio lighting highlighting technical fabric textures.

Movement & Physics: Natural human motion with organic weight shifts. Avoid mechanical rotation. Model must move with fluid, lifelike steps and weight distribution. Fabric should show realistic tension and folding during movement.

Sequence 1 (00:00-00:01): Static front view. Model stands in a relaxed, natural stance. Subtle micro-movements (breathing/shifting) for realism.
Sequence 2 (00:01-00:04): Smooth 180-degree pivot. Model takes small, natural steps to turn. The turn is led by the hips and feet stepping naturally to show the side profile and garment construction.
Sequence 3 (00:04-00:06): Rear view. Model completes the turn to face away, holding the pose briefly to show rear yoke and pocket details as per reference.
Sequence 4 (00:06-00:08): Return to front. Model continues rotating in the same direction using rhythmic, human steps, settling back into the original front-facing pose. Fabric settles naturally upon stopping.

Camera: Strictly static close-up from waist to feet throughout all sequences.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _create_reference_images(
    front_bytes: bytes, back_bytes: bytes | None, framing: str
) -> list[bytes]:
    """Create reference images for Veo based on framing and available views."""
    refs = []

    # Use exact image bytes natively to avoid aspect ratio mismatch with Veo (9:16)
    refs.append(front_bytes)

    # Add back image if provided
    if back_bytes is not None:
        refs.append(back_bytes)

    # Add face crop if detected in front image and we still need a 2nd image
    if len(refs) == 1 and framing in ("full_body", "upper_body"):
        face_bytes = crop_face(front_bytes, padding_ratio=0.5)
        if face_bytes is not None:
            refs.append(face_bytes)

    return refs


def _evaluate_clip(
    video_bytes: bytes,
    reference_face_bytes: bytes | None,
    clip_index: int,
    start_seconds: float = 6.0,
    fps: int = 24,
) -> float:
    """Evaluate face similarity for a single clip."""
    if reference_face_bytes is None:
        logger.info(
            f"[Face Eval] Clip {clip_index}: skipping face eval (no reference face)"
        )
        return 100.0  # Pass by default if no face to evaluate

    frames = extract_frames_as_bytes_list(video_bytes)
    total = len(frames)
    if total == 0:
        logger.warning(f"[Face Eval] Clip {clip_index}: no frames extracted")
        return 0.0

    start_frame = int(start_seconds * fps)
    frames = frames[start_frame:]
    if not frames:
        logger.warning(
            f"[Face Eval] Clip {clip_index}: no frames after {start_seconds}s"
        )
        return 0.0

    step = max(1, len(frames) // EVAL_SAMPLE_COUNT)
    sampled = frames[::step][:EVAL_SAMPLE_COUNT]

    scores = []
    for frame in sampled:
        result = evaluate_person_match(reference_face_bytes, frame)
        scores.append(result["similarity_percentage"])

    nonzero = sorted([s for s in scores if s > 0])
    median_score = nonzero[len(nonzero) // 2] if nonzero else 0.0
    scores_str = ", ".join(f"{s:.1f}" for s in scores)
    logger.info(
        f"[Face Eval] Clip {clip_index}: {len(sampled)}/{total} frames, "
        f"{len(scores) - len(nonzero)} skipped (no face), "
        f"median={median_score:.1f}%, "
        f"min={min(nonzero, default=0):.1f}%, "
        f"max={max(nonzero, default=0):.1f}%\n"
        f"  scores: [{scores_str}]"
    )
    return median_score


def _evaluate_all_clips(
    videos: list[bytes], reference_face_bytes: bytes | None
) -> list[float]:
    """Evaluate face similarity for all clips in parallel."""
    with ThreadPoolExecutor(max_workers=len(videos)) as pool:
        futures = [
            pool.submit(_evaluate_clip, v, reference_face_bytes, idx)
            for idx, v in enumerate(videos)
        ]
        return [f.result() for f in futures]


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------
async def run_animate_product_fitting(
    front_image: bytes,
    back_image: bytes | None = None,
    framing: str = "full_body",
    number_of_videos: int = 4,
    prompt: str = "",
) -> AsyncGenerator[dict, None]:
    """Animate product fitting images into videos.

    Args:
        front_image: Image bytes of the model wearing garments (front view).
        back_image: Optional image bytes of the model wearing garments (back view).
        framing: Framing of the input image (e.g., "full_body", "lower_body").
        number_of_videos: Number of videos to generate.
        prompt: Optional custom animation prompt.

    Yields:
        dict with status updates.

    """
    _ensure_config()

    # Check if face is present in the reference image (use front image)
    reference_face_bytes = await asyncio.get_event_loop().run_in_executor(
        None, crop_face, front_image
    )

    # Validate model photo only if face is present
    if reference_face_bytes is not None:
        validation = await asyncio.get_event_loop().run_in_executor(
            None, validate_model_photo, front_image
        )
        if not validation["valid"]:
            logger.warning(
                f"[AnimateFitting] Validation failed: {validation['reason']}"
            )
            yield {"status": "error", "detail": validation["reason"]}
            return
    else:
        logger.info(
            "[AnimateFitting] No face detected in input image, skipping face validation."
        )

    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")

    veo_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )

    yield {"status": "generating_videos"}

    if prompt:
        effective_prompt = prompt.strip()
        prompt_name = "CUSTOM"
    elif framing == "lower_body":
        effective_prompt = LOWER_BODY_VEO_PROMPT
        prompt_name = "LOWER_BODY"
    else:
        effective_prompt = DEFAULT_VEO_PROMPT
        prompt_name = "DEFAULT"

    logger.info(f"[AnimateFitting] Using {prompt_name} prompt")

    reference_images = _create_reference_images(
        front_image, back_image, framing
    )

    EARLY_ABORT_THRESHOLD = 40.0
    MAX_RETRIES = 5

    def _check_first_clip(video_bytes: bytes) -> bool:
        if reference_face_bytes is None:
            return True  # Skip check if no face
        score = _evaluate_clip(video_bytes, reference_face_bytes, clip_index=0)
        logger.info(
            f"[AnimateFitting] First clip early check: score = {score:.1f}%"
        )
        return score >= EARLY_ABORT_THRESHOLD

    filtered = []
    scores = []

    def _generate_one(_index):
        return generate_veo_r2v(
            client=veo_client,
            reference_images=reference_images,
            prompt=effective_prompt,
            model=VEO_MODEL,
            duration=8,
            person_generation="allow_all",
            aspect_ratio="9:16",
            seed=0,
        )

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            logger.info(
                f"[AnimateFitting] Retry {attempt}/{MAX_RETRIES}: regenerating videos"
            )

        # Generate videos in parallel
        with ThreadPoolExecutor(max_workers=number_of_videos) as executor:
            futures = [
                executor.submit(_generate_one, i)
                for i in range(number_of_videos)
            ]

            first_checked = False
            video_bytes_list = [None] * number_of_videos
            future_to_index = {f: i for i, f in enumerate(futures)}
            aborted = False

            from concurrent.futures import as_completed

            for completed in as_completed(futures):
                idx = future_to_index[completed]
                video_bytes_list[idx] = completed.result()

                if not first_checked and reference_face_bytes is not None:
                    first_checked = True
                    if not _check_first_clip(video_bytes_list[idx]):
                        logger.warning(
                            "[AnimateFitting] First clip failed check — cancelling remaining clips"
                        )
                        for f in futures:
                            f.cancel()
                        aborted = True
                        break

        if aborted:
            if attempt == MAX_RETRIES:
                logger.error(
                    f"[AnimateFitting] All {MAX_RETRIES} attempts failed early-abort check"
                )
            continue

        video_bytes_list = [v for v in video_bytes_list if v is not None]

        # Evaluate
        scores = await asyncio.get_event_loop().run_in_executor(
            None, _evaluate_all_clips, video_bytes_list, reference_face_bytes
        )
        logger.info(f"[AnimateFitting] Scores: {scores}")

        ranked = sorted(
            zip(scores, video_bytes_list, strict=False),
            key=lambda x: x[0],
            reverse=True,
        )
        scores = [s for s, _ in ranked]
        sorted_videos = [v for _, v in ranked]

        filtered = [
            (s, v)
            for s, v in zip(scores, sorted_videos, strict=False)
            if s >= MIN_SIMILARITY_THRESHOLD
        ]

        if filtered or reference_face_bytes is None:
            # If no face, we don't filter by face score, just take what we got
            if reference_face_bytes is None:
                filtered = [(100.0, v) for v in video_bytes_list]
            break

    if not filtered:
        best_score = scores[0] if scores else 0.0
        yield {
            "status": "error",
            "detail": (
                f"All generated videos failed face similarity check "
                f"after {MAX_RETRIES} attempts "
                f"(best: {best_score:.1f}%)."
            ),
        }
        return

    final_scores = [s for s, _ in filtered]
    final_videos = [v for _, v in filtered]

    encoded_videos = [base64.b64encode(v).decode("utf-8") for v in final_videos]
    filenames = [f"video_fitting_{uuid.uuid4()}.mp4" for _ in final_videos]

    yield {
        "status": "videos",
        "videos": encoded_videos,
        "scores": final_scores,
        "filenames": filenames,
    }
