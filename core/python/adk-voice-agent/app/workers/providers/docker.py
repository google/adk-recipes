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

"""`COMPUTER_PROVIDER=docker`: the computer image in a local container per user."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.auth.sandbox_env import sandbox_location
from app.workers.environment import (
    COMPUTER_DAEMON_PORT,
    COMPUTER_HOME,
    Environment,
    SandboxEnvironment,
)
from app.workers.providers.base import (
    ComputerState,
    ComputerStatus,
    ComputerUnavailableError,
)
from app.workers.providers.common import (
    COMPUTER_LABEL,
    OWNER_LABEL,
    OwnerLocks,
    app_port,
    owner_id,
    wait_for_daemon,
)

logger = logging.getLogger(__name__)

DEFAULT_COMPUTER_IMAGE = "adk-voice-agent-computer:dev"
COMPUTER_NETWORK = "adk-voice-agent-computers"
_CONTAINER_ADC = "/etc/computer/adc.json"

CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


async def run_command(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else 1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


_STATES = {
    "created": ComputerState.STOPPED,
    "running": ComputerState.RUNNING,
    "restarting": ComputerState.STARTING,
    "paused": ComputerState.STOPPED,
    "removing": ComputerState.STOPPING,
    "exited": ComputerState.STOPPED,
    "dead": ComputerState.ERROR,
}


def computer_name(owner: str) -> str:
    return f"adk-computer-{owner}"


class DockerProvider:
    name = "docker"

    def __init__(
        self,
        *,
        runner: CommandRunner = run_command,
        transport: Any | None = None,
        adc_file: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        from app.auth.adc import adc_path

        self._run = runner
        self._transport = transport
        self._adc_file = adc_file if adc_file is not None else adc_path()
        self._sleep = sleep
        self._locks = OwnerLocks()
        self._notices: dict[str, str] = {}

    @property
    def image(self) -> str:
        return os.getenv("COMPUTER_IMAGE") or DEFAULT_COMPUTER_IMAGE

    async def _docker(self, *args: str, check: bool = True) -> tuple[int, str]:
        code, out, err = await self._run(["docker", *args])
        if check and code != 0:
            raise ComputerUnavailableError(
                f"docker {args[0]} failed: {(err or out).strip()[:300]}"
            )
        return code, out

    async def _inspect(self, owner: str) -> dict[str, Any] | None:
        code, out = await self._docker(
            "container", "inspect", computer_name(owner), check=False
        )
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data[0] if isinstance(data, list) and data else None

    async def _image_id(self) -> str | None:
        code, out = await self._docker(
            "image", "inspect", "--format", "{{.Id}}", self.image, check=False
        )
        if code != 0:
            return None
        return out.strip() or None

    async def _ensure_network(self) -> None:
        code, _ = await self._docker(
            "network", "inspect", COMPUTER_NETWORK, check=False
        )
        if code != 0:
            await self._docker(
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                COMPUTER_NETWORK,
            )

    def _run_args(self, owner: str) -> list[str]:
        name = computer_name(owner)
        args = [
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{COMPUTER_LABEL}=true",
            "--label",
            f"{OWNER_LABEL}={owner}",
            "--restart",
            "unless-stopped",
            "--volume",
            f"{name}:{COMPUTER_HOME}",
            "--publish",
            f"127.0.0.1::{COMPUTER_DAEMON_PORT}",
            "--network",
            COMPUTER_NETWORK,
            "--add-host",
            "host.docker.internal:host-gateway",
            "--env",
            f"COMPUTER_MCP_UPSTREAM=http://host.docker.internal:{app_port()}",
            "--env",
            f"GOOGLE_CLOUD_LOCATION={sandbox_location()}",
        ]
        for key in (
            "GOOGLE_CLOUD_PROJECT",
            "DOTFILES_REPO",
        ):
            if value := (os.getenv(key) or "").strip():
                args += ["--env", f"{key}={value}"]
        if self._adc_file.is_file():
            args += [
                "--volume",
                f"{self._adc_file}:{_CONTAINER_ADC}:ro",
                "--env",
                f"GOOGLE_APPLICATION_CREDENTIALS={_CONTAINER_ADC}",
            ]
        return [*args, self.image]

    async def _environment(self, owner: str) -> SandboxEnvironment:
        _, out = await self._docker(
            "port", computer_name(owner), f"{COMPUTER_DAEMON_PORT}/tcp"
        )
        # One line per binding, e.g. "127.0.0.1:55001".
        first = out.strip().splitlines()[0] if out.strip() else ""
        port = first.rsplit(":", 1)[-1]
        if not port.isdigit():
            raise ComputerUnavailableError(
                f"{computer_name(owner)} publishes no port for the daemon."
            )
        return SandboxEnvironment(
            name=computer_name(owner),
            url=f"http://127.0.0.1:{port}",
            transport=self._transport,
        )

    async def _has_active_runs(self, owner: str, info: dict[str, Any]) -> bool:
        if not (info.get("State") or {}).get("Running"):
            return False
        try:
            env = await self._environment(owner)
            return (await env.activity()).active_runs > 0
        except Exception:
            return False

    async def ensure(self, user_id: str) -> Environment:
        owner = owner_id(user_id)
        async with self._locks.hold(owner):
            info = await self._inspect(owner)
            image_id = await self._image_id()
            if (
                info is not None
                and image_id
                and info.get("Image") != image_id
                and not await self._has_active_runs(owner, info)
            ):
                # A newer image: replace the container, keep the volume.
                await self._docker("rm", "--force", computer_name(owner))
                info = None
                self._notices[owner] = (
                    "Your computer was recreated from a newer image; "
                    "everything in your home directory was kept."
                )
            if info is None:
                await self._ensure_network()
                await self._docker(*self._run_args(owner))
            elif not (info.get("State") or {}).get("Running"):
                await self._docker("start", computer_name(owner))
            env = await self._environment(owner)
            await wait_for_daemon(
                env.base_url,
                headers=env.headers,
                transport=self._transport,
                timeout_s=60.0,
                sleep=self._sleep,
            )
            return env

    async def running_environment(self, user_id: str) -> Environment | None:
        owner = owner_id(user_id)
        info = await self._inspect(owner)
        if not info or not (info.get("State") or {}).get("Running"):
            return None
        return await self._environment(owner)

    def pop_notice(self, user_id: str) -> str | None:
        return self._notices.pop(owner_id(user_id), None)

    async def status(self, user_id: str) -> ComputerStatus:
        info = await self._inspect(owner_id(user_id))
        if info is None:
            return ComputerStatus(ComputerState.MISSING)
        raw = str((info.get("State") or {}).get("Status") or "")
        return ComputerStatus(_STATES.get(raw, ComputerState.ERROR), raw)

    async def stop(self, user_id: str) -> None:
        owner = owner_id(user_id)
        if await self._inspect(owner) is not None:
            await self._docker("stop", computer_name(owner))

    async def known_users(self) -> list[str]:
        _, out = await self._docker(
            "ps",
            "--all",
            "--filter",
            f"label={COMPUTER_LABEL}=true",
            "--format",
            f'{{{{.Label "{OWNER_LABEL}"}}}}',
        )
        return sorted(
            {line.strip() for line in out.splitlines() if line.strip()}
        )
