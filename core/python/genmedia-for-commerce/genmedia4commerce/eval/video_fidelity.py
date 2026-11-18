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

"""Matching-frame fidelity of a generated video against reference views.

1. Pick the stillest, sharpest frame per 0.5 s of video.
2. Label each picked frame with the view it shows (``orientation``).
3. For each reference view, the candidate frames carry that label (shoes
   widen to neighbouring views in tiers); the winner is the candidate most
   similar (SSIM) to the reference. With ``any_frame``, a view no frame
   carries falls back to the most similar of all frames.
4. Judge each (reference, winner) pair in parallel (``judge``).
5. Publish only when every view was found and every pair passed.
"""

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal

from genmedia4commerce.core import gen, media
from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.errors import EvalError, InputError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import garment
from genmedia4commerce.eval import orientation as orient
from genmedia4commerce.eval.orientation import Orientation

__all__ = ["Judge", "evaluate_video_fidelity"]

logger = logging.getLogger(__name__)

Judge = Literal["shoe", "garment", "product"]

# Shoe frame labels accepted for a reference view, best tier first.
SHOE_TIERS = {
    "right": [["right"], ["front_right", "back_right"]],
    "front": [["front"], ["front_right", "front_left"], ["top_front"]],
    "back": [["back_right"], ["back", "back_left"]],
    "back_right": [["back_right"], ["back", "back_left"]],
}

# A three-quarter reference with no frame labelled with it (the generic
# Gemini labeller has no three-quarter labels) is matched against frames
# labelled with either neighbouring view.
THREE_QUARTER_NEIGHBOURS = {
    "front_right": ["front", "right"],
    "back_right": ["back", "right"],
    "back_left": ["back", "left"],
    "front_left": ["front", "left"],
}

SHOE_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "same_shoe": {"type": "boolean"},
        "aspects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "aspect": {
                        "type": "string",
                        "enum": [
                            "silhouette",
                            "color",
                            "material",
                            "branding",
                            "sole",
                            "hardware",
                            "stitching",
                            "other",
                        ],
                    },
                    "is_defect": {"type": "boolean"},
                    "explanation": {"type": "string"},
                },
                "required": ["aspect", "is_defect", "explanation"],
            },
        },
    },
    "required": ["same_shoe", "aspects"],
}

SHOE_JUDGE_SYSTEM = """You are an expert footwear QA evaluator comparing TWO images of a shoe:
  - REFERENCE: a clean studio product crop of the real shoe.
  - TARGET: one frame from an AI-generated 360-degree spin video of the SAME shoe.

The two images show the shoe FROM DIFFERENT ANGLES (that is expected — the video frame is mid-rotation). Judge the shoe's IDENTITY and construction, NOT pixel similarity, framing, lighting, motion blur, or angle.

Report a JSON object:
  {
    "same_shoe": bool,   # is the TARGET clearly the SAME shoe model as the REFERENCE?
    "aspects": [ { "aspect": <enum>, "is_defect": bool, "explanation": str }, ... ]
  }

Emit one `aspects` entry per distinctive, customer-visible property you can compare AT THE TARGET ANGLE:
  - silhouette: overall shape/type/proportions (loafer vs sneaker vs boot, toe shape, heel/sole height).
  - color: main color(s) and any color-blocking.
  - material: leather / suede / canvas / knit / rubber, texture.
  - branding: logos, wordmarks, embossed brand marks, hardware logos, crests.
  - sole: sole color, shape, outsole grip pattern, welt.
  - hardware: buckles, tassels, zips, eyelets, metal trims.
  - stitching: contrast stitching, moc-toe seam, piping.

is_defect semantics:
  - true  = a customer would notice this property is WRONG / different / missing / hallucinated on the TARGET vs the REFERENCE shoe (wrong color, wrong logo shape/letters, missing tassel, different outsole grip pattern, morphed silhouette, extra/absent hardware).
  - false = the property matches, OR any difference is explained by angle / lighting / motion blur / a surface rotated out of view / lower video resolution.

VISIBLE HARDWARE / BRANDING FIDELITY (counterweight to the leniency below):
When a HARDWARE piece (padlock, tassel, ring, hook, key, charm, buckle, eyelet, zip pull, metal trim) or a BRANDING element (logo, wordmark, embossed mark) is CLEARLY VISIBLE in BOTH images, do NOT stop at "gold hardware present" / "a logo is there". Actively compare its SPECIFIC shape, TYPE, COUNT, and ARRANGEMENT:
  - If the reference shows a padlock + a CIRCULAR ring/hook and the target shows a padlock + a RECTANGULAR key/tag (or a different charm shape), that is a hardware defect: is_defect=true.
  - If the COUNT of hardware pieces differs (2 charms vs 1), or their ARRANGEMENT/position on the strap differs, is_defect=true.
  - If a logo's letters/shape are rendered differently or become an unreadable blob where the reference shows a specific mark, is_defect=true (branding).
This holds EVEN THOUGH the viewing angle differs — shape/type/count/arrangement of clearly-visible hardware is angle-invariant. Only excuse it when the piece is genuinely rotated out of view or too motion-blurred to resolve at all.

DO NOT flag (is_defect=false):
  - A feature not visible at the TARGET angle because that surface rotated away (e.g. a heel logo when viewing the toe). Only assess what is visible.
  - Softer focus, motion blur, lower resolution, lighting/shadow shifts from the video.
  - Minor perspective/foreshortening differences.

Set same_shoe=false only if the TARGET is clearly a DIFFERENT shoe (wrong type/color/major design). Otherwise same_shoe=true.
Only add aspect entries you can actually judge at the target angle; skip ones on hidden surfaces."""

