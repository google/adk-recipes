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

"""``on_session_start_callback`` binds the per-session ``BaseEnvironment``.

The callback installs the environment via ``set_active_environment``
on every invocation (so the ContextVar is set in each turn's task) and
caches the env per ``user_id`` to avoid re-provisioning a sandbox on
every turn.

Backend selection is driven by env vars:
* ``LHA_ENVIRONMENT_BACKEND`` ∈ ``{local, sandbox}`` (default: sandbox)
* ``LHA_LOCAL_ROOT`` — directory for the local cache (default
  ``~/.lha``)
* ``AGENT_ENGINE_RESOURCE_NAME`` — parent agent_engine resource for
  sandbox provisioning; missing triggers a local fallback so a
  misconfigured deploy doesn't crash the agent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.sandbox import provider


def _fake_context(
    user_id: str = "user-1", state: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, state=state if state is not None else {}
    )


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    """Stand-in for ``SandboxEnvironment.initialize`` in tests so we don't try
    to poll the LB hostname."""
    return None


IMAGE_URI = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.3.1"
IMAGE_URI_OLD = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.2.0"
CALLER_SA = "lha-caller@p.iam.gserviceaccount.com"


def _dn(user_id: str, image_uri: str = IMAGE_URI) -> str:
    """Version-scoped sandbox display_name, matching production identity."""
    from app.sandbox.lifecycle import sandbox_display_name

    return sandbox_display_name(user_id, image_uri)


SANDBOX_LB_HOST = "sb-1.us-central1.sandbox.aiplatform.googleapis.com"
SANDBOX_ROUTING_TOKEN = "route-token-xyz"
SANDBOX_JWT = "jwt-fake"
SANDBOX_LOCATION = "us-central1"
DEFAULT_ENGINE_DISPLAY_NAME = "lha-sandbox-host"


def _fake_vertex_client(
    *,
    existing_templates: list[Any] | None = None,
    existing_engines: list[tuple[str, str]] | None = None,
    existing_sandboxes: list[Any] | None = None,
    existing_snapshots: list[Any] | None = None,
    sandbox_get_response: Any = None,
    sandbox_get_error: Exception | None = None,
) -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    """Stub ``vertexai.Client`` covering every SDK surface session_start touches:
    ``agent_engines.list``/``.create``, ``sandboxes.create``/``.get``/``.list``,
    ``sandboxes.templates.list``/``.create``, and
    ``sandboxes.generate_access_token``.

    ``existing_engines`` is a list of ``(display_name, resource_name)`` pairs
    returned by ``agent_engines.list()``.

    ``existing_sandboxes`` seeds ``sandboxes.list`` — drives the
    ``find_user_sandbox`` discovery path; use ``_sandbox_stub`` to build
    each entry.

    ``sandbox_get_response`` / ``sandbox_get_error`` control reattach: set
    one to drive what ``sandboxes.get(name=...)`` returns or raises.
    """
    calls: dict[str, list[dict[str, Any]]] = {
        "create": [],
        "delete": [],
        "template_list": [],
        "template_create": [],
        "token_mint": [],
        "engine_list": [],
        "engine_create": [],
        "sandbox_get": [],
        "sandbox_list": [],
        "snapshot_list": [],
        "snapshot_delete": [],
    }
    client = MagicMock()
    pool: list[Any] = list(existing_templates or [])
    engines: list[Any] = [
        SimpleNamespace(api_resource=SimpleNamespace(display_name=dn, name=rn))
        for dn, rn in (existing_engines or [])
    ]
    sandbox_pool: list[Any] = list(existing_sandboxes or [])
    snapshot_pool: list[Any] = list(existing_snapshots or [])

    def fake_create(*, name: str, config: Any = None) -> Any:
        calls["create"].append({"name": name, "config": config})
        sandbox_obj = SimpleNamespace(
            name=f"{name}/sandboxEnvironments/sb-1",
            connection_info=SimpleNamespace(
                load_balancer_hostname=SANDBOX_LB_HOST,
                routing_token=SANDBOX_ROUTING_TOKEN,
            ),
        )
        return SimpleNamespace(done=True, error=None, response=sandbox_obj)

    def fake_template_list(*, name: str, config: Any = None) -> Any:
        calls["template_list"].append({"name": name})
        return iter(pool)

    def fake_template_create(
        *, name: str, display_name: str, config: Any = None
    ) -> Any:
        calls["template_create"].append(
            {"name": name, "display_name": display_name, "config": config}
        )
        # Encode the egress mode into the ref so templates.get can recover it.
        mode = "net" if display_name.endswith("-net") else "hermetic"
        new = f"{name}/sandboxEnvironmentTemplates/tmpl-{mode}"
        pool.append(
            SimpleNamespace(
                name=new,
                display_name=display_name,
                egress_control_config=SimpleNamespace(
                    internet_access=(mode == "net")
                ),
                custom_container_environment=SimpleNamespace(
                    custom_container_spec=SimpleNamespace(image_uri=IMAGE_URI),
                ),
            )
        )
        return SimpleNamespace(
            done=True, error=None, response=SimpleNamespace(name=new)
        )

    def fake_template_get(*, name: str, config: Any = None) -> Any:
        calls.setdefault("template_get", []).append({"name": name})
        for t in pool:
            if getattr(t, "name", None) == name:
                return t
        ia = True if name.endswith("-net") else False
        return SimpleNamespace(
            name=name,
            display_name=None,
            egress_control_config=SimpleNamespace(internet_access=ia),
        )

    def fake_generate_access_token(service_account: str) -> str:
        calls["token_mint"].append({"service_account": service_account})
        return SANDBOX_JWT

    def fake_engine_list() -> Any:
        calls["engine_list"].append({})
        return iter(engines)

    def fake_engine_create(*, config: Any = None) -> Any:
        calls["engine_create"].append({"config": config})
        display_name = getattr(config, "display_name", None)
        new_name = f"projects/p/locations/{SANDBOX_LOCATION}/reasoningEngines/r-{len(engines)}"
        engine = SimpleNamespace(
            api_resource=SimpleNamespace(
                display_name=display_name, name=new_name
            )
        )
        engines.append(engine)
        return engine

    def fake_sandbox_get(*, name: str, config: Any = None) -> Any:
        calls["sandbox_get"].append({"name": name})
        if sandbox_get_error is not None:
            raise sandbox_get_error
        return sandbox_get_response

    def fake_sandbox_list(*, name: str, config: Any = None) -> Any:
        calls["sandbox_list"].append({"name": name})
        return iter(sandbox_pool)

    def fake_sandbox_delete(*, name: str, config: Any = None) -> Any:
        calls["delete"].append({"name": name})
        return SimpleNamespace(done=True, error=None, response=None)

    def fake_snapshot_list(*, name: str, config: Any = None) -> Any:
        calls["snapshot_list"].append({"name": name})
        return iter(snapshot_pool)

    def fake_snapshot_delete(*, name: str, config: Any = None) -> Any:
        calls["snapshot_delete"].append({"name": name})
        return SimpleNamespace(done=True, error=None, response=None)

    snapshot_mgr = SimpleNamespace(
        list=fake_snapshot_list, delete=fake_snapshot_delete
    )

    client.agent_engines.list = fake_engine_list
    client.agent_engines.create = fake_engine_create
    client.agent_engines.sandboxes.create = fake_create
    client.agent_engines.sandboxes.get = fake_sandbox_get
    client.agent_engines.sandboxes.list = fake_sandbox_list
    client.agent_engines.sandboxes.delete = fake_sandbox_delete
    client.agent_engines.sandboxes.snapshots = snapshot_mgr
    client.agent_engines.sandboxes.templates.list = fake_template_list
    client.agent_engines.sandboxes.templates.create = fake_template_create
    client.agent_engines.sandboxes.templates.get = fake_template_get
    client.agent_engines.sandboxes.generate_access_token = (
        fake_generate_access_token
    )
    return client, calls


def _sandbox_stub(
    *, name: str, display_name: str, state: str = "STATE_RUNNING"
) -> SimpleNamespace:
    """List-shaped sandbox stub for seeding ``existing_sandboxes``."""
    return SimpleNamespace(
        name=name,
        display_name=display_name,
        state=SimpleNamespace(name=state),
    )


@pytest.fixture(autouse=True)
def _reset_env_cache() -> None:
    """Each test starts with no cached environments."""
    from app.conversation import session_start
    from app.environment_context import clear_active_environment
    from app.sandbox import provider

    session_start._env_cache.clear()  # type: ignore[attr-defined]
    provider._template_cache.clear()  # type: ignore[attr-defined]
    provider._engine_cache.clear()  # type: ignore[attr-defined]
    clear_active_environment()


async def test_default_backend_falls_back_to_local_when_sandbox_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default backend is ``sandbox``, but with sandbox env vars absent the
    callback warns and degrades to ``LocalEnvironment`` so offline dev and
    unit tests still work."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment import LocalEnvironment
    from app.environment_context import active_environment

    monkeypatch.delenv("LHA_ENVIRONMENT_BACKEND", raising=False)
    monkeypatch.delenv("LHA_RUNTIME_IMAGE", raising=False)
    monkeypatch.delenv("LHA_SANDBOX_CALLER_SA", raising=False)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    await on_session_start_callback(_fake_context(user_id="user-1"))

    env = active_environment()
    assert isinstance(env, LocalEnvironment)
    assert env.working_dir == tmp_path / "users" / "user-1"


async def test_local_backend_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation.session_start import on_session_start_callback
    from app.environment import LocalEnvironment
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    await on_session_start_callback(_fake_context())
    assert isinstance(active_environment(), LocalEnvironment)


def _set_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    agent_instance: str = "projects/p/locations/l/reasoningEngines/r",
) -> None:
    """Set all three env vars the sandbox backend requires."""
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.setenv("AGENT_ENGINE_RESOURCE_NAME", agent_instance)
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))


async def test_sandbox_backend_with_agent_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three sandbox env vars set → ``SandboxEnvironment`` provisioned
    via a BYOC template, talking to the LB via a freshly-minted JWT. The
    create config carries the deterministic ``display_name`` (so the next
    cold start can find it via ``sandboxes.list``) and the default TTL."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    # No need to actually poll the LB during unit tests.
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    # A single version-agnostic discovery list (no sandbox found), then
    # template create + sandbox create. Every JWT mint goes through CALLER_SA
    # (build + per-turn refresh).
    assert calls["sandbox_list"] == [
        {"name": agent_instance},
    ]
    assert len(calls["template_create"]) == 1
    assert len(calls["create"]) == 1
    assert calls["create"][0]["name"] == agent_instance
    config = calls["create"][0]["config"]
    assert config["owner"] == "alice"
    assert config["display_name"] == _dn("alice")
    # _normalize_ttl converts "14d" → Protobuf Duration "<N>s" for the Vertex API
    assert config["ttl"] == f"{14 * 86400}s"
    assert "sandbox_environment_snapshot" not in config
    assert all(c["service_account"] == CALLER_SA for c in calls["token_mint"])
    assert len(calls["token_mint"]) >= 1


async def test_sandbox_backend_reattaches_to_discovered_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with a RUNNING sandbox discoverable via ``sandboxes.list``
    (matched on ``display_name``) reattaches to it — no template ensure,
    no provision, just a get + a fresh JWT mint."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    existing_sandbox = "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-alice-prior"
    reattach_obj = SimpleNamespace(
        name=existing_sandbox,
        state=SimpleNamespace(name="STATE_RUNNING"),
        connection_info=SimpleNamespace(
            load_balancer_hostname=SANDBOX_LB_HOST,
            routing_token=SANDBOX_ROUTING_TOKEN,
        ),
    )
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=existing_sandbox, display_name=_dn("alice")),
        ],
        sandbox_get_response=reattach_obj,
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    assert env.sandbox_name == existing_sandbox
    # Reattach short-circuits provisioning entirely — no create, no template.
    assert calls["create"] == []
    assert calls["template_create"] == []
    assert calls["template_list"] == []
    # We listed (discovery), then got (reattach), and minted a fresh JWT.
    assert calls["sandbox_list"] == [{"name": agent_instance}]
    assert calls["sandbox_get"] == [{"name": existing_sandbox}]
    # One mint for the reattach build, then a second from the per-turn refresh
    # inside the callback (the fast path doesn't suppress refresh).
    assert all(c["service_account"] == CALLER_SA for c in calls["token_mint"])
    assert len(calls["token_mint"]) >= 1


async def test_sandbox_backend_reprovisions_when_discovered_sandbox_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sandboxes.list`` returns a match but ``get`` 404s (deleted between
    list and get, or stale list cache) → fall through to provisioning a
    fresh one rather than failing the session."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    gone = (
        "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-gone"
    )

    class FakeNotFound(Exception):
        def __init__(self) -> None:
            super().__init__("404")
            self.code = 404

    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=gone, display_name=_dn("alice"))
        ],
        sandbox_get_error=FakeNotFound(),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    # Fresh provision happened (template ensure + sandbox create) after
    # the failed reattach.
    assert calls["sandbox_get"] == [{"name": gone}]
    assert len(calls["template_create"]) == 1
    assert len(calls["create"]) == 1


async def test_sandbox_backend_reprovisions_when_discovered_sandbox_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STOPPING/PENDING sandboxes are filtered out by ``find_user_sandbox``
    itself, so we go straight to provisioning without even calling ``get``."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    stale = (
        "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-stale"
    )
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(
                name=stale, display_name=_dn("alice"), state="STATE_STOPPING"
            ),
        ],
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    # Discovery filtered out the STOPPING sandbox → straight to provision,
    # no wasteful get RPC.
    assert calls["sandbox_get"] == []
    assert len(calls["create"]) == 1


async def test_sandbox_provision_passes_ttl_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LHA_SANDBOX_TTL`` override propagates to the ``create`` config so
    operators can tune sandbox lifetime per environment without code
    changes."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.setenv("LHA_SANDBOX_TTL", "7d")

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    # _normalize_ttl converts "7d" → Protobuf Duration "<N>s" for the Vertex API
    assert calls["create"][0]["config"]["ttl"] == f"{7 * 86400}s"


def _tmpl_ref(mode: str) -> str:
    """Template ref whose suffix encodes the egress mode (``net``|``hermetic``)."""
    return (
        "projects/p/locations/l/reasoningEngines/r/"
        f"sandboxEnvironmentTemplates/tmpl-{mode}"
    )


def _running_sandbox(
    name: str,
    *,
    template_mode: str | None = None,
    template_ref: str | None = None,
) -> SimpleNamespace:
    ref = (
        template_ref
        if template_ref is not None
        else (_tmpl_ref(template_mode) if template_mode else None)
    )
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(name="STATE_RUNNING"),
        sandbox_environment_template=ref,
        connection_info=SimpleNamespace(
            load_balancer_hostname=SANDBOX_LB_HOST,
            routing_token=SANDBOX_ROUTING_TOKEN,
        ),
    )


async def test_routine_bump_reattaches_to_old_version_without_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline non-disruptive behavior: after an image bump, a user
    whose only RUNNING sandbox is on the PRIOR image reattaches to it —
    no provision, no migration, no delete. Their installed CLIs survive."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    prior = f"{agent_instance}/sandboxEnvironments/sb-old"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=prior, display_name=_dn("alice", IMAGE_URI_OLD)),
        ],
        sandbox_get_response=_running_sandbox(prior),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    assert env.sandbox_name == prior
    # Reattached to the old-version sandbox: no provision, no migration.
    assert calls["create"] == []
    assert calls["template_create"] == []
    assert calls["delete"] == []
    assert calls["sandbox_list"] == [{"name": agent_instance}]
    assert calls["sandbox_get"] == [{"name": prior}]


async def test_scheduler_session_reattaches_version_agnostically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2/A6 regression: a scheduler-sourced session takes the same
    reattach path and is NOT force-upgraded mid-run by a routine bump."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment
    from app.infrastructure.constants import (
        SCHEDULER_SOURCE,
        SESSION_SOURCE_KEY,
    )

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    prior = f"{agent_instance}/sandboxEnvironments/sb-old"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=prior, display_name=_dn("alice", IMAGE_URI_OLD)),
        ],
        sandbox_get_response=_running_sandbox(prior),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(
        _fake_context(
            user_id="alice", state={SESSION_SOURCE_KEY: SCHEDULER_SOURCE}
        )
    )

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    assert env.sandbox_name == prior
    assert calls["create"] == []


async def test_subfloor_sandbox_force_upgrades_and_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When LHA_RUNTIME_MIN_VERSION is above the reattach candidate's
    version, force-upgrade: provision fresh, copy /workspace, delete prior."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    # Current image is v0.3.1; the only sandbox is on v0.2.0; floor v0.3.0
    # makes the v0.2.0 sandbox sub-floor → force-upgrade.
    monkeypatch.setenv("LHA_RUNTIME_MIN_VERSION", "v0.3.0")

    prior = f"{agent_instance}/sandboxEnvironments/sb-old"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=prior, display_name=_dn("alice", IMAGE_URI_OLD)),
        ],
        sandbox_get_response=_running_sandbox(prior),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    transfers: dict[str, list[Any]] = {"download": [], "upload": []}

    async def fake_download(self: Any, path: Any) -> bytes:
        transfers["download"].append((self.sandbox_name, str(path)))
        return b"ZIPDATA"

    async def fake_upload(self: Any, path: Any, data: bytes) -> None:
        transfers["upload"].append((self.sandbox_name, str(path), data))

    monkeypatch.setattr(
        SandboxEnvironment, "download_zip", fake_download, raising=True
    )
    monkeypatch.setattr(
        SandboxEnvironment, "upload_zip", fake_upload, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    new_name = f"{agent_instance}/sandboxEnvironments/sb-1"
    assert env.sandbox_name == new_name
    assert len(calls["create"]) == 1
    assert transfers["download"] == [(prior, "/workspace")]
    assert transfers["upload"] == [(new_name, "/workspace", b"ZIPDATA")]
    assert calls["delete"] == [{"name": prior}]


async def test_subfloor_migration_failure_leaves_prior_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the force-upgrade copy raises, the new sandbox is still usable and
    the prior is NOT deleted (so files can't be lost to a migration bug)."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.setenv("LHA_RUNTIME_MIN_VERSION", "v0.3.0")

    prior = f"{agent_instance}/sandboxEnvironments/sb-old"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=prior, display_name=_dn("alice", IMAGE_URI_OLD)),
        ],
        sandbox_get_response=_running_sandbox(prior),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    async def boom(self: Any, path: Any) -> bytes:
        raise OSError("sandbox unreachable")

    monkeypatch.setattr(SandboxEnvironment, "download_zip", boom, raising=True)

    await on_session_start_callback(_fake_context(user_id="alice"))

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    assert env.sandbox_name == f"{agent_instance}/sandboxEnvironments/sb-1"
    assert len(calls["create"]) == 1
    # Copy failed → prior left alive for TTL cleanup.
    assert calls["delete"] == []


async def test_no_prior_sandbox_skips_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-ever session (no prior sandbox) provisions cleanly with no
    download/upload/delete."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    called: list[str] = []

    async def track_download(self: Any, path: Any) -> bytes:
        called.append("download")
        return b""

    monkeypatch.setattr(
        SandboxEnvironment, "download_zip", track_download, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert called == []
    assert calls["delete"] == []
    assert calls["sandbox_get"] == []


def _snapshot_stub(
    *, name: str, display_name: str, create_time: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, display_name=display_name, create_time=create_time
    )


async def test_restores_from_snapshot_when_no_running_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No RUNNING sandbox but a snapshot exists (TTL/idle teardown) → restore
    from the snapshot instead of provisioning a blank sandbox. The restored
    sandbox is labelled with the snapshot's image version, not the current one."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.setenv("LHA_SNAPSHOT_ENABLED", "1")

    snap = f"{agent_instance}/sandboxEnvironmentSnapshots/snap-old"
    client, calls = _fake_vertex_client(
        existing_snapshots=[
            _snapshot_stub(
                name=snap, display_name=f"{_dn('alice', IMAGE_URI_OLD)}-snap"
            ),
        ],
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    env = active_environment()
    assert isinstance(env, SandboxEnvironment)
    # Restored via sandboxes.create with the snapshot field — NOT a template.
    assert len(calls["create"]) == 1
    config = calls["create"][0]["config"]
    assert config["sandbox_environment_snapshot"] == snap
    assert "sandbox_environment_template" not in config
    # Labelled with the snapshot's version (v0.2.0), not the current image.
    assert config["display_name"] == _dn("alice", IMAGE_URI_OLD)
    assert calls["template_create"] == []


async def test_snapshot_restore_disabled_by_default_provisions_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With LHA_SNAPSHOT_ENABLED unset (default off, gated on the probe), an
    existing snapshot is ignored and a fresh sandbox is provisioned."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.delenv("LHA_SNAPSHOT_ENABLED", raising=False)

    snap = f"{agent_instance}/sandboxEnvironmentSnapshots/snap-old"
    client, calls = _fake_vertex_client(
        existing_snapshots=[
            _snapshot_stub(
                name=snap, display_name=f"{_dn('alice', IMAGE_URI_OLD)}-snap"
            ),
        ],
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert len(calls["template_create"]) == 1
    config = calls["create"][0]["config"]
    assert "sandbox_environment_snapshot" not in config


async def test_provisions_fresh_when_no_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sandbox and no snapshot → provision fresh from the template (today's
    first-session path), with no snapshot restore."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert len(calls["template_create"]) == 1
    assert len(calls["create"]) == 1
    assert "sandbox_environment_snapshot" not in calls["create"][0]["config"]


async def test_upgrade_user_sandbox_provisions_migrates_and_hot_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/sandbox-upgrade` happy path: user on an old-version sandbox →
    provision current image, migrate /workspace, delete prior, and hot-swap
    the cached + active env so the running session uses the new sandbox."""
    from app.conversation.session_start import (
        _env_cache,
        upgrade_user_sandbox,
    )
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    prior = f"{agent_instance}/sandboxEnvironments/sb-old"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=prior, display_name=_dn("alice", IMAGE_URI_OLD)),
        ],
        sandbox_get_response=_running_sandbox(prior),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    async def fake_download(self: Any, path: Any) -> bytes:
        return b"ZIPDATA"

    async def fake_upload(self: Any, path: Any, data: bytes) -> None:
        return None

    monkeypatch.setattr(
        SandboxEnvironment, "download_zip", fake_download, raising=True
    )
    monkeypatch.setattr(
        SandboxEnvironment, "upload_zip", fake_upload, raising=True
    )

    # Seed the cache with a stale env (as if the session had reattached).
    stale_env = SandboxEnvironment(
        client=client,
        sandbox_name=prior,
        lb_host=SANDBOX_LB_HOST,
        routing_token=SANDBOX_ROUTING_TOKEN,
        sandbox_token=SANDBOX_JWT,
        owner="alice",
    )
    _env_cache[("sandbox", "alice")] = stale_env

    result = await upgrade_user_sandbox("alice")

    new_name = f"{agent_instance}/sandboxEnvironments/sb-1"
    assert result["status"] == "upgraded"
    assert len(calls["create"]) == 1
    # Prior retired after the copy.
    assert calls["delete"] == [{"name": prior}]
    # Cache + active env now point at the fresh sandbox.
    assert _env_cache[("sandbox", "alice")].sandbox_name == new_name
    assert active_environment().sandbox_name == new_name


