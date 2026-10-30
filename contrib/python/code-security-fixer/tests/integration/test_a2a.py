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
"""A2A round-trips used by Gemini Enterprise and the Gemini agent.

Needs Gemini credentials. Starts the FastAPI server and checks:

- the agent card has the A2A 0.3 shape that Gemini Enterprise registration
  validates, and advertises JSON-RPC and HTTP+JSON interfaces;
- JSON-RPC 0.3 `message/send` returns the workflow's report;
- the HTTP+JSON binding the Gemini agent uses through Agent Registry:
  `GET /v1/card` and `POST /v1/message:send` with `role: ROLE_USER`,
  `content[].text` and a unique `messageId`;
- the Agent Engine adapter creates a session.

Excluded from CI; run with:

    uv run pytest tests/integration/test_a2a.py
"""

import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

import pytest
import requests


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"
RPC_URL = f"{BASE_URL}/a2a/app"
CARD_URL = f"{RPC_URL}/.well-known/agent-card.json"
# The workflow runs Bandit and the tests several times.
RUN_TIMEOUT_SECONDS = 900


@pytest.fixture(scope="module")
def server() -> Iterator[None]:
    env = {**os.environ, "INTEGRATION_TEST": "TRUE", "APP_URL": BASE_URL}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.fast_api_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if requests.get(CARD_URL, timeout=5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            pytest.fail("server did not start")
        yield
    finally:
        proc.terminate()
        proc.wait()


def test_agent_card_has_the_a2a_0_3_shape(server: None) -> None:
    card = requests.get(CARD_URL, timeout=10).json()
    assert card["name"] == "code_security_fixer"
    assert card["description"]
    assert card["url"] == RPC_URL
    assert card["protocolVersion"] == "0.3"
    # 0.3 clients see both bindings at the same URL: HTTP+JSON (used by the
    # Gemini agent through Agent Registry) and JSON-RPC.
    transports = {card["preferredTransport"]} | {
        i["transport"] for i in card.get("additionalInterfaces") or []
    }
    assert transports == {"HTTP+JSON", "JSONRPC"}
    assert card["skills"]
    assert any(
        i["protocolBinding"] == "JSONRPC" and i["protocolVersion"] == "0.3"
        for i in card["supportedInterfaces"]
    )


def test_message_send_returns_the_report(server: None) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "parts": [
                    {
                        "kind": "text",
                        "text": "Fix the security findings and give me a patch.",
                    }
                ],
            }
        },
    }
    response = requests.post(
        RPC_URL,
        json=payload,
        headers={"A2A-Version": "0.3"},
        timeout=RUN_TIMEOUT_SECONDS,
    )
    body = response.json()
    assert "error" not in body, body.get("error")
    result = body["result"]
    assert result["status"]["state"] == "completed"
    text = "\n".join(
        part.get("text", "")
        for artifact in result.get("artifacts") or []
        for part in artifact["parts"]
    )
    assert "Security fix report" in text
    assert "| Tests passing | 3/6 | 6/6 |" in text
    assert "```diff" in text
    assert "Nothing was committed" in text


def test_http_json_card_and_message_send(server: None) -> None:
    card = requests.get(f"{RPC_URL}/v1/card", timeout=30)
    assert card.status_code == 200
    card_json = card.json()
    assert card_json["name"] == "code_security_fixer"
    assert card_json["url"] == RPC_URL

    well_known = requests.get(CARD_URL, timeout=30).json()
    bindings = {
        (i["protocolBinding"], i["protocolVersion"])
        for i in well_known["supportedInterfaces"]
    }
    assert ("HTTP+JSON", "0.3") in bindings
    assert ("HTTP+JSON", "1.0") in bindings

    body = {
        "message": {
            "role": "ROLE_USER",
            "content": [
                {"text": "Fix the security findings and give me a patch."}
            ],
            "messageId": uuid.uuid4().hex,
        }
    }
    response = requests.post(
        f"{RPC_URL}/v1/message:send", json=body, timeout=RUN_TIMEOUT_SECONDS
    )
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    text = "\n".join(
        part.get("text", "")
        for artifact in task.get("artifacts", [])
        for part in artifact["parts"]
    )
    assert "| Tests passing | 3/6 | 6/6 |" in text
    assert "```diff" in text


def test_agent_engine_adapter_creates_session(server: None) -> None:
    response = requests.post(
        f"{BASE_URL}/api/reasoning_engine",
        json={
            "class_method": "async_create_session",
            "input": {"user_id": "gemini-agent"},
        },
        timeout=60,
    )
    assert response.status_code == 200
    assert response.json()["output"]["user_id"] == "gemini-agent"
