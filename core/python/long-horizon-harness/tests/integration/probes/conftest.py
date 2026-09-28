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

"""Shared fixtures for the BYOC sandbox integration probes.

Every probe in this directory hits real Vertex Agent Runtime and is gated
behind ``RUN_SANDBOX_PROBE=1``. Required env:

* ``AGENT_ENGINE_RESOURCE_NAME`` — the agent_engine resource the sandboxes
  are provisioned under.
* ``LHA_RUNTIME_IMAGE`` — the BYOC image URI (e.g.
  ``us-central1-docker.pkg.dev/.../runtime:vX.Y.Z``).
* ``LHA_SANDBOX_CALLER_SA`` — the SA whose JWT the shim's LB validates.

Fixtures isolate the on-disk sandbox index per probe so probes don't
cross-contaminate, and clear the in-process env cache from
``session_start._env_cache`` so each probe gets a fresh environment.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REQUIRED_ENV = (
    "AGENT_ENGINE_RESOURCE_NAME",
    "LHA_RUNTIME_IMAGE",
    "LHA_SANDBOX_CALLER_SA",
)

_skipif_no_probe = pytest.mark.skipif(
    not os.environ.get("RUN_SANDBOX_PROBE"),
    reason="set RUN_SANDBOX_PROBE=1 to enable sandbox probes",
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-mark every probe in this directory with the skipif gate so
    individual probe authors don't have to remember to add it."""
    for item in items:
        if "tests/integration/probes" in str(item.fspath):
            item.add_marker(_skipif_no_probe)


@pytest.fixture
def fresh_user_id() -> str:
    """A UUID-prefixed user_id keeps probe state out of any real user's
    sandbox lineage and prevents one failed probe from polluting another."""
    return f"probe-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def isolated_local_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point ``LHA_LOCAL_ROOT`` at a tmpdir so the per-probe sandbox index
    is independent of the developer's real ``~/.lha``."""
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def sandbox_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate the sandbox backend and verify all required env vars are set."""
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "sandbox")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing required env: {', '.join(missing)}")


@pytest.fixture
def reset_env_cache() -> Iterator[None]:
    """Drop any cached environments before AND after the probe so the
    next test starts fresh, even if this one leaks."""
    from app.conversation import session_start
    from app.environment_context import clear_active_environment

    session_start._env_cache.clear()  # type: ignore[attr-defined]
    session_start._template_cache.clear()  # type: ignore[attr-defined]
    clear_active_environment()
    yield
    session_start._env_cache.clear()  # type: ignore[attr-defined]
    session_start._template_cache.clear()  # type: ignore[attr-defined]
    clear_active_environment()


@pytest.fixture
def vertex_client() -> object:
    """A real ``vertexai.Client`` pinned to the sandbox region and v1beta1.

    Constructed lazily so module import doesn't require GCP credentials.
    """
    import vertexai

    return vertexai.Client(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location="us-central1",
        http_options={"api_version": "v1beta1"},
    )


pytestmark = pytest.mark.asyncio
