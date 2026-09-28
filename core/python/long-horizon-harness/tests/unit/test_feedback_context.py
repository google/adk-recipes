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

"""Redacted contextual summary attached to opt-in feedback.

When a user opts to share conversation context with their feedback, the server
builds a compact transcript digest of the recent turns with known secret
shapes scrubbed — never the raw, unredacted session.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.feedback.context import build_redacted_context


def _event(author: str, *texts: str) -> SimpleNamespace:
    parts = [SimpleNamespace(text=t) for t in texts]
    return SimpleNamespace(author=author, content=SimpleNamespace(parts=parts))


def _session(*events: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(events=list(events))


def test_empty_session_yields_empty_string() -> None:
    assert build_redacted_context(_session()) == ""


def test_digest_labels_user_and_assistant_turns() -> None:
    out = build_redacted_context(
        _session(
            _event("user", "how do I deploy?"),
            _event("root_agent", "run make deploy"),
        )
    )
    assert "user: how do I deploy?" in out
    assert "assistant: run make deploy" in out


def test_secrets_are_redacted() -> None:
    out = build_redacted_context(
        _session(
            _event("user", "my key is ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        )
    )
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert "[redacted:" in out


def test_keeps_only_the_most_recent_turns() -> None:
    events = [_event("user", f"turn {i}") for i in range(40)]
    out = build_redacted_context(_session(*events), max_turns=5)
    assert "turn 39" in out
    assert "turn 0" not in out
    assert len(out.splitlines()) == 5


def test_long_turns_are_truncated() -> None:
    out = build_redacted_context(
        _session(_event("user", "x" * 5000)), max_chars_per_turn=100
    )
    line = out.splitlines()[0]
    assert len(line) < 200
    assert line.endswith("…")
