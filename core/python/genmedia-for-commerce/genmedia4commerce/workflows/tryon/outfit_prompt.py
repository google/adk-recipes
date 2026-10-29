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

"""An outfit's front drawing prompt: one image per product (``cards``).

The images, named image_0.png, image_1.png, ... in the order attached: the
wearer's body, the face, one image per product in product order (its card
when it has close-ups, else the product alone), then the look-photo crops.
Each follows its INPUTS entry. A product's entry says how it is worn
(``styling.instructions``, other products named by their image) and lists
its close-ups, coded A1, A2, ... after the crops that are not really closer
are dropped (``cards.useful``; a detail photo always stays), so the prompt
never names a dropped one.
FRAMING, RULES (naming the images), the brand styling rules and SCENE
follow. The shared sections come from ``prompt``, of which this is the
outfit half.
"""

import re
from dataclasses import dataclass

from PIL import Image

from genmedia4commerce.core.gen import Part
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import (
    cards,
    closeups,
    prompt,
    skin,
    styling,
)
from genmedia4commerce.workflows.tryon.types import (
    HEAD_FRAMINGS,
    PromptProduct,
    PromptSpec,
)

__all__ = ["SYSTEM", "build_prompt"]

SYSTEM = (
    "You are an expert fashion photographer and virtual try-on specialist. "
    "You receive {n} labelled reference images ({first_to_last}) and produce "
    "ONE photorealistic photograph of the WEARER wearing the PRODUCT(S) "
    "exactly as supplied, framed as instructed. When generating the image, "
    "you MUST explicitly reference all {n} input filenames in ascending "
    "order ({all}) so every reference image is used. "
    + skin.SKIN_STATE
    + " The wearer's identity and body come only from {wearer}; each "
    "product's appearance comes only from {products}; nothing else is "
    "invented. "
    + prompt._BUILD_STATE
    + " "
    + prompt._FRAMING_STATE
    + " "
    + prompt._POSE_STATE
    + " "
    + prompt._CHEST_STATE
)
ENTRY = "- {image} — {text}"
CARD = (
    "PRODUCT {id}{name} and its labelled detail close-up{s} beside it{how}. "
    "Detail{s} shown in {image}: {items}."
)
CARD_PLAIN = "PRODUCT {id}{name}{how}."
# A product whose own image shows a person (on it or cropped from a look),
# or a mannequin (a worn photo whose person became one, ``worn``).
SHOWN_ON_PERSON = ", shown on a different person: take " + skin.ONLY
SHOWN_ON_MANNEQUIN = ", shown on a mannequin: take " + skin.ONLY_MANNEQUIN
# The styling of a product whose own image is its crop of the look photo.
ON_PERSON = "worn exactly as in its photo on another person"
ON_MANNEQUIN = "worn exactly as in its photo on a mannequin"
# Before the FRAMING line of a 3:4 drawing (other ratios: none).
FRAMING_FULL = "Vertical 3:4 full-length portrait. "  # full_body
FRAMING_OTHER = "Vertical 3:4 portrait. "  # any other framing

RULE_BODY = (
    "Body. Keep {body} (WEARER BODY) exactly as it is: pose, proportions, "
    "height, hairstyle, " + prompt._SIZE + ", and "
    "everything that is part of the person or that they rely on (a "
    "prosthesis, a wheelchair or any other aid), including whatever is "
    "different about their body. "
    + prompt._NOT_SLIMMED
    + " FRAMING only changes the crop and, for "
    "head products, may tuck hair. Never add, remove or reshape any part of "
    "the body, or anything that is part of the person. Garments adapt to "
    "the body; the body never adapts to the garments: each garment falls on "
    "this exact body as it would in real life. It covers what its cut "
    "covers, the body's shape still showing under it, and it leaves bare "
    "what its cut leaves bare. Never reveal what a garment would cover."
)
RULE_FIDELITY = (
    "Product fidelity. Reproduce each product from {products}{closeups} "
    "exactly: colour, material, pattern, cut style, hem shape and every "
    "countable element (buttons, pockets, zips, logos, stitching lines, "
    "hardware). Do not add, remove, move or resize any of these design "
    "elements.{fit}{sentence}"
)
RULE_FIDELITY_CLOSEUPS = " and its detail close-ups from {details}"
RULE_FIDELITY_SENTENCE = (
    " Each detail close-up ({where}) is {source}: reproduce that detail "
    "exactly, at its true size, where it is visible on its product; never "
    "uncover it."
)
CLOSEUP_CROP = "an enlarged crop of its product photo"
CLOSEUP_PHOTO = "a close-up photo of its product"
RULE_INVENTED = (
    "No invented detail. Surfaces not visible in {images} stay plain, in the "
    "product's base colour and material. Never place a print, logo or text "
    "on a surface where the references do not show it. A garment's lining or "
    "inside colour, seen at its neckline or opening in the product photo, "
    "stays inside: never on its lapels, collar, front edges or outer surface."
)
RULE_MIRROR = (
    "No mirroring. Left stays left, right stays right. Any text reads "
    "correctly left-to-right."
)
RULE_ONLY = (
    "Only the supplied products. Every garment and accessory from {body} "
    "(WEARER BODY) is replaced: the wearer keeps none of their own clothes, "
    "scarves, jewellery, bags, belts or hats. Anything in frame that no "
    "product covers becomes a plain, neutral piece that goes with the "
    "products. No extra accessories, bags, belts, hats or jewellery, even "
    "if {body}, a product image or a crop shows them. Never copy any person, "
    "body or face from {images}."
)
RULE_COVERED = (
    "Covered parts stay covered: when one product covers part of another, "
    "reproduce only the visible part; never shorten, lift, open, tuck or "
    "move a product to show another."
)
RULE_OUTPUT = (
    "Output exactly one image of one person. No duplicates, split frames, "
    "collage borders, label codes, text or watermarks."
)

