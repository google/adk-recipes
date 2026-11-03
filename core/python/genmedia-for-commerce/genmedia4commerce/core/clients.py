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

"""The one ``genai.Client`` factory and the one HTTP retry policy."""

import functools
import sys
import threading
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from google import genai
from google.genai import types

from genmedia4commerce.core.settings import get_settings

__all__ = ["RETRY_OPTIONS", "get_client", "reset_clients", "tracking_headers"]

PACKAGE_NAME = "genmedia-for-commerce"

# 1 initial + 4 retries, backoff 1, 2, 4, 8 s plus jitter (each at most 30 s).
RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=5,
    initial_delay=1.0,
    exp_base=2.0,
    max_delay=30.0,
    http_status_codes=[408, 429, 500, 502, 503, 504],
)


# Two threads missing the cache at once would each build a client and one
# would be dropped; genai.Client closes its HTTP client when collected, so a
# ``get_client().models`` still in use fails with "client has been closed".
_LOCK = threading.Lock()


def tracking_headers() -> dict[str, str]:
    """The recipe's identifier on every Gemini call, so its usage can be
    counted (the same identifier the recipe has always sent). The SDK keeps
    these headers and puts its own label in front
    (``google.genai._api_client.append_library_version_headers``); the keys
    are lowercase, as the SDK merges on them."""
    try:
        version = package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        version = "0.0.0"
    agent = f"{version}-{PACKAGE_NAME}/{version}-{PACKAGE_NAME}"
    python = ".".join(str(n) for n in sys.version_info[:3])
    return {
        "user-agent": agent,
        "x-goog-api-client": f"{agent} gl-python/{python} gccl/{agent}",
    }


@functools.cache
def _build(location: str, api_revision: str | None) -> genai.Client:
    settings = get_settings()
    headers = tracking_headers()
    if api_revision:
        headers["Api-Revision"] = api_revision
    http_options = types.HttpOptions(
        headers=headers, retry_options=RETRY_OPTIONS
    )
    if settings.google_api_key:
        return genai.Client(
            api_key=settings.google_api_key, http_options=http_options
        )
    return genai.Client(
        vertexai=True,
        project=settings.project_id,
        location=location,
        http_options=http_options,
    )


def get_client(
    *, location: str | None = None, api_revision: str | None = None
) -> genai.Client:
    """Return the cached client for ``(location, api_revision)``.

    ``location=None`` uses ``settings.location``. With ``GOOGLE_API_KEY`` set
    the client targets AI Studio instead of Vertex AI.
    """
    with _LOCK:
        return _build(location or get_settings().location, api_revision)


def reset_clients() -> None:
    """Drop every cached client (tests, settings changes)."""
    _build.cache_clear()
