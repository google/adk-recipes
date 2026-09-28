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

"""Sandbox lifecycle — provision, delete, and per-user discovery.

Pins the contract between session_start and the Vertex Agent Runtime SDK:
- ``provision_sandbox`` wraps ``sandboxes.create`` and surfaces the resource name.
- ``delete_sandbox`` swallows 404 (idempotent cleanup).
- ``find_user_sandbox`` discovers a user's running sandbox via list + filter
  by ``display_name`` — Cloud-Run safe (no local registry).

All Vertex SDK calls are stubbed end-to-end — no GCP traffic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_op(response_name: str | None) -> Any:
    """A stand-in for ``AgentEngineSandbox*Operation`` — only the
    ``response.name`` field matters for our extraction logic."""
    response = SimpleNamespace(name=response_name) if response_name else None
    return SimpleNamespace(done=True, error=None, response=response)


SANDBOX_LB_HOST = "sb-new.us-central1.sandbox.aiplatform.googleapis.com"
SANDBOX_ROUTING_TOKEN = "route-token-xyz"
SANDBOX_JWT = "fake-sandbox-jwt-token"

RUNTIME_IMAGE = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.11.0"
RUNTIME_IMAGE_OLD = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.9.0"


def _fake_client(
    *,
    existing_templates: list[Any] | None = None,
    existing_sandboxes: list[Any] | None = None,
    existing_snapshots: list[Any] | None = None,
    token: str = SANDBOX_JWT,
) -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    """Returns a stub ``vertexai.Client`` whose sandbox CRUD methods
    record every call. ``calls`` keys: ``create``, ``delete``,
    ``template_list``, ``template_create``, ``token_mint``, ``sandbox_list``,
    ``snapshot_create``, ``snapshot_delete``, ``snapshot_list``."""
    calls: dict[str, list[dict[str, Any]]] = {
        "create": [],
        "delete": [],
        "template_list": [],
        "template_create": [],
        "token_mint": [],
        "sandbox_list": [],
        "snapshot_create": [],
        "snapshot_delete": [],
        "snapshot_list": [],
    }
    client = MagicMock()
    templates_pool: list[Any] = list(existing_templates or [])
    sandboxes_pool: list[Any] = list(existing_sandboxes or [])
    snapshots_pool: list[Any] = list(existing_snapshots or [])

    def fake_create(*, name: str, config: Any = None) -> Any:
        calls["create"].append({"name": name, "config": config})
        sandbox_resource = "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-new"
        # Mimic the real Sandbox object that the SDK hydrates post-LRO.
        sandbox_obj = SimpleNamespace(
            name=sandbox_resource,
            connection_info=SimpleNamespace(
                load_balancer_hostname=SANDBOX_LB_HOST,
                routing_token=SANDBOX_ROUTING_TOKEN,
            ),
        )
        return SimpleNamespace(done=True, error=None, response=sandbox_obj)

    def fake_generate_access_token(service_account: str) -> str:
        calls["token_mint"].append({"service_account": service_account})
        return token

    client.agent_engines.sandboxes.generate_access_token = (
        fake_generate_access_token
    )

    def fake_delete(*, name: str, config: Any = None) -> Any:
        calls["delete"].append({"name": name, "config": config})
        return _make_op(None)

    def fake_template_list(*, name: str, config: Any = None) -> Any:
        calls["template_list"].append({"name": name, "config": config})
        return iter(templates_pool)

    def fake_template_create(
        *, name: str, display_name: str, config: Any = None
    ) -> Any:
        calls["template_create"].append(
            {"name": name, "display_name": display_name, "config": config}
        )
        new_resource = (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-new"
        )
        # Add to the pool so subsequent list() calls see it.
        templates_pool.append(
            SimpleNamespace(name=new_resource, display_name=display_name)
        )
        return _make_op(new_resource)

    # Hydration map: list() returns stubs missing display_name / container spec,
    # get(name=...) returns the full object. Tests opt in by attaching a
    # `_hydrated` attribute on a stub before passing it in.
    def fake_template_get(*, name: str, config: Any = None) -> Any:
        calls.setdefault("template_get", []).append({"name": name})
        for stub in templates_pool:
            hydrated = getattr(stub, "_hydrated", None)
            if hydrated is not None and getattr(stub, "name", None) == name:
                return hydrated
            if getattr(stub, "name", None) == name:
                return stub
        raise LookupError(name)

    def fake_sandbox_list(*, name: str, config: Any = None) -> Any:
        calls["sandbox_list"].append({"name": name, "config": config})
        return iter(sandboxes_pool)

    def fake_snapshot_create(
        *, source_sandbox_environment_name: str, config: Any = None
    ) -> Any:
        calls["snapshot_create"].append(
            {"source": source_sandbox_environment_name, "config": config}
        )
        display_name = _config_get(config, "display_name")
        snap_resource = (
            f"{source_sandbox_environment_name.rsplit('/sandboxEnvironments/', 1)[0]}"
            f"/sandboxEnvironmentSnapshots/snap-new"
        )
        snap_obj = SimpleNamespace(
            name=snap_resource, display_name=display_name
        )
        snapshots_pool.append(snap_obj)
        return SimpleNamespace(done=True, error=None, response=snap_obj)

    def fake_snapshot_list(*, name: str, config: Any = None) -> Any:
        calls["snapshot_list"].append({"name": name})
        return iter(snapshots_pool)

    def fake_snapshot_delete(*, name: str, config: Any = None) -> Any:
        calls["snapshot_delete"].append({"name": name})
        return _make_op(None)

    snapshot_mgr = SimpleNamespace(
        create=fake_snapshot_create,
        list=fake_snapshot_list,
        delete=fake_snapshot_delete,
    )

    client.agent_engines.sandboxes.create = fake_create
    client.agent_engines.sandboxes.delete = fake_delete
    client.agent_engines.sandboxes.list = fake_sandbox_list
    client.agent_engines.sandboxes.snapshots = snapshot_mgr
    client.agent_engines.sandboxes.templates.list = fake_template_list
    client.agent_engines.sandboxes.templates.create = fake_template_create
    client.agent_engines.sandboxes.templates.get = fake_template_get
    return client, calls


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_entrypoints() -> None:
    from app.sandbox import lifecycle

    assert callable(lifecycle.ensure_template)
    assert callable(lifecycle.provision_sandbox)
    assert callable(lifecycle.delete_sandbox)
    assert callable(lifecycle.mint_sandbox_token)
    assert callable(lifecycle.sandbox_display_name)
    assert callable(lifecycle.find_user_sandbox)


def test_module_no_longer_exposes_index_helpers() -> None:
    """Local-file index was Cloud-Run incompatible; the helpers and
    their fcntl/tempfile machinery were dropped in favor of Vertex's
    own ``sandboxes.list`` + ``display_name`` convention."""
    from app.sandbox import lifecycle

    for removed in (
        "read_sandbox_index",
        "write_sandbox_index",
        "sandbox_index_lock",
    ):
        assert not hasattr(lifecycle, removed), removed


# =============================================================================
# provision_sandbox
# =============================================================================


IMAGE_URI = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.3.1"


def _template_with_image(
    *, name: str, display_name: str, image_uri: str
) -> SimpleNamespace:
    """Build a template stub whose ``custom_container_environment`` matches the
    real SDK response shape (``custom_container_spec.image_uri``)."""
    return SimpleNamespace(
        name=name,
        display_name=display_name,
        custom_container_environment=SimpleNamespace(
            custom_container_spec=SimpleNamespace(image_uri=image_uri),
        ),
    )


class TestEnsureTemplate:
    def test_creates_when_missing(self) -> None:
        from app.sandbox.lifecycle import (
            DEFAULT_TEMPLATE_DISPLAY_NAME,
            ensure_template,
        )

        client, calls = _fake_client()  # empty pool
        agent_instance = "projects/p/locations/l/reasoningEngines/r"

        result = ensure_template(
            client, agent_instance_name=agent_instance, image_uri=IMAGE_URI
        )

        assert result == (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-new"
        )
        assert len(calls["template_list"]) == 1
        assert calls["template_list"][0]["name"] == agent_instance
        assert len(calls["template_create"]) == 1
        create_call = calls["template_create"][0]
        assert create_call["name"] == agent_instance
        assert (
            create_call["display_name"]
            == DEFAULT_TEMPLATE_DISPLAY_NAME + "-hermetic"
        )

        # Pins the BYOC payload shape from the plan:
        # custom_container_environment.custom_container_spec.image_uri == IMAGE_URI,
        # ports[0].port == 8080, egress_control_config.internet_access == False
        # (hermetic — opt in with LHA_SANDBOX_INTERNET_ACCESS=1).
        config = create_call["config"]
        cce = _config_get(config, "custom_container_environment")
        assert cce is not None
        spec = _config_get(cce, "custom_container_spec")
        assert _config_get(spec, "image_uri") == IMAGE_URI
        ports = _config_get(cce, "ports")
        assert ports and ports[0]["port"] == 8080
        assert ports[0]["protocol"] == "TCP"
        # Default container category MUST NOT appear — we are BYOC.
        assert _config_get(config, "default_container_environment") in (
            None,
            {},
        )
        assert (
            _config_get(
                _config_get(config, "egress_control_config"), "internet_access"
            )
            is False
        )

    def test_reuses_existing_by_image_uri(self) -> None:
        """Identity for BYOC templates is the image URI, not the display name —
        rebuilds publish a new image tag, and we should re-create then."""
        from app.sandbox.lifecycle import (
            DEFAULT_TEMPLATE_DISPLAY_NAME,
            ensure_template,
        )

        existing_resource = (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-existing"
        )
        client, calls = _fake_client(
            existing_templates=[
                _template_with_image(
                    name=existing_resource,
                    display_name=DEFAULT_TEMPLATE_DISPLAY_NAME + "-hermetic",
                    image_uri=IMAGE_URI,
                ),
            ],
        )

        result = ensure_template(
            client,
            agent_instance_name="projects/p/locations/l/reasoningEngines/r",
            image_uri=IMAGE_URI,
        )

        assert result == existing_resource
        # List was hit, create was NOT.
        assert len(calls["template_list"]) == 1
        assert calls["template_create"] == []

    def test_creates_new_when_image_uri_differs(self) -> None:
        """A template tagged with the old image is not a match for the new
        image — we must build a fresh template."""
        from app.sandbox.lifecycle import (
            DEFAULT_TEMPLATE_DISPLAY_NAME,
            ensure_template,
        )

        old = (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-old"
        )
        client, calls = _fake_client(
            existing_templates=[
                _template_with_image(
                    name=old,
                    display_name=DEFAULT_TEMPLATE_DISPLAY_NAME + "-hermetic",
                    image_uri="us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.2.0",
                ),
            ],
        )

        result = ensure_template(
            client,
            agent_instance_name="projects/p/locations/l/reasoningEngines/r",
            image_uri=IMAGE_URI,
        )

        # Should NOT reuse the v0.2.0 template; must create a new one.
        assert result != old
        assert len(calls["template_create"]) == 1

    def test_reuses_template_when_list_stub_has_null_display_name(self) -> None:
        """Vertex SDK's templates.list() returns stubs with display_name=None
        even though get(name=...) hydrates it. We must hydrate via get() before
        skipping on display-name mismatch — otherwise we create a duplicate
        template on every cold start and burn through quota."""
        from app.sandbox.lifecycle import (
            DEFAULT_TEMPLATE_DISPLAY_NAME,
            ensure_template,
        )

        existing_resource = (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-existing"
        )
        stub = SimpleNamespace(name=existing_resource, display_name=None)
        stub._hydrated = _template_with_image(
            name=existing_resource,
            display_name=DEFAULT_TEMPLATE_DISPLAY_NAME + "-hermetic",
            image_uri=IMAGE_URI,
        )
        client, calls = _fake_client(existing_templates=[stub])

        result = ensure_template(
            client,
            agent_instance_name="projects/p/locations/l/reasoningEngines/r",
            image_uri=IMAGE_URI,
        )

        assert result == existing_resource
        assert calls["template_create"] == []
        assert len(calls.get("template_get", [])) == 1

    def test_ignores_templates_with_other_display_names(self) -> None:
        from app.sandbox.lifecycle import ensure_template

        client, calls = _fake_client(
            existing_templates=[
                _template_with_image(
                    name="projects/.../sandboxEnvironmentTemplates/other",
                    display_name="someone-elses-template",
                    image_uri=IMAGE_URI,
                ),
            ],
        )

        ensure_template(
            client,
            agent_instance_name="projects/p/locations/l/reasoningEngines/r",
            image_uri=IMAGE_URI,
        )

        # Other team's template doesn't count — we create our own.
        assert len(calls["template_create"]) == 1


class TestProvisionSandbox:
    def test_fresh_provision_passes_template_into_config(self) -> None:
        from app.sandbox.lifecycle import provision_sandbox

        client, calls = _fake_client()
        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        template = (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-1"
        )

        result = provision_sandbox(
            client,
            agent_instance_name=agent_instance,
            owner="alice",
            template_name=template,
        )

        # The new return shape surfaces the LB hostname + routing token the
        # caller needs to talk to the shim — caller doesn't have to round-trip
        # back to the SDK to read connection_info.
        assert result.name == (
            "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-new"
        )
        assert result.lb_host == SANDBOX_LB_HOST
        assert result.routing_token == SANDBOX_ROUTING_TOKEN
        assert len(calls["create"]) == 1
        call = calls["create"][0]
        assert call["name"] == agent_instance
        config = call["config"]
        assert _config_get(config, "owner") == "alice"
        assert _config_get(config, "sandbox_environment_template") == template

    def test_normalizes_ttl_to_protobuf_duration(self) -> None:
        from app.sandbox.lifecycle import provision_sandbox

        client, calls = _fake_client()
        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        template = (
            "projects/p/locations/l/reasoningEngines/r/"
            "sandboxEnvironmentTemplates/tmpl-1"
        )

        provision_sandbox(
            client,
            agent_instance_name=agent_instance,
            owner="alice",
            template_name=template,
            ttl="14d",
        )

        # Vertex's Duration field rejects "14d"; must be "<N>s".
        assert _config_get(calls["create"][0]["config"], "ttl") == "1209600s"

    def test_rejects_invalid_ttl(self) -> None:
        from app.sandbox.lifecycle import provision_sandbox

        client, _ = _fake_client()
        with pytest.raises(ValueError, match="Invalid TTL"):
            provision_sandbox(
                client,
                agent_instance_name="projects/p/locations/l/reasoningEngines/r",
                owner="alice",
                template_name="projects/p/locations/l/reasoningEngines/r/sandboxEnvironmentTemplates/tmpl-1",
                ttl="14 fortnights",
            )

    def test_rejects_empty_template(self) -> None:
        from app.sandbox.lifecycle import provision_sandbox

        client, _ = _fake_client()
        with pytest.raises(ValueError, match="template_name"):
            provision_sandbox(
                client,
                agent_instance_name="projects/p/locations/l/reasoningEngines/r",
                owner="alice",
                template_name="",
            )


# =============================================================================
# mint_sandbox_token
# =============================================================================


class TestMintSandboxToken:
    def test_returns_jwt_verbatim(self) -> None:
        from app.sandbox.lifecycle import mint_sandbox_token

        client, calls = _fake_client(token="header.payload.sig")

        token = mint_sandbox_token(
            client, service_account="caller-sa@p.iam.gserviceaccount.com"
        )

        assert token == "header.payload.sig"
        assert calls["token_mint"] == [
            {"service_account": "caller-sa@p.iam.gserviceaccount.com"}
        ]


# =============================================================================
# delete_sandbox
# =============================================================================


class TestDeleteSandbox:
    def test_happy_path(self) -> None:
        from app.sandbox.lifecycle import delete_sandbox

        client, calls = _fake_client()
        delete_sandbox(
            client,
            sandbox_name="projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-1",
        )
        assert len(calls["delete"]) == 1
        assert (
            calls["delete"][0]["name"]
            == "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-1"
        )

    def test_swallows_404(self) -> None:
        """A 404 during cleanup means the sandbox is already gone — that's
        the desired state, so we must not raise."""
        from app.sandbox.lifecycle import delete_sandbox

        client = MagicMock()

        class FakeNotFound(Exception):
            def __init__(self) -> None:
                super().__init__("404 not found")
                self.code = 404
                self.status_code = 404

        def raise_404(**_: Any) -> Any:
            raise FakeNotFound()

        client.agent_engines.sandboxes.delete = raise_404

        # Must not raise.
        delete_sandbox(
            client, sandbox_name="projects/.../sandboxEnvironments/gone"
        )

    def test_propagates_non_404_errors(self) -> None:
        from app.sandbox.lifecycle import delete_sandbox

        client = MagicMock()

        def raise_500(**_: Any) -> Any:
            raise RuntimeError("kaboom")

        client.agent_engines.sandboxes.delete = raise_500

        with pytest.raises(RuntimeError):
            delete_sandbox(
                client, sandbox_name="projects/.../sandboxEnvironments/x"
            )


# =============================================================================
# sandbox_display_name + find_user_sandbox (list+convention discovery)
# =============================================================================


def _sandbox_stub(
    *,
    name: str,
    display_name: str,
    state: str = "STATE_RUNNING",
    create_time: str | None = None,
) -> SimpleNamespace:
    """Shape of a sandbox entry as returned by ``sandboxes.list``."""
    return SimpleNamespace(
        name=name,
        display_name=display_name,
        state=SimpleNamespace(name=state),
        create_time=create_time,
    )


class TestRuntimeImageVersion:
    def test_extracts_and_sanitizes_tag(self) -> None:
        from app.sandbox.lifecycle import runtime_image_version

        assert runtime_image_version(RUNTIME_IMAGE) == "v0_11_0"

    def test_ignores_registry_host_port(self) -> None:
        """A ``host:port`` colon is not a tag — only the tag after the
        final ``:`` (when it has no ``/``) counts."""
        from app.sandbox.lifecycle import runtime_image_version

        assert (
            runtime_image_version("localhost:5000/runtime:v1.2.3") == "v1_2_3"
        )

    def test_untagged_ref_falls_back(self) -> None:
        from app.sandbox.lifecycle import runtime_image_version

        assert runtime_image_version("localhost:5000/runtime") == "untagged"

    def test_digest_ref_uses_short_digest(self) -> None:
        from app.sandbox.lifecycle import runtime_image_version

        ref = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime@sha256:abcdef0123456789"
        assert runtime_image_version(ref) == "sha256_abcdef012345"

    def test_empty_falls_back(self) -> None:
        from app.sandbox.lifecycle import runtime_image_version

        assert runtime_image_version("") == "untagged"


class TestSandboxDisplayName:
    def test_email_like_user_id_is_sanitized(self) -> None:
        from app.sandbox.lifecycle import sandbox_display_name

        assert (
            sandbox_display_name("alice@example.com", RUNTIME_IMAGE)
            == "lha-alice_example_com-v0_11_0"
        )

    def test_empty_user_id_falls_back_to_anonymous(self) -> None:
        from app.sandbox.lifecycle import sandbox_display_name

        assert (
            sandbox_display_name("", RUNTIME_IMAGE) == "lha-anonymous-v0_11_0"
        )

    def test_non_ascii_chars_collapse_to_single_underscore(self) -> None:
        """Runs of unsafe chars collapse to one underscore (regex ``+``
        quantifier on the unsafe character class)."""
        from app.sandbox.lifecycle import sandbox_display_name

        assert sandbox_display_name("用户1", RUNTIME_IMAGE) == "lha-_1-v0_11_0"

    def test_long_user_id_is_truncated(self) -> None:
        """Sanitized user portion clamped to 32 chars; version suffix appended."""
        from app.sandbox.lifecycle import sandbox_display_name

        long_id = "a" * 100
        result = sandbox_display_name(long_id, RUNTIME_IMAGE)
        assert result.startswith("lha-")
        assert result.endswith("-v0_11_0")
        assert len(result) == len("lha-") + 32 + len("-v0_11_0")

    def test_version_scopes_identity(self) -> None:
        """Same user, different image tag → different display_name."""
        from app.sandbox.lifecycle import sandbox_display_name

        assert sandbox_display_name(
            "alice", RUNTIME_IMAGE
        ) != sandbox_display_name("alice", RUNTIME_IMAGE_OLD)

    def test_deterministic_same_input_same_output(self) -> None:
        from app.sandbox.lifecycle import sandbox_display_name

        assert sandbox_display_name(
            "alice", RUNTIME_IMAGE
        ) == sandbox_display_name("alice", RUNTIME_IMAGE)


class TestFindUserSandbox:
    def test_returns_running_sandbox_matching_display_name(self) -> None:
        from app.sandbox.lifecycle import (
            find_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        match = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-alice",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
        )
        other = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-bob",
            display_name=sandbox_display_name("bob", RUNTIME_IMAGE),
        )
        client, calls = _fake_client(existing_sandboxes=[other, match])

        found = find_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            image_uri=RUNTIME_IMAGE,
        )

        assert found == match.name
        assert calls["sandbox_list"] == [
            {"name": agent_instance, "config": None}
        ]

    def test_returns_none_when_no_matches(self) -> None:
        from app.sandbox.lifecycle import find_user_sandbox

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        client, _ = _fake_client(existing_sandboxes=[])

        assert (
            find_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_ignores_sandbox_built_from_a_different_image(self) -> None:
        """The reattach path is version-scoped: a RUNNING sandbox for the
        same user but built from an older image must NOT be reattached —
        the caller migrates from it and provisions fresh instead."""
        from app.sandbox.lifecycle import (
            find_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        stale = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-stale",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
        )
        client, _ = _fake_client(existing_sandboxes=[stale])

        assert (
            find_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_skips_non_running_sandboxes(self) -> None:
        """A sandbox with the right display_name but in STOPPING/PENDING
        is not reattachable — skip it so the caller re-provisions."""
        from app.sandbox.lifecycle import (
            find_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        stopping = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-stopping",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_STOPPING",
        )
        client, _ = _fake_client(existing_sandboxes=[stopping])

        assert (
            find_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_returns_lex_smallest_on_duplicates(self) -> None:
        """Race: two backend instances both provisioned for the same user.
        Pick a deterministic winner so subsequent reads converge; the
        loser ages out via TTL."""
        from app.sandbox.lifecycle import (
            find_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        name = sandbox_display_name("alice", RUNTIME_IMAGE)
        bigger = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-zzzzz",
            display_name=name,
        )
        smaller = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-aaaaa",
            display_name=name,
        )
        # Order in pool intentionally reversed to ensure we sort, not just
        # take the first.
        client, _ = _fake_client(existing_sandboxes=[bigger, smaller])

        found = find_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            image_uri=RUNTIME_IMAGE,
        )
        assert found == smaller.name

    def test_ignores_sandboxes_with_other_display_names(self) -> None:
        """Some other tool's sandbox under the same engine — leave it alone."""
        from app.sandbox.lifecycle import find_user_sandbox

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        other_tool = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-other",
            display_name="some-other-product",
        )
        client, _ = _fake_client(existing_sandboxes=[other_tool])

        assert (
            find_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )


