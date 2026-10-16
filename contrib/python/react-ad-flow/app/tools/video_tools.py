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

"""Video generation tools using two-stage pipeline.

Two-Stage Video Generation Pipeline:
    Stage 1: Scene Image (image model from config.IMAGE_GENERATION)
        - Input: Product image + CreativeVariation parameters
        - Output: Scene-ready first frame (model wearing product for
          wearables; product-centric hero shot otherwise)
        - Saved as: thumbnail

    Stage 2: Video Animation (Gemini Omni Flash via config.VIDEO_GEN_MODEL)
        - Input: Scene image + Animation prompt
        - Output: 4-second 9:16 video
        - Saved as: final video

HITL Workflow:
    - Videos are generated with status='generated'
    - NO metrics are created until activation
    - Use review_tools.activate_video() to push live
"""

import asyncio
import base64
import io
import json
import os
import time
from datetime import datetime
from typing import Any

from google import genai
from google.adk.tools import ToolContext
from google.genai import interactions as im
from google.genai import types
from PIL import Image as PILImage

from .. import storage
from ..config import (
    GENERATED_DIR,
    IMAGE_GENERATION,
    MODEL,
    VIDEO_GEN_MODEL,
)
from ..database.db import get_db_cursor, get_product
from ..models.product import Product
from ..models.variation import (
    PRESET_VARIATIONS,
    CreativeVariation,
    get_default_variation,
)
from ..models.video_properties import VideoProperties
from .prompt_archetypes import WEARABLE, resolve_archetype
from .prompt_builders import (
    build_creative_prompt,
    build_scene_image_prompt,
    build_video_animation_prompt,
)


def generate_video_prompt(
    metadata: dict, campaign_info: dict | None = None
) -> str:
    """Generate a compelling video prompt from image metadata.

    Creates cinematic fashion video descriptions based on:
    - Model characteristics (from image analysis)
    - Clothing details (color, style, pattern)
    - Setting suggestions
    - Campaign context

    Args:
        metadata: Image analysis metadata dictionary
        campaign_info: Optional campaign context for additional styling

    Returns:
        Generated video prompt string
    """
    model_desc = metadata.get("model_description", "a model")
    clothing_desc = metadata.get("clothing_description", "elegant clothing")
    setting_desc = metadata.get("setting_description", "In a beautiful setting")
    garment_type = metadata.get("garment_type", "outfit")
    movement = metadata.get("movement", "moves gracefully")
    camera_style = metadata.get("camera_style", "slowly pans")
    key_feature = metadata.get("key_feature", "the details")
    mood = metadata.get("mood", "elegant, aspirational")

    prompt = f"""A cinematic fashion video featuring {model_desc} wearing {clothing_desc}. {setting_desc}, the {garment_type} {movement}. Camera {camera_style}, capturing {key_feature}. Atmosphere: {mood}. Professional lighting, high-end fashion advertisement style."""

    return prompt


async def analyze_video(video_path: str) -> VideoProperties:
    """Analyze a generated video using Gemini to extract structured properties.

    Uses MODEL from config with structured output (JSON Schema from Pydantic).
    This enables correlation between video properties and performance metrics.

    Args:
        video_path: Path to the video file (filename, relative path, or absolute path)

    Returns:
        VideoProperties: Structured properties extracted from the video
    """
    print(f"[DEBUG analyze_video] Analyzing video: {video_path}")
    print(f"[DEBUG analyze_video] Storage mode: {storage.get_storage_mode()}")

    # Extract just the filename for storage operations
    # Handle various input formats: gs:// URLs, absolute paths, relative paths, or bare filenames
    if video_path.startswith("gs://"):
        # GCS URL: gs://bucket/generated/filename.mp4 -> filename.mp4
        filename = video_path.rsplit("/", maxsplit=1)[-1]
    elif os.path.isabs(video_path):
        # Absolute local path: /path/to/filename.mp4 -> filename.mp4
        filename = os.path.basename(video_path)
    elif video_path.startswith("generated/"):
        # Relative path: generated/filename.mp4 -> filename.mp4
        filename = video_path.replace("generated/", "")
    else:
        # Already a filename
        filename = video_path

    print(f"[DEBUG analyze_video] Extracted filename: {filename}")

    # Check if video exists using storage abstraction
    if not storage.video_exists(filename):
        print(f"[DEBUG analyze_video] Video file not found: {filename}")
        # Return default properties if file not found
        return VideoProperties()

    client = genai.Client()

    # Read video file using storage abstraction
    print("[DEBUG analyze_video] Reading video file...")
    video_bytes = storage.read_video(filename)
    print(f"[DEBUG analyze_video] Video size: {len(video_bytes)} bytes")

    # Create video part for Gemini
    video_part = types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")

    prompt = """Analyze this retail video advertisement and extract structured properties.

Focus on:
1. **Mood and emotional tone**: What feeling does the video evoke? (quirky, warm, bold, serene, mysterious, playful, sophisticated, energetic, elegant, romantic)
2. **Mood intensity**: How strong is this mood? (0.0 to 1.0)
3. **Warmth**: Does the video convey warmth/comfort? (true/false)
4. **Visual style**: What is the overall visual treatment? (cinematic, documentary, editorial, commercial, artistic, minimalist, vintage, modern)
5. **Camera movement**: Primary camera movement (static, pan, orbit, track, dolly, crane, handheld, slow_zoom)
6. **Lighting style**: Lighting approach (natural, studio, dramatic, soft, high_key, low_key, golden_hour, neon)
7. **Energy level**: Pace and movement intensity (calm, moderate, dynamic, high_energy)
8. **Movement amount**: Amount of subject movement (0.0 to 1.0)
9. **Color temperature**: Overall color grading (warm, neutral, cool)
10. **Dominant colors**: List the primary 2-4 colors in the video
11. **Color saturation**: How saturated are the colors? (0.0 to 1.0)
12. **Subject count**: Number of models/subjects visible
13. **Product visibility**: How prominently is the product featured? (0.0 to 1.0)
14. **Setting type**: Setting category (outdoor, studio, urban, nature, indoor, beach, etc.)
15. **Time of day**: Time depicted (golden_hour, day, night, dawn, dusk)
16. **Style tags**: List of 3-5 descriptive style tags
17. **Audio type**: Type of audio if present (none, ambient, music_upbeat, music_calm, music_dramatic, dialogue, voiceover)
18. **Background complexity**: How complex/busy is the background? (0.0 to 1.0)

Respond with a JSON object matching the VideoProperties schema. Be precise and consistent in your analysis."""

    try:
        print(
            f"[DEBUG analyze_video] Calling Gemini {MODEL} for video analysis..."
        )
        response = client.models.generate_content(
            model=MODEL,
            contents=[video_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoProperties.model_json_schema(),
            ),
        )

        print("[DEBUG analyze_video] Response received")
        properties_dict = json.loads(response.text)
        print(
            f"[DEBUG analyze_video] Parsed properties: mood={properties_dict.get('mood')}, "
            f"energy={properties_dict.get('energy_level')}"
        )

        return VideoProperties(**properties_dict)

    except Exception as e:
        print(f"[DEBUG analyze_video] Error analyzing video: {e!s}")
        # Return default properties on error
        return VideoProperties()


