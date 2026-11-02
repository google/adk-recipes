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

"""POST /api/uploads: one input image into a run's ``inputs/`` folder.

Large requests do not fit Cloud Run's request limit, so clients upload each
image here (always to the media bucket, whatever the input mode) and send
the returned ``gs://`` URIs and ``run_id`` to a generation route.

POST /api/preview: a copy of an image the browser can draw (Chrome cannot
show HEIC); nothing is stored.
"""

import asyncio
import logging
import uuid
from typing import Annotated, cast

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from genmedia4commerce.api.sse import BUCKET_UNAVAILABLE, read_images
from genmedia4commerce.core import media
from genmedia4commerce.core.runs import media_url, new_run, save_inputs

__all__ = ["router"]

logger = logging.getLogger(__name__)

PREVIEW_MAX_SIDE = 1024  # long side of a preview, in pixels

router = APIRouter(prefix="/api", tags=["Uploads"])


@router.post("/uploads")
async def upload_input(
    file: Annotated[UploadFile | None, File()] = None,
    run_id: str = Form(""),
    session_id: str = Form(""),
) -> dict:
    """Store one image (PNG, JPEG, WebP, HEIC or AVIF, up to 20 MB) for a run.

    Pass the ``run_id`` of the first upload with the others so a request's
    inputs share one folder. Returns ``{run_id, session_id, uri, url}``.
    """
    run = new_run(run_id=run_id, session_id=session_id)
    images = await read_images("file", [file], min_count=1, max_count=1)
    (data,) = cast(list[bytes], images)  # no URIs: uploads only
    name = f"upload-{uuid.uuid4().hex[:8]}"
    try:
        saved = await asyncio.to_thread(save_inputs, run, {name: data})
    except Exception as exc:
        logger.exception("upload to the media bucket failed")
        raise HTTPException(503, BUCKET_UNAVAILABLE) from exc
    uri = saved[name]
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "uri": uri,
        "url": media_url(uri),
    }


@router.post("/preview")
async def preview_image(
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Return one image as PNG, JPEG or WebP, long side at most 1024 px.

    Same checks as every image input; HEIC and AVIF come back converted.
    """
    images = await read_images("file", [file], min_count=1, max_count=1)
    (data,) = cast(list[bytes], images)  # no URIs: uploads only
    small = await asyncio.to_thread(media.resize_max, data, PREVIEW_MAX_SIDE)
    return Response(small, media_type=media.sniff_mime(small))
