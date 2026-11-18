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

"""Per-reference garment defect detector (judge model) and its verifier.

``detect`` asks the judge model to list the reference's distinctive features,
locate each one in both images and flag defects. ``verify`` re-checks every
flagged defect with the lite model on zoomed crops; it can only drop a flag,
never add one. Bounding boxes are ``[ymin, xmin, ymax, xmax]`` on 0-1000 and
refer to the exact image bytes passed in.
"""

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from PIL import Image

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.eval.garment_prompts import (
    DETECTOR_CLOSING,
    DETECTOR_FEEDBACK,
    DETECTOR_FOCUS,
    DETECTOR_LAYERING,
    DETECTOR_SCHEMA,
    DETECTOR_SYSTEM,
    VERIFIER_SCHEMA,
    VERIFIER_SYSTEM,
    VIDEO_FRAME_NOTE,
)

__all__ = ["DetectorResult", "Finding", "detect", "verify"]

logger = logging.getLogger(__name__)

_KINDS = {
    "match": "pair",
    "missing_in_target": "missing",
    "extra_in_target": "extra",
}
_DETAIL_CATEGORIES = (
    "identity_text",
    "embellishment",
    "structural",
    "hardware",
)
_VERIFY_WORKERS = 8

# Crop around a box: padding = max(40 px, 30% of the box), at least 15% of
# each image side so tiny detections keep enough context for the verifier.
_PADDING_FRAC = 0.30
_MIN_PADDING_PX = 40
_MIN_CROP_FRAC = 0.15

_Sized = tuple[bytes, tuple[int, int]]  # image bytes and (width, height)


@dataclass
class Finding:
    """One compared feature (whole-garment anchor or sub-feature)."""

    label: str
    level: Literal["garment", "detail"]
    category: str  # silhouette | fabric_color | identity_text | ... | other
    kind: Literal["pair", "missing", "extra"]
    is_defect: bool
    explanation: str = ""
    ref_box: list[int] | None = None
    target_box: list[int] | None = None
    occluded: bool = False
    out_of_scope: bool = False

    @property
    def counts(self) -> bool:
        """True for a defect that is neither occluded nor out of scope."""
        return self.is_defect and not self.out_of_scope and not self.occluded


@dataclass
class DetectorResult:
    """The detector's findings for one reference.

    ``product_present`` is False when the focus product is not in the
    reference at all.
    """

    findings: list[Finding] = field(default_factory=list)
    product_present: bool = True

    @property
    def defects(self) -> list[Finding]:
        """Findings that count as defects."""
        return [f for f in self.findings if f.counts]

    @property
    def matches(self) -> int:
        """Number of in-scope, visible features rendered correctly."""
        return sum(
            1
            for f in self.findings
            if not f.is_defect and not f.out_of_scope and not f.occluded
        )


def _box(value: Any) -> list[int] | None:
    return (
        [int(v) for v in value]
        if isinstance(value, list) and len(value) == 4
        else None
    )


def _finding(item: Any, *, level: str, category: str) -> Finding | None:
    if not isinstance(item, dict):
        return None
    out_of_scope = bool(item.get("out_of_scope", False))
    return Finding(
        label=str(item.get("label", "")),
        level=level,  # type: ignore[arg-type]
        category=category,
        kind=_KINDS.get(item.get("kind", "match"), "pair"),  # type: ignore[arg-type]
        # An out-of-scope extra (undergarment, other worn item) is never a defect.
        is_defect=bool(item.get("is_defect", False)) and not out_of_scope,
        explanation=str(item.get("explanation", "")),
        ref_box=_box(item.get("ref_bbox")),
        target_box=_box(item.get("target_bbox")),
        occluded=bool(item.get("occluded", False)),
        out_of_scope=out_of_scope,
    )


def _parse(raw: dict | list) -> DetectorResult:
    if not isinstance(raw, dict):
        return DetectorResult()
    findings = [
        _finding(raw.get(anchor), level="garment", category=anchor)
        for anchor in ("silhouette", "fabric_color")
    ]
    for item in raw.get("features") or []:
        category = item.get("category") if isinstance(item, dict) else None
        if category not in _DETAIL_CATEGORIES:
            category = "other"
        findings.append(_finding(item, level="detail", category=category))
    return DetectorResult(
        [f for f in findings if f is not None],
        product_present=raw.get("product_present") is not False,
    )


