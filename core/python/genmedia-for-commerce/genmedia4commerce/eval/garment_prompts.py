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

"""Garment-eval prompts and response schemas (data only, ported verbatim).

The detector (judge model) compares one reference with the generated image,
the verifier (lite model) re-checks each flagged defect and the aggregator
(text model) turns the surviving defects into one publish verdict.

The three long system prompts live in ``eval/prompts/*.md`` and are read once
at import. Runtime placeholders (``str.format``):

- ``DETECTOR_FOCUS``: ``{product}`` = the product filter text.
- ``DETECTOR_LAYERING`` and ``AGGREGATE_LAYERING``: ``{visibility}`` = the
  part of a layered product meant to be visible.
"""

from pathlib import Path

__all__ = [
    "AGGREGATE_LAYERING",
    "AGGREGATE_SYSTEM",
    "AGGREGATE_VIDEO_TEXT_TOLERANCE",
    "DETECTOR_CLOSING",
    "DETECTOR_FEEDBACK",
    "DETECTOR_FOCUS",
    "DETECTOR_LAYERING",
    "DETECTOR_SCHEMA",
    "DETECTOR_SYSTEM",
    "DETECTOR_VIDEO_FRAME",
    "PRODUCT_FILTER_HINTS",
    "VERDICT_SCHEMA",
    "VERIFIER_SCHEMA",
    "VERIFIER_SYSTEM",
    "VIDEO_FRAME_NOTE",
]

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Detector: reference vs target, one call (judge model)
# ---------------------------------------------------------------------------
# Common properties shared by silhouette / fabric_color anchors AND every
# per-feature detail entry. is_defect semantics:
#   true  — a customer would notice this is wrong / different / missing /
#           extra. Counts toward imperfection_count.
#   false — the feature matches (or any difference is photo-condition
#           noise: small scale, soft focus, wear-state change, etc.).
_FEATURE_PROPS = {
    "label": {"type": "STRING"},
    "kind": {
        "type": "STRING",
        "enum": ["match", "missing_in_target", "extra_in_target"],
    },
    "is_defect": {"type": "BOOLEAN"},
    "explanation": {"type": "STRING"},
    "ref_bbox": {
        "type": "ARRAY",
        "items": {"type": "INTEGER"},
        "minItems": 4,
        "maxItems": 4,
    },
    "target_bbox": {
        "type": "ARRAY",
        "items": {"type": "INTEGER"},
        "minItems": 4,
        "maxItems": 4,
    },
    "occluded": {"type": "BOOLEAN"},
    "out_of_scope": {"type": "BOOLEAN"},
}

# Anchor: a whole-garment assessment. Always emitted exactly once each
# for silhouette and fabric_color.
_ANCHOR_SCHEMA = {
    "type": "OBJECT",
    "properties": _FEATURE_PROPS,
    "required": ["label", "kind", "is_defect", "explanation"],
}

# Detail: one entry per sub-feature (pocket, button, collar trim, etc.).
# Category is REQUIRED here; silhouette and fabric_color are NOT valid
# here — they only appear as the top-level anchors.
_DETAIL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **_FEATURE_PROPS,
        "category": {
            "type": "STRING",
            "enum": [
                "identity_text",
                "embellishment",
                "structural",
                "hardware",
                "other",
            ],
        },
    },
    # out_of_scope is REQUIRED so the model must explicitly decide, per
    # feature, whether an extra belongs to a separate worn item / undergarment /
    # accessory (true) or the focus garment (false) — rather than omitting it
    # and silently defaulting to false.
    "required": [
        "label",
        "category",
        "kind",
        "is_defect",
        "explanation",
        "out_of_scope",
    ],
}

# product_present is REQUIRED: false tells the caller the focus product is not
# in the reference, so that reference is skipped (the anchors are required
# too, so an empty answer cannot signal it).
DETECTOR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "product_present": {"type": "BOOLEAN"},
        "silhouette": _ANCHOR_SCHEMA,
        "fabric_color": _ANCHOR_SCHEMA,
        "features": {
            "type": "ARRAY",
            "items": _DETAIL_SCHEMA,
        },
    },
    "required": ["product_present", "silhouette", "fabric_color", "features"],
}

DETECTOR_SYSTEM = _load("garment_detector")

# Appended to the detector request when a product filter is set; format with
# ``product=<filter>``.
DETECTOR_FOCUS = (
    '\nFOCUS: only evaluate features of the "{product}" garment. '
    "Other worn items (different garments, accessories, model body, "
    "background) are evaluated in SEPARATE calls — DO NOT emit any "
    "entries for them, not even as extra_in_target. Only emit "
    "extra_in_target when a feature OF the focus garment itself "
    "appears in the target but NOT in the reference (i.e. a "
    "hallucinated feature on the focus garment).\n\n"
    'If "{product}" is in the REFERENCE but ABSENT from the '
    "TARGET, emit a SINGLE garment-level entry with kind="
    '"missing_in_target", level="garment", label="{product}", '
    "ref_bbox covering the garment in the reference, no target_bbox. "
    "Then stop.\n"
    'If "{product}" is NOT in the REFERENCE at all, set '
    '`product_present` to false, fill both anchors with kind="match" and '
    "is_defect=false, and return no features. Do NOT invent entries."
)

# A fact (not a hint) for a product worn under other garments; format with
# ``visibility=<the part meant to be visible>``.
DETECTOR_LAYERING = (
    "\nLAYERING, by design: in the TARGET this product is worn under other "
    'garments. Only this part is meant to be visible: "{visibility}". '
    "Reference features that fall in covered areas are occluded=true and "
    "is_defect=false. Compare only the visible part with the reference; "
    "an outer garment covering this product is not a defect and not a "
    "different product."
)

