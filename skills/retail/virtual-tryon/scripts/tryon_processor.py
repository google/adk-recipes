# Copyright 2026 Google LLC
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

"""Virtual try-on processing engine.

Handles image try-on (via general-purpose Gemini image models: gemini-2.5-flash-image
or gemini-2.5-pro-image) and video try-on (via Veo 3.1 reference-to-video mode).
"""

import base64
import io
import logging
import os
import time
import uuid
from pathlib import Path

from google.cloud import storage
from google.genai import types
from google.genai.types import Image as GenaiImage
from google.genai.types import VideoGenerationReferenceImage
from PIL import Image

from scripts.config import config

logger = logging.getLogger(__name__)

IMAGE_MODELS = {
    "gemini-2.5-flash-image": "gemini-2.5-flash-image",
    "gemini-2.5-pro-image": "gemini-2.5-pro-image",
    "flash": "gemini-2.5-flash-image",
    "pro": "gemini-2.5-pro-image",
}

VIDEO_MODELS = {"veo": "veo-3.1-generate-001"}


def _get_client(project_id: str, location: str | None = None):
    """Create google-genai Client."""
    from google import genai

    return genai.Client(
        vertexai=True,
        project=project_id,
        location=location or config.GEMINI_MODEL_LOCATION,
        http_options={"timeout": 60000},
    )


def _load_image_bytes(
    image_path_or_uri: str, project_id: str | None = None
) -> bytes:
    """Load image bytes from local file or GCS URI."""
    if image_path_or_uri.startswith("gs://"):
        if not project_id:
            # storage.Client().project is a plain string (the resolved
            # ADC project id); do NOT tuple-unpack it.
            project_id = storage.Client().project
        gcs_client = storage.Client(project=project_id)
        bucket_name, blob_path = image_path_or_uri[5:].split("/", 1)
        bucket = gcs_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        return blob.download_as_bytes()

    # Try base64 decoding if it seems to be base64
    if len(image_path_or_uri) > 100 and not os.path.exists(image_path_or_uri):
        try:
            return base64.b64decode(image_path_or_uri)
        except Exception:
            pass

    # Read local file
    with open(image_path_or_uri, "rb") as f:
        return f.read()


def _upload_to_gcs(
    image_bytes: bytes,
    bucket_name: str,
    path_prefix: str,
    content_type: str,
    project_id: str,
) -> str:
    """Upload bytes to GCS and return the gs:// URI."""
    gcs_client = storage.Client(project=project_id)
    bucket = gcs_client.bucket(bucket_name)
    blob_name = f"{path_prefix}/{uuid.uuid4()}"
    if content_type == "image/jpeg":
        blob_name += ".jpg"
    elif content_type == "video/mp4":
        blob_name += ".mp4"
    else:
        blob_name += ".png"

    blob = bucket.blob(blob_name)
    blob.upload_from_string(image_bytes, content_type=content_type)
    return f"gs://{bucket_name}/{blob_name}"


