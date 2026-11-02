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

"""Image references and browser previews for the Studio clients.

The clients never hold full images. They hold references:

- ``catalog:<id>`` or ``catalog:<id>/<n>`` for an image of the local
  catalogue (``core.catalog``: products, preset models, spin sets);
- ``gs://...`` for an upload (a run's ``inputs/`` in the media bucket, under
  the studio's session, as ``POST /api/uploads`` stores them), the studio's
  copy of a catalogue pick (``to_gcs``) or a generated output.

``allowed_ref`` accepts only these: image and video files of a Studio run
(``runs/<date>/studio-<id>/<run>/inputs|outputs/``, of one studio when the
caller names it) and the catalogue copies. Nothing else of the bucket (run
records, other runs) is ever used or signed. The description of an upload
is written by the server next to it (``upload_descriptions``), so a
client never supplies the text the agent is shown about an image.

Previews are catalogue thumbnails (``studio.picks.thumb``), small data URLs
for uploads and V4 signed URLs for private outputs. The MCP App's resource
declares ``storage.googleapis.com`` in its CSP so these load in its iframe.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import mimetypes
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from google.api_core.exceptions import NotFound
from PIL import Image, ImageOps

from genmedia4commerce.core import catalog, gcs, media, runs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.runs import RUNS_PREFIX
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.describe import describe_image
from genmedia4commerce.requests import check_image
from genmedia4commerce.studio import picks, store

__all__ = [
    "allowed_record",
    "allowed_ref",
    "catalog_copies",
    "mime_of",
    "preview_url",
    "previews",
    "refresh_urls",
    "resolve",
    "save_image",
    "save_upload",
    "thumb_data_url",
    "to_gcs",
    "upload_descriptions",
]

logger = logging.getLogger(__name__)

COPIES_PREFIX = f"{RUNS_PREFIX}studio_catalog/"
_THUMB_SIDE = 320
#: catalogue ref -> (gs:// copy, description, copied at); refreshed daily
#: because the bucket's lifecycle rule deletes ``runs/`` after 30 days.
_COPIES: dict[str, tuple[str, str, float]] = {}
_COPY_TTL_S = 24 * 3600
_COPIERS = 8  # parallel uploads of one pick (a spin set's views)
_warned_unsigned = False  # the inline fallback is logged once per process
#: A Studio run's input or output file (``core.runs``): group ``id`` is the
#: studio, ``upload`` set for an upload (``save_image``).
_STUDIO_FILE = re.compile(
    rf"{RUNS_PREFIX}\d{{4}}-\d{{2}}-\d{{2}}/studio-(?P<id>[A-Za-z0-9_-]+)/"
    rf"[a-f0-9]{{12}}/(?:outputs|inputs)/(?P<upload>upload-)?[A-Za-z0-9_-]+"
    rf"{runs.MEDIA_SUFFIX}"
)
#: A catalogue copy (``to_gcs``).
_COPY_FILE = re.compile(
    rf"{COPIES_PREFIX}[A-Za-z0-9_-]+/\d+{runs.MEDIA_SUFFIX}"
)
#: The run record of a Studio try-on (``core.runs``: its manifest).
_STUDIO_RECORD = re.compile(
    rf"{RUNS_PREFIX}\d{{4}}-\d{{2}}-\d{{2}}/studio-(?P<id>[A-Za-z0-9_-]+)/"
    rf"[a-f0-9]{{12}}/{runs.MANIFEST}"
)
_MAX_DESCRIPTION = 300
#: ``mime_of``'s table: the extensions the app writes (``media.EXTENSIONS``).
_MIME_OF_EXTENSION = {ext: mime for mime, ext in media.EXTENSIONS.items()} | {
    "jpeg": "image/jpeg"
}


def thumb_data_url(data: bytes, side: int = _THUMB_SIDE) -> str:
    """A small JPEG data URL of an image."""
    with Image.open(io.BytesIO(data)) as source:
        img = ImageOps.exif_transpose(source).convert("RGBA")
    img.thumbnail((side, side), Image.Resampling.LANCZOS)
    flat = Image.new("RGB", img.size, "white")
    flat.paste(img, mask=img.getchannel("A"))
    buf = io.BytesIO()
    flat.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def resolve(ref: str) -> str:
    """A reference as the workflows take it (``catalog:`` or ``gs://``;
    refs of older studios mapped with ``picks.legacy``)."""
    return picks.legacy(ref)


def _describe(data: bytes) -> str:
    """The image in one sentence; "" when the model call fails."""
    try:
        return describe_image(data)
    except Exception as exc:  # a missing description must not block an upload
        logger.warning("Could not describe an image: %s", exc)
        return ""


def save_upload(
    image_base64: str, filename: str, studio_id: str = ""
) -> dict[str, str]:
    """``save_image`` for an image in base64 or a data URL."""
    raw = image_base64.split(";base64,", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError("image_base64: not valid base64") from exc
    return save_image(data, filename, studio_id, field="image_base64")


def save_image(
    data: bytes, filename: str, studio_id: str = "", *, field: str = "file"
) -> dict[str, str]:
    """Save an uploaded image in a run's ``inputs/`` (as ``POST
    /api/uploads`` does, with the same checks, errors naming ``field``);
    return its ref, thumbnail and description.

    PNG, JPEG and WebP are stored as sent (turned upright when their EXIF
    says so), HEIC and AVIF as JPEG (``media.normalise_image``); the
    file's extension and content type are its format's.
    """
    session = store.session_id(studio_id)  # InputError for a bad id
    data = check_image(data, field)
    run = runs.new_run(session_id=session)
    name = f"upload-{uuid.uuid4().hex[:8]}"
    uri = runs.save_inputs(run, {name: data})[name]
    description = _describe(data)[:_MAX_DESCRIPTION]
    _keep_description(uri, description)
    return {
        "ref": uri,
        "label": filename[:80] or "Upload",
        "thumb": thumb_data_url(data),
        "description": description,
    }


def _description_uri(uri: str) -> str:
    """Where the server keeps an upload's description: next to it, as
    JSON (never an allowed ref)."""
    return uri.rsplit(".", 1)[0] + ".json"


def _keep_description(uri: str, description: str) -> None:
    """Store the description the server wrote for an upload (logged when
    it fails: the upload works without it)."""
    if not description:
        return
    bucket = get_settings().media_bucket
    try:
        gcs.upload(
            json.dumps({"description": description}).encode(),
            bucket=bucket,
            path=_description_uri(uri).removeprefix(f"gs://{bucket}/"),
            content_type="application/json",
        )
    except Exception as exc:
        logger.warning("Could not keep the description of %s: %s", uri, exc)


def _upload_description(uri: str) -> str:
    """The description the server wrote for an upload; "" without one."""
    try:
        doc = json.loads(gcs.download(_description_uri(uri)))
    except NotFound:
        return ""
    except Exception as exc:
        logger.warning("No description of %s: %s", uri, exc)
        return ""
    text = doc.get("description") if isinstance(doc, dict) else None
    return text[:_MAX_DESCRIPTION] if isinstance(text, str) else ""


def upload_descriptions(refs: list[str]) -> dict[str, str]:
    """``{ref: description}`` of the Studio uploads among ``refs`` that
    have one, as the server wrote it (``save_image``), read in parallel."""
    todo = [
        ref
        for ref in dict.fromkeys(refs)
        if (found := _studio_file(ref)) and found["upload"]
    ]
    if not todo:
        return {}
    with ThreadPoolExecutor(min(_COPIERS, len(todo))) as pool:
        texts = pool.map(_upload_description, todo)
        return {r: t for r, t in zip(todo, texts, strict=True) if t}


def _catalog_description(item_id: str) -> str:
    """The catalogue's description of an item; "" for preset models and
    spin sets, whose description is their name (their label already says
    it, and the agent looks at the image itself). Never a model call: a
    pick must not wait for one."""
    row = catalog.item(item_id)
    desc = str(row.get("description") or "")
    return desc if desc != row.get("title") else ""


def to_gcs(ref: str) -> tuple[str, str]:
    """A catalogue image as a ``gs://`` copy plus its catalogue
    description (cached).

    Like an upload: every image the agent knows about then has the same
    form, a ``gs://`` URI it can look at and, when the catalogue has one, a
    sentence. Other refs come back unchanged with no description.
    """
    if not catalog.is_ref(ref):
        return ref, ""
    cached = _COPIES.get(ref)
    if cached and time.time() - cached[2] < _COPY_TTL_S:
        return cached[0], cached[1]
    item_id, n = catalog.parse_ref(ref)
    data = catalog.image(item_id, n)
    mime = media.sniff_mime(data)
    ext = media.EXTENSIONS.get(mime, "webp")
    uri = gcs.upload(
        data,
        bucket=get_settings().media_bucket,
        path=f"{COPIES_PREFIX}{item_id}/{n}.{ext}",
        content_type=mime,
    )
    desc = _catalog_description(item_id)
    _COPIES[ref] = (uri, desc, time.time())
    return uri, desc


def catalog_copies(refs: list[str]) -> dict[str, tuple[str, str]]:
    """``to_gcs`` of the catalogue refs among ``refs``, copied in parallel
    (a spin set's views): ``{ref: (gs:// copy, description)}``."""
    todo = list(dict.fromkeys(r for r in refs if catalog.is_ref(r)))
    if not todo:
        return {}
    with ThreadPoolExecutor(min(_COPIERS, len(todo))) as pool:
        return dict(zip(todo, pool.map(to_gcs, todo), strict=True))


def _studio_file(ref: str) -> re.Match[str] | None:
    """The match of a Studio run file in the media bucket, if ``ref`` is
    one."""
    prefix = f"gs://{get_settings().media_bucket}/"
    if not (isinstance(ref, str) and ref.startswith(prefix)):
        return None
    return _STUDIO_FILE.fullmatch(ref.removeprefix(prefix))


def allowed_ref(ref: str, studio_id: str = "") -> bool:
    """Whether a ref may be used or signed: a catalogue image, a catalogue
    copy, or an image or video a Studio run saved (an upload or an output;
    only of ``studio_id`` when given). Run records, manifests and other
    runs of the media bucket never are."""
    if catalog.is_ref(ref):
        try:
            catalog.parse_ref(ref)
        except InputError:
            return False
        return True
    prefix = f"gs://{get_settings().media_bucket}/"
    if not (isinstance(ref, str) and ref.startswith(prefix)):
        return False
    if _COPY_FILE.fullmatch(ref.removeprefix(prefix)):
        return True
    found = _studio_file(ref)
    return found is not None and studio_id in ("", found["id"])


def allowed_record(uri: str, studio_id: str) -> bool:
    """Whether ``uri`` is the run record (``manifest.json``) of a try-on of
    studio ``studio_id``, the only ``source_run`` its jobs may name."""
    prefix = f"gs://{get_settings().media_bucket}/"
    if not (isinstance(uri, str) and uri.startswith(prefix)):
        return False
    found = _STUDIO_RECORD.fullmatch(uri.removeprefix(prefix))
    return found is not None and found["id"] == studio_id


def mime_of(ref: str) -> str:
    """The MIME type a ref's extension implies (``image/png`` by default).

    The app's own extensions first: Python before 3.13 does not know
    ``.webp`` (uploads and catalogue copies) without a system MIME table.
    """
    path = ref.split("?", 1)[0]
    ext = path.rsplit(".", 1)[-1].lower()
    return (
        _MIME_OF_EXTENSION.get(ext)
        or mimetypes.guess_type(path)[0]
        or "image/png"
    )


def previews(refs: list[str], *, inline: bool = True) -> dict[str, str]:
    """Browser URLs for refs the client has no thumbnail for (catalogue
    thumbnails inline or not, as ``picks.thumb``)."""
    out: dict[str, str] = {}
    for ref in refs[:24]:
        try:
            if not allowed_ref(ref):
                continue
            if catalog.is_ref(ref):
                out[ref] = picks.thumb(ref, inline=inline)
            else:
                video = mime_of(ref).startswith("video/")
                out[ref] = preview_url(ref, video=video)
        except Exception as exc:
            logger.info("No preview for %s: %s", ref, exc)
    return out


def preview_url(uri: str, *, video: bool = False) -> str:
    """A URL the iframe can load: signed, or a data URL fallback.

    Only for refs ``allowed_ref`` accepts ("" for any other): callers send
    the refs, so this must not open the rest of the media bucket.
    """
    global _warned_unsigned
    if not uri:
        return ""
    if catalog.is_ref(uri) or not allowed_ref(uri):
        logger.warning("No preview URL for this ref: %s", uri)
        return ""
    try:
        return gcs.signed_url(uri)
    except Exception as exc:  # e.g. user credentials on a laptop
        log = logger.debug if _warned_unsigned else logger.warning
        _warned_unsigned = True
        log("Signed URLs unavailable (%s); previews are inlined", exc)
    data = gcs.resolve_image(uri)  # media bucket runs/ only
    if video:  # whole, in its own format (a spin is an animated WebP)
        mime = media.sniff_mime(data)
        return f"data:{mime};base64," + base64.b64encode(data).decode()
    return thumb_data_url(data, side=1024)


def refresh_urls(results: list[dict[str, Any]], *, video: bool) -> None:
    """Give saved results new preview URLs (signed URLs expire).

    Each item on its own: one that fails gets ``url`` "" and the rest keep
    their previews.
    """
    for r in results:
        if not (isinstance(r, dict) and r.get("uri")):
            continue
        try:
            r["url"] = preview_url(str(r["uri"]), video=video)
        except Exception as exc:
            logger.warning("No preview for %s: %s", r["uri"], exc)
            r["url"] = ""
