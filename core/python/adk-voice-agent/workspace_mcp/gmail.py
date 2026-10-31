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

"""Gmail MCP tools (`python -m workspace_mcp.gmail`)."""

from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

from workspace_mcp._google import GoogleApi, html_to_text, truncate
from workspace_mcp._server import Server

server = Server("gmail")

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_RESULTS = 25
BODY_LIMIT = 4000
_CONCURRENCY = 5
_LIST_HEADERS = ("Subject", "From", "Date")
_REPLY_HEADERS = ("Message-ID", "References", "Subject")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name", "")).lower()
        found.setdefault(name, str(header.get("value", "")))
    return found


def _metadata_params(headers: tuple[str, ...]) -> list[tuple[str, str]]:
    return [("format", "metadata")] + [("metadataHeaders", h) for h in headers]


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _find_bodies(part: dict[str, Any], found: dict[str, str]) -> None:
    mime = str(part.get("mimeType", ""))
    data = (part.get("body") or {}).get("data")
    if (
        data
        and not part.get("filename")
        and mime in ("text/plain", "text/html")
    ):
        found.setdefault(mime, _decode(data))
    for child in part.get("parts") or []:
        _find_bodies(child, found)


def _body_text(payload: dict[str, Any]) -> str:
    found: dict[str, str] = {}
    _find_bodies(payload, found)
    if "text/plain" in found:
        return found["text/plain"].strip()
    return html_to_text(found.get("text/html", ""))


def _addresses(value: Any, field: str) -> str:
    if not value:
        return ""
    addresses = [a.strip() for a in str(value).split(",") if a.strip()]
    bad = [a for a in addresses if "@" not in a]
    if bad:
        raise ValueError(f"`{field}` has an invalid address: {bad[0]}")
    return ", ".join(addresses)


async def _build_raw(
    api: GoogleApi, args: dict[str, Any], *, bcc: bool
) -> dict[str, Any]:
    to = _addresses(args.get("to"), "to")
    if not to:
        raise ValueError("`to` needs at least one address")
    subject = str(args.get("subject") or "").strip()
    thread_id = str(args.get("thread_id") or "").strip()
    message = EmailMessage()
    if thread_id:
        thread = await api.get(
            f"{BASE}/threads/{quote(thread_id, safe='')}",
            params=_metadata_params(_REPLY_HEADERS),
        )
        messages = thread.get("messages") or []
        last = _headers(messages[-1].get("payload") or {}) if messages else {}
        if msg_id := last.get("message-id"):
            message["In-Reply-To"] = msg_id
            refs = last.get("references", "")
            message["References"] = f"{refs} {msg_id}".strip()
        if not subject:
            original = last.get("subject", "")
            has_re = original.lower().startswith("re:")
            subject = original if has_re else f"Re: {original}".strip()
    elif not subject:
        raise ValueError("`subject` is required unless replying (thread_id)")
    message["To"] = to
    if cc := _addresses(args.get("cc"), "cc"):
        message["Cc"] = cc
    if bcc and (bcc_list := _addresses(args.get("bcc"), "bcc")):
        message["Bcc"] = bcc_list
    message["Subject"] = subject
    message.set_content(str(args.get("body") or ""))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    resource: dict[str, Any] = {"raw": raw}
    if thread_id:
        resource["threadId"] = thread_id
    return resource


