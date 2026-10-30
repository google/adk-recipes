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

"""The `google_drive` server's tools against a fake Drive REST API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from workspace_mcp._google import GoogleApi
from workspace_mcp.drive import TEXT_LIMIT, server

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"


def _api(
    reply: Callable[[httpx.Request], httpx.Response],
) -> tuple[GoogleApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request)

    api = GoogleApi("fake-token", transport=httpx.MockTransport(record))
    return api, seen


def _meta(mime_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": "f1",
        "name": "Plan",
        "mimeType": mime_type,
        "webViewLink": "https://drive.test/f1",
        **extra,
    }


def _file_api(
    meta: dict[str, Any], body: str = ""
) -> tuple[GoogleApi, list[httpx.Request]]:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/export") or (
            request.url.params.get("alt") == "media"
        ):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=meta)

    return _api(reply)


@pytest.mark.asyncio
async def test_search_files_builds_an_escaped_query() -> None:
    api, seen = _api(
        lambda r: httpx.Response(
            200,
            json={
                "nextPageToken": "p2",
                "files": [
                    {
                        "id": "f1",
                        "name": "Q3 plan",
                        "mimeType": DOC,
                        "modifiedTime": "2026-10-01T00:00:00Z",
                        "webViewLink": "https://drive.test/f1",
                        "owners": [{"displayName": "Ada"}],
                    }
                ],
            },
        )
    )
    result = await server.call_tool(
        "search_files",
        {
            "query": "bob's \\plan",
            "mime_type": DOC,
            "max_results": 500,
            "page_token": "p1",
        },
        api=api,
    )
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "files": [
            {
                "id": "f1",
                "name": "Q3 plan",
                "mime_type": DOC,
                "modified_time": "2026-10-01T00:00:00Z",
                "web_view_link": "https://drive.test/f1",
            }
        ],
        "next_page_token": "p2",
    }
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/drive/v3/files"
    params = request.url.params
    assert params["q"] == (
        "(name contains 'bob\\'s \\\\plan' or fullText contains "
        "'bob\\'s \\\\plan') and trashed = false and "
        f"mimeType = '{DOC}'"
    )
    assert params["pageSize"] == "50"
    assert params["pageToken"] == "p1"
    assert params["supportsAllDrives"] == "true"
    assert params["includeItemsFromAllDrives"] == "true"
    assert "orderBy" not in params
    assert "files(id,name,mimeType" in params["fields"]


@pytest.mark.asyncio
async def test_search_files_without_a_query_lists_recent_files() -> None:
    api, seen = _api(lambda r: httpx.Response(200, json={}))
    result = await server.call_tool("search_files", {}, api=api)
    assert result["structuredContent"] == {"files": []}
    params = seen[0].url.params
    assert params["q"] == "trashed = false"
    assert params["orderBy"] == "modifiedTime desc"
    assert params["pageSize"] == "20"
    assert "pageToken" not in params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"max_results": 0}, "`max_results` should be at least 1"),
        ({"max_results": "5"}, "`max_results` should be a integer"),
    ],
)
async def test_search_files_rejects_bad_arguments(
    arguments: dict[str, Any], expected: str
) -> None:
    api, seen = _api(lambda r: httpx.Response(200, json={}))
    result = await server.call_tool("search_files", arguments, api=api)
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime_type", "export_as"),
    [
        (DOC, "text/plain"),
        (SHEET, "text/csv"),
        ("application/vnd.google-apps.presentation", "text/plain"),
    ],
)
async def test_read_file_exports_google_files(
    mime_type: str, export_as: str
) -> None:
    api, seen = _file_api(_meta(mime_type), "hello")
    result = await server.call_tool("read_file", {"file_id": "f1"}, api=api)
    assert result["structuredContent"] == {
        "id": "f1",
        "name": "Plan",
        "mime_type": mime_type,
        "web_view_link": "https://drive.test/f1",
        "text": "hello",
    }
    meta_request, export_request = seen
    assert meta_request.url.path == "/drive/v3/files/f1"
    assert meta_request.url.params["fields"] == (
        "id,name,mimeType,size,webViewLink"
    )
    assert export_request.method == "GET"
    assert export_request.url.path == "/drive/v3/files/f1/export"
    assert export_request.url.params["mimeType"] == export_as


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type", ["text/markdown", "application/json", "application/xml"]
)
async def test_read_file_downloads_text_files(mime_type: str) -> None:
    api, seen = _file_api(_meta(mime_type, size="5"), "a,b\n")
    result = await server.call_tool("read_file", {"file_id": "f1"}, api=api)
    assert result["structuredContent"]["text"] == "a,b\n"
    assert "truncated" not in result["structuredContent"]
    download = seen[1]
    assert download.url.path == "/drive/v3/files/f1"
    assert download.url.params["alt"] == "media"
    assert download.headers["Range"].startswith("bytes=0-")


@pytest.mark.asyncio
async def test_read_file_truncates_long_text() -> None:
    api, _ = _file_api(_meta(DOC), "x" * (TEXT_LIMIT + 10))
    result = await server.call_tool("read_file", {"file_id": "f1"}, api=api)
    content = result["structuredContent"]
    assert len(content["text"]) == TEXT_LIMIT
    assert content["truncated"] is True


@pytest.mark.asyncio
async def test_read_file_marks_a_partly_downloaded_file_truncated() -> None:
    api, _ = _file_api(_meta("text/plain", size="999999999"), "short")
    result = await server.call_tool("read_file", {"file_id": "f1"}, api=api)
    assert result["structuredContent"]["text"] == "short"
    assert result["structuredContent"]["truncated"] is True


@pytest.mark.asyncio
async def test_read_file_returns_metadata_only_for_binary_files() -> None:
    api, seen = _file_api(_meta("application/pdf", size="10"))
    result = await server.call_tool("read_file", {"file_id": "f1"}, api=api)
    content = result["structuredContent"]
    assert content["id"] == "f1"
    assert "text" not in content
    assert "isn't readable as text" in content["note"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_read_file_not_found_is_an_error_hint() -> None:
    api, _ = _api(
        lambda r: httpx.Response(
            404, json={"error": {"message": "File not found: nope."}}
        )
    )
    result = await server.call_tool("read_file", {"file_id": "nope"}, api=api)
    assert result["isError"] is True
    assert "not found: check the ID" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "missing required argument(s): file_id"),
        ({"file_id": "  "}, "should be a Drive file ID"),
        (
            {"file_id": "https://docs.google.com/document/d/f1/edit"},
            "not a URL",
        ),
    ],
)
async def test_read_file_rejects_bad_arguments(
    arguments: dict[str, Any], expected: str
) -> None:
    api, seen = _api(lambda r: httpx.Response(200, json={}))
    result = await server.call_tool("read_file", arguments, api=api)
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]
    assert not seen