class TestFindPriorUserSandbox:
    def test_returns_running_sandbox_built_from_an_older_image(self) -> None:
        from app.sandbox.lifecycle import (
            find_prior_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        prior = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-old",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
        )
        client, _ = _fake_client(existing_sandboxes=[prior])

        found = find_prior_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            current_image_uri=RUNTIME_IMAGE,
        )
        assert found == prior.name

    def test_excludes_current_version_sandbox(self) -> None:
        from app.sandbox.lifecycle import (
            find_prior_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        current = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-current",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
        )
        client, _ = _fake_client(existing_sandboxes=[current])

        assert (
            find_prior_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                current_image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_picks_newest_by_create_time(self) -> None:
        """With two prior versions, migrate from the most recently created."""
        from app.sandbox.lifecycle import (
            find_prior_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        older = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-v8",
            display_name=sandbox_display_name(
                "alice",
                "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.8.0",
            ),
            create_time="2026-05-25T18:00:00Z",
        )
        newer = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-v9",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
            create_time="2026-05-25T19:00:00Z",
        )
        client, _ = _fake_client(existing_sandboxes=[older, newer])

        found = find_prior_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            current_image_uri=RUNTIME_IMAGE,
        )
        assert found == newer.name

    def test_skips_non_running_priors(self) -> None:
        from app.sandbox.lifecycle import (
            find_prior_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        stopping = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-old",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
            state="STATE_STOPPING",
        )
        client, _ = _fake_client(existing_sandboxes=[stopping])

        assert (
            find_prior_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                current_image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_ignores_other_users_and_other_tools(self) -> None:
        from app.sandbox.lifecycle import (
            find_prior_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        bob = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-bob",
            display_name=sandbox_display_name("bob", RUNTIME_IMAGE_OLD),
        )
        other_tool = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-other",
            display_name="some-other-product",
        )
        client, _ = _fake_client(existing_sandboxes=[bob, other_tool])

        assert (
            find_prior_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                current_image_uri=RUNTIME_IMAGE,
            )
            is None
        )


