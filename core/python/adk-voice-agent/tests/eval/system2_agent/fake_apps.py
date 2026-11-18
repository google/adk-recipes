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

"""Fake connected-app MCP upstreams (Gmail, Calendar, Drive, GitHub) behind the real gateway."""

from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock
from typing import Any

import httpx

from tests.fake_integrations import FakeDrive

_TOOLS: dict[str, dict[str, tuple[str, dict[str, str], list[str]]]] = {
    "gmail": {
        "search_threads": (
            "Search the user's mailbox. Words match sender, subject, and body; "
            "an empty query lists the most recent threads.",
            {"query": "string", "max_results": "integer"},
            [],
        ),
        "get_thread": (
            "Read one thread in full.",
            {"thread_id": "string"},
            ["thread_id"],
        ),
        "create_draft": (
            "Save a draft email (nothing is sent).",
            {
                "to": "string",
                "subject": "string",
                "body": "string",
                "thread_id": "string",
            },
            ["to", "body"],
        ),
        "send_message": (
            "Send an email now.",
            {
                "to": "string",
                "subject": "string",
                "body": "string",
                "thread_id": "string",
            },
            ["to", "body"],
        ),
    },
    "google_calendar": {
        "list_events": (
            "List calendar events between two ISO 8601 times (default: today and tomorrow).",
            {"time_min": "string", "time_max": "string", "query": "string"},
            [],
        ),
        "get_event": ("Read one event.", {"event_id": "string"}, ["event_id"]),
        "create_event": (
            "Create an event and invite its attendees.",
            {
                "summary": "string",
                "start": "string",
                "end": "string",
                "attendees": "array",
            },
            ["summary", "start"],
        ),
    },
    "google_drive": {
        "search_files": (
            "Search Drive by title and content.",
            {"query": "string"},
            [],
        ),
        "read_file": (
            "Read a document's text.",
            {"file_id": "string"},
            ["file_id"],
        ),
        "share_file": (
            "Share a file with someone.",
            {"file_id": "string", "email": "string", "role": "string"},
            ["file_id", "email"],
        ),
    },
    "github": {
        "list_pull_requests": (
            "List a repository's pull requests.",
            {"repo": "string", "state": "string"},
            [],
        ),
        "get_pull_request": (
            "Read one pull request.",
            {"repo": "string", "number": "integer"},
            ["number"],
        ),
        "list_workflow_runs": (
            "List recent CI runs for a repository.",
            {"repo": "string", "branch": "string"},
            [],
        ),
        "create_pull_request": (
            "Open a pull request.",
            {
                "repo": "string",
                "title": "string",
                "head": "string",
                "base": "string",
                "body": "string",
            },
            ["title", "head"],
        ),
        "merge_pull_request": (
            "Merge a pull request.",
            {"repo": "string", "number": "integer"},
            ["number"],
        ),
    },
}

HOSTS = {
    "gmailmcp.googleapis.com": "gmail",
    "calendarmcp.googleapis.com": "google_calendar",
    "drivemcp.googleapis.com": "google_drive",
    "docsmcp.googleapis.com": "google_docs",
    "sheetsmcp.googleapis.com": "google_sheets",
    "slidesmcp.googleapis.com": "google_slides",
    "api.githubcopilot.com": "github",
}

DRIVE_REST_HOST = "www.googleapis.com"

WRITE_TOOLS = (
    "send_*",
    "create_*",
    "update_*",
    "delete_*",
    "share_*",
    "merge_*",
    "forward_*",
)


def is_write(tool: str) -> bool:
    return any(fnmatch.fnmatch(tool, pattern) for pattern in WRITE_TOOLS)


