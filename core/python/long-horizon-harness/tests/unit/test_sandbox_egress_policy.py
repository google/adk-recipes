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

"""Egress policy: sandbox internet is env-var driven — hermetic in prod, on in dev."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

AGENT_INSTANCE = "projects/p/locations/l/reasoningEngines/r"
IMAGE_URI = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.11.0"

# Only the /egress command tests are async; pytest-asyncio leaves sync tests be.
pytestmark = pytest.mark.asyncio


def _config_get(config: Any, key: str) -> Any:
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get(key)
    return getattr(config, key, None)


def _template_client() -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"template_create": []}
    client = MagicMock()
    client.agent_engines.sandboxes.templates.list = (
        lambda *, name, config=None: iter([])
    )

    def fake_template_create(
        *, name: str, display_name: str, config: Any = None
    ) -> Any:
        calls["template_create"].append(
            {"name": name, "display_name": display_name, "config": config}
        )
        resource = f"{name}/sandboxEnvironmentTemplates/tmpl-new"
        return SimpleNamespace(
            done=True, error=None, response=SimpleNamespace(name=resource)
        )

    client.agent_engines.sandboxes.templates.create = fake_template_create
    return client, calls


class TestDefaultInternetAccess:
    """One knob: ``LHA_SANDBOX_INTERNET_ACCESS``, defaulting per environment."""

    def test_dev_defaults_to_internet_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local dev (no K_SERVICE) gets egress so ``make dev`` works out of the box."""
        from app.sandbox.provider import default_internet_access

        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("LHA_SANDBOX_INTERNET_ACCESS", raising=False)
        assert default_internet_access() is True

    def test_prod_defaults_to_hermetic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud Run (K_SERVICE injected) is default-deny."""
        from app.sandbox.provider import default_internet_access

        monkeypatch.setenv("K_SERVICE", "lha")
        monkeypatch.delenv("LHA_SANDBOX_INTERNET_ACCESS", raising=False)
        assert default_internet_access() is False

    @pytest.mark.parametrize(
        ("k_service", "flag", "expected"),
        [
            (None, "0", False),  # dev opting out
            ("lha", "1", True),  # prod opting in
            (None, "1", True),
            ("lha", "0", False),
        ],
    )
    def test_explicit_flag_wins_in_both_environments(
        self,
        monkeypatch: pytest.MonkeyPatch,
        k_service: str | None,
        flag: str,
        expected: bool,
    ) -> None:
        from app.sandbox.provider import default_internet_access

        if k_service is None:
            monkeypatch.delenv("K_SERVICE", raising=False)
        else:
            monkeypatch.setenv("K_SERVICE", k_service)
        monkeypatch.setenv("LHA_SANDBOX_INTERNET_ACCESS", flag)
        assert default_internet_access() is expected


class TestEnsureTemplateEgress:
    def test_default_is_hermetic(self) -> None:
        from app.sandbox.lifecycle import (
            ensure_template,
            template_display_name,
        )

        client, calls = _template_client()
        ensure_template(
            client, agent_instance_name=AGENT_INSTANCE, image_uri=IMAGE_URI
        )

        create = calls["template_create"][0]
        assert create["display_name"] == template_display_name(False)
        egress = _config_get(create["config"], "egress_control_config")
        assert _config_get(egress, "internet_access") is False

    def test_internet_access_true_creates_net_template(self) -> None:
        from app.sandbox.lifecycle import (
            ensure_template,
            template_display_name,
        )

        client, calls = _template_client()
        ensure_template(
            client,
            agent_instance_name=AGENT_INSTANCE,
            image_uri=IMAGE_URI,
            internet_access=True,
        )

        create = calls["template_create"][0]
        assert create["display_name"] == template_display_name(True)
        egress = _config_get(create["config"], "egress_control_config")
        assert _config_get(egress, "internet_access") is True


class TestProvisionSandboxNoDescription:
    def test_create_config_omits_description(self) -> None:
        """Regression: the live sandbox create API rejects a ``description`` field
        (400 INVALID_ARGUMENT), so we must never send one."""
        from app.sandbox.lifecycle import provision_sandbox

        captured: dict[str, Any] = {}

        def fake_create(*, name: str, config: Any = None) -> Any:
            captured["config"] = config
            return SimpleNamespace(
                done=True,
                error=None,
                response=SimpleNamespace(
                    name="projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb",
                    connection_info=SimpleNamespace(
                        load_balancer_hostname="host", routing_token="tok"
                    ),
                ),
            )

        client = MagicMock()
        client.agent_engines.sandboxes.create = fake_create

        provision_sandbox(
            client,
            agent_instance_name=AGENT_INSTANCE,
            owner="alice",
            template_name="tmpl",
        )
        assert "description" not in captured["config"]


class TestSandboxEnvironmentInternetAccess:
    def _env(self, **kw: Any) -> Any:
        from app.environment.sandbox import SandboxEnvironment

        return SandboxEnvironment(
            client=MagicMock(),
            sandbox_name="sb",
            lb_host="host",
            routing_token="tok",
            sandbox_token="jwt",
            owner="alice",
            **kw,
        )

    def test_constructor_defaults_to_hermetic(self) -> None:
        # Safe-by-default: a directly-constructed env with no mode given is
        # hermetic. Every real provision path passes the mode explicitly; the
        # legacy "internet-on" assumption lives in the reattach path's
        # default=True, not the constructor.
        assert self._env().internet_access is False

    def test_reflects_explicit_mode(self) -> None:
        assert self._env(internet_access=True).internet_access is True
        assert self._env(internet_access=False).internet_access is False


class TestEgressHelpers:
    def test_template_display_name_encodes_mode(self) -> None:
        from app.sandbox.lifecycle import template_display_name

        # The two variants MUST differ so ensure_template's display_name+image
        # match never lets a hermetic caller reuse an internet-enabled template.
        assert template_display_name(True) != template_display_name(False)
        assert template_display_name(True).endswith("-net")
        assert template_display_name(False).endswith("-hermetic")

    def test_template_internet_access_reads_egress_config(self) -> None:
        from app.sandbox.lifecycle import template_internet_access

        on = SimpleNamespace(
            egress_control_config=SimpleNamespace(internet_access=True)
        )
        off = SimpleNamespace(
            egress_control_config=SimpleNamespace(internet_access=False)
        )
        assert template_internet_access(on, default=False) is True
        assert template_internet_access(off, default=True) is False

    def test_template_internet_access_falls_back_to_display_name(self) -> None:
        from app.sandbox.lifecycle import template_internet_access

        net = SimpleNamespace(
            egress_control_config=None, display_name="lha-default-net"
        )
        herm = SimpleNamespace(
            egress_control_config=None, display_name="lha-default-hermetic"
        )
        assert template_internet_access(net, default=False) is True
        assert template_internet_access(herm, default=True) is False

    def test_template_internet_access_defaults_when_unknown(self) -> None:
        from app.sandbox.lifecycle import template_internet_access

        blank = SimpleNamespace(egress_control_config=None, display_name=None)
        assert template_internet_access(blank, default=True) is True
        assert template_internet_access(blank, default=False) is False
