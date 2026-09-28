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

"""FastAPI entry point for the ambient expense agent backend.

This file configures the ADK web server with Pub/Sub trigger endpoints
enabled, allowing the agent to process expense reports autonomously
when deployed to Cloud Run.

The frontend service queries the ADK's built-in session APIs
(``GET /apps/{app}/users/{user}/sessions``) to find pending approvals.

Includes middleware to normalize Pub/Sub subscription names from their
fully-qualified resource paths (``projects/.../subscriptions/NAME``)
to short names, keeping user IDs clean and readable in session records.
"""

import json
import os

import uvicorn
from google.adk.cli.fast_api import get_fast_api_app
from starlette.requests import Request

# The ADK needs the project root as agents_dir so it discovers
# expense_agent/ as an agent package (contains agent.py + __init__.py).
AGENTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = get_fast_api_app(
    agents_dir=AGENTS_DIR,
    web=False,
    trigger_sources=["pubsub"],
)


@app.middleware("http")
async def normalize_pubsub_subscription(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Normalize ``projects/.../subscriptions/NAME`` to just ``NAME``.

    Pub/Sub push deliveries include the fully-qualified subscription
    resource path. The ADK trigger handler uses this value as the
    session ``user_id``. Normalizing to the short name keeps session
    records clean and consistent with the subscription name used by
    the frontend when querying for pending approvals.
    """
    if (
        request.url.path.endswith("/trigger/pubsub")
        and request.method == "POST"
    ):
        body = await request.body()
        try:
            data = json.loads(body)
            sub = data.get("subscription", "")
            if "/" in sub:
                data["subscription"] = sub.rsplit("/", 1)[-1]
                request._body = json.dumps(data).encode()
        except (json.JSONDecodeError, KeyError):
            pass
    return await call_next(request)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 -- container entrypoint
        port=int(os.environ.get("PORT", "8080")),
    )
