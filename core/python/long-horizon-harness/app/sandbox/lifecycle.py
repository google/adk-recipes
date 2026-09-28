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

"""Sandbox lifecycle helpers: provision, delete, per-user discovery.

Pure functions on top of the Vertex Agent Runtime SDK so the session-start
wiring can compose them without dragging in module-level GCP state. The
caller (``conversation/session_start.py``) constructs the
``vertexai.Client`` and threads it through.

Per-user sandbox discovery is done via ``sandboxes.list`` filtered by a
deterministic ``display_name`` (see ``sandbox_display_name``) — no local
registry, so Cloud Run cold starts and multi-instance deploys still find
the right sandbox.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


_TTL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_TTL_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _normalize_ttl(ttl: str) -> str:
    """Convert friendly TTL (``14d``, ``336h``, ``900s``) to Protobuf Duration (``<N>s``).

    Vertex's sandbox API rejects anything that doesn't end in ``s``.
    """
    m = _TTL_RE.match(ttl)
    if not m:
        raise ValueError(
            f"Invalid TTL {ttl!r}: expected <number><s|m|h|d> (e.g. '14d', '900s')"
        )
    return f"{int(m.group(1)) * _TTL_MULT[m.group(2).lower()]}s"


@dataclass(frozen=True)
class ProvisionedSandbox:
    """The three things the caller needs to talk to a freshly-created sandbox:
    the resource name (for snapshot/delete RPCs) and the LB hostname + routing
    token (for ``Authorization``/``X-Sandbox-Routing-Token`` on every shim
    request)."""

    name: str
    lb_host: str
    routing_token: str


def _is_not_found(exc: BaseException) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 404


def _operation_resource_name(operation: Any) -> str:
    response = getattr(operation, "response", None)
    if response is None:
        raise RuntimeError(
            "Sandbox operation returned no response; cannot extract resource name"
        )
    name = getattr(response, "name", None)
    if not name:
        raise RuntimeError("Sandbox operation response missing 'name' field")
    return str(name)


DEFAULT_TEMPLATE_DISPLAY_NAME = "lha-default"
DEFAULT_RUNTIME_PORT = 8080


def template_display_name(internet_access: bool) -> str:
    """Per-egress-mode template name (``-net`` / ``-hermetic``); see
    ``ensure_template`` for why the variants must differ."""
    return f"{DEFAULT_TEMPLATE_DISPLAY_NAME}-{'net' if internet_access else 'hermetic'}"


def template_internet_access(template: Any, *, default: bool) -> bool:
    """Egress mode off a hydrated template: ``egress_control_config.internet_access``,
    else the display_name suffix, else ``default``."""
    egress = getattr(template, "egress_control_config", None)
    if egress is None and isinstance(template, dict):
        egress = template.get("egress_control_config")
    ia = getattr(egress, "internet_access", None)
    if ia is None and isinstance(egress, dict):
        ia = egress.get("internet_access")
    if ia is not None:
        return bool(ia)
    dn = getattr(template, "display_name", None) or ""
    if dn.endswith("-net"):
        return True
    if dn.endswith("-hermetic"):
        return False
    return default


def _template_image_uri(template: Any) -> str | None:
    """Read the image URI off a hydrated template, surviving both the SDK's
    Pydantic shape and dict-shaped stubs."""
    cce = getattr(template, "custom_container_environment", None)
    if cce is None and isinstance(template, dict):
        cce = template.get("custom_container_environment")
    if cce is None:
        return None
    spec = getattr(cce, "custom_container_spec", None)
    if spec is None and isinstance(cce, dict):
        spec = cce.get("custom_container_spec")
    if spec is None:
        return None
    image = getattr(spec, "image_uri", None)
    if image is None and isinstance(spec, dict):
        image = spec.get("image_uri")
    return str(image) if image else None


def ensure_template(
    client: Any,
    *,
    agent_instance_name: str,
    image_uri: str,
    internet_access: bool = False,
) -> str:
    """Return a sandbox template resource name, creating it if missing.

    Templates are BYOC: the container image is the entrypoint, not Vertex's
    default category. Identity is (display_name, image URI) — a template tagged
    with ``runtime:v0.3.0`` does not satisfy a caller that asked for ``v0.3.1``.
    Egress is NOT part of the match key, so the two egress variants live under
    distinct display names (see :func:`template_display_name`); default is
    hermetic (``internet_access=False``).
    """
    display_name = template_display_name(internet_access)
    existing = client.agent_engines.sandboxes.templates.list(
        name=agent_instance_name
    )
    for template in existing:
        candidate_name = getattr(template, "display_name", None)
        candidate_image = _template_image_uri(template)
        # SDK list() returns stubs missing display_name and container spec;
        # hydrate via get() before deciding the template isn't ours, otherwise
        # we'd recreate it every cold start and burn through quota.
        if candidate_name is None or candidate_image is None:
            template_name = getattr(template, "name", None)
            if not template_name:
                continue
            try:
                hydrated = client.agent_engines.sandboxes.templates.get(
                    name=str(template_name)
                )
            except Exception as exc:
                logger.warning(
                    "ensure_template: get(%s) failed during hydration: %s",
                    template_name,
                    exc,
                )
                continue
            candidate_name = getattr(hydrated, "display_name", None)
            candidate_image = _template_image_uri(hydrated)
        if candidate_name != display_name:
            continue
        if candidate_image != image_uri:
            continue
        return str(template.name)

    operation = client.agent_engines.sandboxes.templates.create(
        name=agent_instance_name,
        display_name=display_name,
        config={
            "custom_container_environment": {
                "custom_container_spec": {"image_uri": image_uri},
                "ports": [
                    {"port": DEFAULT_RUNTIME_PORT, "protocol": "TCP"},
                ],
            },
            "egress_control_config": {"internet_access": internet_access},
        },
    )
    return _operation_resource_name(operation)


def mint_sandbox_token(client: Any, *, service_account: str) -> str:
    """Mint the JWT the sandbox LB checks on every shim request.

    Thin wrapper over ``client.agent_engines.sandboxes.generate_access_token``
    so callers don't need to remember the exact method path. The caller's ADC
    must hold ``roles/iam.serviceAccountTokenCreator`` on ``service_account``
    for this to succeed; otherwise the call returns 403.
    """
    return str(
        client.agent_engines.sandboxes.generate_access_token(service_account)
    )


def fetch_routing_token(client: Any, *, sandbox_name: str) -> str:
    """Re-fetch the routing token off a running sandbox's connection_info.

    The LB's ``X-Sandbox-Routing-Token`` has its own TTL independent of
    the Bearer JWT minted by ``mint_sandbox_token``; cached envs need to
    rotate it before the LB starts returning 401 ``Routing token has
    expired``.
    """
    sandbox_obj = client.agent_engines.sandboxes.get(name=sandbox_name)
    conn = getattr(sandbox_obj, "connection_info", None)
    if conn is None:
        raise RuntimeError(
            f"sandbox {sandbox_name} has no connection_info; cannot refresh routing token"
        )
    routing_token = getattr(conn, "routing_token", None)
    if not routing_token:
        raise RuntimeError(
            f"sandbox {sandbox_name} connection_info missing routing_token"
        )
    return str(routing_token)


def provision_sandbox(
    client: Any,
    *,
    agent_instance_name: str,
    owner: str,
    template_name: str,
    display_name: str | None = None,
    ttl: str | None = None,
) -> ProvisionedSandbox:
    """Create a sandbox under ``agent_instance_name`` for ``owner`` from
    ``template_name``.

    Returns a :class:`ProvisionedSandbox` carrying the resource name plus the
    LB hostname and routing token from ``connection_info`` — both are needed
    on every subsequent shim call.
    """
    if not template_name:
        raise ValueError("provision_sandbox requires template_name")

    config: dict[str, Any] = {
        "owner": owner,
        "sandbox_environment_template": template_name,
    }
    if display_name:
        config["display_name"] = display_name
    if ttl:
        config["ttl"] = _normalize_ttl(ttl)

    operation = client.agent_engines.sandboxes.create(
        name=agent_instance_name, config=config
    )
    return _provisioned_from_operation(operation)


def delete_sandbox(client: Any, *, sandbox_name: str) -> None:
    """Best-effort delete. A 404 means the sandbox is already gone, which
    is the desired end state — so we swallow it. Other errors propagate."""
    try:
        client.agent_engines.sandboxes.delete(name=sandbox_name)
    except Exception as exc:
        if _is_not_found(exc):
            logger.info("delete_sandbox: %s already gone (404)", sandbox_name)
            return
        raise


def _provisioned_from_operation(operation: Any) -> ProvisionedSandbox:
    response = getattr(operation, "response", None)
    if response is None:
        raise RuntimeError(
            "Sandbox operation returned no response; cannot extract resource name"
        )
    name = getattr(response, "name", None)
    if not name:
        raise RuntimeError("Sandbox operation response missing 'name' field")
    conn = getattr(response, "connection_info", None)
    if conn is None:
        raise RuntimeError(
            "Sandbox operation response missing 'connection_info' — the LB host "
            "and routing token are mandatory for talking to the shim"
        )
    lb_host = getattr(conn, "load_balancer_hostname", None)
    routing_token = getattr(conn, "routing_token", None)
    if not lb_host or not routing_token:
        raise RuntimeError(
            "Sandbox connection_info missing load_balancer_hostname or routing_token"
        )
    return ProvisionedSandbox(
        name=str(name), lb_host=str(lb_host), routing_token=str(routing_token)
    )


def snapshot_user_sandbox(
    client: Any,
    *,
    sandbox_name: str,
    display_name: str,
    ttl: str | None = None,
) -> str:
    """Snapshot a RUNNING sandbox environment, returning the snapshot resource
    name. The snapshot captures the whole environment (``$HOME`` included), so
    it survives a TTL/idle teardown that the zip ``/workspace`` migration can't.

    ``display_name`` should carry the user prefix (see
    :func:`find_latest_user_snapshot`) so discovery can find it later. ``ttl``
    is pinned explicitly — left unset, Vertex applies its own silent default.
    """
    config: dict[str, Any] = {
        "display_name": display_name,
        "wait_for_completion": True,
    }
    if ttl:
        config["ttl"] = _normalize_ttl(ttl)
    operation = client.agent_engines.sandboxes.snapshots.create(
        source_sandbox_environment_name=sandbox_name, config=config
    )
    name = getattr(getattr(operation, "response", None), "name", None)
    if not name:
        raise RuntimeError("Snapshot operation response missing 'name' field")
    return str(name)


def find_latest_user_snapshot(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
) -> tuple[str, str] | None:
    """Most-recent snapshot for ``user_id`` as ``(name, display_name)``, or
    ``None``. The ``display_name`` carries the version (``lha-<user>-<ver>-snap``)
    so a restore can label the new sandbox with the snapshot's actual image
    version. Mirrors :func:`find_user_sandbox`'s client-side list+filter — no
    local index, so every Cloud Run instance agrees on the answer."""
    snaps = _user_snapshots(
        client, agent_instance_name=agent_instance_name, user_id=user_id
    )
    if not snaps:
        return None
    _, name, display = snaps[0]
    return name, display


def _newest_first(candidates: list) -> None:
    """In-place stable sort so the freshest entry leads: lex-smallest resource
    name (element 1) first, then newest create_time (element 0) — most-recently
    created wins, ties broken by lex-smallest name."""
    candidates.sort(key=lambda c: c[1])
    candidates.sort(key=lambda c: c[0], reverse=True)


def _user_snapshots(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
) -> list[tuple[str, str, str]]:
    """``(create_time, name, display_name)`` for the user's snapshots, newest
    first."""
    prefix = _user_display_prefix(user_id)
    candidates: list[tuple[str, str, str]] = []
    for snap in client.agent_engines.sandboxes.snapshots.list(
        name=agent_instance_name
    ):
        display = getattr(snap, "display_name", None)
        if not display or not display.startswith(prefix):
            continue
        name = getattr(snap, "name", None)
        if name:
            create_time = str(getattr(snap, "create_time", "") or "")
            candidates.append((create_time, str(name), str(display)))
    _newest_first(candidates)
    return candidates


def delete_snapshot(client: Any, *, snapshot_name: str) -> None:
    """Best-effort snapshot delete — swallows 404 (already gone)."""
    try:
        client.agent_engines.sandboxes.snapshots.delete(name=snapshot_name)
    except Exception as exc:
        if _is_not_found(exc):
            logger.info("delete_snapshot: %s already gone (404)", snapshot_name)
            return
        raise


def prune_user_snapshots(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
    keep: int,
) -> list[str]:
    """Delete all but the newest ``keep`` snapshots for ``user_id``. Returns
    the resource names deleted (best-effort; 404s are swallowed)."""
    candidates = _user_snapshots(
        client, agent_instance_name=agent_instance_name, user_id=user_id
    )
    stale = [name for _, name, _ in candidates[keep:]]
    for name in stale:
        delete_snapshot(client, snapshot_name=name)
    return stale


def restore_sandbox_from_snapshot(
    client: Any,
    *,
    agent_instance_name: str,
    owner: str,
    snapshot_name: str,
    display_name: str,
    ttl: str | None = None,
) -> ProvisionedSandbox:
    """Create a sandbox restored from ``snapshot_name``. The restored sandbox
    runs the image the snapshot was taken on — ``sandbox_environment_snapshot``
    and ``sandbox_environment_template`` are mutually exclusive create inputs."""
    config: dict[str, Any] = {
        "owner": owner,
        "display_name": display_name,
        "sandbox_environment_snapshot": snapshot_name,
    }
    if ttl:
        config["ttl"] = _normalize_ttl(ttl)
    operation = client.agent_engines.sandboxes.create(
        name=agent_instance_name, config=config
    )
    return _provisioned_from_operation(operation)


def snapshot_and_prune_user(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
    snapshot_ttl: str,
    keep: int,
) -> dict[str, Any]:
    """Snapshot ``user_id``'s current RUNNING sandbox (preserving its actual
    image version in the snapshot name) and prune to the newest ``keep``
    snapshots. No-op if the user has no running sandbox. Used by the daily
    snapshot scheduler job so an environment survives TTL/idle teardown."""
    latest = find_latest_user_sandbox(
        client, agent_instance_name=agent_instance_name, user_id=user_id
    )
    if latest is None:
        return {"status": "no_sandbox"}
    sandbox_name, sandbox_display = latest
    snapshot_user_sandbox(
        client,
        sandbox_name=sandbox_name,
        display_name=f"{sandbox_display}{SNAPSHOT_DISPLAY_SUFFIX}",
        ttl=snapshot_ttl,
    )
    pruned = prune_user_snapshots(
        client,
        agent_instance_name=agent_instance_name,
        user_id=user_id,
        keep=keep,
    )
    return {"status": "snapshotted", "pruned": len(pruned)}


_SAFE_USER_ID = re.compile(r"[^a-zA-Z0-9_]+")
_USER_ID_MAX_LEN = 32


def runtime_image_version(image_uri: str) -> str:
    """Sanitized version token for the runtime image, used to version-scope
    sandbox identity (see :func:`sandbox_display_name`).

    Prefers the tag after the final ``:`` (ignoring a registry ``host:port``
    colon, detected by a ``/`` in the suffix). Falls back to a short digest
    for ``@sha256:`` refs, or ``untagged`` when neither is present.
    """
    ref = (image_uri or "").strip()
    if "@" in ref:
        digest = ref.rsplit("@", 1)[1]
        return _SAFE_USER_ID.sub("_", digest)[:19] or "untagged"
    _, sep, suffix = ref.rpartition(":")
    if sep and "/" not in suffix:
        return _SAFE_USER_ID.sub("_", suffix) or "untagged"
    return "untagged"


def _sanitize_user_id(user_id: str) -> str:
    return (
        _SAFE_USER_ID.sub("_", user_id or "")[:_USER_ID_MAX_LEN] or "anonymous"
    )


def _user_display_prefix(user_id: str) -> str:
    """The ``lha-<user>-`` prefix shared by every version of a user's sandbox.
    The trailing ``-`` is load-bearing: user and version tokens never contain
    ``-`` after sanitization, so this prefix matches one user unambiguously."""
    return f"lha-{_sanitize_user_id(user_id)}-"


def sandbox_display_name(user_id: str, image_uri: str) -> str:
    """Deterministic per-user, per-image sandbox name used for both create
    (write) and list (read) discovery.

    Shape: ``lha-<user>-<version>``. The user part is sanitized to
    alphanumerics + underscore and truncated to 32 chars; the version part is
    the runtime image token (see :func:`runtime_image_version`). Encoding the
    image version makes reattach version-scoped — a sandbox built from an
    older image won't match a caller running a newer one, so the caller
    migrates and re-provisions instead of serving a stale container.
    """
    return f"{_user_display_prefix(user_id)}{runtime_image_version(image_uri)}"


SNAPSHOT_DISPLAY_SUFFIX = "-snap"


def sandbox_display_name_from_snapshot(snapshot_display_name: str) -> str:
    """Recover the sandbox display name (``lha-<user>-<version>``) from a
    snapshot display name by stripping the ``-snap`` suffix, so a restored
    sandbox is labelled with the snapshot's actual image version."""
    return snapshot_display_name.removesuffix(SNAPSHOT_DISPLAY_SUFFIX)