async def test_upgrade_user_sandbox_already_current_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user already has a current-image sandbox, upgrade is a no-op."""
    from app.conversation.session_start import upgrade_user_sandbox
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    current = f"{agent_instance}/sandboxEnvironments/sb-current"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=current, display_name=_dn("alice")),
        ],
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    result = await upgrade_user_sandbox("alice")

    assert result["status"] == "already_current"
    assert calls["create"] == []
    assert calls["delete"] == []


async def test_upgrade_user_sandbox_unavailable_on_local_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation.session_start import upgrade_user_sandbox

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")

    result = await upgrade_user_sandbox("alice")
    assert result["status"] == "unavailable"


# =============================================================================
# Egress policy: hermetic in prod, internet-on in dev, env var overrides
# =============================================================================


async def test_fresh_sandbox_is_hermetic_by_default_in_prod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Cloud Run with no opt-in, a freshly provisioned sandbox is hermetic: the
    internet-off template (no ``description`` stamp — the create API rejects it),
    and the env reports ``internet_access is False``."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment
    from app.sandbox.lifecycle import template_display_name

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.delenv("LHA_SANDBOX_INTERNET_ACCESS", raising=False)
    monkeypatch.setenv("K_SERVICE", "lha")

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert calls["template_create"][0]["display_name"] == template_display_name(
        False
    )
    assert "description" not in calls["create"][0]["config"]
    assert active_environment().internet_access is False


async def test_fresh_sandbox_has_internet_by_default_in_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local dev (no ``K_SERVICE``) provisions internet-on so ``make dev`` works."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment
    from app.sandbox.lifecycle import template_display_name

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.delenv("LHA_SANDBOX_INTERNET_ACCESS", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert calls["template_create"][0]["display_name"] == template_display_name(
        True
    )
    assert active_environment().internet_access is True


async def test_fresh_sandbox_internet_when_flag_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LHA_SANDBOX_INTERNET_ACCESS=1`` opts a prod sandbox back into egress."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment
    from app.sandbox.lifecycle import template_display_name

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.setenv("LHA_SANDBOX_INTERNET_ACCESS", "1")
    monkeypatch.setenv("K_SERVICE", "lha")

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert calls["template_create"][0]["display_name"] == template_display_name(
        True
    )
    assert "description" not in calls["create"][0]["config"]
    assert active_environment().internet_access is True


async def test_reattach_recovers_internet_access_from_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reattached sandbox recovers its egress mode from its template; a legacy
    sandbox with no template ref is assumed internet-on."""
    from app.conversation.session_start import (
        _ensure_environment,
        _env_cache,
    )
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    prior = f"{agent_instance}/sandboxEnvironments/sb-alice"
    for mode, expected in (("net", True), ("hermetic", False), (None, True)):
        _env_cache.clear()
        client, _ = _fake_vertex_client(
            existing_sandboxes=[
                _sandbox_stub(name=prior, display_name=_dn("alice"))
            ],
            sandbox_get_response=_running_sandbox(prior, template_mode=mode),
        )
        monkeypatch.setattr(
            provider, "_vertex_client_factory", lambda c=client: c
        )
        env = await _ensure_environment("alice")
        assert env.internet_access is expected, mode


