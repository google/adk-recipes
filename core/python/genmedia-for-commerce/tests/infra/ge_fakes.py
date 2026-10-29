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

"""Fakes of the Discovery Engine API and the terminal for the tests of
``infra/gemini_enterprise.py``."""

import json

from infra.gemini_enterprise_policy import POLICY

URL = "https://genmedia-for-commerce-123.us-central1.run.app"
MCP = f"{URL}/mcp"
DE = "https://discoveryengine.googleapis.com"
PARENT = "projects/my-proj/locations/global"
ENGINES = f"{PARENT}/collections/default_collection/engines"
ENGINE = f"{ENGINES}/my-app"  # GEMINI_ENTERPRISE_APP_ID=my-app
OWN = f"{ENGINES}/genmedia-for-commerce-studio"  # the command's own app
CID = "genmedia-studio-1700000000-collection"
CONNECTOR = f"{DE}/v1alpha/{PARENT}/collections/{CID}/dataConnector"
SETUP = f"{DE}/v1alpha/{PARENT}:setUpDataConnector"
REFUSED = (
    "Engines with multiple data stores can not remove data stores such "
    "that <2 data stores remain linked."
)


class FakeResponse:
    def __init__(self, body=None, status=200):
        self.status_code = status
        self.ok = status < 400
        self.text = json.dumps(body or {})
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


def error(status: str, message: str, code: int = 400) -> FakeResponse:
    """A Google API error response."""
    body = {"error": {"code": code, "status": status, "message": message}}
    return FakeResponse(body, code)


class FakeApi:
    """Answers by (method, url without query); records every request. An
    answer that is a list is a sequence: one item per call, the last repeats."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, json, timeout):
        self.calls.append((method, url, json, headers))
        answer = self.answers.get((method, url.split("?")[0]), {})
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return (
            answer if isinstance(answer, FakeResponse) else FakeResponse(answer)
        )

    def writes(self):
        """Every change (``invokeConnectorMcp`` is a POST that only reads)."""
        return [
            (m, u, b)
            for m, u, b, _ in self.calls
            if m != "GET" and not u.endswith(":invokeConnectorMcp")
        ]


class Terminal:
    """``sys.stdin`` and ``input``: not a terminal unless ``tty`` is set;
    ``input`` returns ``answer`` (Enter by default) and ``on_enter`` runs at
    each answer (it may change answers or raise)."""

    def __init__(self):
        self.tty = False
        self.prompts = []
        self.on_enter = None
        self.answer = ""

    def isatty(self):
        return self.tty

    def input(self, prompt=""):
        self.prompts.append(prompt)
        if len(self.prompts) > 20:  # a test that would wait forever
            raise RuntimeError(f"still waiting after: {prompt}")
        if self.on_enter:
            self.on_enter()
        return self.answer


# The connector refused by the organisation policy, then created
BLOCKED = error(
    "FAILED_PRECONDITION",
    f"CUSTOM_ORG_POLICY_VIOLATION: constraints/{POLICY} is enforced",
)
CREATED = {"name": "op", "done": True}


def entity(cid):
    store = f"{PARENT}/collections/default_collection/dataStores/{cid}_mcp_data"
    return {"entityName": "mcp_data", "dataStore": store}


def connector(tools=None, cid=CID):
    """The connector GET: its data store and the tools it has loaded."""
    body = {"state": "ACTIVE", "entities": [entity(cid)]}
    if tools is not None:
        body["dynamicTools"] = [{"name": t} for t in tools]
    return body


def fresh_project_api(cid=CID):
    """A project without a GenMedia Studio connector; the app ``my-app``
    has one data store."""
    path = f"{DE}/v1alpha/{PARENT}/collections/{cid}/dataConnector"
    return FakeApi(
        {
            ("GET", f"{DE}/v1alpha/{PARENT}/collections"): {"collections": []},
            ("POST", SETUP): {"name": f"{PARENT}/operations/op1", "done": True},
            ("GET", path): connector(cid=cid),
            ("GET", f"{DE}/v1alpha/{ENGINE}"): {"dataStoreIds": ["docs"]},
        }
    )