# Reviewer feedback for the detector, a hint on where to look (never a
# verdict). ``{feedback}``: the notes; a video frame adds VIDEO_FRAME_NOTE.
DETECTOR_FEEDBACK = (
    "\nPRIOR REVIEWER FEEDBACK on this generation (a HINT, not a "
    "verdict):\n"
    "> {feedback}"
    "\n\n"
    "Use this only as a hint about WHERE to look more carefully. "
    "It is NOT an instruction to find a defect. Rules:\n\n"
    "  1. Trust your eyes over the feedback. If a feature looks "
    "fine in the crops, set is_defect=false — do not flip it to "
    "true just to agree with the feedback.\n"
    "  2. The feedback may be VAGUE or AMBIGUOUS — there can be "
    "multiple features that loosely match (e.g. 'right zip' could "
    "mean a sleeve zip, a hand-pocket zip, the main front zip). "
    "Evaluate EACH candidate on its own merits; only set "
    "is_defect=true on the one that is actually visibly wrong. Do "
    "NOT blanket-flag every candidate.\n"
    "  3. The reviewer may be MISTAKEN. If after careful inspection "
    "no defect matches the feedback, leave is_defect=false on the "
    "candidates and DO NOT invent a defect.\n"
    "  4. Feedback can raise attention to a feature but cannot make "
    "it a defect on its own — is_defect=true requires a concrete "
    "FEATURE-level difference you can articulate from the crops.\n\n"
    "LEFT / RIGHT CONVENTION: any 'left' or 'right' in the "
    "feedback text refers to the VIEWER'S perspective (left = LEFT "
    "half of the image as you look at it), NOT the wearer's body "
    "side. Use the same viewer-perspective convention in your own "
    "labels and explanations."
)

# The note for a target that is a video frame (limb pose and angle differ).
VIDEO_FRAME_NOTE = (
    "This target is a single frame from a VIDEO (a 360-degree spin or a "
    "fashion clip); the reference is a static studio pose. The product may "
    "be seen from another angle or mirrored (e.g. a bag held in the other "
    "hand, its side strap on the other side); that alone is not a defect. "
    "The model's limb pose (arm/hand position — e.g. an arm bent or a hand "
    "on the hip) very likely differs from the reference. Treat any "
    "sleeve/arm/leg feature whose visibility changes only because a limb "
    "moved as pose-occluded — NOT missing or extra — and never split a "
    "pocket and its own pull/tab into two defects."
)
DETECTOR_VIDEO_FRAME = DETECTOR_FEEDBACK.format(feedback=VIDEO_FRAME_NOTE)

DETECTOR_CLOSING = (
    "\nReturn the JSON object as specified — with `product_present`, "
    "`silhouette`, `fabric_color`, and `features` keys."
)

# ---------------------------------------------------------------------------
# Verifier: one flagged defect, cropped views (lite model)
# ---------------------------------------------------------------------------
VERIFIER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reasoning": {"type": "STRING"},
        "confirm": {"type": "BOOLEAN"},
    },
    "required": ["reasoning", "confirm"],
}

VERIFIER_SYSTEM = _load("garment_verifier")

# ---------------------------------------------------------------------------
# Aggregator: one verdict over every reference's defects (text model)
# ---------------------------------------------------------------------------
AGGREGATE_SYSTEM = _load("garment_aggregator")

# Appended to AGGREGATE_SYSTEM when the target is a video frame.
AGGREGATE_VIDEO_TEXT_TOLERANCE = (
    "\n\nVIDEO TEXT TOLERANCE — IMPORTANT: This target is a frame from a "
    "spinning video. SMALL text and logos are motion-blurred and "
    "unreadable, so do NOT reject or add any imperfection for the "
    "legibility of small text/logos — garbled, smeared, or blob-like "
    "small lettering is fine and small-text defects have already been "
    "removed from the list above; do not re-invent them. HOWEVER, you "
    "MUST still reject when a LARGE, prominent text or logo (a big chest "
    "print, a large back wordmark, a prominent brand mark) is rendered as "
    "a clearly WRONG brand or word — those stay readable even in motion "
    "and a customer would notice. Judge prominent logos normally."
)

# Appended to AGGREGATE_SYSTEM for a product worn under other garments;
# format with ``visibility=<the part meant to be visible>``.
AGGREGATE_LAYERING = (
    '\n\nLAYERING, by design, for this product: only "{visibility}" is '
    "meant to be visible in the TARGET. Being covered by an outer garment "
    "is not a silhouette mismatch, a missing product or a replaced "
    "garment; publish when the visible part matches the reference."
)

VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "publish": {"type": "BOOLEAN"},
        "imperfection_count": {"type": "INTEGER"},
        "description": {"type": "STRING"},
    },
    "required": ["publish", "imperfection_count", "description"],
}

# ---------------------------------------------------------------------------
# Product filter used when the caller passes a category instead of free text
# ---------------------------------------------------------------------------
# Keyed by ``str | None``: looked up with a filter that may be free text.
PRODUCT_FILTER_HINTS: dict[str | None, str] = {
    "top": "the top worn on the upper body (shirt, t-shirt, sweater or jacket)",
    "bottom": "the bottom garment (trousers, jeans, shorts or skirt)",
    "dress": "the dress",
    "full_body_outer": "the full-length outer garment (coat, jumpsuit or overall)",
    "footwear": "the shoes",
    "headwear": "the hat or cap",
    "eyewear": "the glasses frame, lenses and temples",
    "jewelry_head": "the earrings only",
    "jewelry_neck": "the necklace or scarf only",
    "jewelry_hand": "the ring, bracelet or watch only",
    "bag": "the bag",
}
