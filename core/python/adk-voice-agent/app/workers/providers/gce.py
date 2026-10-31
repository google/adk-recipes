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

"""`COMPUTER_PROVIDER=gce`: one Compute Engine VM and persistent home disk per user."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.workers.environment import (
    COMPUTER_DAEMON_PORT,
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
    DEPLOYMENT_LABEL,
    OWNER_LABEL,
    AccessTokenSource,
    IdTokenSource,
    OwnerLocks,
    caller_service_account,
    computer_auth_audience,
    computer_deployment,
    owner_id,
    wait_for_daemon,
)

logger = logging.getLogger(__name__)

COMPUTE_API = "https://compute.googleapis.com/compute/v1"
IAP_API = "https://iap.googleapis.com/v1"
IAM_API = "https://iam.googleapis.com/v1"
TEMPLATE_LABEL = "template"
ENV_METADATA_KEY = "computer-env"
HOME_DEVICE = "home"
DEFAULT_DISK_GB = 50

_STATES = {
    "PROVISIONING": ComputerState.STARTING,
    "STAGING": ComputerState.STARTING,
    "RUNNING": ComputerState.RUNNING,
    "STOPPING": ComputerState.STOPPING,
    "SUSPENDING": ComputerState.STOPPING,
    "SUSPENDED": ComputerState.STOPPED,
    "TERMINATED": ComputerState.STOPPED,
    "STOPPED": ComputerState.STOPPED,
    "REPAIRING": ComputerState.ERROR,
}
_START_CALL = {"TERMINATED": "start", "STOPPED": "start", "SUSPENDED": "resume"}
_LIVE = {"PROVISIONING", "STAGING", "RUNNING"}
_RUNNING_POLLS = 120


def instance_name(owner: str, deployment: str) -> str:
    return f"{deployment}-{owner}"


def instance_state(status: str) -> ComputerState:
    return _STATES.get(status, ComputerState.ERROR)


def disk_name(owner: str, deployment: str) -> str:
    return f"{instance_name(owner, deployment)}-home"


def computer_labels(owner: str, deployment: str) -> dict[str, str]:
    return {
        COMPUTER_LABEL: "true",
        DEPLOYMENT_LABEL: deployment,
        OWNER_LABEL: owner,
    }


def computer_filter(deployment: str) -> str:
    return f"labels.{COMPUTER_LABEL}=true AND labels.{DEPLOYMENT_LABEL}={deployment}"


def template_path(ref: str, project: str) -> str:
    ref = ref.strip()
    if "/compute/v1/" in ref:
        ref = ref.split("/compute/v1/", 1)[1]
    if "/" not in ref:
        ref = f"projects/{project}/global/instanceTemplates/{ref}"
    return ref.strip("/")


def template_label(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _add_binding(policy: dict[str, Any], role: str, member: str) -> bool:
    bindings = policy.setdefault("bindings", [])
    for binding in bindings:
        if binding.get("role") == role and not binding.get("condition"):
            members = binding.setdefault("members", [])
            if member in members:
                return False
            members.append(member)
            return True
    bindings.append({"role": role, "members": [member]})
    return True


@dataclass(frozen=True)
class _Config:
    project: str
    zone: str
    template: str
    disk_gb: int
    computer_sa: str
    snapshot_policy: str
    location: str
    deployment: str

    @property
    def zone_url(self) -> str:
        return f"{COMPUTE_API}/projects/{self.project}/zones/{self.zone}"


def _config() -> _Config:
    from app.auth.env_store import load_dotenv_defaults
    from app.auth.sandbox_env import sandbox_location

    load_dotenv_defaults()
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    zone = (os.getenv("COMPUTER_ZONE") or "").strip()
    template = (os.getenv("COMPUTER_INSTANCE_TEMPLATE") or "").strip()
    missing = [
        key
        for key, value in (
            ("GOOGLE_CLOUD_PROJECT", project),
            ("COMPUTER_ZONE", zone),
            ("COMPUTER_INSTANCE_TEMPLATE", template),
        )
        if not value
    ]
    if missing:
        raise ComputerUnavailableError(
            f"COMPUTER_PROVIDER=gce needs {', '.join(missing)}."
        )
    try:
        disk_gb = int(os.getenv("COMPUTER_DISK_GB") or DEFAULT_DISK_GB)
    except ValueError:
        disk_gb = DEFAULT_DISK_GB
    return _Config(
        project=project,
        zone=zone,
        template=template_path(template, project),
        disk_gb=disk_gb,
        computer_sa=(os.getenv("COMPUTER_SERVICE_ACCOUNT") or "").strip(),
        snapshot_policy=(os.getenv("COMPUTER_SNAPSHOT_POLICY") or "").strip(),
        location=sandbox_location(),
        deployment=computer_deployment(),
    )


def _instance_disk(disk: dict[str, Any], cfg: _Config) -> dict[str, Any]:
    """Expand short template `diskType` names (`pd-balanced`) into zonal resource paths."""
    out = {k: v for k, v in disk.items() if k not in ("kind", "index")}
    params = dict(out.get("initializeParams") or {})
    disk_type = str(params.get("diskType") or "")
    if disk_type and "/" not in disk_type:
        params["diskType"] = (
            f"projects/{cfg.project}/zones/{cfg.zone}/diskTypes/{disk_type}"
        )
        out["initializeParams"] = params
    return out


def _default_dotfiles_repo(user_id: str) -> str:
    return (os.getenv("DOTFILES_REPO") or "").strip()


class GceProvider:
    name = "gce"

    def __init__(
        self,
        *,
        transport: Any | None = None,
        access_token: Callable[[], str] | None = None,
        id_token: Callable[[], str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dotfiles_repo: Callable[[str], str] = _default_dotfiles_repo,
    ) -> None:
        self._transport = transport
        self._access_token = access_token or AccessTokenSource()
        self._id_token = id_token
        self._id_tokens: dict[str, IdTokenSource] = {}
        self._sleep = sleep
        self._dotfiles_repo = dotfiles_repo
        self._locks = OwnerLocks()
        self._notices: dict[str, str] = {}
        self._granted_instances: set[tuple[str, str]] = set()
        self._granted_sa: set[str] = set()

    async def _api(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        import httpx

        from app.app_utils.http_client import get_pooled_http_client

        token = await asyncio.to_thread(self._access_token)
        try:
            async with get_pooled_http_client(
                COMPUTE_API, transport=self._transport
            ) as client:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(150.0, connect=15.0),
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "Compute Engine API %s %s failed: %s", method, url, exc
            )
            raise ComputerUnavailableError(
                "Could not reach Compute Engine to prepare the user's computer."
            ) from exc
        if resp.status_code == 404 and missing_ok:
            return None
        if resp.status_code >= 400:
            logger.warning(
                "Compute Engine API %s %s returned HTTP %s: %s",
                method,
                url,
                resp.status_code,
                resp.text[:300],
            )
            raise ComputerUnavailableError(
                f"Compute Engine request failed (HTTP {resp.status_code})."
            )
        data = resp.json() if resp.content else {}
        return data if isinstance(data, dict) else {}

    async def _wait(self, cfg: _Config, op: dict[str, Any] | None) -> None:
        for _ in range(30):
            if not op or op.get("status") == "DONE":
                break
            op = await self._api(
                "POST", f"{cfg.zone_url}/operations/{op['name']}/wait"
            )
        if op and op.get("error"):
            errors = (op["error"].get("errors") or [{}])[0]
            logger.warning(
                "Compute Engine operation %s failed: %s",
                op.get("name"),
                errors.get("message") or op["error"],
            )
            raise ComputerUnavailableError(
                "Compute Engine operation failed while preparing the user's computer."
            )

    async def _instance(
        self, cfg: _Config, owner: str
    ) -> dict[str, Any] | None:
        return await self._api(
            "GET",
            f"{cfg.zone_url}/instances/{instance_name(owner, cfg.deployment)}",
            missing_ok=True,
        )

    async def _has_active_runs(
        self, cfg: _Config, owner: str, instance: dict[str, Any]
    ) -> bool:
        if instance.get("status") != "RUNNING":
            return False
        try:
            env = self._environment(cfg, owner, instance)
            await asyncio.to_thread(env.headers)
            return (await env.activity()).active_runs > 0
        except Exception:
            return False

    async def ensure(self, user_id: str) -> Environment:
        owner = owner_id(user_id)
        async with self._locks.hold(owner):
            cfg = await asyncio.to_thread(_config)
            current = template_label(cfg.template)
            instance = await self._instance(cfg, owner)
            labels = (instance or {}).get("labels") or {}
            if (
                instance is not None
                and labels.get(TEMPLATE_LABEL) != current
                and not await self._has_active_runs(cfg, owner, instance)
            ):
                await self._delete_keeping_disk(cfg, owner, instance)
                instance = None
                self._notices[owner] = (
                    "Your computer was recreated from a newer image; "
                    "everything in your home directory was kept."
                )
            if instance is None:
                await self._ensure_disk(cfg, owner)
                await self._insert(cfg, owner, user_id)
            instance = await self._until_running(cfg, owner)
            await self._grant_ssh(cfg, owner, user_id, instance)
            env = self._environment(cfg, owner, instance)
            await asyncio.to_thread(env.headers)
            await wait_for_daemon(
                env.base_url,
                headers=env.headers,
                transport=self._transport,
                sleep=self._sleep,
            )
            return env

    def _environment(
        self, cfg: _Config, owner: str, instance: dict[str, Any]
    ) -> SandboxEnvironment:
        interfaces = instance.get("networkInterfaces") or [{}]
        ip = str(interfaces[0].get("networkIP") or "")
        if not ip:
            raise ComputerUnavailableError(
                f"{instance_name(owner, cfg.deployment)} has no internal IP."
            )
        token_source = self._id_token
        if token_source is None:
            audience = computer_auth_audience(owner)
            token_source = self._id_tokens.setdefault(
                audience, IdTokenSource(audience)
            )
        return SandboxEnvironment(
            name=instance_name(owner, cfg.deployment),
            url=f"http://{ip}:{COMPUTER_DAEMON_PORT}",
            transport=self._transport,
            token_source=token_source,
        )

    async def _ensure_disk(self, cfg: _Config, owner: str) -> None:
        url = f"{cfg.zone_url}/disks"
        if await self._api(
            "GET", f"{url}/{disk_name(owner, cfg.deployment)}", missing_ok=True
        ):
            return
        body: dict[str, Any] = {
            "name": disk_name(owner, cfg.deployment),
            "sizeGb": str(cfg.disk_gb),
            "type": f"projects/{cfg.project}/zones/{cfg.zone}/diskTypes/pd-balanced",
            "labels": computer_labels(owner, cfg.deployment),
        }
        if cfg.snapshot_policy:
            body["resourcePolicies"] = [cfg.snapshot_policy]
        await self._wait(cfg, await self._api("POST", url, body=body))

    def _computer_env(self, cfg: _Config, user_id: str) -> str:
        values = {
            "COMPUTER_REQUIRE_AUTH": "1",
            "COMPUTER_AUTH_AUDIENCE": computer_auth_audience(user_id),
            "COMPUTER_ALLOWED_CALLERS": caller_service_account(cfg.project),
            "COMPUTER_MCP_UPSTREAM": (os.getenv("APP_URL") or "").strip(),
            "COMPUTER_MCP_AUDIENCE": (
                os.getenv("COMPUTER_MCP_AUDIENCE") or ""
            ).strip(),
            "DOTFILES_REPO": self._dotfiles_repo(user_id),
            "GOOGLE_CLOUD_PROJECT": cfg.project,
            "GOOGLE_CLOUD_LOCATION": cfg.location,
        }
        return "\n".join(f"{k}={v}" for k, v in values.items() if v)

    async def _insert(self, cfg: _Config, owner: str, user_id: str) -> None:
        template = await self._api("GET", f"{COMPUTE_API}/{cfg.template}") or {}
        props = template.get("properties") or {}
        # Instance insert fields replace template fields, so merge template disks, metadata, and labels.
        disks = [
            _instance_disk(disk, cfg)
            for disk in props.get("disks") or []
            if disk.get("deviceName") != HOME_DEVICE
        ]
        disks.append(
            {
                "source": f"projects/{cfg.project}/zones/{cfg.zone}/disks/{disk_name(owner, cfg.deployment)}",
                "deviceName": HOME_DEVICE,
                "boot": False,
                "autoDelete": False,
                "mode": "READ_WRITE",
                "type": "PERSISTENT",
            }
        )
        items = [
            item
            for item in (props.get("metadata") or {}).get("items") or []
            if item.get("key") != ENV_METADATA_KEY
        ]
        items.append(
            {"key": ENV_METADATA_KEY, "value": self._computer_env(cfg, user_id)}
        )
        body = {
            "name": instance_name(owner, cfg.deployment),
            "labels": {
                **(props.get("labels") or {}),
                **computer_labels(owner, cfg.deployment),
                TEMPLATE_LABEL: template_label(cfg.template),
            },
            "metadata": {"items": items},
            "disks": disks,
        }
        op = await self._api(
            "POST",
            f"{cfg.zone_url}/instances",
            params={"sourceInstanceTemplate": cfg.template},
            body=body,
        )
        await self._wait(cfg, op)

    async def _delete_keeping_disk(
        self, cfg: _Config, owner: str, instance: dict[str, Any]
    ) -> None:
        url = f"{cfg.zone_url}/instances/{instance_name(owner, cfg.deployment)}"
        if any(
            disk.get("deviceName") == HOME_DEVICE and disk.get("autoDelete")
            for disk in instance.get("disks") or []
        ):
            await self._wait(
                cfg,
                await self._api(
                    "POST",
                    f"{url}/setDiskAutoDelete",
                    params={"autoDelete": "false", "deviceName": HOME_DEVICE},
                ),
            )
        await self._wait(cfg, await self._api("DELETE", url))

    async def _until_running(self, cfg: _Config, owner: str) -> dict[str, Any]:
        url = f"{cfg.zone_url}/instances/{instance_name(owner, cfg.deployment)}"
        for _ in range(_RUNNING_POLLS):
            instance = await self._instance(cfg, owner)
            if instance is None:
                raise ComputerUnavailableError(
                    f"{instance_name(owner, cfg.deployment)} disappeared while starting."
                )
            status = str(instance.get("status") or "")
            if status == "RUNNING":
                return instance
            if call := _START_CALL.get(status):
                await self._wait(cfg, await self._api("POST", f"{url}/{call}"))
                continue
            await self._sleep(2.0)
        raise ComputerUnavailableError(
            f"{instance_name(owner, cfg.deployment)} did not reach RUNNING."
        )

    async def _grant_ssh(
        self,
        cfg: _Config,
        owner: str,
        user_id: str,
        instance: dict[str, Any],
    ) -> None:
        if "@" not in user_id:
            return
        member = f"user:{user_id}"
        try:
            key = (
                owner,
                str(instance.get("id") or instance_name(owner, cfg.deployment)),
            )
            if key not in self._granted_instances:
                await self._grant(
                    f"{cfg.zone_url}/instances/{instance_name(owner, cfg.deployment)}/getIamPolicy",
                    f"{cfg.zone_url}/instances/{instance_name(owner, cfg.deployment)}/setIamPolicy",
                    "roles/compute.osAdminLogin",
                    member,
                    get_method="GET",
                )
                tunnel = (
                    f"{IAP_API}/projects/{cfg.project}/iap_tunnel/zones/"
                    f"{cfg.zone}/instances/{instance_name(owner, cfg.deployment)}"
                )
                await self._grant(
                    f"{tunnel}:getIamPolicy",
                    f"{tunnel}:setIamPolicy",
                    "roles/iap.tunnelResourceAccessor",
                    member,
                )
                self._granted_instances.add(key)
            if owner not in self._granted_sa:
                sa = cfg.computer_sa or await self._template_service_account(
                    cfg
                )
                if sa:
                    resource = f"{IAM_API}/projects/-/serviceAccounts/{sa}"
                    await self._grant(
                        f"{resource}:getIamPolicy",
                        f"{resource}:setIamPolicy",
                        "roles/iam.serviceAccountUser",
                        member,
                    )
                self._granted_sa.add(owner)
        except ComputerUnavailableError:
            logger.warning(
                "Could not grant SSH access to %s",
                instance_name(owner, cfg.deployment),
                exc_info=True,
            )

    async def _template_service_account(self, cfg: _Config) -> str:
        template = await self._api("GET", f"{COMPUTE_API}/{cfg.template}") or {}
        accounts = (template.get("properties") or {}).get(
            "serviceAccounts"
        ) or []
        return str(accounts[0].get("email") or "") if accounts else ""

    async def _grant(
        self,
        get_url: str,
        set_url: str,
        role: str,
        member: str,
        *,
        get_method: str = "POST",
    ) -> None:
        for attempt in range(3):
            if get_method == "GET":
                policy = await self._api(
                    "GET", get_url, params={"optionsRequestedPolicyVersion": 3}
                )
            else:
                policy = await self._api(
                    "POST",
                    get_url,
                    body={"options": {"requestedPolicyVersion": 3}},
                )
            policy = policy or {}
            if not _add_binding(policy, role, member):
                return
            policy["version"] = 3
            try:
                await self._api("POST", set_url, body={"policy": policy})
                return
            except ComputerUnavailableError as exc:
                if "HTTP 409" not in str(exc) or attempt == 2:
                    raise

    async def _probe_daemon(self, env: SandboxEnvironment) -> bool:
        import httpx

        from app.app_utils.http_client import get_pooled_http_client

        try:
            headers = await asyncio.to_thread(env.headers)
            async with get_pooled_http_client(
                env.base_url, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{env.base_url}/healthz",
                    headers=headers,
                    timeout=httpx.Timeout(2.0, connect=2.0),
                )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def running_environment(self, user_id: str) -> Environment | None:
        from app.workers.providers.last_known import record_status

        owner = owner_id(user_id)
        cfg = await asyncio.to_thread(_config)
        instance = await self._instance(cfg, owner)
        if not instance or instance.get("status") != "RUNNING":
            return None
        env = self._environment(cfg, owner, instance)
        if not await self._probe_daemon(env):
            record_status(owner, ComputerState.STARTING)
            return None
        return env

    def pop_notice(self, user_id: str) -> str | None:
        return self._notices.pop(owner_id(user_id), None)

    async def status(self, user_id: str) -> ComputerStatus:
        owner = owner_id(user_id)
        cfg = await asyncio.to_thread(_config)
        instance = await self._instance(cfg, owner)
        if instance is None:
            return ComputerStatus(ComputerState.MISSING)
        raw = str(instance.get("status") or "")
        detail = raw
        labels = instance.get("labels") or {}
        if labels.get(TEMPLATE_LABEL) != template_label(cfg.template):
            detail = f"{raw}; recreated from the newer template on next use"
        state = instance_state(raw)
        if state is ComputerState.RUNNING:
            env = self._environment(cfg, owner, instance)
            if not await self._probe_daemon(env):
                state = ComputerState.STARTING
        return ComputerStatus(state, detail)

    async def stop(self, user_id: str) -> None:
        owner = owner_id(user_id)
        cfg = await asyncio.to_thread(_config)
        instance = await self._instance(cfg, owner)
        if instance and instance.get("status") in _LIVE:
            await self._api(
                "POST",
                f"{cfg.zone_url}/instances/{instance_name(owner, cfg.deployment)}/stop",
            )

    async def known_users(self) -> list[str]:
        cfg = await asyncio.to_thread(_config)
        owners: set[str] = set()
        params: dict[str, Any] = {"filter": computer_filter(cfg.deployment)}
        while True:
            page = await self._api(
                "GET", f"{cfg.zone_url}/instances", params=params
            )
            for instance in (page or {}).get("items") or []:
                if owner := (instance.get("labels") or {}).get(OWNER_LABEL):
                    owners.add(str(owner))
            token = (page or {}).get("nextPageToken")
            if not token:
                return sorted(owners)
            params = {**params, "pageToken": token}
