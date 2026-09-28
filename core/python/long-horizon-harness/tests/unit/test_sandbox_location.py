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

from app.sandbox.provider import _sandbox_location


def test_sandbox_location_defaults_to_us_central1(monkeypatch):
    monkeypatch.delenv("LHA_SANDBOX_LOCATION", raising=False)
    assert _sandbox_location() == "us-central1"


def test_sandbox_location_env_override(monkeypatch):
    monkeypatch.setenv("LHA_SANDBOX_LOCATION", "europe-west1")
    assert _sandbox_location() == "europe-west1"
