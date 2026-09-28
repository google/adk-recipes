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

"""Video VTO pipelines.

- run_animate_model: Takes a ready image and generates videos (R2V + face eval).
  Standalone entry point when the image is already prepared.

- run_video_vto: Full pipeline — runs image VTO first, picks the best result,
  then delegates to run_animate_model for video generation.

Both yield SSE-friendly dicts as each stage completes.
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
from workflows.image_vto.clothes.pipeline import VTOResult, run_image_vto
from workflows.shared.image_utils import crop_face
from workflows.shared.person_eval import (
    evaluate_person_match,
    validate_model_photo,
)
from workflows.shared.video_utils import extract_frames_as_bytes_list
from workflows.video_vto.clothes.generate_video_util import (
    DEFAULT_VEO_PROMPT,
    run_r2v_pipeline,
)

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


# ---------------------------------------------------------------------------
# Face evaluation helpers (moved from main.py)
# ---------------------------------------------------------------------------
def _evaluate_clip(
    video_bytes: bytes,
    reference_face_bytes: bytes,
    clip_index: int,
    start_seconds: float = 6.0,
    fps: int = 24,
) -> float:
    """Evaluate face similarity for a single clip.

    Only evaluates frames from start_seconds onward (the face is not visible
    in the early frames since the video starts framed on the lower body).
    Samples EVAL_SAMPLE_COUNT frames uniformly across the remaining frames.
    Returns the median similarity_percentage (ignoring zeros).
    """
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
    videos: list[bytes], reference_face_bytes: bytes
) -> list[float]:
    """Evaluate face similarity for all clips in parallel."""
    with ThreadPoolExecutor(max_workers=len(videos)) as pool:
        futures = [
            pool.submit(_evaluate_clip, v, reference_face_bytes, idx)
            for idx, v in enumerate(videos)
        ]
        return [f.result() for f in futures]


# ---------------------------------------------------------------------------
# Animate Model (video-only, no image VTO)
# ---------------------------------------------------------------------------
async def run_animate_model(
    model_image: bytes,
    number_of_videos: int = 4,
    prompt: str = "",
    reference_face_bytes: bytes | None = None,
) -> AsyncGenerator[dict, None]:
    """Animate a ready model image into catwalk-style videos.

    Takes an image of a model already wearing garments and generates
    videos using Veo 3.1 R2V mode. Skips image VTO entirely.

    Args:
        model_image: Image bytes of the model (already wearing garments).
        number_of_videos: Number of videos to generate. Default: 4.
        prompt: Optional custom animation prompt. Defaults to catwalk sequence.
        reference_face_bytes: Optional pre-cropped reference face for evaluation.
            If None, the face is extracted from model_image automatically.

    Yield order:
      1. {"status": "generating_videos"}
      2. {"status": "videos", "videos": [...], "scores": [...], "filenames": [...]}
         OR {"status": "error", "detail": "..."}

    """
    _ensure_config()

    # Validate model photo
    validation = await asyncio.get_event_loop().run_in_executor(
        None, validate_model_photo, model_image
    )
    if not validation["valid"]:
        logger.warning(
            f"[AnimateModel] Validation failed: {validation['reason']}"
        )
        yield {"status": "error", "detail": validation["reason"]}
        return

    # Extract reference face if not provided
    if reference_face_bytes is None:
        reference_face_bytes = await asyncio.get_event_loop().run_in_executor(
            None, crop_face, model_image
        )

    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")

    veo_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )

    yield {"status": "generating_videos"}

    effective_prompt = prompt.strip() if prompt else DEFAULT_VEO_PROMPT

    EARLY_ABORT_THRESHOLD = 40.0
    MAX_RETRIES = 5

    eval_ref = reference_face_bytes

    def _check_first_clip(video_bytes: bytes) -> bool:
        nonlocal eval_ref
        score = _evaluate_clip(video_bytes, eval_ref, clip_index=0)
        logger.info(
            f"[AnimateModel] First clip early check: score = {score:.1f}%"
        )
        return score >= EARLY_ABORT_THRESHOLD

    filtered = []
    scores = []

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            logger.info(
                f"[AnimateModel] Retry {attempt}/{MAX_RETRIES}: "
                f"regenerating videos (first clip scored below {EARLY_ABORT_THRESHOLD}%)"
            )

        r2v_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda e=eval_ref: run_r2v_pipeline(
                veo_client=veo_client,
                upscale_client=None,
                model_image_bytes=model_image,
                prompt=effective_prompt,
                number_of_videos=number_of_videos,
                first_clip_check=_check_first_clip if e else None,
            ),
        )

        if eval_ref is None:
            eval_ref = r2v_result["first_frame"]

        if r2v_result.get("aborted"):
            if attempt == MAX_RETRIES:
                logger.error(
                    f"[AnimateModel] All {MAX_RETRIES} attempts failed early-abort check"
                )
            continue

        scores = await asyncio.get_event_loop().run_in_executor(
            None, _evaluate_all_clips, r2v_result["videos"], eval_ref
        )
        logger.info(f"[AnimateModel] Face similarity scores: {scores}")

        ranked = sorted(
            zip(scores, r2v_result["videos"], strict=True),
            key=lambda x: x[0],
            reverse=True,
        )
        scores = [s for s, _ in ranked]
        sorted_videos = [v for _, v in ranked]

        filtered = [
            (s, v)
            for s, v in zip(scores, sorted_videos, strict=True)
            if s >= MIN_SIMILARITY_THRESHOLD
        ]
        rejected = len(sorted_videos) - len(filtered)
        if rejected > 0:
            logger.info(
                f"[AnimateModel] Filtered out {rejected} video(s) below "
                f"{MIN_SIMILARITY_THRESHOLD}% threshold"
            )

        break

    if not filtered:
        best_score = scores[0] if scores else 0.0
        yield {
            "status": "error",
            "detail": (
                f"All generated videos failed face similarity check "
                f"after {MAX_RETRIES} attempts "
                f"(best: {best_score:.1f}%). Please try again with a clearer face photo."
            ),
        }
        return

    final_scores = [s for s, _ in filtered]
    final_videos = [v for _, v in filtered]

    encoded_videos = [base64.b64encode(v).decode("utf-8") for v in final_videos]
    filenames = [f"video_vto_{uuid.uuid4()}.mp4" for _ in final_videos]

    yield {
        "status": "videos",
        "videos": encoded_videos,
        "scores": final_scores,
        "filenames": filenames,
    }


# ---------------------------------------------------------------------------
# Full Video VTO (image VTO → animate model)
# ---------------------------------------------------------------------------
async def run_video_vto(
    full_body_image: bytes,
    garment_images: list[bytes | str],
    scenario: str = "a plain white studio background",
    num_variations: int = 3,
    face_image: bytes | None = None,
    number_of_videos: int = 4,
    prompt: str = "",
) -> AsyncGenerator[dict, None]:
    """Run the full video VTO pipeline. Yields dicts suitable for SSE.

    Runs image VTO first to generate the best static VTO image,
    then delegates to run_animate_model for video generation.

    For video-only (image already ready), use run_animate_model directly.

    Yield order:
      1. {"status": "generating_image"}
      2. {"status": "image_ready", "image_base64": "...", "final_score": ...}
      3. {"status": "generating_videos"}
      4. {"status": "videos", "videos": [...], "scores": [...], "filenames": [...]}
         OR {"status": "error", "detail": "..."}
    """
    _ensure_config()

    # --- Validate model photo ---
    face_source = face_image if face_image else full_body_image
    validation = await asyncio.get_event_loop().run_in_executor(
        None, validate_model_photo, face_source
    )
    if not validation["valid"]:
        logger.warning(f"[VideoVTO] Validation failed: {validation['reason']}")
        yield {"status": "error", "detail": validation["reason"]}
        return

    # ------------------------------------------------------------------
    # Step 1: Image VTO
    # ------------------------------------------------------------------
    yield {"status": "generating_image"}

    ready_results: list[VTOResult] = []
    reference_face_bytes: bytes | None = None

    async for result in run_image_vto(
        full_body_image,
        garment_images,
        scenario,
        num_variations,
        face_image,
        image_size="2K",
    ):
        if result.status == "reference_face":
            if result.reference_face_base64:
                reference_face_bytes = base64.b64decode(
                    result.reference_face_base64
                )
            continue
        if result.status == "ready":
            ready_results.append(result)

    if not ready_results:
        yield {
            "status": "error",
            "detail": "Image VTO completed without producing a valid result.",
        }
        return

    ready_results.sort(key=lambda r: r.final_score or 0, reverse=True)
    best = ready_results[0]
    logger.info(
        f"[VideoVTO] Picked best VTO variation {best.index} "
        f"with score {best.final_score}"
    )

    best_image_bytes = best.image
    best_image_b64 = best.image_base64 or (
        base64.b64encode(best_image_bytes).decode("utf-8")
        if best_image_bytes
        else None
    )

    yield {
        "status": "image_ready",
        "image_base64": best_image_b64,
        "final_score": best.final_score,
        "face_score": best.face_score,
    }

    # ------------------------------------------------------------------
    # Step 2: Delegate to run_animate_model for video generation
    # ------------------------------------------------------------------
    async for event in run_animate_model(
        model_image=best_image_bytes,
        number_of_videos=number_of_videos,
        prompt=prompt,
        reference_face_bytes=reference_face_bytes,
    ):
        yield event
