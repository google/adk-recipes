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

"""Product consistency of a generated image against reference photos.

Each reference is compared with the target by the detector (judge model),
its flags are re-checked by the verifier (lite model), then one aggregator
call (text model) turns the surviving defects into a publish verdict.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import garment_detector
from genmedia4commerce.eval.garment_detector import DetectorResult, Finding
from genmedia4commerce.eval.garment_prompts import (
    AGGREGATE_LAYERING,
    AGGREGATE_SYSTEM,
    AGGREGATE_VIDEO_TEXT_TOLERANCE,
    PRODUCT_FILTER_HINTS,
    VERDICT_SCHEMA,
)

__all__ = ["evaluate_product_consistency"]

logger = logging.getLogger(__name__)

# identity_text defects whose box long side (0-1000) is at most this are too
# small to read in a motion-blurred video frame (~20% of the frame).
SMALL_TEXT_MAX_LONG_SIDE = 200


@dataclass
class _Outcome:
    """Detector result (or error) for one reference."""

    result: DetectorResult | None = None
    error: str | None = None
    skipped: bool = False


def _evaluate_reference(
    reference: bytes,
    target: bytes,
    focus: str | None,
    video_frame: bool,
    visibility: str | None,
) -> _Outcome:
    try:
        result = garment_detector.detect(
            reference,
            target,
            product_filter=focus,
            video_frame=video_frame,
            visibility=visibility,
        )
        # A filtered product absent from the reference: skip the reference.
        if focus and not result.product_present:
            return _Outcome(result=result, skipped=True)
        garment_detector.verify(reference, target, result)
    except Exception as exc:
        logger.warning(f"garment detector failed: {exc}")
        return _Outcome(error=str(exc))
    return _Outcome(result=result)


def _long_side(box: list[int] | None) -> int | None:
    if not box:
        return None
    y0, x0, y1, x1 = box
    return max(abs(x1 - x0), abs(y1 - y0))


def _small_text(f: Finding) -> bool:
    """Return whether ``f`` is a small identity_text defect (video-illegible)."""
    if not (f.is_defect and f.category == "identity_text"):
        return False
    sides = [
        s
        for s in (_long_side(f.ref_box), _long_side(f.target_box))
        if s is not None
    ]
    return bool(sides) and max(sides) <= SMALL_TEXT_MAX_LONG_SIDE


def _fmt_size(box: list[int] | None) -> str:
    if not box:
        return "not visible"
    y0, x0, y1, x1 = box
    w, h = x1 - x0, y1 - y0
    return f"{w} x {h} px (long_side: {max(w, h)})"


def _block(n: int, label: str, result: DetectorResult) -> str:
    defects = result.defects
    lines = [
        f"== Input #{n} ({label}) — "
        f"{result.matches} features matched, {len(defects)} defects =="
    ]
    for k, f in enumerate(defects, 1):
        lines.append(
            f"  [{k}] {f.category} / {f.kind.upper()} / defect\n"
            f"      label: {f.label}\n"
            f"      target_size: {_fmt_size(f.target_box)}\n"
            f"      ref_size: {_fmt_size(f.ref_box)}\n"
            f"      {f.explanation.strip()}"
        )
    return "\n".join(lines)


def _aggregate(
    target: bytes,
    kept: list[tuple[bytes, DetectorResult]],
    label: str,
    video_frame: bool,
    visibility: str | None,
) -> tuple[bool, int, str]:
    """One aggregator call over every kept reference; a fabricated element
    always rejects."""
    parts: list[gen.Part] = ["TARGET (generated) image:", target]
    blocks = []
    for n, (reference, result) in enumerate(kept):
        parts += [f"REFERENCE input #{n} ({label}):", reference]
        blocks.append(_block(n, label, result))
    parts.append(
        "Per-input defects (only is_defect=true entries; matches are "
        "summarised in the header counts):\n\n" + "\n\n".join(blocks)
    )
    system = AGGREGATE_SYSTEM + (
        AGGREGATE_VIDEO_TEXT_TOLERANCE if video_frame else ""
    )
    if visibility:
        system += AGGREGATE_LAYERING.format(visibility=visibility)
    raw = gen.generate_json(
        parts,
        system=system,
        schema=VERDICT_SCHEMA,
        params=TextParams(max_output_tokens=16384),
    )
    if not isinstance(raw, dict):
        raise ValueError(
            f"aggregator returned {type(raw).__name__}, not an object"
        )
    publish = bool(raw.get("publish", False))
    try:
        count = max(0, int(raw.get("imperfection_count", 0)))
    except (TypeError, ValueError):
        count = 0
    description = str(raw.get("description", "")).strip()
    return _reject_fabricated(kept, publish, count, description)


def _reject_fabricated(
    kept: list[tuple[bytes, DetectorResult]],
    publish: bool,
    count: int,
    description: str,
) -> tuple[bool, int, str]:
    """Never publish when a fabricated element survives.

    A surviving extra defect (any category) is something rendered on the
    product that the reference does not have; occluded and out-of-scope
    extras never count.
    """
    fabricated = [
        f.label for _, r in kept for f in r.defects if f.kind == "extra"
    ]
    if not fabricated:
        return publish, count, description
    description = (
        f"REJECT: fabricated element: {'; '.join(fabricated)}. {description}"
    ).strip()
    return False, max(count, len(fabricated)), description


def _details(outcomes: list[_Outcome]) -> dict:
    per_reference, findings = [], []
    for index, o in enumerate(outcomes):
        defects = o.result.defects if o.result else []
        per_reference.append(
            {
                "index": index,
                "publish": o.result is not None
                and not o.skipped
                and not defects,
                "defects": [
                    {
                        "label": f.label,
                        "category": f.category,
                        "kind": f.kind,
                        "explanation": f.explanation,
                    }
                    for f in defects
                ],
                "error": o.error,
                "skipped": o.skipped,
            }
        )
        findings += [
            f"{f.category}: {f.label} — {f.explanation}" for f in defects
        ]
    return {"per_reference": per_reference, "findings": findings}


def _detect_all(
    refs: list[bytes],
    target: bytes,
    focus: str | None,
    video_frame: bool,
    parallel: int,
    visibility: str | None,
) -> list[_Outcome]:
    """Run the detector on every reference, ``parallel`` at a time.

    For a video frame, small identity_text defects (illegible) are dropped.
    """
    workers = max(1, min(parallel, len(refs)))
    with ContextPool(max_workers=workers) as pool:
        outcomes = list(
            pool.map(
                lambda r: _evaluate_reference(
                    r, target, focus, video_frame, visibility
                ),
                refs,
            )
        )
    if video_frame:
        for o in outcomes:
            if o.result:
                o.result.findings = [
                    f for f in o.result.findings if not _small_text(f)
                ]
    return outcomes


def _unevaluated(outcomes: list[_Outcome], details: dict) -> Verdict:
    """The verdict when no reference is left for the aggregator."""
    errored = [o.error for o in outcomes if o.error]
    if errored:
        return Verdict(
            publish=False,
            error=True,
            description=(
                f"eval incomplete: all {len(errored)} reference(s) errored "
                f"(transient) — re-run. First error: {errored[0][:150]}"
            ),
            details=details,
        )
    return Verdict(
        publish=False,
        description="No inputs were evaluated — every reference was filtered out.",
        details=details,
    )


def evaluate_product_consistency(
    references: Sequence[bytes],
    target: bytes,
    *,
    product_filter: str | None = None,
    ignore_text_defects: bool = False,
    parallel: int = 8,
    visibility: str | None = None,
) -> Verdict:
    """Judge whether ``target`` shows the product of ``references`` faithfully.

    ``product_filter`` scopes the eval to one product: free text, or a
    ``Category`` value (replaced by its ``PRODUCT_FILTER_HINTS`` sentence).
    ``ignore_text_defects=True`` means the target is a video frame: small or
    garbled text/logos are tolerated and limb-pose occlusion is expected.
    ``visibility``: the product is worn under other garments by design and
    only this part is meant to be visible; the detector and the aggregator
    judge that part and treat the covered rest as occluded (``None``: the
    product is judged as fully visible, prompts unchanged).
    References are evaluated ``parallel`` at a time. When every reference
    errors the verdict has ``error=True`` (transient, re-run). A surviving
    extra defect (a fabricated element) always rejects.

    ``Verdict.details = {"per_reference": [{index, publish, defects: [...]}],
    "findings": [str]}``.
    """
    if not references:
        raise InputError(
            "evaluate_product_consistency needs at least one reference"
        )
    focus = PRODUCT_FILTER_HINTS.get(product_filter, product_filter)
    target_png = media.to_png(target)
    refs_png = [media.to_png(r) for r in references]
    outcomes = _detect_all(
        refs_png, target_png, focus, ignore_text_defects, parallel, visibility
    )
    details = _details(outcomes)
    kept = [
        (refs_png[i], o.result)
        for i, o in enumerate(outcomes)
        if o.result is not None and not o.skipped
    ]
    if not kept:
        return _unevaluated(outcomes, details)
    try:
        publish, count, description = _aggregate(
            target_png, kept, focus or "—", ignore_text_defects, visibility
        )
    except Exception as exc:
        logger.warning(f"garment aggregator failed: {exc}")
        return Verdict(
            publish=False,
            error=True,
            description=f"eval incomplete: aggregator failed (transient) — {exc}",
            details=details,
        )
    return Verdict(
        publish=publish,
        imperfection_count=count,
        description=description,
        details=details,
    )
