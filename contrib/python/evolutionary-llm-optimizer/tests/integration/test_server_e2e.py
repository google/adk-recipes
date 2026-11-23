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

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from requests.exceptions import RequestException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _free_port() -> int:
    # Other local servers often hold 8000/8080; let the OS pick a free port.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"
STREAM_URL = BASE_URL + "/run_sse"
A2A_URL = BASE_URL + "/a2a/app"
AGENT_CARD_URL = A2A_URL + "/.well-known/agent-card.json"
REASONING_ENGINE_URL = BASE_URL + "/api/reasoning_engine"
# A run with MAX_EVALUATIONS=2 makes three or four model calls.
RUN_TIMEOUT_SECONDS = 900

HEADERS = {"Content-Type": "application/json"}


def log_output(pipe: Any, log_func: Any) -> None:
    """Log the output from the given pipe."""
    for line in iter(pipe.readline, ""):
        log_func(line.strip())


def start_server() -> subprocess.Popen[str]:
    """Start the FastAPI server using subprocess and log its output."""
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.fast_api_app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(PORT),
    ]
    env = os.environ.copy()
    env["INTEGRATION_TEST"] = "TRUE"
    env["APP_URL"] = BASE_URL
    # Every message runs the full optimization loop; keep it short here.
    env["MAX_EVALUATIONS"] = "2"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    # Start threads to log stdout and stderr in real-time
    threading.Thread(
        target=log_output, args=(process.stdout, logger.info), daemon=True
    ).start()
    threading.Thread(
        target=log_output, args=(process.stderr, logger.error), daemon=True
    ).start()

    return process


