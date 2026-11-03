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

"""The composite try-on prompt (plan Appendix A).

``build_prompt`` writes every try-on prompt. A single product (and its back
view) gets the layout here: each image after its INPUTS line, then
PRODUCTS, FRAMING, RULES, the brand styling rules and SCENE. The back view
sends no WEARER image and no REFERENCE CROP: FRONT RESULT, last, is the
source of the wearer, and the system text and rules that named the WEARER
images name it. An outfit's
front drawing gets one image per product (``outfit_prompt``), with the same
FRAMING, photograph rule, brand styling rules and SCENE. Empty slots are
omitted. Every image line names its products, and how each product is worn
is resolved in code (``styling``), like a single garment's FRAMING
(``framing``, from where it starts and ends, the same for its back view):
the prompt carries no rule for the model to work out. Every system text
ends asking the model to state the framing.
"""

from genmedia4commerce.core.gen import Part
from genmedia4commerce.core.types import GARMENTS, Category, Framing, ProductRef
from genmedia4commerce.workflows.tryon import closeups, framing, skin, styling
from genmedia4commerce.workflows.tryon.styling import kind, name
from genmedia4commerce.workflows.tryon.types import PromptProduct, PromptSpec

__all__ = [
    "BACK_SYSTEM",
    "CATEGORY_NOTES",
    "FRAMING",
    "SYSTEM",
    "PromptProduct",
    "PromptSpec",
    "build_prompt",
]

# Re-grading changes the size, never the cut.
_CUT = (
    "the cut stays as in its product photo: a wide or relaxed leg or sleeve "
    "stays as loose around this body, a slim one stays slim"
)
# The wearer's build, after the system text: {body} is its source, {others}
# the images of other people.
_BUILD_STATE = (
    "In your generation description, you MUST also include a dedicated "
    "sentence that names the wearer's actual physical build from {body} in "
    "concrete visual terms (for example, if the wearer in {body} is "
    "plus-size, heavy-set or full-figured, explicitly describe their wide "
    "shoulders, full chest and belly, wide hips, wide seat and thick full "
    "thighs with no gap between the inner thighs; if the wearer is slim or "
    "average, describe that exact slim or average build) and states that "
    "every garment, especially the waistband, hip area, coat opening and "
    "trouser leg tubes, is graded to that exact body width and inner-thigh "
    "spacing, never copying the narrow hips, slim trouser legs or thigh gap "
    "of the people shown in {others}; " + _CUT + "."
)
# Last in every system text: the model states the frame's edges.
_FRAMING_STATE = (
    "In your generation description, you MUST state the framing: where the "
    "top and the bottom edge of the photo fall, exactly as the FRAMING "
    "section says."
)
# Front views only (the back view has no WEARER BODY image).
_POSE_STATE = (
    "In your generation description, you MUST state the wearer's pose from "
    "the WEARER BODY image (standing or seated, and what they sit on) and "
    "keep it."
)
# Front views: names what covers the chest (the no-bare-chest rule alone let
# straps count as covering it).
_CHEST_STATE = (
    "In your generation description, you MUST name what covers the wearer's "
    "chest: a product, or the plain top."
)
# After a garment's INPUTS line ({body}: the wearer's body).
_GARMENT_FIT = (
    " Its size follows the wearer's real body in {body}, never the build of "
    "the person in its photo; " + _CUT + "."
)
# The body rule's size and its last word on it.
_SIZE = (
    "body shape and real physical weight/size (whether plus-size, "
    "full-figured, heavy-set, pregnant, average or slim), including the "
    "exact horizontal width of the shoulders, chest, belly, waist, hips, seat "
    "and thighs, the exact spacing between the inner thighs (if the thighs "
    "touch with no gap in {body}, they touch with no gap in the output)"
)
_NOT_SLIMMED = (
    "Never slim, narrow, flatten, enlarge or idealize any part of the body: "
    "each garment is re-graded to the wearer's own size, and their real "
    "belly, hips and thighs stay as they are under it."
)
# After the product fidelity rule, when a garment is among the products.
_REGRADED = (
    " The garment size and fit are the wearer's: waist, belly, hips, seat, "
    "chest, coat opening, sleeve width and trouser-leg width are re-graded "
    "to fit the exact body mass and thigh thickness in {body}, never kept at "
    "the catalogue model's width or thigh gap; " + _CUT + "."
)

