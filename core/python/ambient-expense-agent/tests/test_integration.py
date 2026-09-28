# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Integration tests for the ambient expense agent.

Spins up both backend (ADK + triggers) and frontend (approval UI proxy)
using ASGI test transports — no real HTTP servers needed. Tests the full
workflow: trigger → agent processing → session query → HITL approval.
"""

import base64
import json

import httpx
import pytest
import pytest_asyncio

from app.fast_api_app import app as backend_app
from frontend.main import app as frontend_app


@pytest_asyncio.fixture
async def backend():
    """ASGI client for the backend (ADK agent + triggers)."""
    transport = httpx.ASGITransport(app=backend_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://backend"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def frontend(monkeypatch):
    """ASGI client for the frontend (approval UI proxy).

    Patches BACKEND_URL so the frontend proxies to the backend's ASGI
    transport instead of making real HTTP calls.
    """
    # The frontend makes HTTP calls to BACKEND_URL. We need to patch
    # those to go through the backend's ASGI transport instead.
    # Monkeypatch the backend URL used by the frontend.
    import frontend.main as frontend_module

    monkeypatch.setattr(frontend_module, "BACKEND_URL", "http://backend")

    # Create a shared backend transport for the frontend's httpx calls
    backend_transport = httpx.ASGITransport(app=backend_app)

    # Patch httpx.AsyncClient in the frontend to use the ASGI transport
    original_async_client = httpx.AsyncClient

    class PatchedAsyncClient(original_async_client):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", backend_transport)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedAsyncClient)

    transport = httpx.ASGITransport(app=frontend_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://frontend"
    ) as c:
        yield c


def _make_pubsub_payload(expense: dict) -> dict:
    """Build a Pub/Sub trigger request body from an expense dict."""
    encoded = base64.b64encode(json.dumps(expense).encode()).decode()
    return {
        "message": {
            "data": encoded,
            "attributes": {"source": "test"},
        },
        "subscription": "test-sub",
    }


@pytest.mark.asyncio
async def test_auto_approve(backend):
    """Expenses under $100 are auto-approved without HITL."""
    payload = _make_pubsub_payload(
        {
            "amount": 45.00,
            "submitter": "bob@company.com",
            "category": "meals",
            "description": "Team lunch",
            "date": "2026-04-12",
        }
    )

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


@pytest.mark.asyncio
async def test_review_and_hitl_approval(backend, frontend, monkeypatch):
    """Expenses >= $100 go through review, pause for HITL, and resume on approval."""
    # Step 1: Submit a $250 expense — triggers review + HITL pause
    payload = _make_pubsub_payload(
        {
            "amount": 250.00,
            "submitter": "alice@company.com",
            "category": "travel",
            "description": "Flight to NYC for client meeting",
            "date": "2026-04-10",
        }
    )

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    # Step 2: Frontend finds the pending approval
    resp = await frontend.get("/pending-approvals")
    assert resp.status_code == 200
    pending = resp.json()
    assert len(pending) >= 1

    item = pending[0]
    assert item["payload"]["amount"] == 250.00
    assert item["payload"]["submitter"] == "alice@company.com"
    assert item["session_id"]
    assert item["interrupt_id"]

    # Step 3: Approve via frontend
    approval_body = {
        "appName": "app",
        "userId": item["user_id"],
        "sessionId": item["session_id"],
        "newMessage": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": item["interrupt_id"],
                        "name": "adk_request_input",
                        "response": {
                            "result": json.dumps({"decision": "approve"}),
                        },
                    }
                }
            ],
        },
    }

    resp = await frontend.post("/approve", json=approval_body)
    assert resp.status_code == 200
    events = resp.json()

    # The last event should be process_decision with status=approved
    last_event = events[-1]
    assert last_event["output"]["status"] == "approved"
    assert "process_decision" in last_event["nodeInfo"]["path"]

    # Step 4: No more pending approvals
    resp = await frontend.get("/pending-approvals")
    assert resp.status_code == 200
    assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_review_and_hitl_rejection(backend, frontend, monkeypatch):
    """Rejecting an expense logs the rejection and clears the pending list."""
    # Submit a $500 expense
    payload = _make_pubsub_payload(
        {
            "amount": 500.00,
            "submitter": "charlie@company.com",
            "category": "equipment",
            "description": "Standing desk",
            "date": "2026-04-11",
        }
    )

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200

    # Get pending
    resp = await frontend.get("/pending-approvals")
    pending = resp.json()
    item = next(p for p in pending if p["payload"]["amount"] == 500.00)

    # Reject
    rejection_body = {
        "appName": "app",
        "userId": item["user_id"],
        "sessionId": item["session_id"],
        "newMessage": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": item["interrupt_id"],
                        "name": "adk_request_input",
                        "response": {
                            "result": json.dumps({"decision": "reject"}),
                        },
                    }
                }
            ],
        },
    }

    resp = await frontend.post("/approve", json=rejection_body)
    assert resp.status_code == 200
    events = resp.json()
    last_event = events[-1]
    assert last_event["output"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_subscription_normalization(backend):
    """Full Pub/Sub subscription paths are normalized to short names."""
    payload = _make_pubsub_payload(
        {
            "amount": 30.00,
            "submitter": "dave@company.com",
            "category": "supplies",
            "description": "Printer paper",
            "date": "2026-04-13",
        }
    )
    # Use a full subscription path like real Pub/Sub sends
    payload["subscription"] = "projects/my-project/subscriptions/test-sub"

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200

    # Session should be queryable with the short name
    resp = await backend.get("/apps/app/users/test-sub/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) >= 1
