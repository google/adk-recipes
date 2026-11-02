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

"""Cloud Storage I/O and safe resolution of caller-supplied image references."""

import functools
import logging
import threading
from datetime import timedelta
from urllib.parse import urlparse

import google.auth
import requests
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.auth.credentials import Credentials
from google.auth.transport import requests as auth_requests
from google.cloud import storage

from genmedia4commerce.core import catalog
from genmedia4commerce.core.errors import ConfigError, InputError
from genmedia4commerce.core.settings import get_settings

__all__ = [
    "MAX_IMAGE_BYTES",
    "blob",
    "create",
    "delete",
    "download",
    "list_paths",
    "resolve_image",
    "signed_url",
    "upload",
]

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 2**20  # one input image (uploads and https:// fetches)
_HTTP_TIMEOUT_S = 20
_SIGNING_LOCK = threading.Lock()


@functools.cache
def _client() -> storage.Client:
    return storage.Client(project=get_settings().project_id)


def blob(uri: str) -> storage.Blob:
    """The ``gs://bucket/path`` object (no request is made)."""
    bucket, _, path = uri.removeprefix("gs://").partition("/")
    if not uri.startswith("gs://") or not bucket or not path:
        raise InputError(f"not a gs:// object URI: {uri!r}")
    return _client().bucket(bucket).blob(path)


def download(uri: str) -> bytes:
    """Download a ``gs://bucket/path`` object."""
    return blob(uri).download_as_bytes()


def list_paths(
    bucket: str, prefix: str, match_glob: str | None = None
) -> list[str]:
    """The object names under ``prefix`` in ``bucket``, in name order,
    optionally only those matching ``match_glob`` (Cloud Storage globs)."""
    found = _client().list_blobs(bucket, prefix=prefix, match_glob=match_glob)
    return [b.name for b in found]


def delete(uri: str) -> None:
    """Delete an object under ``sessions/`` in the media bucket (the Studio's
    own documents); any other URI is an ``InputError``."""
    bucket, _, path = uri.removeprefix("gs://").partition("/")
    if (
        bucket != get_settings().media_bucket
        or not path.startswith("sessions/")
        or ".." in path.split("/")
    ):
        raise InputError(f"only sessions/ in the media bucket: {uri!r}")
    blob(uri).delete()


def upload(data: bytes, *, bucket: str, path: str, content_type: str) -> str:
    """Upload bytes and return the ``gs://`` URI."""
    _client().bucket(bucket).blob(path).upload_from_string(
        data, content_type=content_type
    )
    return f"gs://{bucket}/{path}"


def create(data: bytes, uri: str, *, content_type: str) -> bool:
    """Upload bytes to ``uri`` only if no object is there yet
    (``if_generation_match=0``); False when one already is."""
    try:
        blob(uri).upload_from_string(
            data, content_type=content_type, if_generation_match=0
        )
    except PreconditionFailed:
        return False
    return True


@functools.cache
def _credentials() -> Credentials:
    return google.auth.default()[0]


def _signer() -> tuple[str, str]:
    """The service account email and a valid access token to sign with.

    The credentials are loaded once and refreshed only when their token is
    missing or expired.
    """
    with _SIGNING_LOCK:
        creds = _credentials()
        if not creds.valid:
            creds.refresh(auth_requests.Request())
        email = getattr(creds, "service_account_email", None)
        token = creds.token
    if not email:
        raise ConfigError("these credentials cannot sign URLs")
    return email, token


def signed_url(uri: str, *, minutes: int = 60) -> str:
    """A V4 signed GET URL for a ``gs://`` object.

    Signs with the IAM ``signBlob`` API, so the runtime service account needs
    ``roles/iam.serviceAccountTokenCreator`` on itself. Raises
    ``ConfigError`` when the credentials cannot sign (for example, user
    credentials on a laptop).
    """
    email, token = _signer()
    return blob(uri).generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=minutes),
        method="GET",
        service_account_email=email,
        access_token=token,
    )


def _download_ours(uri: str) -> bytes:
    """Download from the media bucket's ``runs/`` only."""
    from genmedia4commerce.core.runs import RUNS_PREFIX  # runs uses gcs

    bucket, _, path = uri.removeprefix("gs://").partition("/")
    if bucket != get_settings().media_bucket:
        raise InputError("gs:// images must be in the media bucket")
    if not path.startswith(RUNS_PREFIX) or ".." in path.split("/"):
        raise InputError(
            f"gs:// images in the media bucket must be under {RUNS_PREFIX}"
        )
    try:
        return download(uri)
    except NotFound as exc:
        raise InputError(f"image not found: {uri}") from exc


def _fetch(url: str) -> bytes:
    """GET an https:// image: no redirects, at most ``MAX_IMAGE_BYTES``."""
    data = bytearray()
    try:
        with requests.get(
            url, timeout=_HTTP_TIMEOUT_S, stream=True, allow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise InputError(
                    f"could not fetch image {url}: HTTP {response.status_code}"
                )
            for chunk in response.iter_content(chunk_size=2**20):
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    raise InputError(
                        f"image {url} is larger than "
                        f"{MAX_IMAGE_BYTES // 2**20} MB"
                    )
    except requests.RequestException as exc:
        raise InputError(f"could not fetch image {url}: {exc}") from exc
    return bytes(data)


def resolve_image(ref: str | bytes) -> bytes:
    """Turn an image reference into bytes.

    Bytes pass through; ``catalog:`` refs are read from the local catalogue
    (``core.catalog``); ``gs://`` is downloaded from ``runs/`` in the media
    bucket only; ``https://`` is fetched only from
    ``settings.allowed_image_hosts`` (no redirects, at most 20 MB). Anything
    else is an ``InputError``.
    """
    if isinstance(ref, bytes):
        return ref
    if not isinstance(ref, str):
        raise InputError(
            f"unsupported image reference type {type(ref).__name__}"
        )
    if catalog.is_ref(ref):
        return catalog.image(*catalog.parse_ref(ref))
    if ref.startswith("gs://"):
        return _download_ours(ref)
    url = urlparse(ref)
    if url.scheme != "https":
        raise InputError(
            "image references must be bytes, catalog: refs, gs:// or "
            "https:// URLs"
        )
    if url.hostname not in get_settings().allowed_image_hosts:
        raise InputError(f"image host {url.hostname!r} is not allowed")
    return _fetch(ref)
