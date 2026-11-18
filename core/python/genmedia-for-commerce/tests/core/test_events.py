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

import base64
import json
import mimetypes

import pytest

from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    FRAMING_BY_CATEGORY,
    Classification,
    Product,
    ProductRef,
    Verdict,
    quality_score,
)


def test_variation_to_dict_base64_and_mime():
    image = png()
    v = Verdict(True, 1, "ok", details={"score": 5.0, "findings": ["a"]})
    ev = VariationEvent(
        index=2,
        status="ready",
        media=image,
        verdict=v,
        score=5.0,
        extra={"view": "front"},
    )
    d = ev.to_dict()
    assert d["type"] == "variation"
    assert "media" not in d
    assert base64.b64decode(d["media_base64"]) == image
    assert d["media_mime"] == "image/png"
    assert d["verdict"] == {
        "publish": True,
        "imperfection_count": 1,
        "description": "ok",
        "error": False,
        "details": {"score": 5.0, "findings": ["a"]},
    }
    assert (
        d["attempt"] == 1
        and d["error"] is None
        and d["extra"] == {"view": "front"}
    )
    json.dumps(d)


def test_variation_without_media():
    ev = VariationEvent(
        index=0, status="error", media=None, verdict=None, score=0.0, error="x"
    )
    d = ev.to_dict()
    assert (
        d["media_base64"] is None
        and d["media_mime"] is None
        and d["verdict"] is None
    )
    assert d["media_uri"] is None and d["media_url"] is None


def test_variation_inline_has_null_uri_and_url():
    d = VariationEvent(
        index=0, status="ready", media=png(), verdict=None, score=1.0
    ).to_dict()
    assert d["media_base64"] and d["media_mime"] == "image/png"
    assert d["media_uri"] is None and d["media_url"] is None


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("image-front-0.png", "image/png"),
        ("video-1.mp4", "video/mp4"),
        ("2.jpg", "image/jpeg"),
        ("2.webp", "image/webp"),
        ("2.bin", "application/octet-stream"),
    ],
)
def test_variation_in_gcs_has_uri_url_and_mime(name, mime):
    uri = f"gs://b/outputs/2026-01-02/r/{name}"
    ev = VariationEvent(
        index=0,
        status="ready",
        media=None,
        verdict=None,
        score=1.0,
        media_uri=uri,
        media_url="/api/media?uri=x",
    )
    d = ev.to_dict()
    assert d["media_base64"] is None
    assert d["media_uri"] == uri
    assert d["media_url"] == "/api/media?uri=x"
    assert d["media_mime"] == mime
    json.dumps(d)


def test_a_saved_spin_is_webp_without_a_system_mime_table(monkeypatch):
    # Python before 3.13 does not know .webp without one (the Docker image).
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    uri = "gs://b/runs/2026-01-02/s/r/outputs/video-0.webp"
    ev = VariationEvent(0, "ready", None, None, 1.0, media_uri=uri)
    assert ev.to_dict()["media_mime"] == "image/webp"


def test_verdict_round_trips():
    v = Verdict(
        False, 3, "bad", error=True, details={"per_view": [{"view": "front"}]}
    )
    ev = VariationEvent(
        index=0, status="error", media=b"\x00", verdict=v, score=1.0
    )
    assert Verdict(**ev.to_dict()["verdict"]) == v


def test_to_sse_format():
    ev = ProgressEvent("preprocessed", "done")
    sse = ev.to_sse()
    assert sse.startswith("data: ") and sse.endswith("\n\n")
    assert json.loads(sse[len("data: ") :]) == {
        "type": "progress",
        "stage": "preprocessed",
        "message": "done",
        "data": {},
    }


def test_progress_data_is_serialised():
    ev = ProgressEvent(
        "image_selected", "m", data={"front_index": 2, "back_index": None}
    )
    assert ev.to_dict() == {
        "type": "progress",
        "stage": "image_selected",
        "message": "m",
        "data": {"front_index": 2, "back_index": None},
    }
    assert ProgressEvent("a", "A").data == {}
    assert ProgressEvent("a", "A").data is not ProgressEvent("b", "B").data