class TestGetUserSandboxState:
    def test_returns_ready_when_sandbox_is_running(self) -> None:
        from app.sandbox.lifecycle import (
            get_user_sandbox_state,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        sandbox = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-alice",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_RUNNING",
        )
        client, _ = _fake_client(existing_sandboxes=[sandbox])

        snap = get_user_sandbox_state(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            image_uri=RUNTIME_IMAGE,
        )
        assert snap == {"status": "ready", "sandbox_name": sandbox.name}

    def test_returns_provisioning_when_sandbox_is_creating(self) -> None:
        from app.sandbox.lifecycle import (
            get_user_sandbox_state,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        sandbox = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-alice",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_CREATING",
        )
        client, _ = _fake_client(existing_sandboxes=[sandbox])

        snap = get_user_sandbox_state(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            image_uri=RUNTIME_IMAGE,
        )
        assert snap == {"status": "provisioning", "sandbox_name": sandbox.name}

    def test_returns_error_when_sandbox_is_in_error_state(self) -> None:
        from app.sandbox.lifecycle import (
            get_user_sandbox_state,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        sandbox = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-alice",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_ERROR",
        )
        client, _ = _fake_client(existing_sandboxes=[sandbox])

        snap = get_user_sandbox_state(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            image_uri=RUNTIME_IMAGE,
        )
        assert snap == {"status": "error", "sandbox_name": sandbox.name}

    def test_returns_none_when_no_matching_sandbox(self) -> None:
        from app.sandbox.lifecycle import get_user_sandbox_state

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        client, _ = _fake_client(existing_sandboxes=[])

        assert (
            get_user_sandbox_state(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_returns_none_for_terminal_non_render_states(self) -> None:
        """STOPPING/STOPPED/UNSPECIFIED don't drive the banner — return None."""
        from app.sandbox.lifecycle import (
            get_user_sandbox_state,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        sandbox = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-alice",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_STOPPING",
        )
        client, _ = _fake_client(existing_sandboxes=[sandbox])

        assert (
            get_user_sandbox_state(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )

    def test_prefers_running_over_creating_when_both_exist(self) -> None:
        """Race: prior CREATING leftover plus a fresh RUNNING. UI cares
        about the usable one, so RUNNING wins."""
        from app.sandbox.lifecycle import (
            get_user_sandbox_state,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        creating = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-creating",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_CREATING",
        )
        running = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-running",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            state="STATE_RUNNING",
        )
        client, _ = _fake_client(existing_sandboxes=[creating, running])

        snap = get_user_sandbox_state(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            image_uri=RUNTIME_IMAGE,
        )
        assert snap == {"status": "ready", "sandbox_name": running.name}

    def test_ignores_other_users_sandboxes(self) -> None:
        from app.sandbox.lifecycle import (
            get_user_sandbox_state,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        bob = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-bob",
            display_name=sandbox_display_name("bob", RUNTIME_IMAGE),
        )
        client, _ = _fake_client(existing_sandboxes=[bob])

        assert (
            get_user_sandbox_state(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
                image_uri=RUNTIME_IMAGE,
            )
            is None
        )


class TestFindLatestUserSandbox:
    """Version-agnostic reattach: return the user's most-recent RUNNING
    sandbox regardless of image version, so a routine image bump no longer
    forces a re-provision."""

    def test_returns_running_sandbox_regardless_of_version(self) -> None:
        from app.sandbox.lifecycle import (
            find_latest_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        # Only an OLD-image sandbox exists; find_user_sandbox would miss it,
        # but find_latest_user_sandbox must reattach to it.
        old = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-old",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
        )
        client, _ = _fake_client(existing_sandboxes=[old])

        found = find_latest_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
        )
        assert found == (old.name, old.display_name)

    def test_picks_newest_by_create_time(self) -> None:
        from app.sandbox.lifecycle import (
            find_latest_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        older = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-v9",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
            create_time="2026-05-25T18:00:00Z",
        )
        newer = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-v11",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            create_time="2026-05-25T19:00:00Z",
        )
        client, _ = _fake_client(existing_sandboxes=[older, newer])

        found = find_latest_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
        )
        assert found == (newer.name, newer.display_name)

    def test_skips_non_running(self) -> None:
        from app.sandbox.lifecycle import (
            find_latest_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        stopping = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-old",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE_OLD),
            state="STATE_STOPPING",
        )
        client, _ = _fake_client(existing_sandboxes=[stopping])

        assert (
            find_latest_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
            )
            is None
        )

    def test_ignores_other_users_and_tools(self) -> None:
        from app.sandbox.lifecycle import (
            find_latest_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        bob = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-bob",
            display_name=sandbox_display_name("bob", RUNTIME_IMAGE),
        )
        other_tool = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-other",
            display_name="some-other-product",
        )
        client, _ = _fake_client(existing_sandboxes=[bob, other_tool])

        assert (
            find_latest_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
            )
            is None
        )

    def test_lex_tie_break_on_equal_create_time(self) -> None:
        from app.sandbox.lifecycle import (
            find_latest_user_sandbox,
            sandbox_display_name,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        name = sandbox_display_name("alice", RUNTIME_IMAGE)
        bigger = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-zzzzz",
            display_name=name,
            create_time="2026-05-25T19:00:00Z",
        )
        smaller = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-aaaaa",
            display_name=name,
            create_time="2026-05-25T19:00:00Z",
        )
        client, _ = _fake_client(existing_sandboxes=[bigger, smaller])

        found = find_latest_user_sandbox(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
        )
        assert found == (smaller.name, smaller.display_name)

    def test_returns_none_when_empty(self) -> None:
        from app.sandbox.lifecycle import find_latest_user_sandbox

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        client, _ = _fake_client(existing_sandboxes=[])

        assert (
            find_latest_user_sandbox(
                client,
                agent_instance_name=agent_instance,
                user_id="alice",
            )
            is None
        )


