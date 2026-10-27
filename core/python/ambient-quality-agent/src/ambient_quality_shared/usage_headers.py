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

"""The product token AQuA adds to the Google API requests it makes.

Every surface (engine, CLI, dashboard) sends
``google-agents-cli-aqua/VERSION`` in ``User-Agent`` and ``X-Goog-Api-Client``
for usage analytics. Engine requests add the `Platform` AQuA runs on. The
token names the client and its release, is the same for every user, and
carries no user data.

`USAGE_ANALYTICS_ENABLED` turns these headers on or off. While it is off, the
builders return nothing and every request carries only its client library's
own headers.

Standard library only: the CLI and the dashboard image ship this package with
few dependencies.
"""

from __future__ import annotations

import enum
import os

from ambient_quality_shared import __version__

USAGE_ANALYTICS_ENABLED = True
"""Whether AQuA sends any usage analytics header."""

PRODUCT_NAME = "google-agents-cli-aqua"

ENGINE_ID_ENV = "GOOGLE_CLOUD_AGENT_ENGINE_ID"
"""Set by Agent Runtime in a deployed engine, and absent elsewhere."""


class Platform(enum.Enum):
    """Where AQuA runs, sent by the engine after its product token.

    A new platform needs a member here and a case in `detect_platform`.
    """

    AGENT_RUNTIME = "agent_runtime"
    LOCAL = "local"


def detect_platform() -> Platform:
    """Reports the platform this process runs on.

    Returns:
        `Platform.AGENT_RUNTIME` when Agent Runtime set `ENGINE_ID_ENV`, else
        `Platform.LOCAL`, such as on a developer's machine.
    """
    return (
        Platform.AGENT_RUNTIME
        if os.environ.get(ENGINE_ID_ENV)
        else Platform.LOCAL
    )


def build_product_token(*, platform: Platform | None = None) -> str | None:
    """Builds the product token, ``google-agents-cli-aqua/VERSION``.

    Args:
        platform: Where the sender runs, appended as ``+<value>``, such as
            ``+agent_runtime``. The engine passes `detect_platform()`; None,
            as for the CLI and the dashboard, appends nothing.

    Returns:
        The token, or None while `USAGE_ANALYTICS_ENABLED` is off.
    """
    if not USAGE_ANALYTICS_ENABLED:
        return None
    token = f"{PRODUCT_NAME}/{__version__}"
    return token if platform is None else f"{token}+{platform.value}"


def build_usage_headers(*, platform: Platform | None = None) -> dict[str, str]:
    """Builds the ``user-agent`` and ``x-goog-api-client`` headers.

    Both carry the product token. Keys are lowercase, the form the Google
    client libraries merge their own tokens into.

    Args:
        platform: Where the sender runs; see `build_product_token`.

    Returns:
        The two headers, or an empty dict while `USAGE_ANALYTICS_ENABLED` is
        off.
    """
    token = build_product_token(platform=platform)
    if token is None:
        return {}
    return {"user-agent": token, "x-goog-api-client": token}
