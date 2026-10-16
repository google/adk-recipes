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

"""Pydantic models for video properties extracted from AI-generated video ads."""

from enum import StrEnum

from pydantic import BaseModel, Field


class MoodType(StrEnum):
    """Primary emotional tone of the video."""

    QUIRKY = "quirky"
    WARM = "warm"
    BOLD = "bold"
    SERENE = "serene"
    MYSTERIOUS = "mysterious"
    PLAYFUL = "playful"
    SOPHISTICATED = "sophisticated"
    ENERGETIC = "energetic"
    ELEGANT = "elegant"
    ROMANTIC = "romantic"


class VisualStyle(StrEnum):
    """Overall visual treatment style of the video."""

    CINEMATIC = "cinematic"
    DOCUMENTARY = "documentary"
    EDITORIAL = "editorial"
    COMMERCIAL = "commercial"
    ARTISTIC = "artistic"
    MINIMALIST = "minimalist"
    VINTAGE = "vintage"
    MODERN = "modern"


class EnergyLevel(StrEnum):
    """Pace and movement intensity of the video."""

    CALM = "calm"
    MODERATE = "moderate"
    DYNAMIC = "dynamic"
    HIGH_ENERGY = "high_energy"


class ColorTemperature(StrEnum):
    """Overall color grading temperature."""

    WARM = "warm"
    NEUTRAL = "neutral"
    COOL = "cool"


class AudioType(StrEnum):
    """Type of audio in the video (for future audio support)."""

    NONE = "none"
    AMBIENT = "ambient"
    MUSIC_UPBEAT = "music_upbeat"
    MUSIC_CALM = "music_calm"
    MUSIC_DRAMATIC = "music_dramatic"
    DIALOGUE = "dialogue"
    VOICEOVER = "voiceover"


class CameraMovement(StrEnum):
    """Primary camera movement in the video."""

    STATIC = "static"
    PAN = "pan"
    ORBIT = "orbit"
    TRACK = "track"
    DOLLY = "dolly"
    CRANE = "crane"
    HANDHELD = "handheld"
    SLOW_ZOOM = "slow_zoom"


class LightingStyle(StrEnum):
    """Lighting approach in the video."""

    NATURAL = "natural"
    STUDIO = "studio"
    DRAMATIC = "dramatic"
    SOFT = "soft"
    HIGH_KEY = "high_key"
    LOW_KEY = "low_key"
    GOLDEN_HOUR = "golden_hour"
    NEON = "neon"


