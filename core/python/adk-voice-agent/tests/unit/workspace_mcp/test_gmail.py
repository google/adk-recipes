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

"""The `gmail` server's tools against a fake Gmail REST API."""

from __future__ import annotations

import base64
import email
import json
from email import policy
from typing import Any

import httpx
import pytest

from workspace_mcp._google import GoogleApi
from workspace_mcp.gmail import BODY_LIMIT, server

_PREFIX = "/gmail/v1/users/me"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _headers(**values: str) -> list[dict[str, str]]:
    return [
        {"name": k.rstrip("_").title(), "value": v} for k, v in values.items()
    ]


def _fake(
    routes: dict[tuple[str, str], Any],
) -> tuple[GoogleApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix(_PREFIX)
        route = routes.get((request.method, path))
        if route is None:
            return httpx.Response(404, json={"error": {"message": "nope"}})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    return GoogleApi("fake-token", transport=httpx.MockTransport(reply)), seen


def _sent_message(request: httpx.Request) -> email.message.EmailMessage:
    body = json.loads(request.content)
    raw = (body.get("message") or body)["raw"]
    return email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
        policy=policy.default,
    )


def _meta_thread(thread_id: str, subject: str, unread: bool) -> dict[str, Any]:
    labels = ["INBOX", "UNREAD"] if unread else ["INBOX"]
    return {
        "id": thread_id,
        "snippet": f"snippet {thread_id}",
        "messages": [
            {
                "id": f"{thread_id}-1",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": _headers(
                        subject=subject, from_="ann@example.com", date="Mon"
                    )
                },
            },
            {"id": f"{thread_id}-2", "labelIds": labels, "payload": {}},
        ],
    }


@pytest.mark.asyncio
async def test_search_threads_lists_then_fetches_metadata() -> None:
    api, seen = _fake(
        {
            ("GET", "/threads"): {
                "threads": [{"id": "t1"}, {"id": "t2"}],
                "nextPageToken": "next",
            },
            ("GET", "/threads/t1"): _meta_thread("t1", "Hello", True),
            ("GET", "/threads/t2"): _meta_thread("t2", "Lunch", False),
        }
    )
    result = await server.call_tool(
        "search_threads",
        {"query": "from:ann", "max_results": 99, "page_token": "p"},
        api=api,
    )
    assert result["isError"] is False
    data = result["structuredContent"]
    assert data["next_page_token"] == "next"
    assert data["threads"][0] == {
        "id": "t1",
        "subject": "Hello",
        "from": "ann@example.com",
        "date": "Mon",
        "snippet": "snippet t1",
        "unread": True,
        "message_count": 2,
    }
    assert data["threads"][1]["unread"] is False
    listing = seen[0]
    assert listing.url.params["q"] == "from:ann"
    assert listing.url.params["maxResults"] == "25"
    assert listing.url.params["pageToken"] == "p"
    meta = seen[1]
    assert meta.url.params["format"] == "metadata"
    assert meta.url.params.get_list("metadataHeaders") == [
        "Subject",
        "From",
        "Date",
    ]


@pytest.mark.asyncio
async def test_search_threads_with_no_query_lists_recent() -> None:
    api, seen = _fake({("GET", "/threads"): {}})
    result = await server.call_tool("search_threads", {}, api=api)
    assert result["structuredContent"] == {"threads": []}
    assert "q" not in seen[0].url.params
    assert seen[0].url.params["maxResults"] == "10"


@pytest.mark.asyncio
async def test_get_thread_decodes_bodies_and_truncates() -> None:
    long_text = "x" * (BODY_LIMIT + 50)
    thread = {
        "id": "t1",
        "messages": [
            {
                "id": "m1",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": _headers(
                        from_="ann@example.com",
                        to="me@example.com",
                        cc="bob@example.com",
                        date="Mon",
                        subject="Hi",
                    ),
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64("<p>html</p>")},
                        },
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64("plain ✓\n")},
                        },
                    ],
                },
            },
            {
                "id": "m2",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64("<div>Hello<br>there</div>")},
                        },
                        {
                            "mimeType": "text/plain",
                            "filename": "notes.txt",
                            "body": {"data": _b64("attachment")},
                        },
                    ],
                },
            },
            {
                "id": "m3",
                "payload": {
                    "mimeType": "text/plain",
                    "body": {"data": _b64(long_text)},
                },
            },
        ],
    }
    api, seen = _fake({("GET", "/threads/t1"): thread})
    result = await server.call_tool("get_thread", {"thread_id": "t1"}, api=api)
    data = result["structuredContent"]
    assert data["thread_id"] == "t1"
    first, second, third = data["messages"]
    assert first == {
        "id": "m1",
        "from": "ann@example.com",
        "to": "me@example.com",
        "cc": "bob@example.com",
        "date": "Mon",
        "subject": "Hi",
        "body": "plain ✓",
        "labels": ["INBOX"],
    }
    assert second["body"] == "Hello\nthere"
    assert "truncated" not in second
    assert len(third["body"]) == BODY_LIMIT
    assert third["truncated"] is True
    assert seen[0].url.params["format"] == "full"


