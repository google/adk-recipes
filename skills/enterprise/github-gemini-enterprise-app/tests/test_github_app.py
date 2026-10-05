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

"""Unit tests for GitHub webhook helpers."""

import hmac
from hashlib import sha256

from app.github_app import (
    GitHubCommentTarget,
    extract_comment_command,
    is_supported_comment_event,
    verify_webhook_signature,
)


def _payload(body: str) -> dict:
    return {
        "action": "created",
        "installation": {"id": 123},
        "repository": {"full_name": "octo/example"},
        "sender": {"login": "octocat"},
        "comment": {
            "id": 456,
            "body": body,
            "html_url": "https://github.com/octo/example/issues/1#comment",
        },
        "issue": {"number": 1, "title": "Question"},
    }


def test_verify_webhook_signature_accepts_valid_signature() -> None:
    body = b'{"ok": true}'
    secret = "shared-secret"
    signature = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    )

    assert verify_webhook_signature(body, signature, secret)


def test_verify_webhook_signature_rejects_invalid_signature() -> None:
    assert not verify_webhook_signature(
        b'{"ok": true}', "sha256=bad", "shared-secret"
    )


def test_supported_comment_event_requires_created_action() -> None:
    assert is_supported_comment_event("issue_comment", _payload("hello"))

    edited_payload = _payload("hello")
    edited_payload["action"] = "edited"
    assert not is_supported_comment_event("issue_comment", edited_payload)


def test_extract_comment_command_returns_prompt_and_context() -> None:
    command = extract_comment_command(
        _payload("@gemini-enterprise What is the refund policy?"),
        command_prefix="@gemini-enterprise",
        app_slug="gemini-enterprise",
    )

    assert command is not None
    assert command.prompt == "What is the refund policy?"
    assert "Repository: octo/example" in command.context
    assert "Issue or PR number: 1" in command.context


def test_extract_comment_command_ignores_other_comments() -> None:
    assert (
        extract_comment_command(
            _payload("What is the refund policy?"),
            command_prefix="@gemini-enterprise",
            app_slug="gemini-enterprise",
        )
        is None
    )


def test_extract_comment_command_ignores_own_bot() -> None:
    payload = _payload("@gemini-enterprise What is the refund policy?")
    payload["sender"]["login"] = "gemini-enterprise[bot]"

    assert (
        extract_comment_command(
            payload,
            command_prefix="@gemini-enterprise",
            app_slug="gemini-enterprise",
        )
        is None
    )


def test_comment_target_from_issue_comment() -> None:
    target = GitHubCommentTarget.from_event("issue_comment", _payload("hello"))

    assert target.kind == "issue"
    assert target.number == 1


def test_comment_target_from_review_comment() -> None:
    payload = _payload("@gemini-enterprise hello")
    payload["pull_request"] = {"number": 2, "title": "Change"}

    target = GitHubCommentTarget.from_event(
        "pull_request_review_comment", payload
    )

    assert target.kind == "review"
    assert target.number == 2
    assert target.comment_id == 456
