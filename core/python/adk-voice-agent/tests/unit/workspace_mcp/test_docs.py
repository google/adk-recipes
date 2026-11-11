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

"""The `google_docs` server's tools against a fake Docs REST API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from workspace_mcp._google import GoogleApi
from workspace_mcp.docs import TEXT_LIMIT, server


def _para(text: str, style: str = "NORMAL_TEXT") -> dict[str, Any]:
    return {
        "paragraph": {
            "elements": [{"textRun": {"content": text}}],
            "paragraphStyle": {"namedStyleType": style},
        }
    }


def _cell(*texts: str) -> dict[str, Any]:
    return {"content": [_para(t) for t in texts]}


_DOC = {
    "documentId": "doc1",
    "title": "Plan",
    "body": {
        "content": [
            {"sectionBreak": {}},
            _para("Plan\n", "TITLE"),
            _para("Goals\n", "HEADING_2"),
            {
                "paragraph": {
                    "elements": [
                        {"textRun": {"content": "Ship "}},
                        {"textRun": {"content": "it.\n"}},
                    ]
                }
            },
            {
                "table": {
                    "tableRows": [
                        {"tableCells": [_cell("Owner\n"), _cell("Due\n")]},
                        {
                            "tableCells": [
                                _cell("Ana\n"),
                                {
                                    "content": [
                                        {
                                            "table": {
                                                "tableRows": [
                                                    {
                                                        "tableCells": [
                                                            _cell("Q3\n"),
                                                            _cell("Q4\n"),
                                                        ]
                                                    }
                                                ]
                                            }
                                        }
                                    ]
                                },
                            ]
                        },
                    ]
                }
            },
            _para("Deep\n", "HEADING_6"),
        ]
    },
}


def _fake(
    replies: dict[tuple[str, str], httpx.Response],
) -> tuple[GoogleApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.get((request.method, request.url.path))
        return reply or httpx.Response(404, json={"error": {"message": "x"}})

    api = GoogleApi("fake-token", transport=httpx.MockTransport(handle))
    return api, seen


@pytest.mark.asyncio
async def test_get_document_renders_headings_and_tables() -> None:
    api, seen = _fake(
        {("GET", "/v1/documents/doc1"): httpx.Response(200, json=_DOC)}
    )
    result = await server.call_tool(
        "get_document", {"document_id": "doc1"}, api=api
    )
    assert result["isError"] is False
    data = result["structuredContent"]
    assert data == {
        "document_id": "doc1",
        "title": "Plan",
        "text": (
            "# Plan\n## Goals\nShip it.\nOwner | Due\nAna | Q3 | Q4\n###### Deep"
        ),
    }
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/documents/doc1")
    ]
    assert seen[0].headers["Authorization"] == "Bearer fake-token"


@pytest.mark.asyncio
async def test_get_document_accepts_a_url_and_truncates() -> None:
    long_doc = {
        "documentId": "doc1",
        "title": "Long",
        "body": {"content": [_para("x" * (TEXT_LIMIT + 50) + "\n")]},
    }
    api, seen = _fake(
        {("GET", "/v1/documents/doc1"): httpx.Response(200, json=long_doc)}
    )
    url = "https://docs.google.com/document/d/doc1/edit?tab=t.0"
    result = await server.call_tool(
        "get_document", {"document_id": url}, api=api
    )
    data = result["structuredContent"]
    assert data["truncated"] is True
    assert len(data["text"]) == TEXT_LIMIT
    assert data["document_id"] == "doc1"
    assert seen[0].url.path == "/v1/documents/doc1"


@pytest.mark.asyncio
async def test_get_document_not_found_is_a_hint() -> None:
    api, _ = _fake({})
    result = await server.call_tool(
        "get_document", {"document_id": "missing"}, api=api
    )
    assert result["isError"] is True
    assert "not found: check the ID" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_append_text_inserts_at_the_end() -> None:
    api, seen = _fake(
        {
            ("POST", "/v1/documents/doc1:batchUpdate"): httpx.Response(
                200, json={"documentId": "doc1", "replies": [{}]}
            )
        }
    )
    result = await server.call_tool(
        "append_text", {"document_id": "doc1", "text": "\nDone."}, api=api
    )
    assert result["structuredContent"] == {
        "document_id": "doc1",
        "appended_chars": 6,
    }
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/documents/doc1:batchUpdate")
    ]
    assert json.loads(seen[0].content) == {
        "requests": [
            {"insertText": {"text": "\nDone.", "endOfSegmentLocation": {}}}
        ]
    }


@pytest.mark.asyncio
async def test_append_text_permission_error_is_a_hint() -> None:
    body = {
        "error": {
            "message": "denied",
            "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
        }
    }
    api, _ = _fake(
        {
            ("POST", "/v1/documents/doc1:batchUpdate"): httpx.Response(
                403, json=body
            )
        }
    )
    result = await server.call_tool(
        "append_text", {"document_id": "doc1", "text": "hi"}, api=api
    )
    assert result["isError"] is True
    assert "Grant access" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("get_document", {}, "missing required argument(s): document_id"),
        ("get_document", {"document_id": "a/b c"}, "not a Docs ID"),
        ("get_document", {"document_id": 5}, "should be a string"),
        ("append_text", {"document_id": "doc1"}, "text"),
        ("append_text", {"document_id": "doc1", "text": ""}, "text"),
    ],
)
async def test_bad_arguments_are_rejected_before_calling_google(
    tool: str, args: dict[str, Any], message: str
) -> None:
    api, seen = _fake({})
    result = await server.call_tool(tool, args, api=api)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert seen == []


def test_annotations() -> None:
    get_hints = server.tools["get_document"].annotations
    append_hints = server.tools["append_text"].annotations
    assert get_hints["readOnlyHint"] is True
    assert not append_hints["readOnlyHint"]
    assert not append_hints["openWorldHint"]
    assert not append_hints["destructiveHint"]
