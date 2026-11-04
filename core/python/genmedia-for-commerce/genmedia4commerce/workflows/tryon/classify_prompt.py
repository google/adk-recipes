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

"""The classifier's system text and response schema (``classify``)."""

from typing import get_args

from genmedia4commerce.core.types import GARMENTS, Category, Landmark, View

__all__ = [
    "CATEGORIES",
    "FULL_BODY_LOOK",
    "LANDMARKS",
    "SCHEMA",
    "VIEWS",
    "system",
]

CATEGORIES = get_args(Category)
VIEWS = get_args(View)
LANDMARKS = get_args(Landmark)  # top to bottom
FULL_BODY_LOOK = "full_body_look"  # a whole-outfit photo, never a product

_CATEGORY_RULES = """Categories:
- "top": shirts, t-shirts, blouses, sweaters, hoodies, turtlenecks, tank tops, vests, short jackets, cropped coats (any upper body garment that ends at or above the hips)
- "full_body_outer": long coats, trench coats, long cardigans, dusters, any outerwear that extends below the hips/to the knees or longer
- "bottom": pants, trousers, jeans, shorts, skirts (ONLY if no straps or bib — pure waist-down garments)
- "dress": dresses, jumpsuits, rompers, overalls, bib shorts, bib pants, cycling bibs, dungarees, any garment with shoulder straps or bib construction (any single garment that extends both above and below the waist)
- "footwear": any footwear (sneakers, heels, boots, sandals, slippers, cleats)
- "headwear": hats, caps, beanies, headbands
- "eyewear": glasses, sunglasses
- "jewelry_head": earrings and other jewellery worn on the ears or face
- "jewelry_neck": necklaces, pendants, chokers, scarves
- "jewelry_hand": rings, bracelets, watches
- "bag": handbags, shoulder bags, backpacks, totes, any carried bag"""

# Where each garment starts and ends (``Product.span``).
_GARMENT_RULES = (
    'GARMENTS: "garments" has one entry per group of photos whose category '
    "is "
    + ", ".join(f'"{g}"' for g in GARMENTS[:-1])
    + f' or "{GARMENTS[-1]}", none for any other: "g" its group, "starts" '
    'the highest point and "ends" the lowest point the garment reaches on a '
    "standing person, as its photos show it, each one of, from the top "
    f"down: {', '.join(LANDMARKS)}. Shoulder straps and a bib start at the "
    "shoulders. For example: a t-shirt starts at the shoulders and ends at "
    "the hips; cycling bib shorts start at the shoulders and end at "
    "mid-thigh; trousers start at the waist and end at the ankles; a "
    "knee-length coat starts at the shoulders and ends at the knees."
)

