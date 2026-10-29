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

"""A product photo worn by a person, turned into the same photo on a
featureless grey mannequin, with every product pixel kept from the original.

An image model edits the photo (``PROMPT``): the person becomes a matte
light-grey mannequin in the same pose. The model redraws the whole photo
and gets small things slightly wrong (lettering, gems, stitching), so the
edit is pasted back onto the original (``paste_back``): the original
everywhere except where the person was.

The paste-back aligns the edit to the original (a plain resize, or a SIFT
homography when it matches the original better outside the person), then
takes the edit only where it turned a coloured pixel into neutral grey (the
mannequin), plus what is linked to it: changes next to it in the person's
own colours (hair, skin), skin the grey grew over, and large solid
differences touching it (a ponytail, hair over a collar). Thin or small
differences (letters, stripes, seams) and anything in the product's colours
stay original. The mask edge is feathered.

Self-contained (the standard library, ``google-genai``, NumPy, OpenCV and
Pillow): copy this file into any project. Usage::

    from google import genai
    import mannequin

    client = genai.Client(vertexai=True, project="my-project",
                          location="global")
    photo = open("worn.jpg", "rb").read()
    png = mannequin.normalize(photo, client=client, model="<image model>")

or, with an edit already made, ``mannequin.paste_back(photo, edit)``. From
the command line (Vertex AI; ``--project`` and ``--location`` default to
``GOOGLE_CLOUD_PROJECT`` and ``GOOGLE_CLOUD_LOCATION``, else "global")::

    python mannequin.py worn.jpg mannequin.png --model <image model>

Bump ``VERSION`` whenever ``PROMPT`` or the paste-back changes: caches of
converted photos key on it.
"""

import argparse
import io
import logging
import math
import os
from pathlib import Path

import cv2
import numpy as np
from google import genai
from google.genai import types
from PIL import Image, ImageOps

__all__ = ["PROMPT", "VERSION", "normalize", "paste_back"]

logger = logging.getLogger(__name__)

PROMPT = (
    "Edit this photo. Replace the person with a featureless mannequin: "
    "smooth matte light-grey surface, no face, no hair, no skin, no nails, "
    "no makeup. The mannequin takes exactly the person's pose, proportions "
    "and body size, so everything worn sits, folds and drapes exactly as in "
    "the photo. Every product stays exactly as it is: shape, length, fit, "
    "colour, material, texture, folds, stitching, prints, lettering, logos, "
    "hardware, and its position and size in the frame. Wherever the person "
    "showed (face, hair, neck, hands, legs), the mannequin's surface shows "
    "instead; a product that covered hair keeps its own shape. When the "
    "photo shows only part of the body, that part becomes the mannequin's. "
    "Keep the framing, crop, camera angle, background and light exactly as "
    "they are. Change nothing else."
)
VERSION = "1"

# Mask: ΔE (CIELAB) thresholds, the edit's neutral grey, sizes in px.
DE = 15.0  # a pixel the edit changed this much and made grey: the person
CHROMA = 10.0  # the edit's chroma under this (and L over MIN_L): grey
MIN_L = 30.0
GROW = 6  # the hard mask grows by this before the feather
FEATHER = 4.0  # sigma of the mask's soft edge
MIN_AREA = 0.0004  # smallest core region, a share of the photo
NEAR = 35  # changes within this of the core may join it
NEAR_DE = 6.0  # a change at all
ORIG_CHROMA = 5.0  # the original has colour (skin; never white/grey/black)
BIG_DE = 15.0  # big blobs: ΔE between the photos blurred by BLUR
OPEN_R = 9  # a blob survives an opening with a disc of this radius
MIN_BLOB = 0.001  # smallest blob, a share of the photo
BLUR = 4.0
_CONFIG = types.GenerateContentConfig(
    response_modalities=["IMAGE"],
    image_config=types.ImageConfig(
        image_size="2K", output_mime_type="image/png"
    ),
    thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
    media_resolution="MEDIA_RESOLUTION_HIGH",
    temperature=0.1,
)
_SENT = {"JPEG", "PNG", "WEBP"}  # formats sent as they are