def crop_face_simple(image_bytes: bytes) -> bytes:
    """Fallback simple crop to get top center of the image (often holds the face/head)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size
    # Crop top 35% of the image horizontally centered
    left = int(width * 0.2)
    right = int(width * 0.8)
    bottom = int(height * 0.35)
    face_img = img.crop((left, 0, right, bottom))

    buf = io.BytesIO()
    face_img.save(buf, format="PNG")
    return buf.getvalue()


def _create_canvas_16_9(image_bytes: bytes) -> bytes:
    """Pad image to a 16:9 canvas (as expected by Veo)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size

    # Target 16:9 aspect ratio
    target_ratio = 16 / 9
    current_ratio = width / height

    if current_ratio > target_ratio:
        # Too wide, pad height
        new_height = int(width / target_ratio)
        new_width = width
    else:
        # Too tall, pad width
        new_width = int(height * target_ratio)
        new_height = height

    canvas = Image.new(
        "RGB", (new_width, new_height), (240, 240, 240)
    )  # Neutral grey bg
    # Center original image
    offset_x = (new_width - width) // 2
    offset_y = (new_height - height) // 2
    canvas.paste(img, (offset_x, offset_y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def generate_tryon_image(
    person_image_path: str,
    product_image_path: str,
    project_id: str,
    output_bucket: str | None = None,
    model_name: str | None = None,
    product_category: str = "clothing",
    product_description: str = "",
    location: str | None = None,
) -> dict:
    """Generate try-on image using Gemini Image model.

    Args:
        person_image_path: Path/URI to the user photo.
        product_image_path: Path/URI to the product image.
        project_id: GCP project ID.
        output_bucket: optional GCS bucket name to save the generated image.
        model_name: alias ("flash" / "pro") or full model id.
            Defaults to config.GEMINI_IMAGE_MODEL.
        product_category: Category like clothing, eyewear, jewelry, etc.
        product_description: Textual description to aid Gemini prompt.
        location: Vertex AI region. Defaults to config.GEMINI_MODEL_LOCATION.
    """
    client = _get_client(project_id, location)

    person_bytes = _load_image_bytes(person_image_path, project_id)
    product_bytes = _load_image_bytes(product_image_path, project_id)

    effective_model = model_name or config.GEMINI_IMAGE_MODEL
    resolved_model = IMAGE_MODELS.get(effective_model, IMAGE_MODELS["flash"])
    logger.info(
        f"Generating Try-On Image using model: {resolved_model} for category: {product_category}"
    )

    # Unified prompt structure matching best practices in genmedia-for-commerce
    system_prompt = (
        "You are an expert fashion photographer, **high-end retoucher**, and virtual try-on specialist. "
        "Your task is to dress the model in new garments while preserving their identity and pose. "
        "**You must show the ENTIRE person from head to toe, including feet and shoes. The full body must be visible.** "
        "**You must improve the image quality, fixing any input noise or masking artifacts, while ensuring the subject looks exactly like the reference.** "
        "**LIGHTING & COMPOSITING:** You must apply uniform, consistent lighting across the entire image — the model's face, body, and garments must share the same light direction, intensity, and color temperature. "
        "Add natural soft shadows beneath the feet/shoes and subtle contact shadows where garments meet the body. "
        "The result must look like a single cohesive photograph, never like a cut-out pasted onto a background."
    )

    scenario = "a plain light grey studio environment"
    desc = product_description or f"a {product_category} item"

    user_task = (
        f"### TASK: FULL BODY VIRTUAL TRY-ON & RESTORATION\n\n"
        f"Dress the model in the provided garments and generate a **full body head-to-toe shot**:\n\n"
        f"1. **PRESERVE IDENTITY & ANATOMY** - Keep the exact pose, body shape, body size, facial features, and skin tone. Do NOT make the person thinner or change their body proportions in any way.\n"
        f"2. **REPLACE THE CLOTHES** - Fit the provided garments naturally onto the model. Adapt the garment size to the person's actual body — the clothes should look like they fit this specific person, not like the person was changed to fit the clothes.\n"
        f"3. **FULL BODY FRAMING** - The image MUST show the entire person from head to feet. Do NOT crop at the knees or waist. The feet and shoes must be visible at the bottom of the frame.\n"
        f"4. **COMPLETE THE OUTFIT** - If not all garments are provided, add appropriate complementary clothing (pants, shoes, etc.) that matches the style and formality of the provided garments.\n"
        f"5. **IMAGE RESTORATION** - Denoise the subject and correct any jagged masking edges or artifacts to ensure a seamless, high-fidelity result.\n"
        f"6. **UNIFORM LIGHTING** - Apply consistent studio lighting across the entire person — face, body, and garments must share the same light direction, intensity, and color temperature.\n"
        f"7. **NATURAL SHADOWS** - Add a soft, natural drop shadow beneath the person's feet/shoes on the ground plane, and subtle contact shadows where garments meet the body.\n"
        f"8. **PHOTOREALISTIC RESULT** - The final output should look like a single high-resolution studio photograph, not a composite.\n\n"
        f"Setting: {scenario}\n"
        f"Garment Description: {desc}"
    )

    response = client.models.generate_content(
        model=resolved_model,
        contents=[
            types.Part.from_bytes(data=person_bytes, mime_type="image/jpeg"),
            types.Part.from_bytes(data=product_bytes, mime_type="image/jpeg"),
            user_task,
        ],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            system_instruction=system_prompt,
            temperature=0.1,
        ),
    )

    # Extract image parts
    image_parts = [
        p
        for p in response.candidates[0].content.parts
        if p.inline_data and p.inline_data.mime_type.startswith("image/")
    ]
    if not image_parts:
        raise RuntimeError(
            "Gemini content generation did not return any image data."
        )

    result_bytes = image_parts[0].inline_data.data

    # Save output
    if output_bucket:
        output_uri = _upload_to_gcs(
            result_bytes, output_bucket, "vto-images", "image/jpeg", project_id
        )
        return {
            "image_bytes": result_bytes,
            "output_uri": output_uri,
            "model_used": resolved_model,
        }
    else:
        # Save locally in a temp folder
        out_dir = Path("tmp/vto-outputs")
        out_dir.mkdir(parents=True, exist_ok=True)
        local_path = out_dir / f"tryon_{uuid.uuid4().hex[:8]}.jpg"
        with open(local_path, "wb") as f:
            f.write(result_bytes)
        return {
            "image_bytes": result_bytes,
            "output_path": str(local_path.resolve()),
            "model_used": resolved_model,
        }


def generate_tryon_video(
    tryon_image_bytes: bytes,
    project_id: str,
    output_bucket: str | None = None,
    scene_description: str = "a minimal fashion studio catwalk walk",
    location: str | None = None,
) -> dict:
    """Generate try-on video using Veo 3.1 Reference-to-Video (R2V).

    Args:
        tryon_image_bytes: The output image bytes from `generate_tryon_image`
        project_id: GCP project ID
        output_bucket: optional GCS bucket to save video
        scene_description: Description of the catwalk animation setting
        location: Vertex AI region. Defaults to config.GEMINI_MODEL_LOCATION.
    """
    client = _get_client(project_id, location)

    # 1. Create reference framings from the try-on composite
    logger.info("Creating reference framings from Try-On image...")
    img = Image.open(io.BytesIO(tryon_image_bytes)).convert("RGB")
    width, height = img.size

    # Split: lower body (bottom 60%) and upper body (top 40%)
    lower_body_img = img.crop((0, int(height * 0.4), width, height))
    upper_body_img = img.crop((0, 0, width, int(height * 0.4)))
    face_bytes = crop_face_simple(tryon_image_bytes)

    def _to_png_bytes(pil_img):
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    lower_bytes = _to_png_bytes(lower_body_img)
    upper_bytes = _to_png_bytes(upper_body_img)

    # Pad to 16:9 canvas
    lower_ref = _create_canvas_16_9(lower_bytes)
    upper_ref = _create_canvas_16_9(upper_bytes)
    face_ref = _create_canvas_16_9(face_bytes)

    ref_images = [lower_ref, upper_ref, face_ref]

    # Prepare Reference Images for Veo Client
    ref_images_list = []
    for img_bytes in ref_images:
        ref_image = VideoGenerationReferenceImage(
            image=GenaiImage(imageBytes=img_bytes, mime_type="image/png"),
            reference_type="asset",
        )
        ref_images_list.append(ref_image)

    # Catwalk sequence prompt from genmedia-for-commerce
    prompt = (
        "Subject: The exactly same person from the reference image, wearing the exactly same outfit. "
        "The person's identity, face, facial expression, body, skin tone, and hair must remain perfectly consistent with the reference image throughout the entire video. "
        "The head must always face straight forward toward the camera — never turning, tilting, or rotating to the side. "
        "Scene: A minimalistic studio setting with a clean, solid, pure white background. "
        "Sequence 1 (00:00 - 00:02): "
        "Action: The exactly same person from the reference image is standing still for a split second, then beginning to take the first slow steps forward toward the camera. "
        "Light and camera movement: Static camera; low-angle framing focused strictly from the waist down to the shoes; the head and face are completely out of frame. Soft, even, neutral studio lighting. "
        "Sequence 2 (00:02 - 00:04): "
        "Action: The exactly same person continues to walk forward with a steady, confident stride and natural arm movement. The person's appearance and outfit remain identical to the reference image. Subtle natural body language: the shoulders shift gently with each step. "
        "Light and camera movement: Camera begins a very slow tilt upward as the person approaches; the framing moves up to the shoulders, but the face remains out of frame. Consistent neutral studio lighting. "
        "Sequence 3 (00:04 - 00:06): "
        "Action: The exactly same person continues the unhurried walk toward the lens, now closer to the camera. The face, when revealed, must match the reference image exactly — same expression, same features. The person keeps their eyes open and steady with a confident gaze — minimal blinking (at most one brief natural blink). The head stays perfectly straight and forward, never turning or tilting. "
        "Light and camera movement: The camera tilts up further to reveal the face for the first time; the framing is now a medium-full shot including the head. Consistent soft studio lighting. "
        "Sequence 4 (00:06 - 00:08): "
        "Action: The exactly same person slows down the pace and comes to a complete stop very close to the camera, looking directly into the lens. The face, identity, and expression must be identical to the reference image. The person keeps their eyes open with a calm, steady expression. The head does not move or turn. The gaze stays fixed on the camera lens. "
        "Light and camera movement: Camera movement stops; final framing is a medium shot (waist up), focusing on the face and upper body of the exactly same person as shown in the reference image. Soft, neutral studio lighting."
    )

    video_bytes = None
    try:
        logger.info("Calling Veo 3.1 video generation API...")
        operation = client.models.generate_videos(
            model=VIDEO_MODELS["veo"],
            prompt=prompt,
            config=types.GenerateVideosConfig(
                aspect_ratio="9:16",
                number_of_videos=1,
                duration_seconds=8,  # Veo R2V strictly requires exactly 8 seconds
                reference_images=ref_images_list,
                person_generation="allow_adult",
                generate_audio=False,
            ),
        )

        # Poll for completion
        while not operation.done:
            time.sleep(2)
            operation = client.operations.get(operation)

        if getattr(operation, "error", None):
            raise RuntimeError(
                f"Veo video generation failed: {operation.error}"
            )

        result = getattr(operation, "result", None)
        if not result or not getattr(result, "generated_videos", None):
            raise RuntimeError("Veo video generation returned no video files.")

        video_bytes = result.generated_videos[0].video.video_bytes
    except Exception as e:
        logger.error(f"VTO Video generation failed: {e}")
        raise RuntimeError(f"Veo video generation failed: {e}") from e

    if output_bucket:
        video_uri = _upload_to_gcs(
            video_bytes, output_bucket, "vto-videos", "video/mp4", project_id
        )
        return {"video_bytes": video_bytes, "output_uri": video_uri}
    else:
        out_dir = Path("tmp/vto-outputs")
        out_dir.mkdir(parents=True, exist_ok=True)
        local_path = out_dir / f"tryon_walk_{uuid.uuid4().hex[:8]}.mp4"
        with open(local_path, "wb") as f:
            f.write(video_bytes)
        return {
            "video_bytes": video_bytes,
            "output_path": str(local_path.resolve()),
        }
