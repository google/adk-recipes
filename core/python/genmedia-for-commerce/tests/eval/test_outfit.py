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

"""The one-call outfit judge: what it sends and how it reads the answer."""

import pytest

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval import outfit
from genmedia4commerce.eval.garment_prompts import PRODUCT_FILTER_HINTS
from genmedia4commerce.eval.outfit import (
    OUTFIT_SYSTEM,
    OutfitProduct,
    evaluate_outfit,
)

SHIRT, SHOES, TARGET = png(color=(1, 1, 1)), png(color=(2, 2, 2)), png()
DEFECT = {
    "feature": "laces",
    "category": "hardware",
    "kind": "missing",
    "explanation": "laces are missing instead of brown cord laces",
}
ANSWER = {
    "products": [
        {"product": "PRODUCT_2", "name": "espadrilles", "defects": [DEFECT]},
        {"product": "PRODUCT_1", "name": "shirt", "defects": []},
    ],
    "publish": False,
    "imperfection_count": 1,
    "description": "The shoes lost their laces.",
}


@pytest.fixture
def calls(monkeypatch) -> list[dict]:
    seen = []

    def generate_json(parts, *, system=None, schema=None, params=None):
        seen.append(
            {
                "parts": parts,
                "system": system,
                "schema": schema,
                "params": params,
            }
        )
        return ANSWER

    monkeypatch.setattr(gen, "generate_json", generate_json)
    return seen


def _products() -> list[OutfitProduct]:
    return [
        OutfitProduct(SHIRT, "top", "collar and cuffs visible under the coat"),
        OutfitProduct(SHOES, "footwear"),
    ]


def test_one_call_with_every_product_then_the_target(calls):
    evaluate_outfit(_products(), TARGET)
    (call,) = calls
    top, shoes = PRODUCT_FILTER_HINTS["top"], PRODUCT_FILTER_HINTS["footwear"]
    assert call["parts"] == [
        f"PRODUCT_1 ({top}) - worn under other garments by design: only "
        "collar and cuffs visible under the coat is meant to be visible; "
        "judge that part and treat the covered rest as occluded:",
        media.to_png(SHIRT),
        f"PRODUCT_2 ({shoes}):",
        media.to_png(SHOES),
        "TARGET (the generated image):",
        media.to_png(TARGET),
        "Judge the TARGET and return the JSON object.",
    ]
    assert call["system"] == outfit.OUTFIT_SYSTEM
    assert call["schema"] is outfit.OUTFIT_SCHEMA
    assert call["params"].model == get_models().judge
    assert call["params"].thinking == "MEDIUM"
    assert call["params"].deadline_s == outfit.DEADLINE_S == 90
    assert call["params"].max_output_tokens == 8192


def test_a_video_frame_adds_the_video_note_before_the_ask(calls):
    evaluate_outfit(_products(), TARGET, video_frame=True)
    evaluate_outfit(_products(), TARGET)
    video, still = calls[0]["parts"], calls[1]["parts"]
    assert video[-2] == outfit.VIDEO_FRAME_NOTE
    assert "small text" in video[-2] and "LARGE" in video[-2]
    assert outfit.VIDEO_FRAME_NOTE not in still
    assert video[-1] == still[-1]
    assert calls[0]["params"].deadline_s == outfit.VIDEO_DEADLINE_S == 50
    assert calls[1]["params"].deadline_s == outfit.DEADLINE_S == 90


def test_free_text_filter_and_no_filter_labels(calls):
    evaluate_outfit(
        [OutfitProduct(SHIRT, "the red scarf"), OutfitProduct(SHOES)], TARGET
    )
    parts = calls[0]["parts"]
    assert parts[0] == "PRODUCT_1 (the red scarf):"
    assert parts[2] == "PRODUCT_2:"


def test_verdict_and_details_per_product(calls):
    verdict = evaluate_outfit(_products(), TARGET)
    assert not verdict.publish and not verdict.error
    assert verdict.imperfection_count == 1
    assert verdict.description == "The shoes lost their laces."
    by_index = {p["index"]: p for p in verdict.details["products"]}
    assert by_index[1]["findings"] == [] and by_index[1]["name"] == "shirt"
    assert by_index[2]["defects"] == [DEFECT]
    assert by_index[2]["findings"] == [
        "hardware: laces — laces are missing instead of brown cord laces"
    ]


