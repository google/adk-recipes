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

"""Run folders: where a request's inputs and results are kept.

Every run has one folder in the media bucket,
``runs/<YYYY-MM-DD>/<session_id>/<run_id>/``, with ``inputs/`` (input mode
``gcs``) and ``outputs/`` (output mode ``gcs``). The two modes are
independent. ``deliver`` wraps a workflow's event stream; workflows never
see the modes. Inline output passes the events through (media as base64);
``gcs`` uploads each variation's image or video to
``outputs/<stage-view-index>.<ext>`` and the event carries ``media_uri`` and
``media_url`` (``GET /api/media``) instead of the bytes. A failed upload
keeps that variation inline.

A workflow's ``RecordEvent`` (what its results were made from) is kept, in
output mode ``gcs`` only, as the run's ``manifest.json``: its payload plus
``files``, the ``gs://`` URI of each of its images, saved under
``record/``. The stream then carries ``ProgressEvent("record")`` with
``data={"manifest": uri}``. Inline output keeps nothing (the event is
dropped). ``load_record`` reads a record back for a later run.

``is_media_file`` says which objects a client may fetch (``GET
/api/media``): the images and videos of a run's ``inputs/`` and
``outputs/``, never its record or manifest.
"""

import asyncio
import contextlib
import dataclasses
import datetime
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import Literal, cast, get_args
from urllib.parse import quote

from google.api_core.exceptions import NotFound

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import (
    Event,
    ProgressEvent,
    RecordEvent,
    VariationEvent,
)
from genmedia4commerce.core.media import EXTENSIONS, sniff_mime
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.settings import Mode, get_settings

__all__ = [
    "MANIFEST",
    "MEDIA_SUFFIX",
    "RUNS_PREFIX",
    "Mode",
    "RunRef",
    "deliver",
    "folder_uri",
    "is_media_file",
    "load_record",
    "media_url",
    "new_run",
    "parse_mode",
    "save_inputs",
    "save_record",
]

logger = logging.getLogger(__name__)

RUNS_PREFIX = "runs/"
_NO_SESSION = "no-session"
_RUN_ID = re.compile(r"[a-f0-9]{12}")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MANIFEST = "manifest.json"
# A run folder in the media bucket: runs/<date>/<session>/<run>.
_FOLDER = (
    rf"{RUNS_PREFIX}\d{{4}}-\d{{2}}-\d{{2}}/{_SESSION_ID.pattern}/"
    rf"{_RUN_ID.pattern}"
)
#: The image and video files the app writes (``core.media.EXTENSIONS``,
#: plus the other common ones).
MEDIA_SUFFIX = r"\.(?:png|jpg|jpeg|webp|gif|mp4|webm|mov)"
#: An input or output image or video of a run (``_upload``).
_MEDIA_FILE = re.compile(
    rf"{_FOLDER}/(?:inputs|outputs)/[A-Za-z0-9_-]+{MEDIA_SUFFIX}"
)


@dataclasses.dataclass(frozen=True)
class RunRef:
    """One run: its id, the caller's session and its UTC creation date."""

    run_id: str
    session_id: str
    date: str

    def prefix(self) -> str:
        """The run folder in the media bucket (no trailing slash)."""
        return f"{RUNS_PREFIX}{self.date}/{self.session_id}/{self.run_id}"


def parse_mode(value: str | None, *, name: str) -> Mode | None:
    """Return ``None`` for "" (deployment default), else "inline" or "gcs"."""
    if not value:
        return None
    if value not in get_args(Mode):
        raise InputError(f"{name} must be inline, gcs or empty; got {value!r}")
    return cast(Mode, value)


def new_run(
    *, run_id: str | None = None, session_id: str | None = None
) -> RunRef:
    """A run with the caller's ids (validated) or a new id and no session."""
    run_id = run_id or uuid.uuid4().hex[:12]
    session_id = session_id or _NO_SESSION
    if not _RUN_ID.fullmatch(run_id):
        raise InputError("run_id must be 12 lowercase hex characters")
    if not _SESSION_ID.fullmatch(session_id):
        raise InputError(
            "session_id must be 1 to 64 letters, digits, '_' or '-'"
        )
    day = datetime.datetime.now(datetime.UTC).date().isoformat()
    return RunRef(run_id=run_id, session_id=session_id, date=day)


def folder_uri(run: RunRef, folder: Literal["inputs", "outputs"]) -> str:
    """``gs://<media bucket>/<run folder>/<folder>/``."""
    return f"gs://{get_settings().media_bucket}/{run.prefix()}/{folder}/"


def is_media_file(path: str) -> bool:
    """Whether ``path`` (in the media bucket) is an image or video a run
    saved in its ``inputs/`` or ``outputs/``."""
    return bool(_MEDIA_FILE.fullmatch(path))


def media_url(uri: str) -> str:
    """The ``GET /api/media`` URL that serves a saved file."""
    return "/api/media?uri=" + quote(uri, safe="")


def _upload(run: RunRef, folder: str, name: str, data: bytes) -> str:
    mime = sniff_mime(data)
    path = f"{run.prefix()}/{folder}/{name}.{EXTENSIONS.get(mime, 'bin')}"
    return gcs.upload(
        data,
        bucket=get_settings().media_bucket,
        path=path,
        content_type=mime,
    )


