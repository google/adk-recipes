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

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import requests
from a2a.client import ClientConfig, create_client
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    TaskState,
)
from requests.exceptions import RequestException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "http://127.0.0.1:8000"
RUN_SSE_URL = BASE_URL + "/run_sse"
A2A_RPC_URL = BASE_URL + "/a2a/app/"
AGENT_CARD_URL = A2A_RPC_URL + ".well-known/agent-card.json"

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
        "0.0.0.0",
        "--port",
        "8000",
    ]
    env = os.environ.copy()
    env["INTEGRATION_TEST"] = "TRUE"
    # Advertise a loopback URL so the A2A client can reach the card's transport.
    env["APP_URL"] = BASE_URL
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
    """Wait for the server to be ready (agent card requires the lifespan to run)."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(AGENT_CARD_URL, timeout=10)
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


def test_adk_run_sse(server_fixture: subprocess.Popen[str]) -> None:
    """Test the native ADK route (/run_sse) end to end."""
    logger.info("Starting ADK /run_sse test")
    user_id = f"user_{uuid.uuid4()}"
    session_data = {"state": {"preferred_language": "English", "visit_count": 1}}

    session_response = requests.post(
        f"{BASE_URL}/apps/app/users/{user_id}/sessions",
        headers=HEADERS,
        json=session_data,
        timeout=60,
    )
    assert session_response.status_code == 200
    session_id = session_response.json()["id"]

    data = {
        "app_name": "app",
        "user_id": user_id,
        "session_id": session_id,
        "new_message": {"role": "user", "parts": [{"text": "Hi!"}]},
        "streaming": True,
    }
    response = requests.post(
        RUN_SSE_URL, headers=HEADERS, json=data, stream=True, timeout=60
    )
    assert response.status_code == 200

    events = []
    for line in response.iter_lines():
        if line:
            line_str = line.decode("utf-8")
            if line_str.startswith("data: "):
                events.append(json.loads(line_str[6:]))

    assert events, "No events received from stream"
    has_text_content = any(
        (content := event.get("content"))
        and content.get("parts")
        and any(part.get("text") for part in content["parts"])
        for event in events
    )
    assert has_text_content, "Expected at least one event with text content"


def test_a2a_chat_stream(server_fixture: subprocess.Popen[str]) -> None:
    """Test the A2A route using the JSON-RPC streaming protocol."""
    logger.info("Starting A2A chat stream test")

    async def _stream() -> list[StreamResponse]:
        config = ClientConfig(
            streaming=True,
            httpx_client=httpx.AsyncClient(timeout=60.0),
        )
        client = await create_client(A2A_RPC_URL.rstrip("/"), config)
        message = Message(
            message_id=f"msg-user-{uuid.uuid4()}",
            role=Role.ROLE_USER,
            parts=[Part(text="Hi!")],
        )
        return [
            chunk
            async for chunk in client.send_message(SendMessageRequest(message=message))
        ]

    responses = asyncio.run(_stream())
    assert responses, "No responses received from stream"

    def _is_completed(chunk: StreamResponse) -> bool:
        if chunk.HasField("status_update"):
            return chunk.status_update.status.state == TaskState.TASK_STATE_COMPLETED
        if chunk.HasField("task"):
            return chunk.task.status.state == TaskState.TASK_STATE_COMPLETED
        return False

    assert any(_is_completed(chunk) for chunk in responses), (
        "No completed task received from stream"
    )


def test_agent_card(server_fixture: subprocess.Popen[str]) -> None:
    """Test that the A2A agent card is served at the well-known URI."""
    for _ in range(3):
        response = requests.get(AGENT_CARD_URL, timeout=10)
        assert response.status_code == 200, (
            f"A2A endpoint returned {response.status_code}"
        )

        served_agent_card = response.json()
        # supportedInterfaces is the A2A 1.0 marker (replaces url/preferredTransport).
        for field in (
            "name",
            "description",
            "skills",
            "capabilities",
            "version",
            "supportedInterfaces",
        ):
            assert field in served_agent_card, f"Missing field in agent card: {field}"

        interfaces = served_agent_card.get("supportedInterfaces", [])
        v03_interfaces = [
            i
            for i in interfaces
            if i.get("protocolBinding") == "JSONRPC"
            and i.get("protocolVersion") == "0.3"
        ]
        assert len(v03_interfaces) == 1, (
            f"Expected exactly one v0.3 interface, found {len(v03_interfaces)}"
        )
