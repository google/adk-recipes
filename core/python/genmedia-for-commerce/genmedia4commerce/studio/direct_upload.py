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

"""Uploads the browser puts straight into the media bucket.

The MCP App panel reaches the server only through tool calls, which would
carry each image as base64. Instead it asks for a signed PUT URL for a new
upload of its studio (``upload_url``), puts the original file there, then
has it checked and described (``upload_saved``), as ``media.save_image``
does with an image sent to the server.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from google.api_core.exceptions import NotFound

from genmedia4commerce.core import gcs, media, runs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.requests import check_image
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import store

__all__ = ["upload_saved", "upload_url"]

logger = logging.getLogger(__name__)

#: The content types ``check_image`` takes and the extension each is stored
#: with (HEIC and AVIF become JPEG).
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/heic": "jpg",
    "image/heif": "jpg",
    "image/avif": "jpg",
}
_MINUTES = 15


def upload_url(studio_id: str, content_type: str) -> dict[str, Any]:
    """A one-time upload for studio ``studio_id``: ``{url, ref, headers}``,
    a signed PUT URL (15 minutes, at most ``gcs.MAX_IMAGE_BYTES``), the
    upload's ref and the headers the PUT must send. ``ConfigError`` when
    the credentials cannot sign."""
    run = runs.new_run(session_id=store.session_id(studio_id))
    ext = _EXTENSIONS.get(content_type)
    if not ext:
        raise InputError(
            "content_type: not a PNG, JPEG, WebP, HEIC or AVIF image"
        )
    name = f"upload-{uuid.uuid4().hex[:8]}.{ext}"
    ref = runs.folder_uri(run, "inputs") + name
    url, headers = gcs.signed_put_url(
        ref,
        content_type=content_type,
        max_bytes=gcs.MAX_IMAGE_BYTES,
        minutes=_MINUTES,
    )
    return {"url": url, "ref": ref, "headers": headers}


def _checked(ref: str) -> bytes:
    """The image put at ``ref``, as ``check_image`` returns it; an
    invalid one is deleted (``InputError``)."""
    try:
        sent = gcs.download(ref)
    except NotFound as exc:
        raise InputError("ref: nothing was uploaded there") from exc
    try:
        data = check_image(sent, "ref")
        ext = media.EXTENSIONS.get(media.sniff_mime(data))
        if not ref.endswith(f".{ext}"):
            raise InputError("ref: the image is not of the type it was sent as")
    except InputError:
        try:
            gcs.blob(ref).delete()
        except Exception as exc:
            logger.warning("Could not delete %s: %s", ref, exc)
        raise
    if data != sent:  # turned upright, or HEIC and AVIF as JPEG
        bucket = get_settings().media_bucket
        gcs.upload(
            data,
            bucket=bucket,
            path=ref.removeprefix(f"gs://{bucket}/"),
            content_type=media.sniff_mime(data),
        )
    return data


def upload_saved(studio_id: str, ref: str, filename: str) -> dict[str, str]:
    """Check and describe the image put at ``ref`` (from ``upload_url``):
    ``{ref, label, thumb, description}``, as ``media.save_image``."""
    store.check_id(studio_id)
    if not (sm.allowed_ref(ref, studio_id) and "/inputs/upload-" in ref):
        raise InputError("ref: not an upload of this studio")
    return sm.upload_item(ref, _checked(ref), filename)
