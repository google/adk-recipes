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
"""Unit tests for the MCP connection, config and instruction.

None of these tests need credentials or a network connection.
"""

import pytest

from app import config, mcp_tools
from app.prompt import build_instruction


class FakeCredentials:
    """Credentials that expire on demand and count their refreshes."""

    def __init__(self) -> None:
        self.valid = False
        self.token = ""
        self.refresh_count = 0

    def refresh(self, _request: object) -> None:
        self.refresh_count += 1
        self.token = f"token-{self.refresh_count}"
        self.valid = True


@pytest.fixture
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> FakeCredentials:
    credentials = FakeCredentials()
    monkeypatch.setattr(mcp_tools, "_credentials", None)
    monkeypatch.setattr(mcp_tools, "_default_project", "")
    monkeypatch.setattr(config, "QUOTA_PROJECT", None)
    monkeypatch.setattr(
        mcp_tools.google.auth,
        "default",
        lambda scopes=None: (credentials, "adc-project"),
    )
    return credentials


def test_headers_carry_token_and_quota_project(
    fake_credentials: FakeCredentials,
) -> None:
    headers = mcp_tools.gcp_auth_headers()

    assert headers["Authorization"] == "Bearer token-1"
    assert headers["x-goog-user-project"] == "adc-project"


def test_valid_token_is_reused(fake_credentials: FakeCredentials) -> None:
    mcp_tools.gcp_auth_headers()
    mcp_tools.gcp_auth_headers()

    assert fake_credentials.refresh_count == 1


def test_expired_token_is_refreshed(fake_credentials: FakeCredentials) -> None:
    mcp_tools.gcp_auth_headers()
    fake_credentials.valid = False

    headers = mcp_tools.gcp_auth_headers()

    assert fake_credentials.refresh_count == 2
    assert headers["Authorization"] == "Bearer token-2"


def test_configured_quota_project_wins(
    fake_credentials: FakeCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "QUOTA_PROJECT", "my-quota-project")

    headers = mcp_tools.gcp_auth_headers()

    assert headers["x-goog-user-project"] == "my-quota-project"


def test_toolset_is_read_only() -> None:
    toolset = mcp_tools.bigquery_toolset()

    assert toolset.tool_filter == config.READONLY_BIGQUERY_TOOLS
    assert "execute_sql" not in config.READONLY_BIGQUERY_TOOLS


def test_billing_project_comes_from_the_table() -> None:
    assert config.billing_project("billing-prj.ds.export") == "billing-prj"


def test_billing_project_override_wins() -> None:
    assert config.billing_project("billing-prj.ds.export", "other") == "other"


def test_billing_project_needs_a_full_table_name() -> None:
    assert config.billing_project(None) == ""
    assert config.billing_project("") == ""
    assert config.billing_project("ds.export") == ""


def test_instruction_names_table_and_project() -> None:
    instruction = build_instruction("billing-prj.ds.export", "billing-prj")

    assert "`billing-prj.ds.export`" in instruction
    assert "project `billing-prj`" in instruction
    assert "__BILLING" not in instruction


def test_instruction_without_table_refuses_to_guess() -> None:
    instruction = build_instruction(None, "")

    assert "BILLING_TABLE" in instruction
    assert "Do not estimate" in instruction