@server.tool(
    "search_threads",
    "Search the user's mailbox with Gmail search syntax (e.g. `from:ann "
    "is:unread`); an empty query lists the most recent threads.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query; omit for recent threads.",
            },
            "max_results": {
                "type": "integer",
                "description": f"Threads to return (default 10, max {MAX_RESULTS}).",
            },
            "page_token": {
                "type": "string",
                "description": "next_page_token from a previous search.",
            },
        },
        "required": [],
    },
    read_only=True,
)
async def search_threads(
    api: GoogleApi, args: dict[str, Any]
) -> dict[str, Any]:
    max_results = args.get("max_results")
    max_results = 10 if max_results is None else max_results
    if max_results < 1:
        raise ValueError("`max_results` must be at least 1")
    params: dict[str, Any] = {"maxResults": min(max_results, MAX_RESULTS)}
    if query := str(args.get("query") or "").strip():
        params["q"] = query
    if page_token := args.get("page_token"):
        params["pageToken"] = page_token
    listing = await api.get(f"{BASE}/threads", params=params)
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def summary(thread_id: str) -> dict[str, Any]:
        async with gate:
            thread = await api.get(
                f"{BASE}/threads/{quote(thread_id, safe='')}",
                params=_metadata_params(_LIST_HEADERS),
            )
        messages = thread.get("messages") or []
        first = _headers(messages[0].get("payload") or {}) if messages else {}
        return {
            "id": thread_id,
            "subject": first.get("subject", ""),
            "from": first.get("from", ""),
            "date": first.get("date", ""),
            "snippet": thread.get("snippet")
            or (messages[-1].get("snippet", "") if messages else ""),
            "unread": any(
                "UNREAD" in (m.get("labelIds") or []) for m in messages
            ),
            "message_count": len(messages),
        }

    ids = [t["id"] for t in listing.get("threads") or [] if t.get("id")]
    result: dict[str, Any] = {
        "threads": list(await asyncio.gather(*(summary(i) for i in ids)))
    }
    if next_token := listing.get("nextPageToken"):
        result["next_page_token"] = next_token
    return result


@server.tool(
    "get_thread",
    "Read one thread in full: each message's sender, recipients, date and "
    f"text body (cut at {BODY_LIMIT} characters).",
    {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Thread ID from search_threads.",
            }
        },
        "required": ["thread_id"],
    },
    read_only=True,
)
async def get_thread(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(args["thread_id"]).strip()
    thread = await api.get(
        f"{BASE}/threads/{quote(thread_id, safe='')}", params={"format": "full"}
    )
    messages = []
    for item in thread.get("messages") or []:
        payload = item.get("payload") or {}
        headers = _headers(payload)
        body, cut = truncate(_body_text(payload), BODY_LIMIT)
        message: dict[str, Any] = {
            "id": item.get("id", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "subject": headers.get("subject", ""),
            "body": body,
            "labels": item.get("labelIds") or [],
        }
        if cc := headers.get("cc"):
            message["cc"] = cc
        if cut:
            message["truncated"] = True
        messages.append(message)
    return {"thread_id": thread.get("id", thread_id), "messages": messages}


@server.tool(
    "list_labels",
    "List the mailbox's system and user labels; search with `label:<name>`.",
    {"type": "object", "properties": {}, "required": []},
    read_only=True,
)
async def list_labels(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    reply = await api.get(f"{BASE}/labels")
    return {
        "labels": [
            {"id": x.get("id"), "name": x.get("name"), "type": x.get("type")}
            for x in reply.get("labels") or []
        ]
    }


_COMPOSE_PROPERTIES: dict[str, Any] = {
    "to": {
        "type": "string",
        "description": "Recipients, comma-separated addresses.",
    },
    "subject": {
        "type": "string",
        "description": "Subject; optional when replying (defaults to Re:).",
    },
    "body": {"type": "string", "description": "Plain-text message body."},
    "cc": {"type": "string", "description": "Cc, comma-separated addresses."},
    "thread_id": {
        "type": "string",
        "description": "Thread ID to reply in (from search_threads).",
    },
}


@server.tool(
    "create_draft",
    "Save a draft email, optionally as a reply in a thread (nothing is sent).",
    {
        "type": "object",
        "properties": _COMPOSE_PROPERTIES,
        "required": ["to", "body"],
    },
)
async def create_draft(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    resource = await _build_raw(api, args, bcc=False)
    draft = await api.post(f"{BASE}/drafts", json={"message": resource})
    message = draft.get("message") or {}
    return {
        "draft_id": draft.get("id", ""),
        "message_id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
    }


@server.tool(
    "send_message",
    "Send an email now, optionally as a reply in a thread.",
    {
        "type": "object",
        "properties": {
            **_COMPOSE_PROPERTIES,
            "bcc": {
                "type": "string",
                "description": "Bcc, comma-separated addresses.",
            },
        },
        "required": ["to", "body"],
    },
    open_world=True,
)
async def send_message(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    resource = await _build_raw(api, args, bcc=True)
    sent = await api.post(f"{BASE}/messages/send", json=resource)
    return {
        "message_id": sent.get("id", ""),
        "thread_id": sent.get("threadId", ""),
    }


if __name__ == "__main__":
    server.run()
