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

"""The guided waits of ``infra/gemini_enterprise.py``: Enter on a terminal,
the printed step (and a non-zero exit where it blocks) without one."""

import subprocess

import pytest

from infra import gemini_enterprise as ge
from infra import gemini_enterprise_api as gea
from tests.infra.ge_fakes import (
    BLOCKED,
    CID,
    CONNECTOR,
    SETUP,
    fresh_project_api,
)
from tests.infra.ge_fakes import connector as connector_body


def test_wait_for_user_on_a_terminal_waits_for_enter():
    prompts = []

    assert gea.wait_for_user("Go? ", read=prompts.append, isatty=lambda: True)
    assert prompts == ["Go? "]


def test_wait_for_user_without_a_terminal_returns_at_once():
    def read(_):
        raise AssertionError("must not read")

    assert not gea.wait_for_user("Go? ", read=read, isatty=lambda: False)


def test_wait_for_user_with_stdin_closed_returns_false():
    def read(_):
        raise EOFError

    assert not gea.wait_for_user("Go? ", read=read, isatty=lambda: True)


def test_poll_retries_until_the_check_passes(sleeps):
    answers = iter([False, False, True])

    assert gea.poll(lambda: next(answers), 20, 180)
    assert sleeps == [20, 20]


def test_poll_gives_up_after_the_timeout(sleeps):
    assert not gea.poll(lambda: False, 20, 180)
    assert sleeps == [20] * 8  # nine attempts in three minutes


def _register(monkeypatch, api):
    monkeypatch.setattr(ge.requests, "request", api)
    monkeypatch.setattr("sys.argv", ["gemini_enterprise.py"])
    return ge.main()


def test_reload_step_asks_again_until_the_action_is_loaded(
    monkeypatch, gcloud_calls, terminal, sleeps, caplog
):
    """Enter without the reload: 30 s of checks, then asked again."""
    api = fresh_project_api()
    old = connector_body(["studio_agent", "studio_open"])  # an older version's
    # data_store_ids, the first check, six checks after the first Enter, one
    # after the second.
    api.answers[("GET", CONNECTOR)] = [
        connector_body([]),
        connector_body([]),
        *[old] * 6,
        connector_body(["studio_agent"]),
    ]
    terminal.tty = True
    caplog.set_level("INFO")

    assert _register(monkeypatch, api) == 0

    assert terminal.prompts == [gea.ENTER, gea.ENTER]
    assert sleeps.count(5) == 5  # every 5 s for 30 s after the first Enter
    assert caplog.text.count("does not list studio_agent yet") == 1
    assert "click Reload custom actions, then press Enter here" in caplog.text
    assert "check that" not in caplog.text  # the script enables it itself
    assert (
        "https://console.cloud.google.com/gemini-enterprise/locations/global/"
        f"collections/{CID}/connector/actions?project=my-proj" in caplog.text
    )
    assert "is loaded and enabled" in caplog.text
    assert "Done:" in caplog.text
    enabling = [u for _, u, _ in api.writes() if "bapConfig" in u]
    assert len(enabling) == 2  # enabled again once the reload has run


def test_a_reload_that_shows_a_few_seconds_later_is_not_asked_again(
    monkeypatch, gcloud_calls, terminal, sleeps, caplog
):
    api = fresh_project_api()
    api.answers[("GET", CONNECTOR)] = [
        connector_body([]),
        connector_body([]),
        connector_body([]),  # just after Enter: not shown yet
        connector_body(["studio_agent"]),
    ]
    terminal.tty = True
    caplog.set_level("INFO")

    assert _register(monkeypatch, api) == 0

    assert terminal.prompts == [gea.ENTER]
    assert "does not list studio_agent yet" not in caplog.text
    assert "is loaded and enabled" in caplog.text


def test_reload_step_without_a_terminal_still_succeeds(
    monkeypatch, gcloud_calls, caplog
):
    api = fresh_project_api()

    assert _register(monkeypatch, api) == 0

    assert "Reload custom actions" in caplog.text
    assert gea.RERUN in caplog.text
    assert "Done:" not in caplog.text


def test_ctrl_c_at_a_prompt_stops_the_command(
    monkeypatch, gcloud_calls, terminal, caplog
):
    def interrupt():
        raise KeyboardInterrupt

    api = fresh_project_api()
    api.answers[("POST", SETUP)] = BLOCKED
    terminal.tty = True
    terminal.on_enter = interrupt

    assert _register(monkeypatch, api) == 130
    assert "Stopped" in caplog.text


# ------------------------------- the Discovery Engine service agent's wait

AGENT = "service-123@gcp-sa-discoveryengine.iam.gserviceaccount.com"


def _binding_fails(monkeypatch, times: int):
    """gcloud whose invoker binding fails ``times`` times: the service
    agent of a new project does not exist yet."""
    calls = []

    def fake_gcloud(*args):
        calls.append(args)
        bindings = [c for c in calls if "add-iam-policy-binding" in c]
        if "add-iam-policy-binding" in args and len(bindings) <= times:
            raise subprocess.CalledProcessError(
                1,
                ["gcloud", *args],
                stderr=f"ERROR: INVALID_ARGUMENT: Service account {AGENT} "
                "does not exist.",
            )
        return "123"

    monkeypatch.setattr(ge, "gcloud", fake_gcloud)
    api = gea.DiscoveryEngine("my-proj", "global", "token", dry_run=False)
    return lambda: ge.grant_invoker(api, ["my-svc"], dry_run=False), calls


def test_the_binding_waits_for_the_service_agent(monkeypatch, sleeps, caplog):
    grant, calls = _binding_fails(monkeypatch, times=2)
    caplog.set_level("INFO")

    grant()

    assert sleeps == [15, 15]
    assert caplog.text.count(ge.AGENT_WAIT) == 1
    assert sum("add-iam-policy-binding" in c for c in calls) == 3


def test_the_binding_gives_up_after_three_minutes(monkeypatch, sleeps):
    grant, _ = _binding_fails(monkeypatch, times=99)

    with pytest.raises(subprocess.CalledProcessError):
        grant()

    assert sleeps == [15] * 12


def test_an_expired_token_is_renewed_once(monkeypatch):
    """gcloud's cached token can expire while the user is in the console."""
    from tests.infra.ge_fakes import FakeApi, error

    api = FakeApi(
        {("GET", CONNECTOR): [error("UNAUTHENTICATED", "expired", 401), {}]}
    )
    tokens = []

    def request(method, url, headers, json, timeout):
        tokens.append(headers["Authorization"])
        return api(method, url, headers, json, timeout)

    monkeypatch.setattr(gea.requests, "request", request)
    monkeypatch.setattr(gea, "gcloud", lambda *_args: "fresh-token")
    engine = gea.DiscoveryEngine("my-proj", "global", "old-token", False)

    assert engine.call("GET", CONNECTOR.split("/v1alpha/")[1]) == {}

    assert tokens == ["Bearer old-token", "Bearer fresh-token"]


def test_highlight_only_on_a_terminal(monkeypatch):
    monkeypatch.setattr(gea.sys.stderr, "isatty", lambda: True)
    assert gea.highlight("Done.") == "\033[1;32mDone.\033[0m"
    monkeypatch.setattr(gea.sys.stderr, "isatty", lambda: False)
    assert gea.highlight("Done.") == "Done."