# =============================================================================
# Two-Stage Video Generation Pipeline
# =============================================================================


async def generate_scene_image(
    product: Product,
    variation: CreativeVariation,
    product_image_bytes: bytes | None = None,
    archetype: str = "wearable",
) -> tuple[bytes, str]:
    """Stage 1: Generate a scene-ready first frame image.

    Creates an image of a model wearing the product in the desired setting
    (wearable archetype), or a product-centric hero shot (all other
    archetypes), which will be used as the first frame for video generation.

    Args:
        product: Product object with metadata (from products table)
        variation: CreativeVariation parameters controlling the scene
        product_image_bytes: Optional product image bytes for reference
        archetype: Prompt archetype from resolve_archetype() — controls the
            reference-image preamble sent to the image model

    Returns:
        Tuple of (scene_image_bytes, scene_prompt)
    """
    print(
        f"[DEBUG generate_scene_image] Starting scene generation for product: {product.name}"
    )
    print(f"[DEBUG generate_scene_image] Variation: {variation.name}")

    # Build scene prompt from product and variation
    scene_prompt = build_scene_image_prompt(product, variation)
    print(f"[DEBUG generate_scene_image] Scene prompt: {scene_prompt[:200]}...")

    client = genai.Client()

    # Native image generation via the configured Gemini image model
    # (config.IMAGE_GENERATION — GA default gemini-3-pro-image, env-overridable)
    try:
        contents = [scene_prompt]

        # If we have product image, include it as reference
        if product_image_bytes:
            print(
                "[DEBUG generate_scene_image] Including product image as reference"
            )
            image_part = types.Part.from_bytes(
                data=product_image_bytes, mime_type="image/png"
            )
            if archetype == "wearable":
                reference_instruction = (
                    "Use this product image as reference for the garment. "
                    "Generate a scene with a model wearing this exact garment:\n"
                )
            else:
                reference_instruction = (
                    "Use this product image as the exact visual reference for the product. "
                    "Generate a scene featuring this exact product — same shape, colors, "
                    "branding, and label design:\n"
                )
            contents = [reference_instruction, image_part, "\n" + scene_prompt]

        response = client.models.generate_content(
            model=IMAGE_GENERATION,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["image", "text"]
            ),
        )

        # Extract image from response
        scene_image_bytes = None
        for part in response.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data:
                scene_image_bytes = part.inline_data.data
                print(
                    f"[DEBUG generate_scene_image] Scene image generated: {len(scene_image_bytes)} bytes"
                )
                break

        if not scene_image_bytes:
            raise ValueError("No image generated in response")

        return scene_image_bytes, scene_prompt

    except Exception as e:
        print(f"[DEBUG generate_scene_image] Error: {e!s}")
        raise


_OMNI_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "incomplete",
    "budget_exceeded",
}


async def _wait_for_omni_interaction(
    client,
    interaction,
    *,
    max_wait_time: int = 600,
    poll_interval: int = 10,
    debug_label: str = "omni",
):
    """Poll an Omni Flash interaction to completion."""
    waited = 0
    status = getattr(interaction, "status", None)
    while status not in _OMNI_TERMINAL_STATUSES:
        if waited >= max_wait_time:
            raise TimeoutError(
                f"Video generation timed out after {max_wait_time} seconds (last status={status})"
            )
        print(
            f"[DEBUG {debug_label}] Waiting... ({waited}s elapsed, status={status})"
        )
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        interaction = client.interactions.get(interaction.id)
        status = getattr(interaction, "status", None)
    print(
        f"[DEBUG {debug_label}] Interaction completed after {waited}s with status={status}"
    )
    if status != "completed":
        raise ValueError(f"Video generation failed with status: {status}")
    return interaction


def _extract_omni_video_bytes(interaction) -> bytes:
    """Pull the model_output step's video content or output_video and base64-decode it."""
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for content in getattr(step, "content", None) or []:
            if getattr(content, "type", None) == "video":
                data = getattr(content, "data", None)
                if data:
                    if isinstance(data, str):
                        return base64.b64decode(data, validate=True)
                    elif isinstance(data, (bytes, bytearray)):
                        return bytes(data)
                uri = getattr(content, "uri", None)
                if uri:
                    raise ValueError(
                        f"Video delivered as uri, not inline: {uri}"
                    )
    output_video = getattr(interaction, "output_video", None)
    if output_video:
        data = getattr(output_video, "data", None)
        if data:
            if isinstance(data, str):
                return base64.b64decode(data, validate=True)
            elif isinstance(data, (bytes, bytearray)):
                return bytes(data)
    raise ValueError(
        "Omni Flash response completed but contained no video content"
    )


