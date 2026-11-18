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

"""Importing core/eval must not touch the network, credentials or env."""

import os
import pkgutil
import subprocess
import sys

import pytest

import genmedia4commerce.core
import genmedia4commerce.eval

MODULES = sorted(
    f"{pkg.__name__}.{m.name}"
    for pkg in (genmedia4commerce.core, genmedia4commerce.eval)
    for m in pkgutil.iter_modules(pkg.__path__)
)

_GUARD = """
import socket, sys

def _blocked(*args, **kwargs):
    raise RuntimeError("network access at import time")

class _Socket:
    def __init__(self, *args, **kwargs):
        _blocked()

socket.socket = _Socket
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
import importlib
for name in sys.argv[1:]:
    importlib.import_module(name)
"""


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_import_without_network_or_project(order):
    modules = MODULES if order == "forward" else MODULES[::-1]
    env = {k: v for k, v in os.environ.items() if k not in ("PROJECT_ID",)}
    env["GOOGLE_CLOUD_PROJECT"] = ""
    env["CLOUDSDK_CONFIG"] = "/nonexistent"
    result = subprocess.run(
        [sys.executable, "-c", _GUARD, *modules],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_every_core_module_listed():
    assert "genmedia4commerce.core.gen" in MODULES
    assert "genmedia4commerce.core.media" in MODULES