def _sandbox_is_running(sandbox_obj: Any) -> bool:
    """RUNNING is the only state we can reattach to. ``state`` is exposed
    as an enum whose ``.name`` is e.g. ``STATE_RUNNING``; dict stubs
    return strings; missing field is treated as eligible so that older
    SDK shapes don't silently exclude every sandbox."""
    state = getattr(sandbox_obj, "state", None)
    if state is None:
        return True
    name = getattr(state, "name", None) or str(state)
    return "RUNNING" in name


def find_user_sandbox(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
    image_uri: str,
) -> str | None:
    """Look up the resource name of ``user_id``'s RUNNING sandbox built from
    ``image_uri`` under ``agent_instance_name``, or ``None`` if none exists.

    Filters client-side by ``display_name == sandbox_display_name(user_id,
    image_uri)`` — server-side filter syntax for ``sandboxes.list`` varies
    across SDK versions and isn't worth the breakage risk; sandbox counts per
    engine are bounded by user count anyway. The match is version-scoped: a
    sandbox built from a different image is intentionally not returned here
    (see :func:`find_prior_user_sandbox` for the migration source).

    On duplicates (two backend instances both provisioned), returns the
    lex-smallest resource name so subsequent reads converge on a single
    winner; the loser ages out via TTL.
    """
    want = sandbox_display_name(user_id, image_uri)
    matches: list[str] = []
    for sandbox in client.agent_engines.sandboxes.list(
        name=agent_instance_name
    ):
        if getattr(sandbox, "display_name", None) != want:
            continue
        if not _sandbox_is_running(sandbox):
            continue
        name = getattr(sandbox, "name", None)
        if name:
            matches.append(str(name))
    if not matches:
        return None
    matches.sort()
    return matches[0]


