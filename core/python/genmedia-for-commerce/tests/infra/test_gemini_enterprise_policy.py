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

"""The organisation policy on custom MCP connectors: in a terminal the
registration asks to allow them and retries while the change applies,
without one it prints how; ``make allow-mcp-connectors`` sets the override
on its own. gcloud, the terminal and the clock are fakes."""

import subprocess
import sys
from pathlib import Path

import pytest

from infra import gemini_enterprise as ge
from infra import gemini_enterprise_api as gea
from infra import gemini_enterprise_policy as gep
from tests.infra.ge_fakes import (
    BLOCKED,
    CONNECTOR,
    CREATED,
    SETUP,
    fresh_project_api,
)
from tests.infra.ge_fakes import connector as connector_body

DENIED = (
    "ERROR: (gcloud.org-policies.set-policy) [me@example.com] does not have "
    "permission to access projects instance [my-proj]: Permission "
    "'orgpolicy.policies.create' denied on resource"
)
SET_POLICY = "gcloud org-policies set-policy policy.yaml --project=my-proj"


@pytest.fixture
def org_policy(monkeypatch, gcloud_calls):
    """gcloud for the override: ``files`` are the policies set; with
    ``error`` set, set-policy fails with it on stderr."""
    state = {"files": [], "error": ""}
    fake = gep.gcloud

    def gcloud(*args):
        if args[:2] == ("org-policies", "set-policy"):
            if state["error"]:
                gcloud_calls.append(args)
                raise subprocess.CalledProcessError(
                    1, ["gcloud", *args], stderr=state["error"]
                )
            state["files"].append(Path(args[2]).read_text())
        return fake(*args)

    monkeypatch.setattr(gep, "gcloud", gcloud)
    return state


def _register(monkeypatch, api, *args):
    monkeypatch.setattr(ge.requests, "request", api)
    monkeypatch.setattr("sys.argv", ["gemini_enterprise.py", *args])
    return ge.main()


def _blocked_api(*answers):
    """A fresh project where the connector POSTs answer ``answers``."""
    api = fresh_project_api()
    api.answers[("POST", SETUP)] = list(answers)
    api.answers[("GET", CONNECTOR)] = connector_body(["studio_agent"])
    return api


def _posts(api):
    return [u for m, u, *_ in api.calls if (m, u) == ("POST", SETUP)]


@pytest.mark.parametrize("answer", ["", "y", "yes", " Yes "])
def test_yes_allows_the_connectors_and_retries_until_created(
    monkeypatch, org_policy, gcloud_calls, terminal, sleeps, caplog, answer
):
    api = _blocked_api(BLOCKED, BLOCKED, BLOCKED, CREATED)
    terminal.tty = True
    terminal.answer = answer
    caplog.set_level("INFO")

    assert _register(monkeypatch, api) == 0

    assert terminal.prompts == [gep.ASK.format(project="my-proj")]
    assert (
        "Allow custom MCP connectors for project my-proj? [Y/n]"
        in (terminal.prompts[0])
    )
    assert "Studio panel needs one" in caplog.text
    assert "not only this one" in caplog.text
    assert org_policy["files"] == [gep.policy_yaml("my-proj")]
    assert org_policy["files"][0] == (
        f"name: projects/my-proj/policies/{gep.POLICY}\n"
        "spec:\n  rules:\n  - enforce: false\n"
    )
    kinds = [c[:2] for c in gcloud_calls]
    assert kinds.index(("services", "enable")) < kinds.index(
        ("org-policies", "set-policy")
    )
    assert (
        *("services", "enable", "orgpolicy.googleapis.com"),
        "--project=my-proj",
        "--quiet",
    ) in gcloud_calls
    assert "Policy changed. Waiting for it to apply" in caplog.text
    assert "still blocked, trying again in 15 s (0:15)" in caplog.text
    assert "still blocked, trying again in 15 s (0:30)" in caplog.text
    assert "(0:45)" not in caplog.text  # created at the third retry
    assert sleeps == [15, 15, 15]
    assert len(_posts(api)) == 4
    assert "Done:" in caplog.text


def test_a_failed_api_enabling_does_not_stop_the_override(
    monkeypatch, org_policy, terminal
):
    calls = []
    set_policy = gep.gcloud

    def gcloud(*args):
        calls.append(args[:2])
        if args[:2] == ("services", "enable"):
            raise subprocess.CalledProcessError(1, ["gcloud", *args])
        return set_policy(*args)

    monkeypatch.setattr(gep, "gcloud", gcloud)
    terminal.tty = True

    assert _register(monkeypatch, _blocked_api(BLOCKED, CREATED)) == 0
    assert calls[:2] == [("services", "enable"), ("org-policies", "set-policy")]


