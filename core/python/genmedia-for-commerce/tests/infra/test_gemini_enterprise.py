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

"""``infra/gemini_enterprise.py`` builds the right gcloud and REST calls,
with GEMINI_ENTERPRISE_APP_ID set (the app ``my-app``)."""

import pytest

from genmedia4commerce.core.settings import get_settings
from infra import gemini_enterprise as ge
from infra import gemini_enterprise_api as gea
from infra import gemini_enterprise_steps as steps
from tests.infra.ge_fakes import (
    DE,
    ENGINE,
    MCP,
    PARENT,
    REFUSED,
    FakeApi,
    FakeResponse,
    error,
)
from tests.infra.ge_fakes import connector as connector_body
from tests.infra.ge_fakes import entity as _entity
from tests.infra.ge_fakes import fresh_project_api as _fresh_project_api


def test_connector_request_is_a_custom_mcp_without_auth():
    body = steps.connector_request(MCP, "c1")
    assert body["collectionId"] == "c1"
    assert body["collectionDisplayName"] == "GenMedia Studio"
    connector = body["dataConnector"]
    assert connector["dataSource"] == "custom_mcp"
    assert connector["entities"] == [{"entityName": "mcp_data"}]
    assert connector["actionConfig"]["actionParams"] == {
        "auth_type": "NO_AUTH",
        "instance_uri": MCP,
    }
    assert connector["params"] == {"oauth_access_token": "unused"}


def test_register_creates_activates_and_attaches(monkeypatch, gcloud_calls):
    cid = "genmedia-studio-1700000000-collection"
    api = _fresh_project_api(cid)
    monkeypatch.setattr(ge.requests, "request", api)

    run_args, mcp_url = ge.studio_url()
    ge.register(run_args, mcp_url, dry_run=False)

    assert mcp_url == MCP
    connector = f"{DE}/v1alpha/{PARENT}/collections/{cid}/dataConnector"
    method, url, body = api.writes()[0]
    assert (method, url) == (
        "POST",
        f"{DE}/v1alpha/{PARENT}:setUpDataConnector",
    )
    assert body == steps.connector_request(MCP, cid)
    assert api.writes()[1] == (
        "PATCH",
        f"{connector}?updateMask=actionConfig",
        {
            "name": connector.removeprefix(f"{DE}/v1alpha/"),
            "actionConfig": steps.action_config(MCP),
        },
    )
    assert api.writes()[2] == (
        "PATCH",
        f"{DE}/v1alpha/{ENGINE}?updateMask=dataStoreIds",
        {"dataStoreIds": ["docs", f"{cid}_mcp_data"]},
    )
    assert all(h["X-Goog-User-Project"] == "my-proj" for *_, h in api.calls)
    assert all(h["Authorization"] == "Bearer token" for *_, h in api.calls)
    binding = next(c for c in gcloud_calls if "add-iam-policy-binding" in c)
    assert "genmedia-for-commerce" in binding
    assert "--region=us-central1" in binding
    assert "--role=roles/run.invoker" in binding
    assert (
        "--member=serviceAccount:"
        "service-123@gcp-sa-discoveryengine.iam.gserviceaccount.com"
    ) in binding


def _reused_api(engine_stores):
    existing = {
        "name": f"{PARENT}/collections/old",
        "displayName": "GenMedia Studio",
        "dataConnector": {
            "actionConfig": {"actionParams": {"instance_uri": MCP}}
        },
    }
    return FakeApi(
        {
            ("GET", f"{DE}/v1alpha/{PARENT}/collections"): {
                "collections": [existing]
            },
            ("GET", f"{DE}/v1alpha/{PARENT}/collections/old/dataConnector"): {
                "state": "ACTIVE",
                "entities": [_entity("old")],
            },
            ("GET", f"{DE}/v1alpha/{ENGINE}"): {"dataStoreIds": engine_stores},
        }
    )


def _enabling(cid):
    path = f"{DE}/v1alpha/{PARENT}/collections/{cid}/dataConnector"
    return (
        "PATCH",
        f"{path}?updateMask=bapConfig",
        {"bapConfig": {"enabledActions": ["studio_agent"]}},
    )


def _activation(cid):
    path = f"{PARENT}/collections/{cid}/dataConnector"
    return (
        "PATCH",
        f"{DE}/v1alpha/{path}?updateMask=actionConfig",
        {"name": path, "actionConfig": steps.action_config(MCP)},
    )