def wait_for_server(timeout: int = 90, interval: int = 1) -> bool:
    """Wait for the server to be ready."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{BASE_URL}/docs", timeout=10)
            if response.status_code == 200:
                logger.info("Server is ready")
                return True
        except RequestException:
            pass
        time.sleep(interval)
    logger.error(f"Server did not become ready within {timeout} seconds")
    return False


@pytest.fixture(scope="session")
def server_fixture(request: Any) -> Iterator[subprocess.Popen[str]]:
    """Pytest fixture to start and stop the server for testing."""
    logger.info("Starting server process")
    server_process = start_server()
    if not wait_for_server():
        pytest.fail("Server failed to start")
    logger.info("Server process started")

    def stop_server() -> None:
        logger.info("Stopping server process")
        server_process.terminate()
        server_process.wait()
        logger.info("Server process stopped")

    request.addfinalizer(stop_server)
    yield server_process


def test_chat_stream(server_fixture: subprocess.Popen[str]) -> None:
    """Test the chat stream functionality."""
    logger.info("Starting chat stream test")
    # Create session first
    user_id = "test_user_123"
    session_data = {
        "state": {"preferred_language": "English", "visit_count": 1}
    }

    session_url = f"{BASE_URL}/apps/app/users/{user_id}/sessions"
    session_response = requests.post(
        session_url,
        headers=HEADERS,
        json=session_data,
        timeout=60,
    )
    assert session_response.status_code == 200
    logger.info(f"Session creation response: {session_response.json()}")
    session_id = session_response.json()["id"]

    # Then send chat message
    data = {
        "app_name": "app",
        "user_id": user_id,
        "session_id": session_id,
        "new_message": {
            "role": "user",
            "parts": [{"text": "Cut node cost for my clusters."}],
        },
        "streaming": True,
    }
    response = requests.post(
        STREAM_URL,
        headers=HEADERS,
        json=data,
        stream=True,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    assert response.status_code == 200

    # Parse SSE events from response
    events = []
    for line in response.iter_lines():
        if line:
            # SSE format is "data: {json}"
            line_str = line.decode("utf-8")
            if line_str.startswith("data: "):
                event_json = line_str[6:]  # Remove "data: " prefix
                event = json.loads(event_json)
                events.append(event)

    assert events, "No events received from stream"
    # Check for valid content in the response
    has_text_content = False
    for event in events:
        content = event.get("content")
        if (
            content is not None
            and content.get("parts")
            and any(part.get("text") for part in content["parts"])
        ):
            has_text_content = True
            break

    assert has_text_content, "Expected at least one event with text content"


def test_chat_stream_error_handling(
    server_fixture: subprocess.Popen[str],
) -> None:
    """Test the chat stream error handling."""
    logger.info("Starting chat stream error handling test")
    data = {
        "input": {
            "messages": [{"type": "invalid_type", "content": "Cause an error"}]
        }
    }
    response = requests.post(
        STREAM_URL, headers=HEADERS, json=data, stream=True, timeout=10
    )

    assert response.status_code == 422, (
        f"Expected status code 422, got {response.status_code}"
    )
    logger.info("Error handling test completed successfully")


def test_a2a_agent_card(server_fixture: subprocess.Popen[str]) -> None:
    """The agent card has the 0.3 JSON-RPC shape Gemini Enterprise registers."""
    response = requests.get(AGENT_CARD_URL, timeout=30)
    assert response.status_code == 200
    card = response.json()
    assert card["name"]
    assert "SQL" in card["description"]
    assert card["skills"]
    interfaces = {
        (i["protocolBinding"], i["protocolVersion"]): i["url"]
        for i in card["supportedInterfaces"]
    }
    # JSON-RPC 0.3 for Gemini Enterprise registration; HTTP+JSON for the
    # Gemini agent (Agent Registry); 1.0 for current A2A clients.
    for binding in ("JSONRPC", "HTTP+JSON"):
        for version in ("0.3", "1.0"):
            assert interfaces.get((binding, version)) == A2A_URL, interfaces


def _texts(value: Any) -> list[str]:
    """Collects every A2A text part in a JSON-RPC result."""
    if isinstance(value, dict):
        found = [value["text"]] if isinstance(value.get("text"), str) else []
        return found + [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def test_a2a_message_send(server_fixture: subprocess.Popen[str]) -> None:
    """A v0.3 A2A message/send runs the workflow and returns the report."""
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "kind": "message",
                "messageId": "e2e-1",
                "parts": [
                    {
                        "kind": "text",
                        "text": "Cut the bytes scanned by the station"
                        " demand query.",
                    }
                ],
            }
        },
    }
    response = requests.post(
        A2A_URL, json=payload, headers=HEADERS, timeout=RUN_TIMEOUT_SECONDS
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    assert body["result"]["status"]["state"] == "completed"
    texts = _texts(body["result"])
    assert any("## SQL cost report: " in t for t in texts)
    assert any("### Summary" in t for t in texts)


def test_a2a_rest_card(server_fixture: subprocess.Popen[str]) -> None:
    """GET /v1/card on the HTTP+JSON binding, as Agent Registry calls it."""
    response = requests.get(f"{A2A_URL}/v1/card", timeout=30)
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["name"] == "evolutionary_llm_optimizer"
    assert card["skills"]


def test_a2a_rest_message_send(server_fixture: subprocess.Popen[str]) -> None:
    """POST /v1/message:send in the shape the Gemini agent sends."""
    body = {
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "content": [{"text": "Optimize the monthly trend query."}],
        }
    }
    response = requests.post(
        f"{A2A_URL}/v1/message:send", json=body, timeout=RUN_TIMEOUT_SECONDS
    )
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    texts = _texts(task)
    assert any(
        "## SQL cost report: Monthly hires per station" in t for t in texts
    )
    assert any("### Summary" in t for t in texts)


def test_reasoning_engine_adapter(
    server_fixture: subprocess.Popen[str],
) -> None:
    """The Agent Engine :query route reaches the AdkApp runtime."""
    response = requests.post(
        REASONING_ENGINE_URL,
        json={
            "class_method": "async_create_session",
            "input": {"user_id": "e2e-user"},
        },
        headers=HEADERS,
        timeout=60,
    )
    assert response.status_code == 200
    assert response.json()["output"]["id"]
