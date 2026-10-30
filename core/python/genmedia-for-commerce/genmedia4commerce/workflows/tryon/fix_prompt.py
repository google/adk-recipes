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

"""The fix pass's prompt: a rejected try-on and the judge's defects (``fix``).

The images, named image_0.png, image_1.png, ... in the order attached: the
wearer's body and face as the drawing got them (the back view, drawn from
FRONT RESULT, gets the run's own), CURRENT RESULT (the rejected image),
then only the images of the products a defect names, as the drawing got
them (photos or card, crops, close-ups), each with its drawing label (other
images it names renamed). Each follows its label line. One instruction
text comes last: each defect by a short name (a product's feature, a
body part) with the images to follow, which show how; what touches a
corrected part adapts. The system text is the outfit drawing's image list
with the shared skin and build sentences.
"""

import re
from dataclasses import dataclass
from itertools import zip_longest

from genmedia4commerce.core.gen import Part
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.workflows.tryon import outfit_prompt, prompt, skin
from genmedia4commerce.workflows.tryon.styling import name
from genmedia4commerce.workflows.tryon.types import PromptProduct, PromptSpec

__all__ = ["CURRENT", "SYSTEM", "build_prompt"]

SYSTEM = (
    "You are an expert fashion photographer and virtual try-on specialist. "
    "You receive {n} labelled reference images ({first_to_last}) and produce "
    "ONE corrected photograph: CURRENT RESULT with only the listed defects "
    "corrected. When generating the image, you MUST explicitly reference "
    "all {n} input filenames in ascending order ({all}) so every reference "
    "image is used. " + skin.SKIN_STATE + " " + prompt._BUILD_STATE
)
CURRENT = (
    "CURRENT RESULT: the try-on to correct; everything not listed below "
    "stays as it is, except what touches a corrected part."
)
CORRECTION = (
    "## CORRECTION\n"
    "Images: {images}.\n"
    "A reviewer compared CURRENT RESULT with the references and found these "
    "defects:\n{defects}\n"
    "Fix only these defects in CURRENT RESULT, each as the image named "
    "next to it shows. Everything else stays as in CURRENT RESULT (pose, "
    "framing, background, light, every other product and everything else "
    "the wearer has on, accessories included), except what touches a "
    "corrected part, which adapts so the photo stays natural (the hair and "
    "a hat around a corrected face, the fabric around a corrected hem).\n"
    "In your generation description, explicitly state how each listed "
    "defect is corrected and which image it follows."
)
DEFECT = "- {text} — follow {images}."
# {others} in the system text when no product image is sent.
NO_PRODUCT_IMAGES = "the PRODUCT images"

_TAGS = {
    "body": "WEARER BODY",
    "face": "WEARER FACE",
    "current": "CURRENT RESULT",
}
_NAME = re.compile(r"image_\d+\.png|IMAGE \d+")  # an image named in a label


@dataclass
class _Image:
    """One image of the request: its role (body, face, current, product),
    the products it shows, label text, bytes, drawing name, name and what
    it is ('WEARER BODY', 'PRODUCT A field jacket')."""

    role: str
    ids: list[str]
    text: str
    image: Part
    drawn: str = ""  # its name in the drawing ('image_3.png', 'IMAGE 4')
    name: str = ""  # image_0.png, ...
    tag: str = ""


def _roles(spec: PromptSpec, n: int) -> list[tuple[str, list[str]]]:
    """(role, product ids) of the drawing's ``n`` images, in order
    (``build_prompt``'s layouts)."""
    ids = [p.id for p in spec.products]
    wearer: list[tuple[str, list[str]]] = [("body", [])]
    wearer += [("face", [])] * (spec.wearer_face is not None)
    if len(ids) > 1:  # outfit_prompt: body, face, products, crops
        own = [("product", [i]) for i in ids]
        return [*wearer, *own, *(("product", c) for _, c in spec.crops)]
    if spec.front_result is not None:  # the back view: FRONT RESULT last
        return [("product", ids)] * (n - 1) + [("front", [])]
    return [*wearer, *[("product", ids)] * (n - len(wearer))]


def _drawn(spec: PromptSpec, parts: list[Part]) -> list[_Image]:
    """The drawing's images, each with its role and label text."""
    labelled = []
    for k, part in enumerate(parts):
        if isinstance(part, str):
            continue
        label = parts[k - 1]
        if not isinstance(label, str):  # every image follows its label
            raise TypeError(f"image {k} of the drawing has no label")
        label = label.removeprefix(f"{prompt._INPUTS}\n")
        drawn, _, text = label.removeprefix("- ").partition(" — ")
        labelled.append((drawn, text, part))
    roles = _roles(spec, len(labelled))
    return [
        _Image(role, ids, text, image, drawn)
        for (role, ids), (drawn, text, image) in zip(
            roles, labelled, strict=True
        )
    ]