def test_register_reuses_a_connector_and_activates_it_again(
    monkeypatch, gcloud_calls
):
    api = _reused_api(["old_mcp_data"])
    monkeypatch.setattr(ge.requests, "request", api)

    ge.register(*ge.studio_url(), dry_run=False)

    # No new connector; the idempotent activation is sent again and the
    # data store the app already has is not added twice.
    assert api.writes() == [_activation("old"), _enabling("old")]


def test_rerun_after_a_partial_failure_activates_and_attaches(
    monkeypatch, gcloud_calls
):
    api = _reused_api(["docs"])
    monkeypatch.setattr(ge.requests, "request", api)

    ge.register(*ge.studio_url(), dry_run=False)

    assert api.writes() == [
        _activation("old"),
        (
            "PATCH",
            f"{DE}/v1alpha/{ENGINE}?updateMask=dataStoreIds",
            {"dataStoreIds": ["docs", "old_mcp_data"]},
        ),
        _enabling("old"),
    ]


@pytest.mark.parametrize(
    ("value", "warned"), [("true", True), ("false", False)]
)
def test_use_iap_prints_a_warning(
    monkeypatch, gcloud_calls, caplog, value, warned
):
    monkeypatch.setenv("USE_IAP", value)
    monkeypatch.setattr(ge.requests, "request", _reused_api(["old_mcp_data"]))

    ge.register(*ge.studio_url(), dry_run=False)

    assert ("USE_IAP=true" in caplog.text) is warned
    assert ("not IAP" in caplog.text) is warned


def test_full_resource_name_sets_project_and_regional_host(
    monkeypatch, gcloud_calls
):
    engine = (
        "projects/987/locations/eu/collections/default_collection/engines/a"
    )
    monkeypatch.setenv("GEMINI_ENTERPRISE_APP_ID", engine)
    api = FakeApi({})
    monkeypatch.setattr(ge.requests, "request", api)
    monkeypatch.setattr(ge, "create_collection", lambda *_: "c")
    monkeypatch.setattr(ge, "data_store_ids", lambda *_: ["c_mcp_data"])

    ge.register(*ge.studio_url(), dry_run=False)

    _, url, _, headers = api.calls[0]
    assert url == (
        "https://eu-discoveryengine.googleapis.com/v1alpha/"
        "projects/987/locations/eu/collections"
    )
    assert headers["X-Goog-User-Project"] == "987"
    assert ("projects", "describe", "987", "--format=value(projectNumber)") in (
        gcloud_calls
    )


def test_dry_run_changes_nothing(monkeypatch, gcloud_calls):
    api = _fresh_project_api()
    monkeypatch.setattr(ge.requests, "request", api)

    ge.register(*ge.studio_url(), dry_run=True)

    assert api.writes() == []
    writes = ("add-iam-policy-binding", "identity")
    assert not any(w in call for call in gcloud_calls for w in writes)


def test_api_failure_prints_the_console_steps(
    monkeypatch, gcloud_calls, caplog
):
    api = FakeApi(
        {
            ("GET", f"{DE}/v1alpha/{PARENT}/collections"): FakeResponse(
                {"error": {"message": "denied by org policy"}}, status=403
            )
        }
    )
    monkeypatch.setattr(ge.requests, "request", api)
    monkeypatch.setattr("sys.argv", ["gemini_enterprise.py"])

    assert ge.main() == 1
    assert "denied by org policy" in caplog.text
    assert f"URL {MCP}" in caplog.text
    assert "organisation policy" in caplog.text


def test_org_policy_block_prints_the_project_override(monkeypatch, caplog):
    def blocked():
        raise ge.GenMediaError(f"HTTP 400: constraints/{steps.POLICY} enforced")

    monkeypatch.setattr(ge, "studio_url", blocked)
    monkeypatch.setattr("sys.argv", ["gemini_enterprise.py"])
    with caplog.at_level("ERROR"):
        assert ge.main() == 1
    project = get_settings().project_id
    assert f"set-policy policy.yaml --project={project}" in caplog.text
    assert "Data stores > Create data store" not in caplog.text


