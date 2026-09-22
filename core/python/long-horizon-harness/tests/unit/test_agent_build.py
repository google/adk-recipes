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

import os
import subprocess
import sys

import pytest

from horizon.agent import _build_app_object, _resolve_root_model
from horizon.models import MODEL_REGISTRY, DispatchingLlm


def test_resolve_root_model_none_returns_default_dispatching_llm():
    result = _resolve_root_model(None)
    assert isinstance(result, DispatchingLlm)


def test_resolve_root_model_valid_key_selects_that_model():
    key = next(iter(MODEL_REGISTRY))
    result = _resolve_root_model(key)
    assert isinstance(result, DispatchingLlm)
    assert result.model == key


def test_resolve_root_model_unknown_key_raises():
    with pytest.raises(ValueError):
        _resolve_root_model("nope-not-a-model")


def test_resolve_root_model_passthrough_returns_same_object():
    sentinel = object()
    assert _resolve_root_model(sentinel) is sentinel


def test_build_app_object_default_tools_and_callbacks():
    app = _build_app_object()
    agent = app.root_agent
    tool_names = {
        getattr(t, "name", None)
        or getattr(t, "__name__", None)
        or type(t).__name__
        for t in agent.tools
    }
    assert "read" in tool_names
    assert "write" in tool_names
    assert "bash" in tool_names
    bm = [
        getattr(c, "__name__", type(c).__name__)
        for c in agent.before_model_callback
    ]
    assert bm[0] == "select_model_callback"
    assert len(app.plugins) == 3


def test_build_app_object_root_agent_name():
    assert _build_app_object().root_agent.name == "root_agent"


def test_module_singletons_are_built():
    from horizon import agent

    assert agent.app.root_agent is agent.root_agent
    assert agent.root_agent.name == "root_agent"


def test_lha_version_is_str():
    import horizon

    assert isinstance(horizon.__version__, str)
    assert "__version__" in horizon.__all__


def test_import_horizon_and_build_agent_without_credentials():
    # Credentials/env are process-global and google.auth caches, so run in a
    # subprocess with ADC genuinely stripped (no GOOGLE_APPLICATION_CREDENTIALS
    # file, no gcloud config dir). Importing horizon.agent builds the App; this
    # must not require GCP credentials.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent",
        "CLOUDSDK_CONFIG": "/nonexistent",
        "GOOGLE_CLOUD_PROJECT": "",
    }
    code = "import horizon.agent as a; print(a.root_agent.name)"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 0, (
        f"import horizon.agent failed offline:\nSTDOUT={r.stdout}\nSTDERR={r.stderr}"
    )
    assert "root_agent" in r.stdout
