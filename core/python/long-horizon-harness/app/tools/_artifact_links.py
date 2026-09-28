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

"""Build a link to a saved artifact for the user.

When an artifact GCS bucket is configured (``LOGS_BUCKET_NAME``), mint a V4
signed URL straight to the object so clients without our frontend (e.g. Gemini
Enterprise) can open it. The bucket is private (uniform access +
public-access-prevention) and objects are per-user, so a signed URL is the only
way in and only ever names the caller's own object. Falls back to the
same-origin ``/lha/workspace/download`` endpoint (proxied under ``/lha`` in dev
and prod) when no bucket/signer is available — e.g. local dev.
"""

from __future__ import annotations

import datetime
import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SIGNED_URL_TTL = datetime.timedelta(days=7)  # GCS V4 signed-URL max lifetime


def workspace_download_url(
    workspace_rel_path: str, *, inline: bool = True
) -> str:
    qs = f"path={quote(workspace_rel_path)}"
    if inline:
        qs += "&inline=1"
    return f"/lha/workspace/download?{qs}"


def _signed_blob_url(
    bucket: str, blob_name: str, *, mime: str, inline: bool
) -> str | None:
    """V4 signed URL via IAM SignBlob (no private key). None on any failure."""
    try:
        import google.auth
        import google.auth.transport.requests
        from google.cloud import storage

        creds, _ = google.auth.default()
        if not creds.valid:  # google-auth needs a live token to IAM-sign
            creds.refresh(google.auth.transport.requests.Request())
        sa_email = getattr(creds, "service_account_email", None)
        if not sa_email:
            return None
        blob = storage.Client(credentials=creds).bucket(bucket).blob(blob_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=_SIGNED_URL_TTL,
            method="GET",
            service_account_email=sa_email,
            access_token=creds.token,
            # response_type overrides Content-Type so the browser renders inline
            # instead of downloading the bytes as octet-stream.
            response_type=mime if inline else None,
            response_disposition="inline" if inline else "attachment",
        )
    except Exception:
        logger.warning(
            "signed-url mint failed for gs://%s/%s",
            bucket,
            blob_name,
            exc_info=True,
        )
        return None


def artifact_url(
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    filename: str,
    version: int,
    rel_path: str,
    mime: str,
    inline: bool = True,
) -> str:
    """Signed GCS URL when a bucket is configured, else the same-origin endpoint.

    The blob path mirrors ADK ``GcsArtifactService._get_blob_name``
    (``{prefix}/{version}``, session-scoped prefix). Couples to ADK's private
    ``_get_blob_name`` method and is covered by the signing probe.
    """
    bucket = (
        os.environ.get("LOGS_BUCKET_NAME", "").strip().removeprefix("gs://")
    )
    if bucket:
        blob_name = f"{app_name}/{user_id}/{session_id}/{filename}/{version}"
        signed = _signed_blob_url(bucket, blob_name, mime=mime, inline=inline)
        if signed:
            return signed
    return workspace_download_url(rel_path, inline=inline)


__all__ = ["artifact_url", "workspace_download_url"]