async def animate_scene_with_omni(
    scene_image_bytes: bytes,
    product: Product,
    variation: CreativeVariation,
    duration_seconds: int = 4,
) -> tuple[bytes, str]:
    """Stage 2: Animate a scene image into a video using Gemini Omni Flash.

    Args:
        scene_image_bytes: Scene image bytes from Stage 1
        product: Product object with metadata
        variation: CreativeVariation parameters
        duration_seconds: Video duration (default 4 seconds)

    Returns:
        Tuple of (video_bytes, video_prompt)
    """
    print(
        f"[DEBUG animate_scene_with_omni] Starting animation for: {product.name}"
    )
    print(f"[DEBUG animate_scene_with_omni] Duration: {duration_seconds}s")

    # Build animation-focused prompt
    video_prompt = build_video_animation_prompt(product, variation)
    print(
        f"[DEBUG animate_scene_with_omni] Animation prompt: {video_prompt[:200]}..."
    )

    client = genai.Client()

    image_b64 = base64.b64encode(scene_image_bytes).decode("ascii")
    user_step = im.UserInputStep(
        content=[
            im.ImageContent(data=image_b64, mime_type="image/png"),
            im.TextContent(text=video_prompt),
        ]
    )
    generation_config = im.GenerationConfig(
        video_config=im.VideoConfig(task="image_to_video")
    )
    response_format = im.VideoResponseFormat(
        type="video",
        delivery="inline",
        aspect_ratio="9:16",
    )

    print(
        f"[DEBUG animate_scene_with_omni] Calling Omni Flash ({VIDEO_GEN_MODEL})..."
    )
    interaction = client.interactions.create(
        model=VIDEO_GEN_MODEL,
        input=[user_step],
        generation_config=generation_config,
        response_format=response_format,
        background=True,
    )

    interaction = await _wait_for_omni_interaction(
        client, interaction, debug_label="animate_scene_with_omni"
    )
    video_bytes = _extract_omni_video_bytes(interaction)

    print(
        f"[DEBUG animate_scene_with_omni] Video generated: {len(video_bytes)} bytes"
    )
    return video_bytes, video_prompt


async def animate_scene_with_veo(
    scene_image_bytes: bytes,
    product: Product,
    variation: CreativeVariation,
    duration_seconds: int = 4,
) -> tuple[bytes, str]:
    """Deprecated alias for animate_scene_with_omni."""
    return await animate_scene_with_omni(
        scene_image_bytes=scene_image_bytes,
        product=product,
        variation=variation,
        duration_seconds=duration_seconds,
    )


def generate_video_filename(product_name: str, variation_name: str) -> str:
    """Generate a descriptive video filename.

    Format: {product-name}-{MMDDYY}-{variation-name}.mp4

    Args:
        product_name: Product name (already hyphenated)
        variation_name: Variation name (already hyphenated)

    Returns:
        Video filename string
    """
    date_str = datetime.now().strftime("%m%d%y")
    return f"{product_name}-{date_str}-{variation_name}.mp4"


def save_video_metadata(
    video_filename: str,
    product: dict[str, Any],
    variation: CreativeVariation,
    scene_prompt: str,
    video_prompt: str,
    pipeline_type: str = "two-stage",
) -> str:
    """Save video metadata alongside the video file.

    Creates a .txt file with the same name as the video containing
    all generation parameters for reproducibility.

    Args:
        video_filename: The video filename (without path)
        product: Product dictionary
        variation: CreativeVariation used
        scene_prompt: Stage 1 prompt
        video_prompt: Stage 2 prompt
        pipeline_type: 'two-stage' or 'single-stage'

    Returns:
        Path to the metadata file
    """
    metadata_filename = video_filename.replace(".mp4", ".txt")

    metadata_content = f"""Product: {product.name if product is not None else "unknown"}
Variation: {variation.name}
Pipeline: {pipeline_type.title()} {"(Scene + Animation)" if pipeline_type == "two-stage" else ""}
Generated: {datetime.now().isoformat()}

Variation Parameters:
{json.dumps(variation.to_dict(), indent=2)}

STAGE 1 - Scene Image Prompt:
{scene_prompt}

STAGE 2 - Video Generation Prompt:
{video_prompt}
"""

    # Save metadata file
    if storage.get_storage_mode() == "gcs":
        # For GCS, save alongside video
        storage.save_video(metadata_filename, metadata_content.encode("utf-8"))
        metadata_path = storage.get_video_path(metadata_filename)
    else:
        metadata_path = os.path.join(GENERATED_DIR, metadata_filename)
        with open(metadata_path, "w") as f:
            f.write(metadata_content)

    print(f"[DEBUG save_video_metadata] Saved metadata to: {metadata_path}")
    return metadata_path


def _variation_params_for_storage(
    variation: CreativeVariation, archetype: str
) -> dict[str, Any]:
    """Serialize a CreativeVariation for the campaign_videos.variation_params
    column, dropping model-only fields for archetypes with no human model in
    the shot.

    model_ethnicity and activity describe a person; consumable-hero /
    staged-product / product-hero archetypes have no model, so persisting
    those fields there is a legacy artifact from the fashion-only era (ws16
    Task 11, ws14 candidate). Only the WEARABLE archetype keeps them.
    """
    data = variation.to_dict()
    if archetype != WEARABLE:
        data.pop("model_ethnicity", None)
        data.pop("activity", None)
    return data


