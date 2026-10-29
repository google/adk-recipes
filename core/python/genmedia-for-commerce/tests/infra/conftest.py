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

"""Fixtures of the ``infra/gemini_enterprise.py`` tests: gcloud, the
terminal and the clock are fakes, so nothing waits or calls Google."""

import sys
import time

import pytest

from infra import gemini_enterprise as ge
from infra import gemini_enterprise_policy as gep
from tests.infra.ge_fakes import URL, Terminal


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    """stdin and stdout are not a terminal unless a test sets
    ``terminal.tty``."""
    term = Terminal()
    monkeypatch.setattr("sys.stdin", term)
    monkeypatch.setattr(sys.stdout, "isatty", term.isatty)
    monkeypatch.setattr("builtins.input", term.input)
    return term


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Every ``time.sleep``, recorded instead of slept."""
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setattr(time, "time", lambda: 1700000000)
    return slept


@pytest.fixture
def gcloud_calls(monkeypatch):
    calls = []

    def fake_gcloud(*args):
        calls.append(args)
        if args[:3] == ("run", "services", "describe"):
            return URL
        if args[:2] == ("projects", "describe"):
            return "123"
        return "token" if args[:2] == ("auth", "print-access-token") else ""

    monkeypatch.setattr(ge, "gcloud", fake_gcloud)
    monkeypatch.setattr(gep, "gcloud", fake_gcloud)
    for name in ("GEMINI_ENTERPRISE_LOCATION", "ID", "SERVICE_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROJECT_ID", "my-proj")
    monkeypatch.setenv("DEFAULT_REGION", "us-central1")
    monkeypatch.setenv("GEMINI_ENTERPRISE_APP_ID", "my-app")
    return calls


@pytest.fixture
def own_app(gcloud_calls, monkeypatch):
    """No GEMINI_ENTERPRISE_APP_ID: the command uses its own app."""
    monkeypatch.delenv("GEMINI_ENTERPRISE_APP_ID")
    return gcloud_calls
