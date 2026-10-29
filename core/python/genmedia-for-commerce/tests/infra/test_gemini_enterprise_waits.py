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

from infra import gemini_enterprise as ge
from infra import gemini_enterprise_api as gea
from infra import gemini_enterprise_steps as steps
from tests.infra.ge_fakes import (
    CONNECTOR,
    SETUP,
    error,
    fresh_project_api,
)
from tests.infra.ge_fakes import connector as connector_body

BLOCKED = error(
    "FAILED_PRECONDITION",
    f"CUSTOM_ORG_POLICY_VIOLATION: constraints/{steps.POLICY} is enforced",
)
CREATED = {"name": "op", "done": True}


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


def test_org_policy_without_a_terminal_exits_with_the_override(
    monkeypatch, gcloud_calls, caplog
):
    api = fresh_project_api()
    api.answers[("POST", SETUP)] = BLOCKED

    assert _register(monkeypatch, api) == 1

    assert "set-policy policy.yaml --project=my-proj" in caplog.text
    assert "Organization Policy Administrator" in caplog.text
    assert gea.RERUN in caplog.text
    assert "Data stores > Create data store" not in caplog.text


def test_org_policy_retries_after_enter_while_it_applies(
    monkeypatch, gcloud_calls, terminal, sleeps
):
    api = fresh_project_api()
    api.answers[("POST", SETUP)] = [BLOCKED, BLOCKED, CREATED]
    api.answers[("GET", CONNECTOR)] = connector_body(["studio_agent"])
    terminal.tty = True

    assert _register(monkeypatch, api) == 0

    assert terminal.prompts == [steps.POLICY_PROMPT]
    assert sleeps == [steps.POLICY_RETRY_S]


def test_org_policy_asks_again_after_three_minutes(
    monkeypatch, gcloud_calls, terminal, sleeps, caplog
):
    api = fresh_project_api()
    api.answers[("POST", SETUP)] = [BLOCKED] * 10 + [CREATED]
    api.answers[("GET", CONNECTOR)] = connector_body(["studio_agent"])
    terminal.tty = True

    assert _register(monkeypatch, api) == 0

    assert terminal.prompts == [steps.POLICY_PROMPT] * 2
    assert sleeps == [steps.POLICY_RETRY_S] * 8
    assert "Still blocked" in caplog.text


def test_reload_step_waits_until_the_action_is_loaded(
    monkeypatch, gcloud_calls, terminal, caplog
):
    api = fresh_project_api()
    # data_store_ids, the first check, then one check per Enter.
    api.answers[("GET", CONNECTOR)] = [
        connector_body([]),
        connector_body([]),
        connector_body(["studio_agent", "studio_open"]),
        connector_body(["studio_agent"]),
    ]
    terminal.tty = True
    caplog.set_level("INFO")

    assert _register(monkeypatch, api) == 0

    assert terminal.prompts == [gea.ENTER, gea.ENTER]
    assert "Reload custom actions" in caplog.text
    assert "gemini-enterprise/data-stores?project=my-proj" in caplog.text
    assert "is loaded and enabled" in caplog.text
    assert "Done:" in caplog.text
    enabling = [u for _, u, _ in api.writes() if "bapConfig" in u]
    assert len(enabling) == 2  # enabled again once the reload has run


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