@dataclass
class Mailbox:
    apps: dict[str, Any]
    today: date
    calls: list[dict[str, Any]] = field(default_factory=list)
    drive: FakeDrive = field(default_factory=FakeDrive)

    def drive_files(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f["name"],
                "mimeType": f.get("mimeType", ""),
                "bytes": len(f["content"]),
            }
            for f in self.drive.files.values()
            if f.get("mimeType") != "application/vnd.google-apps.folder"
        ]

    def at(self, day: int, hhmm: str) -> datetime:
        hour, minute = (int(x) for x in hhmm.split(":"))
        return datetime.combine(
            self.today + timedelta(days=day), clock(hour, minute)
        ).astimezone()

    def _thread(self, t: dict[str, Any], *, full: bool) -> dict[str, Any]:
        sent = datetime.now().astimezone() - timedelta(
            hours=float(t.get("hours_ago", 0))
        )
        out = {
            "thread_id": t["id"],
            "from": t["from"],
            "to": t.get("to", ""),
            "subject": t["subject"],
            "date": sent.isoformat(timespec="minutes"),
            "labels": t.get("labels", []),
        }
        out["body" if full else "snippet"] = (
            t["body"] if full else t["body"][:160]
        )
        return out

    def gmail(self, tool: str, args: dict[str, Any]) -> Any:
        threads = self.apps.get("gmail") or []
        if tool == "search_threads":
            words = [
                w.lower()
                for w in str(args.get("query") or "").split()
                if ":" not in w and w.lower() not in {"and", "or", "the"}
            ]
            hits = [
                t
                for t in threads
                if not words
                or any(
                    w in f"{t['from']} {t['subject']} {t['body']}".lower()
                    for w in words
                )
            ]
            hits.sort(key=lambda t: float(t.get("hours_ago", 0)))
            limit = int(args.get("max_results") or 20)
            return {
                "threads": [self._thread(t, full=False) for t in hits[:limit]]
            }
        if tool == "get_thread":
            for t in threads:
                if t["id"] == args.get("thread_id"):
                    return self._thread(t, full=True)
            raise LookupError(f"No thread {args.get('thread_id')!r}")
        if tool == "create_draft":
            return {
                "draft_id": f"draft-{len(self.calls)}",
                "status": "saved as a draft",
            }
        if tool == "send_message":
            return {"message_id": f"msg-{len(self.calls)}", "status": "sent"}
        raise LookupError(tool)

    def _event(self, e: dict[str, Any]) -> dict[str, Any]:
        day = int(e.get("day", 0))
        attendees = [{"email": self.apps.get("me", ""), "self": True}] + [
            {"email": a} for a in e.get("attendees") or []
        ]
        return {
            "event_id": e["id"],
            "summary": e["summary"],
            "start": self.at(day, e["start"]).isoformat(),
            "end": self.at(day, e["end"]).isoformat(),
            "location": e.get("location", ""),
            "description": e.get("description", ""),
            "attendees": attendees,
        }

    def google_calendar(self, tool: str, args: dict[str, Any]) -> Any:
        events = [self._event(e) for e in self.apps.get("calendar") or []]
        if tool == "list_events":
            lo, hi = (
                _parse_time(args.get("time_min")),
                _parse_time(args.get("time_max")),
            )
            query = str(args.get("query") or "").lower()
            picked = [
                e
                for e in events
                if (lo is None or datetime.fromisoformat(e["end"]) > lo)
                and (hi is None or datetime.fromisoformat(e["start"]) < hi)
                and (not query or query in e["summary"].lower())
            ]
            return {
                "timezone": str(datetime.now().astimezone().tzinfo),
                "events": picked,
            }
        if tool == "get_event":
            for e in events:
                if e["event_id"] == args.get("event_id"):
                    return e
            raise LookupError(f"No event {args.get('event_id')!r}")
        if tool == "create_event":
            return {
                "event_id": f"evt-new-{len(self.calls)}",
                "status": "created",
            }
        raise LookupError(tool)

    def google_drive(self, tool: str, args: dict[str, Any]) -> Any:
        files = self.apps.get("drive") or []
        if tool == "search_files":
            words = [
                w.lower()
                for w in str(args.get("query") or "").split()
                if ":" not in w
            ]
            hits = [
                f
                for f in files
                if not words
                or any(
                    w in f"{f['title']} {f['content']}".lower() for w in words
                )
            ]
            return {
                "files": [
                    {
                        "file_id": f["id"],
                        "title": f["title"],
                        "owner": f.get("owner", ""),
                        "modified": (
                            self.today
                            - timedelta(days=int(f.get("modified_days_ago", 0)))
                        ).isoformat(),
                    }
                    for f in hits
                ]
            }
        if tool == "read_file":
            for f in files:
                if f["id"] == args.get("file_id"):
                    return {
                        "file_id": f["id"],
                        "title": f["title"],
                        "text": f["content"],
                    }
            raise LookupError(f"No file {args.get('file_id')!r}")
        if tool == "share_file":
            return {"status": "shared"}
        raise LookupError(tool)

    def github(self, tool: str, args: dict[str, Any]) -> Any:
        gh = self.apps.get("github") or {}
        prs = gh.get("pull_requests") or []
        if tool == "list_pull_requests":
            state = str(args.get("state") or "all").lower()
            return {
                "repo": gh.get("repo"),
                "pull_requests": [
                    p
                    for p in prs
                    if state in ("all", p["state"])
                    or (state == "closed" and p["state"] == "merged")
                ],
            }
        if tool == "get_pull_request":
            for p in prs:
                if int(p["number"]) == int(args.get("number") or 0):
                    return {"repo": gh.get("repo"), **p}
            raise LookupError(f"No pull request #{args.get('number')}")
        if tool == "list_workflow_runs":
            return {
                "repo": gh.get("repo"),
                "runs": gh.get("workflow_runs") or [],
            }
        if tool == "create_pull_request":
            return {"number": 999, "status": "opened"}
        if tool == "merge_pull_request":
            return {"status": "merged"}
        raise LookupError(tool)

    def call(
        self, server: str, tool: str, args: dict[str, Any]
    ) -> tuple[Any, bool]:
        self.calls.append(
            {
                "server": server,
                "tool": tool,
                "arguments": args,
                "ts": time.time(),
                "write": is_write(tool),
            }
        )
        try:
            return getattr(self, server)(tool, args), False
        except (LookupError, ValueError, TypeError) as exc:
            return {"error": str(exc)}, True


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def tool_list(server: str) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {k: {"type": t} for k, t in props.items()},
                "required": required,
            },
        }
        for name, (description, props, required) in _TOOLS.get(
            server, {}
        ).items()
    ]