def _lab(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def _chroma(lab: np.ndarray) -> np.ndarray:
    return np.hypot(lab[..., 1], lab[..., 2])


def _disc(radius: int) -> np.ndarray:
    size = (2 * radius + 1,) * 2
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, size)


def _square(side: int) -> np.ndarray:
    return np.ones((side, side), np.uint8)


def _gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(float)


ALIGN_SIDE = 1600  # px: the homography is fitted on copies this long


def _small(img: np.ndarray, scale: float) -> np.ndarray:
    """``img`` as grey, scaled by ``scale`` (area resampling)."""
    grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if scale >= 1:
        return grey
    return cv2.resize(
        grey, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
    )


def _homography(orig: np.ndarray, edit: np.ndarray) -> np.ndarray | None:
    """The edit's homography onto the original (SIFT, ratio test, RANSAC),
    fitted on copies at most ``ALIGN_SIDE`` long and scaled back: the edit
    redraws fine texture, whose keypoints only add noise (a knit close-up
    gave 110,000 of them and took 40 s to match). None with too few
    matches or a fit that rotates more than 5° or has any real
    perspective."""
    scale = min(1.0, ALIGN_SIDE / max(orig.shape[:2]))
    sift = cv2.SIFT_create()
    k1, d1 = sift.detectAndCompute(_small(orig, scale), None)
    k2, d2 = sift.detectAndCompute(_small(edit, scale), None)
    if d1 is None or d2 is None:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(d2, d1, k=2)
    good = [
        p[0]
        for p in pairs
        if len(p) == 2 and p[0].distance < 0.75 * p[1].distance
    ]
    if len(good) <= 20:
        return None
    src = np.float32([k2[m.queryIdx].pt for m in good])
    dst = np.float32([k1[m.trainIdx].pt for m in good])
    fit, _ = cv2.findHomography(src, dst, cv2.RANSAC, 5.0 * scale)
    if fit is None:
        return None
    s = np.diag([scale, scale, 1.0])
    fit = np.linalg.inv(s) @ fit @ s
    perspective = math.hypot(fit[2, 0], fit[2, 1])
    rotation = math.degrees(math.atan2(fit[1, 0], fit[0, 0]))
    if perspective > 1e-3 or abs(rotation) > 5.0:
        return None
    return fit


def _keep_big(mask: np.ndarray) -> np.ndarray:
    """The 8-connected regions of ``mask`` of at least ``MIN_AREA``."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= MIN_AREA * mask.size
    return keep[labels].astype(np.uint8)


def _diff(orig: np.ndarray, al: np.ndarray):
    """Both photos in Lab, their ΔE, and where the edit is neutral grey."""
    lo, la = _lab(orig), _lab(al)
    de = np.linalg.norm(lo - la, axis=2)
    neutral = (_chroma(la) < CHROMA) & (la[..., 0] > MIN_L)
    return lo, la, de, neutral


def _core(de: np.ndarray, neutral: np.ndarray) -> np.ndarray:
    """Pixels the edit changed a lot and made grey, in big regions."""
    raw = ((de > DE) & neutral).astype(np.uint8)
    return _keep_big(cv2.morphologyEx(raw, cv2.MORPH_OPEN, _square(3)))


def _residual(orig: np.ndarray, al: np.ndarray, excl: np.ndarray) -> float:
    """Mean grey difference outside ``excl`` (the person)."""
    if excl.all():
        return 0.0
    return float(np.abs(_gray(orig) - _gray(al))[~excl].mean())


def _align(orig: np.ndarray, edit: Image.Image) -> np.ndarray:
    """The edit at the original's size: resized, or warped by its
    homography when that matches the original better outside the person."""
    h, w = orig.shape[:2]
    resized = np.asarray(
        edit.convert("RGB").resize((w, h), Image.Resampling.LANCZOS)
    )
    fit = _homography(orig, resized)
    if fit is None:
        return resized
    warped = cv2.warpPerspective(
        resized,
        fit,
        (w, h),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE,
    )
    candidates = [resized, warped]
    cores = [_core(*_diff(orig, a)[2:]) for a in candidates]
    excl = cv2.dilate(np.maximum.reduce(cores), _square(41)).astype(bool)
    return min(candidates, key=lambda a: _residual(orig, a, excl))


def _centres(px: np.ndarray, k: int = 8) -> np.ndarray:
    """k-means colour centres of up to 6000 of ``px``."""
    rng = np.random.default_rng(0)
    pick = rng.choice(len(px), min(6000, len(px)), replace=False)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    sample = px[pick].astype(np.float32)
    return cv2.kmeans(sample, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)[2]


def _nearest(px: np.ndarray, centres: np.ndarray) -> np.ndarray:
    """Each pixel's ΔE to its nearest centre (``px``: any shape, Lab last)."""
    d = np.full(px.shape[:-1], 1e9, np.float32)
    for c in centres:
        d = np.minimum(d, np.linalg.norm(px - c, axis=-1))
    return d