@pytest.mark.parametrize("answer", ["n", "no", "N"])
def test_no_leaves_the_policy_unchanged(
    monkeypatch, org_policy, terminal, sleeps, caplog, answer
):
    api = _blocked_api(BLOCKED)
    terminal.tty = True
    terminal.answer = answer

    assert _register(monkeypatch, api) == 1

    assert org_policy["files"] == []
    assert sleeps == []
    assert len(_posts(api)) == 1
    assert (
        "Policy not changed. To allow it later: make allow-mcp-connectors, "
        "then run make register-gemini-enterprise again." in caplog.text
    )
    assert "Data stores > Create data store" not in caplog.text


@pytest.mark.parametrize(("stdin", "stdout"), [(False, False), (True, False)])
def test_without_a_terminal_it_prints_how_to_allow_them(
    monkeypatch, org_policy, terminal, sleeps, caplog, stdin, stdout
):
    terminal.tty = stdin
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout)

    assert _register(monkeypatch, _blocked_api(BLOCKED)) == 1

    assert terminal.prompts == []
    assert org_policy["files"] == []
    assert sleeps == []
    assert (
        "allow them for this project only with make allow-mcp-connectors"
        in (caplog.text)
    )
    assert "Or by hand:" in caplog.text
    printf = gep.policy_yaml("my-proj").replace("\n", "\\n")
    assert f"  printf '{printf}' > policy.yaml\n  {SET_POLICY}" in caplog.text
    assert gea.RERUN in caplog.text
    assert "Data stores > Create data store" not in caplog.text


def test_permission_denied_prints_what_an_admin_must_run(
    monkeypatch, org_policy, terminal, sleeps, caplog
):
    api = _blocked_api(BLOCKED)
    org_policy["error"] = DENIED
    terminal.tty = True

    assert _register(monkeypatch, api) == 1

    assert (
        "Could not change the policy (needs the Organization Policy "
        "Administrator role). Ask an admin to run:\n  printf '" in caplog.text
    )
    assert f"{SET_POLICY}\nthen run make register-gemini-enterprise again." in (
        caplog.text
    )
    assert sleeps == []
    assert len(_posts(api)) == 1
    assert "Policy changed" not in caplog.text
    assert "Data stores > Create data store" not in caplog.text


def test_still_blocked_after_three_minutes(
    monkeypatch, org_policy, terminal, sleeps, caplog
):
    api = _blocked_api(BLOCKED)
    terminal.tty = True
    caplog.set_level("INFO")

    assert _register(monkeypatch, api) == 1

    assert org_policy["files"] == [gep.policy_yaml("my-proj")]
    assert sleeps == [15] * 12
    assert len(_posts(api)) == 13  # the first, then one per 15 s
    assert "still blocked, trying again in 15 s (2:45)" in caplog.text
    assert "(3:00)" not in caplog.text
    assert (
        "The policy is set but not applied yet: run make "
        "register-gemini-enterprise again in a minute." in caplog.text
    )
    assert "Data stores > Create data store" not in caplog.text


@pytest.mark.parametrize(
    ("app_id", "project"),
    [
        ("my-app", "my-proj"),
        ("projects/987/locations/eu/collections/c/engines/a", "987"),
    ],
)
def test_make_allow_mcp_connectors_sets_the_override(
    monkeypatch, org_policy, gcloud_calls, caplog, app_id, project
):
    monkeypatch.setenv("GEMINI_ENTERPRISE_APP_ID", app_id)
    caplog.set_level("INFO")

    def no_api(*_args, **_kwargs):
        raise AssertionError("no Discovery Engine call")

    assert _register(monkeypatch, no_api, "--allow-mcp-connectors") == 0

    assert org_policy["files"] == [gep.policy_yaml(project)]
    set_policy = next(c for c in gcloud_calls if c[0] == "org-policies")
    assert f"--project={project}" in set_policy
    assert (
        f"Policy changed: custom MCP connectors are allowed in project "
        f"{project} ({gep.POLICY}, enforce: false)." in caplog.text
    )
    assert not any(
        c[:3] == ("run", "services", "describe") for c in gcloud_calls
    )


def test_make_allow_mcp_connectors_without_the_role(
    monkeypatch, org_policy, caplog
):
    org_policy["error"] = DENIED

    assert _register(monkeypatch, None, "--allow-mcp-connectors") == 1

    assert "Could not change the policy (needs the Organization" in caplog.text
    assert SET_POLICY in caplog.text
    assert "Policy changed" not in caplog.text


def test_make_allow_mcp_connectors_reports_another_gcloud_error(
    monkeypatch, org_policy, caplog
):
    org_policy["error"] = "ERROR: (gcloud.org-policies.set-policy) INVALID"

    assert _register(monkeypatch, None, "--allow-mcp-connectors") == 1

    assert "Error: gcloud org-policies set-policy" in caplog.text
    assert "INVALID" in caplog.text
    assert "Could not change the policy" not in caplog.text


def test_make_allow_mcp_connectors_needs_a_project(
    monkeypatch, org_policy, caplog
):
    monkeypatch.delenv("PROJECT_ID")

    assert _register(monkeypatch, None, "--allow-mcp-connectors") == 1

    assert "PROJECT_ID is not set" in caplog.text
    assert org_policy["files"] == []
