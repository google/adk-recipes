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

"""Image helpers (synchronous, one implementation of each)."""

import dataclasses
import functools
import io
import logging
from typing import Any

import cv2
import numpy as np
from google.cloud import vision
from pi_heif import register_heif_opener
from PIL import ExifTags, Image, ImageDraw, ImageFilter, ImageOps
from skimage.metrics import structural_similarity

from genmedia4commerce.core import _fit, gen
from genmedia4commerce.core._fit import content_mask, crop_to_content, fit, trim
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.settings import get_settings

__all__ = [
    "EXTENSIONS",
    "FACE_UPSCALE_PROMPT",
    "UPSCALE_PROMPT",
    "blur_face",
    "content_mask",
    "crop_box",
    "crop_face",
    "crop_fraction",
    "crop_to_content",
    "fit",
    "image_size",
    "normalise_image",
    "resize_max",
    "sniff_mime",
    "ssim",
    "to_png",
    "trim",
    "upscale",
]

logger = logging.getLogger(__name__)

# HEIC/HEIF (iPhone photos) open through pi-heif-decoder (decode only, no
# encoder); AVIF is native in Pillow.
register_heif_opener()

# Decompression-bomb guard: PIL warns above this and raises above twice it,
# so images over 100 MP (about 10000x10000) are refused. Full-resolution
# phone photos (up to about 50 MP) still pass; ``fit`` sizes each image for
# the model it is sent to.
Image.MAX_IMAGE_PIXELS = 50_000_000
_FACE_TIMEOUT_S = 30
# Cloud Vision's size guidance for face detection (1600x1200).
_VISION_MAX_PIXELS = 1_920_000


# Strict super-resolution: the model readily repaints or invents detail, so the
# prompt forbids every change but sharpness. It always says "4K" (a quality
# cue); the real output size comes from ImageParams.image_size.
UPSCALE_PROMPT = """You are a high-fidelity image SUPER-RESOLUTION upscaler. Output the SAME image at 4K resolution, only sharper.

DO (resolution/clarity only):
- Increase resolution and sharpness; recover fine detail that is ALREADY present (edges, fabric weave, stitching, seams, textures).
- Remove blur, noise, and compression artifacts.

DO NOT (preserve everything else exactly):
- Do NOT add, remove, invent, or alter ANY detail, object, person, text, logo, print, pattern, stitching, hardware, or texture.
- Do NOT change composition, framing, crop, camera viewpoint, angle, perspective, or zoom.
- Do NOT change colors, tones, lighting, shadows, proportions, shapes, or the background.
- Do NOT beautify, stylize, smooth, or repaint — keep every existing imperfection.
- Keep all text/logos pixel-faithful: identical characters, shape, and position. Never re-letter or reconstruct text.

Return only the upscaled image — identical in content to the input, at 4K resolution."""

# The same for a face crop sent as an identity reference: any change to the
# face would be copied into every image drawn from it.
FACE_UPSCALE_PROMPT = """You are a high-fidelity image SUPER-RESOLUTION upscaler for a portrait crop. Output the SAME photo at higher resolution, only sharper.

DO (resolution/clarity only):
- Increase resolution and sharpness; recover fine detail that is ALREADY present (eyes, lashes, brows, hair strands, skin pores, lips).
- Remove blur, noise, and compression artifacts.

DO NOT (keep exactly the same person):
- Do NOT change the face shape, any facial feature or their proportions, the skin tone, the apparent age, the expression, the gaze or the head pose.
- Do NOT beautify, retouch, slim, smooth skin, remove blemishes, wrinkles, freckles or moles, or add makeup.
- Do NOT change the hair, the framing, the crop, the colors, the lighting or the background; add or remove nothing.

Return only the upscaled image — the same person, identical in content to the input, only sharper."""


# File extension of each MIME type ``sniff_mime`` detects.
EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
}


