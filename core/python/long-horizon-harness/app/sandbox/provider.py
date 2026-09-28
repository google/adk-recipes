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

"""``SandboxProvider`` — the per-user provisioning/reattach/snapshot/upgrade interface.

``VertexSandboxProvider`` composes ``app/sandbox/lifecycle.py`` against Vertex
Agent Runtime; ``LocalProvider`` is the host-dev fallback. ``session_start``
orchestrates the env cache / locks / ContextVar over whichever provider is active
(``set_sandbox_provider`` in ``environment_context.py`` lets an external backend
register its own). Providers return ``(env, PendingMigration | None)`` and let the
orchestrator own ``initialize`` / migrate / cache — so the provider stays
free of session_start's process state.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.environment import Environment, LocalEnvironment
from app.infrastructure.env import env_flag, env_str

if TYPE_CHECKING:
    from app.routines.run_context import RoutineRun

logger = logging.getLogger(__name__)

SANDBOX_LOCATION = "us-central1"
DEFAULT_SANDBOX_ENGINE_DISPLAY_NAME = "lha-sandbox-host"
DEFAULT_SANDBOX_TTL = "14d"
# Vertex doesn't expose a TTL for the X-Sandbox-Routing-Token header, but the LB
# starts returning 401 well inside an hour; 30 min preempts that.
_ROUTING_TOKEN_REFRESH_INTERVAL_SECS = 30 * 60

_engine_cache: dict[str, str] = {}
_template_cache: dict[str, str] = {}


class SandboxConfigurationError(RuntimeError):
    """Raised when ``LHA_ENVIRONMENT_BACKEND=sandbox`` is selected but cannot be
    honored. Hard-fails in prod (``K_SERVICE`` set); dev degrades to local."""


@dataclass(frozen=True)
class PendingMigration:
    """A prior-version sandbox to copy ``/workspace`` from into the freshly
    provisioned one, before retiring it. The orchestrator runs the copy async
    after the new sandbox is initialized (``session_start._migrate_workspace``)."""

    client: Any
    source_env: Any
    source_name: str


@dataclass(frozen=True)
class UpgradeProvision:
    """Result of the Vertex half of ``/sandbox-upgrade``. ``status`` is one of
    ``provisioned`` (caller finalizes ``new_env`` + optional ``pending``),
    ``already_current``, ``no_sandbox``, ``unavailable``."""

    status: str
    version: str = ""
    new_env: Any = None
    pending: PendingMigration | None = None
    reason: str = ""


def _in_prod() -> bool:
    return bool(os.environ.get("K_SERVICE"))


def default_internet_access() -> bool:
    """Sandbox egress default: hermetic in prod, on in local dev so ``make dev``
    works out of the box. ``LHA_SANDBOX_INTERNET_ACCESS`` overrides either way."""
    return env_flag("LHA_SANDBOX_INTERNET_ACCESS", default=not _in_prod())


def _default_local_root() -> Path:
    override = os.environ.get("LHA_LOCAL_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".lha"


def _sandbox_location() -> str:
    """Vertex region for sandbox provisioning (``LHA_SANDBOX_LOCATION`` overrides
    the ``us-central1`` default) — read at call time so tests can monkeypatch."""
    return env_str("LHA_SANDBOX_LOCATION", SANDBOX_LOCATION)


def snapshots_enabled() -> bool:
    """Master switch for Phase C (snapshot-based TTL survival)."""
    return env_flag("LHA_SNAPSHOT_ENABLED")


def _vertex_client_factory() -> Any:
    """Indirection so tests can swap in a stub client without touching GCP."""
    import vertexai

    return vertexai.Client(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=_sandbox_location(),
        http_options={"api_version": "v1beta1"},
    )


def _resolve_sandbox_engine(client: Any) -> str:
    """Return the reasoning-engine resource name the sandbox lives under.

    Explicit ``AGENT_ENGINE_RESOURCE_NAME`` wins; otherwise look up by
    ``LHA_SANDBOX_ENGINE_NAME`` (default ``lha-sandbox-host``) and create one if
    missing. Cached per process."""
    pinned = os.environ.get("AGENT_ENGINE_RESOURCE_NAME", "").strip()
    if pinned:
        return pinned

    display_name = (
        os.environ.get("LHA_SANDBOX_ENGINE_NAME", "").strip()
        or DEFAULT_SANDBOX_ENGINE_DISPLAY_NAME
    )
    cached = _engine_cache.get(display_name)
    if cached:
        return cached

    for engine in client.agent_engines.list():
        api_resource = getattr(engine, "api_resource", None)
        if api_resource is None:
            continue
        if getattr(api_resource, "display_name", None) != display_name:
            continue
        name = getattr(api_resource, "name", None)
        if not name:
            continue
        _engine_cache[display_name] = str(name)
        return str(name)

    from vertexai._genai.types import AgentEngineConfig

    created = client.agent_engines.create(
        config=AgentEngineConfig(display_name=display_name)
    )
    api_resource = getattr(created, "api_resource", None)
    name = getattr(api_resource, "name", None) if api_resource else None
    if not name:
        raise RuntimeError(
            "agent_engines.create returned no api_resource.name; "
            "cannot resolve sandbox engine"
        )
    _engine_cache[display_name] = str(name)
    return str(name)


def _connection_info_from_sandbox(sandbox_obj: Any) -> tuple[str, str] | None:
    conn = getattr(sandbox_obj, "connection_info", None)
    if conn is None:
        return None
    lb_host = getattr(conn, "load_balancer_hostname", None)
    routing_token = getattr(conn, "routing_token", None)
    if not lb_host or not routing_token:
        return None
    return str(lb_host), str(routing_token)


def _sandbox_is_running(sandbox_obj: Any) -> bool:
    state = getattr(sandbox_obj, "state", None)
    if state is None:
        return True
    name = getattr(state, "name", None) or str(state)
    return "RUNNING" in name


def _try_reattach(
    client: Any, sandbox_name: str
) -> tuple[str, str, str] | None:
    """Returns ``(lb_host, routing_token, template_ref)`` for a RUNNING sandbox,
    else ``None``. The template_ref lets the caller recover the egress mode."""
    try:
        sandbox_obj = client.agent_engines.sandboxes.get(name=sandbox_name)
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code == 404:
            logger.info("reattach: sandbox %s no longer exists", sandbox_name)
        else:
            logger.warning(
                "reattach: sandboxes.get(%s) failed (%s)", sandbox_name, exc
            )
        return None
    if not _sandbox_is_running(sandbox_obj):
        logger.warning("reattach: sandbox %s is not RUNNING", sandbox_name)
        return None
    conn = _connection_info_from_sandbox(sandbox_obj)
    if conn is None:
        return None
    template_ref = getattr(sandbox_obj, "sandbox_environment_template", None)
    return conn[0], conn[1], str(template_ref or "")


def _template_internet_access(
    client: Any, template_ref: str, *, default: bool
) -> bool:
    """Egress mode of a template by ref (one get); ``default`` on empty ref/failure."""
    from app.sandbox.lifecycle import template_internet_access

    if not template_ref:
        return default
    try:
        tmpl = client.agent_engines.sandboxes.templates.get(name=template_ref)
    except Exception:
        return default
    return template_internet_access(tmpl, default=default)


def _sandbox_template(client: Any, sandbox_name: str) -> Any | None:
    """Hydrated template backing a sandbox, or ``None`` if it's gone or
    template-less (snapshot-restored sandboxes have no template ref)."""
    try:
        obj = client.agent_engines.sandboxes.get(name=sandbox_name)
    except Exception:
        return None
    template_ref = getattr(obj, "sandbox_environment_template", None)
    if not template_ref:
        return None
    try:
        return client.agent_engines.sandboxes.templates.get(
            name=str(template_ref)
        )
    except Exception:
        return None


def _sandbox_internet_access(
    client: Any, sandbox_name: str, *, default: bool
) -> bool:
    """Recover a sandbox's egress mode via its template — the create API rejects a
    ``description`` stamp, so the template is the channel; ``default`` if gone."""
    from app.sandbox.lifecycle import template_internet_access

    tmpl = _sandbox_template(client, sandbox_name)
    if tmpl is None:
        return default
    return template_internet_access(tmpl, default=default)


def _build_refresh_closures(
    client_factory: Callable[[], Any], *, caller_sa: str, sandbox_name: str
) -> tuple[Callable[[], str], Callable[[], str]]:
    """Per-turn refresh minters injected into a SandboxEnvironment. Keep every
    Vertex type behind the abstraction; ``routing_fetcher`` maps a 404 to the generic
    ``BackendGoneError`` the env catches to signal eviction."""
    from app.environment.process import BackendGoneError

    def token_minter() -> str:
        from app.sandbox.lifecycle import mint_sandbox_token

        return mint_sandbox_token(client_factory(), service_account=caller_sa)

    def routing_fetcher() -> str:
        from app.sandbox import lifecycle

        try:
            return lifecycle.fetch_routing_token(
                client_factory(), sandbox_name=sandbox_name
            )
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(
                exc, "status_code", None
            )
            if code == 404:
                raise BackendGoneError() from exc
            raise

    return token_minter, routing_fetcher


def _reattach_or_none(
    client: Any, sandbox_name: str | None, *, caller_sa: str, user_id: str
) -> Any | None:
    if not sandbox_name:
        return None
    reattach = _try_reattach(client, sandbox_name)
    if reattach is None:
        return None
    lb_host, routing_token, template_ref = reattach

    from app.environment.sandbox import SandboxEnvironment
    from app.sandbox.lifecycle import mint_sandbox_token

    # Legacy sandboxes predate the hermetic templates and were internet-on.
    internet_access = _template_internet_access(
        client, template_ref, default=True
    )
    sandbox_token = mint_sandbox_token(client, service_account=caller_sa)
    minter, fetcher = _build_refresh_closures(
        _vertex_client_factory, caller_sa=caller_sa, sandbox_name=sandbox_name
    )
    return SandboxEnvironment(
        client=client,
        sandbox_name=sandbox_name,
        lb_host=lb_host,
        routing_token=routing_token,
        sandbox_token=sandbox_token,
        owner=user_id,
        token_minter=minter,
        routing_fetcher=fetcher,
        routing_refresh_interval=_ROUTING_TOKEN_REFRESH_INTERVAL_SECS,
        internet_access=internet_access,
    )


def _restore_from_snapshot_or_none(
    client: Any, *, agent_instance: str, user_id: str, caller_sa: str
) -> Environment | None:
    if not snapshots_enabled():
        return None

    from app.environment.sandbox import SandboxEnvironment
    from app.sandbox.lifecycle import (
        find_latest_user_snapshot,
        mint_sandbox_token,
        restore_sandbox_from_snapshot,
        sandbox_display_name_from_snapshot,
    )

    found = find_latest_user_snapshot(
        client, agent_instance_name=agent_instance, user_id=user_id
    )
    if found is None:
        return None
    snapshot_name, snapshot_display = found
    try:
        provisioned = restore_sandbox_from_snapshot(
            client,
            agent_instance_name=agent_instance,
            owner=user_id,
            snapshot_name=snapshot_name,
            display_name=sandbox_display_name_from_snapshot(snapshot_display),
            ttl=os.environ.get("LHA_SANDBOX_TTL", "").strip()
            or DEFAULT_SANDBOX_TTL,
        )
    except Exception as exc:
        logger.warning(
            "snapshot restore from %s failed (%s); provisioning fresh instead",
            snapshot_name,
            exc,
        )
        return None

    sandbox_token = mint_sandbox_token(client, service_account=caller_sa)
    minter, fetcher = _build_refresh_closures(
        _vertex_client_factory,
        caller_sa=caller_sa,
        sandbox_name=provisioned.name,
    )
    # Snapshot-restored sandboxes have no template, so egress is unknowable here —
    # this flag is a best-effort label; a toggle reprovisions correctly. Phase C.
    return SandboxEnvironment(
        client=client,
        sandbox_name=provisioned.name,
        lb_host=provisioned.lb_host,
        routing_token=provisioned.routing_token,
        sandbox_token=sandbox_token,
        owner=user_id,
        token_minter=minter,
        routing_fetcher=fetcher,
        routing_refresh_interval=_ROUTING_TOKEN_REFRESH_INTERVAL_SECS,
        internet_access=default_internet_access(),
    )


def _provision_fresh_sandbox(
    client: Any,
    *,
    agent_instance: str,
    user_id: str,
    image_uri: str,
    caller_sa: str,
    internet_access: bool,
    prior_name: str | None = None,
    display_name: str | None = None,
) -> tuple[Environment, PendingMigration | None]:
    from app.environment.sandbox import SandboxEnvironment
    from app.sandbox.lifecycle import (
        ensure_template,
        mint_sandbox_token,
        provision_sandbox,
        sandbox_display_name,
    )

    source_env = _reattach_or_none(
        client, prior_name, caller_sa=caller_sa, user_id=user_id
    )

    # Cache templates per egress mode (egress isn't in ensure_template's match key).
    mode = "net" if internet_access else "hermetic"
    cache_key = f"{agent_instance}|{image_uri}|{mode}"
    template_name = _template_cache.get(cache_key)
    if template_name is None:
        template_name = ensure_template(
            client,
            agent_instance_name=agent_instance,
            image_uri=image_uri,
            internet_access=internet_access,
        )
        _template_cache[cache_key] = template_name

    provisioned = provision_sandbox(
        client,
        agent_instance_name=agent_instance,
        owner=user_id,
        template_name=template_name,
        display_name=display_name or sandbox_display_name(user_id, image_uri),
        ttl=os.environ.get("LHA_SANDBOX_TTL", "").strip()
        or DEFAULT_SANDBOX_TTL,
    )

    sandbox_token = mint_sandbox_token(client, service_account=caller_sa)
    minter, fetcher = _build_refresh_closures(
        _vertex_client_factory,
        caller_sa=caller_sa,
        sandbox_name=provisioned.name,
    )
    new_env = SandboxEnvironment(
        client=client,
        sandbox_name=provisioned.name,
        lb_host=provisioned.lb_host,
        routing_token=provisioned.routing_token,
        sandbox_token=sandbox_token,
        owner=user_id,
        token_minter=minter,
        routing_fetcher=fetcher,
        routing_refresh_interval=_ROUTING_TOKEN_REFRESH_INTERVAL_SECS,
        internet_access=internet_access,
    )
    pending = (
        PendingMigration(
            client=client, source_env=source_env, source_name=prior_name
        )
        if source_env is not None and prior_name
        else None
    )
    return new_env, pending


def _cached_agent_instance() -> str | None:
    """Resolve the sandbox engine name without making create/list RPCs."""
    pinned = os.environ.get("AGENT_ENGINE_RESOURCE_NAME", "").strip()
    if pinned:
        return pinned
    display_name = (
        os.environ.get("LHA_SANDBOX_ENGINE_NAME", "").strip()
        or DEFAULT_SANDBOX_ENGINE_DISPLAY_NAME
    )
    return _engine_cache.get(display_name)


@runtime_checkable
class SandboxProvider(Protocol):
    """Per-user provisioning contract session_start orchestrates against. An
    external backend implements this and registers via ``set_sandbox_provider``."""

    def build_environment(
        self, user_id: str
    ) -> tuple[Environment, PendingMigration | None]: ...

    def build_routine_environment(
        self, run: RoutineRun
    ) -> tuple[Environment, PendingMigration | None]: ...

    def provision_upgrade(self, user_id: str) -> UpgradeProvision: ...

    def snapshot_and_prune(self, user_id: str) -> dict[str, Any]: ...

    def provisioning_status(self, user_id: str) -> dict[str, Any] | None: ...

    def will_provision(self) -> bool: ...


class LocalProvider:
    """Host-filesystem backend: no provisioning, no auth, no snapshots."""

    def build_environment(
        self, user_id: str
    ) -> tuple[Environment, PendingMigration | None]:
        return (
            LocalEnvironment(
                working_dir=_default_local_root() / "users" / user_id
            ),
            None,
        )

    def build_routine_environment(
        self, run: RoutineRun
    ) -> tuple[Environment, PendingMigration | None]:
        return (
            LocalEnvironment(
                working_dir=_default_local_root() / "routines" / run.routine_id
            ),
            None,
        )

    def provision_upgrade(self, user_id: str) -> UpgradeProvision:
        return UpgradeProvision(
            status="unavailable", reason="sandbox backend not enabled"
        )

    def snapshot_and_prune(self, user_id: str) -> dict[str, Any]:
        return {"status": "unavailable"}

    def provisioning_status(self, user_id: str) -> dict[str, Any] | None:
        return None

    def will_provision(self) -> bool:
        return False


class VertexSandboxProvider:
    """Vertex Agent Runtime backend: composes ``lifecycle.py`` for reattach,
    provision, snapshot-restore, upgrade, and auth refresh."""

    def will_provision(self) -> bool:
        return bool(
            os.environ.get("LHA_RUNTIME_IMAGE", "").strip()
            and os.environ.get("LHA_SANDBOX_CALLER_SA", "").strip()
        )

    def build_environment(
        self, user_id: str
    ) -> tuple[Environment, PendingMigration | None]:
        from app.sandbox.lifecycle import (
            find_latest_user_sandbox,
            version_below_floor,
        )

        image_uri = os.environ.get("LHA_RUNTIME_IMAGE", "").strip()
        caller_sa = os.environ.get("LHA_SANDBOX_CALLER_SA", "").strip()

        missing = [
            var
            for var, val in (
                ("LHA_RUNTIME_IMAGE", image_uri),
                ("LHA_SANDBOX_CALLER_SA", caller_sa),
            )
            if not val
        ]
        if missing:
            msg = (
                f"LHA_ENVIRONMENT_BACKEND=sandbox requires {', '.join(missing)}"
            )
            if _in_prod():
                raise SandboxConfigurationError(msg)
            logger.warning("%s; falling back to LocalEnvironment", msg)
            return (
                LocalEnvironment(
                    working_dir=_default_local_root() / "users" / user_id
                ),
                None,
            )

        try:
            client = _vertex_client_factory()
            agent_instance = _resolve_sandbox_engine(client)
        except Exception as exc:
            if _in_prod():
                raise SandboxConfigurationError(
                    f"sandbox engine resolution failed: {exc}"
                ) from exc
            logger.warning(
                "LHA_ENVIRONMENT_BACKEND=sandbox but engine resolution failed (%s); "
                "falling back to LocalEnvironment",
                exc,
            )
            return (
                LocalEnvironment(
                    working_dir=_default_local_root() / "users" / user_id
                ),
                None,
            )

        latest = find_latest_user_sandbox(
            client, agent_instance_name=agent_instance, user_id=user_id
        )
        prior_name: str | None = None
        if latest is not None:
            latest_name, latest_display = latest
            floor = os.environ.get("LHA_RUNTIME_MIN_VERSION", "").strip()
            if floor and version_below_floor(latest_display, floor):
                prior_name = latest_name
            else:
                env = _reattach_or_none(
                    client, latest_name, caller_sa=caller_sa, user_id=user_id
                )
                if env is not None:
                    return env, None

        if prior_name is None:
            restored = _restore_from_snapshot_or_none(
                client,
                agent_instance=agent_instance,
                user_id=user_id,
                caller_sa=caller_sa,
            )
            if restored is not None:
                return restored, None

        return _provision_fresh_sandbox(
            client,
            agent_instance=agent_instance,
            user_id=user_id,
            image_uri=image_uri,
            caller_sa=caller_sa,
            internet_access=default_internet_access(),
            prior_name=prior_name,
        )

    def build_routine_environment(
        self, run: RoutineRun
    ) -> tuple[Environment, PendingMigration | None]:
        from app.sandbox.lifecycle import (
            find_routine_sandbox,
            routine_display_name,
        )

        image_uri = os.environ.get("LHA_RUNTIME_IMAGE", "").strip()
        caller_sa = os.environ.get("LHA_SANDBOX_CALLER_SA", "").strip()
        if not image_uri or not caller_sa:
            raise SandboxConfigurationError(
                "routine sandbox needs LHA_RUNTIME_IMAGE and LHA_SANDBOX_CALLER_SA"
            )
        client = _vertex_client_factory()
        agent_instance = _resolve_sandbox_engine(client)

        existing = find_routine_sandbox(
            client,
            agent_instance_name=agent_instance,
            routine_id=run.routine_id,
            image_uri=image_uri,
        )
        if existing is not None:
            env = _reattach_or_none(
                client, existing, caller_sa=caller_sa, user_id=run.owner
            )
            if env is not None:
                return env, None

        return _provision_fresh_sandbox(
            client,
            agent_instance=agent_instance,
            user_id=run.owner,
            image_uri=image_uri,
            caller_sa=caller_sa,
            internet_access=default_internet_access(),
            display_name=routine_display_name(run.routine_id, image_uri),
        )

    def provision_upgrade(self, user_id: str) -> UpgradeProvision:
        from app.sandbox.lifecycle import (
            find_prior_user_sandbox,
            find_user_sandbox,
            runtime_image_version,
        )

        if not self.will_provision():
            return UpgradeProvision(
                status="unavailable", reason="sandbox backend not enabled"
            )

        image_uri = os.environ.get("LHA_RUNTIME_IMAGE", "").strip()
        caller_sa = os.environ.get("LHA_SANDBOX_CALLER_SA", "").strip()
        version = runtime_image_version(image_uri)
        try:
            client = _vertex_client_factory()
            agent_instance = _resolve_sandbox_engine(client)
        except Exception as exc:
            logger.warning(
                "provision_upgrade: engine resolution failed: %s", exc
            )
            return UpgradeProvision(
                status="unavailable", reason="engine resolution failed"
            )

        if find_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id=user_id,
            image_uri=image_uri,
        ):
            return UpgradeProvision(status="already_current", version=version)

        prior_name = find_prior_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id=user_id,
            current_image_uri=image_uri,
        )
        if not prior_name:
            return UpgradeProvision(status="no_sandbox", version=version)

        # An image upgrade must preserve the sandbox's current egress mode — read
        # it off the prior sandbox rather than resetting to the default.
        internet_access = _sandbox_internet_access(
            client, prior_name, default=default_internet_access()
        )
        new_env, pending = _provision_fresh_sandbox(
            client,
            agent_instance=agent_instance,
            user_id=user_id,
            image_uri=image_uri,
            caller_sa=caller_sa,
            internet_access=internet_access,
            prior_name=prior_name,
        )
        return UpgradeProvision(
            status="provisioned",
            version=version,
            new_env=new_env,
            pending=pending,
        )

    def snapshot_and_prune(self, user_id: str) -> dict[str, Any]:
        from app.sandbox.lifecycle import snapshot_and_prune_user

        if not self.will_provision():
            return {"status": "unavailable"}
        client = _vertex_client_factory()
        agent_instance = _resolve_sandbox_engine(client)
        keep_raw = os.environ.get("LHA_SNAPSHOT_KEEP", "").strip()
        keep = int(keep_raw) if keep_raw.isdigit() else 2
        return snapshot_and_prune_user(
            client,
            agent_instance_name=agent_instance,
            user_id=user_id,
            snapshot_ttl=os.environ.get("LHA_SNAPSHOT_TTL", "").strip()
            or "30d",
            keep=keep,
        )

    def provisioning_status(self, user_id: str) -> dict[str, Any] | None:
        if not self.will_provision():
            return None
        agent_instance = _cached_agent_instance()
        if not agent_instance:
            return None
        try:
            from app.sandbox.lifecycle import get_user_sandbox_state

            client = _vertex_client_factory()
            return get_user_sandbox_state(
                client,
                agent_instance_name=agent_instance,
                user_id=user_id,
                image_uri=os.environ.get("LHA_RUNTIME_IMAGE", "").strip(),
            )
        except Exception as exc:
            logger.warning("provisioning_status: Vertex query failed: %s", exc)
            return None


def _build_sandbox_environment(
    user_id: str,
) -> tuple[Environment, PendingMigration | None]:
    """Module-level convenience over ``VertexSandboxProvider().build_environment``."""
    return VertexSandboxProvider().build_environment(user_id)


def _build_routine_sandbox_environment(
    run: RoutineRun,
) -> tuple[Environment, PendingMigration | None]:
    """Module-level convenience over ``VertexSandboxProvider().build_routine_environment``."""
    return VertexSandboxProvider().build_routine_environment(run)


__all__ = [
    "DEFAULT_SANDBOX_TTL",
    "LocalProvider",
    "PendingMigration",
    "SandboxConfigurationError",
    "SandboxProvider",
    "UpgradeProvision",
    "VertexSandboxProvider",
    "snapshots_enabled",
]
