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

"""Clothes Video VTO - Reference-to-Video (R2V) pipeline.

Uses Veo 3.1 R2V mode with reference images (lower body + upper body + face)
to generate natural catwalk-style animation videos.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

from PIL import Image
from workflows.shared.debug_utils import save_debug_image
from workflows.shared.image_utils import create_canvas, crop_face
from workflows.shared.veo_utils import generate_veo_r2v

logger = logging.getLogger(__name__)

VEO_MODEL = "veo-3.1-generate-001"

DEFAULT_VEO_PROMPT = """
Subject: The exactly same person from the reference image, wearing the exactly same outfit. The person's identity, face, facial expression, body, skin tone, and hair must remain perfectly consistent with the reference image throughout the entire video. The head must always face straight forward toward the camera — never turning, tilting, or rotating to the side.
Scene: A minimalistic setting with a plain grey concrete wall and matching concrete floor.

Sequence 1 (00:00 - 00:02):
Action: The exactly same person from the reference image is standing still for a split second, then beginning to take the first slow steps forward toward the camera.
Light and camera movement: Static camera; low-angle framing focused strictly from the waist down to the shoes; the head and face are completely out of frame. Soft, even, neutral studio lighting.

Sequence 2 (00:02 - 00:04):
Action: The exactly same person continues to walk forward with a steady, confident stride and natural arm movement. The person's appearance and outfit remain identical to the reference image. Subtle natural body language: the shoulders shift gently with each step.
Light and camera movement: Camera begins a very slow tilt upward as the person approaches; the framing moves up to the shoulders, but the face remains out of frame. Consistent neutral studio lighting.

Sequence 3 (00:04 - 00:06):
Action: The exactly same person continues the unhurried walk toward the lens, now closer to the camera. The face, when revealed, must match the reference image exactly — same expression, same features. The person keeps their eyes open and steady with a confident gaze — minimal blinking (at most one brief natural blink). The head stays perfectly straight and forward, never turning or tilting.
Light and camera movement: The camera tilts up further to reveal the face for the first time; the framing is now a medium-full shot including the head. Consistent soft studio lighting.

Sequence 4 (00:06 - 00:08):
Action: The exactly same person slows down the pace and comes to a complete stop very close to the camera, looking directly into the lens. The face, identity, and expression must be identical to the reference image. The person keeps their eyes open with a calm, steady expression. The head does not move or turn. The gaze stays fixed on the camera lens.
Light and camera movement: Camera movement stops; final framing is a medium shot (waist up), focusing on the face and upper body of the exactly same person as shown in the reference image. Soft, neutral studio lighting.
 """


def _create_three_framings(image_bytes: bytes) -> tuple[bytes, bytes, bytes]:
    """Create three framings from a full-body image: lower body, upper body, face.

    Matches the animation flow: video starts on the lower body, reveals upper body
    as the model walks, then shows the face close-up at the end.

    Returns:
        (lower_body_png, upper_body_png, face_png)

    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = img.size

    # Lower body: bottom 60% of image (waist down to shoes)
    lower_body_img = img.crop((0, int(height * 0.4), width, height))

    # Upper body: top 40% of image (head down to chest/midriff)
    upper_body_img = img.crop((0, 0, width, int(height * 0.4)))

    # Face: use crop_face with tight padding (face with minimal context)
    face_bytes = crop_face(image_bytes, padding_ratio=0.5)
    if face_bytes is None:
        # Fallback: top 30% of image
        logger.warning("No face detected for face crop, using top 30%")
        face_img = img.crop((0, 0, width, int(height * 0.3)))
    else:
        face_img = Image.open(BytesIO(face_bytes)).convert("RGB")

    # Convert all to PNG bytes
    def _to_png(pil_img: Image.Image) -> bytes:
        buf = BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    lower_body_png = _to_png(lower_body_img)
    upper_body_png = _to_png(upper_body_img)
    face_png = _to_png(face_img)

    # Save debug images
    save_debug_image(
        lower_body_png, "01_lower_body", prefix="video_vto_framing"
    )
    save_debug_image(
        upper_body_png, "02_upper_body", prefix="video_vto_framing"
    )
    save_debug_image(face_png, "03_face", prefix="video_vto_framing")

    logger.info(
        f"Created 3 framings: lower_body={lower_body_img.size}, "
        f"upper_body={upper_body_img.size}, face={face_img.size}"
    )

    return lower_body_png, upper_body_png, face_png