SYSTEM = (
    "You are an expert fashion photographer and virtual try-on specialist. "
    "You receive labelled reference images and produce ONE photorealistic "
    "photograph of the WEARER wearing the PRODUCT(S) exactly as supplied, "
    "framed as instructed. "
    + skin.SKIN_STATE.format(
        skin_from="the WEARER images",
        face="the skin of the WEARER images",
        others="the PRODUCT images",
    )
    + " The wearer's identity and body come only from the "
    "WEARER images; each product's appearance comes only from its PRODUCT "
    "images; nothing else is invented. "
    + _BUILD_STATE.format(body="the WEARER images", others="the PRODUCT images")
    + " "
    + _FRAMING_STATE
    + " "
    + _POSE_STATE
    + " "
    + _CHEST_STATE
)

BACK_SYSTEM = (
    "You are an expert fashion photographer and virtual try-on specialist. "
    "You receive labelled reference images and produce ONE photorealistic "
    "photograph of the wearer of FRONT RESULT wearing the PRODUCT exactly as "
    "supplied, seen from the back, framed as instructed. "
    + skin.SKIN_STATE.format(
        skin_from="FRONT RESULT",
        face="FRONT RESULT",
        others="the PRODUCT images",
    )
    + " The wearer's "
    "identity and body come only from FRONT RESULT; each product's "
    "appearance comes only from its PRODUCT images; nothing else is invented. "
    + _BUILD_STATE.format(body="FRONT RESULT", others="the PRODUCT images")
    + " "
    + _FRAMING_STATE
)

FRAMING: dict[Framing, str] = {
    "head": "Head-and-shoulders portrait, from the top of the head to the chest, camera slightly above eye level. The whole face is visible. Hair keeps its style; if it hides the product, tuck it behind the ear or shoulder, nothing more.",
    "upper_body": "From the top of the head to the hips. Arms and hands visible, relaxed. The product is fully in frame.",
    "lower_body": "From the waist to the ground, shoes included. The product is fully in frame.",
    "footwear": "From the knees to the ground, camera low and angled slightly down at the feet. Both shoes fully visible in a natural stance; nothing above the knees.",
    "full_body": "The entire person from above the head to below the feet, shoes visible, balanced space on both sides. Never crop at the knees, waist or ankles.",
    "hands": "Hands and wrists close-up, in a natural relaxed pose, the product in sharp focus and fully visible. Nothing above the forearms.",
}

CATEGORY_NOTES: dict[Category, str] = {
    "eyewear": "Lenses keep the reference's tint (clear lenses stay clear); the eyes remain visible through them. Frames sit naturally on the nose and ears.",
    "jewelry_head": "Both ears visible; tuck hair behind the ears if needed, keeping the hairstyle.",
    "headwear": "The hat sits naturally over the existing hairstyle.",
    "jewelry_neck": "Neckline and collarbones visible so the piece sits as it would when worn.",
    "footwear": "Trousers, if any, do not cover the shoe design; socks only if the reference shows them.",
}

_ANGLE_RULE = (
    "Match the camera angle of PRODUCT A's primary reference exactly: "
    "straight-on if it is straight-on, 3/4 if it is 3/4. Never rotate or redraw "
    "the product to fit the wearer; turn the wearer to fit the product."
)

_NEVER_COPY = "Never copy any person, body or face from it."

_INPUTS = "## INPUTS (in the order attached)"
_WEARER_BODY = (
    "WEARER BODY: source of pose, body shape and size, hair and skin tone."
)
_WEARER_FACE = (
    "WEARER FACE: authoritative for facial identity, features, skin texture, "
    "and the exact skin tone, shade and brightness."
)

_FRONT_RESULT = (
    "FRONT RESULT: the wearer already wearing the product, seen from the "
    "front; the source of the wearer: identity, body, hair, the pose (turned "
    "around), everything else they wear and the setting. Produce the "
    "matching BACK view of the same wearer, same product, same setting; "
    "everything that is identical from both sides must stay identical."
)

_FIDELITY = "Product fidelity. Reproduce each product exactly: colour, material, pattern, cut style, hem shape and every countable element (buttons, pockets, zips, logos, stitching lines, hardware). Do not add, remove, move or resize any of these design elements."
_NOT_INVENTED = "No invented detail. Surfaces not visible in the references stay plain, in the product's base colour and material. Never place a print, logo or text on a surface where the references do not show it."
_NOT_MIRRORED = "No mirroring. Left stays left, right stays right. Any text reads correctly."
_COVERED = "Covered parts stay covered: when one product covers part of another (boot shafts under a trouser hem, a shirt under a jacket), reproduce only the visible part; never shorten, lift, open, tuck or move a product to show another."
# After the rule on what else the wearer wears, in every route.
_NOT_BARE = (
    "No bare chest or breasts and no underwear on show: where neither a "
    "product nor anything else the wearer wears covers the chest, the "
    "wearer wears a plain top, and where nothing covers the hips, a plain "
    "bottom, each in a neutral colour that suits the outfit."
)