# Distinct from the user prefix ``lha-<user>-`` so a routine sandbox can never
# be matched by find_user_sandbox / find_latest_user_sandbox (which match
# ``lha-<user>-``), and vice versa: ``lhart-…`` does not start with ``lha-``.
ROUTINE_DISPLAY_PREFIX = "lhart-"


def routine_display_name(routine_id: str, image_uri: str) -> str:
    """Per-routine, per-image sandbox name: ``lhart-<routine>-<version>``.

    Mirrors :func:`sandbox_display_name` but with the routine prefix, so routine
    sandboxes occupy a namespace strictly disjoint from user sandboxes.
    """
    return (
        f"{ROUTINE_DISPLAY_PREFIX}{_sanitize_user_id(routine_id)}-"
        f"{runtime_image_version(image_uri)}"
    )


def find_routine_sandbox(
    client: Any,
    *,
    agent_instance_name: str,
    routine_id: str,
    image_uri: str,
) -> str | None:
    """Resource name of ``routine_id``'s RUNNING sandbox built from ``image_uri``
    under ``agent_instance_name``, or ``None``. Version-scoped exact match on the
    ``lhart-`` display name — never resolves a user's sandbox."""
    want = routine_display_name(routine_id, image_uri)
    matches: list[str] = []
    for sandbox in client.agent_engines.sandboxes.list(
        name=agent_instance_name
    ):
        if getattr(sandbox, "display_name", None) != want:
            continue
        if not _sandbox_is_running(sandbox):
            continue
        name = getattr(sandbox, "name", None)
        if name:
            matches.append(str(name))
    if not matches:
        return None
    matches.sort()
    return matches[0]


