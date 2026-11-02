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

"""Domain types shared by workflows, evals and adapters."""

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "FRAMING_BY_CATEGORY",
    "GARMENTS",
    "Category",
    "Classification",
    "Framing",
    "Landmark",
    "Mode",
    "Photo",
    "PhotoItem",
    "Product",
    "ProductRef",
    "StoredVideo",
    "Verdict",
    "View",
    "quality_score",
]


class StoredVideo(bytes):
    """MP4 bytes of a stored video interaction, which a later turn can extend.

    ``interaction_id``: this turn; ``extends``: the stored turn it continued,
    if any (its video is the start of this one).
    """

    interaction_id: str
    extends: str | None

    def __new__(cls, data: bytes, interaction_id: str, extends: str | None):
        video = super().__new__(cls, data)
        video.interaction_id, video.extends = interaction_id, extends
        return video


@dataclass
class Verdict:
    """Outcome of an evaluation.

    ``error=True`` means the evaluator failed (transient); never publish then.
    ``details`` is free-form and JSON-serialisable.
    """

    publish: bool
    imperfection_count: int = 0
    description: str = ""
    error: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def quality_score(publish: bool, imperfections: int) -> float:
    """Return the 0-100 score of a verdict (``Verdict.details["score"]``)."""
    return float(max(0, 100 - 15 * imperfections - 30 * (not publish)))


Framing = Literal[
    "head", "upper_body", "lower_body", "footwear", "full_body", "hands"
]
Category = Literal[
    "top",
    "bottom",
    "dress",
    "full_body_outer",
    "footwear",
    "headwear",
    "eyewear",
    "jewelry_head",
    "jewelry_neck",
    "jewelry_hand",
    "bag",
]
View = Literal["front", "back", "other"]
Mode = Literal["single", "outfit"]
Presentation = Literal["worn", "flat"]
RefSource = Literal["upload", "model_crop"]
# Where a garment starts and ends on a standing person, top to bottom.
Landmark = Literal[
    "head",
    "chin",
    "shoulders",
    "chest",
    "waist",
    "hips",
    "mid-thigh",
    "knees",
    "mid-calf",
    "ankles",
    "feet",
]
# Worn on the body: re-graded to the wearer's size, and a single one is
# framed by where it starts and ends (``Product.span``).
GARMENTS: tuple[Category, ...] = ("top", "bottom", "dress", "full_body_outer")

FRAMING_BY_CATEGORY: dict[Category, Framing] = {
    "top": "upper_body",
    "bottom": "lower_body",
    "dress": "full_body",
    "full_body_outer": "full_body",
    "footwear": "footwear",
    "headwear": "head",
    "eyewear": "head",
    "jewelry_head": "head",
    "jewelry_neck": "upper_body",
    "jewelry_hand": "hands",
    "bag": "full_body",
}


@dataclass
class ProductRef:
    """One reference photo of a product."""

    image: bytes
    view: View
    rank: int = 1  # 1 = the best of the product's photos of this view
    presentation: Presentation = "flat"
    source: RefSource = "upload"
    # A worn photo (or a crop of one) whose person became a mannequin.
    mannequin: bool = False

    @property
    def priority(self) -> int:
        """Return 1 for a worn upload, 2 for a flat one, 3 for a reference
        crop (crops go along as extra views: ``outfit.extra_crops``)."""
        if self.source == "model_crop":
            return 3
        return 1 if self.presentation == "worn" else 2


@dataclass
class Product:
    """A distinct product and its reference photos."""

    id: str  # "A", "B", ...
    category: Category
    description: str
    refs: list[ProductRef]
    # Close-up photos of a part of it (a logo, a buckle, the fabric), each
    # with the side of the part it frames (its view); never in ``refs``.
    details: list[ProductRef] = field(default_factory=list)
    # A garment: where it starts and ends on a standing person, as its
    # photos show it (``classify``); None for any other product.
    span: tuple[Landmark, Landmark] | None = None

    def refs_for(self, view: View) -> list[ProductRef]:
        """Return the refs showing ``view``, best first (priority, rank).

        Equal ranks keep the upload order.
        """
        return sorted(
            (r for r in self.refs if r.view == view),
            key=lambda r: (r.priority, r.rank),
        )

    def best_ref(self, view: View | None = None) -> ProductRef | None:
        """Return the best ref for ``view`` (any view when ``None``)."""
        refs = (
            self.refs_for(view)
            if view
            else sorted(self.refs, key=lambda r: (r.priority, r.rank))
        )
        return refs[0] if refs else None


@dataclass
class PhotoItem:
    """A product the classifier saw whole in a photo."""

    category: Category
    name: str
    # The uploaded product it is (``Product.id``); any other item: a key of
    # its own ("g7"), the same in every photo that shows it.
    product: str
    covered: bool = False  # another garment hides most of it


@dataclass
class Photo:
    """One photo as the classifier saw it."""

    product: str | None  # the product it is a photo of; None: a look photo
    view: View
    worn: bool
    zoom: bool  # frames parts of its product only (a detail photo)
    items: list[PhotoItem] = field(default_factory=list)  # shown whole
    mannequin: bool = False  # worn, and its person became a mannequin


@dataclass
class Classification:
    """Result of classifying the product images of a request."""

    mode: Mode
    products: list[Product]
    # outfit -> "full_body"; single -> FRAMING_BY_CATEGORY (a single
    # garment's FRAMING text comes from its span: ``tryon.framing``)
    framing: Framing
    # The photo (index in ``photos``) that shows a person head to toe in a
    # whole outfit, from the front or turned: the reference model, else a
    # look photo among the uploads; None without one.
    look_photo: int | None = None
    # Every photo classified, in upload order (the reference model last).
    photos: list[Photo] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-friendly summary without image bytes."""
        return {
            "mode": self.mode,
            "framing": self.framing,
            "products": [
                {
                    "id": p.id,
                    "category": p.category,
                    "description": p.description,
                    "refs": [{"view": r.view, "rank": r.rank} for r in p.refs],
                }
                for p in self.products
            ],
        }