def save_inputs(run: RunRef, named: Mapping[str, bytes]) -> dict[str, str]:
    """Upload each input to the run's ``inputs/``; name -> ``gs://`` URI."""
    return {
        name: _upload(run, "inputs", name, data) for name, data in named.items()
    }


def _in_parallel(fn, items: list) -> list:
    """``fn`` of each item, all at once (Cloud Storage round trips)."""
    if not items:
        return []
    with ContextPool(max_workers=len(items)) as pool:
        return list(pool.map(fn, items))


def save_record(run: RunRef, event: RecordEvent) -> str:
    """Upload a record's images to the run's ``record/`` and its manifest
    (the payload plus ``files``: name -> ``gs://`` URI); return the
    manifest's URI."""
    names = list(event.files)
    uris = _in_parallel(
        lambda name: _upload(run, "record", name, event.files[name]), names
    )
    manifest = {**event.payload, "files": dict(zip(names, uris, strict=True))}
    return gcs.upload(
        json.dumps(manifest).encode(),
        bucket=get_settings().media_bucket,
        path=f"{run.prefix()}/{MANIFEST}",
        content_type="application/json",
    )


def _record_folder(source_run: str) -> str:
    """The ``gs://`` run folder ``source_run`` names (the folder, its
    manifest or any file in it); ``InputError`` for any other URI."""
    bucket = re.escape(get_settings().media_bucket)
    found = re.fullmatch(rf"(gs://{bucket}/{_FOLDER})(/[^?#]*)?", source_run)
    if not found or ".." in source_run.split("/"):
        raise InputError(
            "source_run must be a run folder of the media bucket "
            "(gs://<bucket>/runs/<date>/<session>/<run>) or a file in it"
        )
    return found[1]


def load_record(source_run: str) -> tuple[dict, dict[str, bytes]] | None:
    """The record of the run ``source_run`` names (``_record_folder``):
    its manifest and the bytes of each of its files by name; None when the
    run kept none (inline output, or a run made before records)."""
    folder = _record_folder(source_run)
    try:
        manifest = json.loads(gcs.download(f"{folder}/{MANIFEST}"))
    except NotFound:
        return None
    except ValueError as exc:
        raise InputError(f"source_run: unreadable {MANIFEST}") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(uri, str) and uri.startswith(f"{folder}/record/")
        for uri in files.values()
    ):
        raise InputError(f"source_run: {MANIFEST} has no valid files")
    names = list(files)
    data = _in_parallel(lambda name: gcs.download(files[name]), names)
    return manifest, dict(zip(names, data, strict=True))


async def _keep_record(event: RecordEvent, run: RunRef) -> Event | None:
    """Save a record; the event announcing its manifest, or None when the
    save failed (the run goes on without one)."""
    try:
        uri = await asyncio.to_thread(save_record, run, event)
    except Exception as exc:
        logger.warning(f"run record not saved: {exc}")
        return None
    return ProgressEvent(
        "record", "Saved the run record", data={"manifest": uri}
    )


async def _to_gcs(event: VariationEvent, run: RunRef) -> VariationEvent:
    """A copy of ``event`` with its media uploaded; inline if that fails.

    A copy, because the workflow may still use the bytes (video try-on
    animates the image variation it streamed).
    """
    if not event.media:
        return event
    parts = (event.extra.get("stage"), event.extra.get("view"), event.index)
    name = "-".join(str(part) for part in parts if part not in (None, ""))
    try:
        uri = await asyncio.to_thread(
            _upload, run, "outputs", name, event.media
        )
    except Exception as exc:
        logger.warning(f"upload of output {name} failed; inline: {exc}")
        extra = {**event.extra, "output_fallback": "inline"}
        return dataclasses.replace(event, extra=extra)
    return dataclasses.replace(
        event, media=None, media_uri=uri, media_url=media_url(uri)
    )


async def deliver(
    events: AsyncGenerator[Event, None],
    *,
    run: RunRef,
    mode: Mode | None = None,
    run_info: Mapping[str, str | None] | None = None,
) -> AsyncGenerator[Event, None]:
    """Announce the run, then stream ``events`` with media as ``mode`` says.

    The first event is ``ProgressEvent("run")`` with ``data`` = run_id,
    session_id, the ``inputs`` / ``outputs`` folder URIs (null when that
    side is inline) and ``manifest`` (null: set by the record event);
    ``run_info`` sets ``inputs`` (the adapter knows the input mode).
    ``mode=None`` uses ``settings.output_mode``. A
    ``RecordEvent`` is kept (gcs) or dropped (inline): see the module doc.
    Closing this stream closes ``events``.
    """
    mode = mode or get_settings().output_mode
    data = {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "inputs": None,
        "outputs": folder_uri(run, "outputs") if mode == "gcs" else None,
        "manifest": None,
        **(run_info or {}),
    }
    async with contextlib.aclosing(events):
        yield ProgressEvent("run", f"Run {run.run_id}", data=data)
        async for event in events:
            if isinstance(event, RecordEvent):
                kept = await _keep_record(event, run) if mode == "gcs" else None
                if kept is not None:
                    yield kept
            elif (
                mode == "gcs"
                and isinstance(event, VariationEvent)
                and event.media
            ):
                yield await _to_gcs(event, run)
            else:
                yield event