async def generate_video_from_product(
    campaign_id: int,
    product_id: int,
    variation: dict | None = None,
    use_two_stage: bool = True,
    duration_seconds: int = 4,
    tool_context: ToolContext = None,
) -> dict:
    """Generate a video ad using the two-stage pipeline.

    This is the primary video generation function that:
    1. Uses product from the products table
    2. Applies CreativeVariation parameters
    3. Generates via two-stage pipeline (scene image → video animation)
    4. Saves to campaign_videos table with status='generated'
    5. Does NOT create mock metrics (metrics only on activation)

    Args:
        campaign_id: The campaign to generate for
        product_id: The product ID from products table
        variation: Optional dict with variation parameters. Supported keys:
            - name: Unique variation identifier
            - model_ethnicity: asian, european, african, latina, south-asian, diverse
            - setting: studio, beach, urban, cafe, rooftop, garden, nature, etc.
            - mood: elegant, romantic, bold, playful, sophisticated, etc.
            - lighting: natural, studio, dramatic, soft, golden, neon, moody
            - activity: walking, standing, sitting, dancing, spinning, posing
            - camera_movement: orbit, pan, dolly, static, tracking, crane
            - time_of_day: golden-hour, sunrise, day, sunset, dusk, night
            - visual_style: cinematic, editorial, commercial, artistic
            - energy: calm, moderate, dynamic, high-energy
            If None, uses elegant studio defaults.
        use_two_stage: Use two-stage pipeline (default True)
        duration_seconds: Video duration (default 4 seconds)
        tool_context: Optional ADK ToolContext for artifact storage

    Returns:
        Dictionary with video details and status='generated'
    """
    print(
        f"[DEBUG generate_video_from_product] Starting for campaign_id={campaign_id}, product_id={product_id}"
    )

    # Ensure generated directory exists (only in local mode)
    if storage.get_storage_mode() == "local":
        os.makedirs(GENERATED_DIR, exist_ok=True)

    # Get campaign
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign {campaign_id} not found",
            }

    # Get product from products table
    product = get_product(product_id)
    if not product:
        return {"status": "error", "message": f"Product {product_id} not found"}

    print(f"[DEBUG generate_video_from_product] Product: {product.name}")
    print(f"[DEBUG generate_video_from_product] Campaign: {campaign['name']}")

    # Convert dict to CreativeVariation or use default
    # ADK 1.21+ requires dict instead of Pydantic model in function signature
    if variation is None:
        variation_obj = get_default_variation()
    elif isinstance(variation, dict):
        try:
            variation_obj = CreativeVariation.model_validate(
                {"name": "custom", **variation}
            )
        except Exception as e:
            return {
                "status": "error",
                "error": f"Invalid variation parameters: {e}",
                "hint": "Valid fields: "
                + ", ".join(CreativeVariation.model_fields.keys()),
            }
    elif isinstance(variation, CreativeVariation):
        # Already a CreativeVariation object (internal calls)
        variation_obj = variation
    else:
        return {
            "status": "error",
            "error": f"Invalid variation type: {type(variation).__name__}",
            "hint": "Valid fields: "
            + ", ".join(CreativeVariation.model_fields.keys()),
        }

    print(
        f"[DEBUG generate_video_from_product] Variation: {variation_obj.name}"
    )

    # Resolve the prompt archetype once — drives the reference-image preamble
    # (Stage 1) and product-centric naming below. Raises ValueError for
    # malformed presentation_mode inputs (e.g. with_model on a non-wearable
    # category) — surfaced as a structured error rather than a silent default.
    try:
        archetype = resolve_archetype(product, variation_obj)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if archetype != "wearable" and variation_obj.name.startswith(
        f"{variation_obj.model_ethnicity}-"
    ):
        variation_obj.name = (
            f"{product.category}-{variation_obj.setting}-{variation_obj.mood}"
        )

    # Check if product is linked to campaign
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id FROM campaign_products
            WHERE campaign_id = ? AND product_id = ?
        """,
            (campaign_id, product_id),
        )
        if not cursor.fetchone():
            # Auto-link product to campaign
            cursor.execute(
                """
                INSERT INTO campaign_products (campaign_id, product_id)
                VALUES (?, ?)
            """,
                (campaign_id, product_id),
            )
            print(
                "[DEBUG generate_video_from_product] Linked product to campaign"
            )

    # Get product image bytes using storage abstraction
    product_image_filename = product.image_filename
    product_image_bytes = None
    if product_image_filename:
        try:
            if storage.product_image_exists(product_image_filename):
                product_image_bytes = storage.read_product_image(
                    product_image_filename
                )
                image_path = storage.get_product_image_path(
                    product_image_filename
                )
                print(
                    f"[DEBUG generate_video_from_product] Loaded product image from: {image_path}"
                )
            else:
                print(
                    f"[DEBUG generate_video_from_product] Product image not found: {product_image_filename}"
                )
        except Exception as e:
            print(
                f"[DEBUG generate_video_from_product] Could not load product image: {e}"
            )

    reference_image_used = product_image_bytes is not None

    # Generate video filename
    video_filename = generate_video_filename(product.name, variation_obj.name)
    thumbnail_filename = video_filename.replace(".mp4", "-thumbnail.png")

    try:
        start_time = time.time()

        if use_two_stage:
            # Stage 1: Generate scene image
            print(
                "[DEBUG generate_video_from_product] Stage 1: Generating scene image..."
            )
            scene_image_bytes, scene_prompt = await generate_scene_image(
                product=product,
                variation=variation_obj,
                product_image_bytes=product_image_bytes,
                archetype=archetype,
            )

            # Save scene image as thumbnail (storage seam handles local vs GCS)
            thumbnail_path = storage.save_video(
                thumbnail_filename, scene_image_bytes
            )
            print(
                f"[DEBUG generate_video_from_product] Saved thumbnail: {thumbnail_path}"
            )

            # Stage 2: Animate scene with Omni
            print(
                "[DEBUG generate_video_from_product] Stage 2: Animating with Gemini Omni Flash..."
            )
            video_bytes, video_prompt = await animate_scene_with_omni(
                scene_image_bytes=scene_image_bytes,
                product=product,
                variation=variation_obj,
                duration_seconds=duration_seconds,
            )
        else:
            # Single-stage: Direct video generation (fallback)
            print(
                "[DEBUG generate_video_from_product] Single-stage video generation..."
            )
            scene_prompt = ""
            video_prompt = build_creative_prompt(product, variation_obj)

            # Load product image for direct Omni generation
            if not product_image_bytes:
                return {
                    "status": "error",
                    "message": "Product image required for single-stage generation",
                }

            client = genai.Client()
            image_b64 = base64.b64encode(product_image_bytes).decode("ascii")
            user_step = im.UserInputStep(
                content=[
                    im.ImageContent(data=image_b64, mime_type="image/png"),
                    im.TextContent(text=video_prompt),
                ]
            )
            generation_config = im.GenerationConfig(
                video_config=im.VideoConfig(task="image_to_video")
            )
            response_format = im.VideoResponseFormat(
                type="video",
                delivery="inline",
                aspect_ratio="9:16",
            )

            print(
                f"[DEBUG generate_video_from_product] Calling Omni Flash ({VIDEO_GEN_MODEL})..."
            )
            interaction = client.interactions.create(
                model=VIDEO_GEN_MODEL,
                input=[user_step],
                generation_config=generation_config,
                response_format=response_format,
                background=True,
            )

            interaction = await _wait_for_omni_interaction(
                client,
                interaction,
                debug_label="generate_video_from_product",
            )
            video_bytes = _extract_omni_video_bytes(interaction)

            thumbnail_path = None
            thumbnail_filename = None

        generation_time = int(time.time() - start_time)
        print(
            f"[DEBUG generate_video_from_product] Total generation time: {generation_time}s"
        )

        # Save video (storage seam handles local vs GCS)
        video_path = storage.save_video(video_filename, video_bytes)
        print(f"[DEBUG generate_video_from_product] Saved video: {video_path}")

        # Save metadata file
        save_video_metadata(
            video_filename=video_filename,
            product=product,
            variation=variation_obj,
            scene_prompt=scene_prompt,
            video_prompt=video_prompt,
            pipeline_type="two-stage" if use_two_stage else "single-stage",
        )

        # Save as ADK artifact if tool_context provided
        if tool_context:
            video_artifact = types.Part.from_bytes(
                data=video_bytes, mime_type="video/mp4"
            )
            version = await tool_context.save_artifact(
                filename=video_filename, artifact=video_artifact
            )
            print(
                f"[DEBUG generate_video_from_product] Saved artifact version: {version}"
            )

        # Insert into campaign_videos table with status='generated'
        # NOTE: NO metrics are created - metrics only on activation
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO campaign_videos (
                    campaign_id, product_id, video_filename, local_path, thumbnail_path,
                    scene_prompt, video_prompt, pipeline_type, variation_name, variation_params,
                    duration_seconds, aspect_ratio, status, generation_time_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?)
            """,
                (
                    campaign_id,
                    product_id,
                    video_filename,
                    video_path,
                    thumbnail_path,
                    scene_prompt,
                    video_prompt,
                    "two-stage" if use_two_stage else "single-stage",
                    variation_obj.name,
                    json.dumps(
                        _variation_params_for_storage(variation_obj, archetype)
                    ),
                    duration_seconds,
                    "9:16",
                    generation_time,
                ),
            )
            video_id = cursor.lastrowid

        result = {
            "status": "success",
            "message": "Video generated successfully. Use activate_video to push live.",
            "video": {
                "id": video_id,
                "campaign_id": campaign_id,
                "campaign_name": campaign["name"],
                "product_id": product_id,
                "product_name": product.name,
                "video_filename": video_filename,
                "video_path": video_path,
                "thumbnail_path": thumbnail_path,
                "variation": variation_obj.name,
                "pipeline": "two-stage" if use_two_stage else "single-stage",
                "duration_seconds": duration_seconds,
                "generation_time_seconds": generation_time,
                "status": "generated",
                "artifact_saved": tool_context is not None,
            },
            "prompts": {
                "scene_prompt": scene_prompt[:200] + "..."
                if len(scene_prompt) > 200
                else scene_prompt,
                "video_prompt": video_prompt[:200] + "..."
                if len(video_prompt) > 200
                else video_prompt,
            },
            "note": "Video is in 'generated' status. Metrics will only be created after activation.",
            "reference_image_used": reference_image_used,
        }
        if not reference_image_used:
            result["warning"] = (
                f"No product image found for {product.name} — scene generated "
                "from text description only"
            )
        return result

    except Exception as e:
        import traceback

        print(f"[DEBUG generate_video_from_product] Error: {e!s}")
        print(
            f"[DEBUG generate_video_from_product] Traceback: {traceback.format_exc()}"
        )
        return {
            "status": "error",
            "message": f"Video generation failed: {e!s}",
            "product": product.name,
            "variation": variation_obj.name if variation_obj else "default",
        }


