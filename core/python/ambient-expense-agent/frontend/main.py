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

"""Approval UI frontend — thin FastAPI proxy to the ADK backend.

Serves the approval HTML and queries the backend's built-in ADK session
APIs to find expenses pending human approval:

- ``GET /pending-approvals`` → queries ADK session APIs on the backend
- ``POST /approve`` → backend ``POST /run`` (resumes HITL workflow)

On Cloud Run, uses ID tokens for service-to-service authentication.
Locally, calls the backend directly without auth.
"""

import asyncio
import json
import logging
import os
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080")
USE_SERVICE_AUTH = os.environ.get("USE_SERVICE_AUTH", "false").lower() == "true"
APP_NAME = os.environ.get("APP_NAME", "expense_agent")

# The Pub/Sub subscription name is used as user_id by the ADK trigger
# handler. This must match the ``subscription`` field in trigger requests.
# For local development the curl test commands use "test-sub".
PUBSUB_SUBSCRIPTION = os.environ.get("PUBSUB_SUBSCRIPTION", "test-sub")

# Minimum expense amount that triggers the review path.
REVIEW_THRESHOLD = 100

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Expense Approval UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


async def _get_auth_headers() -> dict[str, str]:
    """Get ID token headers for authenticated calls to the backend.

    On Cloud Run, the frontend's service account must have
    ``roles/run.invoker`` on the backend service. The metadata server
    provides an ID token scoped to the backend URL.
    """
    if not USE_SERVICE_AUTH:
        return {}

    import google.auth.transport.requests
    import google.oauth2.id_token

    auth_req = google.auth.transport.requests.Request()
    token = google.oauth2.id_token.fetch_id_token(auth_req, BACKEND_URL)
    return {"Authorization": f"Bearer {token}"}


def _extract_pending_approval(session: dict, user_id: str) -> dict | None:
    """Extract pending approval info from a session's events.

    Scans for an ``adk_request_input`` function call without a matching
    response, and collects the review context from
    ``emit_expense_alert`` if present.
    """
    request_input = None
    responded = False
    review_summary = None

    for event in session.get("events", []):
        content = event.get("content") or {}
        parts = content.get("parts") or []

        for part in parts:
            fc = part.get("functionCall")
            if fc:
                name = fc.get("name")
                if name == "emit_expense_alert":
                    args = fc.get("args", {})
                    if args.get("risk_summary"):
                        review_summary = args["risk_summary"]
                elif name == "adk_request_input":
                    args = fc.get("args", {})
                    payload = args.get("payload")
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    request_input = {
                        "interrupt_id": fc.get("id", ""),
                        "message": args.get("message", ""),
                        "payload": payload,
                    }

            fr = part.get("functionResponse")
            if fr and fr.get("name") == "adk_request_input":
                responded = True

    if not request_input or responded:
        return None

    item = {
        "session_id": session["id"],
        "user_id": user_id,
        **request_input,
    }
    if review_summary:
        item["review"] = review_summary
    return item


@app.get("/approval")
async def approval_ui() -> FileResponse:
    """Serve the approval UI."""
    return FileResponse(os.path.join(STATIC_DIR, "approval.html"))


@app.get("/pending-approvals")
async def pending_approvals():
    """List expenses waiting for manager approval.

    Queries the backend's built-in ADK session APIs to find sessions
    with a pending ``adk_request_input`` function call. The ``user_id``
    is the Pub/Sub subscription name, set automatically by the ADK
    trigger handler when processing incoming messages.
    """
    user_id = PUBSUB_SUBSCRIPTION
    encoded_uid = quote(user_id, safe="")
    headers = await _get_auth_headers()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # List all sessions for this Pub/Sub subscription user.
            url = f"{BACKEND_URL}/apps/{APP_NAME}/users/{encoded_uid}/sessions"
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return []
            sessions = resp.json()

            # Pre-filter: only sessions that went through the review path
            # (amount >= threshold). The state is populated in list response.
            review_ids = [
                s["id"]
                for s in sessions
                if s.get("state", {}).get("expense_data", {}).get("amount", 0)
                >= REVIEW_THRESHOLD
            ]
            if not review_ids:
                return []

            # Fetch full session details (with events) concurrently.
            async def fetch_session(sid: str) -> dict | None:
                r = await client.get(
                    f"{BACKEND_URL}/apps/{APP_NAME}/users/{encoded_uid}/sessions/{sid}",
                    headers=headers,
                )
                return r.json() if r.status_code == 200 else None

            full_sessions = await asyncio.gather(
                *(fetch_session(sid) for sid in review_ids)
            )

            pending = []
            for session in full_sessions:
                if not session:
                    continue
                item = _extract_pending_approval(session, user_id)
                if item:
                    pending.append(item)
            return pending

    except Exception:
        return []


@app.post("/approve")
async def approve(request: Request):
    """Proxy an approval/rejection decision to the backend's POST /run."""
    body = await request.json()
    headers = await _get_auth_headers()
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/run", json=body, headers=headers, timeout=30.0
        )
    return resp.json()


if __name__ == "__main__":
    # Port resolution:
    #   * Cloud Run injects PORT into every container, so honouring it is
    #     required for the deployed frontend to receive traffic.
    #   * Locally, the backend also reads PORT (with default 8080). If both
    #     services shared a single PORT from .env, they'd collide on one
    #     port. FRONTEND_PORT is the local-only escape hatch: set it in
    #     .env alongside a distinct PORT for the backend to keep the two
    #     services on separate ports. Default 8081 preserves the original
    #     behaviour for a plain `make dev-frontend` with no env config.
    port = int(
        os.environ.get("FRONTEND_PORT") or os.environ.get("PORT", "8081")
    )
    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104