def _build_routine_env(
    monkeypatch: pytest.MonkeyPatch, agent_instance: str
) -> Any:
    from app.routines.run_context import RoutineRun

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    env, _ = provider.VertexSandboxProvider().build_routine_environment(
        RoutineRun(routine_id="r1", owner="alice")
    )
    return env, calls


async def test_routine_sandbox_follows_fleet_default_hermetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routines are unattended, so they inherit the same egress default as
    interactive sandboxes — hermetic when deployed, not internet-on."""
    from app.sandbox.lifecycle import template_display_name

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.delenv("LHA_SANDBOX_INTERNET_ACCESS", raising=False)
    monkeypatch.setenv("K_SERVICE", "lha")

    env, calls = _build_routine_env(monkeypatch, agent_instance)

    assert calls["template_create"][0]["display_name"] == template_display_name(
        False
    )
    assert env.internet_access is False


async def test_routine_sandbox_follows_fleet_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LHA_SANDBOX_INTERNET_ACCESS=1`` opts routine sandboxes in too."""
    from app.sandbox.lifecycle import template_display_name

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.setenv("LHA_SANDBOX_INTERNET_ACCESS", "1")

    env, calls = _build_routine_env(monkeypatch, agent_instance)

    assert calls["template_create"][0]["display_name"] == template_display_name(
        True
    )
    assert env.internet_access is True


