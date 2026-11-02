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

"""Hermetic end-to-end boot test for the server process and `/api/v1` control plane."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT_S = 60.0
# Variables the server process may inherit; everything else (tokens, DB URLs) is dropped.
PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "VIRTUAL_ENV")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str) -> tuple[int, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read())


def _isolated_env(root: Path) -> dict[str, str]:
    (root / "home").mkdir()
    (root / ".env").write_text(
        (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    env = {k: os.environ[k] for k in PASSTHROUGH if k in os.environ}
    env.update(
        {
            "HOME": str(root / "home"),
            "VOICE_AGENT_LOAD_DOTENV": "1",
            "ADK_DISABLE_LOAD_DOTENV": "1",
            "VOICE_AGENT_SKIP_STARTUP_WARMUP": "1",
            "VOICE_AGENT_STATE_DIR": str(root / "state"),
            "VOICE_AGENT_DOTENV_PATH": str(root / ".env"),
            "VOICE_AGENT_WORKSPACE_ROOT": str(root / "workspaces"),
            "VOICE_AGENT_WORKSPACES_LOCAL_YAML": str(
                root / "workspaces.local.yaml"
            ),
            "THREAD_DB_URL": f"sqlite+aiosqlite:///{root / 'threads.db'}",
            "COMPUTER_PROVIDER": "host",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = root / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.fast_api_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=_isolated_env(root),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            if process.poll() is not None:
                pytest.fail(f"Server exited early:\n{log_path.read_text()}")
            try:
                if _get_json(f"{base_url}/api/v1/state")[0] == 200:
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.monotonic() > deadline:
                pytest.fail(
                    f"Server not ready in {STARTUP_TIMEOUT_S}s:\n{log_path.read_text()}"
                )
            time.sleep(0.25)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_server_serves_the_agent_and_the_control_plane(server: str) -> None:
    status, apps = _get_json(f"{server}/list-apps")
    assert status == 200 and "app" in apps

    status, state = _get_json(f"{server}/api/v1/state")
    assert status == 200
    assert state["threads"] == [], "a fresh, isolated thread store"

    status, config = _get_json(f"{server}/api/v1/config")
    assert status == 200
    assert sum(h["is_default"] for h in config["harnesses"]) == 1