def test_event_types_and_dicts():
    assert ClassifiedEvent({"mode": "single"}).to_dict() == {
        "type": "classified",
        "payload": {"mode": "single"},
    }
    assert CompleteEvent([1, 0], 1).to_dict() == {
        "type": "complete",
        "ranked_indices": [1, 0],
        "best_index": 1,
    }
    assert ErrorEvent("boom").to_dict() == {
        "type": "error",
        "message": "boom",
        "retryable": False,
    }
    with pytest.raises(TypeError, match="message"):
        ProgressEvent("back")  # every progress event says what is going on


def test_classification_to_dict_has_no_bytes():
    product = Product(
        id="A",
        category="eyewear",
        description="round glasses",
        refs=[ProductRef(b"img1", "front", 6), ProductRef(b"img2", "front", 9)],
    )
    c = Classification(mode="single", products=[product], framing="head")
    d = c.to_dict()
    assert d == {
        "mode": "single",
        "framing": "head",
        "products": [
            {
                "id": "A",
                "category": "eyewear",
                "description": "round glasses",
                "refs": [
                    {"view": "front", "rank": 6},
                    {"view": "front", "rank": 9},
                ],
            }
        ],
    }
    assert "img1" not in json.dumps(ClassifiedEvent(d).to_dict())


def test_classified_event_keeps_the_classification_off_the_wire():
    ref = ProductRef(b"photo-bytes", "front", 8)
    c = Classification(
        "single", [Product("A", "top", "t", [ref])], "upper_body"
    )
    event = ClassifiedEvent(c.to_dict(), classification=c)
    out = event.to_dict()
    assert set(out) == {"type", "payload"}
    assert out == {"type": "classified", "payload": c.to_dict()}
    wire = json.dumps(out)
    assert base64.b64encode(b"photo-bytes").decode() not in wire
    assert "photo-bytes" not in wire and "photo-bytes" not in repr(event)
    assert event == ClassifiedEvent(c.to_dict())
    assert event.classification is c


def test_refs_for_sorted_by_rank_then_upload_order():
    p = Product(
        "A",
        "top",
        "shirt",
        [
            ProductRef(b"1", "front", 2),
            ProductRef(b"2", "back", 1),
            ProductRef(b"3", "front", 1),
            ProductRef(b"4", "front", 2),
        ],
    )
    assert [r.image for r in p.refs_for("front")] == [b"3", b"1", b"4"]
    assert [r.image for r in p.refs_for("back")] == [b"2"]
    assert p.refs_for("other") == []


def test_framing_by_category_complete():
    assert FRAMING_BY_CATEGORY["jewelry_hand"] == "hands"
    assert len(FRAMING_BY_CATEGORY) == 11


def test_ref_priority_worn_upload_then_flat_then_crop():
    worn = ProductRef(b"w", "front", 5, presentation="worn")
    crop = ProductRef(
        b"c", "front", 9, presentation="worn", source="model_crop"
    )
    flat = ProductRef(b"f", "front", 10)
    assert (worn.priority, flat.priority, crop.priority) == (1, 2, 3)
    p = Product("A", "bottom", "jeans", [flat, crop, worn])
    assert [r.image for r in p.refs_for("front")] == [b"w", b"f", b"c"]
    assert p.best_ref("front") is worn


def test_best_ref_any_view_and_rank_tiebreak():
    a = ProductRef(b"a", "back", 2)
    b = ProductRef(b"b", "front", 1)
    p = Product("A", "top", "shirt", [a, b])
    assert p.best_ref() is b
    assert p.best_ref("other") is None


@pytest.mark.parametrize(
    ("publish", "imperfections", "expected"),
    [
        (True, 0, 100.0),
        (False, 0, 70.0),
        (True, 1, 85.0),
        (True, 2, 70.0),
        (False, 2, 40.0),
        (False, 5, 0.0),
        (False, 50, 0.0),
    ],
)
def test_quality_score(publish, imperfections, expected):
    score = quality_score(publish, imperfections)
    assert isinstance(score, float)
    assert score == expected
    assert 0.0 <= score <= 100.0


def test_quality_score_bounds_over_the_whole_range():
    scores = [
        quality_score(publish, n)
        for publish in (True, False)
        for n in range(12)
    ]
    assert min(scores) == 0.0
    assert max(scores) == 100.0
