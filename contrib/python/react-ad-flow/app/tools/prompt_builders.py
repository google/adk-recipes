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

"""Prompt builders for two-stage video generation pipeline.

Stage 1: Scene Image Prompt - Creates first frame with model wearing product
Stage 2: Video Animation Prompt - Animates the scene image
"""

from ..models.product import Product
from ..models.variation import CreativeVariation
from .prompt_archetypes import (
    CONSUMABLE_HERO,
    PRODUCT_HERO,
    STAGED_PRODUCT,
    WEARABLE,
    resolve_archetype,
)

# Ad style policy (owner directive, ws09): all generated ads are music-only with a
# clean frame — no rendered text and no spoken audio, for every archetype.
_NO_TEXT_BLOCK = """NO TEXT OR GRAPHICS:
- Do NOT render any text, words, numbers, labels, badges, captions, watermarks, or graphic overlays anywhere in the frame
- The only text allowed is text that is physically part of the product's own packaging or label"""

_AUDIO_BLOCK = """AUDIO & ON-SCREEN TEXT:
- Instrumental background music only, matched to the mood and theme of the scene
- No voiceover, no narration, no spoken words, no lyrics, no dialogue
- No on-screen text, captions, subtitles, titles, badges, or graphic overlays"""


def build_scene_image_prompt(
    product: Product, variation: CreativeVariation
) -> str:
    """Build a prompt for generating a scene-ready first frame image."""
    archetype = resolve_archetype(product, variation)
    if archetype == WEARABLE:
        return _build_wearable_scene_prompt(product, variation)
    return _build_product_scene_prompt(product, variation, archetype)


def build_video_animation_prompt(
    product: Product, variation: CreativeVariation
) -> str:
    """Build a prompt focused on animating an existing scene image."""
    archetype = resolve_archetype(product, variation)
    if archetype == WEARABLE:
        return _build_wearable_animation_prompt(product, variation)
    return _build_product_animation_prompt(product, variation, archetype)


def build_creative_prompt(
    product: Product, variation: CreativeVariation
) -> str:
    """Build a single-stage video prompt (fallback when not using two-stage)."""
    archetype = resolve_archetype(product, variation)
    if archetype == WEARABLE:
        return _build_wearable_creative_prompt(product, variation)
    return _build_product_creative_prompt(product, variation, archetype)


