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

"""Request and intermediate types of the image try-on workflow."""

from dataclasses import dataclass, field
from typing import Literal

from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.retry import Deadline
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.types import (
    Category,
    Classification,
    Framing,
    Landmark,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows.details import Detail

__all__ = [
    "FEET_FRAMINGS",
    "HEAD_FRAMINGS",
    "CompositionPlan",
    "Prepared",
    "PromptProduct",
    "PromptSpec",
    "TryOnContext",
    "TryOnRequest",
    "ViewName",
]

ViewName = Literal["front", "back"]

# Framings that show the head: the prompt's identity rule.
HEAD_FRAMINGS: tuple[Framing, ...] = ("head", "upper_body", "full_body")
# Framings that show the feet: plain shoes, the ground shadow.
FEET_FRAMINGS: tuple[Framing, ...] = ("footwear", "lower_body", "full_body")


@dataclass
class TryOnRequest:
    """One try-on run. ``None`` fields fall back to the deployment settings."""

    product_images: list[bytes]
    person_image: bytes
    reference_model_image: bytes | None = None
    # Products only the reference model shows are cut from it and added.
    complete_look: bool = True
    # Shoes added from the reference model are upscaled to 2K first.
    upscale_crops: bool = False
    style_guidelines: str | None = None  # None -> settings.style_guidelines
    scenario: str | None = None  # None -> settings.scenario
    generate_back: bool = True
    policy: RunPolicy | None = None  # None -> RunPolicy.from_settings()
    image: ImageParams | None = None  # None -> ImageParams()


@dataclass
class CompositionPlan:
    """How the outfit is worn.

    ``items[i] = {"product_id", "tuck", "closure", "layer_order", "hem",
    "visibility"}``, one per product.
    """

    items: list[dict] = field(default_factory=list)


@dataclass
class TryOnContext:
    """Everything the prompt and the eval need for one run."""

    classification: Classification
    wearer_body: bytes
    wearer_face: bytes | None  # face crop for the prompt
    reference_face: bytes | None  # face crop for the judge
    style_guidelines: str
    scenario: str
    params: ImageParams
    chosen: list[tuple[Product, ProductRef]] = field(default_factory=list)
    plan: CompositionPlan | None = None


@dataclass
class PromptProduct:
    """One product slot: its images (primary first) and the classifier's
    one-sentence description."""

    id: str
    category: Category
    refs: list[ProductRef]  # single route: primary + 0..1 secondary; outfit: 1
    description: str


@dataclass
class PromptSpec:
    """Everything that varies between try-on prompts.

    One product = the single-product route (secondary ref, angle rule,
    category note); several = the outfit route (one image per product,
    ``outfit_prompt``). ``front_result`` makes this the back-view pass;
    ``closeups``: (product id, detail) crops of the products' photos and
    their detail photos;
    ``aspect_ratio``: the drawing's (``ImageParams.aspect_ratio``);
    ``span``: the single garment's (``Product.span``), which frames it
    (``framing``), the same for its back view; None: ``framing`` does;
    ``mannequin_crops``: the ``crops`` (by identity) cut from a photo whose
    person became a mannequin (``worn``), so their lines say so.
    """

    framing: Framing
    view: ViewName
    wearer_body: bytes
    products: list[PromptProduct]
    scenario: str
    wearer_face: bytes | None = None
    front_result: bytes | None = None
    composition_plan: CompositionPlan | None = None
    style_guidelines: str | None = None
    crops: list[tuple[bytes, list[str]]] = field(default_factory=list)
    closeups: list[tuple[str, Detail]] = field(default_factory=list)
    aspect_ratio: str = "3:4"
    span: tuple[Landmark, Landmark] | None = None
    mannequin_crops: list[bytes] = field(default_factory=list)


@dataclass
class Prepared:
    """What a run draws with: its context, policy and deadline.

    ``run_tryon`` sets them before its first front variation, so a caller
    can draw the back view of any of its fronts (``pipeline.back_view``).
    """

    ctx: TryOnContext | None = None
    policy: RunPolicy | None = None
    deadline: Deadline | None = None

    @property
    def has_back(self) -> bool:
        """A back view can be drawn: a single product with a back upload."""
        if self.ctx is None:
            return False
        c = self.ctx.classification
        return c.mode == "single" and any(
            r.source == "upload" for r in c.products[0].refs_for("back")
        )
