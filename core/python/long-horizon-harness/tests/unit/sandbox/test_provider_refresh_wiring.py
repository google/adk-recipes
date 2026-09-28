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

import inspect

import pytest

from app.environment.process import BackendGoneError
from app.sandbox import provider as prov


def test_provider_protocol_has_no_refresh_auth():
    assert not hasattr(prov.LocalProvider, "refresh_auth")
    assert not hasattr(prov.VertexSandboxProvider, "refresh_auth")


def test_no_isinstance_sandboxenvironment_in_provider():
    assert "isinstance(env, SandboxEnvironment)" not in inspect.getsource(prov)


def test_routing_fetcher_maps_404_to_backend_gone(monkeypatch):
    class _E(Exception):
        code = 404

    def fake_fetch(client, *, sandbox_name):
        raise _E()

    # The closure resolves the symbol at call time via the module, so patching
    # after building still takes effect.
    monkeypatch.setattr(
        "app.sandbox.lifecycle.fetch_routing_token",
        fake_fetch,
        raising=False,
    )
    _, fetcher = prov._build_refresh_closures(
        object, caller_sa="sa", sandbox_name="s"
    )
    with pytest.raises(BackendGoneError):
        fetcher()