def _build_wearable_scene_prompt(
    product: Product, variation: CreativeVariation
) -> str:
    """Build a prompt for generating a scene-ready first frame image.

    This creates an image of a model wearing the product in the desired setting,
    which will be used as the first frame for video generation.

    Args:
        product: Typed Product
        variation: Creative variation parameters

    Returns:
        Optimized image generation prompt
    """
    # Extract product details (style -> category -> literal fallback preserved
    # from the pre-Product dict era; golden test pins the output)
    garment_type = (
        product.attributes.get("style")
        or product.category
        or "elegant fashion piece"
    )
    color = product.attributes.get("color") or ""
    fabric = product.attributes.get("fabric") or ""

    garment_desc = f"{color} {fabric} {garment_type}".strip()

    if not garment_desc:
        garment_desc = "elegant fashion piece"

    # Build model description
    ethnicity_map = {
        "asian": "a graceful Asian woman with sleek dark hair",
        "european": "a striking European woman with refined features",
        "african": "a stunning African woman with radiant skin",
        "latina": "a vibrant Latina woman with warm features",
        "middle-eastern": "an elegant Middle Eastern woman with captivating eyes",
        "south-asian": "a beautiful South Asian woman with flowing dark hair",
        "diverse": "a confident, radiant woman with a warm, engaging presence",
    }
    model_desc = variation.model_description or ethnicity_map.get(
        variation.model_ethnicity or "diverse",
        "a confident, radiant woman with a warm, engaging presence",
    )

    # Build setting description
    setting_map = {
        "beach": "on a pristine sandy beach with gentle waves in the background",
        "urban": "in a stylish urban cityscape with modern architecture",
        "cafe": "at an elegant European-style cafe with soft ambient lighting",
        "rooftop": "on a luxurious rooftop terrace overlooking a city skyline",
        "studio": "in a minimalist professional photography studio",
        "garden": "in a lush, blooming garden with vibrant flowers",
        "street": "on a charming cobblestone street in a European city",
        "luxury-interior": "in an opulent luxury interior with elegant furnishings",
        "nature": "in a serene natural landscape with beautiful scenery",
        "park": "in a beautiful sunlit park with trees and greenery",
    }
    setting_desc = setting_map.get(
        variation.setting, f"in a {variation.setting} setting"
    )

    # Time of day
    time_map = {
        "golden-hour": "during golden hour with warm, soft light",
        "sunrise": "at sunrise with soft pink and orange morning light",
        "day": "in bright natural daylight",
        "sunset": "at sunset with warm orange and purple hues",
        "dusk": "at dusk with purple twilight ambiance",
        "night": "at night with atmospheric city lights and ambient glow",
        "morning": "in soft morning light",
    }
    time_desc = time_map.get(variation.time_of_day, "")

    # Pose/activity for still image
    pose_map = {
        "walking": "mid-stride in an elegant walking pose",
        "standing": "standing confidently with poised posture",
        "sitting": "seated elegantly with graceful posture",
        "dancing": "in a dynamic dance pose with flowing movement",
        "spinning": "with fabric flowing as if mid-spin",
        "posing": "striking an elegant fashion pose",
        "running": "in dynamic motion",
    }
    pose_desc = pose_map.get(
        variation.activity or "walking", "in an elegant pose"
    )

    # Lighting
    lighting_map = {
        "natural": "Natural, soft lighting",
        "studio": "Professional studio lighting with soft shadows",
        "dramatic": "Dramatic contrast lighting with deep shadows",
        "soft": "Soft, diffused ethereal lighting",
        "golden": "Warm golden hour lighting",
        "neon": "Atmospheric neon lighting with colorful accents",
        "moody": "Moody, atmospheric low-key lighting",
    }
    lighting_desc = lighting_map.get(
        variation.lighting, f"{variation.lighting} lighting"
    )

    # Props
    props_desc = ""
    if variation.props:
        prop_phrases = {
            "dog": "with a friendly dog beside her",
            "cat": "with an elegant cat nearby",
            "coffee": "holding a stylish cup of coffee",
            "umbrella": "holding a chic umbrella",
            "flowers": "surrounded by beautiful fresh flowers",
            "sunglasses": "wearing stylish designer sunglasses",
            "bag": "carrying a luxury designer handbag",
        }
        prop_parts = [prop_phrases.get(p, f"with {p}") for p in variation.props]
        props_desc = ", " + ", ".join(prop_parts)

    # Visual style
    style_map = {
        "cinematic": "Cinematic fashion photography",
        "editorial": "High-fashion editorial photography",
        "commercial": "Polished commercial advertising photography",
        "artistic": "Artistic fashion photography",
        "documentary": "Natural documentary-style photography",
    }
    style_desc = style_map.get(
        variation.visual_style, "Professional fashion photography"
    )

    # Key features from product
    key_features = product.description or "fabric texture and construction"

    # Construct the scene image prompt
    prompt = f"""{style_desc} of {model_desc} wearing a stunning {garment_desc}.

She is {pose_desc} {setting_desc} {time_desc}{props_desc}.

{lighting_desc}. The garment is clearly visible with all its details: {key_features}.

CRITICAL - PRODUCT PRESERVATION:
- The garment must match the reference product EXACTLY - same design, color, fabric, and construction details
- Do NOT alter, modify, or reinterpret the garment design in any way
- Preserve all product details: stitching, patterns, textures, embellishments, neckline, sleeves, hemline

QUALITY REQUIREMENTS:
- Full body or 3/4 shot showing the complete garment
- Model facing camera or slight angle
- Sharp focus on the garment and model
- Professional fashion advertisement quality
- Vertical 9:16 aspect ratio composition
- The scene should look like the perfect first frame of a fashion video ad

HUMAN FIGURE QUALITY:
- Realistic human proportions and anatomy
- Natural, beautiful facial features - no distortion or artifacts
- Properly proportioned hands and fingers
- Natural skin texture and tone
- Elegant, natural body posture

{_NO_TEXT_BLOCK}

Style: Luxury fashion campaign, magazine-quality, aspirational."""

    return prompt


