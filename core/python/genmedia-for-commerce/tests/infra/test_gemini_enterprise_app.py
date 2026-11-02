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

"""Without GEMINI_ENTERPRISE_APP_ID, ``infra/gemini_enterprise.py`` creates
or reuses its own Gemini Enterprise app, GenMedia Studio."""

import pytest

from genmedia4commerce.core.models import get_models
from infra import gemini_enterprise as ge
from infra import gemini_enterprise_api as gea
from infra import gemini_enterprise_app as gapp
from tests.infra.ge_fakes import (
    CID,
    CONNECTOR,
    DE,
    ENGINES,
    OWN,
    PARENT,
    error,
    fresh_project_api,
)
from tests.infra.ge_fakes import connector as connector_body

STORE = f"{CID}_mcp_data"
CREATE = f"{DE}/v1alpha/{ENGINES}"
CREATE_URL = f"{CREATE}?engineId=genmedia-for-commerce-studio"
NO_LICENCE = (
    "The project has no Gemini Enterprise subscription "
    "(SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT)."
)


def _own_app_api(app=None, tools=("studio_agent",)):
    """A fresh project; ``app`` is the GET of our app (None: not found)."""
    api = fresh_project_api()
    api.answers[("GET", f"{DE}/v1alpha/{OWN}")] = (
        app if app is not None else error("NOT_FOUND", "no such engine", 404)
    )
    api.answers[("POST", CREATE)] = {"name": f"{PARENT}/operations/e1"}
    api.answers[("GET", f"{DE}/v1alpha/{PARENT}/operations/e1")] = {
        "name": f"{PARENT}/operations/e1",
        "done": True,
    }
    api.answers[("GET", CONNECTOR)] = connector_body(list(tools))
    return api


def _engine_writes(api):
    return [
        (m, u, b)
        for m, u, b in api.writes()
        if u.startswith(CREATE) and "/assistants/" not in u
    ]


ASSISTANT = (
    f"{DE}/v1alpha/{OWN}/assistants/default_assistant?updateMask="
    "webGroundingType,generationConfig.defaultModelId,"
    "generationConfig.systemInstruction.additionalSystemInstruction"
)


@pytest.mark.parametrize("app", [None, {"dataStoreIds": [STORE]}])
def test_its_own_app_s_chat_only_relays_to_the_studio(
    monkeypatch, own_app, app
):
    """Created or reused: no Google Search, the assistant model and the
    relay instructions."""
    monkeypatch.setenv("GENMEDIA_ASSISTANT_MODEL", "gemini-test-flash")
    get_models.cache_clear()
    api = _own_app_api(app=app)
    monkeypatch.setattr(ge.requests, "request", api)

    ge.register(*ge.studio_url(), dry_run=False)
    get_models.cache_clear()

    (body,) = [b for m, u, b in api.writes() if u == ASSISTANT]
    assert body["webGroundingType"] == "WEB_GROUNDING_TYPE_DISABLED"
    config = body["generationConfig"]
    assert config["defaultModelId"] == "gemini-test-flash"
    relay = config["systemInstruction"]["additionalSystemInstruction"]
    assert relay.startswith("You are only a relay")
    assert "studio_agent" in relay and "studio_id" in relay
    assert 'the tool\'s "reply"' in relay
    assert "I'm sending this to the Studio" in relay  # named as banned


def test_a_refused_assistant_setting_only_warns(monkeypatch, own_app, caplog):
    api = _own_app_api(app={"dataStoreIds": [STORE]})
    api.answers[("PATCH", ASSISTANT.split("?", maxsplit=1)[0])] = error(
        "PERMISSION_DENIED", "Not allowed.", 403
    )
    monkeypatch.setattr(ge.requests, "request", api)

    with caplog.at_level("INFO"):
        ge.register(*ge.studio_url(), dry_run=False)

    assert "Could not set up the app's chat (Not allowed.)" in caplog.text
    assert "Done:" in caplog.text


def test_an_existing_app_keeps_its_search_setting(monkeypatch, gcloud_calls):
    """GEMINI_ENTERPRISE_APP_ID: a customer's app is not changed."""
    api = fresh_project_api()
    api.answers[("GET", CONNECTOR)] = connector_body(["studio_agent"])
    monkeypatch.setattr(ge.requests, "request", api)

    ge.register(*ge.studio_url(), dry_run=False)

    assert not [u for _, u, _ in api.writes() if "/assistants/" in u]