# Product fidelity (``_rules``) follows each list's body rule.
_RULES = [
    "Body. Keep WEARER BODY exactly as it is: pose, proportions, height, hairstyle, "
    + _SIZE.format(body="WEARER BODY")
    + ", and everything that is part of the person or that they rely on (a prosthesis, a wheelchair or any other aid), including whatever is different about their body. "
    + _NOT_SLIMMED
    + " FRAMING only changes the crop and, for head products, may tuck hair. Never add, remove or reshape any part of the body, or anything that is part of the person. Garments adapt to the body; the body never adapts to the garments: each garment falls on this exact body as it would in real life. It covers what its cut covers, the body's shape still showing under it, and it leaves bare what its cut leaves bare. Never reveal what a garment would cover.",
    _NOT_INVENTED,
    _NOT_MIRRORED,
    "Only the supplied products. Every garment and accessory from WEARER BODY is replaced: the wearer keeps none of their own clothes, scarves, jewellery, bags, belts or hats. Anything in frame that no product covers becomes a plain, neutral piece that goes with the products (for example plain trousers under a supplied top). No extra accessories, bags, belts, hats or jewellery, even if WEARER BODY, a product photo on a person or a REFERENCE CROP shows them.",
    _NOT_BARE,
    _COVERED,
]

# The back view (no WEARER image, no REFERENCE CROP): the rules that named
# WEARER BODY name FRONT RESULT.
_BACK_RULES = [
    "Body. Keep FRONT RESULT's person exactly as it is, with its pose turned around: proportions, height, hairstyle, "
    + _SIZE.format(body="FRONT RESULT")
    + ", and everything that is part of the person or that they rely on (a prosthesis, a wheelchair or any other aid), including whatever is different about their body. "
    + _NOT_SLIMMED
    + " FRAMING only changes the crop and, for head products, may tuck hair. Never add, remove or reshape any part of the body, or anything that is part of the person. Garments adapt to the body; the body never adapts to the garments: each garment falls on this exact body as it would in real life. It covers what its cut covers, the body's shape still showing under it, and it leaves bare what its cut leaves bare. Never reveal what a garment would cover.",
    _NOT_INVENTED,
    _NOT_MIRRORED,
    "Only the supplied products. Everything else the wearer wears comes from FRONT RESULT: anything in frame that no product covers is the plain, neutral piece FRONT RESULT shows, seen from the back. No extra accessories, bags, belts, hats or jewellery, even if a product photo on a person shows them.",
    _NOT_BARE,
    _COVERED,
]
_BACK_IDENTITY = "Identity. The wearer is the person in FRONT RESULT: the same features, skin tone, hairstyle, hair colour and length. Do not beautify, age, slim or reshape the wearer."

_OUTPUT_RULE = "Output exactly one image of one person. No duplicates, split frames, text or watermarks."


def _named(product: PromptProduct) -> str:
    """'PRODUCT B carpenter trousers' ('PRODUCT B' without a name)."""
    return " ".join(x for x in (f"PRODUCT {product.id}", name(product)) if x)


def _photo(ref: ProductRef) -> str:
    """'photo on a person, front view'."""
    view = f", {ref.view} view" if ref.view in ("front", "back") else ""
    return kind(ref) + view


def _fit(spec: PromptSpec, text: str) -> str:
    """``text`` naming the wearer's body (FRONT RESULT for the back view)
    when the single product is a garment, else ''."""
    if spec.products[0].category not in GARMENTS:
        return ""
    back = spec.front_result is not None
    return text.format(body="FRONT RESULT" if back else "WEARER BODY")


def _product_lines(spec: PromptSpec) -> list[tuple[str, bytes]]:
    product = spec.products[0]
    primary, *secondary = product.refs
    never = _NEVER_COPY
    if styling.ref_label(primary) != "FLAT":  # a photo on a person
        only = skin.ONLY_MANNEQUIN if primary.mannequin else skin.ONLY
        never = f"Take {only.format(what=name(product) or 'product')}."
    lines = [
        (
            f"{_named(product)} ({_photo(primary)}): shows how the product "
            f"looks and sets the camera angle. {never}"
            + _fit(spec, _GARMENT_FIT),
            primary.image,
        )
    ]
    lines += [
        (
            f"{_named(product)}, secondary reference ({_photo(r)}): use only "
            "to confirm details not visible in the primary. Never use it for "
            f"pose or angle. {_NEVER_COPY}",
            r.image,
        )
        for r in secondary
    ]
    return lines