@pytest.mark.asyncio
async def test_get_thread_not_found_is_an_error_hint() -> None:
    api, _ = _fake({})
    result = await server.call_tool(
        "get_thread", {"thread_id": "gone"}, api=api
    )
    assert result["isError"] is True
    assert "not found: check the ID" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_list_labels() -> None:
    api, _ = _fake(
        {
            ("GET", "/labels"): {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "system", "x": 1},
                    {"id": "Label_1", "name": "Work", "type": "user"},
                ]
            }
        }
    )
    result = await server.call_tool("list_labels", {}, api=api)
    assert result["structuredContent"] == {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_1", "name": "Work", "type": "user"},
        ]
    }


@pytest.mark.asyncio
async def test_create_draft_builds_an_rfc822_message() -> None:
    api, seen = _fake(
        {
            ("POST", "/drafts"): {
                "id": "d1",
                "message": {"id": "m9", "threadId": "t9"},
            }
        }
    )
    result = await server.call_tool(
        "create_draft",
        {
            "to": "ann@example.com, bob@example.com",
            "cc": "cat@example.com",
            "subject": "Plan",
            "body": "See you at 3.",
        },
        api=api,
    )
    assert result["structuredContent"] == {
        "draft_id": "d1",
        "message_id": "m9",
        "thread_id": "t9",
    }
    (post,) = seen
    assert "threadId" not in json.loads(post.content)["message"]
    message = _sent_message(post)
    assert message["To"] == "ann@example.com, bob@example.com"
    assert message["Cc"] == "cat@example.com"
    assert message["Subject"] == "Plan"
    assert message.get_content().strip() == "See you at 3."


@pytest.mark.asyncio
async def test_create_draft_reply_threads_and_defaults_subject() -> None:
    thread = {
        "id": "t1",
        "messages": [
            {"payload": {"headers": _headers(subject="Old")}},
            {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Budget"},
                        {"name": "Message-ID", "value": "<b@mail>"},
                        {"name": "References", "value": "<a@mail>"},
                    ]
                }
            },
        ],
    }
    api, seen = _fake(
        {
            ("GET", "/threads/t1"): thread,
            ("POST", "/drafts"): {
                "id": "d1",
                "message": {"id": "m2", "threadId": "t1"},
            },
        }
    )
    result = await server.call_tool(
        "create_draft",
        {"to": "ann@example.com", "body": "Agreed.", "thread_id": "t1"},
        api=api,
    )
    assert result["structuredContent"]["thread_id"] == "t1"
    fetch, post = seen
    assert fetch.url.params["format"] == "metadata"
    assert fetch.url.params.get_list("metadataHeaders") == [
        "Message-ID",
        "References",
        "Subject",
    ]
    assert json.loads(post.content)["message"]["threadId"] == "t1"
    message = _sent_message(post)
    assert message["Subject"] == "Re: Budget"
    assert message["In-Reply-To"] == "<b@mail>"
    assert message["References"] == "<a@mail> <b@mail>"


@pytest.mark.asyncio
async def test_send_message_posts_with_bcc() -> None:
    api, seen = _fake(
        {("POST", "/messages/send"): {"id": "m1", "threadId": "t1"}}
    )
    result = await server.call_tool(
        "send_message",
        {
            "to": "ann@example.com",
            "bcc": "boss@example.com",
            "subject": "Done",
            "body": "Shipped.",
        },
        api=api,
    )
    assert result["structuredContent"] == {
        "message_id": "m1",
        "thread_id": "t1",
    }
    (post,) = seen
    assert post.method == "POST"
    assert "raw" in json.loads(post.content)
    assert _sent_message(post)["Bcc"] == "boss@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("create_draft", {"to": "ann@example.com"}, "missing required"),
        (
            "create_draft",
            {"to": "ann@example.com", "body": "hi"},
            "`subject` is required unless replying",
        ),
        (
            "send_message",
            {"to": "ann, bob@example.com", "subject": "s", "body": "b"},
            "`to` has an invalid address: ann",
        ),
        (
            "send_message",
            {"to": " , ", "subject": "s", "body": "b"},
            "`to` needs at least one address",
        ),
        (
            "send_message",
            {"to": "a@example.com", "subject": "s\nBcc: x@y", "body": "b"},
            "linefeed",
        ),
        ("search_threads", {"max_results": 0}, "at least 1"),
        ("get_thread", {}, "missing required argument(s): thread_id"),
    ],
)
async def test_bad_arguments_are_rejected_before_any_send(
    tool: str, arguments: dict[str, Any], expected: str
) -> None:
    api, seen = _fake({})
    result = await server.call_tool(tool, arguments, api=api)
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]
    assert not [r for r in seen if r.method == "POST"]
