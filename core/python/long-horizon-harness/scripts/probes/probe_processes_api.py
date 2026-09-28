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

"""Live smoke for the /lha/processes surface (Background panel backend).

Drives the REAL root agent (real Gemini) to start a background `terminal`
process, then exercises the actual HTTP route the web panel uses:
``GET /lha/processes`` must list it and ``DELETE /lha/processes/{id}`` must
kill it. Validates the whole model -> tool -> registry -> API -> kill chain.

Run:  (env from .env)  uv run python scripts/probes/probe_processes_api.py
Hits Vertex — needs GCP auth + GOOGLE_CLOUD_* set. LHA_ENVIRONMENT_BACKEND=local.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import types

_SLEEP = "sleep 120"
_PROMPT = (
    "Call the `terminal` tool once with background=True and this exact command: "
    f"{_SLEEP}. Just start it and tell me the session_id. Do NOT kill it."
)

_USER_ID = "probe-user"


async def main() -> None:
    from app.api.processes import attach_processes_routes
    from app.auth import current_user_id
    from app.fast_api_app import build_runner

    runner = build_runner()
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=_USER_ID
    )

    print("=== ask root to start a background process ===")
    async for _ in runner.run_async(
        user_id=_USER_ID,
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=_PROMPT)]
        ),
    ):
        pass

    app = FastAPI()
    attach_processes_routes(app)
    app.dependency_overrides[current_user_id] = lambda: _USER_ID
    client = TestClient(app)

    listing = client.get("/lha/processes").json()["processes"]
    print("GET /lha/processes ->", [p["command"] for p in listing])
    match = [p for p in listing if "sleep 120" in p["command"] and p["running"]]
    if not match:
        print("RESULT: INCONCLUSIVE (agent did not start a visible bg process)")
        return

    sid = match[0]["session_id"]
    killed = client.delete(f"/lha/processes/{sid}")
    print(
        f"DELETE /lha/processes/{sid} -> {killed.status_code} {killed.json()}"
    )

    after = client.get("/lha/processes").json()["processes"]
    still_running = any(p["session_id"] == sid and p["running"] for p in after)
    ok = killed.status_code == 200 and not still_running
    print(
        f"\nRESULT: {'PASS' if ok else 'FAIL'} (listed=True, killed={not still_running})"
    )


if __name__ == "__main__":
    asyncio.run(main())
