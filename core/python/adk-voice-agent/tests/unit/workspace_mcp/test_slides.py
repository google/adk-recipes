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

"""The `google_slides` server's tools against a faked Slides REST API."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from workspace_mcp._google import GoogleApi
from workspace_mcp.slides import TEXT_LIMIT, server


def _text(*runs: str) -> dict[str, Any]:
    return {"textElements": [{"textRun": {"content": r}} for r in runs]}


def _shape(*runs: str) -> dict[str, Any]:
    return {"objectId": "s", "shape": {"text": _text(*runs)}}


PRESENTATION = {
    "presentationId": "p1",
    "title": "Q3 review",
    "slides": [
        {
            "objectId": "slide_a",
            "pageElements": [
                _shape("Q3 ", "results\n"),
                {"objectId": "img", "image": {}},
                {
                    "objectId": "t",
                    "table": {
                        "tableRows": [
                            {
                                "tableCells": [
                                    {"text": _text("Revenue\n")},
                                    {"text": _text("$10M\n")},
                                ]
                            }
                        ]
                    },
                },
            ],
        },
        {
            "objectId": "slide_b",
            "pageElements": [
                {
                    "objectId": "g",
                    "elementGroup": {
                        "children": [
                            _shape("Outer\n"),
                            {
                                "objectId": "g2",
                                "elementGroup": {
                                    "children": [_shape("Inner\n")]
                                },
                            },
                        ]
                    },
                }
            ],
        },
        {"objectId": "slide_c"},
    ],
}


class _Fake:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = PRESENTATION if body is None else body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    def api(self) -> GoogleApi:
        return GoogleApi("fake-token", transport=httpx.MockTransport(self))


@pytest.mark.asyncio
async def test_get_presentation_reads_shapes_tables_and_groups() -> None:
    fake = _Fake()
    async with fake.api() as api:
        result = await server.call_tool(
            "get_presentation", {"presentation_id": "p1"}, api=api
        )
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "presentation_id": "p1",
        "title": "Q3 review",
        "slides": [
            {
                "slide_id": "slide_a",
                "number": 1,
                "text": "Q3 results\nRevenue\n$10M",
            },
            {"slide_id": "slide_b", "number": 2, "text": "Outer\nInner"},
            {"slide_id": "slide_c", "number": 3, "text": ""},
        ],
    }
    [request] = fake.requests
    assert request.method == "GET"
    assert request.url.host == "slides.googleapis.com"
    assert request.url.path == "/v1/presentations/p1"
    assert request.headers["Authorization"] == "Bearer fake-token"


@pytest.mark.asyncio
async def test_get_presentation_caps_the_total_text() -> None:
    long = "x" * (TEXT_LIMIT - 10)
    body = {
        "presentationId": "p2",
        "title": "Long",
        "slides": [
            {"objectId": "a", "pageElements": [_shape(long)]},
            {"objectId": "b", "pageElements": [_shape("y" * 50)]},
            {"objectId": "c", "pageElements": [_shape("z" * 50)]},
        ],
    }
    async with _Fake(body=body).api() as api:
        result = await server.call_tool(
            "get_presentation", {"presentation_id": "p2"}, api=api
        )
    data = result["structuredContent"]
    assert data["truncated"] is True
    assert [s["slide_id"] for s in data["slides"]] == ["a", "b", "c"]
    assert sum(len(s["text"]) for s in data["slides"]) == TEXT_LIMIT
    assert data["slides"][1]["text"] == "y" * 10
    assert data["slides"][2]["text"] == ""


@pytest.mark.asyncio
async def test_short_presentations_are_not_marked_truncated() -> None:
    async with _Fake().api() as api:
        result = await server.call_tool(
            "get_presentation", {"presentation_id": "p1"}, api=api
        )
    assert "truncated" not in result["structuredContent"]


@pytest.mark.asyncio
async def test_a_missing_presentation_is_a_not_found_hint() -> None:
    fake = _Fake(404, {"error": {"code": 404, "message": "Not found"}})
    async with fake.api() as api:
        result = await server.call_tool(
            "get_presentation", {"presentation_id": "nope"}, api=api
        )
    assert result["isError"] is True
    assert "not found: check the ID" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "missing required argument(s): presentation_id"),
        ({"presentation_id": 5}, "`presentation_id` should be a string"),
        (
            {"presentation_id": "https://docs.google.com/presentation/d/x"},
            "not a URL",
        ),
    ],
)
async def test_bad_arguments_never_call_google(
    args: dict[str, Any], message: str
) -> None:
    fake = _Fake()
    async with fake.api() as api:
        result = await server.call_tool("get_presentation", args, api=api)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert fake.requests == []


def test_get_presentation_is_annotated_read_only() -> None:
    tool = server.tools["get_presentation"]
    assert tool.annotations["readOnlyHint"] is True
    assert set(server.tools) == {"get_presentation"}