def _build_wearable_animation_prompt(
    product: Product, variation: CreativeVariation
) -> str:
    """Build a prompt focused on animating an existing scene image.

    Since Stage 1 creates the scene, this prompt focuses on movement and animation.

    Args:
        product: Typed Product
        variation: Creative variation parameters

    Returns:
        Animation-focused video prompt
    """
    # Activity animation descriptions
    activity_animation = {
        "walking": "The model walks gracefully forward, the garment flowing with each step",
        "standing": "The model shifts weight subtly, the fabric catching the light",
        "sitting": "The model moves gracefully, adjusting position elegantly",
        "dancing": "The model moves rhythmically, the garment swirling beautifully",
        "spinning": "The model spins slowly, the fabric flowing outward dramatically",
        "posing": "The model transitions between elegant poses fluidly",
        "running": "The model moves dynamically, fabric flowing with motion",
    }
    activity_desc = activity_animation.get(
        variation.activity or "walking", "The model moves gracefully"
    )

    # Camera movement
    camera_map = {
        "orbit": "Camera slowly orbits around the model",
        "pan": "Camera pans smoothly across the scene",
        "dolly": "Camera dollies in slowly toward the model",
        "static": "Camera holds steady with subtle breathing movement",
        "tracking": "Camera tracks alongside the model's movement",
        "crane": "Camera sweeps with elegant crane movement",
        "handheld": "Camera has subtle natural handheld movement",
    }
    camera_desc = camera_map.get(
        variation.camera_movement, "Camera moves smoothly"
    )

    # Environmental motion
    env_motion = []
    if variation.setting == "beach":
        env_motion.append("waves gently rolling in the background")
    elif variation.setting == "urban":
        env_motion.append("city life flowing in the background")
    elif variation.setting == "garden":
        env_motion.append("flowers swaying gently in the breeze")
    elif variation.setting == "nature":
        env_motion.append("leaves rustling softly")

    if variation.weather == "rainy":
        env_motion.append("rain falling gently")
    elif variation.weather == "snowy":
        env_motion.append("snow falling softly")

    env_desc = (
        ", ".join(env_motion) if env_motion else "subtle ambient movement"
    )

    # Mood/energy
    energy_map = {
        "calm": "slow, graceful, meditative pace",
        "moderate": "smooth, elegant movement",
        "dynamic": "energetic, fluid motion",
        "high-energy": "vibrant, dynamic, fast-paced movement",
    }
    energy_desc = energy_map.get(variation.energy, "elegant movement")

    garment_desc = (
        product.attributes.get("style") or product.category or "garment"
    )

    prompt = f"""{activity_desc}. {camera_desc}, showcasing the {garment_desc}'s movement, drape, and fabric flow.

The scene has {env_desc}. The movement is {energy_desc}.

CRITICAL - PRODUCT & QUALITY PRESERVATION:
- Maintain the {garment_desc}'s exact appearance from the first frame throughout the video
- The fabric's natural flow and drape as the model moves
- Lighting playing across the garment's surface
- Smooth, professional camera work
- High-end fashion advertisement aesthetic

HUMAN FIGURE QUALITY:
- Maintain realistic human proportions throughout the animation
- Natural, beautiful facial expressions - no distortion or morphing artifacts
- Properly proportioned hands and fingers during movement
- Fluid, natural body movement without anatomical errors
- Consistent model appearance from start to end

{_AUDIO_BLOCK}

8 seconds. Vertical 9:16. Cinematic quality. Professional fashion video ad."""

    return prompt


