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

"""Fixtures for the Video Try-On tests: images, classification, fake media."""

from dataclasses import dataclass, field, replace

import pytest

from genmedia4commerce.core import media, video
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Category,
    Classification,
    Framing,
    Product,
    ProductRef,
    Verdict,
)
from genmedia4commerce.eval import identity, orientation, outfit, unseen
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.video_tryon import branch
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    ReferenceSet,
    Shot,
)

TRYON = png(100, 200, (10, 20, 30))
BACK = png(100, 200, (40, 50, 60))
FACE = png(30, 30, (70, 80, 90))
CLIP = b"\x00\x00\x00\x18ftypmp42fake-clip"
FRAMES = [png(8, 8, (i, i, i)) for i in (10, 20, 30)]


def product_png(i: int) -> bytes:
    """Return a distinct small product image."""
    return png(20 + i, 20, (100 + i, 0, 0))


def classification(
    framing: Framing, category: Category = "footwear"
) -> Classification:
    """Return a single-product classification with the given framing."""
    ref = ProductRef(image=product_png(0), view="front", rank=1)
    product = Product(id="A", category=category, description="item", refs=[ref])
    return Classification(mode="single", products=[product], framing=framing)


def passed() -> Verdict:
    """A judge verdict with nothing wrong."""
    return Verdict(True, details={"findings": []})


@dataclass
class FakeEval:
    """Canned face crop, identity answer, still frames, view labels, blind
    spots and judge verdicts.

    The clip is 7.5 s at 0.4 fps: ``FRAMES`` are its frames 0, 1, 2 at 0,
    2.5 and 5 s, all stable and, by default, all still.
    """

    face_crop: bytes | BaseException | None = FACE
    # identity.same_person's answer (same_person, reason), or its error
    identity: tuple[bool, str] | BaseException = (True, "same person")
    still: list[int] = field(default_factory=lambda: [0, 1, 2])
    views: list[str] | None = None  # per judged frame; None: all "front"
    unseen: list[int] = field(default_factory=list)  # blind-spot frames
    # (product_filter, frame number in FRAMES) -> verdict; default passed()
    verdicts: dict[tuple, Verdict | BaseException] = field(default_factory=dict)
    identity_calls: list[tuple[list[bytes], bytes]] = field(
        default_factory=list
    )
    unseen_calls: list[list[bytes]] = field(default_factory=list)
    label_calls: list[dict] = field(default_factory=list)
    judge_calls: list[dict] = field(default_factory=list)
    outfit_calls: list[tuple] = field(default_factory=list)  # per frame

    def unseen_frames(self, clip, references):
        self.unseen_calls.append(list(references))
        return list(self.unseen), "the back" if self.unseen else ""

    def crop_face(self, data, **_kwargs):
        if isinstance(self.face_crop, BaseException):
            raise self.face_crop
        return self.face_crop

    def same_person(self, frames, reference_face):
        self.identity_calls.append((list(frames), reference_face))
        if isinstance(self.identity, BaseException):
            raise self.identity
        return self.identity

    def stillest_frames(self, clip, **_kwargs):
        return [(i, FRAMES[i]) for i in self.still]

    def label_frames(self, frames, **kwargs):
        self.label_calls.append({"frames": list(frames), **kwargs})
        return list(self.views or ["front"] * len(frames))

    def judge(self, references, target, **kwargs):
        self.judge_calls.append(
            {"references": list(references), "target": target, **kwargs}
        )
        key = (kwargs.get("product_filter"), FRAMES.index(target))
        verdict = self.verdicts.get(key) or passed()
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    def outfit(self, products, target, *, video_frame=False, wearer=None):
        """Stands in for ``outfit.evaluate_outfit`` (one call per frame):
        each product is answered by ``judge``; any error is the frame's."""
        self.outfit_calls.append(
            (FRAMES.index(target), [p.product_filter for p in products])
        )
        verdicts = [
            self.judge(
                [p.reference],
                target,
                product_filter=p.product_filter,
                ignore_text_defects=video_frame,
            )
            for p in products
        ]
        errored = next((v for v in verdicts if v.error), None)
        if errored:
            return errored
        found = [
            v.details.get("findings") or ([] if v.publish else [v.description])
            for v in verdicts
        ]
        return Verdict(
            all(v.publish for v in verdicts),
            sum(v.imperfection_count for v in verdicts),
            "; ".join(v.description for v in verdicts if not v.publish),
            details={
                "products": [
                    {"index": k, "findings": f}
                    for k, f in enumerate(found, start=1)
                ]
            },
        )