async def test_upgrade_user_sandbox_preserves_internet_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/sandbox-upgrade of an internet-on sandbox must re-provision internet-on
    (read the prior's template egress), not reset to the hermetic fleet default."""
    from app.conversation.session_start import upgrade_user_sandbox
    from app.environment.sandbox import SandboxEnvironment
    from app.sandbox.lifecycle import template_display_name

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )
    monkeypatch.delenv(
        "LHA_SANDBOX_INTERNET_ACCESS", raising=False
    )  # fleet = deny

    prior = f"{agent_instance}/sandboxEnvironments/sb-old"
    client, calls = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=prior, display_name=_dn("alice", IMAGE_URI_OLD)),
        ],
        sandbox_get_response=_running_sandbox(prior, template_mode="net"),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    async def fake_download(self: Any, path: Any) -> bytes:
        return b"ZIPDATA"

    async def fake_upload(self: Any, path: Any, data: bytes) -> None:
        return None

    monkeypatch.setattr(
        SandboxEnvironment, "download_zip", fake_download, raising=True
    )
    monkeypatch.setattr(
        SandboxEnvironment, "upload_zip", fake_upload, raising=True
    )

    result = await upgrade_user_sandbox("alice")

    assert result["status"] == "upgraded"
    # Prior was internet-on → the upgraded sandbox must use the net template.
    assert calls["template_create"][0]["display_name"] == template_display_name(
        True
    )