def _build_wearable_creative_prompt(
    product: Product, variation: CreativeVariation
) -> str:
    """Build a single-stage video prompt (fallback when not using two-stage).

    This combines scene and animation into one prompt for direct video generation.

    Args:
        product: Typed Product
        variation: Creative variation parameters

    Returns:
        Full video generation prompt
    """
    # Extract product details
    garment_type = (
        product.attributes.get("style")
        or product.category
        or "elegant fashion piece"
    )
    color = product.attributes.get("color") or ""
    fabric = product.attributes.get("fabric") or ""
    garment_desc = (
        f"{color} {fabric} {garment_type}".strip() or "elegant fashion piece"
    )

    # Build model description
    ethnicity_map = {
        "asian": "a graceful Asian woman with sleek dark hair",
        "european": "a striking European woman with refined features",
        "african": "a stunning African woman with radiant skin",
        "latina": "a vibrant Latina woman with warm features",
        "middle-eastern": "an elegant Middle Eastern woman with captivating eyes",
        "south-asian": "a beautiful South Asian woman with flowing dark hair",
        "diverse": "a confident, radiant woman with a warm, engaging presence",
    }
    model_desc = variation.model_description or ethnicity_map.get(
        variation.model_ethnicity or "diverse",
        "a confident, radiant woman with a warm, engaging presence",
    )

    # Build setting
    setting_map = {
        "beach": "on a pristine sandy beach",
        "urban": "in a stylish urban cityscape",
        "cafe": "at an elegant European-style cafe",
        "rooftop": "on a luxurious rooftop terrace",
        "studio": "in a minimalist professional studio",
        "garden": "in a lush, blooming garden",
        "street": "on a charming cobblestone street",
        "luxury-interior": "in an opulent luxury interior",
        "nature": "in a serene natural landscape",
        "park": "in a beautiful sunlit park",
    }
    setting_desc = setting_map.get(
        variation.setting, f"in a {variation.setting} setting"
    )

    # Time of day
    time_map = {
        "golden-hour": "during magical golden hour",
        "sunrise": "at sunrise with soft morning light",
        "day": "in natural daylight",
        "sunset": "at sunset with warm orange hues",
        "dusk": "at dusk with purple twilight",
        "night": "at night with atmospheric city lights",
    }
    time_desc = time_map.get(variation.time_of_day, "")

    # Activity
    activity_map = {
        "walking": "walking gracefully",
        "standing": "standing confidently",
        "sitting": "seated elegantly",
        "dancing": "moving rhythmically",
        "spinning": "spinning gracefully, letting the fabric flow",
        "posing": "striking elegant poses",
        "running": "moving dynamically",
    }
    activity_desc = activity_map.get(
        variation.activity or "walking", variation.activity
    )

    # Camera
    camera_map = {
        "orbit": "Camera slowly orbits around the subject",
        "pan": "Camera pans smoothly across the scene",
        "dolly": "Camera dollies in dramatically",
        "static": "Camera holds steady with subtle movement",
        "tracking": "Camera tracks alongside the subject",
        "crane": "Camera sweeps with crane-like movement",
        "handheld": "Camera has subtle handheld movement",
    }
    camera_desc = camera_map.get(
        variation.camera_movement, f"Camera {variation.camera_movement}"
    )

    # Lighting
    lighting_map = {
        "natural": "Natural lighting",
        "studio": "Professional studio lighting",
        "dramatic": "Dramatic contrast lighting",
        "soft": "Soft, diffused lighting",
        "golden": "Warm golden hour lighting",
        "neon": "Atmospheric neon lighting",
        "moody": "Moody, atmospheric lighting",
    }
    lighting_desc = lighting_map.get(
        variation.lighting, f"{variation.lighting} lighting"
    )

    # Style
    style_map = {
        "cinematic": "Cinematic",
        "editorial": "High-fashion editorial",
        "commercial": "Polished commercial",
        "artistic": "Artistic",
        "documentary": "Documentary-style",
    }
    style_desc = style_map.get(variation.visual_style, variation.visual_style)

    # Construct prompt
    prompt = f"""{style_desc} fashion video advertisement featuring {model_desc} wearing a stunning {garment_desc}.

She is {activity_desc} {setting_desc} {time_desc}.

{camera_desc}, showcasing the garment's movement and drape.

{lighting_desc}.

CRITICAL - PRODUCT PRESERVATION:
- The garment must match the reference product EXACTLY throughout the video
- Preserve all product details: design, color, fabric, patterns, construction

HUMAN FIGURE QUALITY:
- Realistic human proportions and anatomy
- Natural, beautiful facial features - no distortion or artifacts
- Properly proportioned hands and fingers
- Fluid, natural body movement

{_AUDIO_BLOCK}

Professional high-end fashion advertisement. 8 seconds. Vertical 9:16 format."""

    return prompt