@pytest.fixture
def fake_eval(monkeypatch) -> FakeEval:
    """Fake the face crop, the frame pickers, the identity check, the view
    labels, the blind spots and the product judge."""
    fake = FakeEval()
    monkeypatch.setattr(media, "crop_face", fake.crop_face)
    monkeypatch.setattr(
        video, "pick_stable_frames", lambda clip, **_kw: list(enumerate(FRAMES))
    )
    monkeypatch.setattr(video, "stillest_frames", fake.stillest_frames)
    monkeypatch.setattr(video, "probe", lambda clip: (3, 0.4, 7500))
    monkeypatch.setattr(identity, "same_person", fake.same_person)
    monkeypatch.setattr(orientation, "label_frames", fake.label_frames)
    monkeypatch.setattr(unseen, "unseen_frames", fake.unseen_frames)
    monkeypatch.setattr(outfit, "evaluate_outfit", fake.outfit)
    return fake


@dataclass
class FakePlanner:
    """Stands in for ``plan.plan_animation``: one 8 s pose by default."""

    plan: Plan | BaseException = field(
        default_factory=lambda: Plan([Shot("pose", 8.0)])
    )
    calls: list[tuple[ReferenceSet, str | None]] = field(default_factory=list)

    def __call__(self, refs, request=None):
        self.calls.append((refs, request))
        if isinstance(self.plan, BaseException):
            raise self.plan
        return self.plan


@pytest.fixture
def videos_per_image(monkeypatch):
    """Set ``GENMEDIA_VIDEOS_PER_IMAGE`` (fresh settings); default 2."""

    def set_count(n: int) -> None:
        monkeypatch.setenv("GENMEDIA_VIDEOS_PER_IMAGE", str(n))
        get_settings.cache_clear()

    return set_count


@pytest.fixture(autouse=True)
def blurred(monkeypatch) -> list[tuple[bytes, bool]]:
    """Fake ``media.blur_face`` (no Cloud Vision); records (image,
    keep_eyes) and returns the image unchanged."""
    calls: list[tuple[bytes, bool]] = []

    def blur(image, *, keep_eyes=False):
        calls.append((image, keep_eyes))
        return image

    monkeypatch.setattr(media, "blur_face", blur)
    return calls


@pytest.fixture(autouse=True)
def planner(monkeypatch) -> FakePlanner:
    """The pipeline's planner, faked (``test_plan`` tests the real one)."""
    fake = FakePlanner()
    monkeypatch.setattr(branch, "plan_animation", fake)
    return fake


@dataclass
class FakeDetails:
    """Stands in for ``details.find`` (no detail by default; each call
    recorded as (images, limit, product)) and ``details.upscale`` (each
    close-up's image gets " 2K" appended)."""

    found: list[details.Detail] = field(default_factory=list)
    calls: list[tuple[list[bytes], int, str]] = field(default_factory=list)
    upscaled: list[list[details.Detail]] = field(default_factory=list)

    def __call__(self, images, *, limit=2, upscale=True, product=""):
        self.calls.append((list(images), limit, product))
        return self.found[:limit]

    def upscale(self, found):
        self.upscaled.append(list(found))
        return [replace(d, image=d.image + b" 2K") for d in found]


@pytest.fixture(autouse=True)
def zoom(monkeypatch) -> FakeDetails:
    """The detail finder and upscaler, faked
    (``tests/workflows/test_details.py`` tests the real ones)."""
    fake = FakeDetails()
    monkeypatch.setattr(details, "find", fake)
    monkeypatch.setattr(details, "upscale", fake.upscale)
    return fake