def _named(verdict: Verdict) -> list[str]:
    """The products a defect names, by id."""
    entries = verdict.details.get("products") or []
    return [e["product_id"] for e in entries if e.get("defects")]


def _wearer(spec: PromptSpec, drawn: list[_Image]) -> list[_Image]:
    """The body and face as the drawing got them, else the run's own."""
    sent = {d.role: d for d in drawn if d.role in ("body", "face")}
    images = [("body", prompt._WEARER_BODY, spec.wearer_body)]
    if spec.wearer_face is not None:
        images.append(("face", prompt._WEARER_FACE, spec.wearer_face))
    return [
        _Image(role, [], text, sent[role].image if role in sent else image)
        for role, text, image in images
    ]


def _images(
    spec: PromptSpec, parts: list[Part], current: bytes, named: list[str]
) -> list[_Image]:
    """The request's images, named; drawing names in the product labels
    become the request's (or the product's name when it is not sent)."""
    drawn = _drawn(spec, parts)
    products = [
        d for d in drawn if d.role == "product" and set(d.ids) & set(named)
    ]
    wearer = _wearer(spec, drawn)
    images = [*wearer, _Image("current", [], CURRENT, current), *products]
    by_id = {p.id: prompt._named(p) for p in spec.products}
    for k, image in enumerate(images):
        image.name = f"image_{k}.png"
        names = [by_id.get(i, f"PRODUCT {i}") for i in image.ids]
        image.tag = _TAGS.get(image.role) or " and ".join(names)
    renamed = {"body": wearer[0].name, "face": wearer[-1].name}
    to = {
        d.drawn: d.name or renamed.get(d.role) or by_id.get(d.ids[0], "")
        for d in drawn
        if d.role != "front"
    }
    for image in products:
        image.text = _NAME.sub(lambda m: to.get(m[0], m[0]), image.text)
    return images


def _title(product: PromptProduct) -> str:
    """'PRODUCT A (field jacket)'; 'PRODUCT A' without a name."""
    n = name(product)
    return f"PRODUCT {product.id}" + (f" ({n})" if n else "")


def _defects(
    verdict: Verdict, images: list[_Image], spec: PromptSpec
) -> list[str]:
    """Each defect by its short name (the body part, the product's
    feature; the judge's sentence when it has none) with the images to
    follow; the verdict's description when it lists none."""
    join = outfit_prompt._join
    wearer = join(
        [f"{i.name} ({i.tag})" for i in images if i.role in ("body", "face")]
    )
    body = zip_longest(
        verdict.details.get("body_defects") or [],
        verdict.details.get("body_parts") or [],
    )
    lines = [
        DEFECT.format(text=f"Body, {part}" if part else d, images=wearer)
        for d, part in body
        if d
    ]
    by_id = {p.id: prompt._named(p) for p in spec.products}
    titles = {p.id: _title(p) for p in spec.products}
    for entry in verdict.details.get("products") or []:
        pid = entry["product_id"]
        mine = [i.name for i in images if pid in i.ids]
        named = by_id.get(pid, f"PRODUCT {pid}")
        lines_above = (
            "its line above" if len(mine) == 1 else "their lines above"
        )
        follow = f"{join(mine)} ({named}) and {lines_above}" if mine else ""
        title = titles.get(pid, f"PRODUCT {pid}")
        for d in entry.get("defects") or []:
            text = f"{title}, {d.get('feature') or d.get('explanation')}"
            line = DEFECT.format(text=text, images=follow)
            lines.append(line if mine else f"- {text}.")
    return lines or [f"- {verdict.description.strip()}"]


def build_prompt(
    spec: PromptSpec, parts: list[Part], current: bytes, verdict: Verdict
) -> tuple[str, list[Part]]:
    """The fix request of ``current``, drawn from ``build_prompt(spec)``'s
    ``parts`` and rejected with ``verdict``: (system, parts)."""
    images = _images(spec, parts, current, _named(verdict))
    names = [i.name for i in images]
    body, face = images[0].name, images[1].name
    if images[1].role != "face":
        face = body
    shown = [i.name for i in images if i.role == "product"]
    others = outfit_prompt._join(shown) if shown else NO_PRODUCT_IMAGES
    system = SYSTEM.format(
        n=len(images),
        first_to_last=f"{names[0]} through {names[-1]}",
        all=", ".join(names),
        skin_from=outfit_prompt._join(list(dict.fromkeys([face, body]))),
        face=face,
        others=others,
        body=body,
    )
    request: list[Part] = []
    for k, image in enumerate(images):
        entry = outfit_prompt.ENTRY.format(image=image.name, text=image.text)
        request += [(f"{prompt._INPUTS}\n" if k == 0 else "") + entry]
        request.append(image.image)
    request.append(
        CORRECTION.format(
            images="; ".join(f"{i.name}: {i.tag}" for i in images),
            defects="\n".join(_defects(verdict, images, spec)),
        )
    )
    return system, request