class TestVersionBelowFloor:
    """Safety floor: force-upgrade a reattached sandbox whose version is
    older than ``LHA_RUNTIME_MIN_VERSION`` (the escape hatch for breaking
    shim/protocol changes)."""

    def test_below_floor_is_true(self) -> None:
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-v0_9_0", "v0.11.0") is True

    def test_at_floor_is_false(self) -> None:
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-v0_11_0", "v0.11.0") is False

    def test_above_floor_is_false(self) -> None:
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-v0_12_0", "v0.11.0") is False

    def test_unmangles_underscores_before_compare(self) -> None:
        """The version token in a display_name has ``.``→``_`` mangling
        (runtime_image_version). Un-mangle before semver compare or
        ``v0_9_0`` parses as one non-numeric release."""
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-v0_9_0", "v0.10.0") is True

    def test_unparseable_token_is_false(self) -> None:
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-untagged", "v0.11.0") is False

    def test_unparseable_floor_is_false(self) -> None:
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-v0_9_0", "latest") is False

    def test_empty_floor_is_false(self) -> None:
        from app.sandbox.lifecycle import version_below_floor

        assert version_below_floor("lha-alice-v0_9_0", "") is False


def _snapshot_stub(
    *, name: str, display_name: str, create_time: str | None = None
) -> SimpleNamespace:
    """Shape of a snapshot entry as returned by ``snapshots.list``."""
    return SimpleNamespace(
        name=name, display_name=display_name, create_time=create_time
    )


