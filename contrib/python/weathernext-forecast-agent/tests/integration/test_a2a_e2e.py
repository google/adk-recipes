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
"""A2A round trips over both bindings the Gemini agent / Gemini Enterprise use.

Starts the server and checks:
- JSON-RPC: the agent card has the v0.3 JSON-RPC shape, and a v0.3
  `message/send` returns the workflow's answer as a completed task.
- HTTP+JSON (the binding Agent Registry uses to invoke A2A agents from the
  Gemini agent): `GET /v1/card` and `POST /v1/message:send` with
  `role: ROLE_USER`, `content[].text` and a unique `messageId`.
- The Agent Engine adapter can create a session.

Answers are checked against the bundled ECMWF ensemble snapshot (run
2026-10-19 06 UTC). Calls Gemini, so it needs credentials in .env; excluded
from CI like the other integration tests:

    uv run pytest tests/integration/test_a2a_e2e.py
"""

import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

import pytest
import requests
from requests.exceptions import RequestException


def _free_port() -> int:
    # Other local servers often hold 8000/8080; let the OS pick a free port.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"
RPC_URL = f"{BASE_URL}/a2a/app"
CARD_URL = f"{RPC_URL}/.well-known/agent-card.json"


@pytest.fixture(scope="module")
def server() -> Iterator[None]:
    env = os.environ.copy()
    env.update(
        INTEGRATION_TEST="TRUE",
        APP_URL=BASE_URL,
        ADK_DISABLE_LOCAL_STORAGE="1",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.fast_api_app:app",
            "--port",
            str(PORT),
        ],
        env=env,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if requests.get(CARD_URL, timeout=5).status_code == 200:
                    break
            except RequestException:
                pass
            time.sleep(1)
        else:
            pytest.fail("Server did not start")
        yield
    finally:
        process.terminate()
        process.wait()


def test_agent_card_has_v0_3_jsonrpc_interface(server: None) -> None:
    card = requests.get(CARD_URL, timeout=30).json()
    assert card["name"] and card["description"]
    assert card["skills"]
    interfaces = {
        (i["protocolBinding"], i["protocolVersion"]): i["url"]
        for i in card["supportedInterfaces"]
    }
    assert interfaces[("JSONRPC", "0.3")] == RPC_URL
    # Top-level fields of the 0.3 card shape.
    assert card["url"] == RPC_URL
    assert card["protocolVersion"] == "0.3"


def test_message_send_returns_completed_plan(server: None) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [
                    {
                        "kind": "text",
                        "text": "Can we do a blade exchange at Whitelee this week?",
                    }
                ],
            }
        },
    }
    response = requests.post(RPC_URL, json=body, timeout=300)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["kind"] == "task"
    assert result["status"]["state"] == "completed"
    answer = " ".join(
        part.get("text", "")
        for artifact in result.get("artifacts", [])
        for part in artifact["parts"]
    )
    # Snapshot: best 8-hour lift window at Whitelee is Thu 22 Oct, 98% (50/51).
    assert re.search(r"98(\.0)?\s?%", answer), answer
    assert "50/51" in answer.replace(" ", "") or "50 of 51" in answer
    assert "decision support" in answer.lower()


def test_http_json_card_and_message_send(server: None) -> None:
    card = requests.get(f"{RPC_URL}/v1/card", timeout=30)
    assert card.status_code == 200
    card_json = card.json()
    assert card_json["name"] and card_json["description"]
    assert card_json["url"] == RPC_URL

    wk = requests.get(CARD_URL, timeout=30).json()
    bindings = {
        (i["protocolBinding"], i["protocolVersion"])
        for i in wk["supportedInterfaces"]
    }
    assert ("HTTP+JSON", "0.3") in bindings
    assert ("HTTP+JSON", "1.0") in bindings

    body = {
        "message": {
            "role": "ROLE_USER",
            "content": [
                {"text": "When can we do blade repairs at Clyde this week?"}
            ],
            "messageId": str(uuid.uuid4()),
        }
    }
    response = requests.post(
        f"{RPC_URL}/v1/message:send", json=body, timeout=300
    )
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    answer = " ".join(
        part.get("text", "")
        for artifact in task.get("artifacts", [])
        for part in artifact["parts"]
    )
    # Snapshot: best 6-hour blade-repair window at Clyde is Thu 22 Oct, 92.2%.
    assert "92.2" in answer, answer
    assert "decision support" in answer.lower()


def test_agent_engine_adapter_creates_session(server: None) -> None:
    # Queries through the adapter need Vertex AI credentials (the Agent Engine
    # runtime forces GOOGLE_GENAI_USE_VERTEXAI=1); creating a session does not.
    response = requests.post(
        f"{BASE_URL}/api/reasoning_engine",
        json={
            "class_method": "async_create_session",
            "input": {"user_id": "u"},
        },
        timeout=60,
    )
    assert response.status_code == 200
    assert response.json()["output"]["id"]
