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

"""Unit tests for `app/workers/environment.py`, `app/workers/providers/static.py`, and `terraform/`."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.workers.environment import LocalEnvironment, SandboxEnvironment
from app.workers.providers.base import ComputerUnavailableError
from app.workers.providers.static import StaticProvider
from tests.fakes import wait_until


def _sandbox(
    handler: Callable[..., httpx.Response],
) -> SandboxEnvironment:
    return SandboxEnvironment(
        name="sb",
        url="https://sandbox.test",
        access_token="at",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_sandbox_exec_separates_transport_errors_from_command_failures() -> (
    None
):
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"stdout": "hi", "stderr": "", "exit_code": 3}
        )

    result = await _sandbox(ok).exec("false", env={"A": "1"})
    assert result.exit_code == 3 and result.error is None and not result.ok
    body = json.loads(seen[0].content)
    assert body["env"] == {"A": "1"} and body["command"].endswith("false")
    assert body["timeout"] == 60.0
    assert seen[0].headers["Authorization"] == "Bearer at"
    assert not any(h.lower().startswith("x-sandbox-") for h in seen[0].headers)
    # Commands find the image's virtualenv and npm-installed CLIs first.
    assert body["command"].startswith(
        'export PATH="$HOME/.local/bin:/opt/venv/bin:$PATH"; '
    )

    for response, fragment in (
        (httpx.Response(502, text="no healthy upstream"), "HTTP 502"),
        (httpx.Response(200, text="<html>"), "unparsable"),
        (httpx.Response(200, json=["x"]), "expected an object"),
        (httpx.Response(200, json={"exit_code": "?"}), "non-numeric"),
        (httpx.Response(200, json={"timed_out": True}), "timed out"),
    ):
        result = await _sandbox(lambda _r, resp=response: resp).exec("true")
        assert result.error and fragment in result.error
        assert "no healthy upstream" not in result.error
        assert "sandbox.test" not in result.error

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    result = await _sandbox(boom).exec("true")
    assert result.error and "unreachable" in result.error
    assert "sandbox.test" not in result.error


@pytest.mark.asyncio
async def test_sandbox_exec_retries_only_requests_that_never_connected() -> (
    None
):
    attempts: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append("connect")
        if len(attempts) < 3:
            raise httpx.ConnectError("nodename nor servname provided")
        return httpx.Response(200, json={"exit_code": 0})

    assert (await _sandbox(flaky).exec("true")).ok and len(attempts) == 3

    attempts.clear()

    def slow(request: httpx.Request) -> httpx.Response:
        attempts.append("read")
        raise httpx.ReadTimeout("timed out")

    result = await _sandbox(slow).exec("true")
    assert result.error and attempts == ["read"], (
        "the command may already be running"
    )


@pytest.mark.asyncio
async def test_file_requests_retry_a_connection_that_never_opened() -> None:
    attempts: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) < 3:
            raise httpx.ConnectTimeout("")
        return httpx.Response(200, json={"content": "{}", "truncated": False})

    assert (await _sandbox(flaky).read_file("notes.md")).content == "{}"
    assert attempts == ["/files/read"] * 3

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    with pytest.raises(
        RuntimeError, match=r"/files/read: ConnectTimeout\)"
    ) as err:
        await _sandbox(down).read_file("notes.md")
    assert "sandbox.test" not in str(err.value), "users see this message"

    exec_attempts: list[str] = []

    def exec_flaky(request: httpx.Request) -> httpx.Response:
        exec_attempts.append("x")
        if len(exec_attempts) < 2:
            raise httpx.ConnectTimeout("")
        return httpx.Response(200, json={"exit_code": 0})

    assert (await _sandbox(exec_flaky).exec("true")).ok and len(
        exec_attempts
    ) == 2


@pytest.mark.asyncio
async def test_local_harnesses_never_see_server_only_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_WORKSPACE_ACCESS_TOKEN", "token.server-only")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "1//server-only")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "server-only")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "server-only")
    monkeypatch.setenv("GH_TOKEN", "gh-server-only")
    monkeypatch.setenv("GITHUB_ACCESS_TOKEN", "ghu-server-only")
    env = LocalEnvironment(tmp_path)

    res = await env.exec(
        "env",
        env={
            "GOOGLE_WORKSPACE_ACCESS_TOKEN": "explicitly-passed",
            "EXTRA": "1",
        },
    )

    assert res.exit_code == 0
    assert "server-only" not in res.stdout
    assert "GH_TOKEN=" not in res.stdout
    assert "GITHUB_ACCESS_TOKEN=" not in res.stdout
    assert "GOOGLE_WORKSPACE_ACCESS_TOKEN=explicitly-passed" in res.stdout
    assert "EXTRA=1" in res.stdout


@pytest.mark.asyncio
async def test_a_cancelled_local_command_is_killed_and_reaped(
    tmp_path: Path,
) -> None:
    env = LocalEnvironment(tmp_path)
    pid_file = tmp_path / "pid"
    command = asyncio.create_task(
        env.exec(f"echo $$ > {pid_file}; exec sleep 30", timeout_s=60)
    )
    await wait_until(
        lambda: pid_file.exists() and bool(pid_file.read_text().strip()),
        timeout_s=10,
        interval_s=0.02,
        message="the command to start",
    )
    pid = int(pid_file.read_text())

    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_static_provider_uses_the_configured_computer_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPUTER_URL", raising=False)
    monkeypatch.delenv("COMPUTER_URL_TEMPLATE", raising=False)
    with pytest.raises(ComputerUnavailableError, match="COMPUTER_URL"):
        await StaticProvider().ensure("alice@example.com")

    monkeypatch.setenv("COMPUTER_URL", "https://computer.example.com/")
    tokens: list[str] = []

    class FakeIdTokens:
        def __init__(self, audience: str) -> None:
            tokens.append(audience)

        def __call__(self) -> str:
            return "iam-id-token"

    monkeypatch.setattr(
        "app.workers.providers.static.IdTokenSource", FakeIdTokens
    )
    env = await StaticProvider().ensure("alice@example.com")
    assert isinstance(env, SandboxEnvironment)
    assert env.base_url == "https://computer.example.com"
    assert env.headers()["Authorization"] == "Bearer iam-id-token"
    assert tokens == ["adk-voice-agent-computer"]


@pytest.mark.asyncio
async def test_sandbox_environment_pools_http_client_and_refreshes_token_off_loop() -> (
    None
):
    import threading

    loop_thread = threading.get_ident()
    token_threads: list[int] = []

    def token_source() -> str:
        token_threads.append(threading.get_ident())
        return "fresh-id-token"

    env = SandboxEnvironment(
        url="http://sandbox.test:8080",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200, json={"exit_code": 0, "stdout": "ok"}
            )
        ),
        token_source=token_source,
    )
    c1 = env._client()
    assert (await env.exec("echo hi")).ok
    c2 = env._client()
    assert (await env.exec("echo hi")).ok
    assert c1 is c2
    assert not c1.is_closed
    assert len(token_threads) == 2
    assert all(tid != loop_thread for tid in token_threads)


def test_terraform_provisions_gce_computers_not_a_cloud_run_worker() -> None:
    tf_dir = Path(__file__).resolve().parents[2] / "terraform"
    tf = {p.name: p.read_text() for p in tf_dir.glob("*.tf")}
    every = "\n".join(tf.values())
    cloud_init = (tf_dir / "computer-cloud-init.yaml.tftpl").read_text()

    assert '"worker"' not in tf["cloud_run.tf"]
    assert "google_storage_bucket" not in every
    assert "CLOUD_COMPUTER_URL" not in every
    assert 'egress = "PRIVATE_RANGES_ONLY"' in tf["cloud_run.tf"]
    computer = tf["computer.tf"]
    assert 'resource "google_compute_instance_template" "computer"' in computer
    assert (
        "name_prefix" in computer and "create_before_destroy = true" in computer
    )
    assert "cos-cloud" in computer and "access_config" not in computer.replace(
        "No access_config", ""
    )
    assert "gce-container-declaration" not in every
    assert "/dev/disk/by-id/google-${disk_device_name}" in cloud_init
    assert "-v ${home_mount}:/home/agent" in cloud_init
    assert "computer-env" in cloud_init
    assert (
        'resource "google_compute_resource_policy" "home_snapshots"' in computer
    )
    assert "35.235.240.0/20" in tf["network.tf"]
    assert 'resource "google_compute_router_nat"' in tf["network.tf"]
    for name in (
        "computer_instance_template",
        "computer_zone",
        "computer_service_account",
        "computer_snapshot_policy",
        "computer_mcp_audience",
        "app_url",
    ):
        assert f'output "{name}"' in tf["outputs.tf"]