@pytest.mark.parametrize(
    "fail",
    [RuntimeError("deadline exceeded"), ["not", "an", "object"]],
    ids=["raises", "not-an-object"],
)
def test_a_failed_call_is_a_transient_error(monkeypatch, fail):
    """Asked once more after a failure (``core.gen`` retries a stall within
    a call); failing twice is a transient error."""
    tries = []

    def generate_json(parts, **kw):
        tries.append(1)
        if isinstance(fail, Exception):
            raise fail
        return fail

    monkeypatch.setattr(gen, "generate_json", generate_json)
    verdict = evaluate_outfit(_products(), TARGET)
    assert verdict.error and not verdict.publish
    assert verdict.description.startswith("eval incomplete")
    assert len(tries) == 2


def test_a_judge_that_fails_once_still_judges(monkeypatch):
    """A timeout under load does not throw away the image."""
    tries = []

    def generate_json(parts, **kw):
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("deadline exceeded")
        return ANSWER

    monkeypatch.setattr(gen, "generate_json", generate_json)
    verdict = evaluate_outfit(_products(), TARGET)
    assert not verdict.error
    assert len(tries) == 2


def test_no_products_is_an_input_error(calls):
    with pytest.raises(InputError):
        evaluate_outfit([], TARGET)
    assert calls == []


def test_the_judge_judges_depiction_not_styling():
    assert (
        "how a product is styled is never judged, only how it is depicted"
        in OUTFIT_SYSTEM
    )
    assert "a hem lifted or bunched by a hand in a pocket" in OUTFIT_SYSTEM
    assert "tight or stretched where the product is loose" in OUTFIT_SYSTEM
    assert "self-inconsistent fastening" not in OUTFIT_SYSTEM
    assert "INTENDED STYLING" not in OUTFIT_SYSTEM
    assert "hidden behind a hand, an arm or a carried bag" in OUTFIT_SYSTEM


def test_the_judge_rejects_a_sleeve_or_leg_worn_shorter_or_rolled():
    assert "sleeves rolled or pushed up" not in OUTFIT_SYSTEM
    assert (
        "rolled, pushed up or cuffed where the product photo" in OUTFIT_SYSTEM
    )
    assert "is a defect of category wearing" in OUTFIT_SYSTEM
    assert "unless the product itself is cut asymmetric" in OUTFIT_SYSTEM
    assert "lifted on one leg only, with no hand or pose" in OUTFIT_SYSTEM
    assert "different lengths (SLEEVE AND LEG LENGTH)" in OUTFIT_SYSTEM


def test_the_judge_counts_rows_of_hardware_it_can_see():
    assert "COUNT every row or group of repeated pieces" in OUTFIT_SYSTEM
    assert "a different number is a hardware defect" in OUTFIT_SYSTEM
    assert "the covered pieces are occluded" in OUTFIT_SYSTEM
    assert "count the pieces of one front (OPENED GARMENTS)" in OUTFIT_SYSTEM
    assert "PLANNED VISIBILITY" in OUTFIT_SYSTEM
    assert "judge every part the TARGET shows" in OUTFIT_SYSTEM


RIB = {
    "feature": "fabric surface",
    "category": "fabric_texture",
    "kind": "differs",
    "explanation": "fabric surface is a prominent vertical ribbed knit "
    "instead of a fine smooth jersey",
}


def _answer(monkeypatch, *defects, publish=False, count=None) -> None:
    answer = {
        "products": [
            {"product": "PRODUCT_1", "name": "shirt", "defects": list(defects)}
        ],
        "publish": publish,
        "imperfection_count": len(defects) if count is None else count,
        "description": "The shirt's fabric differs.",
    }
    monkeypatch.setattr(gen, "generate_json", lambda parts, **kw: answer)


def test_the_schema_has_a_texture_category():
    defect = outfit.OUTFIT_SCHEMA["properties"]["products"]["items"]
    categories = defect["properties"]["defects"]["items"]["properties"]
    enum = categories["category"]["enum"]
    assert "fabric_texture" in enum and "fabric_color" in enum
    assert outfit.MINOR == {"fabric_texture"}


@pytest.mark.parametrize("video_frame", [False, True], ids=["image", "video"])
def test_a_texture_only_rejection_is_published_with_a_penalty(
    monkeypatch, video_frame
):
    """The live case: one knit mismatch lowers the score, never rejects."""
    _answer(monkeypatch, RIB, count=0)
    verdict = evaluate_outfit(_products(), TARGET, video_frame=video_frame)
    assert verdict.publish and not verdict.error
    assert verdict.imperfection_count == 1  # at least one per minor defect
    assert verdict.description == (
        f"The shirt's fabric differs. {outfit.MINOR_NOTE}"
    )
    (shirt,) = verdict.details["products"]
    assert (
        shirt["minor"]
        == shirt["findings"]
        == [
            "fabric_texture: fabric surface — fabric surface is a prominent "
            "vertical ribbed knit instead of a fine smooth jersey"
        ]
    )


