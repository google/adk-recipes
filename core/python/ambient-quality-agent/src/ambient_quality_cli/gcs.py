# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GCS REST client for CLI commands using HTTPX and ADC authentication.

Uses direct REST calls to avoid a `google-cloud-storage` dependency.
Accepts caller-defined :class:`Remediation` instructions to provide context-specific
error guidance (e.g. for metrics publishing vs. trace dumping) on 403 or 404 responses.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import urllib.parse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_BASE = "https://storage.googleapis.com/storage/v1/b"
_UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b"

_LIST_PAGE_SIZE = "1000"
_READ_TIMEOUT = 60.0
_WRITE_TIMEOUT = 120.0

# 64 KiB chunk size for streaming downloads without buffering full objects in memory.
_STREAM_CHUNK_BYTES = 1 << 16


class GcsError(Exception):
    """Raised when a GCS operation fails with actionable operator guidance."""


@contextlib.contextmanager
def _translate_transport_errors(bucket: str) -> Iterator[None]:
    """Translates network and transport errors into `GcsError`.

    Args:
        bucket: GCS bucket name being accessed.

    Yields:
        None.

    Raises:
        GcsError: If an HTTPX network or connection error occurs.
    """
    import httpx

    try:
        yield
    except httpx.HTTPError as exc:
        raise GcsError(
            f"could not reach gs://{bucket}: {type(exc).__name__}: {exc}"
        ) from exc


@dataclasses.dataclass(frozen=True)
class Remediation:
    """Actionable guidance displayed to operators on HTTP 403 or 404 errors.

    Attributes:
        forbidden: Resolution steps for HTTP 403 (e.g. missing IAM roles).
        missing: Resolution steps for HTTP 404 (e.g. undeployed infrastructure).
    """

    forbidden: str
    missing: str


def list_objects(
    bucket: str, token: str, prefix: str, remediation: Remediation
) -> list[dict[str, Any]]:
    """Lists all objects under a prefix in a GCS bucket, handling pagination.

    Args:
        bucket: GCS bucket name.
        token: OAuth2 access token.
        prefix: Object name prefix to filter by.
        remediation: Guidance displayed if the request fails with 403 or 404.

    Returns:
        List of object resource dictionaries from the GCS API.

    Raises:
        GcsError: If the request fails.
    """
    import httpx

    items: list[dict[str, Any]] = []
    page: str | None = None
    while True:
        params = {"prefix": prefix, "maxResults": _LIST_PAGE_SIZE}
        if page:
            params["pageToken"] = page
        with _translate_transport_errors(bucket):
            response = httpx.get(
                f"{_BASE}/{urllib.parse.quote(bucket, safe='')}/o",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_READ_TIMEOUT,
            )
        raise_for_status(response, bucket, remediation)
        body = response.json()
        items.extend(body.get("items") or [])
        page = body.get("nextPageToken")
        if not page:
            return items


def read(bucket: str, token: str, name: str, remediation: Remediation) -> bytes:
    """Downloads a GCS object completely into memory.

    Args:
        bucket: GCS bucket name.
        token: OAuth2 access token.
        name: Object key name.
        remediation: Guidance displayed if the request fails with 403 or 404.

    Returns:
        Raw bytes of the downloaded object.

    Raises:
        GcsError: If the download fails.
    """
    import httpx

    with _translate_transport_errors(bucket):
        response = httpx.get(
            _build_object_url(bucket, name),
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_READ_TIMEOUT,
        )
    raise_for_status(response, bucket, remediation)
    return response.content


@contextlib.contextmanager
def stream(
    bucket: str, token: str, name: str, remediation: Remediation
) -> Iterator[Iterator[bytes]]:
    """Streams a GCS object in chunks without buffering the full payload in memory.

    Args:
        bucket: GCS bucket name.
        token: OAuth2 access token.
        name: Object key name.
        remediation: Guidance displayed if the request fails with 403 or 404.

    Yields:
        Iterator yielding raw byte chunks of the object.

    Raises:
        GcsError: If the stream cannot be initiated or fails mid-transfer.
    """
    import httpx

    # Guard stream iteration so dropped connections during consumption raise GcsError.
    with (
        _translate_transport_errors(bucket),
        httpx.stream(
            "GET",
            _build_object_url(bucket, name),
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_READ_TIMEOUT,
        ) as response,
    ):
        if response.status_code >= 400:
            # Read response body before checking status to capture error details.
            response.read()
            raise_for_status(response, bucket, remediation)
        yield response.iter_bytes(_STREAM_CHUNK_BYTES)


def upload(
    bucket: str, token: str, name: str, body: bytes, remediation: Remediation
) -> None:
    """Uploads binary data to a GCS object.

    Args:
        bucket: Target GCS bucket name.
        token: OAuth2 access token.
        name: Destination object key name.
        body: Raw bytes to upload.
        remediation: Guidance displayed if the request fails with 403 or 404.

    Raises:
        GcsError: If the upload fails.
    """
    import httpx

    with _translate_transport_errors(bucket):
        response = httpx.post(
            f"{_UPLOAD}/{urllib.parse.quote(bucket, safe='')}/o",
            params={"uploadType": "media", "name": name},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=body,
            timeout=_WRITE_TIMEOUT,
        )
    raise_for_status(response, bucket, remediation)


def delete(
    bucket: str, token: str, name: str, remediation: Remediation
) -> None:
    """Deletes a GCS object, treating missing objects (404) as success.

    Args:
        bucket: GCS bucket name.
        token: OAuth2 access token.
        name: Object key name to delete.
        remediation: Guidance displayed if the request fails with 403.

    Raises:
        GcsError: If deletion fails for reasons other than 404.
    """
    import httpx

    with _translate_transport_errors(bucket):
        response = httpx.delete(
            _build_object_url(bucket, name),
            headers={"Authorization": f"Bearer {token}"},
            timeout=_READ_TIMEOUT,
        )
    if response.status_code == 404:
        return
    raise_for_status(response, bucket, remediation)


def raise_for_status(
    response: Any, bucket: str, remediation: Remediation
) -> None:
    """Translates GCS HTTP error responses into descriptive GcsError exceptions.

    Formats 403 and 404 responses with caller-provided remediation instructions.

    Args:
        response: HTTPX response object.
        bucket: GCS bucket name associated with the request.
        remediation: Guidance for 403 and 404 status codes.

    Raises:
        GcsError: If the response status code indicates an error (>= 400).
    """
    if response.status_code < 400:
        return
    if response.status_code == 403:
        raise GcsError(
            f"no permission on gs://{bucket}. {remediation.forbidden}"
        )
    if response.status_code == 404:
        raise GcsError(f"gs://{bucket} does not exist. {remediation.missing}")
    try:
        detail = json.dumps(response.json().get("error", {}).get("message", ""))
    except Exception:
        detail = response.text[:200]
    raise GcsError(
        f"GCS returned {response.status_code} for {bucket}: {detail}"
    )


def _build_object_url(bucket: str, name: str) -> str:
    return (
        f"{_BASE}/{urllib.parse.quote(bucket, safe='')}"
        f"/o/{urllib.parse.quote(name, safe='')}"
    )