class TestSnapshotUserSandbox:
    def test_creates_snapshot_and_returns_name(self) -> None:
        from app.sandbox.lifecycle import snapshot_user_sandbox

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        sandbox = f"{agent_instance}/sandboxEnvironments/sb-alice"
        client, calls = _fake_client()

        name = snapshot_user_sandbox(
            client,
            sandbox_name=sandbox,
            display_name="lha-alice-v0_11_0-snap",
            ttl="30d",
        )

        assert name.endswith("/sandboxEnvironmentSnapshots/snap-new")
        assert len(calls["snapshot_create"]) == 1
        call = calls["snapshot_create"][0]
        assert call["source"] == sandbox
        assert (
            _config_get(call["config"], "display_name")
            == "lha-alice-v0_11_0-snap"
        )
        # _normalize_ttl converts "30d" → Protobuf Duration "<N>s".
        assert _config_get(call["config"], "ttl") == f"{30 * 86400}s"


class TestFindLatestUserSnapshot:
    def test_returns_newest_matching_prefix(self) -> None:
        from app.sandbox.lifecycle import find_latest_user_snapshot

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        older = _snapshot_stub(
            name=f"{agent_instance}/sandboxEnvironmentSnapshots/snap-old",
            display_name="lha-alice-v0_9_0-snap",
            create_time="2026-05-25T18:00:00Z",
        )
        newer = _snapshot_stub(
            name=f"{agent_instance}/sandboxEnvironmentSnapshots/snap-new",
            display_name="lha-alice-v0_11_0-snap",
            create_time="2026-05-25T19:00:00Z",
        )
        client, _ = _fake_client(existing_snapshots=[older, newer])

        found = find_latest_user_snapshot(
            client, agent_instance_name=agent_instance, user_id="alice"
        )
        assert found == (newer.name, newer.display_name)

    def test_ignores_other_users(self) -> None:
        from app.sandbox.lifecycle import find_latest_user_snapshot

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        bob = _snapshot_stub(
            name=f"{agent_instance}/sandboxEnvironmentSnapshots/snap-bob",
            display_name="lha-bob-v0_11_0-snap",
        )
        client, _ = _fake_client(existing_snapshots=[bob])

        assert (
            find_latest_user_snapshot(
                client, agent_instance_name=agent_instance, user_id="alice"
            )
            is None
        )

    def test_returns_none_when_empty(self) -> None:
        from app.sandbox.lifecycle import find_latest_user_snapshot

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        client, _ = _fake_client(existing_snapshots=[])

        assert (
            find_latest_user_snapshot(
                client, agent_instance_name=agent_instance, user_id="alice"
            )
            is None
        )