def test_activation_retries_while_the_connector_is_initializing(
    monkeypatch, gcloud_calls, sleeps
):
    cid = "genmedia-studio-1700000000-collection"
    api = _fresh_project_api(cid)
    connector = f"{DE}/v1alpha/{PARENT}/collections/{cid}/dataConnector"
    busy = FakeResponse(
        {"error": {"message": "dataConnector is in INITIALIZING state."}}, 400
    )
    answers = iter([busy, busy])

    def request(method, url, headers, json, timeout):
        if (method, url) == ("PATCH", f"{connector}?updateMask=actionConfig"):
            api.calls.append((method, url, json, headers))
            return next(answers, FakeResponse({}))
        return api(method, url, headers, json, timeout)

    monkeypatch.setattr(ge.requests, "request", request)

    ge.register(*ge.studio_url(), dry_run=False)

    patches = [
        u for m, u, _ in api.writes() if u.endswith("updateMask=actionConfig")
    ]
    assert len(patches) == 3  # refused twice while initializing, then sent
    assert sleeps == [10, 10]


def _first_store_refused(cid, *after):
    """An existing app without data stores: the API refuses the first one;
    ``after`` are the app's GETs once the user has pressed Enter."""
    api = _fresh_project_api(cid)
    api.answers[("GET", f"{DE}/v1alpha/{ENGINE}")] = [
        {"displayName": "Team app"},
        *after,
    ]
    api.answers[("PATCH", f"{DE}/v1alpha/{ENGINE}")] = error(
        "FAILED_PRECONDITION", REFUSED
    )
    return api


def test_existing_app_limit_without_a_terminal_prints_the_step(
    monkeypatch, gcloud_calls, caplog
):
    cid = "genmedia-studio-1700000000-collection"
    api = _first_store_refused(cid)
    monkeypatch.setattr(ge.requests, "request", api)
    monkeypatch.setattr("sys.argv", ["gemini_enterprise.py"])

    assert ge.main() == 1

    assert "cannot add the first data store" in caplog.text
    assert REFUSED in caplog.text
    step = "Apps > Team app > Connected data stores > add GenMedia Studio"
    assert step in caplog.text
    assert f"{cid}_mcp_data" in caplog.text
    assert gea.RERUN in caplog.text
    assert "Data stores > Create data store" not in caplog.text
    assert not any("bapConfig" in u for _, u, _ in api.writes())


def test_existing_app_limit_waits_until_the_app_lists_the_store(
    monkeypatch, gcloud_calls, terminal, caplog
):
    cid = "genmedia-studio-1700000000-collection"
    linked = {"dataStoreIds": [f"{cid}_mcp_data"]}
    api = _first_store_refused(cid, {}, linked)  # not yet, then linked
    path = f"{DE}/v1alpha/{PARENT}/collections/{cid}/dataConnector"
    api.answers[("GET", path)] = connector_body(["studio_agent"], cid)
    monkeypatch.setattr(ge.requests, "request", api)
    terminal.tty = True

    ge.register(*ge.studio_url(), dry_run=False)

    assert terminal.prompts == [gea.ENTER, gea.ENTER]
    assert f"does not list data store {cid}_mcp_data yet" in caplog.text
    assert any("bapConfig" in u for _, u, _ in api.writes())


# Loaded: only studio_agent. Not loaded: no tools yet, or the tools of an
# older version (studio_open too) until the next "Reload custom actions".
@pytest.mark.parametrize(
    ("names", "loaded"),
    [
        (["studio_agent"], True),
        ([], False),
        (["studio_agent", "studio_open"], False),
    ],
)
def test_register_enables_the_action_and_reports_it(
    monkeypatch, gcloud_calls, caplog, names, loaded
):
    cid = "genmedia-studio-1700000000-collection"
    api = _fresh_project_api(cid)
    connector = f"{DE}/v1alpha/{PARENT}/collections/{cid}/dataConnector"
    tools = [{"name": n} for n in names]
    api.answers[("GET", connector)] = {
        "state": "ACTIVE",
        "entities": [_entity(cid)],
        "dynamicTools": tools,
    }
    monkeypatch.setattr(ge.requests, "request", api)

    run_args, mcp_url = ge.studio_url()
    with caplog.at_level("INFO"):
        ge.register(run_args, mcp_url, dry_run=False)

    assert (
        "PATCH",
        f"{connector}?updateMask=bapConfig",
        {"bapConfig": {"enabledActions": ["studio_agent"]}},
    ) in api.writes()
    assert ("is loaded and enabled" in caplog.text) is loaded
    assert ("Reload custom actions" in caplog.text) is not loaded
    assert (gea.RERUN in caplog.text) is not loaded
    assert ("Done:" in caplog.text) is loaded
    assert "licence" in caplog.text