def test_creates_its_own_app_with_the_data_store_linked(
    monkeypatch, own_app, sleeps, caplog
):
    api = _own_app_api()
    widget = f"{DE}/v1/{OWN}/widgetConfigs/default_search_widget_config"
    api.answers[("GET", widget)] = {"configId": "abc-123"}
    monkeypatch.setattr(ge.requests, "request", api)

    with caplog.at_level("INFO"):
        ge.register(*ge.studio_url(), dry_run=False)

    assert _engine_writes(api) == [
        ("POST", CREATE_URL, gapp.app_request([STORE]))
    ]
    body = gapp.app_request([STORE])
    assert body["displayName"] == "GenMedia Studio"
    assert body["solutionType"] == "SOLUTION_TYPE_SEARCH"
    assert body["appType"] == "APP_TYPE_INTRANET"
    assert body["searchEngineConfig"] == {
        "searchTier": "SEARCH_TIER_ENTERPRISE",
        "searchAddOns": ["SEARCH_ADD_ON_LLM"],
        "requiredSubscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
    }
    assert sleeps == [5]  # the create operation was not done at once
    assert "Done:" in caplog.text
    assert (
        "Open the app: https://vertexaisearch.cloud.google.com/home/cid/"
        "abc-123" in caplog.text
    )
    assert (
        "gemini-enterprise/locations/global/engines/"
        "genmedia-for-commerce-studio/overview/dashboard?project=my-proj"
        in caplog.text
    )
    assert "licence" not in caplog.text  # the licence step says its own


def test_reuses_its_app_that_already_links_the_data_store(monkeypatch, own_app):
    api = _own_app_api(app={"dataStoreIds": [STORE]})
    monkeypatch.setattr(ge.requests, "request", api)

    ge.register(*ge.studio_url(), dry_run=False)

    assert _engine_writes(api) == []


def test_reused_app_with_an_older_data_store_gets_the_new_one_added(
    monkeypatch, own_app, caplog
):
    older = "genmedia-studio-1600000000-collection_mcp_data"
    api = _own_app_api(app={"dataStoreIds": [older]})
    monkeypatch.setattr(ge.requests, "request", api)

    with caplog.at_level("INFO"):
        ge.register(*ge.studio_url(), dry_run=False)

    # Added, not replaced: the API refuses to leave fewer than two linked.
    assert _engine_writes(api) == [
        (
            "PATCH",
            f"{DE}/v1alpha/{OWN}?updateMask=dataStoreIds",
            {"dataStoreIds": [older, STORE]},
        )
    ]
    assert f"keeps {older}" in caplog.text


def test_dry_run_only_logs_the_app_creation(monkeypatch, own_app, terminal):
    api = _own_app_api()
    monkeypatch.setattr(ge.requests, "request", api)
    terminal.tty = True

    ge.register(*ge.studio_url(), dry_run=True)

    assert api.writes() == []
    assert terminal.prompts == []


def test_without_gemini_enterprise_and_a_terminal_it_stops(
    monkeypatch, own_app, caplog
):
    api = _own_app_api()
    api.answers[("POST", CREATE)] = error("FAILED_PRECONDITION", NO_LICENCE)
    monkeypatch.setattr(ge.requests, "request", api)
    monkeypatch.setattr("sys.argv", ["gemini_enterprise.py"])

    assert ge.main() == 1

    assert "Enable Gemini Enterprise for this project" in caplog.text
    assert "gemini-enterprise/start?project=my-proj" in caplog.text
    assert NO_LICENCE in caplog.text  # the API's own message
    assert gea.RERUN in caplog.text
    assert not any("bapConfig" in u for _, u, _ in api.writes())


def test_waits_for_gemini_enterprise_then_creates_the_app(
    monkeypatch, own_app, terminal, caplog
):
    api = _own_app_api()
    # Even the GET of the app may refuse without Gemini Enterprise.
    api.answers[("GET", f"{DE}/v1alpha/{OWN}")] = error(
        "PERMISSION_DENIED", "Gemini Enterprise license required", 403
    )
    api.answers[("POST", CREATE)] = [
        error("FAILED_PRECONDITION", NO_LICENCE),
        error("PERMISSION_DENIED", "Gemini Enterprise license required", 403),
        {"name": f"{PARENT}/operations/e1", "done": True},
    ]
    monkeypatch.setattr(ge.requests, "request", api)
    terminal.tty = True

    with caplog.at_level("INFO"):
        ge.register(*ge.studio_url(), dry_run=False)

    assert terminal.prompts == [gea.ENTER, gea.ENTER]
    assert [m for m, *_ in _engine_writes(api)] == ["POST"] * 3
    assert "Done:" in caplog.text


def test_another_create_error_is_not_mistaken_for_a_missing_subscription(
    monkeypatch, own_app, terminal
):
    api = _own_app_api()
    api.answers[("POST", CREATE)] = error("INVALID_ARGUMENT", "bad engine id")
    monkeypatch.setattr(ge.requests, "request", api)
    terminal.tty = True

    with pytest.raises(gea.ApiError, match="bad engine id"):
        ge.register(*ge.studio_url(), dry_run=False)
    assert terminal.prompts == []


@pytest.mark.parametrize(
    ("message", "missing"),
    [
        ("Gemini Enterprise subscription not found", True),
        ("This project has no license for SEARCH_AND_ASSISTANT", True),
        ("Billing account required", True),
        ("bad engine id", False),
    ],
)
def test_subscription_detection_is_tolerant(message, missing):
    assert gapp.subscription_missing(Exception(message)) is missing