class TestDeleteSnapshot:
    def test_calls_delete(self) -> None:
        from app.sandbox.lifecycle import delete_snapshot

        client, calls = _fake_client()
        snap = "projects/p/locations/l/reasoningEngines/r/sandboxEnvironmentSnapshots/s1"
        delete_snapshot(client, snapshot_name=snap)
        assert calls["snapshot_delete"] == [{"name": snap}]

    def test_swallows_404(self) -> None:
        from app.sandbox.lifecycle import delete_snapshot

        client, _ = _fake_client()

        class FakeNotFound(Exception):
            def __init__(self) -> None:
                super().__init__("404")
                self.code = 404

        def boom(*, name: str, config: Any = None) -> Any:
            raise FakeNotFound()

        client.agent_engines.sandboxes.snapshots.delete = boom
        # Should not raise.
        delete_snapshot(client, snapshot_name="gone")


class TestRestoreSandboxFromSnapshot:
    def test_creates_sandbox_from_snapshot(self) -> None:
        from app.sandbox.lifecycle import restore_sandbox_from_snapshot

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        snap = f"{agent_instance}/sandboxEnvironmentSnapshots/snap-1"
        client, calls = _fake_client()

        provisioned = restore_sandbox_from_snapshot(
            client,
            agent_instance_name=agent_instance,
            owner="alice",
            snapshot_name=snap,
            display_name="lha-alice-v0_11_0",
            ttl="14d",
        )

        assert provisioned.lb_host == SANDBOX_LB_HOST
        assert provisioned.routing_token == SANDBOX_ROUTING_TOKEN
        assert len(calls["create"]) == 1
        config = calls["create"][0]["config"]
        assert _config_get(config, "sandbox_environment_snapshot") == snap
        # A restore must NOT also pass a template — they are alternatives.
        assert _config_get(config, "sandbox_environment_template") is None
        assert _config_get(config, "display_name") == "lha-alice-v0_11_0"