def _person_linked(
    lo: np.ndarray, hard: np.ndarray, zone_px: int = 31
) -> np.ndarray:
    """Pixels within ``zone_px`` of ``hard`` (the person) whose colour is
    nearer the person's colours than the product's, connected to ``hard``
    (and ``hard`` itself); none when either sample is under 500 px."""
    zone = cv2.dilate(hard, _disc(zone_px)) > 0
    background = (_chroma(lo) < 6) & (lo[..., 0] > 80)
    far = ~cv2.dilate(hard, _disc(40)).astype(bool)
    product = far & ~background
    if hard.sum() < 500 or product.sum() < 500:
        return np.zeros(hard.shape, bool)
    theirs, mine = _centres(lo[product]), _centres(lo[hard > 0])
    near = zone & (hard == 0)  # the only pixels the test decides
    nearer = np.zeros(hard.shape, bool)
    nearer[near] = _nearest(lo[near], mine) < _nearest(lo[near], theirs)
    linked = (nearer | (hard > 0)).astype(np.uint8)
    _, labels = cv2.connectedComponents(linked, 8)
    seeds = np.unique(labels[hard > 0])
    return np.isin(labels, seeds[seeds > 0])


def _mask(lo: np.ndarray, de: np.ndarray, neutral: np.ndarray) -> tuple:
    """The person's mask: (hard mask, its feathered version)."""
    core = _core(de, neutral)
    add = cv2.dilate(core, _disc(NEAR)) & (de > NEAR_DE)
    add = add & (neutral | _person_linked(lo, core, zone_px=NEAR))
    m = (core | add).astype(np.uint8)
    cand = neutral & (_chroma(lo) > ORIG_CHROMA) & (de > NEAR_DE)
    both = cand.astype(np.uint8) | m
    _, labels = cv2.connectedComponents(both, 8)
    touch = np.unique(labels[m > 0])
    grown = np.isin(labels, touch[touch > 0]) & both.astype(bool)
    hard = cv2.morphologyEx(grown.astype(np.uint8), cv2.MORPH_OPEN, _square(3))
    hard = cv2.morphologyEx(hard, cv2.MORPH_CLOSE, _square(9))
    grown = cv2.dilate(hard, _square(2 * GROW + 1)).astype(np.float32)
    return hard, cv2.GaussianBlur(grown, (0, 0), FEATHER)


