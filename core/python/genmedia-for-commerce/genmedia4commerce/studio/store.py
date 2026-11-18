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

"""Studio documents in the media bucket, readable from every instance.

Cloud Run runs several instances, and a later call of the app can reach any
of them, so what the app reads back is a small JSON file in the studio's
folder, ``sessions/studio-<id>/`` (``session_id``; ``check_id`` says which
ids are valid: they are never rewritten, so two ids never share a folder):

- ``adk/``: the agent's ADK session (``studio.sessions``);
- ``state.json``: the latest studio state;
- ``results.json``: the latest generation's results;
- ``sessions/studio-jobs/<job_id>.json``: a generation job, rewritten as
  its events arrive.

Writers on different instances share these files, so a read-modify-write
goes through ``update``: it writes only if the object is still the
generation it read, and starts again otherwise.
"""

import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any

from google.api_core.exceptions import (
    NotFound,
    PreconditionFailed,
    TooManyRequests,
)

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import InputError, StorageError
from genmedia4commerce.core.settings import get_settings

__all__ = [
    "JOB_ID_PATTERN",
    "MAX_STUDIO_ID",
    "STUDIO_ID_PATTERN",
    "check_id",
    "check_job_id",
    "job_path",
    "read",
    "results_path",
    "session_id",
    "state_path",
    "update",
    "write",
]

logger = logging.getLogger(__name__)

#: A studio id is at most this long, so that its run session
#: ``studio-<id>`` fits the 64 characters of a run's session id
#: (``core.runs``).
MAX_STUDIO_ID = 57
#: Valid studio and job ids (also the REST API's path patterns).
STUDIO_ID_PATTERN = rf"^[A-Za-z0-9_-]{{1,{MAX_STUDIO_ID}}}$"
JOB_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
#: ``studio-jobs`` is the jobs folder, not a studio.
_RESERVED = ("jobs",)
#: Tries of one ``update`` (another writer won, or the object's write rate
#: limit of about one per second was hit) and the first backoff.
_ATTEMPTS = 6
_BACKOFF_S = 0.1


def check_id(studio_id: object) -> str:
    """``studio_id`` if it is a valid studio id (``STUDIO_ID_PATTERN``, not
    ``jobs``); ``InputError`` otherwise. Ids are never rewritten, so two
    different ids never share a studio."""
    if (
        not isinstance(studio_id, str)
        or not re.fullmatch(STUDIO_ID_PATTERN, studio_id)
        or studio_id in _RESERVED
    ):
        raise InputError(
            f"studio_id must be 1 to {MAX_STUDIO_ID} letters, digits, '_' "
            "or '-' (and not 'jobs')"
        )
    return studio_id


def check_job_id(job_id: object) -> str:
    """``job_id`` if it is a valid job id; ``InputError`` otherwise."""
    if not isinstance(job_id, str) or not re.fullmatch(JOB_ID_PATTERN, job_id):
        raise InputError("job_id must be 1 to 64 letters, digits, '_' or '-'")
    return job_id


def session_id(studio_id: str) -> str:
    """The studio's folder name ``studio-<id>`` (``sessions/studio-<id>/``)
    and the run session of its uploads and generations
    (``runs/<date>/studio-<id>/``); ``InputError`` for an invalid id."""
    return f"studio-{check_id(studio_id)}"


def state_path(studio_id: str) -> str:
    return f"sessions/{session_id(studio_id)}/state.json"


def results_path(studio_id: str) -> str:
    return f"sessions/{session_id(studio_id)}/results.json"


def job_path(job_id: str) -> str:
    return f"sessions/studio-jobs/{check_job_id(job_id)}.json"


def _uri(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def _get(path: str) -> tuple[Any, int]:
    """The document at ``path`` and its generation; ``(None, 0)`` if absent.

    Other storage errors and invalid JSON raise as they are.
    """
    blob = gcs.blob(_uri(path))
    try:
        raw = blob.download_as_bytes()
    except NotFound:
        return None, 0
    return json.loads(raw), int(blob.generation or 0)


def read(path: str) -> Any:
    """The JSON document at ``path`` in the media bucket, ``None`` if absent.

    Raises ``StorageError`` when it exists but cannot be read, so a caller
    never mistakes a failed read for an empty document.
    """
    try:
        return _get(path)[0]
    except Exception as exc:
        raise StorageError(f"could not read {path}: {exc}") from exc


def write(path: str, doc: Any) -> bool:
    """Store ``doc`` at ``path`` (last writer wins); whether it worked."""
    try:
        gcs.upload(
            json.dumps(doc).encode(),
            bucket=get_settings().media_bucket,
            path=path,
            content_type="application/json",
        )
    except Exception as exc:
        logger.warning(f"could not write {path}: {exc}")
        return False
    return True


def update(path: str, change: Callable[[Any], Any]) -> bool:
    """Read-modify-write ``path`` safely across instances; whether it worked.

    ``change`` gets the stored document (``None`` when absent) and returns
    the new one; it may run more than once, so it must not keep state. The
    write only succeeds if nobody wrote in between (generation
    precondition); on a conflict or a rate limit it starts again after a
    short backoff. A failed read is never overwritten. Failures are logged.
    """
    for attempt in range(_ATTEMPTS):
        try:
            doc, generation = _get(path)
            data = json.dumps(change(doc)).encode()
            gcs.blob(_uri(path)).upload_from_string(
                data,
                content_type="application/json",
                if_generation_match=generation,
            )
            return True
        except (PreconditionFailed, TooManyRequests) as exc:
            reason = exc
        except Exception as exc:
            logger.warning(f"could not update {path}: {exc}")
            return False
        if attempt + 1 < _ATTEMPTS:
            jitter = random.uniform(0.5, 1.5)  # noqa: S311 (not security)
            time.sleep(_BACKOFF_S * 2**attempt * jitter)
    logger.warning(f"gave up updating {path}: {reason}")
    return False
