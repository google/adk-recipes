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

"""Creative variation parameters for video generation.

This module defines the CreativeVariation Pydantic model which controls
all aspects of video generation: model characteristics, setting,
mood, camera work, and visual style.

Using Pydantic BaseModel for ADK compatibility with automatic function calling.
"""

from typing import Any

from pydantic import BaseModel, Field


class CreativeVariation(BaseModel):
    """Defines creative parameters for video generation.

    The CreativeVariation controls all aspects of how a product video
    will be generated, including:
    - Model characteristics (ethnicity, description)
    - Setting and location
    - Time, season, and weather
    - Mood and energy level
    - Camera movement and angle
    - Props and model activity
    - Lighting and visual style
    """

    name: str = Field(
        description="Unique variation identifier (e.g., 'asian-urban-confident')"
    )

    # Presentation mode
    presentation_mode: str | None = Field(
        default=None,
        description="How the product is presented: None/'auto' (archetype registry decides "
        "by product category), 'with_model' (human model — wearable products only), "
        "'product_only' (product-centric shot, no human)",
    )

    # Model characteristics
    model_ethnicity: str | None = Field(
        default="diverse",
        description="Model ethnicity: asian, european, african, latina, middle-eastern, south-asian, diverse",
    )
    model_description: str | None = Field(
        default="", description="Override for specific model description"
    )

    # Setting/Location
    setting: str = Field(
        default="studio",
        description="Setting: beach, urban, cafe, rooftop, studio, garden, street, luxury-interior, nature",
    )
    location_detail: str = Field(
        default="", description="Additional location specifics"
    )

    # Time and Season
    season: str = Field(
        default="neutral",
        description="Season: summer, winter, fall, spring, neutral",
    )
    time_of_day: str = Field(
        default="day",
        description="Time of day: golden-hour, sunrise, day, sunset, dusk, night",
    )
    weather: str = Field(
        default="clear",
        description="Weather: clear, cloudy, rainy, snowy, foggy",
    )

    # Mood and Atmosphere
    mood: str = Field(
        default="elegant",
        description="Mood: romantic, energetic, sophisticated, playful, bold, mysterious, serene, confident",
    )
    energy: str = Field(
        default="moderate",
        description="Energy level: calm, moderate, dynamic, high-energy",
    )

    # Camera and Movement
    camera_movement: str = Field(
        default="orbit",
        description="Camera movement: orbit, pan, dolly, static, tracking, crane, handheld",
    )
    camera_angle: str = Field(
        default="eye-level",
        description="Camera angle: low-angle, eye-level, high-angle, dutch-angle",
    )

    # Props and Companions
    props: list[str] = Field(
        default_factory=list,
        description="Props: dog, cat, coffee, umbrella, flowers, sunglasses, bag",
    )

    # Model Activity
    activity: str | None = Field(
        default="walking",
        description="Activity: walking, standing, sitting, dancing, spinning, posing, running",
    )

    # Lighting
    lighting: str = Field(
        default="natural",
        description="Lighting: natural, studio, dramatic, soft, golden, neon, moody",
    )

    # Visual Style
    visual_style: str = Field(
        default="cinematic",
        description="Visual style: cinematic, editorial, commercial, artistic, documentary",
    )
    color_grading: str = Field(
        default="neutral",
        description="Color grading: warm, cool, neutral, vintage, high-contrast",
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CreativeVariation":
        """Create a CreativeVariation from a dictionary."""
        return cls.model_validate(data)

    def get_summary(self) -> str:
        """Get a brief summary of the variation for display."""
        parts = [self.name]
        if (self.model_ethnicity or "diverse") != "diverse":
            parts.append(self.model_ethnicity)
        if self.setting != "studio":
            parts.append(self.setting)
        if self.mood != "elegant":
            parts.append(self.mood)
        return " | ".join(parts)

    class Config:
        """Pydantic configuration."""

        extra = "ignore"  # Ignore unknown fields


# Preset variations for common use cases
PRESET_VARIATIONS = {
    "diversity": [
        CreativeVariation(
            name="asian-urban-confident",
            model_ethnicity="asian",
            setting="urban",
            mood="confident",
            lighting="natural",
            activity="walking",
            time_of_day="golden-hour",
        ),
        CreativeVariation(
            name="european-studio-elegant",
            model_ethnicity="european",
            setting="studio",
            mood="elegant",
            lighting="studio",
            activity="posing",
            visual_style="editorial",
        ),
        CreativeVariation(
            name="african-rooftop-bold",
            model_ethnicity="african",
            setting="rooftop",
            mood="bold",
            lighting="golden",
            time_of_day="sunset",
            activity="standing",
        ),
        CreativeVariation(
            name="latina-beach-playful",
            model_ethnicity="latina",
            setting="beach",
            mood="playful",
            lighting="natural",
            season="summer",
            activity="walking",
        ),
        CreativeVariation(
            name="south-asian-garden-romantic",
            model_ethnicity="south-asian",
            setting="garden",
            mood="romantic",
            lighting="soft",
            time_of_day="golden-hour",
            props=["flowers"],
        ),
    ],
    "settings": [
        CreativeVariation(
            name="beach-summer",
            setting="beach",
            season="summer",
            mood="playful",
            lighting="natural",
            time_of_day="day",
        ),
        CreativeVariation(
            name="urban-night",
            setting="urban",
            time_of_day="night",
            mood="bold",
            lighting="neon",
            visual_style="cinematic",
        ),
        CreativeVariation(
            name="cafe-morning",
            setting="cafe",
            time_of_day="day",
            mood="sophisticated",
            props=["coffee"],
            activity="sitting",
        ),
        CreativeVariation(
            name="luxury-interior",
            setting="luxury-interior",
            mood="elegant",
            lighting="soft",
            visual_style="editorial",
        ),
        CreativeVariation(
            name="nature-fall",
            setting="nature",
            season="fall",
            mood="serene",
            lighting="golden",
            time_of_day="golden-hour",
        ),
    ],
    "moods": [
        CreativeVariation(
            name="romantic-soft",
            mood="romantic",
            lighting="soft",
            setting="garden",
            camera_movement="pan",
            color_grading="warm",
        ),
        CreativeVariation(
            name="energetic-dynamic",
            mood="energetic",
            energy="high-energy",
            setting="urban",
            camera_movement="tracking",
            activity="walking",
        ),
        CreativeVariation(
            name="mysterious-moody",
            mood="mysterious",
            lighting="moody",
            time_of_day="dusk",
            color_grading="cool",
            visual_style="cinematic",
        ),
        CreativeVariation(
            name="playful-fun",
            mood="playful",
            energy="dynamic",
            setting="beach",
            activity="dancing",
            props=["sunglasses"],
        ),
        CreativeVariation(
            name="confident-bold",
            mood="confident",
            setting="rooftop",
            lighting="dramatic",
            camera_angle="low-angle",
            visual_style="editorial",
        ),
    ],
}


def get_preset_variations(preset_name: str) -> list[CreativeVariation]:
    """Get a list of preset variations by name.

    Args:
        preset_name: One of 'diversity', 'settings', 'moods'

    Returns:
        List of CreativeVariation objects
    """
    return PRESET_VARIATIONS.get(preset_name, [])


def get_default_variation() -> CreativeVariation:
    """Get the default variation for quick video generation."""
    return CreativeVariation(
        name="default-elegant",
        model_ethnicity="diverse",
        setting="studio",
        mood="elegant",
        lighting="studio",
        activity="posing",
        visual_style="cinematic",
    )