def _big_blobs(orig: np.ndarray, al: np.ndarray, soft: np.ndarray) -> tuple:
    """Large solid differences touching the mask: (them, the new soft
    mask)."""
    m3 = (soft > 0.5).astype(np.uint8)
    blur = [cv2.GaussianBlur(a, (0, 0), BLUR) for a in (orig, al)]
    de = np.linalg.norm(_lab(blur[0]) - _lab(blur[1]), axis=2)
    big = (de > BIG_DE).astype(np.uint8)
    big = cv2.morphologyEx(big, cv2.MORPH_OPEN, _disc(OPEN_R))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(big, 8)
    touch = np.unique(labels[cv2.dilate(m3, _square(5)) > 0])
    keep = stats[:, cv2.CC_STAT_AREA] >= MIN_BLOB * big.size
    keep &= np.isin(np.arange(n), touch)
    keep[0] = False
    added = keep[labels].astype(np.uint8) & (1 - m3)
    m = cv2.morphologyEx(m3 | added, cv2.MORPH_CLOSE, _square(9))
    m = cv2.dilate(m, _square(5)).astype(np.float32)
    return added, cv2.GaussianBlur(m, (0, 0), FEATHER)


def _blend(orig: np.ndarray, al: np.ndarray) -> np.ndarray:
    """The original with the aligned edit where the person was."""
    lo, _, de, neutral = _diff(orig, al)
    hard, soft = _mask(lo, de, neutral)
    added, soft = _big_blobs(orig, al, soft)
    ok = (
        neutral
        | (de < NEAR_DE)
        | (cv2.dilate(added, _square(5)) > 0)
        | _person_linked(lo, hard | added)
    )  # the soft edge takes the edit only where it is grey or the person's
    soft = soft * cv2.GaussianBlur(ok.astype(np.float32), (0, 0), 1.0)
    weight = soft[..., None]
    return (al * weight + orig * (1 - weight)).astype(np.uint8)


def _rgb(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def paste_back(original: bytes, edit: bytes) -> bytes:
    """``edit`` (the mannequin edit of ``original``) pasted back onto
    ``original``: a PNG at the original's size (EXIF orientation applied),
    the original's pixels everywhere but where the person was."""
    orig = np.asarray(_rgb(original))
    al = _align(orig, _rgb(edit))
    buf = io.BytesIO()
    Image.fromarray(_blend(orig, al)).save(buf, format="PNG")
    return buf.getvalue()


def _part(image: bytes) -> types.Part:
    """``image`` as a request part: as it is in a format the model takes,
    else as a PNG."""
    with Image.open(io.BytesIO(image)) as img:
        if img.format in _SENT:
            return types.Part.from_bytes(
                data=image, mime_type=Image.MIME[img.format]
            )
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")


def _image(response: types.GenerateContentResponse) -> bytes:
    """The response's first image; ``RuntimeError`` without one."""
    candidates = response.candidates or []
    for candidate in candidates:
        content = candidate.content
        for part in (content.parts if content else None) or []:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
    finish = candidates[0].finish_reason if candidates else None
    raise RuntimeError(f"no image in the model response (finish: {finish})")


def normalize(image: bytes, *, client: genai.Client, model: str) -> bytes:
    """``image`` with its person turned into a mannequin: one ``PROMPT``
    edit by ``model``, pasted back onto ``image`` (``paste_back``); a PNG.
    ``RuntimeError`` when the model returns no image."""
    response = client.models.generate_content(
        model=model, contents=[PROMPT, _part(image)], config=_CONFIG
    )
    return paste_back(image, _image(response))


def main(argv: list[str] | None = None) -> None:
    """The command line: ``IN OUT --model MODEL [--project --location]``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path, help="the worn photo")
    parser.add_argument("output", type=Path, help="the PNG to write")
    parser.add_argument("--model", required=True, help="the image model")
    parser.add_argument(
        "--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    parser.add_argument(
        "--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    )
    args = parser.parse_args(argv)
    client = genai.Client(
        vertexai=True, project=args.project, location=args.location
    )
    photo = args.input.read_bytes()
    args.output.write_bytes(normalize(photo, client=client, model=args.model))
    logger.info(f"wrote {args.output}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
