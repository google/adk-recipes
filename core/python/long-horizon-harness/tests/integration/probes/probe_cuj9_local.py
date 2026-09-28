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

"""CUJ-9: Local dev mode — ``LHA_ENVIRONMENT_BACKEND=local`` does NOT
touch Vertex.

Sanity check that the local fallback path stays hermetic: no template or
sandbox provisioning, even if the sandbox env vars are also set. Probes
``provision_sandbox`` is monkeypatched to raise; if anything in
``session_start`` calls it under the local backend we fail loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_local_backend_never_calls_provision(
    fresh_user_id: str,
    isolated_local_root: Path,
    reset_env_cache: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.conversation import session_start
    from app.environment import LocalEnvironment
    from app.sandbox import lifecycle

    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Vertex SDK must not be called in local mode")

    monkeypatch.setattr(lifecycle, "provision_sandbox", _raise)
    monkeypatch.setattr(lifecycle, "ensure_template", _raise)
    monkeypatch.setattr(lifecycle, "mint_sandbox_token", _raise)

    env, _ = session_start._build_environment(fresh_user_id)  # type: ignore[attr-defined]
    assert isinstance(env, LocalEnvironment)
    assert env.working_dir == isolated_local_root / "users" / fresh_user_id
