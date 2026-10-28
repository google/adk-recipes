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

"""The product token and the usage analytics headers built from it."""

from __future__ import annotations

import pytest
from ambient_quality_shared import __version__, usage_headers

_TOKEN = f"google-agents-cli-aqua/{__version__}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, usage_analytics_on) -> None:
    monkeypatch.delenv(usage_headers.ENGINE_ID_ENV, raising=False)


def test_the_token_names_the_product_and_version() -> None:
    assert usage_headers.build_product_token() == _TOKEN


@pytest.mark.parametrize("platform", list(usage_headers.Platform))
def test_the_platform_follows_the_version(
    platform: usage_headers.Platform,
) -> None:
    assert (
        usage_headers.build_product_token(platform=platform)
        == f"{_TOKEN}+{platform.value}"
    )


def test_a_process_off_agent_runtime_is_local() -> None:
    assert usage_headers.detect_platform() is usage_headers.Platform.LOCAL


def test_a_process_on_agent_runtime_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(usage_headers.ENGINE_ID_ENV, "1234567890")

    assert (
        usage_headers.detect_platform() is usage_headers.Platform.AGENT_RUNTIME
    )


def test_both_headers_carry_the_same_value() -> None:
    headers = usage_headers.build_usage_headers(
        platform=usage_headers.Platform.LOCAL
    )

    assert headers == {
        "user-agent": f"{_TOKEN}+local",
        "x-goog-api-client": f"{_TOKEN}+local",
    }


def test_without_a_platform_the_headers_carry_the_bare_token() -> None:
    assert usage_headers.build_usage_headers() == {
        "user-agent": _TOKEN,
        "x-goog-api-client": _TOKEN,
    }


def test_the_switch_off_sends_no_usage_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)
    monkeypatch.setenv(usage_headers.ENGINE_ID_ENV, "1234567890")

    assert usage_headers.build_product_token() is None
    assert (
        usage_headers.build_product_token(
            platform=usage_headers.Platform.AGENT_RUNTIME
        )
        is None
    )
    assert usage_headers.build_usage_headers() == {}
    assert (
        usage_headers.build_usage_headers(
            platform=usage_headers.Platform.AGENT_RUNTIME
        )
        == {}
    )
