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

"""Request, reference and plan types of the Video Try-On workflow."""

from dataclasses import dataclass, field
from typing import Literal

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.types import Classification, Framing
from genmedia4commerce.workflows.details import Detail

__all__ = [
    "Closeup",
    "Motion",
    "Move",
    "Plan",
    "ReferenceSet",
    "Shot",
    "VideoTryOnRequest",
]

# auto: the planner picks the shots from the images; custom: it turns the
# user's request into shots (prompt), within what the images can show.
Motion = Literal["auto", "custom"]
Move = Literal[
    "pose", "walk", "push_in", "tilt", "pull_out", "detail", "turn_back"
]
Transition = Literal["continuous", "crossfade", "cut"]


@dataclass
class VideoTryOnRequest:
    """One Video Try-On run.

    Without ``tryon_image`` the image try-on runs first on ``person_image``
    and ``product_images`` (stage 1) and its best result is animated. With
    ``tryon_image`` (the dressed person) stage 1 is skipped. The video
    references always go as separate images. ``image`` ``None`` follows
    the video (``pipeline.stage_one_image``); ``policy`` (both
    stages) and ``video`` ``None`` use the deployment run policy and
    ``VideoParams(aspect_ratio="9:16")``.

    With ``tryon_image``, ``product_images`` are the products it shows:
    the photo the try-on chose for each (with its ``classification`` and
    ``closeups``, from the try-on's run record) or, unclassified, photos
    that are classified first, as the try-on does. ``videos_per_image``:
    the videos of each animated image (None: ``settings.videos_per_image``).
    """

    # Stage 1 (image try-on), used when tryon_image is None. The video model
    # also gets the photo of each accessory and pair of shoes (every photo
    # when unclassified), as close-up references.
    product_images: list[bytes] = field(default_factory=list)
    person_image: bytes | None = None
    reference_model_image: bytes | None = None
    complete_look: bool = True  # see TryOnRequest
    upscale_crops: bool = False  # see TryOnRequest
    style_guidelines: str | None = None
    scenario: str | None = None
    image: ImageParams | None = None  # stage-1 image params
    # Shortcut: animate an existing try-on image
    tryon_image: bytes | None = None
    back_image: bytes | None = None  # its back view, when there is one
    classification: Classification | None = None  # names the products
    # The close-ups of each product photo, found by the try-on that made
    # tryon_image (its run record); None: searched here.
    closeups: list[list[Detail]] | None = None
    # Stage 2 (video)
    motion: Motion = "auto"
    prompt: str | None = None  # the user's request; required when "custom"
    policy: RunPolicy | None = None
    video: VideoParams | None = None
    videos_per_image: int | None = None

    @property
    def animated_image(self) -> bytes:
        """``tryon_image``, the image the video stage animates: set by then
        (``pipeline`` checks the request and animates made images only)."""
        if self.tryon_image is None:
            raise InputError("no tryon_image to animate")
        return self.tryon_image


@dataclass
class Closeup:
    """A product photo sent as a close-up (detail) reference."""

    index: int  # its position in ``ReferenceSet.images``
    name: str  # what the plan and the prompt call it, e.g. "bag"
    worn: str  # how the video shows it, e.g. "in the person's hand"
    # Its zoomed details: (index in ``images``, what it shows), e.g.
    # (7, "padlock on the strap").
    details: list[tuple[int, str]] = field(default_factory=list)
    # Its product's description, told to the detail search ("": unknown).
    product: str = ""


@dataclass
class ReferenceSet:
    """The images sent to the video model, in order; 0 is the try-on."""

    images: list[bytes]
    framing: Framing = "full_body"  # of the try-on image
    back: int | None = None  # index of the back view
    face: int | None = None  # index of the face close-up
    closeups: list[Closeup] = field(default_factory=list)


@dataclass
class Shot:
    """One shot of the animation (``plan.MOVES`` describes each move)."""

    move: Move
    seconds: float
    region: str = ""  # push_in/tilt: a body region; detail: a close-up name
    to_region: str = ""  # tilt only
    transition: Transition = "continuous"  # into this shot
    action: str = ""  # a short extra for the shot, facing the camera


@dataclass
class Plan:
    """The animation: 1 to 3 shots, 8 to 10 seconds in total."""

    shots: list[Shot]
    note: str = ""  # for the user, e.g. why a custom request was adapted
    changes: list[str] = field(default_factory=list)  # what the code fixed

    @property
    def duration(self) -> int:
        """The video length in whole seconds."""
        return round(sum(s.seconds for s in self.shots))

    @property
    def shows_back(self) -> bool:
        return any(s.move == "turn_back" for s in self.shots)

    def summary(self) -> str:
        """e.g. "walk → detail (bag) → pull_out, 10 s"."""
        moves = " → ".join(
            s.move + (f" ({s.region})" if s.region else "") for s in self.shots
        )
        return f"{moves}, {self.duration} s"
