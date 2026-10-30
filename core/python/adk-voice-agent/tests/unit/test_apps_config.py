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

"""Tests that `config/integrations.yaml`, `.mcp.json`, `config/policy.yaml`, and `Dockerfile` agree."""

from __future__ import annotations

import fnmatch
import json
from pathlib import PurePosixPath

import pytest
import yaml

from app.api.mcp_gateway import MCP_JSON_PATH
from app.auth.env_store import PROJECT_ROOT
from app.auth.providers import all_providers, load_integrations_config

S = "https://www.googleapis.com/auth/"
GOOGLE_SERVER_SCOPES = {
    f"{S}calendar.events",
    f"{S}calendar.readonly",
    f"{S}gmail.modify",
    f"{S}drive.readonly",
    f"{S}drive.file",
    f"{S}documents",
    f"{S}spreadsheets",
    f"{S}presentations",
}


def _mcp_servers() -> dict[str, dict]:
    return json.loads(MCP_JSON_PATH.read_text())["mcpServers"]


def test_every_mcp_server_belongs_to_exactly_one_app() -> None:
    listed = [s for app in all_providers().values() for s in app.mcp_servers]
    assert len(listed) == len(set(listed)), f"listed twice: {listed}"
    assert set(listed) == set(_mcp_servers())


def test_an_apps_scopes_are_its_own_plus_its_servers() -> None:
    raw = load_integrations_config()["apps"]
    for name, app in all_providers().items():
        own = raw[name].get("scopes") or []
        servers = {s for scopes in app.mcp_servers.values() for s in scopes}
        assert set(app.scopes) == set(own) | servers, name
        assert len(app.scopes) == len(set(app.scopes)), name


def test_google_asks_for_exactly_what_its_servers_need() -> None:
    google = all_providers()["google_workspace"]
    servers = {s for scopes in google.mcp_servers.values() for s in scopes}
    assert servers == GOOGLE_SERVER_SCOPES
    assert set(google.scopes) == GOOGLE_SERVER_SCOPES | {f"{S}userinfo.email"}


def test_there_is_no_slack() -> None:
    assert list(all_providers()) == ["google_workspace", "github"]
    assert not [s for s in _mcp_servers() if "slack" in s]


def test_every_mcp_gate_names_a_server_in_mcp_json() -> None:
    policy = yaml.safe_load(
        (PROJECT_ROOT / "config" / "policy.yaml").read_text()
    )
    gates = policy["mcp_gates"]["ask"]
    assert gates
    servers = set(_mcp_servers())
    unknown = [g for g in gates if g.split(".", 1)[0] not in servers]
    assert not unknown, (
        f"gates for servers .mcp.json doesn't declare: {unknown}"
    )


def _runtime_stage() -> list[str]:
    lines = (PROJECT_ROOT / "Dockerfile").read_text().splitlines()
    start = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    return [
        line.strip()
        for line in lines[start:]
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _copied_to(stage: list[str], source: str) -> PurePosixPath | None:
    workdir = PurePosixPath("/")
    for line in stage:
        parts = line.split()
        if parts[0] == "WORKDIR":
            workdir = workdir / parts[1]
        elif (
            parts[0] == "COPY"
            and not parts[1].startswith("--")
            and source in parts[1:-1]
        ):
            dest = parts[-1]
            target = workdir / dest
            if dest.endswith("/") or len(parts) > 3:
                target = target / PurePosixPath(source).name
            return PurePosixPath(*target.parts)
    return None


def test_the_image_ships_mcp_json_where_the_gateway_reads_it() -> None:
    stage = _runtime_stage()
    app_dir = _copied_to(stage, "app")
    assert app_dir is not None, "the runtime stage doesn't copy app/"
    expected = (
        app_dir.parent / MCP_JSON_PATH.relative_to(PROJECT_ROOT).as_posix()
    )
    assert expected == PurePosixPath("/code/.mcp.json")
    assert _copied_to(stage, ".mcp.json") == expected


def test_the_image_ships_the_builtin_mcp_servers_next_to_app() -> None:
    stage = _runtime_stage()
    assert _copied_to(stage, "workspace_mcp") == PurePosixPath(
        "/code/workspace_mcp"
    )
    copy = next(i for i, line in enumerate(stage) if "workspace_mcp" in line)
    install = max(
        i for i, line in enumerate(stage) if line.startswith("RUN uv sync")
    )
    assert copy < install


@pytest.mark.parametrize("source", [".mcp.json", "workspace_mcp"])
def test_dockerignore_keeps_the_mcp_servers_in_the_build_context(
    source: str,
) -> None:
    excluded = False
    for raw in (PROJECT_ROOT / ".dockerignore").read_text().splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        pattern = pattern.removeprefix("!").removeprefix("**/").rstrip("/")
        if fnmatch.fnmatch(source, pattern):
            excluded = not negated
    assert not excluded
