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
from typing import Literal

from pydantic import BaseModel, Field


class StyleProfile(BaseModel):
    """A model to hold the extracted theme/style of a presentation."""

    title_font_name: str
    title_font_size_pt: float
    title_font_color_rgb: tuple[int, int, int]
    body_font_name: str
    body_font_size_pt: float
    body_font_color_rgb: tuple[int, int, int]
    accent_colors: list[tuple[int, int, int]]
    image_box_hint: tuple[int, int, int, int] | None = None
    supports_subtitles: bool = False


class CoverSpec(BaseModel):
    title: str
    subhead: str | None = None


class SlideSpec(BaseModel):
    """
    Defines the content and structure for a single presentation slide.
    This is the single source of truth for both planning and final output.
    """

    title: str
    subhead: str | None = None
    bullets: list[str] = Field(
        default_factory=list,
        description="A list of bullet points. During planning, "
        "this contains a single summary of the slide's focus.",
    )

    layout_name: Literal[
        "Title Slide",
        "Title and Content",
        "Section Header",
        "Two Content",
        "Comparison",
        "Title Only",
        "Blank",
        "Title and Image",
        "Image Grid",
        "Title and Chart",
        "Quote",
        "Agenda",
    ] = "Title and Content"
    image_data: str | bytes | None = None
    visual_prompt: str | None = None
    citations: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="List of URL citations supporting the facts on this slide.",
    )
    image_position: dict[str, float] | None = None
    image_file_path: str | None = None
    speaker_notes: str | None = None


class DeckSpec(BaseModel):
    """The full specification for a presentation deck."""

    cover: CoverSpec
    slides: list[SlideSpec]
    closing_title: str


class PresentationOutline(BaseModel):
    """The initial structural plan for the presentation."""

    strategic_briefing: str = Field(
        description="A high-level summary of the opportunity, market context, and a winning strategy."
    )
    cover: CoverSpec
    slides: list[SlideSpec] = Field(
        description="The sequential list of planned slides."
    )
    closing_title: str


class SynthesizerResponse(BaseModel):
    """The structured output from the content synthesis specialist when generating an outline."""

    outline: PresentationOutline
