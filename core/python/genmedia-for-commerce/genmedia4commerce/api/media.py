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

"""GET /api/media: run files saved to the media bucket (``core.runs``).

Only the images and videos of a run's ``inputs/`` and ``outputs/`` in the
media bucket are served (inputs saved with ``input=gcs`` or ``POST
/api/uploads``, results saved with ``output=gcs``: ``core.runs
.is_media_file``) and the Studio's catalogue copies, so the route never
exposes chat history, studio documents, run records or manifests. A
single ``Range`` is honoured so browsers can stream and seek videos.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from google.api_core.exceptions import NotFound

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.media import sniff_mime
from genmedia4commerce.core.runs import RUNS_PREFIX, is_media_file
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio.media import allowed_ref

__all__ = ["router"]

logger = logging.getLogger(__name__)


def _one_line(value: str) -> str:
    """A user-provided value made safe for one log line (no line breaks)."""
    return value.replace("\r", " ").replace("\n", " ")


router = APIRouter(prefix="/api", tags=["Media"])

CACHE_CONTROL = "private, max-age=3600"
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def _check(uri: str) -> None:
    """``InputError`` unless ``uri`` is an input or output image or video
    of a run in the media bucket, or a Studio catalogue copy."""
    bucket, _, path = uri.removeprefix("gs://").partition("/")
    if not (
        uri.startswith("gs://")
        and bucket == get_settings().media_bucket
        and (is_media_file(path) or allowed_ref(uri))
    ):
        raise InputError(
            f"uri must be gs://<media bucket>/{RUNS_PREFIX}... "
            "of a saved input or output image or video"
        )


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive (start, end) a single ``Range`` asks for.

    ``None`` (serve everything) when there is no valid single range; 416
    when it starts past the end.
    """
    match = _RANGE.fullmatch(header.strip()) if header else None
    if not match or match.groups() == ("", ""):
        return None
    first, last = match.groups()
    if first and last and int(last) < int(first):
        return None
    if first:
        start, end = int(first), int(last) if last else size - 1
    else:  # the last N bytes; N = 0 is unsatisfiable
        start, end = size - int(last) if int(last) else size, size - 1
    start = max(start, 0)
    if start >= size:
        raise HTTPException(
            416, "range not satisfiable", {"Content-Range": f"bytes */{size}"}
        )
    return start, min(end, size - 1)


async def _storage(uri: str, fn, *args, **kwargs):
    """Run a blocking storage call: missing object 404, other failures 502."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, "media not found") from exc
    except Exception as exc:
        logger.warning(f"media {_one_line(uri)} failed: {exc}")
        raise HTTPException(502, "could not fetch the media") from exc


@router.get("/media")
async def saved_media(
    uri: str, range_header: str | None = Header(None, alias="Range")
) -> Response:
    """Return one saved image or video, or the byte range asked for.

    A ``Range`` request reads the object's size and type, then downloads only
    the bytes asked for.
    """
    _check(uri)
    blob = await _storage(uri, gcs.blob, uri)
    headers = {"Cache-Control": CACHE_CONTROL, "Accept-Ranges": "bytes"}
    span = None
    if range_header:
        await _storage(uri, blob.reload)
        span = _byte_range(range_header, blob.size)
    if span is None:
        data = await _storage(uri, blob.download_as_bytes)
        return Response(
            content=data, media_type=sniff_mime(data), headers=headers
        )
    start, end = span
    data = await _storage(uri, blob.download_as_bytes, start=start, end=end)
    headers["Content-Range"] = f"bytes {start}-{end}/{blob.size}"
    return Response(
        content=data,
        status_code=206,
        media_type=blob.content_type or sniff_mime(data),
        headers=headers,
    )