def _crop_lines(
    spec: PromptSpec, named: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One REFERENCE CROP line per crop, naming the products it shows; a
    crop of a photo on a mannequin says so."""
    on = [any(c is m for m in spec.mannequin_crops) for c, _ in spec.crops]
    return [
        (
            "REFERENCE CROP: "
            + " and ".join(named.get(i, f"PRODUCT {i}") for i in ids)
            + (skin.CROP_MANNEQUIN if mannequin else skin.CROP),
            image,
        )
        for (image, ids), mannequin in zip(spec.crops, on, strict=True)
    ]


def _inputs(spec: PromptSpec) -> list[tuple[str, bytes]]:
    """The INPUTS lines with their images; the back view's wearer is FRONT
    RESULT, last, with no WEARER image."""
    back = spec.front_result is not None
    lines = [] if back else [(_WEARER_BODY, spec.wearer_body)]
    if not back and spec.wearer_face is not None:
        lines.append((_WEARER_FACE, spec.wearer_face))
    named = {p.id: _named(p) for p in spec.products}
    lines += _product_lines(spec) + _crop_lines(spec, named)
    lines += closeups.lines(spec.closeups, named)
    if spec.front_result is not None:
        lines.append((_FRONT_RESULT, spec.front_result))
    return [
        (f"- IMAGE {i} — {text}", img) for i, (text, img) in enumerate(lines, 1)
    ]


def _products(spec: PromptSpec) -> str:
    return "\n".join(["## PRODUCTS", *styling.product_lines(spec)])


def _framing(spec: PromptSpec) -> str:
    """A single garment's framing from its span, else the fixed one."""
    framed = framing.text(spec.span) if spec.span else FRAMING[spec.framing]
    lines = ["## FRAMING", framed]
    view = f"Show the wearer from the {spec.view}."
    if len(spec.products) == 1:
        note = CATEGORY_NOTES.get(spec.products[0].category)
        lines += [note] if note else []
        view += " " + _ANGLE_RULE
    return "\n".join([*lines, view])


def _photo_rule(spec: PromptSpec) -> str:
    shadows = "contact shadows where fabric meets the body"
    if framing.shows_feet(spec):  # ground shadow
        shadows = (
            "a natural soft shadow on the ground under and beside the feet "
            f"and {shadows}"
        )
    return (
        "One photograph, not a composite: the wearer is photographed in the "
        "scene, not pasted into it. One subject, centred, soft studio "
        "lighting on face, body and products, falling naturally on the "
        f"person and the background; {shadows}. Edges blend naturally into "
        "the background, with no halo, hard outline or cut-out look. Clean "
        "input noise without touching the face or body."
    )


def _identity(spec: PromptSpec) -> list[str]:
    """Identity only where the face shows."""
    if not framing.shows_head(spec):
        return []
    if spec.front_result is not None:
        return [_BACK_IDENTITY]
    source = "WEARER FACE" if spec.wearer_face is not None else "WEARER BODY"
    return [skin.IDENTITY.format(face=source, body="WEARER BODY")]


def _rules(spec: PromptSpec) -> str:
    """The front's rules, or the back view's (its wearer is FRONT RESULT)."""
    back = spec.front_result is not None
    body, *rest = _BACK_RULES if back else _RULES
    fidelity = _FIDELITY + _fit(spec, _REGRADED)
    rules = [*_identity(spec), body, fidelity, *rest]
    rules += [_photo_rule(spec), _OUTPUT_RULE]
    return "## RULES\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))


def _closing(spec: PromptSpec) -> list[str]:
    """The sections after RULES: the brand styling rules, SCENE and the
    request."""
    guidelines = (spec.style_guidelines or "").strip()
    brand = [f"Brand styling rules: {guidelines}"] if guidelines else []
    return [*brand, f"## SCENE\n{spec.scenario}", "Generate the image."]


def build_prompt(spec: PromptSpec) -> tuple[str, list[Part]]:
    """Return the system prompt and the ordered user message.

    The message interleaves each INPUTS line with its image, then PRODUCTS,
    FRAMING, RULES, the brand styling rules and SCENE as one text part. An
    outfit's front drawing: ``outfit_prompt.build_prompt``.
    """
    if len(spec.products) > 1:
        # Deferred: outfit_prompt uses this module's sections.
        from genmedia4commerce.workflows.tryon import outfit_prompt

        return outfit_prompt.build_prompt(spec)
    parts: list[Part] = []
    for i, (line, image) in enumerate(_inputs(spec)):
        parts += [(f"{_INPUTS}\n" if i == 0 else "") + line, image]
    sections = [_products(spec), _framing(spec), _rules(spec), *_closing(spec)]
    parts.append("\n\n".join(sections))
    return (SYSTEM if spec.front_result is None else BACK_SYSTEM), parts