class TestPruneUserSnapshots:
    def test_keeps_latest_n_deletes_rest(self) -> None:
        from app.sandbox.lifecycle import prune_user_snapshots

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        snaps = [
            _snapshot_stub(
                name=f"{agent_instance}/sandboxEnvironmentSnapshots/snap-{i}",
                display_name="lha-alice-v0_11_0-snap",
                create_time=f"2026-05-2{i}T00:00:00Z",
            )
            for i in range(1, 5)  # snap-1 (oldest) .. snap-4 (newest)
        ]
        client, calls = _fake_client(existing_snapshots=snaps)

        deleted = prune_user_snapshots(
            client, agent_instance_name=agent_instance, user_id="alice", keep=2
        )

        # Keeps snap-4 + snap-3 (newest two); deletes snap-2 + snap-1.
        deleted_names = {c["name"] for c in calls["snapshot_delete"]}
        assert deleted_names == {
            f"{agent_instance}/sandboxEnvironmentSnapshots/snap-2",
            f"{agent_instance}/sandboxEnvironmentSnapshots/snap-1",
        }
        assert set(deleted) == deleted_names

    def test_noop_when_within_keep(self) -> None:
        from app.sandbox.lifecycle import prune_user_snapshots

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        snaps = [
            _snapshot_stub(
                name=f"{agent_instance}/sandboxEnvironmentSnapshots/snap-{i}",
                display_name="lha-alice-v0_11_0-snap",
                create_time=f"2026-05-2{i}T00:00:00Z",
            )
            for i in range(1, 3)
        ]
        client, calls = _fake_client(existing_snapshots=snaps)

        deleted = prune_user_snapshots(
            client, agent_instance_name=agent_instance, user_id="alice", keep=2
        )
        assert deleted == []
        assert calls["snapshot_delete"] == []