def find_prior_user_sandbox(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
    current_image_uri: str,
) -> str | None:
    """Find ``user_id``'s RUNNING sandbox built from a *different* (older)
    image than ``current_image_uri`` — the source to migrate ``/workspace``
    from when the runtime image has been bumped. ``None`` if none exists.

    Matches the shared ``lha-<user>-`` prefix but excludes the current
    version. With several priors, picks the most recently created (by
    ``create_time`` when present) so the freshest workspace wins; ties and
    missing timestamps fall back to lex order for determinism.
    """
    prefix = _user_display_prefix(user_id)
    current = sandbox_display_name(user_id, current_image_uri)
    candidates: list[tuple[str, str]] = []
    for sandbox in client.agent_engines.sandboxes.list(
        name=agent_instance_name
    ):
        display = getattr(sandbox, "display_name", None)
        if not display or not display.startswith(prefix) or display == current:
            continue
        if not _sandbox_is_running(sandbox):
            continue
        name = getattr(sandbox, "name", None)
        if name:
            create_time = str(getattr(sandbox, "create_time", "") or "")
            candidates.append((create_time, str(name)))
    if not candidates:
        return None
    _newest_first(candidates)
    return candidates[0][1]


def find_latest_user_sandbox(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
) -> tuple[str, str] | None:
    """Find ``user_id``'s most-recent RUNNING sandbox under
    ``agent_instance_name`` *regardless of image version*, returning
    ``(resource_name, display_name)`` or ``None``.

    Unlike :func:`find_user_sandbox` (exact, version-scoped), this matches the
    shared ``lha-<user>-`` prefix so a routine image bump reattaches to the
    user's existing sandbox instead of force-provisioning a fresh one. The
    returned ``display_name`` carries the version token so the caller can apply
    the upgrade floor (see :func:`version_below_floor`) without a second RPC.

    With several RUNNING sandboxes, picks the most recently created (by
    ``create_time`` when present); ties and missing timestamps fall back to
    lex-smallest resource name for determinism.
    """
    prefix = _user_display_prefix(user_id)
    candidates: list[tuple[str, str, str]] = []
    for sandbox in client.agent_engines.sandboxes.list(
        name=agent_instance_name
    ):
        display = getattr(sandbox, "display_name", None)
        if not display or not display.startswith(prefix):
            continue
        if not _sandbox_is_running(sandbox):
            continue
        name = getattr(sandbox, "name", None)
        if name:
            create_time = str(getattr(sandbox, "create_time", "") or "")
            candidates.append((create_time, str(name), str(display)))
    if not candidates:
        return None
    _newest_first(candidates)
    winner = candidates[0]
    return winner[1], winner[2]


