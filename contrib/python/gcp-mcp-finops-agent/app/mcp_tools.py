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
"""Connection to the Google-hosted BigQuery MCP server."""

import google.auth
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from app import config

_credentials: Credentials | None = None
_default_project: str = ""


def gcp_auth_headers(
    _context: ReadonlyContext | None = None,
) -> dict[str, str]:
    """Returns auth headers for one MCP request.

    ADK calls this before every request, so the token is refreshed when it
    expires (about hourly). Credentials load on first use, which keeps the
    agent importable on a machine without credentials.
    """
    global _credentials, _default_project

    if _credentials is None:
        _credentials, project = google.auth.default(scopes=config.GCP_SCOPES)
        _default_project = project or ""

    if not _credentials.valid:
        _credentials.refresh(Request())

    return {
        "Authorization": f"Bearer {_credentials.token}",
        "x-goog-user-project": config.QUOTA_PROJECT or _default_project,
    }


def bigquery_toolset() -> McpToolset:
    """Builds the read-only BigQuery MCP toolset. Connects on first use."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=config.BIGQUERY_MCP_URL,
            timeout=config.MCP_TIMEOUT_SECONDS,
            sse_read_timeout=config.MCP_SSE_READ_TIMEOUT_SECONDS,
        ),
        header_provider=gcp_auth_headers,
        tool_filter=config.READONLY_BIGQUERY_TOOLS,
    )