PRODUCT_JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "same_product": {"type": "BOOLEAN"},
        "defects": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["same_product", "defects"],
}

PRODUCT_JUDGE_SYSTEM = (
    "Is the product in FRAME the same product as in REFERENCE (same "
    "silhouette, colour, material, branding, hardware)? List any visible "
    "difference or artefact. FRAME is one frame of a 360° spin video, so the "
    "angle, lighting and sharpness may differ: judge the product itself and "
    "ignore surfaces rotated out of view. A removable accessory (a detachable "
    "shoulder strap, a charm, a tag) shown in REFERENCE but absent in FRAME is "
    "not a difference: the spin leaves such parts off."
)


@dataclass
class _Pair:
    """Outcome for one reference view."""

    view: str
    frame_index: int | None
    publish: bool = False
    findings: list[str] = field(default_factory=list)
    error: bool = False
    defects: int = 0


def _judge_params() -> TextParams:
    return TextParams(model=get_models().judge, thinking="LOW", deadline_s=90)


def _judge_shoe(reference: bytes, frame: bytes) -> tuple[bool, int, list[str]]:
    raw = gen.generate_json(
        [
            "REFERENCE image (real shoe, studio crop):",
            reference,
            "TARGET image (one frame from the generated spin video):",
            frame,
            "Compare and return the JSON object as specified.",
        ],
        system=SHOE_JUDGE_SYSTEM,
        schema=SHOE_JUDGE_SCHEMA,
        params=_judge_params(),
    )
    if not isinstance(raw, dict):
        raise EvalError(f"shoe judge: not a JSON object: {raw!r:.200}")
    same = bool(raw.get("same_shoe", True))
    defects = [a for a in raw.get("aspects") or [] if a.get("is_defect")]
    findings = [
        f"{a.get('aspect')}: {a.get('explanation', '')}" for a in defects
    ]
    if not same:
        findings.insert(0, "different shoe")
    return same and not defects, len(defects) + (0 if same else 1), findings


def _judge_product(
    reference: bytes, frame: bytes
) -> tuple[bool, int, list[str]]:
    raw = gen.generate_json(
        [
            "REFERENCE image:",
            reference,
            "FRAME image:",
            frame,
            "Compare and return the JSON object as specified.",
        ],
        system=PRODUCT_JUDGE_SYSTEM,
        schema=PRODUCT_JUDGE_SCHEMA,
        params=_judge_params(),
    )
    if not isinstance(raw, dict):
        raise EvalError(f"product judge: not a JSON object: {raw!r:.200}")
    same = bool(raw.get("same_product", False))
    findings = [str(d) for d in raw.get("defects") or []]
    if not same:
        findings.insert(0, "different product")
    return same and not raw.get("defects"), len(findings), findings


def _best_frame(
    view: str,
    reference: bytes,
    picks: list[tuple[int, bytes]],
    labels: list[str],
    strategy: Orientation,
    any_frame: bool,
) -> tuple[int, bytes] | None:
    if strategy == "shoe":
        tiers = SHOE_TIERS.get(view, [[view]])
    else:  # the view itself, then (for a three-quarter view) its neighbours
        neighbours = THREE_QUARTER_NEIGHBOURS.get(view)
        tiers = [[view], neighbours] if neighbours else [[view]]
    for tier in tiers:
        candidates = [
            p for p, label in zip(picks, labels, strict=True) if label in tier
        ]
        if candidates:
            return max(candidates, key=lambda p: media.ssim(p[1], reference))
    if any_frame:
        return max(picks, key=lambda p: media.ssim(p[1], reference))
    return None


