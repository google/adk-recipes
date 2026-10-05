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

"""GitHub App authentication, webhook validation, and comment helpers."""

from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import httpx
import jwt

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class CommentCommand:
    """A parsed request for the Gemini Enterprise agent."""

    prompt: str
    context: str


@dataclass(frozen=True)
class GitHubCommentTarget:
    """Where a response should be posted."""

    kind: Literal["issue", "review"]
    number: int | None = None
    comment_id: int | None = None

    @classmethod
    def from_event(
        cls, event_name: str, payload: dict[str, Any]
    ) -> GitHubCommentTarget:
        """Resolve the reply target for a supported GitHub webhook payload."""
        if event_name == "issue_comment":
            return cls(kind="issue", number=payload["issue"]["number"])
        if event_name == "pull_request_review_comment":
            return cls(kind="review", comment_id=payload["comment"]["id"])
        raise ValueError(f"Unsupported GitHub event: {event_name}")


def verify_webhook_signature(
    payload_body: bytes, signature_header: str, webhook_secret: str
) -> bool:
    """Verify GitHub's HMAC SHA-256 webhook signature."""
    expected = (
        "sha256="
        + hmac.new(
            webhook_secret.encode("utf-8"),
            payload_body,
            sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


def is_supported_comment_event(
    event_name: str, payload: dict[str, Any]
) -> bool:
    """Return whether this webhook should be considered for an answer."""
    if payload.get("action") != "created":
        return False
    if "installation" not in payload or "repository" not in payload:
        return False
    return event_name in {"issue_comment", "pull_request_review_comment"}


def extract_comment_command(
    payload: dict[str, Any],
    *,
    command_prefix: str,
    app_slug: str | None,
) -> CommentCommand | None:
    """Extract a command addressed to the app from a GitHub comment payload."""
    comment = payload.get("comment", {})
    sender_login = payload.get("sender", {}).get("login")
    if app_slug and sender_login == f"{app_slug}[bot]":
        return None

    body = str(comment.get("body", "")).strip()
    if not body.startswith(command_prefix):
        return None

    prompt = body.removeprefix(command_prefix).strip()
    if not prompt:
        return None

    repository = payload.get("repository", {})
    issue = payload.get("issue", {})
    pull_request = payload.get("pull_request", {})
    context_parts = [
        f"Repository: {repository.get('full_name', 'unknown')}",
        f"Comment URL: {comment.get('html_url', 'unknown')}",
    ]
    if "number" in issue:
        context_parts.append(f"Issue or PR number: {issue['number']}")
        context_parts.append(f"Title: {issue.get('title', 'unknown')}")
    if "number" in pull_request:
        context_parts.append(f"Pull request number: {pull_request['number']}")
        context_parts.append(f"Title: {pull_request.get('title', 'unknown')}")

    return CommentCommand(
        prompt=prompt,
        context="\n".join(context_parts),
    )


class GitHubAppClient:
    """Minimal GitHub App client for installation comments."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> GitHubAppClient:
        """Create a GitHub App client from environment variables."""
        private_key = os.getenv("GITHUB_PRIVATE_KEY")
        private_key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")
        if not private_key and private_key_path:
            private_key = Path(private_key_path).read_text(encoding="utf-8")
        if not private_key:
            raise RuntimeError(
                "Set GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH."
            )

        return cls(
            app_id=os.environ["GITHUB_APP_ID"],
            private_key_pem=private_key,
            api_url=os.getenv("GITHUB_API_URL", GITHUB_API_URL),
        )

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    async def _installation_token(self, installation_id: int) -> str:
        url = (
            f"{self._api_url}/app/installations/{installation_id}/access_tokens"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            )
            response.raise_for_status()
            return str(response.json()["token"])

    async def post_comment(
        self,
        *,
        installation_id: int,
        repository_full_name: str,
        target: GitHubCommentTarget,
        body: str,
    ) -> None:
        """Post an issue comment or review-thread reply."""
        token = await self._installation_token(installation_id)
        if target.kind == "issue":
            if target.number is None:
                raise ValueError("Issue comment target requires a number.")
            url = (
                f"{self._api_url}/repos/{repository_full_name}/issues/"
                f"{target.number}/comments"
            )
        else:
            if target.comment_id is None:
                raise ValueError("Review reply target requires a comment ID.")
            url = (
                f"{self._api_url}/repos/{repository_full_name}/pulls/comments/"
                f"{target.comment_id}/replies"
            )

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
                json={"body": body},
            )
            response.raise_for_status()