_PRODUCT_ID = re.compile(r"\(([A-Z])\)")  # '(A)' in a styling line


@dataclass
class _Product:
    """A product as drawn: its image cropped tight and its kept close-ups,
    each (code, detail, crop cropped tight)."""

    spec: PromptProduct
    image: Image.Image
    closeups: list[tuple[str, Detail, Image.Image]]


@dataclass
class _Sent:
    """One image attached: its kind (body, face, product or crop), bytes
    and name in the prompt; ``product`` for a product's image."""

    kind: str
    image: bytes
    product: _Product | None = None
    name: str = ""  # image_0.png, ...


def _join(names: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return (
        ", ".join(names[:-1]) + " and " + names[-1] if names[1:] else names[0]
    )


def _names(sent: list[_Sent], *kinds: str) -> list[str]:
    return [s.name for s in sent if s.kind in kinds]


def _product(p: PromptProduct, found: list[tuple[str, Detail]]) -> _Product:
    """``p`` with the close-ups its card keeps (``cards.useful``), coded
    in order. A detail photo is not a crop of the product photo: it always
    stays, like a closure's row, which is told by what the detail search
    said it is (its name and where it is), not by its shortened label."""
    image = cards.tight(p.refs[0].image)
    mine = [(d, cards.tight(d.image)) for pid, d in found if pid == p.id]
    labelled = [
        (closeups.label(f"{p.id}{k}", d), crop)
        for k, (d, crop) in enumerate(mine, 1)
    ]
    rows = [
        k
        for k, (d, _) in enumerate(mine)
        if d.kind == closeups.PHOTO or cards.ROW.search(f"{d.name} {d.where}")
    ]
    keep = cards.useful(image, labelled, rows)
    kept = [mine[k] for k in keep]
    return _Product(
        p, image, [(f"{p.id}{n}", d, c) for n, (d, c) in enumerate(kept, 1)]
    )


def _images(spec: PromptSpec, products: list[_Product]) -> list[_Sent]:
    """The images in the order attached, named image_0.png, ..."""
    sent = [_Sent("body", cards.single(cards.whole(spec.wearer_body)))]
    if spec.wearer_face is not None:
        face = cards.single(cards.tight(spec.wearer_face))
        sent.append(_Sent("face", face))
    for p in products:
        crops = [(closeups.label(c, d), img) for c, d, img in p.closeups]
        image = cards.card(p.image, crops) if crops else cards.single(p.image)
        sent.append(_Sent("product", image, p))
    for image, _ in spec.crops:
        sent.append(_Sent("crop", cards.single(cards.tight(image))))
    for k, s in enumerate(sent):
        s.name = f"image_{k}.png"
    return sent


def _product_entry(
    p: _Product, image: str, how: str, by_id: dict[str, str]
) -> str:
    """'PRODUCT B (carpenter trousers) and its labelled detail close-up
    beside it: ... Detail shown in image_3.png: B1 ...' (``image``: the
    name of ``p``'s image)."""
    worn = _PRODUCT_ID.sub(lambda m: f"({by_id.get(m[1], m[1])})", how)
    name = styling.name(p.spec)
    fields = {
        "id": p.spec.id,
        "name": f" ({name})" if name else "",
        "how": f": {worn}" if worn else "",
    }
    ref = p.spec.refs[0]
    if styling.ref_label(ref) != "FLAT":  # shows a person or a mannequin
        on = ON_MANNEQUIN if ref.mannequin else ON_PERSON
        rest = worn.removeprefix(on).removeprefix(": ")
        rest = re.sub(r"^worn exactly as in ", "worn as in ", rest)
        shown = SHOWN_ON_MANNEQUIN if ref.mannequin else SHOWN_ON_PERSON
        shown = shown.format(what=name or "product")
        fields["how"] = shown + (f"; {rest}" if rest else "")
    if not p.closeups:
        return CARD_PLAIN.format(**fields)
    items = "; ".join(closeups.item(code, d) for code, d, _ in p.closeups)
    plural = "s" if len(p.closeups) > 1 else ""
    return CARD.format(**fields, s=plural, image=image, items=items)


def _entries(spec: PromptSpec, sent: list[_Sent]) -> list[str]:
    """Each image's INPUTS entry; a crop keeps the single route's line."""
    how = styling.instructions(spec)
    by_id = {s.product.spec.id: s.name for s in sent if s.product}
    named = {p.id: prompt._named(p) for p in spec.products}
    crops = iter(text for text, _ in prompt._crop_lines(spec, named))
    texts = {"body": prompt._WEARER_BODY, "face": prompt._WEARER_FACE}
    entries = []
    for s in sent:
        if s.product is not None:
            text = _product_entry(
                s.product, s.name, how[s.product.spec.id], by_id
            )
            if s.product.spec.category in prompt.GARMENTS:
                text += prompt._GARMENT_FIT.format(body=sent[0].name)
        else:
            text = next(crops) if s.kind == "crop" else texts[s.kind]
        entries.append(ENTRY.format(image=s.name, text=text))
    return entries


def _fidelity(sent: list[_Sent], shown: str) -> str:
    """The product fidelity rule, naming the close-ups and their cards;
    the garments re-graded to the wearer's body when there are any."""
    garments = [
        s.product.spec.category in prompt.GARMENTS for s in sent if s.product
    ]
    fit = prompt._REGRADED.format(body=sent[0].name) if any(garments) else ""
    on_cards = [
        (s.name, s.product) for s in sent if s.product and s.product.closeups
    ]
    if not on_cards:
        return RULE_FIDELITY.format(
            products=shown, closeups="", fit=fit, sentence=""
        )
    where = "; ".join(
        f"{', '.join(c for c, _, _ in p.closeups)} in {image}"
        for image, p in on_cards
    )
    photo = [
        d.kind == closeups.PHOTO for _, p in on_cards for _, d, _ in p.closeups
    ]
    nouns = [CLOSEUP_CROP] * (not all(photo)) + [CLOSEUP_PHOTO] * any(photo)
    source = " or ".join(nouns)
    details = _join([image for image, _ in on_cards])
    return RULE_FIDELITY.format(
        products=shown,
        closeups=RULE_FIDELITY_CLOSEUPS.format(details=details),
        fit=fit,
        sentence=RULE_FIDELITY_SENTENCE.format(where=where, source=source),
    )


def _rules(spec: PromptSpec, sent: list[_Sent]) -> str:
    body = sent[0].name
    face = next(iter(_names(sent, "face")), None)
    shown = _join(_names(sent, "product"))
    rules = []
    if spec.framing in HEAD_FRAMINGS:
        rules.append(
            skin.IDENTITY.format(
                face=face or body, body=f"{body} (WEARER BODY)"
            )
        )
    rules += [
        RULE_BODY.format(body=body),
        _fidelity(sent, shown),
        RULE_INVENTED.format(images=shown),
        RULE_MIRROR,
        RULE_ONLY.format(body=body, images=shown),
        prompt._NOT_BARE,
        RULE_COVERED,
        prompt._photo_rule(spec),
        RULE_OUTPUT,
    ]
    return "## RULES\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))


def _framing(spec: PromptSpec) -> str:
    """The single route's FRAMING after the portrait's shape (a 3:4
    drawing only), then plain shoes when they apply (the line after the
    products in PRODUCTS)."""
    head = "## FRAMING\n"
    text = prompt._framing(spec).removeprefix(head)
    shape = FRAMING_FULL if spec.framing == "full_body" else FRAMING_OTHER
    shape = shape if spec.aspect_ratio == "3:4" else ""
    extra = styling.product_lines(spec)[len(spec.products) :]
    return "\n".join([head + shape + text, *extra])


def build_prompt(spec: PromptSpec) -> tuple[str, list[Part]]:
    """Return the system prompt and the ordered user message: each INPUTS
    entry with its image, then FRAMING, RULES, the brand styling rules and
    SCENE as one text part."""
    products = [_product(p, spec.closeups) for p in spec.products]
    sent = _images(spec, products)
    face = next(iter(_names(sent, "face")), sent[0].name)
    system = SYSTEM.format(
        n=len(sent),
        first_to_last=f"{sent[0].name} through {sent[-1].name}",
        all=", ".join(s.name for s in sent),
        body=sent[0].name,
        skin_from=_join(list(dict.fromkeys([face, sent[0].name]))),
        face=face,
        wearer=_join(_names(sent, "body", "face")),
        products=_join(_names(sent, "product")),
        others=_join([s.name for s in sent if s.kind not in ("body", "face")]),
    )
    parts: list[Part] = []
    for k, (entry, s) in enumerate(
        zip(_entries(spec, sent), sent, strict=True)
    ):
        parts += [(f"{prompt._INPUTS}\n" if k == 0 else "") + entry, s.image]
    sections = [_framing(spec), _rules(spec, sent), *prompt._closing(spec)]
    parts.append("\n\n".join(sections))
    return system, parts
