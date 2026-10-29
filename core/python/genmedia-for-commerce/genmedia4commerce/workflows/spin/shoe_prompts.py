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

"""Prompts and response schemas of the shoe reference set.

The tested wording, verbatim: changing it changes which photos and pieces
are kept.
"""

import json

__all__ = [
    "CLASSIFY_LABELS",
    "CLASSIFY_SCHEMA",
    "CLASSIFY_SYSTEM",
    "FOOT_SCHEMA",
    "FOOT_SYSTEM",
    "MASK_ASK",
    "MASK_SCHEMA",
    "NANO_ERASE_PROMPT",
    "NOT_STANDING",
    "PAIRWISE_SCHEMA",
    "PAIRWISE_SYSTEM",
    "STANDING_VIEWS",
    "classify_prompt",
    "twin_prompt",
]

# How the views are told apart (photos, cut-out shoes and the mask call).
_STANDING_VIEWS_TEXT = (
    "Views are defined by where the TOE points in the picture:\n"
    "- right: side profile, the toe points to the right edge of the "
    "picture, the heel to the left.\n"
    "- left: side profile, the toe points to the left edge, the heel to the"
    " right.\n"
    "- front: the toe points straight at the camera (head-on front, about "
    "eye level).\n"
    "- back: the heel points straight at the camera.\n"
    "- front_right: three-quarter between front and right: the toe points "
    "toward the camera and to the right; the front and the side are both "
    "visible.\n"
    "- front_left: the toe points toward the camera and to the left.\n"
    "- back_right: three-quarter between back and right: the toe points "
    "away from the camera and to the right; the heel and the side are both "
    "visible.\n"
    "- back_left: the toe points away from the camera and to the left.\n"
)
_TOP_FRONT_TEXT = (
    "- top_front: the camera is above the item looking down, from an "
    "elevated front view to straight overhead (the toe up or down in the "
    "picture); its top (upper, straps, laces, opening) dominates.\n"
)
_VIEWS_TEXT = _STANDING_VIEWS_TEXT + _TOP_FRONT_TEXT

# Labels every photo of a product (and later every piece cut from them).
CLASSIFY_SYSTEM = (
    "You are an expert in footwear photography. You receive ALL the photos "
    "of ONE footwear product (shoes, sandals, boots, slippers...), labelled"
    " IMAGE_0, IMAGE_1, ... Label EACH photo with one category, so the "
    "photos can be used as references for a 360-degree spin of ONE footwear"
    " item standing on its sole.\n"
    "\n"
    "First, is the photo usable?\n"
    "- invalid: no footwear (snowshoes, skis, poles or other gear that is "
    "not footwear; ski boots ARE footwear and are valid); a person wears "
    "the footwear; the footwear is in a scene or on a coloured or patterned"
    " background (plain white, off-white, beige, grey or black studio "
    "backgrounds are fine); no footwear item is complete: if EVERY footwear"
    " item in the picture is CUT by an edge of the picture (part of it lies"
    " outside the picture), the photo is invalid even when two or more "
    "items are visible (for instance a pair of sandals both cut on the "
    "sides). An item that only touches an edge but is whole is complete; "
    "altered or broken footwear (for instance a sole split into parts, or "
    "parts of another item attached to it); the sole dominates the picture "
    "(the footwear upside down or seen from below).\n"
    "- multiple_touching: two or more footwear items, at least ONE of them "
    "complete (entirely inside the picture, not cut by an edge, not hidden "
    "by the other), and the items TOUCH or OVERLAP: one is partly in front "
    "of or behind another, or they are in contact.\n"
    "- multiple_separate: two or more footwear items, at least ONE of them "
    "complete, and there is clear background between every pair of items: "
    "they do not touch and do not overlap anywhere.\n"
    "\n"
    "Otherwise the photo shows one complete footwear item: give its view. "
    + _VIEWS_TEXT
    + "\n"
    "Compare the photos of the set to tell close views apart. Return one "
    "entry per photo, in order."
)

CLASSIFY_LABELS = (
    "front",
    "front_right",
    "front_left",
    "top_front",
    "right",
    "left",
    "back",
    "back_right",
    "back_left",
    "invalid",
    "multiple_touching",
    "multiple_separate",
)

CLASSIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "labels": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "image": {"type": "INTEGER"},
                    "label": {"type": "STRING", "enum": list(CLASSIFY_LABELS)},
                },
                "required": ["image", "label"],
            },
        }
    },
    "required": ["labels"],
}

# The eight views of an item standing on its sole, in clockwise spin order.
STANDING_VIEWS = (
    "right",
    "front_right",
    "front",
    "front_left",
    "left",
    "back_left",
    "back",
    "back_right",
)