@pytest.mark.parametrize(
    "other",
    ["fabric_color", "silhouette", "identity_text", "hardware", "other"],
)
def test_any_other_defect_next_to_a_texture_one_still_rejects(
    monkeypatch, other
):
    _answer(monkeypatch, RIB, {**RIB, "category": other})
    verdict = evaluate_outfit(_products(), TARGET)
    assert not verdict.publish
    assert verdict.imperfection_count == 2
    assert verdict.description == "The shirt's fabric differs."
    (shirt,) = verdict.details["products"]
    assert len(shirt["findings"]) == 2 and len(shirt["minor"]) == 1


def test_a_rejection_naming_no_defect_still_rejects(monkeypatch):
    _answer(monkeypatch)
    verdict = evaluate_outfit(_products(), TARGET)
    assert not verdict.publish and verdict.imperfection_count == 0


@pytest.mark.parametrize(("count", "expected"), [(0, 1), (3, 3)])
def test_a_published_answer_counts_its_minor_defects(
    monkeypatch, count, expected
):
    _answer(monkeypatch, RIB, publish=True, count=count)
    verdict = evaluate_outfit(_products(), TARGET)
    assert verdict.publish and verdict.imperfection_count == expected
    assert verdict.description == "The shirt's fabric differs."


def test_the_judge_separates_texture_from_colour_and_pattern():
    assert "TEXTURE OR PATTERN" in OUTFIT_SYSTEM
    assert "a rib knit instead of a smooth jersey" in OUTFIT_SYSTEM
    assert "A defect that is both is fabric_color." in OUTFIT_SYSTEM
    assert "fabric_texture defects never decide publish" in OUTFIT_SYSTEM
    assert "including a wrong fabric surface" not in OUTFIT_SYSTEM


WEARER = png(color=(3, 3, 3))
BUMP = "the belly is flat instead of a visible pregnancy bump."


def test_a_wearer_is_shown_after_the_target_with_the_body_rules(calls):
    """After the TARGET: the products are compared with the TARGET as
    without a wearer (the live re-judge: fewer product rejections)."""
    evaluate_outfit(_products(), TARGET, wearer=WEARER)
    (call,) = calls
    assert call["parts"][4:] == [
        "TARGET (the generated image):",
        media.to_png(TARGET),
        "WEARER (the person before the try-on, in their own clothes):",
        media.to_png(WEARER),
        outfit.WEARER_NOTE,
        "Judge the TARGET and return the JSON object.",
    ]
    assert call["system"] == outfit.OUTFIT_SYSTEM
    schema = call["schema"]
    assert schema is outfit.WEARER_SCHEMA
    assert {"body_defects", "body_parts"} <= set(schema["required"])
    assert (
        schema["properties"]["products"]
        is (outfit.OUTFIT_SCHEMA["properties"]["products"])
    )


FACE = png(color=(4, 4, 4))


def test_the_face_crop_is_shown_next_to_the_wearer(calls):
    evaluate_outfit(_products(), TARGET, wearer=WEARER, face=FACE)
    (call,) = calls
    assert call["parts"][6:] == [
        "WEARER (the person before the try-on, in their own clothes):",
        media.to_png(WEARER),
        "FACE (a close crop of the WEARER's face, from the same photo):",
        media.to_png(FACE),
        outfit.WEARER_NOTE,
        "Judge the TARGET and return the JSON object.",
    ]
    assert call["schema"] is outfit.WEARER_SCHEMA


def test_no_wearer_sends_no_wearer_rules(calls):
    evaluate_outfit(_products(), TARGET, video_frame=True, face=FACE)
    (call,) = calls
    assert call["schema"] is outfit.OUTFIT_SCHEMA
    assert "body_defects" not in outfit.OUTFIT_SCHEMA["properties"]
    assert "body_parts" not in outfit.OUTFIT_SCHEMA["properties"]
    assert outfit.WEARER_NOTE not in call["parts"]
    assert not any("WEARER" in str(p) for p in call["parts"])
    assert media.to_png(FACE) not in call["parts"]  # no face without one


def test_the_wearer_rules_check_the_body_and_the_face():
    note = outfit.WEARER_NOTE
    assert note.startswith(
        "WEARER is the person before the try-on, in their own clothes."
    )
    assert "pregnancy, limb differences, prostheses" in note
    assert "wheelchair or other aids, seated or standing" in note
    assert "Reject when a body part, prosthesis or aid is added" in note
    assert "when a garment shows what it would cover" in note
    assert "FACE, when given, is a close crop of the WEARER's face" in note
    assert "when the TARGET's face is not the WEARER's person" in note
    assert "a model in a PRODUCT photo" in note
    assert "glasses or other eyewear may hide the eyes" in note
    assert "body_defects" in note
    assert (
        "at the same position in body_parts, the body part or feature it "
        "concerns in 1-4 words"
    ) in note
    assert "Never judge the products against the wearer's own clothes" in note
    assert "exactly as you would without the WEARER" in note
    assert "WEARER" not in OUTFIT_SYSTEM