# --- Product-centric prompt builders (non-wearable archetypes) ---

_ARCHETYPE_SCENE_FLAVOR = {
    CONSUMABLE_HERO: (
        "Appetizing hero shot. Emphasize freshness and appetite appeal: "
        "condensation on cold surfaces, gentle steam on hot items, vivid "
        "natural textures of the ingredients."
    ),
    STAGED_PRODUCT: (
        "Styled environment staging. Place the product in a realistic, "
        "aspirational setting that shows how it lives in a customer's space, "
        "with supporting props kept subtle and out of focus."
    ),
    PRODUCT_HERO: (
        "Clean product hero shot. The product is the sole subject, centered, "
        "with generous negative space and a premium, minimal backdrop."
    ),
}

_PRODUCT_SETTING_MAP = {
    "studio": "on a minimalist studio pedestal with a seamless backdrop",
    "cafe": "on a rustic wooden cafe table with soft ambient depth behind it",
    "urban": "against a stylish urban backdrop with modern architectural lines",
    "beach": "on sun-warmed driftwood with the shoreline softly blurred behind",
    "rooftop": "on a rooftop terrace ledge overlooking a city skyline",
    "garden": "on a stone surface amid lush greenery and blooming flowers",
    "street": "on a bistro table along a charming cobblestone street",
    "luxury-interior": "on a marble surface in an opulent interior",
    "nature": "on natural stone in a serene landscape",
    "park": "on a picnic table in a sunlit park",
}


_TITLE_ACRONYMS = {"anc", "led", "usb", "uhd", "qsr", "tv"}


def _product_title(product: Product) -> str:
    """Display title from a slug-style name; keeps unit tokens like "330ml" verbatim."""
    words = []
    for word in product.name.replace("_", " ").replace("-", " ").split():
        if word.lower() in _TITLE_ACRONYMS:
            words.append(word.upper())
        elif any(c.isdigit() for c in word):
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _with_article(phrase: str) -> str:
    if phrase and phrase[0].lower() in "aeiou":
        return f"an {phrase}"
    return f"a {phrase}"


def _render_attributes(product: Product) -> str:
    if not product.attributes:
        return ""
    details = ", ".join(
        f"{k.replace('_', ' ')}: {v}"
        for k, v in sorted(product.attributes.items())
    )
    return f"Product details: {details}."