# Eval mode (frames of a spin video): the standing views, or not_standing;
# no usability labels (invalid, multiple...) and no top view.
NOT_STANDING = "not_standing"
_EVAL_SYSTEM = (
    "You are an expert in footwear photography. You receive ALL the photos "
    "of ONE footwear product (shoes, sandals, boots, slippers...), labelled"
    " IMAGE_0, IMAGE_1, ... Label EACH photo with the view of the footwear "
    "item.\n"
    "\n"
    "not_standing: the item is not standing on its sole (upside down, lying "
    "on its side, tipped over, or its sole facing the camera).\n"
    "\n"
    "Otherwise give its view. " + _STANDING_VIEWS_TEXT + "\n"
    "Compare the photos of the set to tell close views apart. Return one "
    "entry per photo, in order."
)
_EVAL_SCHEMA = json.loads(json.dumps(CLASSIFY_SCHEMA))
_EVAL_SCHEMA["properties"]["labels"]["items"]["properties"]["label"]["enum"] = [
    *STANDING_VIEWS,
    NOT_STANDING,
]


def classify_prompt(eval_mode: bool = False) -> tuple[str, dict]:
    """The labelling system prompt and schema; ``eval_mode`` (frames of a
    spin video) allows only the eight standing views and ``not_standing``."""
    if eval_mode:
        return _EVAL_SYSTEM, _EVAL_SCHEMA
    return CLASSIFY_SYSTEM, CLASSIFY_SCHEMA


# One segmentation mask per footwear item of a photo with several.
MASK_ASK = (
    "Give the segmentation masks for EVERY footwear item in the image (each"
    " shoe, sandal, boot), one entry per item, including items that are "
    "partly hidden or cut by the edges. Output a JSON list of segmentation "
    "masks where each entry contains the 2D bounding box in the key "
    '"box_2d", the segmentation mask in key "mask", the text label in the '
    'key "label", and in the key "complete" true if the item is entirely '
    "inside the picture (not cut by an edge and not hidden behind another "
    'item), else false, and in the key "view" the view of that item.\n'
    "view = invalid when the item is cut by an edge of the picture (part of"
    " it lies outside the picture; an item that only touches an edge but is"
    " whole is NOT invalid), is partly hidden behind another item, is "
    "altered or broken (for instance a sole split into parts, or parts of "
    "another item attached to it), shows mostly its sole (upside down or "
    "seen from below), or lies on its side (resting on its side, the sole "
    "standing upright instead of under the item; an item only tilted or "
    "propped up is NOT invalid).\n"
    "Otherwise give its view. "
    + _VIEWS_TEXT
    + "Use at most 30 points per polygon."
)

MASK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "boxes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "box_2d": {
                        "type": "ARRAY",
                        "items": {"type": "INTEGER"},
                        "description": (
                            "[ymin, xmin, ymax, xmax] normalized to 0-1000"
                        ),
                    },
                    "mask": {
                        "type": "ARRAY",
                        "items": {
                            "type": "ARRAY",
                            "items": {"type": "INTEGER"},
                        },
                        "description": (
                            "the segmentation mask of the item as a polygon"
                            " of [x,y] coordinates, normalized to 0-1000"
                        ),
                    },
                    "label": {"type": "STRING"},
                    "complete": {"type": "BOOLEAN"},
                    "view": {
                        "type": "STRING",
                        "enum": [
                            label
                            for label in CLASSIFY_LABELS
                            if not label.startswith("multiple")
                        ],
                    },
                },
                "required": ["box_2d", "mask", "label", "complete", "view"],
            },
        }
    },
    "required": ["boxes"],
}

# Which foot one shoe is made for.
FOOT_SYSTEM = (
    "The image shows ONE shoe, cut out from a product photo. Work out which"
    " FOOT it is made for (left or right), in three steps.\n"
    "1. toe: where the toe points in the picture: left, right, toward the "
    "camera, away from the camera, up, or down (seen from above or below).\n"
    "2. side: which side of the shoe faces the camera. OUTER (lateral) "
    "side: the more curved outline, usually where the logo, buckle, strap "
    "fastening or side branding sits, no arch. INNER (medial) side: the "
    "arch (the sole curves up under the middle), a straighter outline "
    "toward the big toe, usually plainer. Answer outer, inner, top (seen "
    "from above), sole (seen from below), or unclear.\n"
    "3. foot: the foot it is made for. Seen from the side, a shoe pointing "
    "LEFT shows its outer side if it is a LEFT shoe and its inner side if "
    "it is a RIGHT shoe; a shoe pointing RIGHT shows its outer side if it "
    "is a RIGHT shoe and its inner side if it is a LEFT shoe. Seen from "
    "above with the toe up, the big-toe (inner) edge is on the right for a "
    "LEFT shoe and on the left for a RIGHT shoe (reverse it when the toe "
    "points down). For three-quarter views, use the side you can see most. "
    "If you cannot tell, answer unsure. Keep the explanation to one or two "
    "short sentences."
)