def test_a_bare_chest_or_underwear_on_show_is_a_body_defect():
    """With the other body checks, before they are written as body
    defects; a product worn that way by design is not one."""
    assert (
        "Reject too when the TARGET shows a bare chest or breasts or "
        "underwear on show (a strap at a neckline is not on show), unless a "
        "PRODUCT photo shows that product itself "
        "worn that way as its design (a swimwear or underwear product, for "
        "example)."
    ) in outfit.WEARER_NOTE
    note = outfit.WEARER_NOTE
    assert note.index("Reject too when the TARGET shows a bare chest") < (
        note.index("Write each such defect in body_defects")
    )
    assert "bare chest" not in OUTFIT_SYSTEM


@pytest.mark.parametrize(
    "defects",
    [[], [RIB]],
    ids=["published-by-the-model", "texture-only"],
)
def test_a_body_defect_rejects_and_says_so(monkeypatch, defects):
    """A body defect rejects even when the model publishes or lists only a
    minor product defect; the description says it first."""
    answer = {
        "products": [
            {"product": "PRODUCT_1", "name": "shirt", "defects": defects}
        ],
        "publish": not defects,
        "imperfection_count": len(defects),
        "description": "The shirt is right.",
        "body_defects": [BUMP],
        "body_parts": ["belly"],
    }
    monkeypatch.setattr(gen, "generate_json", lambda parts, **kw: answer)
    verdict = evaluate_outfit(_products(), TARGET, wearer=WEARER)
    assert not verdict.publish and not verdict.error
    assert verdict.description == (
        "Body defect: the belly is flat instead of a visible pregnancy "
        "bump. The shirt is right."
    )
    assert verdict.description.startswith(outfit.BODY_NOTE)
    assert verdict.details["body_defects"] == [BUMP.strip(" .")]
    assert verdict.details["body_parts"] == ["belly"]


@pytest.mark.parametrize(
    ("defects", "parts", "body", "named"),
    [
        (
            [BUMP, " ", "x"],
            ["belly.", "face"],
            [BUMP.strip(" ."), "x"],
            ["belly", ""],
        ),
        ([BUMP], None, [BUMP.strip(" .")], [""]),
        ([], None, [], []),
    ],
    ids=["aligned", "old-answer", "none"],
)
def test_body_parts_stay_aligned_with_the_body_defects(
    monkeypatch, defects, parts, body, named
):
    """A body part per kept body defect, '' when the answer names none."""
    answer = {**ANSWER, "body_defects": defects, "body_parts": parts}
    monkeypatch.setattr(gen, "generate_json", lambda parts, **kw: answer)
    verdict = evaluate_outfit(_products(), TARGET, wearer=WEARER)
    assert verdict.details["body_defects"] == body
    assert verdict.details["body_parts"] == named


def test_no_body_defect_keeps_the_verdict(monkeypatch):
    _answer(monkeypatch, RIB, count=0)
    verdict = evaluate_outfit(_products(), TARGET, wearer=WEARER)
    assert verdict.publish
    assert outfit.BODY_NOTE not in verdict.description


def test_the_pose_and_the_garment_cut_are_judged_for_this_wearer():
    """Standing vs seated is a body defect; a pose, build or camera angle
    that moves or reshapes a garment is not; a half-tucked top is."""
    from genmedia4commerce.eval.outfit import WEARER_NOTE as note

    assert "Reject when the wearer stands instead of sitting" in note
    assert "arm and leg positions may differ" in note
    assert "A pose naturally moves the garments" in note
    assert "a wide leg drawn slim" in note
    assert "a top half tucked in and half left out is a defect" in note
    assert note.index("A pose naturally moves") < note.index(
        "Write each such defect in body_defects"
    )


def test_identity_includes_the_hair_and_the_apparent_age():
    """Hair or an age from a product model is a body defect, also when the
    face shape matches."""
    from genmedia4commerce.eval.outfit import WEARER_NOTE as note

    assert "identity includes the hair (its colour, length and texture" in note
    assert "and the apparent age" in note
    assert "even when the face shape matches" in note
    assert note.index("identity includes the hair") < note.index(
        "Write each such defect in body_defects"
    )