def sniff_mime(data: bytes) -> str:
    """Detect the MIME type of image/video bytes from their signature."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in _HEIC_BRANDS:
            return "image/heic"
        return "video/mp4"
    return "application/octet-stream"


_HEIC_BRANDS = (
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"mif1",
    b"msf1",
    b"heim",
    b"heis",
)
_CONVERTED = ("image/heic", "image/avif")
# The formats kept as they are, with the Pillow format they are re-encoded in
# when only their orientation needs fixing.
_KEPT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def normalise_image(data: bytes) -> bytes:
    """Return ``data`` as upright PNG, JPEG or WebP: HEIC and AVIF become JPEG.

    Phone photos arrive as HEIC and shop images as AVIF; the browser, the
    bucket and the models all take JPEG. PNG, JPEG and WebP bytes are kept
    byte for byte unless their EXIF orientation is not upright: then the
    pixels are turned and re-encoded in the same format (``_encode``), so no
    photo is stored sideways. Full resolution is kept; the same bytes always
    give the same result. PNG, JPEG or WebP bytes that cannot be read come
    back unchanged (refused where they are read, as before); HEIC or AVIF
    raise ``InputError``.
    """
    mime = sniff_mime(data)
    if mime not in _CONVERTED and mime not in _KEPT:
        return data
    try:
        img = _fit.open_lazy(data)
    except InputError:
        if mime in _KEPT:
            return data
        raise
    # EXIF orientation: 1 is upright, 2 to 8 a turn and/or a flip.
    turned = img.getexif().get(ExifTags.Base.Orientation, 1) in range(2, 9)
    if mime in _KEPT and not turned:
        return data
    upright = ImageOps.exif_transpose(img) if turned else img
    fmt = _KEPT.get(mime, "JPEG")
    logger.info(
        "normalise_image: %s saved as %s (turned=%s)", mime, fmt, turned
    )
    return _encode(upright, fmt)


def _encode(img: Image.Image, fmt: str) -> bytes:
    """``img`` as ``fmt`` with fixed settings (deterministic): PNG lossless,
    JPEG and WebP quality 95 (JPEG with no chroma subsampling). Only the
    colour profile is kept; no EXIF, so no orientation is applied twice.
    """
    icc = img.info.get("icc_profile")
    buf = io.BytesIO()
    if fmt == "PNG":
        img.save(buf, format="PNG", icc_profile=icc)
    elif fmt == "WEBP":
        mode = "RGBA" if _fit.has_alpha(img) else "RGB"
        img.convert(mode).save(buf, format="WEBP", quality=95, icc_profile=icc)
    else:
        out = img if img.mode in ("L", "RGB", "CMYK") else img.convert("RGB")
        out.save(buf, format="JPEG", quality=95, subsampling=0, icc_profile=icc)
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    return ImageOps.exif_transpose(img := _fit.open_lazy(data)) or img


def image_size(data: bytes) -> tuple[int, int]:
    """Stored width and height (header only); ``InputError`` like ``fit``."""
    return _fit.open_lazy(data).size


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def to_png(data: bytes) -> bytes:
    """Re-encode any image as PNG (EXIF orientation applied, alpha kept)."""
    img = _open(data)
    return _png(img.convert("RGBA" if _fit.has_alpha(img) else "RGB"))


def resize_max(data: bytes, max_side: int) -> bytes:
    """Downscale so the long side is at most ``max_side`` (PNG).

    Images already small enough are returned unchanged.
    """
    img = _open(data)
    if max(img.size) <= max_side:
        return data
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return _png(img)


def crop_box(
    data: bytes, box: tuple[float, float, float, float], *, pad: float = 0.0
) -> bytes:
    """Crop ``(x0, y0, x1, y1)`` pixels, padded by ``pad`` x box size per side."""
    img = _open(data)
    x0, y0, x1, y1 = box
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    left, top = max(0, round(x0 - px)), max(0, round(y0 - py))
    right, bottom = (
        min(img.width, round(x1 + px)),
        min(img.height, round(y1 + py)),
    )
    if right <= left or bottom <= top:
        raise InputError(
            f"empty crop box {box} for a {img.width}x{img.height} image"
        )
    return _png(img.crop((left, top, right, bottom)))


def crop_fraction(
    data: bytes, *, top: float = 0.0, bottom: float = 1.0
) -> bytes:
    """Crop the horizontal band between ``top`` and ``bottom`` (0..1 of height)."""
    if not 0.0 <= top < bottom <= 1.0:
        raise InputError(f"invalid band top={top} bottom={bottom}")
    img = _open(data)
    return _png(
        img.crop(
            (0, round(top * img.height), img.width, round(bottom * img.height))
        )
    )


@functools.cache
def _vision() -> vision.ImageAnnotatorClient:
    return vision.ImageAnnotatorClient(
        client_options={"quota_project_id": get_settings().project_id}
    )


FACE_MARGIN = 0.6  # a face crop is padded by this x the face size per side


def _largest_face(data: bytes) -> tuple[Any, float, float] | None:
    """The largest face Cloud Vision finds in ``data`` and the x and y scale
    from the copy it looked at (at most ``_VISION_MAX_PIXELS``) to ``data``'s
    upright pixels; None when no face is found."""
    sent = fit(data, max_pixels=_VISION_MAX_PIXELS)
    response = _vision().face_detection(
        image=vision.Image(content=sent), timeout=_FACE_TIMEOUT_S
    )
    faces = response.face_annotations
    if not faces:
        logger.info("no face detected")
        return None

    def area(face) -> float:
        v = face.bounding_poly.vertices
        return (v[2].x - v[0].x) * (v[2].y - v[0].y)

    (w, h), (sw, sh) = _open(data).size, image_size(sent)
    return max(faces, key=area), w / sw, h / sh


def face_box(data: bytes) -> tuple[float, float, float, float] | None:
    """The largest face's box (Cloud Vision) in ``data``'s pixels; None when
    no face is found."""
    found = _largest_face(data)
    if found is None:
        return None
    face, sx, sy = found
    v = face.bounding_poly.vertices
    return (v[0].x * sx, v[0].y * sy, v[2].x * sx, v[2].y * sy)


def crop_face(data: bytes, *, margin: float = FACE_MARGIN) -> bytes | None:
    """Crop the largest face (``face_box``), padded by ``margin`` x face
    size, from ``data`` at full resolution; None when no face is found."""
    box = face_box(data)
    return None if box is None else crop_box(data, box, pad=margin)


_MARK = vision.FaceAnnotation.Landmark.Type
# The landmarks that place the blur: eyes, nose, mouth corners, chin.
_BLUR_MARKS = (
    _MARK.LEFT_EYE,
    _MARK.RIGHT_EYE,
    _MARK.NOSE_TIP,
    _MARK.MOUTH_LEFT,
    _MARK.MOUTH_RIGHT,
    _MARK.CHIN_GNATHION,
)


def blur_face(data: bytes, *, keep_eyes: bool = False) -> bytes:
    """``data`` with the largest face's eyes, nose, mouth and chin blurred.

    A feathered ellipse placed with Cloud Vision's face landmarks: centred
    on the eyes, nose and mouth corners, 0.95 d to each side (d: the eye
    distance), from 0.55 d above the eyes (above the brows) to the chin, so
    ears, earrings and hair stay sharp. ``keep_eyes`` starts 0.35 d below
    the eyes (worn glasses). Returns the original bytes when no face (or
    not every landmark) is found, else a PNG.
    """
    found = _largest_face(data)
    if found is None:
        return data
    face, sx, sy = found
    at = {
        m.type_: (m.position.x * sx, m.position.y * sy) for m in face.landmarks
    }
    if any(mark not in at for mark in _BLUR_MARKS):
        logger.warning("blur_face: face landmarks missing, not blurred")
        return data
    points = np.array([at[mark] for mark in _BLUR_MARKS[:5]])
    eyes, chin = points[:2], at[_MARK.CHIN_GNATHION][1]
    d = float(np.linalg.norm(eyes[1] - eyes[0]))
    cx = float(points[:, 0].mean())
    if keep_eyes:
        top = float(eyes[:, 1].max()) + 0.35 * d
    else:
        top = float(eyes[:, 1].min()) - 0.55 * d
    img = _open(data).convert("RGB")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse(
        (cx - 0.95 * d, top, cx + 0.95 * d, chin), fill=255
    )
    soft = ImageFilter.GaussianBlur(0.12 * d)
    return _png(Image.composite(img.filter(soft), img, mask.filter(soft)))


def upscale(
    data: bytes,
    *,
    image_size: str = "4K",
    params: "gen.ImageParams | None" = None,
    prompt: str = UPSCALE_PROMPT,
) -> bytes:
    """Super-resolve ``data`` to ``image_size`` with the image model.

    ``params`` default keeps the input aspect ratio (``aspect_ratio="auto"``);
    ``prompt``: ``FACE_UPSCALE_PROMPT`` for a face.
    """
    base = params or gen.ImageParams(aspect_ratio="auto")
    return gen.generate_image(
        [prompt, data],
        params=dataclasses.replace(base, image_size=image_size),
    )


def ssim(a: bytes, b: bytes, *, size: int = 256) -> float:
    """Structural similarity of two images, compared as size x size grayscale."""

    def gray(data: bytes) -> np.ndarray:
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None:
            raise InputError("ssim: could not decode image")
        return cv2.resize(arr, (size, size), interpolation=cv2.INTER_AREA)

    return float(structural_similarity(gray(a), gray(b), data_range=255))