def _judge_pair(
    judge: Judge,
    product_filter: str | None,
    pair: _Pair,
    reference: bytes,
    frame: bytes,
) -> _Pair:
    try:
        if judge == "garment":
            verdict = garment.evaluate_product_consistency(
                [reference],
                frame,
                product_filter=product_filter,
                ignore_text_defects=True,
            )
            pair.publish, pair.error = verdict.publish, verdict.error
            pair.defects = verdict.imperfection_count
            pair.findings = verdict.details.get("findings") or (
                [] if verdict.publish else [verdict.description]
            )
        else:
            run = _judge_shoe if judge == "shoe" else _judge_product
            pair.publish, pair.defects, pair.findings = run(reference, frame)
    except Exception as exc:
        logger.warning(f"video fidelity judge failed for {pair.view}: {exc}")
        pair.publish, pair.error, pair.findings = (
            False,
            True,
            [f"judge failed: {exc}"],
        )
    return pair


def _describe(pairs: list[_Pair]) -> str:
    bad = [p for p in pairs if not p.publish and not p.error]
    errored = [p for p in pairs if p.error]
    if bad:
        reasons = [f"{p.view}: {'; '.join(p.findings[:2])}" for p in bad[:4]]
        return "rejected: " + " | ".join(reasons)
    if errored:
        reasons = [f"{p.view}: {p.findings[0][:200]}" for p in errored[:3]]
        return "eval incomplete (transient errors — re-run): " + " | ".join(
            reasons
        )
    return f"all {len(pairs)} views passed ({', '.join(p.view for p in pairs)})"


def _judge_views(
    references: Mapping[str, bytes],
    picks: list[tuple[int, bytes]],
    labels: list[str],
    orientation: Orientation,
    judge: Judge,
    product_filter: str | None,
    any_frame: bool,
) -> tuple[list[_Pair], int]:
    """One pair per view, judged in parallel; also the number judged.

    A view with no matching frame is a failed pair that is not judged.
    """
    pairs: list[_Pair] = []
    jobs: list[tuple[_Pair, bytes, bytes]] = []
    for view, reference in references.items():
        best = _best_frame(
            view, reference, picks, labels, orientation, any_frame
        )
        if best is None:
            pairs.append(
                _Pair(view, None, findings=["view not found in video"])
            )
            continue
        pairs.append(_Pair(view, best[0]))
        jobs.append((pairs[-1], reference, best[1]))
    if jobs:
        with ContextPool(max_workers=min(8, len(jobs))) as pool:
            list(
                pool.map(
                    lambda job: _judge_pair(judge, product_filter, *job),
                    jobs,
                )
            )
    return pairs, len(jobs)


def evaluate_video_fidelity(
    video: bytes,
    references: Mapping[str, bytes],
    *,
    orientation: Orientation,
    judge: Judge,
    product_filter: str | None = None,
    frames: list[tuple[int, bytes]] | None = None,
    labels: list[str] | None = None,
    any_frame: bool = False,
) -> Verdict:
    """Judge each reference view against its best-matching video frame.

    ``references``: view label -> reference image ("front", "back", "right",
    ...). ``frames``: the clip's ``core.video.pick_stable_frames``, when the
    caller already has them (the clip is then not decoded again);
    ``labels``: their views, when the caller already has them (no
    orientation call then; the shoe spin labels its own frames).
    ``any_frame``: a view no frame is labelled with is judged on the most
    similar frame instead of rejected (a try-on clip need not face the
    camera; a 360 spin must show every view).
    ``Verdict.details = {"per_view": [{view, frame_index, publish,
    findings}], "n_errored": int}``.
    """
    if not references:
        raise InputError("evaluate_video_fidelity needs at least one reference")
    picks = core_video.pick_stable_frames(video) if frames is None else frames
    if not picks:
        return Verdict(
            publish=False,
            description="no frames decoded from video",
            details={"per_view": [], "n_errored": 0},
        )
    try:
        labels = labels or orient.label_frames(
            [frame for _, frame in picks],
            strategy=orientation,
            references=references,
        )
    except Exception as exc:
        logger.warning(f"video fidelity: orientation failed: {exc}")
        return Verdict(
            publish=False,
            error=True,
            description=f"eval incomplete: orientation failed (transient) — {exc}",
            details={"per_view": [], "n_errored": 0},
        )
    pairs, judged = _judge_views(
        references, picks, labels, orientation, judge, product_filter, any_frame
    )
    n_errored = sum(p.error for p in pairs)
    return Verdict(
        publish=all(p.publish for p in pairs),
        imperfection_count=sum(p.defects for p in pairs),
        description=_describe(pairs),
        error=judged > 0 and n_errored == judged,
        details={
            "per_view": [
                {k: v for k, v in asdict(p).items() if k != "defects"}
                for p in pairs
            ],
            "n_errored": n_errored,
        },
    )
