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

"""Computer providers: selection, host, static, docker (fake CLI), gce (fake Compute API), reaper."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.workers import factory
from app.workers.environment import (
    COMPUTER_MCP_RELAY_URL,
    Activity,
    LocalEnvironment,
    SandboxEnvironment,
)
from app.workers.providers import registry
from app.workers.providers.base import (
    ComputerProvider,
    ComputerState,
    ComputerUnavailableError,
    IdleStoppable,
)
from app.workers.providers.common import owner_id, sandbox_owner
from app.workers.providers.docker import DockerProvider, computer_name
from app.workers.providers.gce import (
    GceProvider,
    disk_name,
    instance_name,
    template_label,
)
from app.workers.providers.host import HostProvider, get_workspace_root
from app.workers.providers.reaper import LiveSessions, reap_once
from app.workers.providers.static import StaticProvider

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
OWNER = sandbox_owner(ALICE)


async def _no_sleep(_: float) -> None:
    return None


def _healthy(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/healthz":
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/activity":
        return httpx.Response(
            200, json={"active_runs": 0, "last_activity": None}
        )
    return httpx.Response(404)


# ---- selection -------------------------------------------------------------------


async def test_the_provider_is_selected_by_name(
    monkeypatch: pytest.MonkeyPatch,
):
    assert isinstance(registry.get_computer_provider(), HostProvider)
    for name, cls in (
        ("docker", DockerProvider),
        ("static", StaticProvider),
        ("gce", GceProvider),
        ("HOST", HostProvider),
    ):
        monkeypatch.setenv("COMPUTER_PROVIDER", name)
        provider = registry.get_computer_provider()
        assert isinstance(provider, cls)
        assert isinstance(provider, ComputerProvider)
        assert registry.get_computer_provider() is provider
    monkeypatch.setenv("COMPUTER_PROVIDER", "nope")
    with pytest.raises(ComputerUnavailableError, match="host"):
        registry.get_computer_provider()


async def test_only_providers_that_bill_while_running_are_reaped():
    assert isinstance(DockerProvider(), IdleStoppable)
    assert isinstance(
        GceProvider(access_token=str, id_token=str), IdleStoppable
    )
    assert not isinstance(HostProvider(), IdleStoppable)
    assert not isinstance(StaticProvider(), IdleStoppable)


async def test_owner_ids_pass_through():
    assert owner_id(ALICE) == OWNER
    assert owner_id(OWNER) == OWNER


async def test_resolve_environment_calls_ensure_once_per_user_until_invalidated(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class Counting(HostProvider):
        name = "counting"

        async def ensure(self, user_id: str) -> Any:
            calls.append(user_id)
            return await super().ensure(user_id)

    monkeypatch.setitem(registry._FACTORIES, "counting", Counting)
    monkeypatch.setenv("COMPUTER_PROVIDER", "counting")
    first = await factory.resolve_environment()
    assert (await factory.resolve_environment()).env is first.env
    assert calls == ["local"]
    factory.invalidate_environment(sandbox_owner("local"))
    await factory.resolve_environment()
    assert calls == ["local", "local"]


async def test_host_gives_each_user_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider = HostProvider()
    env = await provider.ensure(ALICE)
    assert isinstance(env, LocalEnvironment)
    assert Path(env.workspace_root) == get_workspace_root() / OWNER
    assert env.mcp_base_url is None
    assert (await provider.ensure(OWNER)).workspace_root == env.workspace_root
    assert await provider.known_users() == [OWNER]
    assert (await provider.status(ALICE)).state is ComputerState.RUNNING

    monkeypatch.delenv("VOICE_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("VOICE_AGENT_STATE_DIR", str(tmp_path / "state"))
    assert get_workspace_root() == (tmp_path / "state" / "workspaces").resolve()


async def test_host_seeds_the_home_without_overwriting():
    provider = HostProvider()
    home = (await provider.ensure(ALICE)).root
    assert (home / "AGENTS.md").is_file() and (home / "projects").is_dir()
    skill = home / ".agents/skills/connected-apps-mcp/SKILL.md"
    assert skill.is_file()

    (home / "AGENTS.md").write_text("# Mine\n")
    skill.unlink()
    await provider.ensure(ALICE)
    assert (home / "AGENTS.md").read_text() == "# Mine\n"
    assert skill.is_file(), "missing seed files come back"


async def test_static_fills_the_owner_into_the_template(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("COMPUTER_URL_TEMPLATE", "https://computer-{owner}.corp")
    monkeypatch.setenv("COMPUTER_AUTH_AUDIENCE", "my-audience")
    seen: list[str] = []

    class FakeIdTokens:
        def __init__(self, audience: str) -> None:
            seen.append(audience)

        def __call__(self) -> str:
            return "id-token"

    monkeypatch.setattr(
        "app.workers.providers.static.IdTokenSource", FakeIdTokens
    )
    env = await StaticProvider().ensure(ALICE)
    assert isinstance(env, SandboxEnvironment)
    assert env.base_url == f"https://computer-{OWNER}.corp"
    assert env.mcp_base_url == COMPUTER_MCP_RELAY_URL
    assert env.headers() == {"Authorization": "Bearer id-token"}
    assert seen == [f"my-audience/{OWNER}"]


async def test_static_sends_no_token_to_a_loopback_daemon(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("COMPUTER_URL", "http://127.0.0.1:8080")
    provider = StaticProvider(transport=httpx.MockTransport(_healthy))
    env = await provider.ensure(ALICE)
    assert isinstance(env, SandboxEnvironment)
    assert env.headers() == {}
    assert (await provider.status(ALICE)).state is ComputerState.RUNNING
    assert await provider.known_users() == []

    monkeypatch.setenv("VOICE_AGENT_AUTH_MODE", "iap")
    with pytest.raises(ComputerUnavailableError, match="COMPUTER_URL_TEMPLATE"):
        await provider.ensure(ALICE)


class FakeDocker:
    def __init__(self, image_id: str = "sha256:new") -> None:
        self.image_id = image_id
        self.containers: dict[str, dict[str, Any]] = {}
        self.networks: set[str] = set()
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        assert argv[0] == "docker"
        args = argv[1:]
        self.calls.append(args)
        match args:
            case ["network", "inspect", name]:
                if name not in self.networks:
                    return 1, "[]", f"Error: No such network: {name}"
                return 0, json.dumps([{"Name": name}]), ""
            case ["network", "create", *_, name]:
                self.networks.add(name)
                return 0, name + "\n", ""
            case ["container", "inspect", name]:
                if name not in self.containers:
                    return 1, "[]", f"Error: No such container: {name}"
                return 0, json.dumps([self.containers[name]]), ""
            case ["image", "inspect", "--format", "{{.Id}}", _]:
                return 0, self.image_id + "\n", ""
            case ["run", *rest]:
                name = rest[rest.index("--name") + 1]
                self.containers[name] = {
                    "Image": self.image_id,
                    "State": {"Running": True, "Status": "running"},
                    "Labels": dict(
                        rest[i + 1].split("=", 1)
                        for i, a in enumerate(rest)
                        if a == "--label"
                    ),
                }
                return 0, "container-id\n", ""
            case ["start", name] | ["stop", name]:
                running = args[0] == "start"
                self.containers[name]["State"] = {
                    "Running": running,
                    "Status": "running" if running else "exited",
                }
                return 0, name, ""
            case ["rm", "--force", name]:
                self.containers.pop(name, None)
                return 0, name, ""
            case ["port", name, "8080/tcp"]:
                return 0, "127.0.0.1:55001\n[::1]:55001\n", ""
            case ["ps", "--all", "--filter", _, "--format", _]:
                owners = [
                    c["Labels"]["owner"] for c in self.containers.values()
                ]
                return 0, "\n".join(owners) + "\n", ""
        return 1, "", f"unexpected docker {args}"


def _docker(fake: FakeDocker, tmp_path: Path) -> DockerProvider:
    return DockerProvider(
        runner=fake,
        transport=httpx.MockTransport(_healthy),
        adc_file=tmp_path / "missing-adc.json",
        sleep=_no_sleep,
    )


async def test_docker_runs_a_labeled_container_with_the_users_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("APP_URL", "http://localhost:8123")
    monkeypatch.setenv("COMPUTER_IMAGE", "computer:test")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    fake = FakeDocker()
    env = await _docker(fake, tmp_path).ensure(ALICE)

    run = next(c for c in fake.calls if c[0] == "run")
    name = computer_name(OWNER)
    assert run[-1] == "computer:test"
    for flag, value in (
        ("--name", name),
        ("--volume", f"{name}:/home/agent"),
        ("--publish", "127.0.0.1::8080"),
        ("--network", "adk-voice-agent-computers"),
        ("--add-host", "host.docker.internal:host-gateway"),
    ):
        assert value == run[run.index(flag) + 1]
    assert [
        "network",
        "create",
        "--driver",
        "bridge",
        "--opt",
        "com.docker.network.bridge.enable_icc=false",
        "adk-voice-agent-computers",
    ] in fake.calls
    assert "goog-adk-computer=true" in run and f"owner={OWNER}" in run
    assert "COMPUTER_MCP_UPSTREAM=http://host.docker.internal:8123" in run
    assert "GOOGLE_CLOUD_PROJECT=proj" in run
    assert "GOOGLE_CLOUD_LOCATION=global" in run
    assert not any("GOOGLE_APPLICATION_CREDENTIALS" in a for a in run)
    assert isinstance(env, SandboxEnvironment)
    assert env.base_url == "http://127.0.0.1:55001"
    assert env.mcp_base_url == COMPUTER_MCP_RELAY_URL


async def test_docker_mounts_the_developers_adc_read_only(tmp_path: Path):
    adc = tmp_path / "adc.json"
    adc.write_text("{}")
    fake = FakeDocker()
    provider = DockerProvider(
        runner=fake, transport=httpx.MockTransport(_healthy), adc_file=adc
    )
    await provider.ensure(ALICE)
    run = next(c for c in fake.calls if c[0] == "run")
    assert f"{adc}:/etc/computer/adc.json:ro" in run
    assert "GOOGLE_APPLICATION_CREDENTIALS=/etc/computer/adc.json" in run


async def test_docker_starts_a_stopped_container_and_recreates_an_outdated_one(
    tmp_path: Path,
):
    fake = FakeDocker(image_id="sha256:old")
    provider = _docker(fake, tmp_path)
    await provider.ensure(ALICE)
    await provider.stop(ALICE)
    assert (await provider.status(ALICE)).state is ComputerState.STOPPED
    assert await provider.running_environment(OWNER) is None

    fake.calls.clear()
    await provider.ensure(ALICE)
    assert ["start", computer_name(OWNER)] in fake.calls
    assert not any(c[0] in ("run", "rm") for c in fake.calls)
    assert provider.pop_notice(ALICE) is None

    fake.image_id = "sha256:new"
    fake.calls.clear()
    await provider.ensure(ALICE)
    assert ["rm", "--force", computer_name(OWNER)] in fake.calls
    run = next(c for c in fake.calls if c[0] == "run")
    assert f"{computer_name(OWNER)}:/home/agent" in run
    assert not any(c[:2] == ["volume", "rm"] for c in fake.calls)
    assert "newer image" in (provider.pop_notice(ALICE) or "")
    assert await provider.known_users() == [OWNER]
    assert await provider.running_environment(OWNER) is not None


async def test_docker_reports_a_missing_container(tmp_path: Path):
    provider = _docker(FakeDocker(), tmp_path)
    assert (await provider.status(ALICE)).state is ComputerState.MISSING
    await provider.stop(ALICE)


async def test_docker_fails_when_the_daemon_never_answers(tmp_path: Path):
    provider = DockerProvider(
        runner=FakeDocker(),
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        adc_file=tmp_path / "none",
        sleep=_no_sleep,
    )
    with pytest.raises(ComputerUnavailableError, match="HTTP 503"):
        await provider.ensure(ALICE)


PROJECT, ZONE = "proj", "us-central1-a"
DEPLOYMENT = "voice-staging"
TEMPLATE_V1 = f"projects/{PROJECT}/global/instanceTemplates/computer-v1"
TEMPLATE_V2 = f"projects/{PROJECT}/global/instanceTemplates/computer-v2"
COMPUTER_SA = f"computer@{PROJECT}.iam.gserviceaccount.com"


class FakeCompute:
    def __init__(self) -> None:
        self.instances: dict[str, dict[str, Any]] = {}
        self.disks: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.inserted: list[dict[str, Any]] = []
        self.set_policy_calls: list[str] = []
        self.next_id = 1000
        self.active_runs = 0
        self.templates = {
            name: {
                "properties": {
                    "disks": [
                        {
                            "kind": "compute#attachedDisk",
                            "boot": True,
                            "autoDelete": True,
                            "deviceName": "boot",
                            # As the API returns templates: a short disk type name.
                            "initializeParams": {
                                "sourceImage": "cos",
                                "diskType": "pd-balanced",
                            },
                        }
                    ],
                    "metadata": {
                        "items": [
                            {"key": "user-data", "value": "#cloud-config"}
                        ]
                    },
                    "labels": {"from-template": "yes"},
                    "serviceAccounts": [{"email": COMPUTER_SA}],
                }
            }
            for name in ("computer-v1", "computer-v2")
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _op(name: str = "op-1") -> httpx.Response:
        return httpx.Response(200, json={"name": name, "status": "DONE"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        path = url.path
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if url.host.startswith("10.") and path in ("/healthz", "/activity"):
            assert request.headers["authorization"] == "Bearer id-token"
            if path == "/activity":
                return httpx.Response(
                    200,
                    json={
                        "active_runs": getattr(self, "active_runs", 0),
                        "last_activity": None,
                    },
                )
            return httpx.Response(200, json={"ok": True})
        assert request.headers["authorization"] == "Bearer access-token"
        zone = f"/compute/v1/projects/{PROJECT}/zones/{ZONE}"
        if url.host == "compute.googleapis.com":
            if path.startswith(
                f"/compute/v1/projects/{PROJECT}/global/instanceTemplates/"
            ):
                return httpx.Response(
                    200, json=self.templates[path.rsplit("/", 1)[1]]
                )
            if path.startswith(f"{zone}/operations/"):
                return self._op()
            if path.startswith(f"{zone}/disks"):
                rest = path[len(f"{zone}/disks") :].strip("/")
                if method == "GET":
                    disk = self.disks.get(rest)
                    return (
                        httpx.Response(200, json=disk)
                        if disk
                        else httpx.Response(404)
                    )
                if method == "POST":
                    self.disks[body["name"]] = body
                    return httpx.Response(
                        200, json={"name": "op-disk", "status": "RUNNING"}
                    )
            if path == f"{zone}/instances":
                if method == "GET":
                    wanted = url.params["filter"]
                    assert wanted == (
                        "labels.goog-adk-computer=true AND labels.deployment=voice-staging"
                    )
                    return httpx.Response(
                        200, json={"items": list(self.instances.values())}
                    )
                assert url.params["sourceInstanceTemplate"].startswith(
                    "projects/"
                )
                self.inserted.append(body)
                self.next_id += 1
                self.instances[body["name"]] = {
                    **body,
                    "id": str(self.next_id),
                    "status": "PROVISIONING",
                    "networkInterfaces": [{"networkIP": "10.0.0.7"}],
                }
                return self._op("op-insert")
            if path.startswith(f"{zone}/instances/"):
                name, _, action = path[len(f"{zone}/instances/") :].partition(
                    "/"
                )
                instance = self.instances.get(name)
                if instance is None:
                    return httpx.Response(404)
                if method == "GET" and not action:
                    # Provisioning finishes by the next look.
                    if instance["status"] in ("PROVISIONING", "STAGING"):
                        instance["status"] = "RUNNING"
                        return httpx.Response(
                            200, json={**instance, "status": "STAGING"}
                        )
                    if instance["status"] == "STOPPING":
                        instance["status"] = "TERMINATED"
                    return httpx.Response(200, json=instance)
                if method == "DELETE":
                    self.instances.pop(name)
                    # Instance-level IAM goes with the instance.
                    for key in [
                        k for k in self.policies if f"/instances/{name}" in k
                    ]:
                        self.policies.pop(key)
                    return self._op("op-delete")
                if action == "start":
                    instance["status"] = "STAGING"
                    return self._op("op-start")
                if action == "stop":
                    instance["status"] = "STOPPING"
                    return self._op("op-stop")
                if action == "setDiskAutoDelete":
                    assert url.params["autoDelete"] == "false"
                    return self._op("op-autodelete")
                if action == "getIamPolicy":
                    return httpx.Response(
                        200, json=self.policies.get(path, {"etag": "e"})
                    )
                if action == "setIamPolicy":
                    return self._set_policy(
                        path.replace("setIamPolicy", "getIamPolicy"), body
                    )
        if url.host in ("iap.googleapis.com", "iam.googleapis.com"):
            resource, _, verb = path.rpartition(":")
            if verb == "getIamPolicy":
                return httpx.Response(
                    200, json=self.policies.get(resource, {"etag": "e"})
                )
            if verb == "setIamPolicy":
                return self._set_policy(resource, body)
        return httpx.Response(500, text=f"unexpected {method} {url}")

    def _set_policy(self, key: str, body: dict[str, Any]) -> httpx.Response:
        self.set_policy_calls.append(key)
        self.policies[key] = body["policy"]
        return httpx.Response(200, json=body["policy"])

    def members(self, role: str) -> dict[str, list[str]]:
        return {
            key: binding["members"]
            for key, policy in self.policies.items()
            for binding in policy.get("bindings", [])
            if binding["role"] == role
        }


@pytest.fixture
def gce_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTER_ZONE", ZONE)
    monkeypatch.setenv("COMPUTER_INSTANCE_TEMPLATE", "computer-v1")
    monkeypatch.setenv("COMPUTER_DEPLOYMENT", DEPLOYMENT)
    monkeypatch.setenv("APP_URL", "https://app.example.com")
    monkeypatch.setenv("COMPUTER_MCP_AUDIENCE", "iap-client-id")
    monkeypatch.delenv("COMPUTER_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("SANDBOX_CALLER_SA", raising=False)


def _gce(fake: FakeCompute) -> GceProvider:
    return GceProvider(
        transport=fake.transport(),
        access_token=lambda: "access-token",
        id_token=lambda: "id-token",
        sleep=_no_sleep,
    )


async def test_gce_creates_the_disk_and_vm_from_the_template(gce_env: None):
    fake = FakeCompute()
    provider = _gce(fake)
    env = await provider.ensure(ALICE)

    disk = fake.disks[disk_name(OWNER, DEPLOYMENT)]
    assert disk["sizeGb"] == "50" and disk["type"].endswith("/pd-balanced")
    assert disk_name(OWNER, DEPLOYMENT) == f"voice-staging-{OWNER}-home"
    # terraform caps service_name at 39 chars (variables.tf) to fit GCE's 63.
    assert len(disk_name(OWNER, "d" * 39)) == 63
    assert disk["labels"] == {
        "goog-adk-computer": "true",
        "deployment": DEPLOYMENT,
        "owner": OWNER,
    }

    (body,) = fake.inserted
    assert body["name"] == f"voice-staging-{OWNER}"
    assert body["labels"] == {
        "from-template": "yes",
        "goog-adk-computer": "true",
        "deployment": DEPLOYMENT,
        "owner": OWNER,
        "template": template_label(TEMPLATE_V1),
    }
    boot, home = body["disks"]
    assert boot["boot"] and "kind" not in boot
    # instances.insert rejects a short disk type name; it needs the zonal path.
    assert boot["initializeParams"]["diskType"] == (
        f"projects/{PROJECT}/zones/{ZONE}/diskTypes/pd-balanced"
    )
    assert home == {
        "source": f"projects/{PROJECT}/zones/{ZONE}/disks/{disk_name(OWNER, DEPLOYMENT)}",
        "deviceName": "home",
        "boot": False,
        "autoDelete": False,
        "mode": "READ_WRITE",
        "type": "PERSISTENT",
    }
    items = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert items["user-data"] == "#cloud-config"
    computer_env = dict(
        line.split("=", 1) for line in items["computer-env"].splitlines()
    )
    assert computer_env == {
        "COMPUTER_REQUIRE_AUTH": "1",
        "COMPUTER_AUTH_AUDIENCE": f"adk-voice-agent-computer/{OWNER}",
        "COMPUTER_ALLOWED_CALLERS": f"adk-voice-agent@{PROJECT}.iam.gserviceaccount.com",
        "COMPUTER_MCP_UPSTREAM": "https://app.example.com",
        "COMPUTER_MCP_AUDIENCE": "iap-client-id",
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "GOOGLE_CLOUD_LOCATION": "global",
    }

    assert isinstance(env, SandboxEnvironment)
    assert env.base_url == "http://10.0.0.7:8080"
    assert env.headers() == {"Authorization": "Bearer id-token"}
    assert env.mcp_base_url == COMPUTER_MCP_RELAY_URL
    assert ("GET", "/healthz") in fake.requests
    assert provider.pop_notice(ALICE) is None


async def test_gce_grants_the_user_ssh_once(gce_env: None):
    fake = FakeCompute()
    await _gce(fake).ensure(ALICE)
    member = f"user:{ALICE}"
    assert list(fake.members("roles/compute.osAdminLogin").values()) == [
        [member]
    ]
    tunnel = fake.members("roles/iap.tunnelResourceAccessor")
    assert list(tunnel) == [
        f"/v1/projects/{PROJECT}/iap_tunnel/zones/{ZONE}/instances/{instance_name(OWNER, DEPLOYMENT)}"
    ]
    sa = fake.members("roles/iam.serviceAccountUser")
    assert list(sa) == [f"/v1/projects/-/serviceAccounts/{COMPUTER_SA}"]
    assert len(fake.set_policy_calls) == 3

    await _gce(fake).ensure(ALICE)
    assert len(fake.set_policy_calls) == 3


async def test_gce_skips_ssh_grants_without_an_email(gce_env: None):
    fake = FakeCompute()
    await _gce(fake).ensure("local")
    assert fake.set_policy_calls == []


async def test_gce_starts_a_stopped_vm(gce_env: None):
    fake = FakeCompute()
    provider = _gce(fake)
    await provider.ensure(ALICE)
    await provider.stop(ALICE)
    assert (await provider.status(ALICE)).state is ComputerState.STOPPED
    assert await provider.running_environment(OWNER) is None

    fake.requests.clear()
    env = await provider.ensure(ALICE)
    assert isinstance(env, SandboxEnvironment)
    path = f"/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{instance_name(OWNER, DEPLOYMENT)}"
    assert ("POST", f"{path}/start") in fake.requests
    assert len(fake.inserted) == 1
    assert env.base_url == "http://10.0.0.7:8080"
    assert (await provider.status(ALICE)).state is ComputerState.RUNNING
    assert await provider.running_environment(OWNER) is not None


async def test_gce_recreates_an_outdated_vm_and_keeps_the_disk(
    gce_env: None, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeCompute()
    provider = _gce(fake)
    await provider.ensure(ALICE)
    first_id = fake.instances[instance_name(OWNER, DEPLOYMENT)]["id"]

    monkeypatch.setenv(
        "COMPUTER_INSTANCE_TEMPLATE",
        f"https://www.googleapis.com/compute/v1/{TEMPLATE_V2}",
    )
    assert "newer template" in (await provider.status(ALICE)).detail
    fake.requests.clear()
    await provider.ensure(ALICE)

    path = f"/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{instance_name(OWNER, DEPLOYMENT)}"
    methods = [r for r in fake.requests if r[1].startswith(path)]
    # The home disk was attached with autoDelete off: nothing to flip.
    assert ("POST", f"{path}/setDiskAutoDelete") not in methods
    assert ("DELETE", path) in methods
    assert not any(m == "DELETE" and "/disks" in p for m, p in fake.requests)
    assert not any(
        m == "POST" and p.endswith("/disks") for m, p in fake.requests
    )
    assert len(fake.inserted) == 2
    second = fake.instances[instance_name(OWNER, DEPLOYMENT)]
    assert second["id"] != first_id
    assert second["labels"]["template"] == template_label(TEMPLATE_V2)
    assert second["disks"][-1]["source"].endswith(disk_name(OWNER, DEPLOYMENT))
    assert "newer image" in (provider.pop_notice(ALICE) or "")
    # The new VM gets the instance-level grants again.
    assert len(fake.set_policy_calls) == 5


async def test_gce_and_docker_defer_recreate_while_active_runs_nonzero(
    gce_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = FakeCompute()
    provider = _gce(fake)
    await provider.ensure(ALICE)
    first_id = fake.instances[instance_name(OWNER, DEPLOYMENT)]["id"]

    monkeypatch.setenv(
        "COMPUTER_INSTANCE_TEMPLATE",
        f"https://www.googleapis.com/compute/v1/{TEMPLATE_V2}",
    )
    fake.active_runs = 1
    await provider.ensure(ALICE)
    assert fake.instances[instance_name(OWNER, DEPLOYMENT)]["id"] == first_id
    assert provider.pop_notice(ALICE) is None

    fake.active_runs = 0
    await provider.ensure(ALICE)
    assert fake.instances[instance_name(OWNER, DEPLOYMENT)]["id"] != first_id
    assert "newer image" in (provider.pop_notice(ALICE) or "")

    # Same for DockerProvider when an image changes mid-run.
    docker_active_runs = 1

    def docker_daemon(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/activity":
            return httpx.Response(
                200,
                json={"active_runs": docker_active_runs, "last_activity": None},
            )
        return httpx.Response(404)

    fake_docker = FakeDocker(image_id="sha256:v1")
    docker_provider = DockerProvider(
        runner=fake_docker,
        transport=httpx.MockTransport(docker_daemon),
        adc_file=tmp_path / "missing-adc.json",
        sleep=_no_sleep,
    )
    await docker_provider.ensure(ALICE)
    fake_docker.image_id = "sha256:v2"
    fake_docker.calls.clear()
    await docker_provider.ensure(ALICE)
    assert not any(c[0] == "rm" for c in fake_docker.calls)

    docker_active_runs = 0
    await docker_provider.ensure(ALICE)
    assert any(c[0] == "rm" for c in fake_docker.calls)


async def test_gce_lists_known_users_and_stops(gce_env: None):
    fake = FakeCompute()
    provider = _gce(fake)
    assert (await provider.status(ALICE)).state is ComputerState.MISSING
    await provider.stop(ALICE)  # missing: a no-op
    await provider.ensure(ALICE)
    await provider.ensure("bob@example.com")
    assert await provider.known_users() == sorted(
        [OWNER, sandbox_owner("bob@example.com")]
    )
    await provider.stop(OWNER)
    assert (
        fake.instances[instance_name(OWNER, DEPLOYMENT)]["status"] == "STOPPING"
    )


async def test_gce_reports_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
):
    for key in (
        "GOOGLE_CLOUD_PROJECT",
        "COMPUTER_ZONE",
        "COMPUTER_INSTANCE_TEMPLATE",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ComputerUnavailableError, match="COMPUTER_ZONE"):
        await _gce(FakeCompute()).ensure(ALICE)


async def test_gce_concurrent_ensures_create_one_vm(gce_env: None):
    fake = FakeCompute()
    provider = _gce(fake)
    await asyncio.gather(provider.ensure(ALICE), provider.ensure(ALICE))
    assert len(fake.inserted) == 1


# ---- the idle reaper ------------------------------------------------------------------


class FakeActivityEnv:
    def __init__(self, activity: Activity) -> None:
        self._activity = activity

    async def activity(self) -> Activity:
        return self._activity


class FakeStoppable:
    name = "fake"

    def __init__(self, activities: dict[str, Activity | None]) -> None:
        self.activities = activities
        self.stopped: list[str] = []

    async def ensure(self, user_id: str) -> Any:
        raise AssertionError("the reaper must never start a computer")

    async def status(self, user_id: str) -> Any:
        raise NotImplementedError

    async def stop(self, user_id: str) -> None:
        self.stopped.append(user_id)

    async def known_users(self) -> list[str]:
        return list(self.activities)

    async def running_environment(self, user_id: str) -> Any:
        activity = self.activities[user_id]
        return None if activity is None else FakeActivityEnv(activity)


async def test_the_reaper_stops_only_idle_computers(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("COMPUTER_IDLE_STOP_MIN", "20")
    monkeypatch.setenv("COMPUTER_WARM_HOURS", "off")
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=30)).isoformat()
    recent = (now - timedelta(minutes=5)).isoformat()
    owners = [sandbox_owner(f"user{i}@example.com") for i in range(6)]
    provider = FakeStoppable(
        {
            owners[0]: Activity(active_runs=0, last_activity=old),  # idle
            owners[1]: Activity(
                active_runs=1, last_activity=old
            ),  # a run is going
            owners[2]: Activity(
                active_runs=0, last_activity=recent
            ),  # recently used
            owners[3]: None,  # already stopped
            owners[4]: Activity(
                active_runs=0, last_activity=old
            ),  # talking to us
            owners[5]: Activity(
                active_runs=0, last_activity=""
            ),  # unknown: idle
        }
    )
    live = LiveSessions()
    session = asyncio.get_running_loop().create_future()
    live.open("user4@example.com", session)  # type: ignore[arg-type]
    invalidated: list[str] = []
    monkeypatch.setattr(factory, "invalidate_environment", invalidated.append)

    stopped = await reap_once(provider, live=live, now=now)  # type: ignore[arg-type]
    assert stopped == provider.stopped == [owners[0], owners[5]]
    assert invalidated == stopped

    # Once the voice session ends, that computer may sleep too.
    session.set_result(None)
    await asyncio.sleep(0)
    assert not live.has_live(owners[4])
    assert owners[4] in await reap_once(provider, live=live, now=now)  # type: ignore[arg-type]


async def test_the_reaper_ignores_providers_that_cannot_stop():
    assert await reap_once(HostProvider()) == []


async def test_gce_running_environment_probes_healthz_and_reaper_stops_unreachable_vm(
    gce_env: None, monkeypatch: pytest.MonkeyPatch
):
    from app.workers.providers.base import ComputerStatus
    from app.workers.providers.last_known import last_known_status

    fake = FakeCompute()
    provider = _gce(fake)
    await provider.ensure(ALICE)
    assert await provider.running_environment(ALICE) is not None

    def timeout_healthz(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            raise httpx.ConnectTimeout("daemon unreachable")
        return fake.handle(request)

    unhealthy_provider = GceProvider(
        access_token=lambda: "access-token",
        id_token=lambda: "id-token",
        transport=httpx.MockTransport(timeout_healthz),
        sleep=_no_sleep,
    )
    assert await unhealthy_provider.running_environment(ALICE) is None
    assert (
        await unhealthy_provider.status(ALICE)
    ).state is ComputerState.STARTING
    assert last_known_status(OWNER) == ComputerStatus(ComputerState.STARTING)

    monkeypatch.setenv("COMPUTER_IDLE_STOP_MIN", "20")
    monkeypatch.setenv("COMPUTER_WARM_HOURS", "off")
    monkeypatch.setattr(factory, "invalidate_environment", lambda _owner: None)
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    unreachable: dict[str, datetime] = {}
    assert (
        await reap_once(
            unhealthy_provider,
            live=LiveSessions(),
            now=now,
            unreachable=unreachable,
        )
        == []
    )
    assert await reap_once(
        unhealthy_provider,
        live=LiveSessions(),
        now=now + timedelta(minutes=21),
        unreachable=unreachable,
    ) == [OWNER]
    assert (
        fake.instances[instance_name(OWNER, DEPLOYMENT)]["status"] == "STOPPING"
    )


async def test_the_last_known_status_follows_ensure_and_the_reaper(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.providers.base import ComputerStatus
    from app.workers.providers.last_known import last_known_status

    assert last_known_status(ALICE) is None

    seen: list[ComputerStatus | None] = []
    fail = False

    class Recording(HostProvider):
        name = "recording"

        async def ensure(self, user_id: str) -> Any:
            seen.append(last_known_status(user_id))
            if fail:
                raise ComputerUnavailableError("no capacity in the zone")
            return await super().ensure(user_id)

    monkeypatch.setitem(registry._FACTORIES, "recording", Recording)
    monkeypatch.setenv("COMPUTER_PROVIDER", "recording")
    await factory.resolve_environment()
    assert seen == [ComputerStatus(ComputerState.STARTING)]
    assert last_known_status("local") == ComputerStatus(ComputerState.RUNNING)
    factory.invalidate_environment("local")
    fail = True
    with pytest.raises(ComputerUnavailableError):
        await factory.resolve_environment()
    assert last_known_status("local") == ComputerStatus(
        ComputerState.ERROR, "no capacity in the zone"
    )

    monkeypatch.setenv("COMPUTER_IDLE_STOP_MIN", "20")
    monkeypatch.setenv("COMPUTER_WARM_HOURS", "off")
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    idle, busy, off = (sandbox_owner(f"{n}@example.com") for n in "abc")
    provider = FakeStoppable(
        {
            idle: Activity(active_runs=0, last_activity=""),
            busy: Activity(active_runs=1, last_activity=""),
            off: None,
        }
    )
    monkeypatch.setattr(factory, "invalidate_environment", lambda _owner: None)
    assert await reap_once(provider, live=LiveSessions(), now=now) == [idle]  # type: ignore[arg-type]
    assert {o: last_known_status(o) for o in (idle, busy, off)} == {
        idle: ComputerStatus(ComputerState.STOPPED),
        busy: ComputerStatus(ComputerState.RUNNING),
        off: ComputerStatus(ComputerState.STOPPED),
    }


def _warm_env(
    monkeypatch: pytest.MonkeyPatch, hours: str, days: str = ""
) -> None:
    monkeypatch.setenv("COMPUTER_WARM_HOURS", hours)
    monkeypatch.setenv("COMPUTER_WARM_DAYS", days)
    monkeypatch.setenv("COMPUTER_TIMEZONE", "America/Chicago")


def _chicago(day: int, hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(
        2026, 10, day, hour, minute, tzinfo=ZoneInfo("America/Chicago")
    )


CHICAGO = ZoneInfo("America/Chicago")


async def test_warm_hours_cover_weekday_working_hours_in_the_timezone(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.providers.reaper import warm_schedule

    _warm_env(monkeypatch, "08:00-18:00")
    hours = warm_schedule()
    assert hours is not None
    assert hours.opened_on(_chicago(23, 8), CHICAGO) == date(2026, 10, 23)
    assert hours.opened_on(_chicago(23, 17, 59), CHICAGO) == date(2026, 10, 23)
    assert hours.opened_on(_chicago(23, 18), CHICAGO) is None
    assert hours.opened_on(_chicago(23, 7, 59), CHICAGO) is None
    assert hours.opened_on(_chicago(24, 12), CHICAGO) is None  # Saturday
    # 13:00 UTC is 08:00 in Chicago (CDT), but only 06:00 in Los Angeles.
    utc_13 = datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
    assert hours.opened_on(utc_13, CHICAGO) is not None
    assert hours.opened_on(utc_13, ZoneInfo("America/Los_Angeles")) is None


async def test_a_warm_window_across_midnight_belongs_to_the_day_it_opened(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.providers.reaper import warm_schedule

    _warm_env(monkeypatch, "22:00-06:00", "fri")
    hours = warm_schedule()
    assert hours is not None
    assert hours.opened_on(_chicago(23, 23), CHICAGO) == date(2026, 10, 23)
    assert hours.opened_on(_chicago(24, 5), CHICAGO) == date(2026, 10, 23)
    assert hours.opened_on(_chicago(24, 23), CHICAGO) is None  # opened Saturday
    assert hours.opened_on(_chicago(23, 5), CHICAGO) is None  # opened Thursday


async def test_warm_hours_are_on_by_default_on_weekdays(
    monkeypatch: pytest.MonkeyPatch,
):
    from datetime import time as clock

    from app.workers.providers.reaper import warm_schedule

    monkeypatch.delenv("COMPUTER_WARM_HOURS", raising=False)
    monkeypatch.delenv("COMPUTER_WARM_DAYS", raising=False)
    hours = warm_schedule()
    assert hours is not None
    assert (hours.start, hours.end) == (clock(8), clock(18))
    assert hours.days == frozenset(range(5))


@pytest.mark.parametrize(
    ("hours", "days"),
    [
        ("off", ""),
        ("OFF", ""),
        ("none", ""),
        ("0", ""),
        ("8am-6pm", ""),
        ("08:00-08:00", ""),
        ("08:00-18:00", "someday"),
    ],
)
async def test_off_or_invalid_warm_hours_turn_the_window_off(
    monkeypatch: pytest.MonkeyPatch, hours: str, days: str
):
    from app.workers.providers.reaper import warm_schedule

    monkeypatch.setenv("COMPUTER_WARM_HOURS", hours)
    monkeypatch.setenv("COMPUTER_WARM_DAYS", days)
    assert warm_schedule() is None


async def test_each_owner_keeps_the_warm_hours_in_their_own_timezone(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.store.user_settings import get_user_settings_store
    from app.user_settings import TIMEZONE
    from app.workers.providers.reaper import WarmZones

    store = get_user_settings_store()
    await store.save("tokyo@example.com", {TIMEZONE: "Asia/Tokyo"})
    await store.save("typo@example.com", {TIMEZONE: "Mars/Olympus"})
    monkeypatch.setenv("COMPUTER_TIMEZONE", "Europe/London")
    zones = await WarmZones.load()
    assert zones.zone(sandbox_owner("tokyo@example.com")) == ZoneInfo(
        "Asia/Tokyo"
    )
    assert zones.zone("tokyo@example.com") == ZoneInfo("Asia/Tokyo")
    assert zones.zone(sandbox_owner("nobody@example.com")) == ZoneInfo(
        "Europe/London"
    )
    monkeypatch.delenv("COMPUTER_TIMEZONE")
    monkeypatch.setenv("USER_TIMEZONE", "America/Denver")
    assert zones.zone(sandbox_owner("nobody@example.com")) == ZoneInfo(
        "America/Denver"
    )
    monkeypatch.delenv("USER_TIMEZONE")
    assert zones.zone(sandbox_owner("nobody@example.com")) == CHICAGO
    assert zones.zone(sandbox_owner("typo@example.com")) == CHICAGO


async def test_the_reaper_skips_only_owners_inside_their_warm_hours(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.store.user_settings import get_user_settings_store
    from app.user_settings import TIMEZONE

    monkeypatch.setenv("COMPUTER_IDLE_STOP_MIN", "20")
    _warm_env(monkeypatch, "08:00-18:00")
    await get_user_settings_store().save(
        "tokyo@example.com", {TIMEZONE: "Asia/Tokyo"}
    )
    old = (_chicago(23, 9) - timedelta(minutes=30)).isoformat()
    chicago, tokyo = (
        sandbox_owner("idle@example.com"),
        sandbox_owner("tokyo@example.com"),
    )
    provider = FakeStoppable(
        {
            chicago: Activity(active_runs=0, last_activity=old),
            tokyo: Activity(active_runs=0, last_activity=old),
        }
    )
    monkeypatch.setattr(factory, "invalidate_environment", lambda _owner: None)

    # 09:00 Friday in Chicago is 23:00 in Tokyo: only Tokyo's computer may sleep.
    assert await reap_once(provider, now=_chicago(23, 9)) == [tokyo]  # type: ignore[arg-type]
    assert await reap_once(provider, now=_chicago(23, 19)) == [chicago, tokyo]  # type: ignore[arg-type]
    # With warm hours off, the idle stop applies at any hour.
    monkeypatch.setenv("COMPUTER_WARM_HOURS", "off")
    provider.stopped.clear()
    assert await reap_once(provider, now=_chicago(23, 9)) == [chicago, tokyo]  # type: ignore[arg-type]


async def test_the_warm_hours_start_each_stopped_computer_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.providers import reaper

    _warm_env(monkeypatch, "08:00-18:00")
    running, stopped = (
        sandbox_owner("a@example.com"),
        sandbox_owner("b@example.com"),
    )

    class Warmable(FakeStoppable):
        def __init__(self) -> None:
            super().__init__(
                {
                    running: Activity(active_runs=0, last_activity=""),
                    stopped: None,
                }
            )
            self.ensured: list[str] = []

        async def ensure(self, user_id: str) -> Any:
            self.ensured.append(user_id)

    provider = Warmable()
    warmed: dict[str, date] = {}

    assert (
        await reaper.warm_once(provider, now=_chicago(23, 7), warmed=warmed)
        == []
    )  # type: ignore[arg-type]
    assert await reaper.warm_once(
        provider, now=_chicago(23, 8), warmed=warmed
    ) == [  # type: ignore[arg-type]
        stopped
    ]
    for _ in range(5):
        await asyncio.sleep(0)
    assert provider.ensured == [stopped]
    from app.workers.providers.last_known import last_known_status

    assert last_known_status(running) == last_known_status(stopped)
    assert last_known_status(stopped).state == ComputerState.RUNNING  # type: ignore[union-attr]
    # Later ticks in the same window leave it alone, even if it was stopped by hand.
    assert (
        await reaper.warm_once(provider, now=_chicago(23, 9), warmed=warmed)
        == []
    )  # type: ignore[arg-type]
    # The next working day's window starts it again; the weekend's doesn't exist.
    assert (
        await reaper.warm_once(provider, now=_chicago(24, 9), warmed=warmed)
        == []
    )  # type: ignore[arg-type]
    assert await reaper.warm_once(
        provider, now=_chicago(26, 8), warmed=warmed
    ) == [  # type: ignore[arg-type]
        stopped
    ]


async def test_the_warm_hours_open_at_each_owners_own_morning(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.store.user_settings import get_user_settings_store
    from app.user_settings import TIMEZONE
    from app.workers.providers import reaper

    _warm_env(monkeypatch, "08:00-18:00")
    await get_user_settings_store().save(
        "la@example.com", {TIMEZONE: "America/Los_Angeles"}
    )
    chicago, la = (
        sandbox_owner("c@example.com"),
        sandbox_owner("la@example.com"),
    )

    class Warmable(FakeStoppable):
        async def ensure(self, user_id: str) -> Any:
            return None

    provider = Warmable({chicago: None, la: None})
    warmed: dict[str, date] = {}
    # 08:00 in Chicago is 06:00 in Los Angeles; LA's window opens two hours later.
    assert await reaper.warm_once(
        provider, now=_chicago(23, 8), warmed=warmed
    ) == [  # type: ignore[arg-type]
        chicago
    ]
    assert await reaper.warm_once(
        provider, now=_chicago(23, 10), warmed=warmed
    ) == [  # type: ignore[arg-type]
        la
    ]


async def test_warm_hours_alone_keep_the_reaper_loop_running(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.providers.reaper import reaper_enabled

    monkeypatch.setenv("COMPUTER_IDLE_STOP_MIN", "0")
    monkeypatch.setenv("COMPUTER_WARM_HOURS", "off")
    provider = FakeStoppable({})
    assert not reaper_enabled(provider)  # type: ignore[arg-type]
    monkeypatch.delenv("COMPUTER_WARM_HOURS")  # on by default
    assert reaper_enabled(provider)  # type: ignore[arg-type]
    assert not reaper_enabled(HostProvider())


async def test_a_voice_session_prewarms_the_computer_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.providers import reaper

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_resolve() -> Any:
        started.set()
        await release.wait()
        raise RuntimeError("the computer is on fire")

    monkeypatch.setattr(factory, "resolve_environment", slow_resolve)
    session = asyncio.get_running_loop().create_future()
    # Returns at once even though the computer takes a while (and then fails).
    reaper.voice_session_opened(ALICE, session)  # type: ignore[arg-type]
    assert reaper._live_sessions.has_live(ALICE)
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not reaper._prewarms
    session.set_result(None)
    await asyncio.sleep(0)
    assert not reaper._live_sessions.has_live(ALICE)


# ---- the driver --------------------------------------------------------------------------


async def test_the_driver_points_harnesses_at_the_environments_mcp_url(
    lab: Any, monkeypatch: pytest.MonkeyPatch
):
    from app.api import mcp

    seen: list[str | None] = []
    real = mcp.mcp_env_for_thread

    def recording(thread_id: str, user_id: str, base_url: str | None = None):
        seen.append(base_url)
        return real(thread_id, user_id, base_url=base_url)

    monkeypatch.setattr(mcp, "mcp_env_for_thread", recording)
    monkeypatch.setattr(lab.env, "mcp_base_url", COMPUTER_MCP_RELAY_URL)
    await lab.dispatch()
    assert seen and set(seen) == {COMPUTER_MCP_RELAY_URL}
    assert urlsplit(COMPUTER_MCP_RELAY_URL).path == "/mcp-relay"


async def test_gce_turns_off_a_flipped_auto_delete_before_recreating(
    gce_env: None, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeCompute()
    provider = _gce(fake)
    await provider.ensure(ALICE)
    vm = fake.instances[instance_name(OWNER, DEPLOYMENT)]
    vm["disks"][-1]["autoDelete"] = True  # someone flipped it

    monkeypatch.setenv(
        "COMPUTER_INSTANCE_TEMPLATE",
        f"https://www.googleapis.com/compute/v1/{TEMPLATE_V2}",
    )
    fake.requests.clear()
    await provider.ensure(ALICE)

    path = f"/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{instance_name(OWNER, DEPLOYMENT)}"
    methods = [r for r in fake.requests if r[1].startswith(path)]
    assert methods.index(("POST", f"{path}/setDiskAutoDelete")) < methods.index(
        ("DELETE", path)
    )


_COMPUTER_TF = Path(__file__).resolve().parents[2] / "terraform" / "computer.tf"

_CALL_PERMISSIONS: list[tuple[str, str, frozenset[str]]] = [
    ("GET", r"/zones/[^/]+/disks/[^/]+", frozenset({"compute.disks.get"})),
    (
        "POST",
        r"/zones/[^/]+/disks",
        frozenset(
            {
                "compute.disks.create",
                "compute.disks.setLabels",
                "compute.disks.addResourcePolicies",
                "compute.resourcePolicies.use",
            }
        ),
    ),
    (
        "GET",
        r"/global/instanceTemplates/[^/]+",
        frozenset({"compute.instanceTemplates.get"}),
    ),
    ("GET", r"/zones/[^/]+/instances", frozenset({"compute.instances.list"})),
    (
        "POST",
        r"/zones/[^/]+/instances",
        frozenset(
            {
                "compute.instances.create",
                "compute.instances.setMetadata",
                "compute.instances.setLabels",
                "compute.instances.setServiceAccount",
                "compute.instanceTemplates.useReadOnly",
                "compute.images.useReadOnly",
                "compute.subnetworks.use",
                "compute.disks.create",  # the boot disk
                "compute.disks.use",  # the home disk
                "iam.serviceAccounts.actAs",  # runs as the computer account
            }
        ),
    ),
    (
        "GET",
        r"/zones/[^/]+/instances/[^/]+",
        frozenset({"compute.instances.get"}),
    ),
    (
        "DELETE",
        r"/zones/[^/]+/instances/[^/]+",
        frozenset({"compute.instances.delete"}),
    ),
    (
        "POST",
        r"/zones/[^/]+/instances/[^/]+/start",
        frozenset({"compute.instances.start"}),
    ),
    (
        "POST",
        r"/zones/[^/]+/instances/[^/]+/resume",
        frozenset({"compute.instances.resume"}),
    ),
    (
        "POST",
        r"/zones/[^/]+/instances/[^/]+/stop",
        frozenset({"compute.instances.stop"}),
    ),
    (
        "POST",
        r"/zones/[^/]+/instances/[^/]+/setDiskAutoDelete",
        frozenset(
            {"compute.instances.setDiskAutoDelete", "compute.disks.update"}
        ),
    ),
    (
        "GET",
        r"/zones/[^/]+/instances/[^/]+/getIamPolicy",
        frozenset({"compute.instances.getIamPolicy"}),
    ),
    (
        "POST",
        r"/zones/[^/]+/instances/[^/]+/setIamPolicy",
        frozenset({"compute.instances.setIamPolicy"}),
    ),
    (
        "POST",
        r"/zones/[^/]+/operations/[^/]+/wait",
        frozenset({"compute.zoneOperations.get"}),
    ),
    (
        "GET",
        r"/zones/[^/]+/operations/[^/]+",
        frozenset({"compute.zoneOperations.get"}),
    ),
    (
        "POST",
        r"/iap_tunnel/zones/[^/]+/instances/[^/]+:getIamPolicy",
        frozenset({"iap.tunnelInstances.getIamPolicy"}),
    ),
    (
        "POST",
        r"/iap_tunnel/zones/[^/]+/instances/[^/]+:setIamPolicy",
        frozenset({"iap.tunnelInstances.setIamPolicy"}),
    ),
    (
        "POST",
        r"/serviceAccounts/[^/]+:getIamPolicy",
        frozenset({"iam.serviceAccounts.getIamPolicy"}),
    ),
    (
        "POST",
        r"/serviceAccounts/[^/]+:setIamPolicy",
        frozenset({"iam.serviceAccounts.setIamPolicy"}),
    ),
    (
        "GET",
        r"/(?:healthz|activity)",
        frozenset(),
    ),
]

_PREDEFINED = {"iam.serviceAccounts.actAs": "roles/iam.serviceAccountUser"}


def _custom_roles() -> dict[str, set[str]]:
    text = _COMPUTER_TF.read_text()
    roles = {}
    for match in re.finditer(
        r'resource "google_project_iam_custom_role" "(\w+)" \{(.*?)\n\}',
        text,
        re.S,
    ):
        block = match.group(2).split("permissions", 1)[-1]
        roles[match.group(1)] = set(
            re.findall(r'"([a-z]+\.[A-Za-z]+\.[A-Za-z]+)"', block)
        )
    return roles


def _needed(method: str, path: str) -> frozenset[str]:
    for want, pattern, permissions in _CALL_PERMISSIONS:
        if method == want and re.search(f"{pattern}$", path):
            return permissions
    raise AssertionError(
        f"{method} {path}: add it to _CALL_PERMISSIONS and grant its permissions "
        "in terraform/computer.tf"
    )


async def test_gce_roles_grant_every_call_the_provider_makes(
    gce_env: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "COMPUTER_SNAPSHOT_POLICY",
        f"projects/{PROJECT}/regions/us-central1/resourcePolicies/snapshots",
    )
    fake = FakeCompute()
    provider = _gce(fake)
    # Every path: create (disk, VM, SSH grants), stop and start, list, and recreate
    # with a flipped autoDelete.
    await provider.ensure(ALICE)
    await provider.stop(ALICE)
    await provider.ensure(ALICE)
    await provider.known_users()
    fake.instances[instance_name(OWNER, DEPLOYMENT)]["disks"][-1][
        "autoDelete"
    ] = True
    monkeypatch.setenv(
        "COMPUTER_INSTANCE_TEMPLATE",
        f"https://www.googleapis.com/compute/v1/{TEMPLATE_V2}",
    )
    await provider.ensure(ALICE)

    roles = _custom_roles()
    granted = set().union(*roles.values())
    tf = _COMPUTER_TF.read_text()
    for method, path in sorted(set(fake.requests)):
        for permission in _needed(method, path):
            if permission in _PREDEFINED:
                assert _PREDEFINED[permission] in tf, permission
            else:
                assert permission in granted, (
                    f"{method} {path} needs {permission}"
                )


async def test_disk_permissions_are_granted_only_on_this_deployments_disks():
    roles = _custom_roles()
    # Only listing and creating are project-wide: none reaches an existing disk's data.
    assert {
        p for p in roles["computer_manager"] if p.startswith("compute.disks.")
    } == {
        "compute.disks.list",
        "compute.disks.create",
        "compute.disks.setLabels",
        "compute.disks.addResourcePolicies",
    }
    assert roles["computer_disk_manager"] == {
        "compute.disks.get",
        "compute.disks.use",
        "compute.disks.update",
    }
    binding = re.search(
        r'resource "google_project_iam_member" "app_computer_disk_manager" \{(.*?)\n\}',
        _COMPUTER_TF.read_text(),
        re.S,
    )
    assert (
        binding
        and 'startsWith(\\"${var.service_name}-u-\\")' in binding.group(1)
    )
