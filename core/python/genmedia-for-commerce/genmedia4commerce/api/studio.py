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

"""/api/studio: the Studio for the web app.

The same operations as the Studio MCP App (``studio.service``), with the
same response bodies as its tools' ``structuredContent`` (catalogue
thumbnails as same-origin URLs, not data URLs): the web app shows
the studio the server owns, sends each click as an action, follows jobs and
talks to the Studio agent in panel mode. Errors are HTTP errors with a
``detail``: 400 bad input, 422 a bad studio or job id or body, 502 a
failed model call, 503 storage or a busy server, 504 a deadline; other
failures 500. A ``detail`` is only a message meant for the user
(``core.errors.user_message``): never a bucket, a path or a setting.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Path, UploadFile
from google.auth import exceptions as auth_exceptions
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import (
    USER_ERRORS,
    BusyError,
    DeadlineExceeded,
    GenerationError,
    GenMediaError,
    InputError,
    StorageError,
    user_message,
)
from genmedia4commerce.studio import service, store
from genmedia4commerce.studio.session_limits import MAX_MESSAGE

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/studio", tags=["Studio"])

_STORAGE_DETAIL = "Storage is unavailable; try again."
_SERVER_DETAIL = "The request failed on the server; try again."
_CREDENTIALS_DETAIL = (
    "The server's Google Cloud credentials are missing or have expired. "
    "Running locally? Run `gcloud auth application-default login`, then "
    "try again."
)


def _credentials_problem(exc: BaseException) -> bool:
    """Whether ``exc`` comes from missing or expired Google credentials
    (``google.auth`` refresh or default-credentials errors, anywhere in its
    cause chain)."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, auth_exceptions.GoogleAuthError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _detail(exc: GenMediaError) -> str:
    """The message a client sees: one meant for the user, else a generic
    one (saying when the cause is the server's credentials)."""
    if not isinstance(exc, USER_ERRORS) and _credentials_problem(exc):
        return _CREDENTIALS_DETAIL
    storage = isinstance(exc, StorageError)
    return user_message(exc, _STORAGE_DETAIL if storage else _SERVER_DETAIL)


def _not_jobs(studio_id: str) -> str:
    """``jobs`` would clash with ``/api/studio/jobs/{job_id}``."""
    if studio_id == "jobs":
        raise ValueError("'jobs' is not a studio id")
    return studio_id


StudioId = Annotated[
    str, Path(pattern=store.STUDIO_ID_PATTERN), AfterValidator(_not_jobs)
]
JobId = Annotated[str, Path(pattern=store.JOB_ID_PATTERN)]
#: Most specific first.
_STATUS: tuple[tuple[type[GenMediaError], int], ...] = (
    (InputError, 400),
    (DeadlineExceeded, 504),
    (GenerationError, 502),
    (StorageError, 503),
    (BusyError, 503),
)


@contextlib.contextmanager
def _http_errors() -> Iterator[None]:
    """``core.errors`` types as HTTP errors with a message meant for the
    user (``_detail``: storage and config errors can name buckets, paths or
    settings); the details are logged."""
    try:
        yield
    except GenMediaError as exc:
        code = next((c for t, c in _STATUS if isinstance(exc, t)), 500)
        logger.warning(
            "studio request failed (%s): %s",
            code,
            exc,
            exc_info=not isinstance(exc, USER_ERRORS),
        )
        raise HTTPException(code, _detail(exc)) from exc


class AgentBody(BaseModel):
    """``POST /api/studio/{studio_id}/agent``."""

    message: str = Field(min_length=1, max_length=MAX_MESSAGE)


class ActionBody(BaseModel):
    """``POST /api/studio/{studio_id}/actions``: one ``studio.state`` op or
    ``{"job_finished": {"job_id"}}``."""

    event: dict[str, Any]


class SearchBody(BaseModel):
    """``POST /api/studio/{studio_id}/catalog-search``."""

    query: str = ""
    audience: str = ""


class JobBody(BaseModel):
    """``POST /api/studio/{studio_id}/jobs``: a job spec without its
    studio_id (person, reference_model, products, spin, settings; for a
    Video Try-On "Make video", tryon_image: one of the studio's video
    try-on images, whose run record and back view the server adds)."""

    model_config = ConfigDict(extra="allow")

    capability: str


class PreviewsBody(BaseModel):
    """``POST /api/studio/previews``."""

    refs: list[str]


@router.get("/gallery")
async def gallery() -> dict:
    """The preset ``models`` and the ``spin`` sets of the catalogue."""
    with _http_errors():
        return await asyncio.to_thread(
            functools.partial(service.gallery, inline=False)
        )


@router.post("/previews")
async def previews(body: PreviewsBody) -> dict:
    """``{previews: {ref: url}}`` for image refs without a thumbnail."""
    with _http_errors():
        return await asyncio.to_thread(
            functools.partial(service.previews, body.refs, inline=False)
        )


@router.get("/jobs/{job_id}")
async def job_status(job_id: JobId) -> dict:
    """A generation's status, progress and results so far."""
    with _http_errors():
        return await asyncio.to_thread(service.job_status, job_id)


@router.post("/{studio_id}/agent")
async def agent_turn(studio_id: StudioId, body: AgentBody) -> dict:
    """One message to the Studio agent: ``{studio_id, reply, patches,
    issued_at}`` and, when the turn saved the studio, ``state`` and
    ``updated_at``."""
    with _http_errors():
        return await service.agent_turn(body.message, studio_id)


@router.get("/{studio_id}/state")
async def state(studio_id: StudioId) -> dict:
    """The saved studio: ``{studio_id, saved, updated_at, state}``."""
    with _http_errors():
        return await asyncio.to_thread(service.state, studio_id)


@router.get("/{studio_id}/messages")
async def messages(studio_id: StudioId) -> dict:
    """The chat with the agent: ``{studio_id, messages: [{role, text}]}``."""
    with _http_errors():
        return await service.conversation(studio_id)


@router.post("/{studio_id}/actions")
async def action(studio_id: StudioId, body: ActionBody) -> dict:
    """Apply one user action: ``{state, updated_at}`` (and ``refs``)."""
    with _http_errors():
        return await asyncio.to_thread(service.event, studio_id, body.event)


@router.post("/{studio_id}/uploads")
async def upload(
    studio_id: StudioId, file: Annotated[UploadFile, File()]
) -> dict:
    """Save an image: ``{ref, label, thumb, description}``."""
    # One byte over the limit is enough for the size check to refuse it.
    data = await file.read(gcs.MAX_IMAGE_BYTES + 1)
    with _http_errors():
        return await asyncio.to_thread(
            service.upload, data, file.filename or "", studio_id
        )


@router.post("/{studio_id}/catalog-search")
async def catalog_search(studio_id: StudioId, body: SearchBody) -> dict:
    """The products picker: ``{query, items, error}``, the featured
    products for an empty query."""
    with _http_errors():
        return await service.catalog_search(
            body.query, body.audience, studio_id, inline=False
        )


@router.post("/{studio_id}/jobs")
async def job_start(studio_id: StudioId, body: JobBody) -> dict:
    """Start a generation: ``{job_id, status, capability}``."""
    spec = {**body.model_dump(), "studio_id": studio_id}
    with _http_errors():
        return await service.job_start(spec)
