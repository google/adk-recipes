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

import importlib
import inspect
import subprocess
import sys

import pytest


def _fresh_import(mod: str) -> None:
    r = subprocess.run(
        [sys.executable, "-c", f"import {mod}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr


def test_environment_imports_clean():
    _fresh_import("app.environment")


def test_tools_processes_imports_clean():
    _fresh_import("app.tools.processes")


def test_environment_layer_has_no_tools_import():
    import app.environment.registry as reg
    from app.environment import base, local

    for mod in (base, local, reg):
        assert "app.tools" not in inspect.getsource(mod), mod.__name__


def test_old_locations_are_gone():
    for mod in (
        "app.tools.processes.handle",
        "app.tools.processes.registry",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod)
