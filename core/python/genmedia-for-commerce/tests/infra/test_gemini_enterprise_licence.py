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

"""The registration offers Gemini Enterprise's free trial when the app's
location has no licence configuration."""

import pytest

from infra import gemini_enterprise_licence as lic

PARENT = "projects/my-proj/locations/global"
NAME = "projects/123/locations/global/licenseConfigs/free_trial_gemini"


class FakeApi:
    project, parent = "my-proj", PARENT

    def __init__(self, configs):
        self.configs, self.calls, self.waited = configs, [], []

    def call(self, method, path, body=None, version="v1alpha"):
        self.calls.append((method, path, body, version))
        if method == "GET":
            return {"licenseConfigs": self.configs} if self.configs else {}
        if path.startswith(f"{PARENT}/licenseConfigs"):
            return {
                "name": NAME,
                "startDate": {"year": 2026, "month": 10, "day": 29},
                "endDate": {"year": 2026, "month": 11, "day": 29},
            }
        return {"name": "operations/x", "done": True}

    def wait(self, operation):
        self.waited.append(operation)


@pytest.fixture
def terminal(monkeypatch):
    def answer(text):
        monkeypatch.setattr(
            lic.sys.stdin, "isatty", lambda: True, raising=False
        )
        monkeypatch.setattr(
            lic.sys.stdout, "isatty", lambda: True, raising=False
        )
        monkeypatch.setattr("builtins.input", lambda _prompt: text)

    return answer


def test_an_existing_licence_is_left_alone(terminal):
    terminal("")
    api = FakeApi([{"name": "paid"}])
    lic.ensure_licence(api, "me@example.com")
    assert [c[0] for c in api.calls] == ["GET"]


def test_enter_starts_the_trial_like_the_console(terminal, caplog):
    terminal("")
    api = FakeApi([])
    with caplog.at_level("INFO"):
        lic.ensure_licence(api, "me@example.com")
    (
        (_, create, body, version),
        (_, store, store_body, _),
        (_, assign, licences, _),
    ) = api.calls[1:]
    assert create.endswith("licenseConfigs?licenseConfigId=free_trial_gemini")
    assert body["freeTrial"] is True and version == "v1"
    assert "updateMask=defaultLicenseConfig,enableLicenseAutoRegister" in store
    assert store_body == {
        "defaultLicenseConfig": NAME,
        "enableLicenseAutoRegister": True,
    }
    assert assign.endswith(":batchUpdateUserLicenses")
    user = licences["inlineSource"]["userLicenses"][0]
    assert user == {"userPrincipal": "me@example.com", "licenseConfig": NAME}
    assert "Free trial started" in caplog.text and "2026-11-29" in caplog.text


def test_no_means_no_licence_and_says_how(terminal, caplog):
    terminal("n")
    api = FakeApi([])
    lic.ensure_licence(api, "me@example.com")
    assert [c[0] for c in api.calls] == ["GET"]
    assert "has no Gemini Enterprise licence" in caplog.text


def test_without_a_terminal_nothing_is_started(monkeypatch, caplog):
    monkeypatch.setattr(lic.sys.stdin, "isatty", lambda: False, raising=False)
    api = FakeApi([])
    lic.ensure_licence(api, "me@example.com")
    assert [c[0] for c in api.calls] == ["GET"]
    assert "Manage users" in caplog.text
