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

"""What the three streaming routes share: inputs, limits and the SSE stream.

Each workflow event is one ``data: <json>`` frame; a ``: keepalive`` comment
goes out every ``KEEPALIVE_S`` while waiting. A client disconnect cancels
the workflow.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence

from fastapi import Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from genmedia4commerce.core.errors import InputError, user_message
from genmedia4commerce.core.events import ErrorEvent, Event
from genmedia4commerce.core.gcs import MAX_IMAGE_BYTES
from genmedia4commerce.core.media import normalise_image, sniff_mime
from genmedia4commerce.requests import start_run

__all__ = [
    "BUCKET_UNAVAILABLE",
    "HEADERS",
    "KEEPALIVE_S",
    "MAX_IMAGE_BYTES",
    "MAX_TOTAL_BYTES",
    "RunForm",
    "check_total",
    "read_images",
    "start",
    "stream",
]

logger = logging.getLogger(__name__)

KEEPALIVE_S = 10.0
MAX_TOTAL_BYTES = 50 * 2**20
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
BUCKET_UNAVAILABLE = "the media bucket is not available"
_DONE = object()


@dataclasses.dataclass
class RunForm:
    """The run fields of every streaming route (``core.runs``).

    ``input`` / ``output``: "inline", "gcs" or "" for the deployment
    default. ``run_id`` (from ``POST /api/uploads``) and ``session_id``
    choose the run folder.
    """

    input: str = Form("")
    output: str = Form("")
    run_id: str = Form("")
    session_id: str = Form("")


async def read_images(
    field: str,
    files: Sequence[UploadFile | None] | None,
    uris: Sequence[str] | None = None,
    *,
    min_count: int = 0,
    max_count: int = 8,
) -> list[bytes | str]:
    """One image field: its uploads' bytes, then its ``gs://``/https URIs.

    400 on a bad count (files and URIs together), size or file type.
    """
    uploads = [f for f in files or [] if f is not None]
    links = [uri for uri in uris or [] if uri]
    count = len(uploads) + len(links)
    if not min_count <= count <= max_count:
        raise HTTPException(
            400, f"{field}: {min_count} to {max_count} images, got {count}"
        )
    images: list[bytes | str] = []
    for upload in uploads:
        # One byte over the limit is enough for the size check to refuse it.
        data = await upload.read(MAX_IMAGE_BYTES + 1)
        if not data:
            raise HTTPException(400, f"{field}: {upload.filename} is empty")
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                400,
                f"{field}: images must be at most {MAX_IMAGE_BYTES // 2**20} MB",
            )
        if not sniff_mime(data).startswith("image/"):
            raise HTTPException(
                400,
                f"{field}: {upload.filename} is not a PNG, JPEG, WebP, "
                "HEIC or AVIF image",
            )
        images.append(normalise_image(data))
    return images + links


def check_total(*groups: Sequence[bytes | str]) -> None:
    """400 when the uploaded images together exceed ``MAX_TOTAL_BYTES``.

    URIs do not count: they are how large inputs avoid the request limit.
    """
    total = sum(
        len(image)
        for group in groups
        for image in group
        if isinstance(image, bytes)
    )
    if total > MAX_TOTAL_BYTES:
        raise HTTPException(
            400, f"images must total at most {MAX_TOTAL_BYTES // 2**20} MB"
        )


async def start(
    request: Request,
    form: RunForm,
    refs: Mapping[str, Sequence[bytes | str]],
    build: Callable[..., object],
    workflow: Callable[..., AsyncGenerator[Event, None]],
) -> StreamingResponse:
    """Check the inputs, start the run (``requests.start_run``) and stream it.

    400 on bad input; 503 when the media bucket cannot be read or written.
    """
    check_total(*refs.values())
    try:
        events = await start_run(
            refs, build, workflow, **dataclasses.asdict(form)
        )
    except InputError:
        raise
    except Exception as exc:
        logger.exception("could not prepare the run's inputs")
        raise HTTPException(503, BUCKET_UNAVAILABLE) from exc
    return stream(events, request)


async def _pump(
    events: AsyncGenerator[Event, None], queue: asyncio.Queue
) -> None:
    """Move the workflow's events to ``queue``; a failure becomes an error
    event, with its message only for user-facing errors."""
    try:
        async with contextlib.aclosing(events):
            async for event in events:
                await queue.put(event)
    except Exception as exc:
        logger.exception("workflow failed while streaming")
        retryable = getattr(exc, "retryable", False)
        message = user_message(exc)  # never storage paths or credentials
        await queue.put(ErrorEvent(message=message, retryable=retryable))
    finally:
        await queue.put(_DONE)


async def _frames(
    events: AsyncGenerator[Event, None], request: Request
) -> AsyncGenerator[str, None]:
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_pump(events, queue))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                if await request.is_disconnected():
                    logger.info("client disconnected; cancelling the workflow")
                    return
                yield ": keepalive\n\n"
                continue
            if item is _DONE:
                return
            yield item.to_sse()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def stream(
    events: AsyncGenerator[Event, None], request: Request
) -> StreamingResponse:
    """Stream ``events`` as SSE; disconnecting cancels the workflow."""
    return StreamingResponse(
        _frames(events, request),
        media_type="text/event-stream",
        headers=HEADERS,
    )
