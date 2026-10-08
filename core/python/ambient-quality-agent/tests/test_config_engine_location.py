# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The engine's region must survive a global `GOOGLE_CLOUD_LOCATION`."""

import pytest
from ambient_quality_agent import config as config_module

_REQUIRED_ENV = {
    "AQA_OBSERVED_AGENT_NAME": "watched-agent",
    "GOOGLE_CLOUD_PROJECT": "my-project",
}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"GOOGLE_CLOUD_LOCATION": "us-east4"}, "us-east4"),
        (
            {"GOOGLE_CLOUD_LOCATION": "global"},
            config_module.DEFAULT_ENGINE_LOCATION,
        ),
        (
            {"GOOGLE_CLOUD_LOCATION": "GLOBAL"},
            config_module.DEFAULT_ENGINE_LOCATION,
        ),
        ({}, config_module.DEFAULT_ENGINE_LOCATION),
        (
            {
                "GOOGLE_CLOUD_LOCATION": "global",
                "AQA_ENGINE_LOCATION": "europe-west4",
            },
            "europe-west4",
        ),
    ],
)
def test_engine_location(monkeypatch, env, expected):
    monkeypatch.delenv("AQA_ENGINE_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    for key, value in {**_REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(key, value)

    assert config_module.load().location == expected
