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

"""``_slice_state`` pulls ``pending_credential`` from event actions.

When a tool calls ``tool_context.request_credential(auth_config)``, ADK
enqueues the ``AuthConfig`` in ``event.actions.requested_auth_configs``
keyed by ``function_call_id``. The chat UI reads this via
``GET /lha/state`` to render an auth card.  Once a later event carries
the auth response — either ``oauth:<credential_key>`` (lha's OAuth
callback marker) or ``temp:<credential_key>`` (ADK's standard auth
preprocessor stash) — the slice clears.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.api.state import _latest_pending_credential, _slice_state


def _auth_config(
    *,
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth?client_id=...",
    credential_key: str = "google-drive",
    scopes: list[str] | None = None,
) -> SimpleNamespace:
    flow = SimpleNamespace(
        scopes={s: s for s in (scopes or ["drive.readonly"])},
        authorizationUrl="https://accounts.google.com/o/oauth2/auth",
        tokenUrl="https://oauth2.googleapis.com/token",
    )
    return SimpleNamespace(
        credential_key=credential_key,
        auth_scheme=SimpleNamespace(
            flows=SimpleNamespace(authorizationCode=flow)
        ),
        exchanged_auth_credential=SimpleNamespace(
            oauth2=SimpleNamespace(auth_uri=auth_uri)
        ),
    )


def _event_with_request(
    fcid: str, *, config: SimpleNamespace
) -> SimpleNamespace:
    return SimpleNamespace(
        content=None,
        actions=SimpleNamespace(
            requested_auth_configs={fcid: config},
            requested_tool_confirmations={},
        ),
    )


def _event_with_state_delta(delta: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        content=None,
        actions=SimpleNamespace(
            requested_auth_configs={},
            requested_tool_confirmations={},
            state_delta=delta,
        ),
    )


def test_empty_events_returns_none():
    assert _latest_pending_credential(None) is None
    assert _latest_pending_credential([]) is None


def test_unresolved_request_returns_payload():
    events = [
        _event_with_request(
            "fc-drive-1",
            config=_auth_config(
                auth_uri="https://accounts.google.com/o/oauth2/auth?cid=1",
                credential_key="google-drive",
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            ),
        )
    ]
    pending = _latest_pending_credential(events)
    assert pending == {
        "interrupt_id": "fc-drive-1",
        "provider": "google-drive",
        "auth_url": "https://accounts.google.com/o/oauth2/auth?cid=1",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    }


def test_resolved_request_via_state_delta_returns_none():
    events = [
        _event_with_request(
            "fc-drive-1",
            config=_auth_config(credential_key="google-drive"),
        ),
        _event_with_state_delta({"temp:google-drive": "raw-token"}),
    ]
    assert _latest_pending_credential(events) is None


def test_resolved_request_via_oauth_marker_returns_none():
    """The lha OAuth callback writes ``oauth:<provider>: True`` as a
    persistent marker (``temp:`` keys get stripped by ADK before persist).
    """
    events = [
        _event_with_request(
            "fc-drive-1",
            config=_auth_config(credential_key="google-drive"),
        ),
        _event_with_state_delta({"oauth:google-drive": True}),
    ]
    assert _latest_pending_credential(events) is None


def test_latest_pending_wins_when_multiple_unresolved():
    events = [
        _event_with_request(
            "fc-1", config=_auth_config(credential_key="github")
        ),
        _event_with_request(
            "fc-2", config=_auth_config(credential_key="google-drive")
        ),
    ]
    pending = _latest_pending_credential(events)
    assert pending["interrupt_id"] == "fc-2"
    assert pending["provider"] == "google-drive"


def test_slice_state_exposes_pending_credential_field():
    events = [_event_with_request("fc-drive-9", config=_auth_config())]
    sliced = _slice_state({}, events=events)
    assert "pending_credential" in sliced
    assert sliced["pending_credential"]["interrupt_id"] == "fc-drive-9"


def test_slice_state_without_events_has_null_pending_credential():
    sliced = _slice_state({}, events=None)
    assert sliced["pending_credential"] is None