async def test_sandbox_backend_auto_discovers_engine_by_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``AGENT_ENGINE_RESOURCE_NAME`` is unset, look up the engine by
    its display name (default ``lha-sandbox-host``) and reuse the
    existing one — same survival-across-restart pattern Memory Bank uses."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    existing = (
        f"projects/p/locations/{SANDBOX_LOCATION}/reasoningEngines/r-existing"
    )
    client, calls = _fake_vertex_client(
        existing_engines=[(DEFAULT_ENGINE_DISPLAY_NAME, existing)]
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert isinstance(active_environment(), SandboxEnvironment)
    assert calls["engine_create"] == []  # discovered, not created
    assert calls["create"][0]["name"] == existing  # provisioned under it


async def test_sandbox_backend_creates_engine_when_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No engine matches the display name → create one so the deploy is
    self-bootstrapping on a fresh project."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    client, calls = _fake_vertex_client(existing_engines=[])
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert len(calls["engine_create"]) == 1
    created_config = calls["engine_create"][0]["config"]
    assert (
        getattr(created_config, "display_name", None)
        == DEFAULT_ENGINE_DISPLAY_NAME
    )
    # Sandbox was provisioned under the just-created engine.
    assert calls["create"][0]["name"].startswith(
        f"projects/p/locations/{SANDBOX_LOCATION}/reasoningEngines/"
    )


async def test_sandbox_backend_env_var_overrides_auto_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``AGENT_ENGINE_RESOURCE_NAME`` short-circuits discovery —
    no list/create RPCs, sandbox provisioned under the pinned engine."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    pinned = "projects/p/locations/l/reasoningEngines/r-pinned"
    _set_sandbox_env(monkeypatch, tmp_path=tmp_path, agent_instance=pinned)

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))

    assert calls["engine_list"] == []
    assert calls["engine_create"] == []
    assert calls["create"][0]["name"] == pinned