class FakeApps:
    def __init__(self) -> None:
        self._by_token: dict[str, Mailbox] = {}

    def register(self, tokens: list[str], mailbox: Mailbox) -> None:
        for token in tokens:
            self._by_token[token] = mailbox

    def forget(self, tokens: list[str]) -> None:
        for token in tokens:
            self._by_token.pop(token, None)

    def _mailbox(self, request: httpx.Request) -> Mailbox | None:
        token = (
            request.headers.get("authorization", "")
            .removeprefix("Bearer ")
            .strip()
        )
        return self._by_token.get(token)

    async def drive(self, request: httpx.Request) -> httpx.Response:
        mailbox = self._mailbox(request)
        if mailbox is None:
            return httpx.Response(401, json={"error": "unknown token"})
        return await mailbox.drive.handle(request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        server = HOSTS[request.url.host]
        mailbox = self._mailbox(request)
        if mailbox is None:
            return httpx.Response(401, json={"error": "unknown token"})
        body = json.loads(request.content or b"{}")
        method, req_id = body.get("method"), body.get("id")
        if req_id is None:
            return httpx.Response(202)
        if method == "initialize":
            result: Any = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server},
            }
        elif method == "tools/list":
            result = {"tools": tool_list(server)}
        elif method == "tools/call":
            params = body.get("params") or {}
            data, is_error = mailbox.call(
                server, str(params.get("name")), params.get("arguments") or {}
            )
            result = {
                "content": [{"type": "text", "text": json.dumps(data)}],
                "isError": is_error,
            }
        else:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": method},
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": req_id, "result": result},
            headers={"Mcp-Session-Id": f"{server}-session"},
        )


class RoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self, apps: FakeApps) -> None:
        self._apps = apps
        self._network = httpx.AsyncHTTPTransport()

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        if request.url.host in HOSTS:
            await request.aread()
            return self._apps.handle(request)
        if (
            request.url.host == DRIVE_REST_HOST
            and "/drive/v3/files" in request.url.path
        ):
            await request.aread()
            return await self._apps.drive(request)
        return await self._network.handle_async_request(request)

    async def aclose(self) -> None:
        # Shared across every httpx.AsyncClient for the life of the process.
        return None
