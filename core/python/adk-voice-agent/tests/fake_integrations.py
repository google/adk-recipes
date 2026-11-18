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

"""Canned REST responses for downstream integrations, injected at the HTTP transport seam."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.auth import jwt as google_jwt
from google.auth.crypt import es256


def _today_events(now: datetime) -> list[dict[str, Any]]:
    base = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)

    def event(
        summary: str, start_min: int, length_min: int, **extra: Any
    ) -> dict:
        start = base + timedelta(minutes=start_min)
        return {
            "id": f"evt-{start_min}",
            "summary": summary,
            "start": {"dateTime": start.isoformat()},
            "end": {
                "dateTime": (start + timedelta(minutes=length_min)).isoformat()
            },
            "htmlLink": f"https://calendar.google.com/event?eid={start_min}",
            **extra,
        }

    people = [{"email": f"p{i}@voice-agent.dev"} for i in range(5)]
    return [
        event(
            "Architecture Review",
            0,
            45,
            hangoutLink="https://meet.google.com/abc-defg-hij",
            attendees=[{"email": "dev@voice-agent.dev", "self": True}, *people],
        ),
        event(
            "Sprint Triage",
            60,
            30,
            hangoutLink="https://meet.google.com/xyz-uvwx-rst",
            attendees=people[:3],
        ),
        event("1:1 with Maya", 150, 30, location="Room 4B"),
    ]


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


_GMAIL_MESSAGES = {
    "m1": (
        "Alex Chen <alex@voice-agent.dev>",
        "Auth service rollout update",
        "Canary is at 25% with no errors so far. Planning to go to 50% after lunch.",
        12,
        ["INBOX", "UNREAD"],
    ),
    "m2": (
        '"Maya Patel" <maya@voice-agent.dev>',
        "Q3 latency benchmarks",
        "p95 dropped from 220ms to 140ms after the pool change. Full sheet attached.",
        95,
        ["INBOX", "UNREAD"],
    ),
    "m3": (
        "GitHub <notifications@github.com>",
        "[google/adk-python] Review requested on #18",
        "dpark requested your review on Add Redis JWT rotation with 15m grace window.",
        60 * 26,
        ["INBOX"],
    ),
}

_DRIVE_FILES = [
    (
        "application/vnd.google-apps.spreadsheet",
        "Q3 latency benchmarks",
        26,
        "Maya Patel",
    ),
    (
        "application/vnd.google-apps.presentation",
        "Auth rollout review",
        72,
        None,
    ),
    ("application/pdf", "OAuth 2.1 draft (IETF).pdf", 24 * 9, "Alex Chen"),
]

# (sender, subject, body, minutes ago, labels)
GmailMessage = tuple[str, str, str, float, list[str]]


@dataclass(frozen=True)
class Workspace:
    """What the fake Calendar, Gmail, and Drive hold (`None` files: the default Drive)."""

    events: Callable[[datetime], list[dict[str, Any]]] = _today_events
    messages: Mapping[str, GmailMessage] = field(
        default_factory=lambda: _GMAIL_MESSAGES
    )
    files: list[dict[str, Any]] | None = None


# number -> (title, author, additions, deletions, check conclusions, review states)
_PULLS: dict[int, tuple[str, str, int, int, list[str | None], list[str]]] = {
    18: (
        "Add Redis JWT rotation with 15m grace window",
        "voice-agent-dev",
        214,
        38,
        ["success", "success"],
        ["APPROVED"],
    ),
    21: (
        "Stream partial tool results in run_live",
        "kchen",
        96,
        12,
        [None],
        [],
    ),
    19: (
        "Fix session resumption after WebSocket reconnect",
        "dpark",
        41,
        57,
        ["failure", "success"],
        ["CHANGES_REQUESTED"],
    ),
    12: (
        "Retry Vertex sandbox exec on 502",
        "voice-agent-dev",
        33,
        4,
        ["success"],
        ["APPROVED"],
    ),
}


def _pull(number: int, slug: str = "google/adk-python") -> dict[str, Any]:
    title, author, additions, deletions, _, _ = _PULLS.get(number, _PULLS[18])
    return {
        "number": number,
        "title": title,
        "state": "open",
        "user": {"login": author},
        "html_url": f"https://github.com/{slug}/pull/{number}",
        "updated_at": _ago(hours=number % 7 + 1),
        "head": {"label": f"{author}:feat/pr-{number}", "sha": f"sha{number}"},
        "additions": additions,
        "deletions": deletions,
        "requested_reviewers": [],
    }


def integrations_transport(
    *,
    requests: list[httpx.Request] | None = None,
    workspace: Workspace | None = None,
) -> httpx.MockTransport:
    """`MockTransport` serving deterministic Google Workspace, GitHub, and IAP key responses."""
    ws = workspace or Workspace()

    async def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        handler = {
            "www.googleapis.com": lambda r: _google(r, ws),
            "gmail.googleapis.com": lambda r: _gmail(r, ws),
            "slides.googleapis.com": _slides,
            "api.github.com": _github,
            "www.gstatic.com": _iap_keys,
        }.get(request.url.host)
        response = handler(request) if handler else None
        return response or httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(_handler)


def _google(request: httpx.Request, ws: Workspace) -> httpx.Response | None:
    path = request.url.path
    if path == "/oauth2/v2/userinfo":
        return httpx.Response(
            200,
            json={
                "email": "dev@voice-agent.local",
                "name": "ADK Voice Agent Dev",
            },
        )
    if path.endswith("/events/quickAdd") and request.method == "POST":
        text = request.url.params.get("text", "Architecture Sync")
        start = datetime.now().astimezone().replace(
            hour=10, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        return httpx.Response(
            200,
            json={
                "summary": text,
                "start": {"dateTime": start.isoformat()},
                "end": {
                    "dateTime": (start + timedelta(minutes=30)).isoformat()
                },
                "hangoutLink": "https://meet.google.com/new-meet-ing",
            },
        )
    if path.endswith("/calendars/primary/events") and request.method == "POST":
        event = json.loads(request.content or b"{}")
        return httpx.Response(
            200,
            json={
                **event,
                "id": "evt-new",
                "htmlLink": "https://www.google.com/calendar/event?eid=evt-new",
            },
        )
    if "/calendars/primary/events/" in path and request.method == "PATCH":
        event_id = path.rsplit("/", 1)[-1]
        patch = json.loads(request.content or b"{}")
        existing = next(
            (
                e
                for e in ws.events(datetime.now().astimezone())
                if e["id"] == event_id
            ),
            {"id": event_id, "summary": "Updated Event"},
        )
        return httpx.Response(
            200,
            json={
                **existing,
                **patch,
                "id": event_id,
                "htmlLink": existing.get(
                    "htmlLink",
                    f"https://www.google.com/calendar/event?eid={event_id}",
                ),
            },
        )
    if "/calendars/primary/events/" in path and request.method == "DELETE":
        return httpx.Response(204)
    if path.startswith("/drive/v3/files/") and path.endswith("/permissions"):
        return httpx.Response(
            200, json={"id": "perm-1", "kind": "drive#permission"}
        )
    if path.endswith("/calendars/primary/events"):
        events = ws.events(datetime.now().astimezone())
        lo, hi = (request.url.params.get(k) for k in ("timeMin", "timeMax"))
        if lo and hi:
            lo_t, hi_t = datetime.fromisoformat(lo), datetime.fromisoformat(hi)
            events = [
                e
                for e in events
                if lo_t <= datetime.fromisoformat(e["start"]["dateTime"]) < hi_t
            ]
        return httpx.Response(200, json={"items": events})
    if path == "/drive/v3/files":
        q = request.url.params.get("q", "")
        if ws.files is not None:
            phrase = (
                q.split("contains '", 1)[1].split("'", 1)[0]
                if "contains '" in q
                else ""
            )
            files = [
                f
                for f in ws.files
                if all(w in f["name"].lower() for w in phrase.lower().split())
            ]
            return httpx.Response(200, json={"files": files})
        if "createdTime >" in q:
            # The thread-audit query: nothing new unless a test serves its own files.
            return httpx.Response(200, json={"files": []})
        name = "Token Rotation Architecture Spec"
        if "contains '" in q:
            name = q.split("contains '", 1)[1].split("'", 1)[0] or name
        spec = {
            "id": "doc-1",
            "name": name,
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": _ago(hours=3),
            "owners": [{"displayName": "ADK Voice Agent Dev", "me": True}],
            "lastModifyingUser": {"displayName": "Alex Chen"},
            "description": "Specifies Redis-backed JWT rotation with a 15-minute grace window and zero-downtime key rollover.",
        }
        others = [
            {
                "id": f"file-{i}",
                "name": file_name,
                "mimeType": mime,
                "modifiedTime": _ago(hours=hours),
                "owners": [
                    {
                        "displayName": owner or "ADK Voice Agent Dev",
                        "me": owner is None,
                    }
                ],
            }
            for i, (mime, file_name, hours, owner) in enumerate(_DRIVE_FILES)
        ]
        return httpx.Response(200, json={"files": [spec, *others]})
    if path == "/upload/drive/v3/files" and request.method == "POST":
        name = "ADK Voice Agent Artifact"
        body = request.content.decode(errors="replace")
        if '"name": "' in body:
            name = body.split('"name": "', 1)[1].split('"', 1)[0]
        return httpx.Response(
            200,
            json={
                "id": "doc-new",
                "name": name,
                "webViewLink": "https://docs.google.com/document/d/doc-new/edit",
            },
        )
    return None


def _slides(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path
    if path == "/v1/presentations" and request.method == "POST":
        title = json.loads(request.content or b"{}").get("title", "Deck")
        return httpx.Response(
            200,
            json={
                "presentationId": "deck-new",
                "title": title,
                "slides": [{"objectId": "p"}],
            },
        )
    if path.endswith(":batchUpdate"):
        return httpx.Response(
            200, json={"presentationId": "deck-new", "replies": []}
        )
    return None


def _gmail(request: httpx.Request, ws: Workspace) -> httpx.Response | None:
    path = request.url.path
    messages = ws.messages
    if path.endswith("/messages/send") and request.method == "POST":
        return httpx.Response(
            200, json={"id": "sent-1", "threadId": "t-sent-1"}
        )
    if path.endswith("/drafts") and request.method == "POST":
        return httpx.Response(
            200, json={"id": "draft-1", "message": {"id": "msg-d1"}}
        )
    if path.endswith("/users/me/messages"):
        return httpx.Response(
            200, json={"messages": [{"id": mid} for mid in messages]}
        )
    if path.endswith("/users/me/threads"):
        q = request.url.params.get("q", "")
        threads = [
            {"id": f"t-{mid}", "snippet": message[2]}
            for mid, message in messages.items()
            if _gmail_matches(q, message)
        ]
        return httpx.Response(200, json={"threads": threads})
    mid = path.rsplit("/", 1)[-1]
    if "/users/me/threads/" in path and mid.removeprefix("t-") in messages:
        message = _gmail_message(mid.removeprefix("t-"), messages)
        return httpx.Response(
            200,
            json={
                "id": mid,
                "snippet": message["snippet"],
                "messages": [message],
            },
        )
    if "/users/me/messages/" in path and mid in messages:
        return httpx.Response(200, json=_gmail_message(mid, messages))
    return None


def _gmail_matches(q: str, message: GmailMessage) -> bool:
    """Gmail search: `is:`/`in:` labels, `from:` senders, and words in the message."""
    sender, subject, body, _, labels = message
    text = f"{sender} {subject} {body}".lower()
    for term in q.lower().split():
        key, _, value = term.partition(":")
        if value and key in ("is", "in"):
            if value.upper() not in labels:
                return False
        elif value and key == "from":
            if value not in sender.lower():
                return False
        elif term not in text:
            return False
    return True


def _gmail_message(
    mid: str, messages: Mapping[str, GmailMessage]
) -> dict[str, Any]:
    sender, subject, snippet, minutes_ago, labels = messages[mid]
    received = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    body = base64.urlsafe_b64encode(snippet.encode()).decode().rstrip("=")
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "labelIds": labels,
        "snippet": snippet,
        "internalDate": str(int(received.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": format_datetime(received)},
                {"name": "Message-ID", "value": f"<{mid}@voice-agent.dev>"},
            ],
            "body": {"data": body},
        },
    }


def _github(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path
    segments = path.strip("/").split("/")
    if path == "/user":
        return httpx.Response(200, json={"login": "voice-agent-dev"})
    if path.endswith("/actions/runs"):
        run = {
            "name": "CI Unit & Integration",
            "conclusion": "success",
            "status": "completed",
            "head_branch": "feat/redis-jwt-rotation",
            "event": "pull_request",
            "updated_at": _ago(minutes=40),
        }
        return httpx.Response(200, json={"workflow_runs": [run]})
    if path == "/search/issues":
        mine = [
            {
                **_pull(number),
                "repository_url": "https://api.github.com/repos/google/adk-python",
                "pull_request": {
                    "merged_at": _ago(days=3) if number == 12 else None
                },
                "state": "open" if number == 18 else "closed",
            }
            for number in (18, 12)
        ]
        return httpx.Response(200, json={"items": mine})
    if path.endswith("/check-runs"):
        conclusions = _PULLS.get(
            int(segments[-2].removeprefix("sha")), _PULLS[18]
        )[4]
        runs = [
            {"status": "completed" if c else "in_progress", "conclusion": c}
            for c in conclusions
        ]
        return httpx.Response(200, json={"check_runs": runs})
    if path.endswith("/reviews"):
        states = _PULLS.get(int(segments[-2]), _PULLS[18])[5]
        reviews = [{"user": {"login": "reviewer"}, "state": s} for s in states]
        return httpx.Response(200, json=reviews)
    if segments[-2:-1] == ["pulls"]:
        return httpx.Response(200, json=_pull(int(segments[-1])))
    if path.endswith("/pulls"):
        return httpx.Response(200, json=[_pull(n) for n in (18, 21, 19)])
    if path.endswith("/issues"):
        issues = [
            {
                "number": 42,
                "title": "Redis connection pool tuning",
                "user": {"login": "kchen"},
            },
            {
                "number": 45,
                "title": "OAuth token refresh",
                "user": {"login": "dpark"},
            },
        ]
        return httpx.Response(200, json=issues)
    return None


IAP_KEY_ID = "fake-iap-key"
IAP_AUDIENCE = (
    "/projects/123456789/locations/us-central1/services/adk-voice-agent"
)
_IAP_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())


def _pem_public_key(private_key: ec.EllipticCurvePrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def iap_assertion(
    email: str,
    *,
    audience: str = IAP_AUDIENCE,
    issuer: str = "https://cloud.google.com/iap",
    expires_in_s: int = 600,
    private_key: ec.EllipticCurvePrivateKey | None = None,
    key_id: str = IAP_KEY_ID,
) -> str:
    pem = (private_key or _IAP_PRIVATE_KEY).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = es256.ES256Signer.from_string(pem, key_id=key_id)
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": f"accounts.google.com:{email}",
        "email": email,
        "iat": now - 5,
        "exp": now + expires_in_s,
    }
    return google_jwt.encode(signer, claims).decode()


def iap_headers(email: str, **kwargs: Any) -> dict[str, str]:
    return {"X-Goog-IAP-JWT-Assertion": iap_assertion(email, **kwargs)}


def _iap_keys(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/iap/verify/public_key":
        return httpx.Response(
            200, json={IAP_KEY_ID: _pem_public_key(_IAP_PRIVATE_KEY)}
        )
    return None


def google_token_transport(
    grants: dict[str, list[str]],
    *,
    requests: list[httpx.Request] | None = None,
    ignore_scope: bool = False,
) -> httpx.MockTransport:
    """Google OAuth token endpoint over `integrations_transport()`, enforcing granted scopes unless `ignore_scope`."""
    fallback = integrations_transport(requests=requests)
    issued = [0]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "oauth2.googleapis.com":
            return await fallback.handle_async_request(request)
        if requests is not None:
            requests.append(request)
        form = dict(httpx.QueryParams(request.content.decode()))
        granted = grants.get(form.get("refresh_token", ""))
        if granted is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        asked = form.get("scope", "").split()
        if asked and not ignore_scope and not set(asked) <= set(granted):
            return httpx.Response(
                400,
                json={
                    "error": "invalid_scope",
                    "error_description": "Bad scope",
                },
            )
        issued[0] += 1
        scopes = asked if asked and not ignore_scope else granted
        return httpx.Response(
            200,
            json={
                "access_token": f"ya29.{form['refresh_token'].split('-')[0]}.{issued[0]}",
                "expires_in": 3599,
                "scope": " ".join(scopes),
                "token_type": "Bearer",
            },
        )

    return httpx.MockTransport(handler)


class FakeDrive:
    """Stateful stand-in for Drive v3 folder/file creation and multipart uploads."""

    def __init__(
        self, fallback: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.fallback = fallback
        self.files: dict[str, dict[str, Any]] = {}

    def named(self, name: str) -> dict[str, Any]:
        return next(f for f in self.files.values() if f["name"] == name)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/drive/v3/files" and method == "GET":
            q = request.url.params.get("q", "")
            if "appProperties has" in q:
                value = q.split("value='", 1)[1].split("'", 1)[0]
                hits = [
                    {"id": i}
                    for i, f in self.files.items()
                    if f.get("appProperties", {}).get("adk_voice_agent")
                    == value
                ]
                return httpx.Response(200, json={"files": hits[:1]})
        if path == "/drive/v3/files" and method == "POST":
            return self._store(json.loads(request.content), b"", "")
        if path.startswith("/drive/v3/files/") and method == "PATCH":
            file_id = path.rsplit("/", 1)[-1]
            if file_id not in self.files:
                return httpx.Response(404, json={"error": {"code": 404}})
            self.files[file_id].update(json.loads(request.content))
            return httpx.Response(200, json={"id": file_id})
        if path == "/upload/drive/v3/files" and method == "POST":
            return self._store(*_multipart(request))
        if path.startswith("/upload/drive/v3/files/") and method == "PATCH":
            file_id = path.rsplit("/", 1)[-1]
            metadata, content, content_type = _multipart(request)
            self.files[file_id].update(
                {**metadata, "content": content, "content_type": content_type}
            )
            return httpx.Response(200, json=self._public(file_id))
        if self.fallback is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        return await self.fallback.handle_async_request(request)

    def _store(
        self, metadata: dict[str, Any], content: bytes, content_type: str
    ) -> httpx.Response:
        file_id = f"drive-{len(self.files) + 1}"
        self.files[file_id] = {
            **metadata,
            "content": content,
            "content_type": content_type,
        }
        return httpx.Response(200, json=self._public(file_id))

    def _public(self, file_id: str) -> dict[str, str]:
        return {
            "id": file_id,
            "name": self.files[file_id]["name"],
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        }


def _multipart(request: httpx.Request) -> tuple[dict[str, Any], bytes, str]:
    boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
    meta_part, media_part = request.content.split(b"--" + boundary)[1:3]
    metadata = json.loads(meta_part.split(b"\r\n\r\n", 1)[1])
    head, media = media_part.split(b"\r\n\r\n", 1)
    content_type = head.decode().split("Content-Type: ", 1)[1].strip()
    return metadata, media.removesuffix(b"\r\n"), content_type
