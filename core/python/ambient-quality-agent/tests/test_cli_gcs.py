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

"""The CLI's Cloud Storage helper: the headers each request carries."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from ambient_quality_cli import gcs
from ambient_quality_shared import usage_headers
from ambient_quality_shared.usage_headers import build_product_token

_REMEDIATION = gcs.Remediation(forbidden="grant access", missing="create it")


def _record(monkeypatch: pytest.MonkeyPatch, method: str) -> list[dict]:
    """Replaces one `httpx` call with a fake that records its headers.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        method: `httpx` function to replace, such as ``get``.

    Returns:
        The headers of each call, in order.
    """
    seen: list[dict] = []

    def fake(url: str, **kwargs: Any) -> httpx.Response:
        seen.append(dict(kwargs["headers"]))
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, method, fake)
    return seen


def test_reads_and_writes_send_the_product_token(
    monkeypatch: pytest.MonkeyPatch, usage_analytics_on
) -> None:
    reads = _record(monkeypatch, "get")
    writes = _record(monkeypatch, "post")

    gcs.list_objects("bucket", "tok", "prefix/", _REMEDIATION)
    gcs.upload("bucket", "tok", "name", b"body", _REMEDIATION)

    for headers in (*reads, *writes):
        assert headers["Authorization"] == "Bearer tok"
        assert headers["user-agent"] == build_product_token()
        assert headers["x-goog-api-client"] == build_product_token()
    assert writes[0]["Content-Type"] == "application/octet-stream"


def test_the_switch_off_sends_only_the_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)
    reads = _record(monkeypatch, "get")

    gcs.list_objects("bucket", "tok", "prefix/", _REMEDIATION)

    assert reads == [{"Authorization": "Bearer tok"}]