class VideoProperties(BaseModel):
    """Structured properties extracted from or specified for video ads.

    This model defines all the controllable and analyzable properties of a video ad.
    Properties can be:
    1. Extracted from generated videos using Gemini video analysis
    2. Specified when generating new videos for templated control
    3. Used to apply winning formulas from top-performing ads
    """

    # Mood/Emotion Properties
    mood: MoodType = Field(
        default=MoodType.ELEGANT,
        description="Primary emotional tone of the video",
    )
    mood_intensity: float = Field(
        default=0.7, ge=0, le=1, description="Intensity of the mood (0-1)"
    )
    has_warmth: bool = Field(
        default=True, description="Whether the video conveys warmth/comfort"
    )

    # Visual Style Properties
    visual_style: VisualStyle = Field(
        default=VisualStyle.CINEMATIC,
        description="Overall visual treatment style",
    )
    camera_movement: CameraMovement = Field(
        default=CameraMovement.ORBIT, description="Primary camera movement"
    )
    lighting_style: LightingStyle = Field(
        default=LightingStyle.STUDIO, description="Lighting approach"
    )

    # Energy/Pace Properties
    energy_level: EnergyLevel = Field(
        default=EnergyLevel.MODERATE, description="Pace and movement intensity"
    )
    movement_amount: float = Field(
        default=0.5, ge=0, le=1, description="Amount of subject movement (0-1)"
    )

    # Color Properties
    color_temperature: ColorTemperature = Field(
        default=ColorTemperature.NEUTRAL, description="Overall color grading"
    )
    dominant_colors: list[str] = Field(
        default_factory=lambda: ["neutral"],
        description="Primary colors in the video",
    )
    color_saturation: float = Field(
        default=0.7, ge=0, le=1, description="Color saturation level (0-1)"
    )

    # Subject Properties
    subject_count: int = Field(
        default=1, ge=1, description="Number of subjects/models"
    )
    garment_visibility: float = Field(
        default=0.8,
        ge=0,
        le=1,
        description="How prominently garment is featured (0-1)",
    )
    has_multiple_outfits: bool = Field(
        default=False, description="Whether multiple outfits are shown"
    )

    # Audio Properties (for future audio support)
    audio_type: AudioType = Field(
        default=AudioType.NONE, description="Type of audio"
    )
    has_dialogue: bool = Field(
        default=False, description="Whether video has dialogue"
    )
    music_tempo: str | None = Field(
        default=None,
        description="Music tempo if applicable (slow, medium, fast)",
    )
    audio_mood: str | None = Field(
        default=None,
        description="Mood of the audio (uplifting, dramatic, calm, etc.)",
    )

    # Quality/Style Tags
    style_tags: list[str] = Field(
        default_factory=list, description="Descriptive style tags"
    )
    quality_score: float | None = Field(
        default=None, ge=0, le=1, description="Estimated quality score (0-1)"
    )

    # Setting/Environment Properties
    setting_type: str = Field(
        default="studio",
        description="Setting category (outdoor, studio, urban, nature, etc.)",
    )
    time_of_day: str = Field(
        default="day",
        description="Time of day depicted (golden_hour, day, night, dawn, dusk)",
    )
    background_complexity: float = Field(
        default=0.3,
        ge=0,
        le=1,
        description="Background visual complexity (0=minimal, 1=busy)",
    )

    # Production Properties
    aspect_ratio: str = Field(default="9:16", description="Video aspect ratio")
    has_text_overlays: bool = Field(
        default=False, description="Whether video has text overlays"
    )
    has_brand_elements: bool = Field(
        default=False, description="Whether video has visible brand elements"
    )

    # Note: json_schema_extra with 'examples' is NOT supported by Gemini structured output.
    # Gemini only allows: type, format, description, nullable, enum, maxItems, minItems,
    # properties, required, propertyOrdering, items
    # See: https://ai.google.dev/gemini-api/docs/structured-output
    model_config = {
        "use_enum_values": True,
    }

    def to_prompt_fragment(self) -> str:
        """Convert properties to a prompt fragment for video generation."""
        fragments = []

        # Mood description
        mood_desc = {
            "quirky": "with a quirky, unexpected charm",
            "warm": "with warm, inviting atmosphere",
            "bold": "with bold, striking confidence",
            "serene": "with serene, peaceful elegance",
            "mysterious": "with an air of mystery",
            "playful": "with playful, fun energy",
            "sophisticated": "with sophisticated refinement",
            "energetic": "with vibrant, high energy",
            "elegant": "with timeless elegance",
            "romantic": "with romantic, dreamy quality",
        }
        if self.mood in mood_desc:
            fragments.append(mood_desc[self.mood])

        # Energy description
        energy_desc = {
            "calm": "The subject moves gently and gracefully",
            "moderate": "The subject moves with measured elegance",
            "dynamic": "The subject moves with dynamic energy",
            "high_energy": "The subject moves with vibrant, high energy",
        }
        if self.energy_level in energy_desc:
            fragments.append(energy_desc[self.energy_level])

        # Camera movement
        camera_desc = {
            "static": "Camera holds steady",
            "pan": "Camera pans smoothly",
            "orbit": "Camera slowly orbits around the subject",
            "track": "Camera tracks alongside the subject",
            "dolly": "Camera dollies in smoothly",
            "crane": "Camera moves with crane-like fluidity",
            "handheld": "Camera has subtle handheld movement",
            "slow_zoom": "Camera slowly zooms",
        }
        if self.camera_movement in camera_desc:
            fragments.append(camera_desc[self.camera_movement])

        # Lighting
        fragments.append(f"{self.lighting_style} lighting")

        # Color temperature
        fragments.append(f"{self.color_temperature} color tones")

        # Setting
        fragments.append(f"Shot in a {self.setting_type} setting")
        if self.time_of_day != "day":
            fragments.append(f"during {self.time_of_day}")

        return ". ".join(fragments) + "."
