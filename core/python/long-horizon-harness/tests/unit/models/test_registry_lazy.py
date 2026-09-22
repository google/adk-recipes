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

"""The model registry builds backends lazily: bare import + building the root
LLM need neither GCP credentials nor the optional data / postgres extras, and
the Vertex priority service tier is opt-in."""

import os
import subprocess
import sys

# Simulate a minimal install: block the optional extras' top-level packages and
# spy on the ADC probe, then `import horizon`. Must succeed with no ADC probe.
_MINIMAL_INSTALL_SIM = """
import sys

_BLOCKED = {"asyncpg", "yoyo", "yoyo_migrations", "bcrypt",
            "pandas", "matplotlib", "seaborn"}

class _Block:
    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in _BLOCKED:
            raise ImportError(f"{root} blocked (minimal-install simulation)")
        return None

sys.meta_path.insert(0, _Block())

import google.auth
_auth_calls = []
_orig = google.auth.default
def _spy(*a, **k):
    _auth_calls.append(1)
    return _orig(*a, **k)
google.auth.default = _spy

import horizon

assert "asyncpg" not in sys.modules, "asyncpg imported on bare import"
assert not _auth_calls, "google.auth.default() probed on bare import"
assert isinstance(horizon.__version__, str)
print("MINIMAL_OK")
"""


def test_bare_import_minimal_install_no_auth_probe():
    env = dict(os.environ)
    env["GOOGLE_CLOUD_PROJECT"] = ""  # force the (now-deferred) ADC probe path
    r = subprocess.run(
        [sys.executable, "-c", _MINIMAL_INSTALL_SIM],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 0, f"STDOUT={r.stdout}\nSTDERR={r.stderr}"
    assert "MINIMAL_OK" in r.stdout


# Building the root LLM (a DispatchingLlm over the lazy registry) must work with
# no optional extra installed — the Gemini-only minimal slice.
_BUILD_DEFAULT_LLM_SIM = """
import sys

_BLOCKED = {"asyncpg", "pandas", "matplotlib", "seaborn"}

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ImportError("extra blocked (minimal-install simulation)")
        return None

sys.meta_path.insert(0, _Block())

from horizon.models import build_root_llm

llm = build_root_llm()
assert llm.model == "gemini-3.7-flash"
print("BUILD_DEFAULT_OK")
"""


def test_build_root_llm_minimal_install():
    r = subprocess.run(
        [sys.executable, "-c", _BUILD_DEFAULT_LLM_SIM],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, f"STDOUT={r.stdout}\nSTDERR={r.stderr}"
    assert "BUILD_DEFAULT_OK" in r.stdout


def test_registry_membership_is_key_only():
    from horizon.models.registry import MODEL_REGISTRY

    # key-only ops never instantiate a backend
    assert "gemini-3.7-flash" in MODEL_REGISTRY
    assert "nope" not in MODEL_REGISTRY
    assert sorted(MODEL_REGISTRY) == [
        "gemini-3.1-pro-preview",
        "gemini-3.7-flash",
    ]


def test_gemini_backend_default_is_on_demand(monkeypatch):
    monkeypatch.delenv("LHA_VERTEX_SERVICE_TIER", raising=False)
    from google.adk.models import Gemini

    from horizon.models.registry import _build_gemini, _PriorityGemini

    g = _build_gemini()
    assert type(g) is Gemini
    assert not isinstance(g, _PriorityGemini)


def test_gemini_backend_priority_tier_opt_in(monkeypatch):
    monkeypatch.setenv("LHA_VERTEX_SERVICE_TIER", "priority")
    from horizon.models.registry import _build_gemini, _PriorityGemini

    assert isinstance(_build_gemini(), _PriorityGemini)