def detect(
    reference: bytes,
    target: bytes,
    *,
    product_filter: str | None,
    video_frame: bool = False,
    user_feedback: str | None = None,
    visibility: str | None = None,
) -> DetectorResult:
    """Compare ``target`` with one ``reference`` (one judge-model call).

    ``product_filter`` scopes the comparison to one product; ``video_frame``
    tells the judge that ``target`` is a video frame (limb pose and angle may
    differ from the reference); ``user_feedback``: a reviewer's notes, a hint
    on where to look (the judge confirms or refutes, never just agrees);
    ``visibility``: the only part of the product meant to be visible, worn
    under other garments by design (stated as a fact; covered features are
    occluded, not defects).
    """
    parts: list[gen.Part] = [
        "REFERENCE image:",
        reference,
        "TARGET image:",
        target,
    ]
    if product_filter:
        parts.append(DETECTOR_FOCUS.format(product=product_filter))
    if visibility:
        parts.append(DETECTOR_LAYERING.format(visibility=visibility))
    notes = [VIDEO_FRAME_NOTE] if video_frame else []
    if user_feedback and user_feedback.strip():
        notes.append(user_feedback.strip())
    if notes:
        parts.append(DETECTOR_FEEDBACK.format(feedback="\n> ".join(notes)))
    parts.append(DETECTOR_CLOSING)
    raw = gen.generate_json(
        parts,
        system=DETECTOR_SYSTEM,
        schema=DETECTOR_SCHEMA,
        params=TextParams(
            model=get_models().judge,
            thinking="LOW",
            max_output_tokens=65535,
            deadline_s=90,
        ),
    )
    return _parse(raw)


def _crop(image: bytes, size: tuple[int, int], box: list[int]) -> bytes | None:
    """Crop around a 0-1000 box with padding and a minimum crop size."""
    width, height = size
    y1, x1, y2, x2 = box
    ax1, ax2 = sorted((int(x1 / 1000 * width), int(x2 / 1000 * width)))
    ay1, ay2 = sorted((int(y1 / 1000 * height), int(y2 / 1000 * height)))
    pad = max(_MIN_PADDING_PX, int(_PADDING_FRAC * max(ax2 - ax1, ay2 - ay1)))
    cx1, cy1 = max(0, ax1 - pad), max(0, ay1 - pad)
    cx2, cy2 = min(width, ax2 + pad), min(height, ay2 + pad)
    min_w, min_h = int(_MIN_CROP_FRAC * width), int(_MIN_CROP_FRAC * height)
    if cx2 - cx1 < min_w:
        cx1 = max(0, (cx1 + cx2) // 2 - min_w // 2)
        cx2 = min(width, cx1 + min_w)
        cx1 = max(0, cx2 - min_w)
    if cy2 - cy1 < min_h:
        cy1 = max(0, (cy1 + cy2) // 2 - min_h // 2)
        cy2 = min(height, cy1 + min_h)
        cy1 = max(0, cy2 - min_h)
    try:
        return media.crop_box(image, (cx1, cy1, cx2, cy2))
    except InputError:
        return None


def _verify_parts(
    images: tuple[_Sized, _Sized], finding: Finding
) -> list[gen.Part]:
    (ref, ref_size), (tgt, tgt_size) = images
    parts: list[gen.Part] = []
    ref_crop = (
        _crop(ref, ref_size, finding.ref_box) if finding.ref_box else None
    )
    if ref_crop:
        parts += ["REF crop (zoomed view of the feature area):", ref_crop]
    elif not finding.ref_box:
        parts.append("REF crop: not available (extra-in-target case).")
    tgt_crop = (
        _crop(tgt, tgt_size, finding.target_box) if finding.target_box else None
    )
    if tgt_crop:
        parts += ["TARGET crop (same area in the generated image):", tgt_crop]
    elif not finding.target_box:
        parts.append("TARGET crop: not available (missing-in-target case).")
    return [
        *parts,
        "FULL REFERENCE image (whole garment, for re-locating the feature if the crop is misaligned):",
        ref,
        "FULL TARGET image (whole generated image, for re-locating the feature if the crop is misaligned):",
        tgt,
        f"Feature label: {finding.label}\n"
        f"Detector kind: {finding.kind}\n"
        f"Detector complaint: {finding.explanation}",
    ]


def _verify_one(images: tuple[_Sized, _Sized], finding: Finding) -> None:
    if not finding.ref_box and not finding.target_box:
        return
    try:
        raw = gen.generate_json(
            _verify_parts(images, finding),
            system=VERIFIER_SYSTEM,
            schema=VERIFIER_SCHEMA,
            params=TextParams(
                model=get_models().text_lite,
                thinking="LOW",
                max_output_tokens=2048,
            ),
        )
    except Exception as exc:
        logger.warning(f"verifier call failed for {finding.label!r}: {exc}")
        return
    if isinstance(raw, dict) and raw.get("confirm") is False:
        reasoning = str(raw.get("reasoning") or "").strip()
        finding.is_defect = False
        finding.explanation += f" [verifier dropped: {reasoning}]"
        logger.info(f"verifier dropped {finding.label!r}: {reasoning[:80]}")


def verify(
    reference: bytes, target: bytes, result: DetectorResult
) -> DetectorResult:
    """Re-check each flagged defect on crops; drop the ones the verifier rejects.

    Mutates and returns ``result``.
    """
    if not result.defects:
        return result
    ref, tgt = (
        (data, Image.open(io.BytesIO(data)).size)
        for data in (reference, target)
    )
    images = (ref, tgt)
    with ContextPool(max_workers=_VERIFY_WORKERS) as pool:
        list(pool.map(lambda f: _verify_one(images, f), result.defects))
    return result