async def test_sandbox_engine_resolution_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovered engine name is cached across users so we only pay the
    list RPC once per process."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment.sandbox import SandboxEnvironment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    existing = (
        f"projects/p/locations/{SANDBOX_LOCATION}/reasoningEngines/r-existing"
    )
    client, calls = _fake_vertex_client(
        existing_engines=[(DEFAULT_ENGINE_DISPLAY_NAME, existing)]
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    await on_session_start_callback(_fake_context(user_id="alice"))
    await on_session_start_callback(_fake_context(user_id="bob"))

    assert len(calls["engine_list"]) == 1
    assert calls["engine_create"] == []


async def test_sandbox_backend_engine_resolution_failure_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If auto-discovery raises (no GCP access, quota, etc.) the agent
    must NOT crash — fall back to local so the deploy survives."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment import LocalEnvironment
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    def boom() -> Any:
        raise RuntimeError("vertex unreachable")

    monkeypatch.setattr(provider, "_vertex_client_factory", boom)

    await on_session_start_callback(_fake_context(user_id="alice"))
    assert isinstance(active_environment(), LocalEnvironment)


async def test_sandbox_backend_without_runtime_image_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LHA_RUNTIME_IMAGE`` is mandatory for the sandbox backend — if
    missing, fall back rather than calling ensure_template with no image."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment import LocalEnvironment
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.setenv(
        "AGENT_ENGINE_RESOURCE_NAME",
        "projects/p/locations/l/reasoningEngines/r",
    )
    monkeypatch.delenv("LHA_RUNTIME_IMAGE", raising=False)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    await on_session_start_callback(_fake_context())
    assert isinstance(active_environment(), LocalEnvironment)


async def test_sandbox_backend_without_caller_sa_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No caller SA → cannot mint the LB JWT → fall back."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment import LocalEnvironment
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.setenv(
        "AGENT_ENGINE_RESOURCE_NAME",
        "projects/p/locations/l/reasoningEngines/r",
    )
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.delenv("LHA_SANDBOX_CALLER_SA", raising=False)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    await on_session_start_callback(_fake_context())
    assert isinstance(active_environment(), LocalEnvironment)


async def test_build_sandbox_raises_when_prod_and_missing_runtime_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In Cloud Run (``K_SERVICE`` set) the local fallback is itself a bug —
    the container's filesystem isn't writable. A missing env var must
    surface as ``SandboxConfigurationError`` so the deploy is loudly broken."""
    from app.conversation.session_start import (
        SandboxConfigurationError,
        on_session_start_callback,
    )

    monkeypatch.setenv("K_SERVICE", "lha")
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.setenv(
        "AGENT_ENGINE_RESOURCE_NAME",
        "projects/p/locations/l/reasoningEngines/r",
    )
    monkeypatch.delenv("LHA_RUNTIME_IMAGE", raising=False)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    with pytest.raises(SandboxConfigurationError, match="LHA_RUNTIME_IMAGE"):
        await on_session_start_callback(_fake_context())


async def test_build_sandbox_raises_when_prod_and_missing_caller_sa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation.session_start import (
        SandboxConfigurationError,
        on_session_start_callback,
    )

    monkeypatch.setenv("K_SERVICE", "lha")
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.setenv(
        "AGENT_ENGINE_RESOURCE_NAME",
        "projects/p/locations/l/reasoningEngines/r",
    )
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.delenv("LHA_SANDBOX_CALLER_SA", raising=False)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    with pytest.raises(
        SandboxConfigurationError, match="LHA_SANDBOX_CALLER_SA"
    ):
        await on_session_start_callback(_fake_context())


async def test_build_sandbox_raises_when_prod_and_engine_resolution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vertex unreachable in prod is a hard failure — the agent can't do
    anything useful without its tool execution backend."""
    from app.conversation.session_start import (
        SandboxConfigurationError,
        on_session_start_callback,
    )

    monkeypatch.setenv("K_SERVICE", "lha")
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.setenv("LHA_RUNTIME_IMAGE", IMAGE_URI)
    monkeypatch.setenv("LHA_SANDBOX_CALLER_SA", CALLER_SA)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    def boom() -> Any:
        raise RuntimeError("vertex unreachable")

    monkeypatch.setattr(provider, "_vertex_client_factory", boom)

    with pytest.raises(SandboxConfigurationError, match="vertex unreachable"):
        await on_session_start_callback(_fake_context())


async def test_same_user_reuses_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-user cache prevents re-provisioning across turns."""
    from app.conversation.session_start import on_session_start_callback
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    await on_session_start_callback(_fake_context(user_id="alice"))
    env_a = active_environment()
    await on_session_start_callback(_fake_context(user_id="alice"))
    env_b = active_environment()
    assert env_a is env_b


async def test_different_users_get_distinct_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation.session_start import on_session_start_callback
    from app.environment_context import active_environment

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    await on_session_start_callback(_fake_context(user_id="alice"))
    env_a = active_environment()
    await on_session_start_callback(_fake_context(user_id="bob"))
    env_b = active_environment()
    assert env_a is not env_b
    # Filesystem isolation — distinct working_dirs, not just distinct instances.
    assert env_a.working_dir != env_b.working_dir


async def test_first_invocation_flag_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing ``_session_started`` first-turn marker must still be
    set on first invocation and remain on subsequent invocations."""
    from app.conversation.session_start import (
        SESSION_STARTED_AT_STATE_KEY,
        SESSION_STARTED_STATE_KEY,
        on_session_start_callback,
    )

    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    ctx = _fake_context()
    await on_session_start_callback(ctx)
    assert ctx.state[SESSION_STARTED_STATE_KEY] is True
    assert SESSION_STARTED_AT_STATE_KEY in ctx.state

    started_at = ctx.state[SESSION_STARTED_AT_STATE_KEY]
    await on_session_start_callback(ctx)
    # Idempotent — first-turn marker doesn't get re-stamped.
    assert ctx.state[SESSION_STARTED_AT_STATE_KEY] == started_at


async def test_provisioning_status_returns_ready_when_sandbox_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RUNNING sandbox in Vertex → ``status: ready``. Drives the banner
    off the authoritative cross-instance state, not a per-instance dict."""
    from app.conversation.session_start import get_provisioning_status

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    sandbox_name = f"{agent_instance}/sandboxEnvironments/sb-alice"
    client, _ = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(name=sandbox_name, display_name=_dn("alice")),
        ],
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)

    snap = get_provisioning_status("alice")
    assert snap == {"status": "ready", "sandbox_name": sandbox_name}


async def test_provisioning_status_returns_provisioning_when_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CREATING sandbox → ``status: provisioning`` so the UI shows the
    "spinning up" banner backed by live Vertex state."""
    from app.conversation.session_start import get_provisioning_status

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    sandbox_name = f"{agent_instance}/sandboxEnvironments/sb-alice"
    client, _ = _fake_vertex_client(
        existing_sandboxes=[
            _sandbox_stub(
                name=sandbox_name,
                display_name=_dn("alice"),
                state="STATE_CREATING",
            ),
        ],
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)

    snap = get_provisioning_status("alice")
    assert snap == {"status": "provisioning", "sandbox_name": sandbox_name}


async def test_provisioning_status_returns_none_when_no_sandbox_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty ``sandboxes.list`` → no banner."""
    from app.conversation.session_start import get_provisioning_status

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, _ = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)

    assert get_provisioning_status("alice") is None


async def test_provisioning_status_skips_vertex_when_local_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local backend → return None without making a Vertex RPC. Cheap path
    for the UI poll loop when no sandbox is in play."""
    from app.conversation.session_start import get_provisioning_status

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    def boom() -> Any:
        raise AssertionError("_vertex_client_factory should not be called")

    monkeypatch.setattr(provider, "_vertex_client_factory", boom)
    assert get_provisioning_status("alice") is None


async def test_provisioning_status_skips_vertex_when_env_vars_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sandbox configured but mandatory env vars absent → don't show the
    banner (we're not actually attempting a sandbox), and don't hit Vertex."""
    from app.conversation.session_start import get_provisioning_status

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    monkeypatch.delenv("LHA_RUNTIME_IMAGE", raising=False)
    monkeypatch.delenv("LHA_SANDBOX_CALLER_SA", raising=False)
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))

    def boom() -> Any:
        raise AssertionError("_vertex_client_factory should not be called")

    monkeypatch.setattr(provider, "_vertex_client_factory", boom)
    assert get_provisioning_status("alice") is None


async def test_provisioning_status_swallows_vertex_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient Vertex error must not 500 ``/lha/state``. Return None and
    let the UI poll again; the next iteration likely recovers."""
    from app.conversation.session_start import get_provisioning_status

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    def boom() -> Any:
        raise RuntimeError("vertex unreachable")

    monkeypatch.setattr(provider, "_vertex_client_factory", boom)
    assert get_provisioning_status("alice") is None


async def test_cached_sandbox_env_refreshes_jwt_on_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache hit on _ensure_environment must re-mint the JWT — UI workspace
    endpoints reuse the cached env outside the agent-turn pathway, so without
    this the sandbox shim 401s once the initial token expires (~1h)."""
    from app.conversation.session_start import _ensure_environment
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    env1 = await _ensure_environment("alice")
    mints_after_first = len(calls["token_mint"])
    env2 = await _ensure_environment("alice")

    assert env1 is env2
    assert len(calls["token_mint"]) > mints_after_first


async def test_refresh_sandbox_auth_skips_routing_token_within_throttle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JWT refresh fires on every cache hit but routing-token refresh stays
    throttled — without this, each UI workspace poll would hit Vertex for a
    fresh routing token even though the cached one is still valid."""
    from app.conversation.session_start import _ensure_environment
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, calls = _fake_vertex_client()
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    env = await _ensure_environment("alice")
    mints_after_build = len(calls["token_mint"])
    gets_after_build = len(calls["sandbox_get"])

    env_again = await _ensure_environment("alice")

    assert env_again is env
    assert len(calls["token_mint"]) > mints_after_build
    assert len(calls["sandbox_get"]) == gets_after_build
    assert env._http.headers["X-Sandbox-Routing-Token"] == SANDBOX_ROUTING_TOKEN


async def test_refresh_sandbox_auth_refreshes_routing_token_after_throttle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the throttle window elapses, the next cache hit re-fetches the
    routing token from connection_info and swaps the header."""
    from app.conversation.session_start import (
        _ensure_environment,
    )
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    new_routing_token = "route-token-fresh"
    refreshed_obj = SimpleNamespace(
        connection_info=SimpleNamespace(
            load_balancer_hostname=SANDBOX_LB_HOST,
            routing_token=new_routing_token,
        ),
    )
    client, calls = _fake_vertex_client(sandbox_get_response=refreshed_obj)
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(
        "app.environment.sandbox.time.monotonic", fake_monotonic
    )

    env = await _ensure_environment("alice")
    assert env._http.headers["X-Sandbox-Routing-Token"] == SANDBOX_ROUTING_TOKEN
    gets_after_build = len(calls["sandbox_get"])

    clock["now"] += provider._ROUTING_TOKEN_REFRESH_INTERVAL_SECS + 1
    await _ensure_environment("alice")

    assert len(calls["sandbox_get"]) == gets_after_build + 1
    assert env._http.headers["X-Sandbox-Routing-Token"] == new_routing_token


async def test_refresh_sandbox_auth_swallows_routing_token_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient Vertex error during routing-token refresh must not fail
    the request — the cached token is usually still valid and the next poll
    retries. Header must stay at the previously-stamped value."""
    from app.conversation.session_start import (
        _ensure_environment,
    )
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    client, _ = _fake_vertex_client(
        sandbox_get_error=RuntimeError("vertex unreachable"),
    )
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(
        "app.environment.sandbox.time.monotonic", fake_monotonic
    )

    env = await _ensure_environment("alice")
    assert env._http.headers["X-Sandbox-Routing-Token"] == SANDBOX_ROUTING_TOKEN

    clock["now"] += provider._ROUTING_TOKEN_REFRESH_INTERVAL_SECS + 1
    await _ensure_environment("alice")

    assert env._http.headers["X-Sandbox-Routing-Token"] == SANDBOX_ROUTING_TOKEN


async def test_refresh_evicts_cache_when_sandbox_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5: when the throttled routing-token refresh 404s (another instance
    deleted the sandbox during a /sandbox-upgrade), evict the dead env and
    rebuild a fresh one in the SAME call rather than serving the dead one."""
    from app.conversation.session_start import (
        _ensure_environment,
        _env_cache,
    )
    from app.environment.sandbox import SandboxEnvironment

    agent_instance = "projects/p/locations/l/reasoningEngines/r"
    _set_sandbox_env(
        monkeypatch, tmp_path=tmp_path, agent_instance=agent_instance
    )

    class FakeNotFound(Exception):
        def __init__(self) -> None:
            super().__init__("404")
            self.code = 404

    client, calls = _fake_vertex_client(sandbox_get_error=FakeNotFound())
    monkeypatch.setattr(provider, "_vertex_client_factory", lambda: client)
    monkeypatch.setattr(
        SandboxEnvironment, "initialize", _async_noop, raising=True
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "app.environment.sandbox.time.monotonic", lambda: clock["now"]
    )

    env1 = await _ensure_environment("alice")
    creates_after_build = len(calls["create"])

    clock["now"] += provider._ROUTING_TOKEN_REFRESH_INTERVAL_SECS + 1
    env2 = await _ensure_environment("alice")

    # The 404 evicted the dead env and the same call rebuilt a fresh one.
    assert env2 is not env1
    assert len(calls["create"]) == creates_after_build + 1
    assert _env_cache[("sandbox", "alice")] is env2


pytestmark = pytest.mark.asyncio