async def generate_video_ad(
    campaign_id: int,
    image_id: int | None = None,
    custom_prompt: str | None = None,
    duration_seconds: int = 4,
    tool_context: ToolContext = None,
) -> dict:
    """Generate a video ad using Gemini Omni Flash.

    Uses a seed image as reference and generates a cinematic fashion video.
    Polls for completion and saves to the generated/ folder.
    Optionally saves video as ADK artifact if tool_context is provided.

    Args:
        campaign_id: The campaign to generate the ad for
        image_id: Optional specific image ID to use. If not provided, uses the first image.
        custom_prompt: Optional custom prompt. If not provided, generates from image metadata.
        duration_seconds: Video duration (default 4 seconds)
        tool_context: Optional ADK ToolContext for artifact storage

    Returns:
        Dictionary with video path and generation details
    """
    print(f"[DEBUG generate_video_ad] Starting for campaign_id={campaign_id}")
    print(
        f"[DEBUG generate_video_ad] image_id={image_id}, duration_seconds={duration_seconds}"
    )
    print(
        f"[DEBUG generate_video_ad] custom_prompt={custom_prompt[:100] if custom_prompt else 'None'}..."
    )

    valid_durations = [4, 6, 8]
    if duration_seconds not in valid_durations:
        duration_seconds = 4
        print(
            f"[DEBUG generate_video_ad] Adjusted duration to: {duration_seconds}"
        )

    # Ensure generated directory exists (only in local mode)
    if storage.get_storage_mode() == "local":
        os.makedirs(GENERATED_DIR, exist_ok=True)
        print(f"[DEBUG generate_video_ad] GENERATED_DIR: {GENERATED_DIR}")
    else:
        print(
            "[DEBUG generate_video_ad] Using GCS storage, skipping local directory creation"
        )

    with get_db_cursor() as cursor:
        # Get campaign info
        print(
            f"[DEBUG generate_video_ad] Fetching campaign {campaign_id} from database..."
        )
        cursor.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        campaign = cursor.fetchone()
        if not campaign:
            print(f"[DEBUG generate_video_ad] Campaign {campaign_id} not found")
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }
        print(f"[DEBUG generate_video_ad] Found campaign: {campaign['name']}")

        # Get image
        if image_id:
            print(
                f"[DEBUG generate_video_ad] Fetching specific image {image_id}..."
            )
            cursor.execute(
                """
                SELECT * FROM campaign_images
                WHERE id = ? AND campaign_id = ?
            """,
                (image_id, campaign_id),
            )
        else:
            print(
                "[DEBUG generate_video_ad] Fetching first image for campaign..."
            )
            cursor.execute(
                """
                SELECT * FROM campaign_images
                WHERE campaign_id = ?
                ORDER BY created_at
                LIMIT 1
            """,
                (campaign_id,),
            )

        image_row = cursor.fetchone()
        if not image_row:
            print(
                f"[DEBUG generate_video_ad] No images found for campaign {campaign_id}"
            )
            return {
                "status": "error",
                "message": f"No images found for campaign {campaign_id}. Add a seed image first.",
            }
        print(
            f"[DEBUG generate_video_ad] Found image: {image_row['image_path']}"
        )

        image_filename = image_row["image_path"]
        image_path = storage.get_image_path(image_filename)
        print(f"[DEBUG generate_video_ad] Image path: {image_path}")
        print(
            f"[DEBUG generate_video_ad] Storage mode: {storage.get_storage_mode()}"
        )
        if not storage.image_exists(image_filename):
            print(
                f"[DEBUG generate_video_ad] Image file not found: {image_filename}"
            )
            return {
                "status": "error",
                "message": f"Image file not found: {image_filename}",
            }
        print("[DEBUG generate_video_ad] Image file exists")

        # Get or generate prompt
        if custom_prompt:
            prompt = custom_prompt
            print("[DEBUG generate_video_ad] Using custom prompt")
        else:
            metadata = (
                json.loads(image_row["metadata"])
                if image_row["metadata"]
                else {}
            )
            campaign_info = {
                "name": campaign["name"],
                "category": campaign["category"],
                "city": campaign["city"],
                "state": campaign["state"],
            }
            prompt = generate_video_prompt(metadata, campaign_info)
            print("[DEBUG generate_video_ad] Generated prompt from metadata")
        print(f"[DEBUG generate_video_ad] Prompt: {prompt[:100]}...")

        # Create pending ad record
        print("[DEBUG generate_video_ad] Creating pending ad record...")
        cursor.execute(
            """
            INSERT INTO campaign_ads (campaign_id, image_id, video_path, prompt_used, duration_seconds, status)
            VALUES (?, ?, '', ?, ?, 'generating')
        """,
            (campaign_id, image_row["id"], prompt, duration_seconds),
        )
        ad_id = cursor.lastrowid
        print(f"[DEBUG generate_video_ad] Created ad record with id={ad_id}")

    # Generate video using Gemini Omni Flash
    try:
        print("[DEBUG generate_video_ad] Initializing genai client...")
        client = genai.Client()

        # Load image using storage abstraction and convert to bytes
        print("[DEBUG generate_video_ad] Loading image...")
        image_bytes = storage.read_image(image_filename)
        print(f"[DEBUG generate_video_ad] Image bytes size: {len(image_bytes)}")

        # Use PIL to determine format and re-encode if needed
        with PILImage.open(io.BytesIO(image_bytes)) as pil_img:
            print(
                f"[DEBUG generate_video_ad] Image format: {pil_img.format}, size: {pil_img.size}"
            )
            img_format = pil_img.format or "JPEG"

        mime_type = f"image/{img_format.lower()}"
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        user_step = im.UserInputStep(
            content=[
                im.ImageContent(data=image_b64, mime_type=mime_type),
                im.TextContent(text=prompt),
            ]
        )
        generation_config = im.GenerationConfig(
            video_config=im.VideoConfig(task="image_to_video")
        )
        response_format = im.VideoResponseFormat(
            type="video",
            delivery="inline",
            aspect_ratio="9:16",
        )

        # Start video generation
        print(
            f"[DEBUG generate_video_ad] Starting video generation with {VIDEO_GEN_MODEL}..."
        )
        interaction = client.interactions.create(
            model=VIDEO_GEN_MODEL,
            input=[user_step],
            generation_config=generation_config,
            response_format=response_format,
            background=True,
        )
        print(
            f"[DEBUG generate_video_ad] Video generation started, interaction: {interaction}"
        )

        # Poll for completion; this call site maps helper exceptions to the
        # legacy behavior (mark ad failed in DB + return an error dict).
        try:
            interaction = await _wait_for_omni_interaction(
                client, interaction, debug_label="generate_video_ad"
            )
            video_data = _extract_omni_video_bytes(interaction)
        except TimeoutError:
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE campaign_ads SET status = 'failed' WHERE id = ?
                """,
                    (ad_id,),
                )
            return {
                "status": "error",
                "message": "Video generation timed out after 10 minutes",
                "ad_id": ad_id,
            }
        except ValueError:
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE campaign_ads SET status = 'failed' WHERE id = ?
                """,
                    (ad_id,),
                )
            return {
                "status": "error",
                "message": "Video generation completed but returned no result. Check API quota and permissions.",
                "ad_id": ad_id,
                "prompt_used": prompt,
            }

        timestamp = int(time.time())
        output_filename = f"campaign_{campaign_id}_ad_{ad_id}_{timestamp}.mp4"
        print(f"[DEBUG generate_video_ad] Video bytes size: {len(video_data)}")

        # Save video - handle both local and GCS storage modes
        if storage.get_storage_mode() == "gcs":
            output_path = storage.save_video(output_filename, video_data)
            print(
                f"[DEBUG generate_video_ad] Video uploaded to GCS: {output_path}"
            )
        else:
            output_path = os.path.join(GENERATED_DIR, output_filename)
            print(f"[DEBUG generate_video_ad] Saving video to: {output_path}")
            with open(output_path, "wb") as f:
                f.write(video_data)
            print("[DEBUG generate_video_ad] Video saved successfully")

        # Save as ADK artifact if tool_context is provided
        if tool_context:
            print("[DEBUG generate_video_ad] Saving as ADK artifact...")
            # Use video_data we already have in memory (no need to re-read from storage)
            video_artifact = types.Part.from_bytes(
                data=video_data, mime_type="video/mp4"
            )
            # save_artifact is async, await it properly
            version = await tool_context.save_artifact(
                filename=output_filename, artifact=video_artifact
            )
            print(
                f"[DEBUG generate_video_ad] Artifact saved, version: {version}"
            )
        else:
            print(
                "[DEBUG generate_video_ad] No tool_context, skipping artifact save"
            )

        # Analyze the generated video to extract properties
        print(
            "[DEBUG generate_video_ad] Analyzing generated video for properties..."
        )
        video_properties = await analyze_video(output_path)
        print(
            f"[DEBUG generate_video_ad] Extracted properties: mood={video_properties.mood}, "
            f"energy={video_properties.energy_level}, style={video_properties.visual_style}"
        )

        # Update database with video path and properties
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                UPDATE campaign_ads
                SET video_path = ?, status = 'completed', video_properties = ?
                WHERE id = ?
            """,
                (output_filename, video_properties.model_dump_json(), ad_id),
            )

        # NOTE: Auto-metrics generation REMOVED per HITL workflow
        # Metrics are now only created when a video is activated via review_tools.activate_video()
        print(
            "[DEBUG generate_video_ad] Video saved. No auto-metrics (HITL workflow)."
        )

        return {
            "status": "success",
            "message": "Video ad generated successfully. Use activate_video to push live and generate metrics.",
            "ad": {
                "id": ad_id,
                "campaign_id": campaign_id,
                "campaign_name": campaign["name"],
                "video_path": output_filename,
                "full_path": output_path,
                "prompt_used": prompt,
                "duration_seconds": duration_seconds,
                "source_image": image_row["image_path"],
                "artifact_saved": tool_context is not None,
                "status": "completed",
            },
            "video_properties": video_properties.model_dump(),
            "note": "Metrics will only be generated after video activation (HITL workflow).",
        }

    except Exception as e:
        import traceback

        print(f"[DEBUG generate_video_ad] Exception: {e!s}")
        print(f"[DEBUG generate_video_ad] Traceback: {traceback.format_exc()}")
        # Update ad status to failed
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                UPDATE campaign_ads SET status = 'failed' WHERE id = ?
            """,
                (ad_id,),
            )

        return {
            "status": "error",
            "message": f"Video generation failed: {e!s}",
            "ad_id": ad_id,
            "prompt_used": prompt,
        }