def version_below_floor(display_name: str, floor: str) -> bool:
    """True if the version encoded in ``display_name`` is strictly older than
    ``floor`` (e.g. ``LHA_RUNTIME_MIN_VERSION``).

    The version token in a display_name has ``.``→``_`` mangling (see
    :func:`runtime_image_version`), so it is un-mangled before the semver
    compare. Returns ``False`` (never force an upgrade) when ``floor`` is empty
    or either side is not a parseable version — an ambiguous compare must not
    trigger a disruptive re-provision.
    """
    if not floor:
        return False
    token = display_name[len(display_name.rsplit("-", 1)[0]) + 1 :]
    try:
        return Version(token.replace("_", ".").lstrip("v")) < Version(
            floor.lstrip("v")
        )
    except InvalidVersion:
        return False


def _sandbox_state_name(sandbox_obj: Any) -> str:
    state = getattr(sandbox_obj, "state", None)
    if state is None:
        return ""
    return getattr(state, "name", None) or str(state)


def get_user_sandbox_state(
    client: Any,
    *,
    agent_instance_name: str,
    user_id: str,
    image_uri: str,
) -> dict[str, Any] | None:
    """Snapshot the per-user, current-version sandbox state for the UI
    provisioning banner.

    Returns ``{"status": ..., "sandbox_name": ...}`` where ``status`` is
    one of ``provisioning`` / ``ready`` / ``error``, or ``None`` when no
    sandbox matches ``user_id`` at ``image_uri`` (or it's in a state that
    shouldn't drive a banner, e.g. STOPPING). Version-scoped so an old-image
    sandbox being migrated away doesn't report the new one as ready.

    Multi-instance Cloud Run safe: derived from Vertex's authoritative
    state, so every instance returns the same answer for the same user.
    """
    want = sandbox_display_name(user_id, image_uri)
    candidates: list[tuple[str, str]] = []
    for sandbox in client.agent_engines.sandboxes.list(
        name=agent_instance_name
    ):
        if getattr(sandbox, "display_name", None) != want:
            continue
        name = getattr(sandbox, "name", None)
        if not name:
            continue
        candidates.append((_sandbox_state_name(sandbox), str(name)))
    if not candidates:
        return None

    def _rank(state_name: str) -> int:
        if "RUNNING" in state_name:
            return 0
        if "CREATING" in state_name or "PENDING" in state_name:
            return 1
        if "ERROR" in state_name:
            return 2
        return 3

    candidates.sort(key=lambda pair: (_rank(pair[0]), pair[1]))
    state_name, resource_name = candidates[0]
    if "RUNNING" in state_name:
        status = "ready"
    elif "CREATING" in state_name or "PENDING" in state_name:
        status = "provisioning"
    elif "ERROR" in state_name:
        status = "error"
    else:
        return None
    return {"status": status, "sandbox_name": resource_name}