FOOT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "explanation": {
            "type": "STRING",
            "description": (
                "one or two short sentences: the toe direction, the visible"
                " side and why"
            ),
        },
        "toe": {
            "type": "STRING",
            "enum": ["left", "right", "toward", "away", "up", "down"],
        },
        "side": {
            "type": "STRING",
            "enum": ["outer", "inner", "top", "sole", "unclear"],
        },
        "foot": {"type": "STRING", "enum": ["left", "right", "unsure"]},
    },
    "required": ["explanation", "toe", "side", "foot"],
}

# Whether two photos pointing the same way show the same side of the shoe.
PAIRWISE_SYSTEM = (
    "You receive two photos of the same shoe product, IMAGE_A and IMAGE_B. "
    "In both, the shoe points to the same side of the picture. Decide "
    "whether they show the SAME side of the shoe (the same face: same "
    "logos, text, buckles, panels and outline in the same places) or "
    "OPPOSITE sides (one shows the outer side and the other the inner side,"
    " i.e. one is effectively the other foot's shoe seen from the same "
    "direction). Compare concrete details: logos, text, buckles or straps, "
    "panels, stitching lines, the arch of the sole. Small changes of angle "
    "or zoom do not matter. Explain in one short sentence, then answer."
)

PAIRWISE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "explanation": {"type": "STRING", "description": "one short sentence"},
        "answer": {
            "type": "STRING",
            "enum": ["same_side", "opposite_sides", "unsure"],
        },
    },
    "required": ["explanation", "answer"],
}

# The image model erases the back shoe of a photo whose shoes touch/overlap;
# cleanly separated pairs are handled by the mask cuts instead.
NANO_ERASE_PROMPT = (
    "This studio product photo shows TWO shoes that overlap or touch each "
    "other. STEP 1 — Look carefully and determine exactly which pixels "
    "belong to each shoe. Identify which shoe is the FRONT one (nearest the"
    " camera — it sits on top and partially covers/occludes the other) and "
    "which is the BACK one (further from the camera, partially hidden "
    "behind the front shoe). Do NOT assume a side: the front shoe may be on"
    " the left or the right, and may be a front, side, or heel view.\n"
    "STEP 2 — Keep ONLY the FRONT shoe and completely remove the BACK shoe."
    " Erase every visible part of the back shoe, including any sliver "
    "poking out from behind the front shoe, and replace it — together with "
    "the region where the two shoes touch — with the same plain off-white "
    "studio background and soft studio shadow that surround the shoes.\n"
    "ABSOLUTE RULE — the FRONT shoe you keep must remain 100% "
    "PIXEL-IDENTICAL to the input. Do NOT move, rotate, flip, mirror, "
    "re-angle, rescale, recolor, relight, retouch, clean, sharpen, redraw, "
    "or change ANY pixel of it. Preserve its exact orientation, position, "
    "size, perspective, materials, texture, stitching, logos, hardware, "
    "laces, sole and its own cast shadow, EXACTLY as in the original. Never"
    " invent, complete, or hallucinate any part of the kept shoe — if the "
    "front shoe is itself partially occluded, leave it as-is, do not fill "
    "it in.\n"
    "Output the edited image at the same resolution: only the untouched "
    "front shoe on the plain studio background."
)


# Where the toe and the heel sit in each side view.
_TOE_HEEL = {
    "right": ("right edge", "left edge"),
    "left": ("left edge", "right edge"),
    "front_right": (
        "lower right, toward the camera",
        "upper left, away from the camera",
    ),
    "front_left": (
        "lower left, toward the camera",
        "upper right, away from the camera",
    ),
    "back_right": (
        "upper right, away from the camera",
        "lower left, toward the camera",
    ),
    "back_left": (
        "upper left, away from the camera",
        "lower right, toward the camera",
    ),
}


def twin_prompt(view: str, mirrored_view: str) -> str:
    """The image model's mirror twin of a shoe seen from ``view`` (a side
    view) that must come out as ``mirrored_view``. Naming where the toe and
    heel go is what stops it returning the input unchanged."""
    (toe_in, heel_in), (toe_out, heel_out) = (
        _TOE_HEEL[view],
        _TOE_HEEL[mirrored_view],
    )
    return (
        "Make the MIRROR TWIN of the shoe in this photo: the same shoe for "
        "the other foot, exactly as if the photo were flipped "
        "left-to-right.\n"
        f"In the input, the toe is at the {toe_in} and the heel at the "
        f"{heel_in}.\n"
        f"In your output, the toe must be at the {toe_out} and the heel at "
        f"the {heel_out}. Everything on the left of the input appears on the "
        "right of the output at the same height, and everything on the right"
        " appears on the left: the outline, the opening, the straps, seams "
        "and stitching, the shadow and the light.\n"
        "Keep the same camera angle, distance, height and framing, the same "
        "background, colours and materials; change nothing else.\n"
        "Text, lettering, numbers and logos with letters must still read "
        "correctly (never backwards), with the same font, size and colour, "
        "in their mirrored position on the shoe.\n"
        "Your output must NOT be the same as the input: if the toe is still "
        f"at the {toe_in}, the image is wrong."
    )