def list_campaign_ads(campaign_id: int) -> dict:
    """List all generated video ads for a campaign.

    Args:
        campaign_id: The ID of the campaign

    Returns:
        Dictionary with list of ads and their details
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id, name FROM campaigns WHERE id = ?", (campaign_id,)
        )
        campaign = cursor.fetchone()
        if not campaign:
            return {
                "status": "error",
                "message": f"Campaign with ID {campaign_id} not found",
            }

        cursor.execute(
            """
            SELECT ca.*, ci.image_path as source_image
            FROM campaign_ads ca
            LEFT JOIN campaign_images ci ON ca.image_id = ci.id
            WHERE ca.campaign_id = ?
            ORDER BY ca.created_at DESC
        """,
            (campaign_id,),
        )

        ads = []
        for row in cursor.fetchall():
            video_filename = row["video_path"]
            # Use storage abstraction for path resolution and existence check
            video_path = (
                storage.get_video_path(video_filename)
                if video_filename
                else None
            )
            video_exists = (
                storage.video_exists(video_filename)
                if video_filename
                else False
            )
            # Parse video properties if available
            video_props = None
            if row["video_properties"]:
                try:
                    video_props = json.loads(row["video_properties"])
                except json.JSONDecodeError:
                    pass
            ads.append(
                {
                    "id": row["id"],
                    "video_path": video_filename,
                    "full_path": video_path,
                    "exists": video_exists,
                    "prompt_used": row["prompt_used"],
                    "duration_seconds": row["duration_seconds"],
                    "status": row["status"],
                    "source_image": row["source_image"],
                    "video_properties": video_props,
                    "created_at": row["created_at"],
                }
            )

        return {
            "status": "success",
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "ad_count": len(ads),
            "ads": ads,
        }


# =============================================================================
# Product and Video Listing Functions
# =============================================================================


def list_products(
    category: str | None = None, include_urls: bool = True
) -> dict:
    """List all available products for video generation.

    Products are pre-loaded from the products table (22-item fashion catalog plus the multi-vertical retail core test set).
    Includes public GCS URLs for product images when available.

    Args:
        category: Optional category filter (e.g. dress, top, pants, beverage, electronics)
        include_urls: Whether to include public image URLs (default: True)

    Returns:
        Dictionary with list of products including image URLs
    """
    from ..database.db import list_products as db_list_products

    products = db_list_products(category)

    product_list = []
    for p in products:
        product_data = {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "style": p.attributes.get("style"),
            "color": p.attributes.get("color"),
            "fabric": p.attributes.get("fabric"),
            "image_filename": p.image_filename,
        }

        # Image URL policy (ws09): existence-checked public URL in GCS mode,
        # no URL in local mode — image_status says what a UI can rely on.
        if include_urls and p.image_filename:
            image_available = False
            try:
                image_available = storage.product_image_exists(p.image_filename)
            except Exception:
                image_available = False
            product_data["image_status"] = (
                "available" if image_available else "missing"
            )
            if image_available:
                image_url = storage.get_product_image_public_url(
                    p.image_filename, check_exists=False
                )
                if image_url:
                    product_data["image_url"] = image_url

        product_list.append(product_data)

    return {
        "status": "success",
        "product_count": len(product_list),
        "products": product_list,
        "note": "image_status shows whether each product's reference image is stored; image_url is present only in GCS mode for images that exist",
    }


def list_campaign_videos(
    campaign_id: int | None = None, status: str | None = None
) -> dict:
    """List videos from the campaign_videos table.

    This is the new function for the HITL workflow that lists videos
    with their status (generated, activated, paused, archived).

    Args:
        campaign_id: Optional campaign ID filter
        status: Optional status filter (generated, activated, paused, archived)

    Returns:
        Dictionary with list of videos and their details
    """
    with get_db_cursor() as cursor:
        # Build query based on filters
        query = """
            SELECT cv.*, c.name as campaign_name, p.name as product_name
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            LEFT JOIN products p ON cv.product_id = p.id
        """
        params = []
        conditions = []

        if campaign_id:
            conditions.append("cv.campaign_id = ?")
            params.append(campaign_id)
        if status:
            conditions.append("cv.status = ?")
            params.append(status)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY cv.created_at DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        videos = []
        for row in rows:
            variation_params = None
            if row["variation_params"]:
                try:
                    variation_params = json.loads(row["variation_params"])
                except json.JSONDecodeError:
                    pass

            videos.append(
                {
                    "id": row["id"],
                    "campaign_id": row["campaign_id"],
                    "campaign_name": row["campaign_name"],
                    "product_id": row["product_id"],
                    "product_name": row["product_name"],
                    "video_filename": row["video_filename"],
                    "thumbnail_path": row["thumbnail_path"],
                    "variation_name": row["variation_name"],
                    "variation_params": variation_params,
                    "pipeline_type": row["pipeline_type"],
                    "duration_seconds": row["duration_seconds"],
                    "status": row["status"],
                    "activated_at": row["activated_at"],
                    "created_at": row["created_at"],
                    "generation_time_seconds": row["generation_time_seconds"],
                }
            )

        return {
            "status": "success",
            "video_count": len(videos),
            "videos": videos,
        }


async def generate_video_with_variation(
    campaign_id: int,
    product_id: int,
    variation_name: str | None = None,
    model_ethnicity: str = "diverse",
    setting: str = "studio",
    mood: str = "elegant",
    lighting: str = "natural",
    activity: str = "walking",
    camera_movement: str = "orbit",
    time_of_day: str = "day",
    visual_style: str = "cinematic",
    energy: str = "moderate",
    duration_seconds: int = 4,
    use_two_stage: bool = True,
    presentation_mode: str | None = None,
    tool_context: ToolContext = None,
) -> dict:
    """Generate a video with variation parameters as individual arguments.

    This is a convenience wrapper around generate_video_from_product that
    accepts variation parameters as individual arguments for easier agent usage.

    Args:
        campaign_id: The campaign to generate for
        product_id: The product ID from products table
        variation_name: Name for this variation (auto-generated if not provided)
        model_ethnicity: Model ethnicity (asian, european, african, latina, south-asian, diverse)
        setting: Setting (studio, beach, urban, cafe, rooftop, garden, nature, etc.)
        mood: Mood (elegant, romantic, bold, playful, sophisticated, etc.)
        lighting: Lighting (natural, studio, dramatic, soft, golden, neon, moody)
        activity: Activity (walking, standing, sitting, dancing, spinning, posing)
        camera_movement: Camera movement (orbit, pan, dolly, static, tracking, crane)
        time_of_day: Time of day (golden-hour, sunrise, day, sunset, dusk, night)
        visual_style: Visual style (cinematic, editorial, commercial, artistic)
        energy: Energy level (calm, moderate, dynamic, high-energy)
        duration_seconds: Video duration (4, 6, or 8 seconds)
        use_two_stage: Use two-stage pipeline (default True)
        presentation_mode: How the product is presented — "auto" (default,
            decided by product category), "with_model" (wearables only), or
            "product_only" (no human model, forces a product-centric shot)
        tool_context: Optional ADK ToolContext for artifact storage

    Returns:
        Dictionary with video details
    """
    # Generate variation name if not provided
    if not variation_name:
        variation_name = f"{model_ethnicity}-{setting}-{mood}"

    # Create CreativeVariation from parameters
    variation = CreativeVariation(
        name=variation_name,
        model_ethnicity=model_ethnicity,
        setting=setting,
        mood=mood,
        lighting=lighting,
        activity=activity,
        camera_movement=camera_movement,
        time_of_day=time_of_day,
        visual_style=visual_style,
        energy=energy,
        presentation_mode=presentation_mode,
    )

    # Call the main function
    return await generate_video_from_product(
        campaign_id=campaign_id,
        product_id=product_id,
        variation=variation,
        use_two_stage=use_two_stage,
        duration_seconds=duration_seconds,
        tool_context=tool_context,
    )


def get_variation_presets() -> dict:
    """Get available preset variations for video generation.

    Returns predefined variation sets for:
    - diversity: Different model ethnicities
    - settings: Different locations/environments
    - moods: Different emotional tones

    Returns:
        Dictionary with preset variations
    """
    presets = {}
    for preset_name, variations in PRESET_VARIATIONS.items():
        presets[preset_name] = [
            {
                "name": v.name,
                "model_ethnicity": v.model_ethnicity,
                "setting": v.setting,
                "mood": v.mood,
                "lighting": v.lighting,
                "activity": v.activity,
            }
            for v in variations
        ]

    return {
        "status": "success",
        "preset_count": len(presets),
        "presets": presets,
    }
