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

"""HTTP smoke tier — spawn the real FastAPI app on an ephemeral port.

Drives ``/a2a`` (JSON-RPC over SSE) against real Gemini to catch wiring
bugs that only surface in the FastAPI → A2A executor → ADK Runner chain,
not visible from the in-process Phase 1 backend tier. Gated behind
``RUN_SMOKE_LLM=1`` because the agent loop here also hits Vertex.

Subprocess uvicorn on an ephemeral port so we never collide with a
developer's running server (typically :8000 / :8001) and never have to
guess a free port. ``USE_IN_MEMORY_SESSION`` and
``USE_IN_MEMORY_TASK_STORE`` keep the suite from writing SQLite files
under the repo or hitting Vertex Memory Bank.
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("RUN_SMOKE_LLM"):
        return
    skip = pytest.mark.skip(
        reason="set RUN_SMOKE_LLM=1 to enable HTTP smoke tests (drives real Gemini)"
    )
    for item in items:
        if "tests/smoke/http" in str(item.fspath):
            item.add_marker(skip)


def _pick_free_port() -> int:
    """OS-assigned ephemeral port. Race-free between bind+close and the
    uvicorn re-bind in practice; uvicorn will fail fast on collision."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _log_pipe(pipe: Any, log_func: Any) -> None:
    for line in iter(pipe.readline, ""):
        log_func("uvicorn[smoke]: %s", line.rstrip())


@pytest.fixture(scope="session")
def smoke_http_port() -> int:
    return _pick_free_port()


@pytest.fixture(scope="session")
def smoke_http_server(
    smoke_http_port: int, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """Spawn ``app.fast_api_app:app`` via subprocess; yield base URL."""

    env = os.environ.copy()
    env["USE_IN_MEMORY_SESSION"] = "true"
    env["USE_IN_MEMORY_TASK_STORE"] = "true"
    env["LHA_LOCAL_ROOT"] = str(tmp_path_factory.mktemp("smoke-http-local"))
    env["INTEGRATION_TEST"] = "TRUE"
    env.setdefault("LOG_LEVEL", "warning")

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.fast_api_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(smoke_http_port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    threading.Thread(
        target=_log_pipe, args=(process.stdout, logger.info), daemon=True
    ).start()
    threading.Thread(
        target=_log_pipe, args=(process.stderr, logger.warning), daemon=True
    ).start()

    base_url = f"http://127.0.0.1:{smoke_http_port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"uvicorn exited early with code {process.returncode} — "
                "check warning-level logs above"
            )
        try:
            r = requests.get(f"{base_url}/docs", timeout=5)
            if r.status_code == 200:
                break
        except RequestException:
            pass
        time.sleep(1)
    else:
        process.terminate()
        process.wait(timeout=10)
        raise RuntimeError(
            f"uvicorn did not become ready within 120s on {base_url}"
        )

    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="session")
def smoke_http_user_id() -> str:
    """Stable per-session ``lha_user_id`` so the second request in a
    test sees the warm ``_env_cache`` entry the first request populated."""
    return f"smoke-http-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def unique_marker() -> str:
    """Fresh prompt-shaping marker per test — keeps test prompts
    distinguishable in logs without colliding."""
    return uuid.uuid4().hex[:8]