class TestSnapshotAndPruneUser:
    def test_snapshots_running_sandbox_then_prunes(self) -> None:
        from app.sandbox.lifecycle import (
            sandbox_display_name,
            snapshot_and_prune_user,
        )

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        running = _sandbox_stub(
            name=f"{agent_instance}/sandboxEnvironments/sb-alice",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE),
            create_time="2026-05-25T10:00:00Z",
        )
        old_snap = _snapshot_stub(
            name=f"{agent_instance}/sandboxEnvironmentSnapshots/snap-old",
            display_name=sandbox_display_name("alice", RUNTIME_IMAGE) + "-snap",
            create_time="2026-05-20T00:00:00Z",
        )
        client, calls = _fake_client(
            existing_sandboxes=[running], existing_snapshots=[old_snap]
        )

        result = snapshot_and_prune_user(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            snapshot_ttl="30d",
            keep=2,
        )

        assert result["status"] == "snapshotted"
        # Snapshot taken of the running sandbox, named with the -snap suffix.
        assert len(calls["snapshot_create"]) == 1
        call = calls["snapshot_create"][0]
        assert call["source"] == running.name
        assert _config_get(call["config"], "display_name") == (
            sandbox_display_name("alice", RUNTIME_IMAGE) + "-snap"
        )
        # keep=2 with the new snapshot + 1 prior = 2 total → nothing pruned.
        assert calls["snapshot_delete"] == []

    def test_no_running_sandbox_is_noop(self) -> None:
        from app.sandbox.lifecycle import snapshot_and_prune_user

        agent_instance = "projects/p/locations/l/reasoningEngines/r"
        client, calls = _fake_client(existing_sandboxes=[])

        result = snapshot_and_prune_user(
            client,
            agent_instance_name=agent_instance,
            user_id="alice",
            snapshot_ttl="30d",
            keep=2,
        )
        assert result["status"] == "no_sandbox"
        assert calls["snapshot_create"] == []


# =============================================================================
# Helpers
# =============================================================================


def _config_get(config: Any, key: str) -> Any:
    """Pull a field out of either a dict-shaped config or a Pydantic model."""
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get(key)
    return getattr(config, key, None)