def run_r2v_pipeline(
    veo_client,
    model_image_bytes,
    prompt,
    number_of_videos=4,
    upscale_client=None,
    original_model_image_bytes=None,
    first_clip_check=None,
):
    """Run the clothes video VTO R2V pipeline.

    Uses Veo 3.1 R2V mode with three reference images at different framings
    (lower body, upper body, face) from the VTO result to generate natural
    catwalk-style animation videos matching the animation flow.

    Args:
        veo_client: Veo client for video generation
        model_image_bytes: VTO result image (model wearing clothes) as bytes
        prompt: Animation prompt for Veo
        number_of_videos: Number of videos to generate (default: 4)
        upscale_client: GenAI client for Imagen upscaling (GLOBAL_REGION), or None to skip
        original_model_image_bytes: Original model photo before VTO (unused, kept for API compat)
        first_clip_check: Optional callable(video_bytes) -> bool. Called as soon
            as the first clip completes. If it returns False, remaining clips are
            cancelled and the result will have "aborted": True.

    Returns:
        dict with keys:
            - videos: list of video bytes
            - first_frame: lower body reference image bytes (PNG)
            - last_frame: face reference image bytes (PNG)
            - aborted: bool (True if first_clip_check failed and remaining were cancelled)

    """
    save_debug_image(
        model_image_bytes, "00_vto_original", prefix="video_vto_framing"
    )

    # Create three framings from VTO result (no upscaling): lower body, upper body, face
    lower_body_png, upper_body_png, face_png = _create_three_framings(
        model_image_bytes
    )

    # Fit each framing onto a 16:9 canvas (as large as possible, no margins)
    lower_body_png = create_canvas(lower_body_png, margin_top=0, margin_side=0)
    upper_body_png = create_canvas(upper_body_png, margin_top=0, margin_side=0)
    face_png = create_canvas(face_png, margin_top=0, margin_side=0)

    save_debug_image(
        lower_body_png, "04_lower_body_canvas", prefix="video_vto_framing"
    )
    save_debug_image(
        upper_body_png, "05_upper_body_canvas", prefix="video_vto_framing"
    )
    save_debug_image(face_png, "06_face_canvas", prefix="video_vto_framing")

    # Build reference images list: [lower body, upper body, face]
    reference_images = [lower_body_png, upper_body_png, face_png]

    # Generate videos with Veo R2V — N calls in parallel (1 video each)
    logger.info(
        f"Generating {number_of_videos} Veo 3.1 R2V videos in parallel..."
    )

    def _generate_one(_index):
        return generate_veo_r2v(
            client=veo_client,
            reference_images=reference_images,
            prompt=prompt,
            model=VEO_MODEL,
            duration=8,
            person_generation="allow_adult",
        )

    base_result = {
        "first_frame": lower_body_png,
        "last_frame": face_png,
    }

    with ThreadPoolExecutor(max_workers=number_of_videos) as executor:
        futures = [
            executor.submit(_generate_one, i) for i in range(number_of_videos)
        ]

        if first_clip_check is None:
            # No early-abort check — collect all results as before
            video_bytes_list = [f.result() for f in futures]
            logger.info(f"Generated {len(video_bytes_list)} R2V videos")
            return {**base_result, "videos": video_bytes_list, "aborted": False}

        # Early-abort: check the first clip that completes
        first_checked = False
        video_bytes_list = [None] * number_of_videos
        future_to_index = {f: i for i, f in enumerate(futures)}

        for completed in as_completed(futures):
            idx = future_to_index[completed]
            video_bytes_list[idx] = completed.result()

            if not first_checked:
                first_checked = True
                if not first_clip_check(video_bytes_list[idx]):
                    # First clip failed — cancel all remaining futures
                    logger.warning(
                        "[R2V] First clip failed check — cancelling remaining clips"
                    )
                    for f in futures:
                        f.cancel()
                    return {**base_result, "videos": [], "aborted": True}

    # All completed and first clip passed
    video_bytes_list = [v for v in video_bytes_list if v is not None]
    logger.info(f"Generated {len(video_bytes_list)} R2V videos")
    return {**base_result, "videos": video_bytes_list, "aborted": False}
