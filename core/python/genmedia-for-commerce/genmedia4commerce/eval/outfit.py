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

"""One-call outfit judge: every product photo and the try-on image at once.

One judge call lists each product's features, compares them with the
generated image and decides whether it can be published (no boxes, no
second check, no combining step). Each product photo is labelled with its
category and, when the styling plan wears the product partly covered, the
part meant to be visible.

Each defect has a category. A ``MINOR`` category (``fabric_texture``: only
the knit, weave or surface texture differs, colour and pattern right) lowers
the score but never rejects: an answer rejecting with minor defects only is
published (``_settle``).

With a WEARER image (the person before the try-on) the judge also checks
the body against it, and the face against it and the FACE crop when there
is one (``WEARER_NOTE``): any body defect, a face that is not the
wearer's among them, rejects and the description starts with
``BODY_NOTE``. Each body defect comes with a short name of the body part
or feature it concerns (``body_parts``, aligned one-to-one).
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval.garment_prompts import PRODUCT_FILTER_HINTS

__all__ = ["BODY_NOTE", "MINOR", "OutfitProduct", "evaluate_outfit"]

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


OUTFIT_SYSTEM = _prompt("outfit_judge")
# Added for a video frame: blur, small text and limb poses are tolerated.
VIDEO_FRAME_NOTE = _prompt("outfit_video_frame")
# Added with a WEARER image: keep the person's body and face as they are.
WEARER_NOTE = _prompt("outfit_wearer")
WEARER_LABEL = "WEARER (the person before the try-on, in their own clothes):"
FACE_LABEL = "FACE (a close crop of the WEARER's face, from the same photo):"
THINKING = "MEDIUM"
# A normal answer takes 10-40 s; a call that stalls past it is asked again
# once by ``core.gen``.
DEADLINE_S = 90
# A video frame takes 8-18 s: a stalled call and its retry fit in the
# video check's cap (``video_tryon.checks.JUDGE_CAP_S``).
VIDEO_DEADLINE_S = 50
MAX_OUTPUT_TOKENS = 8192  # a verdict uses under 1k tokens with thinking

_CATEGORIES = [
    "silhouette",
    "fabric_color",
    "fabric_texture",
    "identity_text",
    "embellishment",
    "structural",
    "hardware",
    "wearing",
    "other",
]
_DEFECT = {
    "type": "OBJECT",
    "properties": {
        "feature": {"type": "STRING"},
        "category": {"type": "STRING", "enum": _CATEGORIES},
        "kind": {
            "type": "STRING",
            "enum": ["missing", "extra", "differs", "wearing"],
        },
        "explanation": {"type": "STRING"},
    },
    "required": ["feature", "category", "kind", "explanation"],
}
OUTFIT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "products": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "product": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "defects": {"type": "ARRAY", "items": _DEFECT},
                },
                "required": ["product", "name", "defects"],
            },
        },
        "publish": {"type": "BOOLEAN"},
        "imperfection_count": {"type": "INTEGER"},
        "description": {"type": "STRING"},
    },
    "required": ["products", "publish", "imperfection_count", "description"],
}
# With a WEARER image: the body defects too, each with the body part it
# concerns at the same position (``WEARER_NOTE``).
WEARER_SCHEMA = {
    **OUTFIT_SCHEMA,
    "properties": {
        **OUTFIT_SCHEMA["properties"],
        "body_defects": {"type": "ARRAY", "items": {"type": "STRING"}},
        "body_parts": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [*OUTFIT_SCHEMA["required"], "body_defects", "body_parts"],
}
BODY_NOTE = "Body defect:"
# Defect categories that lower the score but never reject on their own.
MINOR = frozenset({"fabric_texture"})
MINOR_NOTE = "Published: only minor differences (fabric texture), scored lower."


def _is_minor(defect: dict) -> bool:
    """Whether a defect lowers the score without rejecting (``MINOR``)."""
    return defect.get("category") in MINOR


@dataclass(frozen=True)
class OutfitProduct:
    """One product to judge: its photo, category and visible part."""

    reference: bytes
    product_filter: str | None = None
    visibility: str | None = None


def _label(k: int, product: OutfitProduct) -> str:
    """``PRODUCT_k (<category hint>)``, plus the visible part if covered."""
    focus = PRODUCT_FILTER_HINTS.get(
        product.product_filter, product.product_filter
    )
    label = f"PRODUCT_{k}" + (f" ({focus})" if focus else "")
    if product.visibility:
        label += (
            " - worn under other garments by design: only "
            f"{product.visibility} is meant to be visible; judge that part "
            "and treat the covered rest as occluded"
        )
    return label + ":"


def _parts(
    products: Sequence[OutfitProduct],
    target: bytes,
    video_frame: bool,
    wearer: bytes | None,
    face: bytes | None,
) -> list:
    """Each product, the TARGET, then the notes and the WEARER with its
    FACE (after the TARGET, so the products are compared with it as without
    a WEARER)."""
    parts: list = []
    for k, product in enumerate(products, start=1):
        parts += [_label(k, product), media.to_png(product.reference)]
    parts += ["TARGET (the generated image):", media.to_png(target)]
    if video_frame:
        parts.append(VIDEO_FRAME_NOTE)
    if wearer is not None:
        parts += [WEARER_LABEL, media.to_png(wearer)]
        if face is not None:
            parts += [FACE_LABEL, media.to_png(face)]
        parts.append(WEARER_NOTE)
    return [*parts, "Judge the TARGET and return the JSON object."]


def _index(entry: dict, fallback: int) -> int:
    """The 1-based product number of an answer entry (``PRODUCT_k``)."""
    match = re.search(r"\d+", str(entry.get("product") or ""))
    return int(match.group()) if match else fallback


def _finding(defect: dict) -> str:
    return (
        f"{defect.get('category')}: {defect.get('feature')} — "
        f"{defect.get('explanation')}"
    )


def _body(answer: dict) -> tuple[list[str], list[str]]:
    """The answer's body defects and, aligned, their body parts ('' when
    the answer names none)."""
    pairs = zip_longest(
        answer.get("body_defects") or [],
        answer.get("body_parts") or [],
        fillvalue="",
    )
    body = [(str(d).strip(" ."), str(p or "").strip(" .")) for d, p in pairs]
    kept = [(d, p) for d, p in body if d]
    return [d for d, _ in kept], [p for _, p in kept]


def _details(answer: dict) -> dict:
    """``{"products": [{index, name, defects, findings, minor}],
    "body_defects": [...], "body_parts": [...]}`` from the answer;
    ``minor``: the findings of its ``MINOR`` defects."""
    body, parts = _body(answer)
    products = []
    for n, entry in enumerate(answer.get("products") or [], start=1):
        defects = [d for d in entry.get("defects") or [] if isinstance(d, dict)]
        products.append(
            {
                "index": _index(entry, n),
                "name": str(entry.get("name") or ""),
                "defects": defects,
                "findings": [_finding(d) for d in defects],
                "minor": [_finding(d) for d in defects if _is_minor(d)],
            }
        )
    return {"products": products, "body_defects": body, "body_parts": parts}


def _settle(answer: dict, details: dict) -> tuple[bool, int, str]:
    """(publish, imperfection_count, description) of the answer.

    A rejection whose defects are all minor (at least one) is published.
    Minor defects lower the score: the count is at least their number.
    Any body defect (``body_defects``, only asked with a WEARER image)
    rejects, the description starting with ``BODY_NOTE`` and them.
    """
    publish = bool(answer.get("publish"))
    description = str(answer.get("description") or "")
    products = details["products"]
    n_minor = sum(len(p["minor"]) for p in products)
    only_minor = n_minor > 0 and all(
        len(p["findings"]) == len(p["minor"]) for p in products
    )
    body = details["body_defects"]
    count = max(int(answer.get("imperfection_count") or 0), n_minor)
    if body:
        publish = False
        description = f"{BODY_NOTE} {'; '.join(body)}. {description}".strip()
    elif not publish and only_minor:
        publish, description = True, f"{description} {MINOR_NOTE}".strip()
    return publish, count, description


def _ask_twice(parts: list, deadline_s: float, schema: dict) -> dict:
    """``_ask``, once more when it fails: a judge that times out under load
    must not throw away a good image. The second call has its own
    deadline."""
    try:
        return _ask(parts, deadline_s, schema)
    except Exception as exc:
        logger.warning(f"outfit judge failed, asking once more: {exc}")
        return _ask(parts, deadline_s, schema)


def _ask(parts: list, deadline_s: float, schema: dict) -> dict:
    """One judge call; raises on a failed call or a non-object answer."""
    answer = gen.generate_json(
        parts,
        system=OUTFIT_SYSTEM,
        schema=schema,
        params=TextParams(
            model=get_models().judge,
            thinking=THINKING,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            deadline_s=deadline_s,
        ),
    )
    if not isinstance(answer, dict):
        raise ValueError(f"expected a JSON object, got {type(answer)}")
    return answer


def evaluate_outfit(
    products: Sequence[OutfitProduct],
    target: bytes,
    *,
    video_frame: bool = False,
    wearer: bytes | None = None,
    face: bytes | None = None,
) -> Verdict:
    """Judge whether ``target`` shows every product faithfully, in one call.

    ``video_frame``: ``target`` is a frame of a video (``VIDEO_FRAME_NOTE``:
    blur, small unreadable text and limb poses are not defects;
    ``VIDEO_DEADLINE_S``).
    ``wearer``: the person before the try-on; the judge also checks that
    the body and the face are kept (``WEARER_NOTE``, ``WEARER_SCHEMA``): a
    body defect rejects, the description starting with ``BODY_NOTE``.
    ``face``: a close crop of the wearer's face, shown next to the WEARER
    (``FACE_LABEL``; ignored without a ``wearer``).

    A failed call (``core.gen`` has already retried transport errors, empty
    answers and a stall) or a non-object answer returns ``error=True``
    (transient, re-run).
    ``Verdict.details = {"products": [{index, name, defects, findings,
    minor}], "body_defects": [...], "body_parts": [...]}``, ``index`` being
    the product's 1-based position in ``products``, ``minor`` the findings
    that lower the score without rejecting (``MINOR``; ``_settle``) and
    ``body_parts`` the short name of each body defect's part ('' when the
    judge names none).
    """
    if not products:
        raise InputError("evaluate_outfit needs at least one product")
    parts = _parts(products, target, video_frame, wearer, face)
    deadline_s = VIDEO_DEADLINE_S if video_frame else DEADLINE_S
    schema = OUTFIT_SCHEMA if wearer is None else WEARER_SCHEMA
    try:
        answer = _ask_twice(parts, deadline_s, schema)
    except Exception as exc:
        logger.warning(f"outfit judge failed: {exc}")
        return Verdict(
            publish=False,
            error=True,
            description=f"eval incomplete: judge failed (transient) — {exc}",
        )
    details = _details(answer)
    publish, count, description = _settle(answer, details)
    return Verdict(
        publish=publish,
        imperfection_count=count,
        description=description,
        details=details,
    )