def _build_product_scene_prompt(
    product: Product, variation: CreativeVariation, archetype: str
) -> str:
    product_title = _product_title(product)
    setting_desc = _PRODUCT_SETTING_MAP.get(
        variation.setting, f"in {_with_article(variation.setting)} setting"
    )
    time_map = {
        "golden-hour": "during golden hour with warm, soft light",
        "sunrise": "at sunrise with soft pink and orange morning light",
        "day": "in bright natural daylight",
        "sunset": "at sunset with warm orange and purple hues",
        "dusk": "at dusk with purple twilight ambiance",
        "night": "at night with atmospheric city lights and ambient glow",
        "morning": "in soft morning light",
    }
    time_desc = time_map.get(variation.time_of_day, "")
    lighting_map = {
        "natural": "Natural, soft lighting",
        "studio": "Professional studio lighting with soft shadows",
        "dramatic": "Dramatic contrast lighting with deep shadows",
        "soft": "Soft, diffused ethereal lighting",
        "golden": "Warm golden hour lighting",
        "neon": "Atmospheric neon lighting with colorful accents",
        "moody": "Moody, atmospheric low-key lighting",
    }
    lighting_desc = lighting_map.get(
        variation.lighting, f"{variation.lighting} lighting"
    )
    style_map = {
        "cinematic": "Cinematic commercial product photography",
        "editorial": "High-end editorial product photography",
        "commercial": "Polished commercial advertising photography",
        "artistic": "Artistic still-life product photography",
        "documentary": "Natural documentary-style product photography",
    }
    style_desc = style_map.get(
        variation.visual_style, "Professional commercial product photography"
    )
    key_features = product.description or "its signature design details"
    presented_clause = " ".join(filter(None, [setting_desc, time_desc]))
    attrs = _render_attributes(product)
    attr_line = f"\n{attrs}" if attrs else ""

    return f"""{style_desc} of {product_title}, presented {presented_clause}.

{_ARCHETYPE_SCENE_FLAVOR[archetype]}

{lighting_desc}. The product is clearly visible with all its details: {key_features}.{attr_line}

CRITICAL - PRODUCT PRESERVATION:
- The product must match the reference EXACTLY - same shape, colors, materials, branding, and label design
- Do NOT alter, modify, or reinterpret the product design, packaging, or logo in any way

QUALITY REQUIREMENTS:
- The product is the clear hero of the frame, sharply in focus
- Professional advertisement quality with {_with_article(variation.mood)} mood
- Vertical 9:16 aspect ratio composition
- The scene should look like the perfect first frame of a premium video ad

{_NO_TEXT_BLOCK}

Style: Premium retail campaign, magazine-quality, aspirational."""


def _build_product_animation_prompt(
    product: Product, variation: CreativeVariation, archetype: str
) -> str:
    camera_map = {
        "orbit": "Camera slowly orbits around the product",
        "pan": "Camera pans smoothly across the scene",
        "dolly": "Camera dollies in slowly toward the product",
        "static": "Camera holds steady with subtle breathing movement",
        "tracking": "Camera glides alongside the product",
        "crane": "Camera sweeps with elegant crane movement",
        "handheld": "Camera has subtle natural handheld movement",
    }
    camera_desc = camera_map.get(
        variation.camera_movement, "Camera moves smoothly"
    )
    motion_flavor = {
        CONSUMABLE_HERO: "condensation droplets glistening, gentle steam or fizz, "
        "ingredients settling naturally",
        STAGED_PRODUCT: "ambient light shifting across surfaces, subtle environmental "
        "movement around the product",
        PRODUCT_HERO: "light sweeping slowly across the product's surfaces",
    }[archetype]
    energy_map = {
        "calm": "slow, graceful, meditative pace",
        "moderate": "smooth, elegant movement",
        "dynamic": "energetic, fluid motion",
        "high-energy": "vibrant, dynamic, fast-paced movement",
    }
    energy_desc = energy_map.get(variation.energy, "smooth, elegant movement")
    product_title = _product_title(product)

    return f"""{camera_desc}, showcasing {product_title} from its most appealing angles.

The scene has {motion_flavor}. The movement is {energy_desc}.

CRITICAL - PRODUCT & QUALITY PRESERVATION:
- Maintain the product's exact appearance from the first frame throughout the video
- Branding, labels, and materials stay crisp and unaltered
- Smooth, professional camera work
- High-end retail advertisement aesthetic

{_AUDIO_BLOCK}

8 seconds. Vertical 9:16. Cinematic quality. Professional product video ad."""


def _build_product_creative_prompt(
    product: Product, variation: CreativeVariation, archetype: str
) -> str:
    scene = _build_product_scene_prompt(product, variation, archetype)
    motion = _build_product_animation_prompt(product, variation, archetype)
    return f"{scene}\n\n{motion}"
