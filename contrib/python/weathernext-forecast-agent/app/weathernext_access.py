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
"""Checks Google Cloud credentials and WeatherNext allowlist access.

WeatherNext data on Cloud Storage is readable only by approved Google
accounts. Before reading, this check confirms that Application Default
Credentials exist and that the account can list the configured store, and
explains what to do otherwise. Run it on its own with::

    uv run python -m app.weathernext_access
"""

import os
import sys
from typing import Any

from .config import require_env

ACCESS_FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSeCf1JY8G78UDWzbm0ly9kJxfSjUIJT5WyMR_HiNqCm-IHIBg/viewform"
)
ACCESS_DOCS_URL = (
    "https://developers.google.com/weathernext/guides/access-forecast"
    "#step_1_request_data_access"
)
_LIST_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"
_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_only"]
_TIMEOUT_SECONDS = 30


def check_access(
    uri: str, billing_project: str | None = None, session: Any = None
) -> tuple[bool, str]:
    """Lists one object under `uri` with the caller's credentials.

    Returns:
        (ok, message): `message` says what to do when `ok` is False.
    """
    from .forecast import split_gcs_uri

    bucket, prefix = split_gcs_uri(uri)
    if session is None:
        import google.auth
        from google.auth import exceptions
        from google.auth.transport.requests import AuthorizedSession

        try:
            credentials, _ = google.auth.default(scopes=_SCOPES)
        except exceptions.DefaultCredentialsError:
            return False, (
                "No Google Cloud Application Default Credentials found. Run"
                " `gcloud auth application-default login` and try again."
            )
        session = AuthorizedSession(credentials)
    params = {"maxResults": "1", "prefix": prefix}
    if billing_project:
        params["userProject"] = billing_project
    resp = session.get(
        _LIST_URL.format(bucket=bucket), params=params, timeout=_TIMEOUT_SECONDS
    )
    if resp.status_code == 200:
        return True, f"Access to gs://{bucket}/{prefix} confirmed."
    if resp.status_code == 401:
        return False, (
            "Google Cloud credentials are invalid or expired. Run `gcloud auth"
            " application-default login` and try again."
        )
    if resp.status_code == 403:
        return False, (
            f"Access denied to gs://{bucket}/. Your Google account is not on"
            " the WeatherNext data allowlist yet. Request access with the"
            f" WeatherNext data request form ({ACCESS_FORM_URL}, see"
            f" {ACCESS_DOCS_URL}); approval is manual and reading will fail"
            " until it is granted."
        )
    if resp.status_code == 400 and "user project" in resp.text.lower():
        return False, (
            f"gs://{bucket}/ is a Requester Pays bucket. Set"
            " WEATHERNEXT_BILLING_PROJECT to a Google Cloud project you can"
            " bill."
        )
    return False, (
        f"Unexpected HTTP {resp.status_code} listing gs://{bucket}/{prefix}:"
        f" {resp.text[:200]}"
    )


def main() -> int:
    """Prints the access check result for WEATHERNEXT_ZARR_URI.

    Importing the `app` package has already loaded `.env`.
    """
    ok, message = check_access(
        require_env("WEATHERNEXT_ZARR_URI", os.getenv("WEATHERNEXT_ZARR_URI")),
        os.getenv("WEATHERNEXT_BILLING_PROJECT") or None,
    )
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