_SYSTEM = """You are a product classifier for virtual try-on. You receive {n} product photo(s), labelled IMAGE_0 to IMAGE_{last}, in upload order. Look at all of them together: comparing photos of the same item is how you tell front from back.

Return one row per photo, in the same order:
- "c": the CATEGORY, one of: {categories}. "full_body_look" is a full-body look photo (see FULL-BODY LOOK below); every other value is the category of the product the photo shows.
- "d": a short generic NAME of the product: its type and cut in 1 to 4 words (e.g. "cycling bib shorts", "t-bar loafers", "sleeveless wrap coat"). No colour, material, pattern, logo, brand, text or hardware: the photos show those. Only when two different products would get the same name, add the one word that tells them apart. A "full_body_look" photo: "full model reference".
- "s": the SIDE the photo shows: "front", "back" or "other".
- "w": true when the product is photographed on a person and the photo focuses on it; false for a packshot, flat-lay, mannequin, or a person who is not the focus.
- "x": true when the photo shows no product in full: it frames one or more parts of the product (a logo, a buckle, a pocket, the waist and the hip, a sleeve and its cuff, the fabric) and leaves the rest of the product outside the frame. false when the whole product is inside the frame, worn or flat, from any side, even small; a part hidden by another garment still counts as inside the frame. false also for a close-up whose main subject is something worn with the product (another garment, an accessory).
- "g": the GROUP number. Photos of the same item (other angles, worn or flat, close-ups of its parts) share the group number, the category and the name. Number the groups 1, 2, 3, ... in order of first appearance. A "full_body_look" photo is a reference, not a product photo: its group is -1. Any other photo of a person wearing several uploaded items goes in the group of the item it focuses on. A belt is part of the bottom it is on (skirt, trousers), never a product of its own: its photos go in the group of that bottom.

FULL-BODY LOOK: a photo of a person seen from head to feet (head to shoes) wearing a whole outfit, several garments with the shoes included, is a "full_body_look". It is a styling reference that shows how the products are worn together, never a photo of one product, even when one of the uploaded products is in it. A photo cut at the knees, the thighs or the waist, a photo that focuses on one garment, a close-up, a flat lay and a packshot are never a "full_body_look". A "full_body_look" photo has the name "full model reference" and the group -1; its "s" is the side it shows ("front", "back" or "other"), "w" is true and "x" is false.

ITEMS: for every photo, "items" lists every product shown whole inside the frame, one entry per product: garments, shoes, bags, hats, glasses, jewellery and scarves. A part hidden by another garment still counts as inside the frame; a product cut off by the frame is never listed. For each item: "c" its category (a product category, never "full_body_look"), "d" its short name (the same rules as "d" above), "g" its group. "covered" is true when another garment hides most of the item (an inner layer under a jacket or coat, a shirt showing only its collar), else false. The group is the group of the uploaded product when the item is that product: the same item as in the other photos, even worn open instead of closed, seen from another side, or partly covered. Every part of an uploaded product is that product: its lining, fur, hood, collar, cuffs or belt are never items of their own. Any other item gets a new group number, after every group of the photos; the same item in two photos has the same group. "instead_of", only for an item in a new group: the group of the uploaded product it would replace, when both are the same kind of item worn in the same layer (a jacket and an uploaded jacket, a necklace and an uploaded necklace; never a jacket worn over an uploaded shirt); else leave it out. A belt worn on a bottom is part of that bottom. For a "full_body_look" photo whose "s" is "back", "items" is [].

{garment_rules}

STRICT RULE: The entire product must be visible in the final photo. If a garment has ANY component that extends above the waist (shoulder straps, bib front, suspenders, crossed back straps), it MUST be classified as "dress" — even if the lower portion looks like shorts or pants. This includes cycling bib shorts, ski bib pants, fishing waders, overalls, dungarees, and any garment with built-in straps.

You must also consider the LENGTH of the garment:
- A short jacket (waist-length or hip-length) is a "top"
- A long coat or trench coat that extends below the hips/to the knees or longer is a "full_body_outer"

{category_rules}

View classification for each image (worn photos too: a person facing the camera shows the front of what they wear):
- "front": the front of the product squarely facing the camera, its left and right halves equally visible. For footwear, a side (lateral) or 3/4 view showing the shoe's main design counts as "front". For a zoom photo ("x" true): the part it frames is on the front of the product.
- "back": any view of the back of the product, straight or turned, even slightly. For footwear, the heel view. For a zoom photo: the part it frames is on the back of the product.
- "other": a front turned away from the camera, from a three-quarter to a side view (except footwear); a top-down or sole-only view; several products in one image; or not a valid product photo. For a zoom photo: the part it frames is on a side of the product, or has no side (a fabric swatch).

How to determine front vs back — use these cues in order of reliability:
1. **Closures and hardware**: buttons, main zippers, snaps, front clasps → FRONT. Back zippers (e.g. dresses) → BACK.
2. **Pockets**: front hip pockets, chest pockets → FRONT. Back patch pockets (e.g. jeans, cycling jerseys) → BACK.
3. **Garment construction and cut**: the FRONT of a garment typically has MORE fabric coverage and structure on the torso (wider panels, fuller coverage). The BACK typically has LESS coverage or thinner construction (racerback straps, cutaway shoulders, thinner panels, open-back designs). For example: vests have a full front panel but a thinner back; tank tops have wider straps in front but a racerback behind; cycling bibs have a bib panel covering the front abdomen while the back has a mesh racerback with a chamois pad in the seat area.
4. **Neckline shape**: deeper V-neck, scoop neck, or decorative neckline → usually FRONT. Higher, simpler neckline → usually BACK.
5. **Tags and labels**: small fabric tags at the inner neckline are typically at the BACK of the garment.

Return JSON only."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "photos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "c": {
                        "type": "STRING",
                        "enum": [*CATEGORIES, FULL_BODY_LOOK],
                    },
                    "d": {"type": "STRING"},
                    "s": {"type": "STRING", "enum": list(VIEWS)},
                    "w": {"type": "BOOLEAN"},
                    "x": {"type": "BOOLEAN"},
                    "g": {"type": "INTEGER"},
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "c": {
                                    "type": "STRING",
                                    "enum": list(CATEGORIES),
                                },
                                "d": {"type": "STRING"},
                                "g": {"type": "INTEGER"},
                                "covered": {"type": "BOOLEAN"},
                                "instead_of": {"type": "INTEGER"},
                            },
                            "required": ["c", "d", "g", "covered"],
                        },
                    },
                },
                "required": ["c", "d", "s", "w", "x", "g", "items"],
            },
        },
        "garments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "g": {"type": "INTEGER"},
                    "starts": {"type": "STRING", "enum": list(LANDMARKS)},
                    "ends": {"type": "STRING", "enum": list(LANDMARKS)},
                },
                "required": ["g", "starts", "ends"],
                "propertyOrdering": ["g", "starts", "ends"],
            },
        },
    },
    "required": ["photos", "garments"],
    "propertyOrdering": ["photos", "garments"],
}


def system(n: int) -> str:
    """The system text for ``n`` photos."""
    return _SYSTEM.format(
        n=n,
        last=n - 1,
        categories=", ".join([*CATEGORIES, FULL_BODY_LOOK]),
        category_rules=_CATEGORY_RULES,
        garment_rules=_GARMENT_RULES,
    )
